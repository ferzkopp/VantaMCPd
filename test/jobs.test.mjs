import assert from "node:assert/strict";
import path from "node:path";
import test from "node:test";
import { JobManager } from "../dist/jobs/manager.js";
import { JobRegistry } from "../dist/jobs/registry.js";
import { isTerminalJobStatus, jobStatusKey, parseJobState } from "../dist/jobs/types.js";

const root = path.resolve(import.meta.dirname, "..");

function node() {
  return {
    name: "storage-node",
    host: "127.0.0.1",
    port: 22,
    user: "test",
    auth: "key",
    privateKeyPath: "unused",
    sudo: "nopasswd",
    role: "worker+storage",
    tags: ["worker", "storage"],
    storage: { device: "/dev/sda1", mountpoint: "/mnt/ssd", fsType: "ext4", label: "clusterssd", nfs: { enabled: false, network: "10.0.0.0/24", options: "rw,sync,no_subtree_check" } },
    connectTimeoutMs: 1000,
    commandTimeoutMs: 1000,
    strictHostKeyChecking: true,
  };
}

function config(target = node()) {
  return {
    nodes: [target],
    jobs: { retentionDays: 7, pollIntervalMs: 10_000, cancelGraceMs: 5_000, maxLogBytes: 1_000_000 },
    security: { maxOutputBytes: 200_000 },
    maxConcurrency: 1,
  };
}

function execResult(target, stdout = "") {
  return { node: target.name, host: target.host, ok: true, code: 0, stdout, stderr: "", durationMs: 1, truncated: false, timedOut: false };
}

test("validates durable job states and terminal statuses", () => {
  const state = parseJobState({
    schemaVersion: 1,
    jobId: "12345678-1234-4234-8234-123456789abc",
    kind: "module-install",
    status: "queued",
    targetNode: "storage-node",
    resourceKeys: ["module:corpus-search:storage-node"],
    createdAt: "2026-09-13T12:00:00.000Z",
  });
  assert.equal(state.status, "queued");
  assert.equal(isTerminalJobStatus("running"), false);
  assert.equal(isTerminalJobStatus("succeeded"), true);
  assert.equal(jobStatusKey({ status: "succeeded" }), "ok");
  assert.equal(jobStatusKey({ status: "failed", result: { exitCode: 23 } }), "exit 23");
  assert.equal(jobStatusKey({ status: "failed" }), "error");
  assert.equal(jobStatusKey({ status: "running" }), "running");
  assert.throws(() => parseJobState({ ...state, unexpected: true }), /unrecognized key/i);
});

test("rejects unregistered job kinds before remote execution", async () => {
  const target = node();
  const pool = { execMany: async () => [execResult(target)], exec: async () => execResult(target) };
  const manager = new JobManager(config(target), pool, new JobRegistry(), path.join(root, "src", "jobs", "remote-runner.py"));
  await assert.rejects(
    manager.submit(target, {
      kind: "module-install",
      resourceKeys: ["module:corpus-search:storage-node"],
      command: ["/bin/true"],
      cwd: "/tmp",
      timeoutMs: 60_000,
    }),
    /Unregistered job kind/,
  );
});

test("submits an allowlisted job as a remote systemd unit", async () => {
  const target = node();
  const commands = [];
  let listOptions;
  const pool = {
    execMany: async (_nodes, _command, options) => {
      listOptions = options;
      return [execResult(target)];
    },
    exec: async (_node, command, options) => {
      commands.push({ command, options });
      return execResult(target);
    },
  };
  const registry = new JobRegistry();
  registry.register("module-install");
  const manager = new JobManager(config(target), pool, registry, path.join(root, "src", "jobs", "remote-runner.py"));
  const state = await manager.submit(target, {
    kind: "module-install",
    moduleId: "corpus-search",
    resourceKeys: ["module:corpus-search:storage-node"],
    command: ["/bin/true"],
    cwd: "/tmp",
    timeoutMs: 60_000,
  });
  assert.equal(state.status, "queued");
  assert.equal(state.targetNode, target.name);
  assert.match(state.jobId, /^[0-9a-f-]{36}$/);
  assert.equal(commands.length, 1);
  assert.match(commands[0].command, /systemctl enable /);
  assert.match(commands[0].command, /systemctl start --no-block/);
  assert.match(commands[0].command, /remote-runner\.py/);
  assert.match(commands[0].command, /mktemp .*\.spec\.XXXXXX/);
  assert.match(commands[0].command, /mv -f "\$spec_tmp"/);
  assert.equal(commands[0].options.sudo, true);
  assert.equal(listOptions.sudo, true);
});

