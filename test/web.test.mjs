import assert from "node:assert/strict";
import { once } from "node:events";
import { mkdtempSync, rmSync } from "node:fs";
import { request } from "node:http";
import { tmpdir } from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";
import test from "node:test";

import { formatDuration, formatRelativeTime, formatUtcTimestamp } from "../dist/web/time.js";
import { startWebServer } from "../dist/web.js";

const now = Date.parse("2026-09-12T12:00:00.000Z");

test("formats dashboard last-seen timestamps as elapsed minutes, hours, and days", () => {
  assert.equal(formatRelativeTime("2026-09-12T11:59:30.000Z", now), "0 min ago");
  assert.equal(formatRelativeTime("2026-09-12T11:58:30.000Z", now), "1 min ago");
  assert.equal(formatRelativeTime("2026-09-12T10:00:00.000Z", now), "2 hours ago");
  assert.equal(formatRelativeTime("2026-09-11T12:00:00.000Z", now), "1 day ago");
  assert.equal(formatRelativeTime("2026-09-09T12:00:00.000Z", now), "3 days ago");
  assert.equal(formatRelativeTime("2026-09-12T12:01:00.000Z", now), "0 min ago");
  assert.equal(formatRelativeTime("not-a-timestamp", now), "-");
});

test("formats dashboard interaction timestamps as canonical UTC ISO strings", () => {
  assert.equal(formatUtcTimestamp("2026-09-12T14:00:00+02:00"), "2026-09-12T12:00:00.000Z");
  assert.equal(formatUtcTimestamp("not-a-timestamp"), "-");
});

test("formats job durations as compact elapsed time", () => {
  assert.equal(formatDuration(59_400), "59s");
  assert.equal(formatDuration(114_000), "1m 54s");
  assert.equal(formatDuration(3_661_000), "1h 1m 1s");
  assert.equal(formatDuration(Number.NaN), "-");
});

function getJson(server, pathname) {
  const address = server.address();
  assert.equal(typeof address, "object");
  return new Promise((resolve, reject) => {
    const req = request({
      host: "127.0.0.1",
      port: address.port,
      path: pathname,
      headers: { host: "127.0.0.1:0" },
    }, (response) => {
      const chunks = [];
      response.on("data", (chunk) => chunks.push(chunk));
      response.on("end", () => resolve({
        status: response.statusCode,
        body: JSON.parse(Buffer.concat(chunks).toString("utf8")),
      }));
    });
    req.on("error", reject);
    req.end();
  });
}

