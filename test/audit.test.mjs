import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import { AuditLog, withToolParameters } from "../dist/audit.js";

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

    assert.equal(event.tool, "cluster_install_module");
    assert.match(event.parameters, /"moduleId":"text-tools"/);
    assert.doesNotMatch(event.parameters, /do-not-log|also-secret/);
    assert.equal(audit.query({ q: "text-tools" }).length, 1);
  } finally {
    rmSync(logDir, { recursive: true, force: true });
  }
});