import { z } from "zod";
import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import type { ClusterConfig } from "../config.js";
import type { JobManager } from "../jobs/manager.js";
import type { ModuleManager } from "../modules/manager.js";
import type { SshPool } from "../ssh.js";

/**
 * The only part of the MCP server surface the tool modules need. Registering against this lets the
 * entry point wrap registration (for audit attribution) without patching the SDK object itself.
 */
export type ToolServer = Pick<McpServer, "registerTool">;

export interface ToolContext {
  config: ClusterConfig;
  pool: SshPool;
  jobs: JobManager;
  modules: ModuleManager;
  /** Set once the dashboard is actually listening, so tools report what is really running. */
  monitor?: { url: string; logDir: string };
}

export const targetsSchema = z
  .array(z.string())
  .optional()
  .describe('Node names (e.g. ["cluster1","cluster4"]), tags (e.g. ["storage"]), or omit / ["all"] for every node.');

export const timeoutSchema = z
  .number()
  .int()
  .positive()
  .max(3_600_000)
  .optional()
  .describe("Per-node timeout in milliseconds. Defaults to the config value (120000).");
