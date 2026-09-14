import assert from "node:assert/strict";
import path from "node:path";
import test from "node:test";
import { z } from "zod";

import { capabilitySummary, loadModuleCatalog } from "../dist/modules/catalog.js";
import { registerExecTools } from "../dist/tools/exec.js";
import { registerFileTools } from "../dist/tools/files.js";
import { registerJobTools } from "../dist/tools/jobs.js";
import { registerLogTools } from "../dist/tools/logs.js";
import { registerModuleTools } from "../dist/tools/modules.js";
import { registerPackageTools } from "../dist/tools/packages.js";
import { registerServiceTools } from "../dist/tools/services.js";
import { registerStorageTools } from "../dist/tools/storage.js";
import { registerSwapTools } from "../dist/tools/swap.js";
import { registerSystemTools } from "../dist/tools/system.js";

const REGISTRARS = [
  registerSystemTools,
  registerExecTools,
  registerLogTools,
  registerPackageTools,
  registerServiceTools,
  registerFileTools,
  registerStorageTools,
  registerSwapTools,
  registerJobTools,
  registerModuleTools,
];

/** Every tool the daemon advertises. A change here has to be a deliberate API change. */
const EXPECTED_TOOLS = [
  "cluster_call_module_tool",
  "cluster_cancel_job",
  "cluster_check_command",
  "cluster_check_module",
  "cluster_download",
  "cluster_get_job",
  "cluster_get_job_log",
  "cluster_hardware",
  "cluster_install_module",
  "cluster_list_dir",
  "cluster_list_jobs",
  "cluster_list_module_tools",
  "cluster_list_modules",
  "cluster_list_nodes",
  "cluster_logs",
  "cluster_packages",
  "cluster_ping",
  "cluster_power",
  "cluster_purge_module_data",
  "cluster_read_file",
  "cluster_run",
  "cluster_services",
  "cluster_status",
  "cluster_storage",
  "cluster_swap",
  "cluster_uninstall_module",
  "cluster_upload",
  "cluster_write_file",
];

function node(name, host, role, tags, storage) {
  return {
    name,
    host,
    port: 22,
    user: "vanta",
    auth: "key",
    privateKeyPath: "unused",
    sudo: "nopasswd",
    role,
    tags,
    storage,
    connectTimeoutMs: 1_000,
    commandTimeoutMs: 1_000,
    strictHostKeyChecking: true,
  };
}

function testConfig() {
  return {
    nodes: [
      node("cluster1", "10.0.0.11", "worker", ["worker"]),
      node("cluster4", "10.0.0.14", "worker+storage", ["worker", "storage"], {
        device: "/dev/sda1",
        mountpoint: "/mnt/ssd",
        fsType: "ext4",
        label: "clusterssd",
        nfs: { enabled: true, network: "10.0.0.0/24", options: "rw,sync,no_subtree_check" },
      }),
    ],
    security: { allowArbitraryCommands: true, requireConfirmForDangerous: true, maxOutputBytes: 200_000, extraDenyPatterns: [] },
    monitoring: { enabled: false, web: false, port: 7420, logDir: "/tmp", maxEvents: 10, maxLogMb: 1, logOutput: false },
    jobs: { retentionDays: 7, pollIntervalMs: 10_000, cancelGraceMs: 5_000, maxLogBytes: 1_000 },
    modules: {},
    maxConcurrency: 4,
    autoDiscoverHardware: false,
    autoUpdateModules: false,
    nfsNetwork: "10.0.0.0/24",
    configPath: "/tmp/cluster.config.json",
    knownHostsPath: "/tmp/known_hosts.json",
  };
}

/** Records what each tool would have run instead of opening an SSH connection. */
function recordingPool(overrides = {}) {
  const calls = [];
  const result = (target, command, opts) => ({
    node: target.name,
    host: target.host,
    ok: true,
    code: 0,
    stdout: "",
    stderr: "",
    durationMs: 1,
    truncated: false,
    timedOut: false,
    ...overrides,
  });
  return {
    calls,
    async exec(target, command, opts = {}) {
      calls.push({ nodes: [target.name], command, opts });
      return result(target, command, opts);
    },
    async execMany(targets, command, opts = {}) {
      calls.push({ nodes: targets.map((t) => t.name), command, opts });
      return targets.map((t) => result(t, command, opts));
    },
    async sftp() {
      throw new Error("sftp is not available in this test");
    },
  };
}

