import { z } from "zod";
import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { resolveTargets } from "../config.js";
import { errorText, renderResults } from "../format.js";
import { assertNoControlChars, q, validatePackage } from "../security.js";
import { targetsSchema, timeoutSchema, type ToolContext } from "./context.js";

const APT_ENV = "export DEBIAN_FRONTEND=noninteractive; export NEEDRESTART_MODE=a;";
const APT_OPTS = `-y -o DPkg::Lock::Timeout=300 -o Dpkg::Options::=--force-confold -o Dpkg::Options::=--force-confdef`;

export function registerPackageTools(server: McpServer, ctx: ToolContext): void {
  server.registerTool(
    "cluster_packages",
    {
      title: "Manage apt packages",
      description:
        "Manage Debian packages across the cluster: refresh indexes, list or apply upgrades, install, remove, purge, autoremove, search, or show package details. " +
        "Read-only actions (search, show, list_installed, list_upgradable, policy) need no sudo. " +
        "Use dryRun=true first for install/remove/upgrade to preview what apt would do - important on these 1GB / 16GB nodes.",
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
      },
    },
    async ({ action, targets, packages, query, dryRun, updateFirst, timeoutMs }) => {
      try {
        const nodes = resolveTargets(ctx.config, targets);
        const pkgs = (packages ?? []).map(validatePackage);
        const needsPkgs = ["install", "reinstall", "remove", "purge", "show", "policy"];
        if (needsPkgs.includes(action) && pkgs.length === 0) {
          throw new Error(`action="${action}" requires at least one entry in "packages".`);
        }
        const quoted = pkgs.map(q).join(" ");
        const sim = dryRun ? " -s" : "";

        let command: string;
        let sudo = true;
        let defaultTimeout = 300_000;

        switch (action) {
          case "update":
            command = `${APT_ENV} apt-get update -o DPkg::Lock::Timeout=300`;
            break;
          case "upgrade":
          case "full_upgrade": {
            const verb = action === "upgrade" ? "upgrade" : "full-upgrade";
            const pre = updateFirst === false ? "" : `${APT_ENV} apt-get update -o DPkg::Lock::Timeout=300 >/dev/null 2>&1;`;
            command = `${pre} ${APT_ENV} apt-get${sim} ${APT_OPTS} ${verb}`;
            defaultTimeout = 1_800_000;
            break;
          }
          case "install":
          case "reinstall": {
            const pre = updateFirst === false ? "" : `${APT_ENV} apt-get update -o DPkg::Lock::Timeout=300 >/dev/null 2>&1;`;
            const extra = action === "reinstall" ? "--reinstall" : "";
            command = `${pre} ${APT_ENV} apt-get${sim} ${APT_OPTS} install ${extra} ${quoted}`;
            defaultTimeout = 900_000;
            break;
          }
          case "remove":
          case "purge":
            command = `${APT_ENV} apt-get${sim} ${APT_OPTS} ${action} ${quoted}`;
            defaultTimeout = 600_000;
            break;
          case "autoremove":
            command = `${APT_ENV} apt-get${sim} ${APT_OPTS} autoremove`;
            break;
          case "clean":
            command = `${APT_ENV} apt-get clean && df -h / | tail -n 1`;
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
            command = `apt-cache policy ${quoted}; echo; apt-cache show ${quoted} | head -n 120`;
            sudo = false;
            break;
          case "policy":
            command = `apt-cache policy ${quoted}`;
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
            command = `apt-get -s -o Debug::NoLocking=1 upgrade 2>/dev/null | grep '^Inst '${filter}`;
            sudo = false;
            break;
          }
        }

        const results = await ctx.pool.execMany(nodes, `${command} 2>&1`, {
          sudo,
          timeoutMs: timeoutMs ?? defaultTimeout,
        });
        const title = `apt ${action}${dryRun ? " (dry run)" : ""}${pkgs.length ? `: ${pkgs.join(" ")}` : ""}`;
        return { content: [{ type: "text" as const, text: renderResults(title, results) }] };
      } catch (err) {
        return errorText(err);
      }
    },
  );
}
