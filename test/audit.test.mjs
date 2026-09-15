import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import { AuditLog, currentAuditAttribution, setCurrentAuditResult, withTool, withToolParameters } from "../dist/audit.js";

test("attributes redacted tool parameters to downstream audit events", () => {
  const logDir = mkdtempSync(path.join(tmpdir(), "vantamcpd-audit-"));
  try {
    const audit = new AuditLog({ logDir, maxEvents: 10, maxLogMb: 1, logOutput: false });
    const event = withToolParameters(
      "cluster_install_module",
      {
        moduleId: "text-tools",
        targets: ["cluster1"],
        confirm: true,
        credentials: { apiKey: "do-not-log", password: "also-secret" },
      },
      () =>
        audit.record({
          node: "cluster1",
          host: "192.0.2.1",
          kind: "exec",
          command: "install text-tools",
          sudo: true,
          ok: true,
          code: 0,
          durationMs: 1,
          bytesOut: 0,
          bytesErr: 0,
        }),
    );

    assert.equal(event.module, "text-tools");
    assert.equal(event.tool, "cluster_install_module");
    assert.equal(event.origin, "agent");
    assert.match(event.parameters, /"moduleId":"text-tools"/);
    assert.doesNotMatch(event.parameters, /do-not-log|also-secret/);
    assert.equal(audit.query({ module: "text-tools" }).length, 1);
    assert.equal(audit.query({ module: "core" }).length, 0);
    assert.equal(audit.query({ q: "text-tools" }).length, 1);
  } finally {
    rmSync(logDir, { recursive: true, force: true });
  }
});

test("removes credentials, queries, and fragments from URL audit parameters", () => {
  const attribution = withToolParameters(
    "cluster_call_module_tool",
    {
      moduleId: "browser-retrieval",
      arguments: {
        url: "https://user:password@example.com/docs/page?token=secret-value#private",
        sourceUrl: "https://example.org/source?signature=hidden",
      },
    },
    () => currentAuditAttribution(),
  );

  assert.match(attribution.parameters, /https:\/\/example\.com\/docs\/page/);
  assert.match(attribution.parameters, /https:\/\/example\.org\/source/);
  assert.doesNotMatch(attribution.parameters, /user|password|token|secret-value|private|signature|hidden/);
});

test("attributes non-module operations to core", () => {
  const logDir = mkdtempSync(path.join(tmpdir(), "vantamcpd-audit-"));
  try {
    const audit = new AuditLog({ logDir, maxEvents: 10, maxLogMb: 1, logOutput: false });
    const event = withToolParameters(
      "cluster_status",
      { targets: ["cluster1"] },
      () =>
        audit.record({
          node: "cluster1",
          host: "192.0.2.1",
          kind: "exec",
          command: "uptime",
          sudo: false,
          ok: true,
          code: 0,
          durationMs: 1,
          bytesOut: 0,
          bytesErr: 0,
        }),
    );

    assert.equal(event.module, "core");
    assert.equal(event.origin, "agent");
    assert.deepEqual(audit.modules(), ["core"]);
    assert.equal(audit.query({ module: "core" }).length, 1);
    assert.equal(audit.query({ module: "text-tools" }).length, 0);
  } finally {
    rmSync(logDir, { recursive: true, force: true });
  }
});

test("preserves captured attribution when work completes under another context", () => {
  const logDir = mkdtempSync(path.join(tmpdir(), "vantamcpd-audit-"));
  try {
    const audit = new AuditLog({ logDir, maxEvents: 10, maxLogMb: 1, logOutput: false });
    const attribution = withToolParameters(
      "cluster_call_module_tool",
      { moduleId: "text-tools" },
      () => {
        const captured = currentAuditAttribution();
        setCurrentAuditResult({
          complete: false,
          truncationReasons: ["row-pagination"],
          responseLimitBytes: 1000,
        });
        return captured;
      },
    );
    const event = withTool("dashboard_module_refresh", () =>
      audit.record({
        ...attribution,
        node: "cluster1",
        host: "192.0.2.1",
        kind: "exec",
        command: "python3 server.py",
        sudo: false,
        ok: true,
        code: 0,
        durationMs: 1,
        bytesOut: 125,
        bytesErr: 0,
      }));

    assert.equal(event.module, "text-tools");
    assert.equal(event.tool, "cluster_call_module_tool");
    assert.equal(event.origin, "agent");
    assert.match(event.parameters, /"moduleId":"text-tools"/);
    assert.deepEqual(event.result, {
      complete: false,
      truncationReasons: ["row-pagination"],
      responseBytes: 125,
      responseLimitBytes: 1000,
      responseLimitPercent: 12.5,
    });
  } finally {
    rmSync(logDir, { recursive: true, force: true });
  }
});

test("marks internal work as engine polling and excludes it on request", () => {
  const logDir = mkdtempSync(path.join(tmpdir(), "vantamcpd-audit-"));
  try {
    const audit = new AuditLog({ logDir, maxEvents: 10, maxLogMb: 1, logOutput: false });
    const event = withTool("job_reconcile", () =>
      audit.record({
        node: "cluster1",
        host: "192.0.2.1",
        kind: "exec",
        command: "read job states",
        sudo: true,
        ok: true,
        code: 0,
        durationMs: 1,
        bytesOut: 0,
        bytesErr: 0,
      }));

    assert.equal(event.origin, "engine");
    assert.equal(audit.query({ includeEngine: true }).length, 1);
    assert.equal(audit.query({ includeEngine: false }).length, 0);
    assert.deepEqual(audit.statuses(false), []);
  } finally {
    rmSync(logDir, { recursive: true, force: true });
  }
});