test("reconciliation fails a nonterminal job whose systemd unit is inactive", async () => {
  const target = node();
  const active = {
    schemaVersion: 1,
    jobId: "12345678-1234-4234-8234-123456789abc",
    kind: "module-install",
    status: "running",
    targetNode: target.name,
    moduleId: "corpus-search",
    resourceKeys: ["module:corpus-search:storage-node"],
    createdAt: "2026-09-13T12:00:00.000Z",
    startedAt: "2026-09-13T12:00:01.000Z",
  };
  const encoded = Buffer.from(JSON.stringify(active)).toString("base64") + "\n";
  const commands = [];
  const pool = {
    execMany: async () => [execResult(target, encoded)],
    exec: async (_node, command) => {
      commands.push(command);
      return execResult(target, "__FAILED__");
    },
  };
  const registry = new JobRegistry();
  registry.register("module-install");
  const manager = new JobManager(config(target), pool, registry, path.join(root, "src", "jobs", "remote-runner.py"));
  const result = await manager.reconcile();
  assert.equal(result.jobs[0].status, "failed");
  assert.match(result.jobs[0].error, /not active/);
  assert.match(commands[0], /systemctl is-active/);
  assert.match(commands[0], /--fail/);
});

test("blocks a job when an active remote job owns the same resource", async () => {
  const target = node();
  const active = {
    schemaVersion: 1,
    jobId: "12345678-1234-4234-8234-123456789abc",
    kind: "module-install",
    status: "running",
    targetNode: target.name,
    moduleId: "corpus-search",
    resourceKeys: ["module:corpus-search:storage-node"],
    createdAt: "2026-09-13T12:00:00.000Z",
    startedAt: "2026-09-13T12:00:01.000Z",
  };
  const encoded = Buffer.from(JSON.stringify(active)).toString("base64") + "\n";
  const pool = { execMany: async () => [execResult(target, encoded)], exec: async () => execResult(target) };
  const registry = new JobRegistry();
  registry.register("module-install");
  const manager = new JobManager(config(target), pool, registry, path.join(root, "src", "jobs", "remote-runner.py"));
  await assert.rejects(
    manager.submit(target, {
      kind: "module-install",
      resourceKeys: ["module:corpus-search:storage-node"],
      command: ["/bin/true"],
      cwd: "/tmp",
      timeoutMs: 60_000,
    }),
    /Resource is busy with job/,
  );
});

test("reconciliation gives a newly queued systemd job time to start", async () => {
  const target = node();
  const queued = {
    schemaVersion: 1,
    jobId: "12345678-1234-4234-8234-123456789abc",
    kind: "module-install",
    status: "queued",
    targetNode: target.name,
    moduleId: "corpus-search",
    resourceKeys: ["module:corpus-search:storage-node"],
    createdAt: new Date().toISOString(),
  };
  const encoded = Buffer.from(JSON.stringify(queued)).toString("base64") + "\n";
  let execCalls = 0;
  const pool = {
    execMany: async () => [execResult(target, encoded)],
    exec: async () => {
      execCalls += 1;
      return execResult(target, "__FAILED__");
    },
  };
  const registry = new JobRegistry();
  registry.register("module-install");
  const manager = new JobManager(config(target), pool, registry, path.join(root, "src", "jobs", "remote-runner.py"));
  const result = await manager.reconcile();
  assert.equal(result.jobs[0].status, "queued");
  assert.equal(execCalls, 0);
});