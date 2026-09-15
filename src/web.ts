import { readFileSync } from "node:fs";
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { withTool, withToolParameters, type AuditLog } from "./audit.js";
import type { ClusterConfig } from "./config.js";
import type { JobManager } from "./jobs/manager.js";
import { jobStatusKey } from "./jobs/types.js";
import type { ModuleManager, NodeModuleInventory } from "./modules/manager.js";

/**
 * Loopback only, by construction. The log holds hostnames, usernames and full command lines, so the
 * dashboard is never bound to a routable address and there is deliberately no option to change that.
 */
const BIND_HOST = "127.0.0.1";
const MODULE_REFRESH_MS = 60 * 60 * 1000;

/**
 * Static assets live next to the compiled output (scripts/copy-assets.mjs puts them there). Serving
 * the script and stylesheet as their own resources is what lets the CSP below refuse inline code.
 */
const ASSET_DIR = path.join(path.dirname(fileURLToPath(import.meta.url)), "web");

const ASSETS: Record<string, { file: string; type: string; cache: string }> = {
  "/": { file: "index.html", type: "text/html; charset=utf-8", cache: "no-store" },
  "/index.html": { file: "index.html", type: "text/html; charset=utf-8", cache: "no-store" },
  "/style.css": { file: "style.css", type: "text/css; charset=utf-8", cache: "no-store" },
  "/app.js": { file: "app.js", type: "text/javascript; charset=utf-8", cache: "no-store" },
  "/time.js": { file: "time.js", type: "text/javascript; charset=utf-8", cache: "no-store" },
  "/icon.svg": { file: "icon.svg", type: "image/svg+xml; charset=utf-8", cache: "max-age=86400" },
};

const CSP =
  "default-src 'none'; img-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; " +
  "base-uri 'none'; form-action 'none'; frame-ancestors 'none'";

/** Read once at startup: the files never change while the daemon runs, and this fails loudly if missing. */
function loadAssets(): Map<string, Buffer> {
  const cache = new Map<string, Buffer>();
  for (const { file } of Object.values(ASSETS)) {
    if (cache.has(file)) continue;
    cache.set(file, readFileSync(path.join(ASSET_DIR, file)));
  }
  return cache;
}

function sendJson(res: ServerResponse, body: unknown, status: number, scrub: (serialized: string) => string): void {
  res.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
  });
  res.end(scrub(JSON.stringify(body)));
}

function currentLogFileUrl(logDir: string): string {
  const filename = `vanta-${new Date().toISOString().slice(0, 10)}.jsonl`;
  return pathToFileURL(path.join(logDir, filename)).href;
}

function intParam(value: string | null, fallback: number, min: number, max: number): number {
  // Number(null) is 0 and Number("") is 0, so an absent param must be rejected before parsing.
  if (value === null || value.trim() === "") return fallback;
  const n = Number(value);
  if (!Number.isFinite(n)) return fallback;
  return Math.min(Math.max(Math.trunc(n), min), max);
}

const IPV4 = /\b\d{1,3}(?:\.\d{1,3}){3}\b/g;

/**
 * Builds a scrubber that rewrites every IPv4 address in a serialized dashboard payload. Addresses arrive
 * from places an allow-list cannot anticipate — NFS mount sources, export CIDRs, hand-written
 * descriptions, command text and SSH error messages — so the substitution runs over the whole response
 * rather than chosen fields. A configured node's address becomes its node name, which reads better than a
 * mask and keeps the relationship between nodes visible. The on-disk audit log keeps the raw host.
 */
export function addressScrubber(config: ClusterConfig): (serialized: string) => string {
  const names = new Map(
    config.nodes
      .filter((node) => /^\d{1,3}(?:\.\d{1,3}){3}$/.test(node.host))
      .map((node) => [node.host, node.name] as const),
  );
  return (serialized) =>
    // Four dotted groups are not necessarily an address: kernel and package versions look the same.
    serialized.replace(IPV4, (match) =>
      match.split(".").every((octet) => Number(octet) <= 255) ? names.get(match) ?? "x.x.x.x" : match,
    );
}

