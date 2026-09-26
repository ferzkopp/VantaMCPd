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

test("reports a job the moment it settles so cached module state can refresh", async () => {
  const target = node();
  const base = {
    schemaVersion: 1,
    jobId: "12345678-1234-4234-8234-123456789abc",
    kind: "module-install",
    targetNode: target.name,
    moduleId: "python-compute",
    resourceKeys: ["module:python-compute:storage-node"],
    createdAt: "2026-09-15T12:00:00.000Z",
    startedAt: "2026-09-15T12:00:01.000Z",
  };
  const encode = (state) => Buffer.from(JSON.stringify(state)).toString("base64") + "\n";
  let payload = encode({ ...base, status: "running" });
  const pool = { execMany: async () => [execResult(target, payload)], exec: async () => execResult(target) };
  const registry = new JobRegistry();
  registry.register("module-install");
  const manager = new JobManager(config(target), pool, registry, path.join(root, "src", "jobs", "remote-runner.py"));

  const settled = [];
  const unsubscribe = manager.onJobSettled((job) => settled.push(job));

  await manager.list();
  assert.deepEqual(settled, [], "a running job must not be reported as settled");

  payload = encode({ ...base, status: "succeeded", finishedAt: "2026-09-15T12:05:00.000Z" });
  await manager.list();
  assert.equal(settled.length, 1);
  assert.equal(settled[0].jobId, base.jobId);
  assert.equal(settled[0].kind, "module-install");

  await manager.list();
  assert.equal(settled.length, 1, "a settled job must only be reported once");

  unsubscribe();
  payload = encode({ ...base, jobId: "22345678-1234-4234-8234-123456789abc", status: "running" });
  await manager.list();
  payload = encode({ ...base, jobId: "22345678-1234-4234-8234-123456789abc", status: "failed", finishedAt: "2026-09-15T12:06:00.000Z" });
  await manager.list();
  assert.equal(settled.length, 1, "an unsubscribed listener stops receiving jobs");
});

