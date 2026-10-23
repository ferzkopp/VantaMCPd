import { createHash } from "node:crypto";
import { open } from "node:fs/promises";
import type { ResolvedNode } from "./config.js";
import type { ToolContext } from "./tools/context.js";
import { withArtifactSession } from "./artifact-upload.js";
import type { ModuleToolCallResult } from "./modules/manager.js";

export async function downloadArtifact(ctx: ToolContext, artifactId: string, file: string, maximumBytes: number, signal?: AbortSignal) {
  if (!/^[0-9a-f]{32}$/.test(artifactId)) throw new Error("Invalid artifact ID.");
  return withArtifactSession(ctx, undefined, "artifact_fetch",
    (call) => downloadInSession(ctx, artifactId, file, maximumBytes, call, signal), signal);
}

async function downloadInSession(ctx: ToolContext, artifactId: string, file: string, maximumBytes: number,
  call: (args: Record<string, unknown>, node?: ResolvedNode) => Promise<ModuleToolCallResult>, signal?: AbortSignal) {
  let selectedNode: ResolvedNode | undefined;
  const invoke = async (args: Record<string, unknown>) => {
    signal?.throwIfAborted();
    const result = await call({ ...args, artifactId }, selectedNode);
    if (!result.ok) throw new Error("Artifact download failed; inspect artifact-storage availability and expiry.");
    if (!selectedNode) {
      selectedNode = ctx.config.nodes.find((node) => node.name === result.node);
      if (!selectedNode) throw new Error("Artifact download did not identify its storage node.");
    }
    if (!result.output || typeof result.output !== "object" || Array.isArray(result.output)) throw new Error("Invalid artifact response.");
    return result.output as Record<string, unknown>;
  };
  const info = await invoke({ operation: "info" });
  if (info.id !== artifactId || typeof info.bytes !== "number" || !Number.isSafeInteger(info.bytes) || info.bytes < 0 ||
      info.bytes > maximumBytes || typeof info.sha256 !== "string" || !/^[0-9a-f]{64}$/.test(info.sha256)) {
    throw new Error("Artifact metadata is invalid or exceeds the source transfer limit.");
  }
  const handle = await open(file, "wx");
  const digest = createHash("sha256");
  try {
    let offset = 0;
    while (offset < info.bytes) {
      const length = Math.min(256 * 1024, info.bytes - offset);
      const result = await invoke({ operation: "read", offset, length });
      if (typeof result.data !== "string" || result.data.length > Math.ceil(length / 3) * 4) throw new Error("Invalid artifact chunk.");
      const chunk = Buffer.from(result.data, "base64");
      if (result.artifactId !== artifactId || result.encoding !== "base64" || result.offset !== offset || chunk.length !== length ||
          result.bytes !== chunk.length || result.nextOffset !== offset + chunk.length || chunk.toString("base64") !== result.data ||
          createHash("sha256").update(chunk).digest("hex") !== result.sha256) throw new Error("Artifact chunk verification failed.");
      await handle.writeFile(chunk);
      digest.update(chunk);
      offset += chunk.length;
    }
    signal?.throwIfAborted();
    if (digest.digest("hex") !== info.sha256) throw new Error("Artifact checksum verification failed.");
    return { bytes: info.bytes, sha256: info.sha256 };
  } finally {
    await handle.close();
  }
}