import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { Duplex, PassThrough } from "node:stream";
import test from "node:test";
import { loadModuleCatalog } from "../dist/modules/catalog.js";
import { evaluateCompatibility } from "../dist/modules/compatibility.js";
import { compareSemanticVersions, ModuleManager } from "../dist/modules/manager.js";
import { parseModuleManifest } from "../dist/modules/manifest.js";
import { SshMcpTransport } from "../dist/modules/ssh-transport.js";

const root = path.resolve(import.meta.dirname, "..");

function node(hardware) {
  return {
    name: "test-node",
    host: "127.0.0.1",
    port: 22,
    user: "test",
    auth: "key",
    privateKeyPath: "unused",
    sudo: "none",
    role: "worker",
    tags: ["worker"],
    connectTimeoutMs: 1000,
    commandTimeoutMs: 1000,
    strictHostKeyChecking: true,
    hardware,
  };
}

function receiptLine(moduleId, version = "0.2.0") {
  const receipt = {
    schemaVersion: 1,
    moduleId,
    version,
    installedAt: "2026-09-12T00:00:00.000Z",
    installDirectory: `/opt/vantamcpd/modules/${moduleId}/${version}`,
    currentLink: `/opt/vantamcpd/modules/${moduleId}/current`,
    entrypoint: ["python3", "server.py"],
    files: [],
    runtime: { mode: "on-demand" },
  };
  return `${moduleId}|${Buffer.from(JSON.stringify(receipt)).toString("base64")}\n`;
}

test("compares semantic versions for startup update decisions", () => {
  assert.equal(compareSemanticVersions("0.1.0", "0.2.0"), -1);
  assert.equal(compareSemanticVersions("1.0.0", "1.0.0"), 0);
  assert.equal(compareSemanticVersions("2.0.0", "1.9.9"), 1);
  assert.equal(compareSemanticVersions("1.0.0-beta.2", "1.0.0"), -1);
  assert.equal(compareSemanticVersions("1.0.0-beta.10", "1.0.0-beta.2"), 1);
});

test("startup reconciliation updates only older installations and then removes the old payload", async () => {
  const oldNode = node({});
  const newerNode = { ...node({}), name: "newer-node", host: "127.0.0.2" };
  const cleanupCommands = [];
  const pool = {
    exec: async (_node, command, options) => {
      cleanupCommands.push({ command, options });
      return { node: oldNode.name, host: oldNode.host, ok: true, code: 0, stdout: "", stderr: "", durationMs: 1, truncated: false, timedOut: false };
    },
  };
  const manager = new ModuleManager({ maxConcurrency: 2, nodes: [oldNode, newerNode] }, pool, path.join(root, "modules"));
  manager.installedModules = async () => [
    { node: oldNode.name, reachable: true, count: 1, modules: ["text-tools"], moduleVersions: { "text-tools": "0.1.0" } },
    { node: newerNode.name, reachable: true, count: 1, modules: ["text-tools"], moduleVersions: { "text-tools": "0.3.0" } },
  ];
  manager.install = async (moduleId, targets) => {
    assert.equal(moduleId, "text-tools");
    assert.deepEqual(targets.map((target) => target.name), [oldNode.name]);
    return [{ node: oldNode.name, ok: true, moduleId, version: "0.2.0" }];
  };

  const updates = await manager.updateOutdatedModules();
  assert.deepEqual(updates, [{
    node: oldNode.name,
    moduleId: "text-tools",
    fromVersion: "0.1.0",
    toVersion: "0.2.0",
    updated: true,
    oldVersionRemoved: true,
  }]);
  assert.equal(cleanupCommands.length, 1);
  assert.match(cleanupCommands[0].command, /readlink/);
  assert.match(cleanupCommands[0].command, /text-tools\/0\.1\.0/);
  assert.equal(cleanupCommands[0].options.sudo, true);
});

test("loads the text-tools package deterministically", () => {
  const catalog = loadModuleCatalog(path.join(root, "modules"));
  assert.deepEqual(catalog.errors, []);
  assert.deepEqual(catalog.modules.map((item) => item.manifest.id), ["text-tools"]);
  assert.ok(catalog.modules[0].files.some((file) => file.relativePath === "server.py"));
  assert.deepEqual(catalog.modules[0].manifest.deployment, { mode: "replicated", routing: "round-robin" });
  assert.deepEqual(catalog.modules[0].manifest.runtime, { mode: "on-demand" });
});

