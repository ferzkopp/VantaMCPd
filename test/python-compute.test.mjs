import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

const moduleDirectory = path.resolve(import.meta.dirname, "..", "modules", "python-compute");
const pythonCommand = ["python3", "python"].find((command) => spawnSync(command, ["--version"], { encoding: "utf8" }).status === 0);

function runPython(file, args = []) {
  assert.ok(pythonCommand, "Python 3 is required to test python-compute");
  return spawnSync(pythonCommand, [file, ...args], {
    cwd: moduleDirectory,
    encoding: "utf8",
    timeout: 60_000,
    env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" },
  });
}

test("Python Compute validates its schemas, inventory, harness, and sandbox argv", () => {
  for (const [file, args] of [
    ["schemas.py", []],
    ["inventory.py", ["--self-test"]],
    ["runner.py", ["--self-test"]],
    ["sandbox.py", []],
    ["server.py", ["--self-test"]],
  ]) {
    const result = runPython(file, args);
    assert.equal(result.status, 0, result.stderr || result.error?.message);
    assert.equal(result.stderr, "");
  }
});

test("Python Compute confines every submitted call to a network-free sandbox", () => {
  assert.ok(pythonCommand, "Python 3 is required to test python-compute");
  const generated = spawnSync(pythonCommand, ["-B", "-c", [
    "import json",
    "from sandbox import build_bwrap_argv, build_limited_argv",
    "present = {'/usr', '/bin', '/lib'}",
    "argv = build_bwrap_argv('/state/calls/x', '/opt/runner.py', exists=lambda p: p in present, islink=lambda p: p == '/bin', readlink=lambda p: 'usr/bin')",
    "print(json.dumps({'sandbox': argv, 'limits': build_limited_argv(256, 10)}))",
  ].join("\n")], { cwd: moduleDirectory, encoding: "utf8", timeout: 30_000, env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" } });
  assert.equal(generated.status, 0, generated.stderr || generated.error?.message);
  const { sandbox, limits } = JSON.parse(generated.stdout);

  for (const flag of ["--unshare-net", "--unshare-user", "--unshare-pid", "--die-with-parent", "--new-session", "--clearenv"]) {
    assert.ok(sandbox.includes(flag), `missing ${flag}`);
  }
  assert.ok(!sandbox.some((argument) => argument.startsWith("--share-net")), "the sandbox must never share the host network");
  assert.deepEqual(sandbox.slice(-4), ["/usr/bin/python3", "-I", "-B", "/vanta-runner.py"]);
  assert.equal(sandbox.filter((argument) => argument === "--bind").length, 2, "only the workspace and font cache are writable");
  assert.ok(limits.includes("--as=268435456") && limits.includes("--core=0"));
});

test("Python Compute mounts only selected artifact inputs read-only", () => {
  assert.ok(pythonCommand, "Python 3 is required to test python-compute");
  const generated = spawnSync(pythonCommand, ["-B", "-c", [
    "import json",
    "from sandbox import build_bwrap_argv",
    "present = {'/usr', '/bin', '/lib'}",
    "argv = build_bwrap_argv('/state/calls/x', '/opt/runner.py', exists=lambda p: p in present, islink=lambda p: False, readlink=lambda p: '', has_inputs=True)",
    "print(json.dumps(argv))",
  ].join("\n")], { cwd: moduleDirectory, encoding: "utf8", timeout: 30_000, env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" } });
  assert.equal(generated.status, 0, generated.stderr || generated.error?.message);
  const argv = JSON.parse(generated.stdout);
  const inputBind = argv.indexOf("/inputs");
  assert.ok(inputBind > 0);
  assert.equal(argv[inputBind - 2], "--ro-bind");
  assert.equal(argv[inputBind - 1], "/state/calls/x/inputs");
  assert.ok(!argv.includes("/mnt/ssd"), "the shared store root must not enter the sandbox");
});

test("Python Compute publishes and stages integrity-checked shared artifacts", () => {
  assert.ok(pythonCommand, "Python 3 is required to test python-compute");
  const storeRoot = mkdtempSync(path.join(tmpdir(), "vanta-python-artifacts-"));
  try {
    const script = [
      "import json, os, tempfile",
      "import artifact_io",
      "root = os.environ['TEST_STORE_ROOT']",
      "for name in ('objects', '.uploads', '.reservations'): os.makedirs(os.path.join(root, name), exist_ok=True)",
      "policy = {'totalQuotaBytes': 10_000_000, 'producerQuotaBytes': 10_000_000, 'maxArtifactBytes': 2_000_000, 'defaultRetentionDays': 7, 'maxRetentionDays': 90, 'freeReserveBytes': 0}",
      "open(os.path.join(root, '.store.lock'), 'a').close()",
      "with open(os.path.join(root, '.store.json'), 'w', encoding='utf-8') as handle: json.dump({'protocolVersion': 1, 'policy': policy}, handle)",
      "with tempfile.TemporaryDirectory() as workspace:",
      "    source = os.path.join(workspace, 'result.csv')",
      "    with open(source, 'wb') as handle: handle.write(b'a,b\\n1,2\\n')",
      "    reservation = artifact_io.reserve(root, 'python-compute', 1024)",
      "    published = artifact_io.publish(root, reservation, 'python-compute', [{'path': '/work/result.csv', 'name': 'result.csv', 'mimeType': 'text/csv'}], workspace, 7)",
      "    inputs = os.path.join(workspace, 'inputs')",
      "    paths = artifact_io.stage_inputs(root, [{'artifactId': published[0]['id'], 'name': 'input.csv'}], inputs, 4096)",
      "    assert open(os.path.join(inputs, 'input.csv'), 'rb').read() == b'a,b\\n1,2\\n'",
      "    print(json.dumps({'artifact': published[0], 'paths': paths}))",
    ].join("\n");
    const result = spawnSync(pythonCommand, ["-B", "-c", script], {
      cwd: moduleDirectory,
      encoding: "utf8",
      timeout: 30_000,
      env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1", TEST_STORE_ROOT: storeRoot },
    });
    assert.equal(result.status, 0, result.stderr || result.error?.message);
    const output = JSON.parse(result.stdout);
    assert.equal(output.artifact.producer, "python-compute");
    assert.equal(output.artifact.bytes, 8);
    assert.equal(output.paths["input.csv"], "/inputs/input.csv");
  } finally {
    rmSync(storeRoot, { recursive: true, force: true });
  }
});

test("Python Compute sizes its limits on the node and applies matching cgroup caps", () => {
  const installer = readFileSync(path.join(moduleDirectory, "install.sh"), "utf8");
  const unit = readFileSync(path.join(moduleDirectory, "python-compute.service"), "utf8");
  const uninstaller = readFileSync(path.join(moduleDirectory, "uninstall.sh"), "utf8");

  assert.match(installer, /MemTotal.*\/proc\/meminfo/s, "limits must be derived from the node's memory");
  assert.match(installer, /cores=\$\(nproc\)/);
  for (const variable of ["VANTA_PYTHON_MAX_MEMORY_MB", "VANTA_PYTHON_DEFAULT_MEMORY_MB", "VANTA_PYTHON_MAX_TIMEOUT_MS", "VANTA_PYTHON_CONCURRENT_CALLS", "VANTA_PYTHON_CALLS_PER_MINUTE"]) {
    assert.ok(installer.includes(variable), `the installer must record ${variable}`);
  }
  for (const override of ["VANTA_MODULE_OPTION_MAX_MEMORY_MB", "VANTA_MODULE_OPTION_CONCURRENT_CALLS", "VANTA_MODULE_OPTION_MAX_TIMEOUT_MS", "VANTA_MODULE_OPTION_CALLS_PER_MINUTE"]) {
    assert.ok(installer.includes(override), `${override} must be able to override the derived value`);
  }
  // A raised per-call limit is useless unless the service cgroup is raised with it.
  assert.match(installer, /vantamcpd-python-compute\.service\.d/);
  assert.match(installer, /MemoryHigh=%sM\\nMemoryMax=%sM\\nCPUQuota=%s%%\\nTasksMax=%s/);
  assert.match(installer, /Group=%s\\nReadWritePaths=%s/);
  assert.match(uninstaller, /rm -rf -- \/etc\/systemd\/system\/vantamcpd-python-compute\.service\.d/);
  assert.match(unit, /MemoryMax=700M/, "the shipped unit keeps a conservative floor for small nodes");

  // Verified on Debian 13 / kernel 6.12: these break bubblewrap's mount path, so the sandbox cannot start.
  for (const setting of ["ProtectKernelTunables", "ProtectKernelLogs", "ProtectHostname", "RestrictSUIDSGID"]) {
    assert.ok(!new RegExp(`^${setting}=yes`, "m").test(unit), `${setting}=yes prevents the sandbox from starting`);
  }
  for (const setting of ["NoNewPrivileges=yes", "CapabilityBoundingSet=", "ProtectSystem=strict", "ProtectHome=yes", "PrivateDevices=yes", "RestrictAddressFamilies=AF_UNIX AF_NETLINK"]) {
    assert.ok(unit.includes(setting), `the unit must keep ${setting}`);
  }
});

test("Python Compute declares a job-backed service manifest with bundle options", () => {
  const manifest = JSON.parse(readFileSync(path.join(moduleDirectory, "module.json"), "utf8"));
  assert.equal(manifest.version, "0.2.4");
  assert.equal(manifest.schemaVersion, 2);
  assert.equal(manifest.lifecycle.execution.mode, "job");
  assert.equal(manifest.runtime.mode, "service");
  assert.equal(manifest.deployment.mode, "replicated");
  assert.deepEqual(manifest.artifactAccess, { read: true, write: true });
  assert.equal(manifest.persistentData, undefined, "submitted code must not keep state between calls");
  assert.deepEqual(manifest.installOptions.bundle.values, ["core", "science", "full"]);
  assert.equal(manifest.installOptions.bundle.default, "science");
  for (const option of ["memoryMb", "maxMemoryMb", "maxTimeoutMs", "concurrentCalls", "callsPerMinute"]) {
    // A declared default is always sent to the installer, which would suppress the node-sized derivation.
    assert.equal(manifest.installOptions[option].default, undefined, `${option} must not declare a default`);
    assert.equal(manifest.installOptions[option].type, "integer");
  }
  assert.ok(manifest.compatibility.architectures.includes("armhf"));
  for (const command of ["bwrap", "prlimit", "systemctl"]) {
    assert.ok(manifest.compatibility.requiredCommands.includes(command), `missing required command ${command}`);
  }
  for (const command of ["runuser", "useradd"]) {
    // /usr/sbin is not on the SSH user's PATH, so preflight cannot see these; install.sh checks them as root.
    assert.ok(!manifest.compatibility.requiredCommands.includes(command), `${command} is not visible to preflight`);
  }
});
