import { createHash } from "node:crypto";
import { createReadStream } from "node:fs";
import { open, stat } from "node:fs/promises";
import path from "node:path";
import { expandHome, resolveTargets } from "./config.js";
import type { ResolvedNode } from "./config.js";
import type { ModuleToolCallResult } from "./modules/manager.js";
import type { ToolContext } from "./tools/context.js";

export interface ArtifactUploadOptions {
  localPath: string;
  target?: string;
  name?: string;
  mimeType?: string;
  producer?: string;
  retentionDays?: number;
  sha256?: string;
}

type ArtifactCall = (args: Record<string, unknown>, node?: ResolvedNode) => Promise<ModuleToolCallResult>;

export async function withArtifactSession<T>(ctx: ToolContext, node: ResolvedNode | undefined, toolName: string,
  operation: (call: ArtifactCall) => Promise<T>, signal?: AbortSignal): Promise<T> {
  const storageNodes = ctx.config.nodes.filter((candidate) => candidate.storage);
  const selectedNode = node ?? (ctx.config.artifacts?.storageNode
    ? ctx.config.nodes.find((candidate) => candidate.name === ctx.config.artifacts.storageNode)
    : storageNodes.length === 1 ? storageNodes[0] : undefined);
  if (typeof ctx.modules.withToolSession !== "function") {
    return operation((args, selected) => ctx.modules.callTool("artifact-storage", selected ?? selectedNode, toolName, args));
  }
  return ctx.modules.withToolSession("artifact-storage", selectedNode, [toolName],
    (call) => operation((args) => call(toolName, args)), signal);
}

export async function uploadLocalArtifact(ctx: ToolContext, options: ArtifactUploadOptions, signal?: AbortSignal) {
  const nodes = options.target === undefined ? undefined : resolveTargets(ctx.config, [options.target]);
  if (nodes && nodes.length !== 1) throw new Error("Artifact target must resolve to one node.");
  return withArtifactSession(ctx, nodes?.[0], "artifact_upload", (call) => uploadArtifactInSession(ctx, options, call, signal), signal);
}

async function uploadArtifactInSession(ctx: ToolContext, options: ArtifactUploadOptions, call: ArtifactCall, signal?: AbortSignal) {
  if (options.localPath.includes("\u0000")) throw new Error("Local path contains a NUL byte.");
  const file = path.resolve(expandHome(options.localPath));
  const info = await stat(file);
  if (!info.isFile()) throw new Error("Artifact input must be a regular file.");
  let sha256 = options.sha256;
  if (sha256 !== undefined && !/^[0-9a-f]{64}$/.test(sha256)) throw new Error("Invalid staged file checksum.");
  if (sha256 === undefined) {
    const digest = createHash("sha256");
    for await (const chunk of createReadStream(file, { signal })) digest.update(chunk);
    sha256 = digest.digest("hex");
  }
  const nodes = options.target === undefined ? undefined : resolveTargets(ctx.config, [options.target]);
  if (nodes && nodes.length !== 1) throw new Error("Artifact target must resolve to one node.");
  let selectedNode = nodes?.[0];
  let uploadId: string | undefined;
  const invoke = async (args: Record<string, unknown>, abort = false) => {
    if (!abort) signal?.throwIfAborted();
    const result = abort && signal?.aborted
      ? await ctx.modules.callTool("artifact-storage", selectedNode, "artifact_upload", args)
      : await call(args, selectedNode);
    if (!result.ok) throw new Error(typeof result.output === "string" ? result.output : JSON.stringify(result.output));
    if (!selectedNode && result.node) selectedNode = ctx.config.nodes.find((node) => node.name === result.node);
    if (!result.output || typeof result.output !== "object" || Array.isArray(result.output)) {
      throw new Error(`artifact_upload ${String(args.operation)} returned an invalid response.`);
    }
    return result.output as Record<string, unknown>;
  };
  try {
    const begin = await invoke({ operation: "begin", name: options.name ?? path.basename(file), bytes: info.size,
      producer: options.producer ?? "vanta-local", sha256,
      ...(options.mimeType === undefined ? {} : { mimeType: options.mimeType }),
      ...(options.retentionDays === undefined ? {} : { retentionDays: options.retentionDays }) });
    if (typeof begin.uploadId !== "string") throw new Error("artifact_upload begin did not return an uploadId.");
    uploadId = begin.uploadId;
    const chunkLimit = typeof begin.chunkLimitBytes === "number" ? Math.min(begin.chunkLimitBytes, 512 * 1024) : 512 * 1024;
    if (!Number.isInteger(chunkLimit) || chunkLimit < 1) throw new Error("artifact_upload returned an invalid chunk limit.");
    const handle = await open(file, "r");
    let offset = 0;
    let chunks = 0;
    try {
      while (offset < info.size) {
        signal?.throwIfAborted();
        const buffer = Buffer.allocUnsafe(Math.min(chunkLimit, info.size - offset));
        const { bytesRead } = await handle.read(buffer, 0, buffer.length, offset);
        if (bytesRead === 0) throw new Error(`Local file ended unexpectedly at byte ${offset}.`);
        const chunk = buffer.subarray(0, bytesRead);
        const append = await invoke({ operation: "append", uploadId, offset, data: chunk.toString("base64"),
          sha256: createHash("sha256").update(chunk).digest("hex") });
        if (append.nextOffset !== offset + bytesRead) throw new Error("artifact_upload append returned an unexpected offset.");
        offset += bytesRead;
        chunks++;
      }
    } finally {
      await handle.close();
    }
    const committed = await invoke({ operation: "commit", uploadId });
    if (typeof committed.id !== "string") throw new Error("artifact_upload commit did not return an artifact ID.");
    uploadId = undefined;
    return { ...committed, artifactId: committed.id, chunks };
  } catch (error) {
    if (uploadId) {
      try { await invoke({ operation: "abort", uploadId }, true); } catch {}
    }
    throw error;
  }
}