test("accepts singleton module deployment declarations", () => {
  const manifest = loadModuleCatalog(path.join(root, "modules")).modules[0].manifest;
  const parsed = parseModuleManifest({
    ...manifest,
    id: "state-database",
    deployment: { mode: "singleton" },
  });
  assert.deepEqual(parsed.deployment, { mode: "singleton" });
});

test("requires every module to choose a deployment policy", () => {
  const manifest = loadModuleCatalog(path.join(root, "modules")).modules[0].manifest;
  const { deployment: _deployment, ...withoutDeployment } = manifest;
  assert.throws(() => parseModuleManifest(withoutDeployment), /deployment: Required/);
});

test("accepts boot-persistent service runtime declarations", () => {
  const manifest = loadModuleCatalog(path.join(root, "modules")).modules[0].manifest;
  const parsed = parseModuleManifest({
    ...manifest,
    id: "example-service",
    runtime: { mode: "service", systemdUnit: "example.service" },
  });
  assert.deepEqual(parsed.runtime, { mode: "service", systemdUnit: "example.service" });
});

test("reports malformed packages without hiding valid packages", () => {
  const temporary = mkdtempSync(path.join(tmpdir(), "vanta-modules-"));
  try {
    const invalid = path.join(temporary, "invalid");
    mkdirSync(invalid);
    writeFileSync(path.join(invalid, "module.json"), '{"schemaVersion":2}');
    const catalog = loadModuleCatalog(temporary);
    assert.equal(catalog.modules.length, 0);
    assert.equal(catalog.errors.length, 1);
    assert.match(catalog.errors[0].error, /Invalid module manifest/);
  } finally {
    rmSync(temporary, { recursive: true, force: true });
  }
});

test("evaluates the current ARM profile without making it a global default", () => {
  const manifest = loadModuleCatalog(path.join(root, "modules")).modules[0].manifest;
  const result = evaluateCompatibility(manifest, node({
    cpu: { arch: "armv7l", packageArch: "armhf", cores: 2 },
    memory: { totalMb: 1000 },
    os: { id: "debian", version: "12" },
    accelerators: [],
  }));
  assert.equal(result.status, "compatible");
});

test("evaluates an x86-64 node through the same manifest", () => {
  const manifest = loadModuleCatalog(path.join(root, "modules")).modules[0].manifest;
  const result = evaluateCompatibility(manifest, node({
    cpu: { arch: "x86_64", packageArch: "amd64", cores: 16 },
    memory: { totalMb: 32768 },
    os: { id: "ubuntu", version: "24.04" },
    accelerators: [],
  }));
  assert.equal(result.status, "compatible");
});

test("matches declared GPU capabilities and rejects an insufficient GPU", () => {
  const base = loadModuleCatalog(path.join(root, "modules")).modules[0].manifest;
  const manifest = {
    ...base,
    compatibility: {
      ...base.compatibility,
      architectures: ["amd64"],
      accelerators: [{ kind: "gpu", vendor: "nvidia", minMemoryMb: 16000, runtime: "cuda", minRuntimeVersion: "12.4" }],
    },
  };
  const capable = node({
    cpu: { packageArch: "amd64", cores: 24 },
    memory: { totalMb: 65536 },
    os: { id: "debian" },
    accelerators: [{ kind: "gpu", vendor: "nvidia", model: "RTX 5090", memoryMb: 32768, runtime: "cuda", runtimeVersion: "12.8" }],
  });
  assert.equal(evaluateCompatibility(manifest, capable).status, "compatible");
  capable.hardware.accelerators[0].memoryMb = 8192;
  assert.equal(evaluateCompatibility(manifest, capable).status, "incompatible");
});

test("does not confuse missing accelerator discovery with no accelerator", () => {
  const base = loadModuleCatalog(path.join(root, "modules")).modules[0].manifest;
  const manifest = { ...base, compatibility: { ...base.compatibility, accelerators: [{ kind: "gpu" }] } };
  const result = evaluateCompatibility(manifest, node({
    cpu: { packageArch: "amd64", cores: 8 },
    memory: { totalMb: 16384 },
    os: { id: "debian" },
  }));
  assert.equal(result.status, "unknown");
  assert.match(result.unknown[0], /accelerator inventory/);
});

