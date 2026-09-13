import { z } from "zod";
import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { resolveTargets } from "../config.js";
import { errorText, renderResults } from "../format.js";
import { assertNoControlChars, q, validateAbsPath, validateUnit } from "../security.js";
import { targetsSchema, timeoutSchema, type ToolContext } from "./context.js";

const SINCE_RE = /^[A-Za-z0-9 :+_-]{1,64}$/;

export function registerLogTools(server: McpServer, ctx: ToolContext): void {
  server.registerTool(
    "cluster_logs",
    {
      title: "Read logs",
      description:
        "Read logs from the cluster: systemd journal (optionally for a single unit), kernel ring buffer, or the tail of an arbitrary log file. " +
        'Examples: source="journal" unit="ssh" priority="err"; source="journal" since="1 hour ago"; source="file" path="/var/log/syslog".',
      inputSchema: {
        targets: targetsSchema,
        source: z.enum(["journal", "dmesg", "file"]).default("journal"),
        unit: z.string().optional().describe('systemd unit to filter on, e.g. "ssh" or "nfs-server.service".'),
        path: z.string().optional().describe('Absolute log file path, required when source="file".'),
        lines: z.number().int().min(1).max(2000).default(100).describe("Number of trailing lines to return."),
        since: z.string().optional().describe('journalctl --since value, e.g. "1 hour ago", "today", "2026-09-12 08:00".'),
        priority: z
          .enum(["emerg", "alert", "crit", "err", "warning", "notice", "info", "debug"])
          .optional()
          .describe("Minimum journal priority to include."),
        grep: z.string().optional().describe("Case-insensitive regular expression to filter matching lines."),
        boot: z.enum(["current", "previous", "all"]).optional().describe('Which boot to read. Default "current".'),
        sudo: z.boolean().optional().describe("Read with root privileges. Default true (needed for full system logs)."),
        timeoutMs: timeoutSchema,
      },
    },
    async ({ targets, source, unit, path, lines, since, priority, grep, boot, sudo, timeoutMs }) => {
      try {
        const nodes = resolveTargets(ctx.config, targets);
        let command: string;

        if (source === "file") {
          if (!path) throw new Error('source="file" requires a "path".');
          validateAbsPath(path, "log path");
          command = `tail -n ${lines} -- ${q(path)}`;
          if (grep) {
            assertNoControlChars(grep, "grep pattern");
            command = `grep -iE -- ${q(grep)} ${q(path)} | tail -n ${lines}`;
          }
        } else if (source === "dmesg") {
          command = `dmesg -T 2>/dev/null || dmesg`;
          if (grep) {
            assertNoControlChars(grep, "grep pattern");
            command += ` | grep -iE -- ${q(grep)}`;
          }
          command += ` | tail -n ${lines}`;
        } else {
          const args = ["--no-pager", "--output=short-iso", `-n ${lines}`];
          if (unit) args.push(`-u ${q(validateUnit(unit))}`);
          if (priority) args.push(`-p ${priority}`);
          if (since) {
            if (!SINCE_RE.test(since)) throw new Error(`Invalid "since" value: ${JSON.stringify(since)}`);
            args.push(`--since ${q(since)}`);
          }
          if (boot === "previous") args.push("-b -1");
          else if (boot !== "all") args.push("-b 0");
          if (grep) {
            assertNoControlChars(grep, "grep pattern");
            args.push(`-g ${q(grep)} --case-sensitive=false`);
          }
          command = `journalctl ${args.join(" ")}`;
        }

        const results = await ctx.pool.execMany(nodes, command, {
          sudo: sudo !== false,
          timeoutMs: timeoutMs ?? 60_000,
        });
        return { content: [{ type: "text" as const, text: renderResults(`logs: ${command}`, results) }] };
      } catch (err) {
        return errorText(err);
      }
    },
  );
}