function buildContext(pool = recordingPool()) {
  const ctx = {
    config: testConfig(),
    pool,
    jobs: { async list() { return []; } },
    modules: {
      catalog: loadModuleCatalog(path.resolve(import.meta.dirname, "..", "modules")),
      async install() {
        throw new Error("install must not be reached without confirmation");
      },
    },
  };
  const tools = new Map();
  const server = {
    registerTool(name, config, handler) {
      assert.ok(!tools.has(name), `tool ${name} registered twice`);
      tools.set(name, { config, handler });
    },
  };
  for (const register of REGISTRARS) register(server, ctx);
  return { ctx, tools, pool };
}

function call(tools, name, args) {
  const tool = tools.get(name);
  assert.ok(tool, `tool ${name} is not registered`);
  return tool.handler(args, { signal: new AbortController().signal });
}

function parse(tools, name, args) {
  return z.object(tools.get(name).config.inputSchema).safeParse(args);
}

function body(result) {
  return result.content.map((part) => part.text).join("\n");
}

test("module capabilities are advertised on the proxy tools so the agent can route without being told the module name", () => {
  const { ctx, tools } = buildContext();
  const summary = capabilitySummary(ctx.modules.catalog);
  assert.ok(summary.includes("text-tools:"), "the catalog must contribute text-tools capabilities");

  // These two descriptions are the only place the model learns what the modules can do: the modules'
  // own tools live behind the proxy and never appear in the daemon's tool list.
  for (const name of ["cluster_call_module_tool", "cluster_list_module_tools"]) {
    const { description } = tools.get(name).config;
    assert.ok(description.includes(summary), `${name} does not advertise the module capabilities`);
  }

  const proxy = tools.get("cluster_call_module_tool").config.description;
  assert.match(proxy, /redact emails, IPs and secrets/);
  assert.match(proxy, /search scientific paper metadata/);
});

test("registers exactly the advertised tool surface, each with a title, description and schema", () => {
  const { tools } = buildContext();
  assert.deepEqual([...tools.keys()].sort(), EXPECTED_TOOLS);
  for (const [name, { config }] of tools) {
    assert.ok(config.title, `${name} has no title`);
    assert.ok(config.description && config.description.length > 40, `${name} has no usable description`);
    assert.equal(typeof config.inputSchema, "object", `${name} has no input schema`);
  }
});

test("input schemas reject malformed arguments before a handler runs", () => {
  const { tools } = buildContext();

  assert.equal(parse(tools, "cluster_packages", { action: "upgrade" }).success, true);
  assert.equal(parse(tools, "cluster_packages", { action: "dist-upgrade" }).success, false);
  assert.equal(parse(tools, "cluster_packages", {}).success, false);

  assert.equal(parse(tools, "cluster_write_file", { path: "/etc/x", content: "a", mode: "0644" }).success, true);
  assert.equal(parse(tools, "cluster_write_file", { path: "/etc/x", content: "a", mode: "rwx" }).success, false);
  assert.equal(parse(tools, "cluster_write_file", { path: "/etc/x", content: "a", owner: "root:root" }).success, true);
  assert.equal(parse(tools, "cluster_write_file", { path: "/etc/x", content: "a", owner: "root; rm -rf /" }).success, false);

  assert.equal(parse(tools, "cluster_swap", { action: "status", swappiness: 60 }).success, true);
  assert.equal(parse(tools, "cluster_swap", { action: "status", swappiness: 500 }).success, false);

  assert.equal(parse(tools, "cluster_power", { action: "reboot", confirm: true }).success, true);
  assert.equal(parse(tools, "cluster_power", { action: "reboot", confirm: false }).success, false);
  assert.equal(parse(tools, "cluster_power", { action: "reboot" }).success, false);

  assert.equal(parse(tools, "cluster_install_module", { moduleId: "text-tools", targets: [] }).success, false);
  assert.equal(parse(tools, "cluster_upload", { localPath: "a", remotePath: "/tmp/a", timeoutMs: -1 }).success, false);
});

test("unknown targets are reported as a tool error naming the known nodes and tags", async () => {
  const { tools, pool } = buildContext();
  const result = await call(tools, "cluster_status", { targets: ["cluster9"] });
  assert.equal(result.isError, true);
  assert.match(body(result), /Unknown target\(s\): cluster9/);
  assert.match(body(result), /cluster1, cluster4/);
  assert.equal(pool.calls.length, 0);
});