test("live preflight rejects missing commands and insufficient disk", async () => {
  const target = node({
    cpu: { packageArch: "amd64", cores: 8 },
    memory: { totalMb: 16384 },
    os: { id: "debian", version: "12" },
    accelerators: [],
  });
  const pool = {
    execMany: async () => [{
      node: target.name,
      host: target.host,
      ok: true,
      code: 0,
      stdout: "command_bash|present\ncommand_python3|missing\ncommand_rg|missing\ncommand_jq|missing\ncommand_awk|missing\ncommand_sed|missing\ndisk_available_mb|10\n",
      stderr: "",
      durationMs: 1,
      truncated: false,
      timedOut: false,
    }],
  };
  const manager = new ModuleManager({ maxConcurrency: 1 }, pool, path.join(root, "modules"));
  const [result] = await manager.check("text-tools", [target]);
  assert.equal(result.compatibility.status, "incompatible");
  assert.deepEqual(result.missingCommands, ["python3", "rg", "jq", "awk", "sed"]);
  assert.match(result.compatibility.reasons.join("\n"), /missing required commands/);
  assert.match(result.compatibility.reasons.join("\n"), /40 MB free disk/);
});

test("install provisions declared apt dependencies when required commands are missing", async () => {
  const target = node({
    cpu: { packageArch: "amd64", cores: 8 },
    memory: { totalMb: 16384 },
    os: { id: "debian", version: "12" },
    accelerators: [],
  });
  const ok = (stdout = "") => ({
    node: target.name, host: target.host, ok: true, code: 0, stdout, stderr: "",
    durationMs: 1, truncated: false, timedOut: false,
  });
  const commands = [];
  const pool = {
    execMany: async () => [ok("command_bash|present\ncommand_python3|present\ncommand_rg|missing\ncommand_jq|present\ncommand_awk|present\ncommand_sed|present\ndisk_available_mb|1000\n")],
    exec: async (_node, command, options = {}) => {
      commands.push({ command, options });
      return { ...ok(), ok: false, code: 100, stderr: "package unavailable" };
    },
  };
  const manager = new ModuleManager({ maxConcurrency: 1 }, pool, path.join(root, "modules"));
  let inventoryChanges = 0;
  manager.onInventoryChanged(() => { inventoryChanges += 1; });
  const [result] = await manager.install("text-tools", [target]);
  assert.equal(result.ok, false);
  assert.equal(inventoryChanges, 0);
  assert.match(result.error, /apt dependency installation failed/);
  assert.equal(commands.length, 1);
  assert.match(commands[0].command, /apt-get install/);
  assert.match(commands[0].command, /'ripgrep'/);
  assert.equal(commands[0].options.sudo, true);
});

test("install stages, verifies, installs, writes a receipt, and cleans up", async () => {
  const target = node({
    cpu: { packageArch: "amd64", cores: 8 },
    memory: { totalMb: 16384 },
    os: { id: "debian", version: "12" },
    accelerators: [],
  });
  const catalog = loadModuleCatalog(path.join(root, "modules"));
  const files = catalog.modules[0].files;
  const commands = [];
  const uploads = [];
  let sftpEnded = false;
  const ok = (stdout = "") => ({
    node: target.name,
    host: target.host,
    ok: true,
    code: 0,
    stdout,
    stderr: "",
    durationMs: 1,
    truncated: false,
    timedOut: false,
  });
  const pool = {
    execMany: async () => [ok("command_bash|present\ncommand_python3|present\ncommand_rg|present\ncommand_jq|present\ncommand_awk|present\ncommand_sed|present\ndisk_available_mb|1000\n")],
    exec: async (_node, command, options = {}) => {
      commands.push({ command, options });
      if (command.includes("sha256sum")) {
        return ok(files.map((file) => `${file.relativePath}|${file.sha256}`).join("\n"));
      }
      return ok();
    },
    sftp: async () => ({
      fastPut: (localPath, remotePath, callback) => {
        uploads.push({ localPath, remotePath });
        callback(null);
      },
      end: () => { sftpEnded = true; },
    }),
  };
  const manager = new ModuleManager({ maxConcurrency: 1 }, pool, path.join(root, "modules"));
  let inventoryChanges = 0;
  manager.onInventoryChanged(() => { inventoryChanges += 1; });
  manager.catalog.modules[0].manifest.runtime = { mode: "service", systemdUnit: "server.py" };
  const [result] = await manager.install("text-tools", [target]);
  assert.equal(result.ok, true);
  assert.equal(inventoryChanges, 1);
  assert.equal(uploads.length, files.length);
  assert.equal(sftpEnded, true);
  assert.ok(commands.some((entry) => entry.command.includes("bash 'install.sh'") && entry.options.sudo === true));
  assert.ok(commands.some((entry) => entry.command.includes("chown -R root:root '/opt/vantamcpd/modules/text-tools/0.2.0'") && entry.options.sudo === true));
  assert.ok(commands.some((entry) => entry.command.includes("systemctl enable --now 'vantamcpd-text-tools.service'") && entry.options.sudo === true));
  assert.ok(commands.some((entry) => entry.command.includes("/var/lib/vantamcpd/modules/text-tools.json") && entry.options.sudo === true));
  assert.ok(commands.some((entry) => entry.command.includes("rollback()") && entry.options.sudo === true));
  assert.match(commands.at(-1).command, /^rm -rf -- /);
});

