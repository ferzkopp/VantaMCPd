#!/usr/bin/env node
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { AuditLog, withToolParameters } from "./audit.js";
import { loadConfig, loadEnvFile } from "./config.js";
import { discoverMissingHardware } from "./hardware.js";
import { JobManager } from "./jobs/manager.js";
import { JobRegistry } from "./jobs/registry.js";
import { ModuleManager } from "./modules/manager.js";
import { capabilitySummary } from "./modules/catalog.js";
import { SshPool } from "./ssh.js";
import type { ToolContext, ToolServer } from "./tools/context.js";
import { registerExecTools } from "./tools/exec.js";
import { registerFileTools } from "./tools/files.js";
import { registerLogTools } from "./tools/logs.js";
import { registerJobTools } from "./tools/jobs.js";
import { registerModuleTools } from "./tools/modules.js";
import { registerPackageTools } from "./tools/packages.js";
import { registerServiceTools } from "./tools/services.js";
import { registerStorageTools } from "./tools/storage.js";
import { registerSwapTools } from "./tools/swap.js";
import { registerSystemTools } from "./tools/system.js";
import { startWebServer } from "./web.js";

/** Run every tool handler inside an async context so SSH calls can be attributed to the tool that caused them. */
function attributedRegistrar(server: McpServer): ToolServer {
  type Register = ToolServer["registerTool"];
  const registerTool = ((name: string, config: never, handler: (...args: unknown[]) => unknown) =>
    server.registerTool(
      name,
      config,
      ((...args: unknown[]) => withToolParameters(name, args[0], () => handler(...args))) as never,
    )) as Register;
  return { registerTool };
}

async function main(): Promise<void> {
  loadEnvFile(process.env.VANTA_ENV_FILE ?? ".env");

  const config = loadConfig();
  const audit = config.monitoring.enabled
    ? new AuditLog({
        logDir: config.monitoring.logDir,
        maxEvents: config.monitoring.maxEvents,
        maxLogMb: config.monitoring.maxLogMb,
        logOutput: config.monitoring.logOutput,
      })
    : undefined;
  const pool = new SshPool(config, audit);
  const jobRegistry = new JobRegistry();
  jobRegistry.register("module-install");
  const jobs = new JobManager(config, pool, jobRegistry);
  const modules = new ModuleManager(config, pool, undefined, jobs);
  const ctx: ToolContext = { config, pool, jobs, modules };

  const capabilities = capabilitySummary(modules.catalog);
  const server = new McpServer(
    { name: "vantamcpd", version: "0.1.0" },
    {
      instructions:
        "Manages a heterogeneous cluster of Debian/Armbian nodes over SSH.\n" +
        "Call cluster_list_nodes first to learn the available node names, roles, tags and recorded hardware " +
        "(CPU cores/architecture, GPU/accelerators, memory, disks, OS); use cluster_hardware for the full detail or to re-probe. " +
        "Most tools accept a `targets` array of node names, tags, or omit it to hit every node.\n" +
        "These nodes are resource constrained: check the hardware inventory before installing anything, prefer dry runs " +
        "for apt operations, avoid long-running foreground builds, and check free disk space with cluster_status.\n" +
        "Destructive operations (formatting, reboots, rm -rf, partitioning) require explicit user approval and a confirm flag.\n" +
        (capabilities
          ? "The cluster also runs node modules that do real work for you. Route a request to cluster_call_module_tool " +
            "whenever it matches one of these capabilities, even if the user does not name the module or the node:\n" +
            `${capabilities}\n` +
            "These operations are deterministic, bounded and auditable, so prefer them over answering from memory for " +
            "conversion, extraction, redaction, comparison and formatting work. Use cluster_list_modules to confirm " +
            "what is installed, and cluster_list_module_tools for exact operation names and argument schemas.\n"
          : "") +
        (config.monitoring.enabled && config.monitoring.web
          ? `Every SSH interaction is logged and shown live on a local dashboard at ` +
            `http://127.0.0.1:${config.monitoring.port} - mention it when the user asks what you did, ` +
            `wants to watch progress, or is debugging. cluster_list_nodes reports whether it is actually running.`
          : ""),
    },
  );

  const tools = attributedRegistrar(server);
  registerSystemTools(tools, ctx);
  registerExecTools(tools, ctx);
  registerLogTools(tools, ctx);
  registerPackageTools(tools, ctx);
  registerServiceTools(tools, ctx);
  registerFileTools(tools, ctx);
  registerStorageTools(tools, ctx);
  registerSwapTools(tools, ctx);
  registerJobTools(tools, ctx);
  registerModuleTools(tools, ctx);
  const web = audit && config.monitoring.web ? startWebServer(config, audit, modules, jobs) : undefined;
  // Only advertise the dashboard once the socket is genuinely bound - it may lose a port race.
  web?.once("listening", () => {
    ctx.monitor = { url: `http://127.0.0.1:${config.monitoring.port}`, logDir: config.monitoring.logDir };
  });

  const shutdown = () => {
    web?.close();
    jobs.dispose();
    pool.disposeAll();
    process.exit(0);
  };
  process.on("SIGINT", shutdown);
  process.on("SIGTERM", shutdown);

  await server.connect(new StdioServerTransport());
  // stdout is the MCP transport - diagnostics must go to stderr only.
  process.stderr.write(`vantamcpd ready: ${config.nodes.length} node(s) from ${config.configPath}\n`);

  // Complete discovery and module reconciliation without delaying MCP availability.
  void (async () => {
    try {
      await jobs.reconcile();
      jobs.start();
    } catch (err) {
      process.stderr.write(`job reconciliation failed: ${(err as Error).message}\n`);
    }
    if (config.autoDiscoverHardware) await discoverMissingHardware(config, pool);
    if (!config.autoUpdateModules) return;
    try {
      const updates = await modules.updateOutdatedModules();
      for (const update of updates) {
        const detail = update.updated
          ? `updated ${update.fromVersion} -> ${update.toVersion}` +
            (update.oldVersionRemoved ? "" : `; ${update.error ?? "old version was not removed"}`)
          : `update ${update.fromVersion} -> ${update.toVersion} failed: ${update.error ?? "unknown error"}`;
        process.stderr.write(`module discovery: ${update.moduleId} on ${update.node}: ${detail}\n`);
      }
    } catch (err) {
      process.stderr.write(`module discovery failed: ${(err as Error).message}\n`);
    }
  })();
}

main().catch((err: unknown) => {
  process.stderr.write(`vantamcpd failed to start: ${err instanceof Error ? err.stack ?? err.message : String(err)}\n`);
  process.exit(1);
});
