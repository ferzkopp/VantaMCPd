import { z } from "zod";
import { uploadLocalArtifact } from "../artifact-upload.js";
import { resolveTargets } from "../config.js";
import { errorText, json } from "../format.js";
import { capabilitySummary } from "../modules/catalog.js";
import { targetsSchema, timeoutSchema, type ToolContext, type ToolServer } from "./context.js";

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
        "Launch one installed module and discover its operations. By default this returns compact names and descriptions " +
        "without JSON Schemas. Pass toolName to return one operation's full argument schema; use includeSchemas=true only " +
        "when every schema is genuinely needed. " +
        "When target is omitted, selects a reachable installation automatically." +
        offered,
      inputSchema: {
        moduleId: z.string().describe("Installed module ID."),
        target: z.string().optional().describe("Optional explicit node name; omit to select an installed instance."),
        toolName: z.string().optional().describe("Optional exact operation name; returns only that tool with its full argument schema."),
        includeSchemas: z.boolean().default(false)
          .describe("Include full schemas for every tool. Leave false for compact discovery; toolName always includes its schema."),
      },
    },
    async ({ moduleId, target, toolName, includeSchemas }) => {
      try {
        return json(await ctx.modules.listTools(
          moduleId,
          target === undefined ? undefined : resolveSingleTarget(ctx, target),
          toolName,
          includeSchemas,
        ));
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
        "Use cluster_list_module_tools for compact operation discovery, then pass toolName there when you need one " +
        "exact argument schema. Do this before retrying whenever a module tool rejects an argument or returns an empty result, because module tools " +
        "accept options and query syntax that this description does not repeat. " +
        "Tools listed with execution \"optional\" or \"required\" can run as a durable background job with " +
        "execution=\"background\": use it for long computations, then poll cluster_get_job and read the output with includeResult.",
      inputSchema: {
        moduleId: z.string().describe("Installed module ID."),
        target: z.string().optional().describe("Optional explicit node name; omit to use module routing."),
        toolName: z.string().describe("Module operation name; use compact cluster_list_module_tools discovery if unknown."),
        arguments: z.record(z.unknown()).default({}).describe("Arguments passed to the remote module tool."),
        execution: z
          .enum(["immediate", "background"])
          .default("immediate")
          .describe("immediate returns the result; background submits a durable job and returns its jobId."),
      },
    },
    async ({ moduleId, target, toolName, arguments: args, execution }) => {
      try {
        const result = await ctx.modules.callTool(
          moduleId,
          target === undefined ? undefined : resolveSingleTarget(ctx, target),
          toolName,
          args,
          execution,
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
    async (args, extra) => {
      try {
        return json(await uploadLocalArtifact(ctx, args, extra.signal));
      } catch (err) {
        return errorText(err);
      }
    },
  );
}