test("uninstall validates the receipt before removing its payload and state", async () => {
  const target = node({
    cpu: { packageArch: "amd64", cores: 8 },
    memory: { totalMb: 16384 },
    os: { id: "debian", version: "12" },
    accelerators: [],
  });
  const commands = [];
  const receipt = {
    schemaVersion: 1,
    moduleId: "text-tools",
    version: "0.2.0",
    installedAt: "2026-09-12T00:00:00.000Z",
    installDirectory: "/opt/vantamcpd/modules/text-tools/0.2.0",
    currentLink: "/opt/vantamcpd/modules/text-tools/current",
    entrypoint: ["python3", "server.py"],
    files: [],
    runtime: { mode: "service", systemdUnit: "server.py" },
  };
  const ok = (stdout = "") => ({
    node: target.name,
    host: target.host,
    ok: true,
    code: 0,
    stdout,
    stderr: "",
    durationMs: 1,
    truncated: false,
    timedOut: false,
  });
  const pool = {
    exec: async (_node, command, options = {}) => {
      commands.push({ command, options });
      return commands.length === 1 ? ok(JSON.stringify(receipt)) : ok();
    },
  };
  const manager = new ModuleManager({ maxConcurrency: 1 }, pool, path.join(root, "modules"));
  let inventoryChanges = 0;
  manager.onInventoryChanged(() => { inventoryChanges += 1; });
  manager.catalog.modules[0].manifest.runtime = receipt.runtime;
  const [result] = await manager.uninstall("text-tools", [target]);
  assert.deepEqual(result, {
    node: "test-node",
    ok: true,
    moduleId: "text-tools",
    version: "0.2.0",
    removed: true,
  });
  assert.equal(inventoryChanges, 1);
  assert.equal(commands.length, 2);
  assert.ok(commands[1].options.sudo);
  assert.match(commands[1].command, /uninstall\.sh/);
  assert.match(commands[1].command, /text-tools\.json/);
  assert.match(commands[1].command, /systemctl disable --now 'vantamcpd-text-tools\.service'/);
});

test("SSH MCP transport frames messages and enforces request limits", async () => {
  const writes = [];
  class MockChannel extends Duplex {
    stderr = new PassThrough();
    _read() {}
    _write(chunk, _encoding, callback) {
      writes.push(chunk.toString("utf8"));
      callback();
    }
    close() {
      this.emit("close");
    }
  }
  const channel = new MockChannel();
  const transport = new SshMcpTransport(async () => channel, { maxInputBytes: 128, maxOutputBytes: 256 });
  const received = [];
  transport.onmessage = (message) => received.push(message);
  await transport.start();
  await transport.send({ jsonrpc: "2.0", id: 1, method: "ping" });
  channel.push('{"jsonrpc":"2.0","id":1,"result":{}}\n');
  assert.equal(writes.length, 1);
  assert.equal(received[0].id, 1);
  await assert.rejects(
    transport.send({ jsonrpc: "2.0", id: 2, method: "ping", params: { value: "x".repeat(128) } }),
    /exceeds 128 bytes/,
  );
  channel.stderr.write("diagnostic");
  assert.equal(transport.stderr, "diagnostic");
  channel.close();
});

