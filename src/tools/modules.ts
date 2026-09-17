import { createHash } from "node:crypto";
import { createReadStream, existsSync, openSync, closeSync, readSync, statSync } from "node:fs";
import path from "node:path";
import { z } from "zod";
import { expandHome, resolveTargets } from "../config.js";
import { errorText, json } from "../format.js";
import { capabilitySummary } from "../modules/catalog.js";
import { targetsSchema, timeoutSchema, type ToolContext, type ToolServer } from "./context.js";

const ARTIFACT_CHUNK_BYTES = 512 * 1024;

function localPath(value: string): string {
  if (value.includes("\u0000")) throw new Error("Local path contains a NUL byte.");
  return path.resolve(expandHome(value));
}

function objectResult(value: unknown, operation: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error(`artifact_upload ${operation} returned an invalid response.`);
  }
  return value as Record<string, unknown>;
}

async function fileSha256(file: string): Promise<string> {
  const digest = createHash("sha256");
  for await (const chunk of createReadStream(file)) digest.update(chunk);
  return digest.digest("hex");
}

function resolveSingleTarget(ctx: ToolContext, target: string) {
  const nodes = resolveTargets(ctx.config, [target]);
  if (nodes.length !== 1) {
    throw new Error(`Target ${JSON.stringify(target)} resolves to ${nodes.length} nodes; specify one node name.`);
  }
  return nodes[0]!;
}