test("dashboard lists cached active modules and loads their advertised MCP API", async () => {
  const logDir = mkdtempSync(path.join(tmpdir(), "vantamcpd-web-"));
  const logName = `vanta-${new Date().toISOString().slice(0, 10)}.jsonl`;
  const nodes = [
    { name: "cluster1", role: "worker" },
    { name: "cluster2", role: "worker" },
  ];
  const textTools = {
    manifest: {
      id: "text-tools",
      name: "Text Tools",
      version: "0.2.0",
      description: "Bounded text operations.",
      deployment: { mode: "replicated", routing: "round-robin" },
      runtime: { mode: "on-demand" },
      compatibility: { architectures: ["armhf"], requiredCommands: ["python3"] },
    },
    files: [{}, {}, {}],
    totalBytes: 4096,
  };
  const inactive = {
    manifest: {
      ...textTools.manifest,
      id: "inactive-module",
      name: "Inactive Module",
    },
    files: [],
    totalBytes: 0,
  };
  let inventoryCalls = 0;
  let selectedNode;
  const modules = {
    catalog: { modules: [textTools, inactive], errors: [] },
    onInventoryChanged: () => () => {},
    installedModules: async () => {
      inventoryCalls += 1;
      return [
        {
          node: "cluster1",
          reachable: true,
          count: 1,
          modules: ["text-tools"],
          moduleVersions: { "text-tools": "0.2.0" },
        },
        {
          node: "cluster2",
          reachable: true,
          count: 1,
          modules: ["text-tools"],
          moduleVersions: { "text-tools": "0.1.0" },
        },
      ];
    },
    listTools: async (moduleId, node) => {
      assert.equal(moduleId, "text-tools");
      selectedNode = node;
      return {
        node: node.name,
        moduleId,
        version: "0.2.0",
        deployment: textTools.manifest.deployment,
        selection: "explicit",
        server: { name: "vanta-text-tools", version: "0.2.0" },
        tools: [{ name: "regex_extract", description: "Extract regex matches.", inputSchema: { type: "object" } }],
      };
    },
  };
  let eventFilter;
  const audit = {
    lastSeq: 0,
    summary: () => [],
    nodes: () => [],
    modules: () => ["core", "text-tools"],
    statuses: () => [],
    query: (filter) => {
      eventFilter = filter;
      return [];
    },
    subscribe: () => () => {},
  };
  const jobs = {
    snapshot: () => ({
      refreshedAt: "2026-09-13T12:00:00.000Z",
      jobs: [{
        schemaVersion: 1,
        jobId: "12345678-1234-4234-8234-123456789abc",
        kind: "module-install",
        status: "running",
        targetNode: "cluster1",
        moduleId: "corpus-search",
        resourceKeys: ["module:corpus-search:cluster1"],
        phase: "download",
        progress: { current: 500, total: 10000, unit: "records" },
        createdAt: "2026-09-13T11:59:00.000Z",
        startedAt: "2026-09-13T11:59:01.000Z",
        heartbeatAt: "2026-09-13T12:00:00.000Z",
      }],
    }),
  };
  const server = startWebServer({ monitoring: { port: 0, logDir }, jobs: { pollIntervalMs: 60_000 }, nodes }, audit, modules, jobs);
  assert.ok(server);
  try {
    await once(server, "listening");
    await new Promise((resolve) => setImmediate(resolve));

    const summary = await getJson(server, "/api/summary");
    assert.equal(summary.status, 200);
    assert.equal(summary.body.moduleInventoryPending, false);
    assert.deepEqual(summary.body.availableModules, ["core", "inactive-module", "text-tools"]);
    assert.equal(summary.body.modules.length, 1);
    assert.deepEqual(summary.body.modules[0].installedVersions, ["0.1.0", "0.2.0"]);
    assert.equal(summary.body.modules[0].nodeCount, 2);
    assert.equal(summary.body.modules[0].packageFiles, 3);

    const events = await getJson(server, "/api/events?module=text-tools&includeEngine=false");
    assert.equal(events.status, 200);
    assert.equal(events.body.logFileUrl, pathToFileURL(path.join(logDir, logName)).href);
    assert.deepEqual(events.body.modules, ["core", "inactive-module", "text-tools"]);
    assert.equal(eventFilter.module, "text-tools");
    assert.equal(eventFilter.includeEngine, false);

    await getJson(server, "/api/events?status=exit%201&includeEngine=false");
    assert.equal(eventFilter.status, "exit 1");

    const detail = await getJson(server, "/api/module?id=text-tools");
    assert.equal(detail.status, 200);
    assert.equal(detail.body.api.tools[0].name, "regex_extract");
    assert.equal(selectedNode, nodes[0]);
    assert.equal(inventoryCalls, 1);

    const jobList = await getJson(server, "/api/jobs");
    assert.equal(jobList.status, 200);
    assert.equal(jobList.body.jobs[0].moduleId, "corpus-search");
    assert.equal(jobList.body.jobs[0].progress.current, 500);
    assert.equal(jobList.body.jobs[0].displayStatus, "running");
    // The dashboard shows this so a stale-looking job can be told apart from a slow poll.
    assert.equal(jobList.body.pollIntervalMs, 60_000);
  } finally {
    await new Promise((resolve) => server.close(resolve));
    rmSync(logDir, { recursive: true, force: true });
  }
});