test("module manager lists and calls only advertised tools over SSH MCP", async () => {
  const target = node({
    cpu: { packageArch: "amd64", cores: 8 },
    memory: { totalMb: 16384 },
    os: { id: "debian", version: "12" },
    accelerators: [],
  });
  const replica = { ...target, name: "replica-node", host: "127.0.0.2" };
  const receipt = {
    schemaVersion: 1,
    moduleId: "text-tools",
    version: "0.2.0",
    installedAt: "2026-09-12T00:00:00.000Z",
    installDirectory: "/opt/vantamcpd/modules/text-tools/0.2.0",
    currentLink: "/opt/vantamcpd/modules/text-tools/current",
    entrypoint: ["python3", "server.py"],
    files: [],
  };
  const ok = (stdout = "") => ({
    node: target.name,
    host: target.host,
    ok: true,
    code: 0,
    stdout,
    stderr: "",
    durationMs: 1,
    truncated: false,
    timedOut: false,
  });
  class ModuleChannel extends Duplex {
    stderr = new PassThrough();
    closed = false;
    _read() {}
    _write(chunk, _encoding, callback) {
      for (const line of chunk.toString("utf8").trim().split("\n")) {
        const request = JSON.parse(line);
        if (request.id === undefined) continue;
        let result;
        if (request.method === "initialize") {
          result = {
            protocolVersion: request.params.protocolVersion,
            capabilities: { tools: {} },
            serverInfo: { name: "vanta-text-tools", version: "0.2.0" },
          };
        } else if (request.method === "tools/list") {
          result = { tools: [
            { name: "regex_extract", description: "test", inputSchema: { type: "object" } },
            { name: "fail", description: "test error", inputSchema: { type: "object" } },
          ] };
        } else if (request.params.name === "fail") {
          result = { content: [{ type: "text", text: "invalid input" }], isError: true };
        } else {
          result = { content: [{ type: "text", text: '{"matches":[]}' }], structuredContent: { matches: [] } };
        }
        this.push(`${JSON.stringify({ jsonrpc: "2.0", id: request.id, result })}\n`);
      }
      callback();
    }
    close() {
      if (this.closed) return;
      this.closed = true;
      this.push(null);
      this.emit("close");
    }
    end() {
      this.close();
      return this;
    }
  }
  const opened = [];
  const pool = {
    exec: async (_node, command) => command.startsWith("cat ") ? ok(JSON.stringify(receipt)) : ok(),
    execMany: async () => [
      ok(receiptLine("text-tools")),
      { ...ok(receiptLine("text-tools")), node: replica.name, host: replica.host },
    ],
    openProcess: async (selectedNode, command, options) => {
      opened.push({ node: selectedNode.name, command, options });
      return new ModuleChannel();
    },
  };
  const manager = new ModuleManager({ maxConcurrency: 1, nodes: [target, replica] }, pool, path.join(root, "modules"));
  const listed = await manager.listTools("text-tools", target);
  assert.deepEqual(listed.tools.map((tool) => tool.name), ["regex_extract", "fail"]);
  assert.equal(listed.server.name, "vanta-text-tools");
  assert.equal(listed.selection, "explicit");
  assert.deepEqual(listed.deployment, { mode: "replicated", routing: "round-robin" });
  const called = await manager.callTool("text-tools", target, "regex_extract", { text: "x", pattern: "x" });
  assert.equal(called.ok, true);
  assert.equal(called.node, "test-node");
  assert.equal(called.moduleVersion, "0.2.0");
  assert.equal(called.selection, "explicit");
  assert.deepEqual(called.output, { matches: [] });
  assert.equal(opened.length, 2);
  assert.equal(opened[0].command, "'python3' 'server.py'");
  assert.equal(opened[0].options.cwd, receipt.installDirectory);

  const firstRouted = await manager.callTool("text-tools", undefined, "regex_extract", {});
  const secondRouted = await manager.callTool("text-tools", undefined, "regex_extract", {});
  assert.equal(firstRouted.node, "test-node");
  assert.equal(secondRouted.node, "replica-node");
  assert.equal(firstRouted.selection, "automatic");
  assert.deepEqual(opened.slice(2, 4).map((entry) => entry.node), ["test-node", "replica-node"]);
  const failed = await manager.callTool("text-tools", target, "fail", {});
  assert.equal(failed.ok, false);
  assert.equal(failed.output, "invalid input");
  await assert.rejects(
    manager.callTool("text-tools", target, "not_advertised", {}),
    /does not advertise tool/,
  );
});

test("singleton installation rejects an existing instance on another node", async () => {
  const first = node({});
  const second = { ...node({}), name: "second-node", host: "127.0.0.2" };
  const result = (target, stdout) => ({
    node: target.name,
    host: target.host,
    ok: true,
    code: 0,
    stdout,
    stderr: "",
    durationMs: 1,
    truncated: false,
    timedOut: false,
  });
  const manager = new ModuleManager(
    { maxConcurrency: 2, nodes: [first, second] },
    { execMany: async () => [result(first, ""), result(second, receiptLine("text-tools"))] },
    path.join(root, "modules"),
  );
  manager.catalog.modules[0].manifest.deployment = { mode: "singleton" };

  await assert.rejects(manager.install("text-tools", [first]), /already installed on second-node/);
});

