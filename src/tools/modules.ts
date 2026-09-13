import { z } from "zod";
import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { resolveTargets } from "../config.js";
import { errorText, json } from "../format.js";
import { targetsSchema, timeoutSchema, type ToolContext } from "./context.js";

function resolveSingleTarget(ctx: ToolContext, target: string) {
  const nodes = resolveTargets(ctx.config, [target]);
  if (nodes.length !== 1) {
    throw new Error(`Target ${JSON.stringify(target)} resolves to ${nodes.length} nodes; specify one node name.`);
  }
  return nodes[0]!;
}

export function registerModuleTools(server: McpServer, ctx: ToolContext): void {
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
        confirm: z.boolean().optional().describe("Must be true to authorize remote installation."),
        timeoutMs: timeoutSchema,
      },
    },
    async ({ moduleId, targets, confirm, timeoutMs }) => {
      try {
        if (confirm !== true) throw new Error("Installation requires confirm: true.");
        if (targets.includes("all")) throw new Error("Installation requires explicit node names or tags; 'all' is not accepted.");
        const nodes = resolveTargets(ctx.config, targets);
        return json({ nodes: await ctx.modules.install(moduleId, nodes, timeoutMs) });
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
    "cluster_list_module_tools",
    {
      title: "List tools from an installed node MCP module",
      description:
        "Launch one installed module on demand over SSH stdio and return its MCP tools/list response. " +
        "When target is omitted, selects a reachable installation automatically.",
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
        "Launch one installed module over SSH stdio and call a tool that it advertises. " +
        "When target is omitted, replicated modules use round-robin routing across reachable installations. " +
        "Inputs are sent as MCP data and are never interpolated into a shell command.",
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
}