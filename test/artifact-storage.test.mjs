import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";

const moduleDirectory = path.resolve(import.meta.dirname, "..", "modules", "artifact-storage");
const pythonCommand = ["python3", "python"].find((command) => spawnSync(command, ["--version"], { encoding: "utf8" }).status === 0);

function runPython(file, args = []) {
  assert.ok(pythonCommand, "Python 3 is required to test artifact-storage");
  return spawnSync(pythonCommand, [file, ...args], {
    cwd: moduleDirectory,
    encoding: "utf8",
    timeout: 30_000,
    env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" },
  });
}

test("Artifact Storage validates its store and MCP schemas", () => {
  for (const [file, args] of [["store.py", []], ["server.py", ["--self-test"]]]) {
    const result = runPython(file, args);
    assert.equal(result.status, 0, result.stderr || result.error?.message);
    assert.equal(result.stderr, "");
  }
});

test("Artifact Storage is a retained singleton service with bounded calls", () => {
  const manifest = JSON.parse(readFileSync(path.join(moduleDirectory, "module.json"), "utf8"));
  const installer = readFileSync(path.join(moduleDirectory, "install.sh"), "utf8");
  assert.equal(manifest.schemaVersion, 2);
  assert.equal(manifest.version, "0.1.1");
  assert.equal(manifest.deployment.mode, "singleton");
  assert.equal(manifest.runtime.mode, "service");
  assert.equal(manifest.persistentData.relativePath, "vantamcpd/artifacts");
  assert.equal(manifest.persistentData.retainOnUninstall, true);
  assert.ok(manifest.limits.maxInputBytes < 1_000_000);
  assert.match(installer, /install -d -m 2770 -o "\$service_user" -g "\$caller_group" "\$VANTA_MODULE_DATA_DIR"/);
});