test("targets resolve node names, tags and the implicit whole cluster", async () => {
  const { tools, pool } = buildContext();
  await call(tools, "cluster_status", { targets: ["storage"] });
  await call(tools, "cluster_status", { targets: ["cluster1"] });
  await call(tools, "cluster_status", {});
  assert.deepEqual(pool.calls.map((c) => c.nodes), [["cluster4"], ["cluster1"], ["cluster1", "cluster4"]]);
});

test("destructive tools refuse to act until the caller confirms", async () => {
  const { tools, pool } = buildContext();

  const run = await call(tools, "cluster_run", { command: "rm -rf /var/tmp/x", targets: ["cluster1"] });
  assert.equal(run.isError, true);
  assert.match(body(run), /confirm: true/);

  const format = await call(tools, "cluster_storage", { action: "format", node: "cluster4" });
  assert.equal(format.isError, true);
  assert.match(body(format), /confirmDevice="\/dev\/sda1"/);

  const create = await call(tools, "cluster_swap", { action: "create", device: "/dev/sdb", targets: ["cluster1"] });
  assert.equal(create.isError, true);
  assert.match(body(create), /confirmDevice="\/dev\/sdb"/);

  const mismatch = await call(tools, "cluster_swap", {
    action: "create",
    device: "/dev/sdb",
    confirmDevice: "/dev/sdc",
    targets: ["cluster1"],
  });
  assert.equal(mismatch.isError, true);

  const install = await call(tools, "cluster_install_module", { moduleId: "text-tools", targets: ["cluster1"] });
  assert.equal(install.isError, true);
  assert.match(body(install), /confirm: true/);

  const everywhere = await call(tools, "cluster_install_module", { moduleId: "text-tools", targets: ["all"], confirm: true });
  assert.equal(everywhere.isError, true);
  assert.match(body(everywhere), /'all' is not accepted/);

  assert.equal(pool.calls.length, 0, "no command may reach a node before confirmation");
});

test("guarded devices are rejected before any partitioning command is built", async () => {
  const { tools, pool } = buildContext();

  const sdCard = await call(tools, "cluster_swap", {
    action: "create",
    device: "/dev/mmcblk0",
    confirmDevice: "/dev/mmcblk0",
    targets: ["cluster1"],
  });
  assert.equal(sdCard.isError, true);
  assert.match(body(sdCard), /SD card \/ boot media/);

  const partition = await call(tools, "cluster_swap", {
    action: "create",
    device: "/dev/sdb1",
    confirmDevice: "/dev/sdb1",
    targets: ["cluster1"],
  });
  assert.equal(partition.isError, true);
  assert.match(body(partition), /repartitions a whole disk/);

  const storage = await call(tools, "cluster_swap", {
    action: "create",
    device: "/dev/sda",
    confirmDevice: "/dev/sda",
    targets: ["cluster4"],
  });
  assert.equal(storage.isError, true);
  assert.match(body(storage), /configured storage device/);

  assert.equal(pool.calls.length, 0);
});

test("swap creation names the first partition the way the kernel does", async () => {
  const { tools, pool } = buildContext();

  const usb = await call(tools, "cluster_swap", {
    action: "create",
    device: "/dev/sdb",
    confirmDevice: "/dev/sdb",
    targets: ["cluster1"],
  });
  assert.match(body(usb), /create swap on \/dev\/sdb1/);
  assert.match(pool.calls[0].command, /mkswap -L 'clusterswap' '\/dev\/sdb1'/);

  const nvme = await call(tools, "cluster_swap", {
    action: "create",
    device: "/dev/nvme0n1",
    confirmDevice: "/dev/nvme0n1",
    targets: ["cluster1"],
  });
  assert.match(body(nvme), /create swap on \/dev\/nvme0n1p1/);
  assert.match(pool.calls[1].command, /mkswap -L 'clusterswap' '\/dev\/nvme0n1p1'/);
  assert.doesNotMatch(pool.calls[1].command, /nvme0n11/);
});

