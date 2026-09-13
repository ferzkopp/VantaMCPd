import { z } from "zod";
import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { resolveTargets } from "../config.js";
import { errorText, renderResults } from "../format.js";
import { assertNoControlChars, q, validateUnit } from "../security.js";
import { targetsSchema, timeoutSchema, type ToolContext } from "./context.js";

const READ_ONLY = new Set(["status", "is_active", "is_enabled", "list", "list_failed", "show"]);

export function registerServiceTools(server: McpServer, ctx: ToolContext): void {
  server.registerTool(
    "cluster_services",
    {
      title: "Manage systemd services",
      description:
        "Inspect and control systemd units across the cluster: status, start, stop, restart, reload, enable, disable, mask/unmask, daemon-reload, and listing units or failed units.",
      inputSchema: {
        action: z.enum([
          "status",
          "is_active",
          "is_enabled",
          "list",
          "list_failed",
          "show",
          "start",
          "stop",
          "restart",
          "reload",
          "enable",
          "disable",
          "mask",
          "unmask",
          "daemon_reload",
          "reset_failed",
        ]),
        targets: targetsSchema,
        unit: z.string().optional().describe('Unit name, e.g. "ssh", "nfs-server", "docker.service".'),
        pattern: z.string().optional().describe('Filter for action="list", e.g. "nfs*".'),
        now: z.boolean().optional().describe("For enable/disable: also start/stop the unit immediately (--now)."),
        lines: z.number().int().min(0).max(200).optional().describe("Journal lines to include with status. Default 20."),
        timeoutMs: timeoutSchema,
      },
    },
    async ({ action, targets, unit, pattern, now, lines, timeoutMs }) => {
      try {
        const nodes = resolveTargets(ctx.config, targets);
        const needsUnit = !["list", "list_failed", "daemon_reload", "reset_failed"].includes(action);
        if (needsUnit && !unit) throw new Error(`action="${action}" requires a "unit".`);
        const u = unit ? q(validateUnit(unit)) : "";
        const nowFlag = now ? " --now" : "";

        let command: string;
        switch (action) {
          case "status":
            command = `systemctl status ${u} --no-pager -n ${lines ?? 20}`;
            break;
          case "is_active":
            command = `systemctl is-active ${u}; systemctl is-enabled ${u} 2>/dev/null || true`;
            break;
          case "is_enabled":
            command = `systemctl is-enabled ${u}`;
            break;
          case "show":
            command = `systemctl show ${u} --no-pager -p Id,Description,LoadState,ActiveState,SubState,UnitFileState,MainPID,MemoryCurrent,CPUUsageNSec,ExecMainStartTimestamp,Restart,NRestarts`;
            break;
          case "list": {
            if (pattern) assertNoControlChars(pattern, "pattern");
            command = `systemctl list-units --type=service --all --no-pager --no-legend ${pattern ? q(pattern) : ""} | head -n 200`;
            break;
          }
          case "list_failed":
            command = `systemctl --failed --no-pager --no-legend; echo "---"; systemctl list-units --state=failed --no-pager --no-legend`;
            break;
          case "daemon_reload":
            command = `systemctl daemon-reload && echo "daemon reloaded"`;
            break;
          case "reset_failed":
            command = `systemctl reset-failed && echo "failed state reset"`;
            break;
          case "enable":
          case "disable":
            command = `systemctl ${action}${nowFlag} ${u} && systemctl is-enabled ${u}`;
            break;
          case "stop":
            command = `systemctl stop ${u} && sleep 1 && ! systemctl is-active --quiet ${u}`;
            break;
          default:
            command = `systemctl ${action.replace("_", "-")} ${u} && sleep 1 && systemctl is-active ${u}`;
            break;
        }

        const results = await ctx.pool.execMany(nodes, `${command} 2>&1`, {
          sudo: !READ_ONLY.has(action),
          timeoutMs: timeoutMs ?? 60_000,
        });
        return { content: [{ type: "text" as const, text: renderResults(`systemctl ${action}${unit ? ` ${unit}` : ""}`, results) }] };
      } catch (err) {
        return errorText(err);
      }
    },
  );
}