test("reconciliation fails a nonterminal job whose systemd unit is inactive", async () => {  const target = node();
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

function runningJob(target, jobId, resourceKey) {
  return Buffer.from(JSON.stringify({
    schemaVersion: 1,
    jobId,
    kind: "module-install",
    status: "running",
    targetNode: target.name,
    resourceKeys: [resourceKey],
    createdAt: "2026-09-13T12:00:00.000Z",
  })).toString("base64") + "\n";
}

function managerWith(target, pool, jobs = {}) {
  const registry = new JobRegistry();
  registry.register("module-call");
  const settings = { ...config(target), jobs: { ...config(target).jobs, maxConcurrentPerNode: 2, perNode: {}, ...jobs } };
  return new JobManager(settings, pool, registry, path.join(root, "src", "jobs", "remote-runner.py"));
}

const callInput = {
  kind: "module-call",
  resourceKeys: ["call:python-compute:storage-node"],
  command: ["/usr/bin/python3", "/opt/vantamcpd/job-runner/2/module-call.py", "/tmp/out"],
  cwd: "/tmp",
  timeoutMs: 60_000,
};

test("rejects a submission when the node already runs its maximum number of jobs", async () => {
  const target = node();
  const listed = runningJob(target, "12345678-1234-4234-8234-123456789abc", "module:a:storage-node")
    + runningJob(target, "22345678-1234-4234-8234-123456789abc", "module:b:storage-node");
  let submitted = false;
  const pool = { execMany: async () => [execResult(target, listed)], exec: async () => { submitted = true; return execResult(target); } };
  await assert.rejects(managerWith(target, pool).submit(target, callInput), /already running 2\/2 durable job/);
  assert.equal(submitted, false);

  const raised = managerWith(target, pool, { perNode: { [target.name]: { maxConcurrent: 3 } } });
  await raised.submit(target, callInput);
  assert.equal(submitted, true);
});

test("rejects a submission when the node lacks the declared free memory", async () => {
  const target = node();
  const commands = [];
  const pool = {
    execMany: async () => [execResult(target)],
    exec: async (_node, command) => {
      commands.push(command);
      return execResult(target, command.includes("MemAvailable") ? "412\n" : "");
    },
  };
  await assert.rejects(
    managerWith(target, pool).submit(target, { ...callInput, minFreeMemoryMb: 512 }),
    /412 MB available memory; this job requires 512 MB/,
  );
  assert.equal(commands.length, 1, "no job may be written after the memory check fails");
});

test("serializes submissions per node so concurrent calls cannot both pass the limit", async () => {
  const target = node();
  let listed = "";
  const pool = {
    execMany: async () => [execResult(target, listed)],
    exec: async () => {
      listed = runningJob(target, "12345678-1234-4234-8234-123456789abc", "call:x:storage-node");
      return execResult(target);
    },
  };
  const manager = managerWith(target, pool, { maxConcurrentPerNode: 1 });
  const results = await Promise.allSettled([
    manager.submit(target, callInput),
    manager.submit(target, { ...callInput, resourceKeys: ["call:other:storage-node"] }),
  ]);
  assert.equal(results[0].status, "fulfilled");
  assert.equal(results[1].status, "rejected");
  assert.match(results[1].reason.message, /already running 1\/1/);
});

test("runs a background call as the SSH user with a private input file and bounded result", async () => {
  const target = node();
  const commands = [];
  const pool = { execMany: async () => [execResult(target)], exec: async (_node, command) => { commands.push(command); return execResult(target); } };
  const state = await managerWith(target, pool).submit(target, {
    ...callInput,
    runAsNodeUser: true,
    rerunOnRestart: false,
    maxResultBytes: 2_000_000,
    input: JSON.stringify({ toolName: "python_run" }),
  });
  const script = commands[0];
  const spec = JSON.parse(Buffer.from(script.match(/printf '%s' '([^']+)' \| base64 -d > "\$spec_tmp"/)[1], "base64").toString("utf8"));
  assert.equal(spec.runAs, "test");
  assert.equal(spec.rerunOnRestart, false);
  assert.equal(spec.maxResultBytes, 2_000_000);
  assert.match(script, /install -d -m 0711 '\/var\/lib\/vantamcpd\/jobs\//);
  assert.match(script, /install -d -m 0700 -o 'test' '\/var\/lib\/vantamcpd\/jobs\/[^']+\/out'/);
  assert.match(script, /install -m 0600 -o 'test' \/dev\/stdin '[^']+\/out\/input\.json'/);
  assert.match(script, /\/opt\/vantamcpd\/job-runner\/2\/module-call\.py/);
  assert.equal(state.status, "queued");
});

test("reads a published job result only when the runner recorded one", async () => {
  const target = node();
  const jobId = "12345678-1234-4234-8234-123456789abc";
  const encode = (value) => Buffer.from(JSON.stringify(value)).toString("base64") + "\n";
  const base = { schemaVersion: 1, jobId, kind: "module-call", status: "succeeded", targetNode: target.name, resourceKeys: ["call:x:storage-node"], createdAt: "2026-09-13T12:00:00.000Z" };
  let listed = encode(base);
  const commands = [];
  const pool = { execMany: async () => [execResult(target, listed)], exec: async (_node, command) => { commands.push(command); return execResult(target, "{\"content\":[]}"); } };
  const manager = managerWith(target, pool);
  assert.deepEqual(await manager.result(jobId), { job: base });
  assert.equal(commands.length, 0);

  listed = encode({ ...base, resultBytes: 14 });
  const read = await manager.result(jobId);
  assert.equal(read.result, "{\"content\":[]}");
  assert.match(commands[0], /cat '\/var\/lib\/vantamcpd\/jobs\/12345678-1234-4234-8234-123456789abc\/result\.json'/);
});