test("apt commands are non-interactive, lock-tolerant and never read the script's own stdin", async () => {
  const { tools, pool } = buildContext();
  await call(tools, "cluster_packages", { action: "install", packages: ["htop"], targets: ["cluster1"] });
  const { command, opts } = pool.calls[0];

  assert.equal(opts.sudo, true);
  assert.match(command, /DEBIAN_FRONTEND=noninteractive/);
  assert.match(command, /NEEDRESTART_MODE=a/);
  assert.match(command, /-o Dpkg::Use-Pty=0/);
  assert.match(command, /-o DPkg::Lock::Timeout=300/);
  assert.match(command, /--force-confdef/);
  assert.match(command, /--force-confold/);
  assert.match(command, /install .*-- 'htop'/);
  assert.equal((command.match(/<\/dev\/null/g) ?? []).length, 2, "both the update and the install must detach stdin");
});

test("apt dry runs simulate and read-only apt actions need no privileges", async () => {
  const { tools, pool } = buildContext();
  await call(tools, "cluster_packages", { action: "upgrade", dryRun: true, updateFirst: false, targets: ["cluster1"] });
  assert.match(pool.calls[0].command, /apt-get -s /);
  assert.doesNotMatch(pool.calls[0].command, /apt-get update/);

  await call(tools, "cluster_packages", { action: "search", query: "htop", targets: ["cluster1"] });
  assert.equal(pool.calls[1].opts.sudo, false);
  assert.match(pool.calls[1].command, /apt-cache search --names-only -- 'htop'/);

  // A node with nothing to upgrade must not be reported as a failed node.
  await call(tools, "cluster_packages", { action: "list_upgradable", targets: ["cluster1"] });
  assert.match(pool.calls[2].command, /\|\| echo "\(no upgradable packages\)"/);
});

test("shell metacharacters in caller values are quoted, not interpolated", async () => {
  const { tools, pool } = buildContext();

  const badPackage = await call(tools, "cluster_packages", { action: "install", packages: ["htop; reboot"], targets: ["cluster1"] });
  assert.equal(badPackage.isError, true);
  assert.match(body(badPackage), /Invalid package name/);

  const badPath = await call(tools, "cluster_list_dir", { path: "relative/dir", targets: ["cluster1"] });
  assert.equal(badPath.isError, true);
  assert.match(body(badPath), /absolute POSIX path/);

  await call(tools, "cluster_list_dir", { path: "/tmp/it's here", targets: ["cluster1"] });
  assert.match(pool.calls[0].command, /'\/tmp\/it'\\''s here'/);
});

test("fstab cleanup tolerates tab-separated entries", async () => {
  const { tools, pool } = buildContext();
  await call(tools, "cluster_storage", { action: "unmount", node: "cluster4", removeFstab: true });
  assert.match(pool.calls[0].command, /\[\[:space:\]\]\/mnt\/ssd\[\[:space:\]\]/);
});

test("dmesg filtering survives the fallback to the untimestamped command", async () => {
  const { tools, pool } = buildContext();
  await call(tools, "cluster_logs", { source: "dmesg", grep: "usb", lines: 8, targets: ["cluster1"] });
  // Unbraced, "||" binds looser than "|" and a successful "dmesg -T" would skip grep and tail.
  assert.match(pool.calls[0].command, /^\{ dmesg -T 2>\/dev\/null \|\| dmesg; \} \| grep -iE -- 'usb' \| tail -n 8$/);

  await call(tools, "cluster_logs", { source: "dmesg", lines: 20, targets: ["cluster1"] });
  assert.match(pool.calls[1].command, /^\{ dmesg -T 2>\/dev\/null \|\| dmesg; \} \| tail -n 20$/);
});

test("per-node results are rendered with node, host and status", async () => {
  const { tools } = buildContext({
    ...recordingPool(),
    async execMany(targets, command) {
      return targets.map((t) => ({
        node: t.name,
        host: t.host,
        ok: false,
        code: 4,
        stdout: "partial",
        stderr: "boom",
        durationMs: 12,
        truncated: true,
        timedOut: false,
      }));
    },
  });
  const rendered = body(await call(tools, "cluster_status", { targets: ["cluster1"], raw: true }));
  assert.match(rendered, /1 node\(s\) - 0 ok, 1 failed/);
  assert.match(rendered, /## cluster1 \(10\.0\.0\.11\) - exit 4 - 12ms/);
  assert.match(rendered, /output truncated/);
});
