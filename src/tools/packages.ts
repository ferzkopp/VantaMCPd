import { z } from "zod";
import { aptGet, aptUpdate } from "../apt.js";
import { resolveTargets } from "../config.js";
import { errorText, renderResults } from "../format.js";
import { assertNoControlChars, q, validatePackage } from "../security.js";
import { targetsSchema, timeoutSchema, type ToolContext, type ToolServer } from "./context.js";

export function registerPackageTools(server: ToolServer, ctx: ToolContext): void {
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