export function registerModuleTools(server: ToolServer, ctx: ToolContext): void {
  const capabilities = capabilitySummary(ctx.modules.catalog);
  const offered = capabilities ? `\nAvailable module capabilities (check installation with cluster_list_modules):\n${capabilities}\n` : "";
  server.registerTool(
    "cluster_list_modules",
    {
      title: "List node MCP modules",
      description:
        "List validated local module packages, deployment policies, recorded compatibility, and live receipt versions/update state " +
        "on target nodes. Use cluster_check_module for live disk and required-command checks.",
      inputSchema: { targets: targetsSchema },
    },
    async ({ targets }) => {
      try {
        return json(await ctx.modules.list(resolveTargets(ctx.config, targets)));
      } catch (err) {
        return errorText(err);
      }
    },
  );

  server.registerTool(
    "cluster_check_module",
    {
      title: "Check node MCP module compatibility",
      description:
        "Evaluate one local module against recorded node capabilities and live available disk/required-command checks. " +
        "Supports heterogeneous CPU and accelerator nodes; an unknown inventory fact is not treated as compatible.",
      inputSchema: {
        moduleId: z.string().describe("Module ID from cluster_list_modules."),
        targets: targetsSchema,
        timeoutMs: timeoutSchema,
      },
    },
    async ({ moduleId, targets, timeoutMs }) => {
      try {
        const nodes = resolveTargets(ctx.config, targets);
        const modulePackage = ctx.modules.get(moduleId);
        return json({
          module: {
            id: modulePackage.manifest.id,
            name: modulePackage.manifest.name,
            version: modulePackage.manifest.version,
          },
          nodes: await ctx.modules.check(moduleId, nodes, timeoutMs),
        });
      } catch (err) {
        return errorText(err);
      }
    },
  );

  server.registerTool(
    "cluster_install_module",
    {
      title: "Install a node MCP module",
      description:
        "Install one trusted local module on explicit target nodes after compatibility and live preflight checks. " +
        "Requires confirm=true and never defaults to every node.",
      inputSchema: {
        moduleId: z.string().describe("Module ID from cluster_list_modules."),
        targets: z.array(z.string()).min(1).describe("Explicit node names or tags. The value 'all' is not accepted."),
        options: z.record(z.unknown()).optional().describe("Module-defined options advertised by cluster_list_modules."),
        confirm: z.boolean().optional().describe("Must be true to authorize remote installation."),
        timeoutMs: timeoutSchema,
      },
    },
    async ({ moduleId, targets, options, confirm, timeoutMs }) => {
      try {
        if (confirm !== true) throw new Error("Installation requires confirm: true.");
        if (targets.includes("all")) throw new Error("Installation requires explicit node names or tags; 'all' is not accepted.");
        const nodes = resolveTargets(ctx.config, targets);
        return json({ nodes: await ctx.modules.install(moduleId, nodes, timeoutMs, options) });
      } catch (err) {
        return errorText(err);
      }
    },
  );

  server.registerTool(
    "cluster_uninstall_module",
    {
      title: "Uninstall a node MCP module",
      description:
        "Remove one installed module and its installation receipt from explicit target nodes. " +
        "Requires confirm=true and never defaults to every node.",
      inputSchema: {
        moduleId: z.string().describe("Module ID from cluster_list_modules."),
        targets: z.array(z.string()).min(1).describe("Explicit node names or tags. The value 'all' is not accepted."),
        confirm: z.boolean().optional().describe("Must be true to authorize remote uninstallation."),
        timeoutMs: timeoutSchema,
      },
    },
    async ({ moduleId, targets, confirm, timeoutMs }) => {
      try {
        if (confirm !== true) throw new Error("Uninstallation requires confirm: true.");
        if (targets.includes("all")) throw new Error("Uninstallation requires explicit node names or tags; 'all' is not accepted.");
        const nodes = resolveTargets(ctx.config, targets);
        return json({ nodes: await ctx.modules.uninstall(moduleId, nodes, timeoutMs) });
      } catch (err) {
        return errorText(err);
      }
    },
  );

  server.registerTool(
    "cluster_purge_module_data",
    {
      title: "Purge retained node module data",
      description:
        "Permanently remove a module's retained data from one explicit storage node. The module must be uninstalled, " +
        "no lifecycle job may be active, and confirm=true is required.",
      inputSchema: {
        moduleId: z.string().describe("Module ID from cluster_list_modules."),
        target: z.string().describe("One explicit storage node name."),
        confirm: z.boolean().optional().describe("Must be true to authorize permanent data removal."),
      },
    },
    async ({ moduleId, target, confirm }) => {
      try {
        if (confirm !== true) throw new Error("Persistent data purge requires confirm: true.");
        return json(await ctx.modules.purgeData(moduleId, resolveSingleTarget(ctx, target)));
      } catch (err) {
        return errorText(err);
      }
    },
  );

  server.registerTool(
    "cluster_list_module_tools",
    {
      title: "List tools from an installed node MCP module",
      description:
        "Launch one installed module on demand over SSH stdio and return its MCP tools/list response. " +
        "Call this to discover the exact operations and argument schemas a module provides before using it. " +
        "When target is omitted, selects a reachable installation automatically." +
        offered,
      inputSchema: {
        moduleId: z.string().describe("Installed module ID."),
        target: z.string().optional().describe("Optional explicit node name; omit to select an installed instance."),
      },
    },
    async ({ moduleId, target }) => {
      try {
        return json(await ctx.modules.listTools(moduleId, target === undefined ? undefined : resolveSingleTarget(ctx, target)));
      } catch (err) {
        return errorText(err);
      }
    },
  );

  server.registerTool(
    "cluster_call_module_tool",
    {
      title: "Call a tool from an installed node MCP module",
      description:
        "Run a workload on the cluster through an installed module. Prefer this over answering from memory " +
        "when a request matches a capability below, because these operations are deterministic, bounded, and auditable. " +
        "When target is omitted, replicated modules use round-robin routing across reachable installations. " +
        "Inputs are sent as MCP data and are never interpolated into a shell command." +
        offered +
        "Use cluster_list_module_tools for the exact operation names and argument schemas. Call it before " +
        "retrying whenever a module tool rejects an argument or returns an empty result, because module tools " +
        "accept options and query syntax that this description does not repeat.",
      inputSchema: {
        moduleId: z.string().describe("Installed module ID."),
        target: z.string().optional().describe("Optional explicit node name; omit to use module routing."),
        toolName: z.string().describe("Tool name advertised by cluster_list_module_tools."),
        arguments: z.record(z.unknown()).default({}).describe("Arguments passed to the remote module tool."),
      },
    },
    async ({ moduleId, target, toolName, arguments: args }) => {
      try {
        const result = await ctx.modules.callTool(
          moduleId,
          target === undefined ? undefined : resolveSingleTarget(ctx, target),
          toolName,
          args,
        );
        return json(result, !result.ok);
      } catch (err) {
        return errorText(err);
      }
    },
  );

  server.registerTool(
    "cluster_upload_artifact",
    {
      title: "Upload a local file as an artifact",
      description:
        "Read an existing local file on the VantaMCPd host and stream its original bytes through the installed " +
        "artifact-storage module using bounded chunks and SHA-256 verification. Returns an immutable artifact ID " +
        "for artifact-aware module calls. Use this for workspace files and attachments that the MCP client exposes " +
        "with a local path; chat-only pasted content must first be saved or otherwise exposed by the client.",
      inputSchema: {
        localPath: z.string().describe("Path to an existing local file on the VantaMCPd host."),
        target: z.string().optional().describe("Optional node hosting artifact-storage; omit to select its installation."),
        name: z.string().min(1).max(128).regex(/^[^/\\\u0000-\u001f\u007f]+$/).optional()
          .describe("Artifact filename. Defaults to the local basename."),
        mimeType: z.string().min(1).max(200).regex(/^\S+$/).optional()
          .describe("Optional MIME type. When omitted, artifact-storage infers it from the filename."),
        producer: z.string().regex(/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/).optional()
          .describe("Quota/audit producer name. Default vanta-local."),
        retentionDays: z.number().int().positive().optional()
          .describe("Optional retention period, bounded by the artifact-storage installation."),
      },
    },
    async ({ localPath: local, target, name, mimeType, producer, retentionDays }) => {
      let uploadId: string | undefined;
      try {
        const resolved = localPath(local);
        if (!existsSync(resolved) || !statSync(resolved).isFile()) throw new Error(`Local file not found: ${resolved}`);
        const fileSize = statSync(resolved).size;
        const digest = await fileSha256(resolved);
        const selectedNode = target === undefined ? undefined : resolveSingleTarget(ctx, target);
        const artifactCall = async (arguments_: Record<string, unknown>) => {
          const result = await ctx.modules.callTool("artifact-storage", selectedNode, "artifact_upload", arguments_);
          if (!result.ok) {
            const detail = typeof result.output === "string" ? result.output : JSON.stringify(result.output);
            throw new Error(detail || `artifact_upload ${String(arguments_.operation)} failed.`);
          }
          return objectResult(result.output, String(arguments_.operation));
        };
        const begin = await artifactCall({
          operation: "begin",
          name: name ?? path.basename(resolved),
          bytes: fileSize,
          ...(mimeType === undefined ? {} : { mimeType }),
          producer: producer ?? "vanta-local",
          ...(retentionDays === undefined ? {} : { retentionDays }),
          sha256: digest,
        });
        if (typeof begin.uploadId !== "string") throw new Error("artifact_upload begin did not return an uploadId.");
        uploadId = begin.uploadId;
        const chunkLimit = typeof begin.chunkLimitBytes === "number"
          ? Math.min(begin.chunkLimitBytes, ARTIFACT_CHUNK_BYTES)
          : ARTIFACT_CHUNK_BYTES;
        if (!Number.isInteger(chunkLimit) || chunkLimit < 1) throw new Error("artifact_upload returned an invalid chunk limit.");

        const handle = openSync(resolved, "r");
        let offset = 0;
        let chunks = 0;
        try {
          while (offset < fileSize) {
            const buffer = Buffer.allocUnsafe(Math.min(chunkLimit, fileSize - offset));
            const bytesRead = readSync(handle, buffer, 0, buffer.length, offset);
            if (bytesRead === 0) throw new Error(`Local file ended unexpectedly at byte ${offset}.`);
            const chunk = bytesRead === buffer.length ? buffer : buffer.subarray(0, bytesRead);
            const append = await artifactCall({
              operation: "append",
              uploadId,
              offset,
              data: chunk.toString("base64"),
              sha256: createHash("sha256").update(chunk).digest("hex"),
            });
            const expectedOffset = offset + bytesRead;
            if (append.nextOffset !== expectedOffset) {
              throw new Error(`artifact_upload append returned offset ${String(append.nextOffset)}; expected ${expectedOffset}.`);
            }
            offset = expectedOffset;
            chunks += 1;
          }
        } finally {
          closeSync(handle);
        }

        const committed = await artifactCall({ operation: "commit", uploadId });
  if (typeof committed.id !== "string") throw new Error("artifact_upload commit did not return an artifact ID.");
        uploadId = undefined;
  return json({ ...committed, artifactId: committed.id, chunks });
      } catch (err) {
        if (uploadId !== undefined) {
          try {
            const selectedNode = target === undefined ? undefined : resolveSingleTarget(ctx, target);
            await ctx.modules.callTool("artifact-storage", selectedNode, "artifact_upload", { operation: "abort", uploadId });
          } catch {
            // Preserve the original upload failure; artifact-storage garbage-collects abandoned sessions.
          }
        }
        return errorText(err);
      }
    },
  );
}