export function startWebServer(config: ClusterConfig, audit: AuditLog, modules: ModuleManager, jobs?: JobManager): Server | undefined {
  const scrubAddresses = addressScrubber(config);
  const json = (res: ServerResponse, body: unknown, status = 200): void => sendJson(res, body, status, scrubAddresses);
  const { port } = config.monitoring;
  const moduleInventory = new Map<string, NodeModuleInventory & { refreshedAt: string; stale?: boolean }>();
  let moduleRefresh: Promise<void> | undefined;
  let moduleRefreshQueued = false;
  const availableModuleIds = [
    "core",
    ...modules.catalog.modules.map((modulePackage) => modulePackage.manifest.id).sort(),
  ];

  const activeModules = () =>
    modules.catalog.modules.flatMap((modulePackage) => {
      const { manifest } = modulePackage;
      const installedNodes = config.nodes.flatMap((node) => {
        const inventory = moduleInventory.get(node.name);
        if (!inventory) return [];
        const version = inventory.moduleVersions?.[manifest.id];
        if (version === undefined) return [];
        return [{ node: node.name, version, reachable: inventory.reachable, stale: inventory.stale === true, refreshedAt: inventory.refreshedAt }];
      });
      if (installedNodes.length === 0) return [];
      return [{
        id: manifest.id,
        name: manifest.name,
        description: manifest.description,
        catalogVersion: manifest.version,
        installedVersions: [...new Set(installedNodes.map((node) => node.version))].sort(),
        installedNodes,
        nodeCount: installedNodes.length,
        configuredNodeCount: config.nodes.length,
        deployment: manifest.deployment,
        runtime: manifest.runtime,
        compatibility: manifest.compatibility,
        packageFiles: modulePackage.files.length,
        packageBytes: modulePackage.totalBytes,
      }];
    });

  const refreshModules = (queueIfActive = false): Promise<void> => {
    if (moduleRefresh) {
      if (queueIfActive) moduleRefreshQueued = true;
      return moduleRefresh;
    }
    moduleRefresh = withTool("dashboard_module_refresh", async () => {
      const refreshedAt = new Date().toISOString();
      const results = await modules.installedModules(config.nodes);
      for (const result of results) {
        const previous = moduleInventory.get(result.node);
        moduleInventory.set(
          result.node,
          result.reachable || !previous
            ? { ...result, refreshedAt }
            : { ...previous, reachable: false, error: result.error, refreshedAt, stale: true },
        );
      }
    }).finally(() => {
      moduleRefresh = undefined;
      if (moduleRefreshQueued) {
        moduleRefreshQueued = false;
        void refreshModules();
      }
    });
    return moduleRefresh;
  };

  let assets: Map<string, Buffer>;
  try {
    assets = loadAssets();
  } catch (err) {
    process.stderr.write(
      `monitor dashboard disabled: cannot read assets from ${ASSET_DIR} (${(err as Error).message}). ` +
        `Run "npm run build" so scripts/copy-assets.mjs populates dist/web.\n`,
    );
    return undefined;
  }

  const unsubscribeModuleChanges = modules.onInventoryChanged(() => void refreshModules(true));

  const server = createServer((req: IncomingMessage, res: ServerResponse) => {
    // Validate Host header to prevent DNS rebinding attacks against loopback.
    const hostHeader = req.headers.host;
    const allowedHost = new RegExp(`^(127\\.0\\.0\\.1|localhost)(:${port})?$`);
    if (!hostHeader || !allowedHost.test(hostHeader)) {
      res.writeHead(403).end("forbidden host");
      return;
    }

    // Same-origin only: a page on another site must not be able to read the log via the browser.
    const origin = req.headers.origin;
    if (origin && !/^http:\/\/(127\.0\.0\.1|localhost)(:\d+)?$/.test(origin)) {
      res.writeHead(403).end("forbidden origin");
      return;
    }
    if (req.method !== "GET") {
      res.writeHead(405).end("method not allowed");
      return;
    }

    const url = new URL(req.url ?? "/", `http://${BIND_HOST}:${port}`);
    const p = url.searchParams;

    const asset = ASSETS[url.pathname];
    if (asset) {
      res.writeHead(200, {
        "content-type": asset.type,
        "cache-control": asset.cache,
        "x-content-type-options": "nosniff",
        "content-security-policy": CSP,
        "referrer-policy": "no-referrer",
      });
      res.end(assets.get(asset.file));
      return;
    }

    switch (url.pathname) {
      case "/api/events":
        json(res, {
          lastSeq: audit.lastSeq,
          logFileUrl: currentLogFileUrl(config.monitoring.logDir),
          nodes: audit.nodes(),
          modules: [...new Set([...availableModuleIds, ...audit.modules()])],
          statuses: audit.statuses(p.get("includeEngine") !== "false"),
          events: audit.query({
            since: p.has("since") ? intParam(p.get("since"), 0, 0, Number.MAX_SAFE_INTEGER) : undefined,
            node: p.get("node") || undefined,
            module: p.get("module") || undefined,
            status: p.get("status") || undefined,
            q: p.get("q") || undefined,
            includeEngine: p.get("includeEngine") !== "false",
            limit: intParam(p.get("limit"), 200, 1, 2000),
          }),
        });
        return;

      case "/api/summary":
        // Addresses are deliberately not sent to the page: the dashboard is screenshot-friendly and
        // the host is already in the on-disk log for anyone who needs it.
        const activity = new Map(audit.summary().map((row) => [row.node, row]));
        json(res, {
          nodes: config.nodes.map((node) => {
            const row = activity.get(node.name);
            const inventory = moduleInventory.get(node.name);
            return {
              node: node.name,
              role: node.role,
              total: row?.total ?? 0,
              failed: row?.failed ?? 0,
              totalMs: row?.totalMs ?? 0,
              avgMs: row?.avgMs ?? 0,
              bytes: row?.bytes ?? 0,
              lastTs: row?.lastTs,
              lastTool: row?.lastTool,
              moduleCount: inventory?.count,
              moduleNames: inventory?.modules,
              moduleVersions: inventory?.moduleVersions,
              invalidModuleReceipts: inventory?.invalidReceipts,
              moduleReachable: inventory?.reachable,
              moduleError: inventory?.error,
              moduleRefreshedAt: inventory?.refreshedAt,
              moduleStale: inventory?.stale,
            };
          }),
          configured: config.nodes.map((n) => ({ name: n.name, role: n.role })),
          availableModules: availableModuleIds,
          modules: activeModules(),
          moduleInventoryPending: config.nodes.some((node) => !moduleInventory.has(node.name)),
          lastSeq: audit.lastSeq,
        });
        return;

      case "/api/jobs": {
        const snapshot = jobs?.snapshot() ?? { jobs: [] };
        json(res, {
          ...snapshot,
          pollIntervalMs: config.jobs.pollIntervalMs,
          jobs: snapshot.jobs.map((job) => ({ ...job, displayStatus: jobStatusKey(job) })),
        });
        return;
      }

      case "/api/module": {
        const moduleId = p.get("id");
        if (!moduleId) {
          json(res, { error: "module id is required" }, 400);
          return;
        }
        const moduleState = activeModules().find((item) => item.id === moduleId);
        if (!moduleState) {
          json(res, { error: "module is not installed" }, 404);
          return;
        }
        const selectedNode = moduleState.installedNodes
          .filter((item) => item.reachable && !item.stale)
          .map((item) => config.nodes.find((node) => node.name === item.node))
          .find((node) => node !== undefined);
        if (!selectedNode) {
          json(res, { error: "module has no reachable installation" }, 503);
          return;
        }
        void withToolParameters("dashboard_module_api", { moduleId }, () => modules.listTools(moduleId, selectedNode))
          .then((api) => json(res, { module: moduleState, api }))
          .catch((err: unknown) => json(res, { error: (err as Error).message }, 502));
        return;
      }

      case "/api/node": {
        const node = config.nodes.find((n) => n.name === p.get("name"));
        if (!node) {
          json(res, { error: "unknown node" }, 404);
          return;
        }
        // Allow-list, not a delete-list: `host`, `privateKeyPath` (which carries the local Windows
        // username) and the SSH user must never reach the page, and a config field added later should
        // not leak by default.
        const storage = node.storage
          ? {
              device: node.storage.device,
              mountpoint: node.storage.mountpoint,
              fsType: node.storage.fsType,
              label: node.storage.label,
              nfs: { enabled: node.storage.nfs.enabled, options: node.storage.nfs.options },
            }
          : undefined;
        json(res, {
          name: node.name,
          role: node.role,
          tags: node.tags,
          description: node.description,
          port: node.port,
          auth: node.auth,
          sudo: node.sudo,
          diskRoles: node.diskRoles,
          storage,
          hardware: node.hardware,
          modules: moduleInventory.get(node.name),
        });
        return;
      }

      case "/api/stream": {
        res.writeHead(200, {
          "content-type": "text/event-stream; charset=utf-8",
          "cache-control": "no-store",
          connection: "keep-alive",
          "x-accel-buffering": "no",
        });
        res.write("retry: 2000\n\n");
        const unsubscribe = audit.subscribe((event) => {
          // Live events bypass json(), so they need the same substitution the polled endpoints get.
          res.write(`data: ${scrubAddresses(JSON.stringify(event))}\n\n`);
        });
        // Keeps intermediaries and idle sockets from dropping the stream.
        const ping = setInterval(() => res.write(": ping\n\n"), 25_000);
        const close = () => {
          clearInterval(ping);
          unsubscribe();
        };
        req.on("close", close);
        res.on("close", close);
        return;
      }

      default:
        res.writeHead(404).end("not found");
    }
  });

  server.on("error", (err) => {
    const hint = (err as NodeJS.ErrnoException).code === "EADDRINUSE" ? ` - is another instance already on :${port}?` : "";
    process.stderr.write(`monitor web server error: ${err.message}${hint}\n`);
  });
  const moduleRefreshTimer = setInterval(() => void refreshModules(), MODULE_REFRESH_MS);
  moduleRefreshTimer.unref();
  server.once("close", () => {
    clearInterval(moduleRefreshTimer);
    unsubscribeModuleChanges();
  });
  server.listen(port, BIND_HOST, () => {
    process.stderr.write(`monitor dashboard: http://${BIND_HOST}:${port}\n`);
  });
  void refreshModules();
  return server;
}
