import { z } from "zod";
import { aptGet, aptUpdate } from "../apt.js";
import { resolveTargets, type ResolvedNode } from "../config.js";
import { errorText, json, renderResults } from "../format.js";
import { isTerminalJobStatus } from "../jobs/types.js";
import { assertNoControlChars, q, validatePackage } from "../security.js";
import type { ExecResult } from "../ssh.js";
import { targetsSchema, timeoutSchema, type ToolContext, type ToolServer } from "./context.js";

const WRITE_ACTIONS = new Set(["update", "upgrade", "full_upgrade", "install", "reinstall", "remove", "purge", "autoremove", "clean"]);
// Killing dpkg midway is worse than waiting, so a background job gets a generous default limit.
const BACKGROUND_TIMEOUT_MS = 6 * 60 * 60 * 1_000;
const BACKGROUND_MIN_FREE_MEMORY_MB = 100;

function aptJobKey(node: ResolvedNode): string {
  return `apt:${node.name}`;
}

export function registerPackageTools(server: ToolServer, ctx: ToolContext): void {
  server.registerTool(
    "cluster_packages",
    {
      title: "Manage apt packages",
      description:
        "Manage Debian packages across the cluster: refresh indexes, list or apply upgrades, install, remove, purge, autoremove, search, or show package details. " +
        "Read-only actions (search, show, list_installed, list_upgradable, policy) need no sudo. " +
        "Use dryRun=true first for install/remove/upgrade to preview what apt would do - important on these 1GB / 16GB nodes. " +
        "Set execution=\"background\" for write actions that may run long (upgrades on slow ARM boards): each node gets a " +
        "durable job that survives SSH drops; follow it with cluster_get_job and cluster_get_job_log.",
      inputSchema: {
        action: z.enum([
          "update",
          "upgrade",
          "full_upgrade",
          "install",
          "reinstall",
          "remove",
          "purge",
          "autoremove",
          "clean",
          "search",
          "show",
          "policy",
          "list_installed",
          "list_upgradable",
        ]),
        targets: targetsSchema,
        packages: z.array(z.string()).optional().describe("Package names for install/reinstall/remove/purge/show/policy."),
        query: z.string().optional().describe('Search term for action="search" or filter for the list_* actions.'),
        dryRun: z.boolean().optional().describe("Simulate with apt-get -s instead of making changes. Strongly recommended first."),
        updateFirst: z.boolean().optional().describe("Run apt-get update before an install/upgrade. Default true."),
        timeoutMs: timeoutSchema,
        execution: z
          .enum(["immediate", "background"])
          .default("immediate")
          .describe("background runs a write action as one durable job per node (default limit 6 h) and returns job IDs."),
      },
    },
    async ({ action, targets, packages, query, dryRun, updateFirst, timeoutMs, execution }) => {
      try {
        const nodes = resolveTargets(ctx.config, targets);
        if (execution === "background") {
          if (!WRITE_ACTIONS.has(action)) throw new Error(`action="${action}" is read-only and always runs immediately.`);
          if (dryRun) throw new Error("A dry run returns at once; run it with execution=\"immediate\".");
        }
        const pkgs = (packages ?? []).map(validatePackage);
        const needsPkgs = ["install", "reinstall", "remove", "purge", "show", "policy"];
        if (needsPkgs.includes(action) && pkgs.length === 0) {
          throw new Error(`action="${action}" requires at least one entry in "packages".`);
        }
        const quoted = pkgs.map(q);
        const preUpdate = updateFirst === false ? "" : `${aptUpdate(true)};`;

        let command: string;
        let sudo = true;
        let defaultTimeout = 300_000;

        switch (action) {
          case "update":
            command = aptUpdate();
            break;
          case "upgrade":
          case "full_upgrade": {
            const verb = action === "upgrade" ? "upgrade" : "full-upgrade";
            command = `${preUpdate} ${aptGet(verb, { dryRun })}`;
            defaultTimeout = 1_800_000;
            break;
          }
          case "install":
          case "reinstall": {
            const flags = action === "reinstall" ? ["--reinstall"] : [];
            command = `${preUpdate} ${aptGet("install", { packages: quoted, dryRun, flags })}`;
            defaultTimeout = 900_000;
            break;
          }
          case "remove":
          case "purge":
            command = aptGet(action, { packages: quoted, dryRun });
            defaultTimeout = 600_000;
            break;
          case "autoremove":
            command = aptGet("autoremove", { dryRun });
            break;
          case "clean":
            command = `${aptGet("clean")} && df -h / | tail -n 1`;
            break;
          case "search": {
            if (!query) throw new Error('action="search" requires a "query".');
            assertNoControlChars(query, "query");
            command = `apt-cache search --names-only -- ${q(query)} | head -n 100`;
            sudo = false;
            defaultTimeout = 120_000;
            break;
          }
          case "show":
            command = `apt-cache policy ${quoted.join(" ")}; echo; apt-cache show ${quoted.join(" ")} | head -n 120`;
            sudo = false;
            break;
          case "policy":
            command = `apt-cache policy ${quoted.join(" ")}`;
            sudo = false;
            break;
          case "list_installed": {
            const filter = query ? ` | grep -iE -- ${q(query)}` : "";
            if (query) assertNoControlChars(query, "query");
            command = `dpkg-query -W -f='\${Package}\\t\${Version}\\t\${Status}\\n' 2>/dev/null | grep 'install ok installed' | cut -f1,2${filter} | head -n 400`;
            sudo = false;
            break;
          }
          case "list_upgradable": {
            const filter = query ? ` | grep -iE -- ${q(query)}` : "";
            if (query) assertNoControlChars(query, "query");
            // grep exits 1 on no match, which would report a fully up-to-date node as a failure.
            command = `apt-get -s -o Debug::NoLocking=1 upgrade 2>/dev/null | grep '^Inst '${filter} || echo "(no upgradable packages)"`;
            sudo = false;
            break;
          }
        }

        const title = `apt ${action}${dryRun ? " (dry run)" : ""}${pkgs.length ? `: ${pkgs.join(" ")}` : ""}`;
        if (execution === "background") {
          const jobs = await Promise.all(nodes.map(async (node) => {
            try {
              const job = await ctx.jobs.submit(node, {
                kind: "apt",
                resourceKeys: [aptJobKey(node)],
                command: ["/bin/bash", "-c", `${command} 2>&1`],
                cwd: "/",
                timeoutMs: timeoutMs ?? BACKGROUND_TIMEOUT_MS,
                minFreeMemoryMb: BACKGROUND_MIN_FREE_MEMORY_MB,
              });
              return { node: node.name, ok: true, jobId: job.jobId, status: job.status };
            } catch (error) {
              return { node: node.name, ok: false, error: (error as Error).message };
            }
          }));
          return json({
            action: title,
            execution: "background",
            jobs,
            next: "Follow each job with cluster_get_job; read apt output with cluster_get_job_log.",
          }, jobs.every((job) => !job.ok));
        }

        // An immediate write would only wait out the dpkg lock behind a running background apt job.
        const busy = new Map<string, string>();
        if (WRITE_ACTIONS.has(action) && !dryRun) {
          for (const job of (await ctx.jobs.list(nodes)).jobs) {
            if (job.kind === "apt" && !isTerminalJobStatus(job.status)) busy.set(job.targetNode, job.jobId);
          }
        }
        const runnable = nodes.filter((node) => !busy.has(node.name));
        const executed = runnable.length === 0 ? [] : await ctx.pool.execMany(runnable, `${command} 2>&1`, {
          sudo,
          timeoutMs: timeoutMs ?? defaultTimeout,
        });
        const results = nodes.map((node): ExecResult => {
          const jobId = busy.get(node.name);
          if (jobId === undefined) return executed[runnable.indexOf(node)]!;
          return {
            node: node.name,
            host: node.host,
            ok: false,
            code: null,
            stdout: "",
            stderr: "",
            durationMs: 0,
            truncated: false,
            timedOut: false,
            error: `background apt job ${jobId} is still running on this node; follow it with cluster_get_job`,
          };
        });
        return { content: [{ type: "text" as const, text: renderResults(title, results) }] };
      } catch (err) {
        return errorText(err);
      }
    },
  );
}