test("singleton installation fails closed when cluster placement cannot be verified", async () => {
  const first = node({});
  const second = { ...node({}), name: "offline-node", host: "127.0.0.2" };
  const manager = new ModuleManager(
    { maxConcurrency: 2, nodes: [first, second] },
    {
      execMany: async () => [
        { node: first.name, host: first.host, ok: true, code: 0, stdout: "", stderr: "", durationMs: 1, truncated: false, timedOut: false },
        { node: second.name, host: second.host, ok: false, code: null, stdout: "", stderr: "", error: "offline", durationMs: 1, truncated: false, timedOut: false },
      ],
    },
    path.join(root, "modules"),
  );
  manager.catalog.modules[0].manifest.deployment = { mode: "singleton" };

  await assert.rejects(manager.install("text-tools", [first]), /unreachable nodes: offline-node/);
});

test("concurrent singleton installations are serialized per module", async () => {
  const first = node({});
  const second = { ...node({}), name: "second-node", host: "127.0.0.2" };
  const manager = new ModuleManager(
    { maxConcurrency: 2, nodes: [first, second] },
    {},
    path.join(root, "modules"),
  );
  const modulePackage = manager.catalog.modules[0];
  modulePackage.manifest.deployment = { mode: "singleton" };
  let installedNode;
  manager.installedModules = async () => [first, second].map((target) => ({
    node: target.name,
    reachable: true,
    count: installedNode === target.name ? 1 : 0,
    modules: installedNode === target.name ? ["text-tools"] : [],
  }));
  manager.check = async (_moduleId, targets) => targets.map((target) => ({
    node: target.name,
    reachable: true,
    compatibility: { status: "compatible", reasons: [], unknown: [] },
  }));
  manager.installOnNode = async (_package, target) => {
    await new Promise((resolve) => setImmediate(resolve));
    installedNode = target.name;
    return { node: target.name, ok: true, moduleId: "text-tools", version: modulePackage.manifest.version };
  };

  const [firstInstall, secondInstall] = await Promise.allSettled([
    manager.install("text-tools", [first]),
    manager.install("text-tools", [second]),
  ]);
  assert.equal(firstInstall.status, "fulfilled");
  assert.equal(secondInstall.status, "rejected");
  assert.match(secondInstall.reason.message, /already installed on test-node/);
});

test("discovers installed module receipt counts per node", async () => {
  const first = node({});
  const second = { ...node({}), name: "offline-node", host: "127.0.0.2" };
  const ok = {
    node: first.name,
    host: first.host,
    ok: true,
    code: 0,
    stdout: receiptLine("text-tools") + receiptLine("science-corpus", "1.4.2") + receiptLine("text-tools"),
    stderr: "",
    durationMs: 1,
    truncated: false,
    timedOut: false,
  };
  const failed = { ...ok, node: second.name, host: second.host, ok: false, code: null, stdout: "", error: "offline" };
  const manager = new ModuleManager(
    { maxConcurrency: 2 },
    { execMany: async () => [ok, failed] },
    path.join(root, "modules"),
  );
  const inventory = await manager.installedModules([first, second]);
  assert.deepEqual(inventory[0], {
    node: "test-node",
    reachable: true,
    count: 2,
    modules: ["science-corpus", "text-tools"],
    moduleVersions: { "text-tools": "0.2.0", "science-corpus": "1.4.2" },
  });
  assert.deepEqual(inventory[1], {
    node: "offline-node",
    reachable: false,
    error: "offline",
  });
  const listing = await manager.list([first, second]);
  assert.equal(listing.inventoryComplete, false);
  assert.deepEqual(listing.unreachableNodes, ["offline-node"]);
  assert.deepEqual(listing.modules[0].installedNodes, ["test-node"]);
  assert.deepEqual(listing.modules[0].installedVersions, { "test-node": "0.2.0" });
  assert.equal(listing.modules[0].nodes[0].installed, true);
  assert.equal(listing.modules[0].nodes[0].installedVersion, "0.2.0");
  assert.equal(listing.modules[0].nodes[0].updateAvailable, false);
  assert.equal(listing.modules[0].nodes[1].installed, undefined);
});