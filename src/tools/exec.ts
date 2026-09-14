import { z } from "zod";
import { resolveTargets } from "../config.js";
import { errorText, renderResults } from "../format.js";
import { assessCommand, guardCommand } from "../security.js";
import { targetsSchema, timeoutSchema, type ToolContext, type ToolServer } from "./context.js";

export function registerExecTools(server: ToolServer, ctx: ToolContext): void {
  server.registerTool(
    "cluster_run",
    {
      title: "Run a shell command",
      description:
        "Run an arbitrary bash command on one or more nodes. The command is sent base64-encoded, so quoting, pipes and multi-line scripts are safe. " +
        "Destructive commands (mkfs, rm -rf, reboot, partitioning, firewall flush, curl|sh, ...) are refused unless confirm=true, which you must only set after the user has explicitly approved.",
      inputSchema: {
        command: z.string().min(1).describe("Bash command or multi-line script to execute."),
        targets: targetsSchema,
        sudo: z.boolean().optional().describe("Run the command as root via sudo. Default false."),
        cwd: z.string().optional().describe("Working directory on the remote node."),
        env: z.record(z.string()).optional().describe("Extra environment variables for the command."),
        confirm: z.boolean().optional().describe("Acknowledge a destructive command. Requires explicit user approval."),
        timeoutMs: timeoutSchema,
      },
    },
    async ({ command, targets, sudo, cwd, env, confirm, timeoutMs }) => {
      try {
        const nodes = resolveTargets(ctx.config, targets);
        const assessment = guardCommand(command, ctx.config.security, confirm === true);
        const results = await ctx.pool.execMany(nodes, command, {
          sudo: sudo === true,
          cwd,
          env,
          timeoutMs,
        });
        const warning = assessment.dangerous
          ? `> Destructive command executed with confirmation. Flags: ${assessment.reasons.join("; ")}\n\n`
          : "";
        return { content: [{ type: "text" as const, text: warning + renderResults(`\`${command}\``, results) }] };
      } catch (err) {
        return errorText(err);
      }
    },
  );

  server.registerTool(
    "cluster_check_command",
    {
      title: "Dry-run safety check",
      description:
        "Analyse a command against the destructive-command policy without running it. Use this before asking the user to approve something risky.",
      inputSchema: { command: z.string().min(1) },
    },
    async ({ command }) => {
      const assessment = assessCommand(command, ctx.config.security);
      return {
        content: [
          {
            type: "text" as const,
            text: assessment.dangerous
              ? `DANGEROUS - would require confirm=true.\nReasons: ${assessment.reasons.join("; ")}`
              : "OK - no destructive patterns detected.",
          },
        ],
      };
    },
  );

  server.registerTool(
    "cluster_power",
    {
      title: "Reboot or shut down nodes",
      description:
        "Reboot or power off nodes. Always requires confirm=true and explicit user approval, because shut-down ARM SBCs need physical access to power back on.",
      inputSchema: {
        action: z.enum(["reboot", "poweroff"]),
        targets: targetsSchema,
        confirm: z.literal(true).describe("Must be true. Only set after the user has explicitly approved."),
        delayMinutes: z.number().int().min(0).max(60).optional().describe("Delay before the action. Default 0 (now)."),
      },
    },
    async ({ action, targets, delayMinutes }) => {
      try {
        const nodes = resolveTargets(ctx.config, targets);
        const when = delayMinutes && delayMinutes > 0 ? `+${delayMinutes}` : "now";
        const flag = action === "reboot" ? "-r" : "-h";
        const results = await ctx.pool.execMany(nodes, `shutdown ${flag} ${when} "requested via VantaMCPd" 2>&1 || true`, {
          sudo: true,
          timeoutMs: 20_000,
        });
        return { content: [{ type: "text" as const, text: renderResults(`${action} (${when})`, results) }] };
      } catch (err) {
        return errorText(err);
      }
    },
  );
}
