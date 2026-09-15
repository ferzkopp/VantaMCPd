import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import path from "node:path";
import test from "node:test";

const moduleDirectory = path.resolve(import.meta.dirname, "..", "modules", "browser-retrieval");
const pythonCommand = ["python3", "python"].find((command) => spawnSync(command, ["--version"], { encoding: "utf8" }).status === 0);

function runPython(file, args = []) {
  assert.ok(pythonCommand, "Python 3 is required to test browser-retrieval");
  return spawnSync(pythonCommand, [file, ...args], {
    cwd: moduleDirectory,
    encoding: "utf8",
    timeout: 30_000,
    env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" },
  });
}

test("Browser Retrieval validates its schemas, extraction limits, and HTTP(S) URL policy", () => {
  for (const file of ["server.py", "browser.py", "network_policy.py", "extraction.py"]) {
    const args = file === "server.py" || file === "network_policy.py" ? ["--self-test"] : [];
    const result = runPython(file, args);
    assert.equal(result.status, 0, result.stderr || result.error?.message);
    assert.equal(result.stderr, "");
  }
});

test("Browser Retrieval emits syntactically valid JavaScript extraction expressions", () => {
  assert.ok(pythonCommand, "Python 3 is required to test browser-retrieval");
  const generated = spawnSync(pythonCommand, ["-B", "-c", [
    "import json",
    "import browser",
    "from extraction import validate_tables",
    "expressions = [",
    "  browser._retrieve_expression(None, 'markdown', 10),",
    "  browser._query_expression([{'name':'heading','selector':'h1','fields':['text'],'limit':1}]),",
    "  browser._tables_expression(validate_tables({'url':'https://example.com'})),",
    "]",
    "print(json.dumps(expressions))",
  ].join("\n")], {
    cwd: moduleDirectory,
    encoding: "utf8",
    timeout: 30_000,
    env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" },
  });
  assert.equal(generated.status, 0, generated.stderr || generated.error?.message);
  for (const expression of JSON.parse(generated.stdout)) new Function(expression);
});

test("Browser Retrieval advertises an amd64 service package with no persistent data", async () => {
  const manifest = JSON.parse(await (await import("node:fs/promises")).readFile(path.join(moduleDirectory, "module.json"), "utf8"));
  assert.equal(manifest.schemaVersion, 1);
  assert.deepEqual(manifest.compatibility.architectures, ["amd64"]);
  assert.equal(manifest.runtime.mode, "service");
  assert.equal(manifest.persistentData, undefined);
  assert.deepEqual(manifest.deployment, { mode: "replicated", routing: "round-robin" });
  assert.deepEqual(manifest.capabilities.length, 4);
});

test("Browser Retrieval allows normal Chromium networking while constraining service resources", async () => {
  const unit = await (await import("node:fs/promises")).readFile(path.join(moduleDirectory, "browser-retrieval.service"), "utf8");
  for (const directive of [
    "NoNewPrivileges=yes",
    "PrivateTmp=yes",
    "ProtectSystem=strict",
    "MemoryMax=1800M",
    "CPUQuota=200%",
    "TasksMax=128",
  ]) assert.match(unit, new RegExp(directive.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
  assert.doesNotMatch(unit, /IPAddressDeny|--no-sandbox|ListenStream|ListenTCP/);
});

test("Browser Retrieval lifecycle smoke-checks Chromium without disabling its sandbox", async () => {
  const install = await (await import("node:fs/promises")).readFile(path.join(moduleDirectory, "install.sh"), "utf8");
  assert.match(install, /VANTA_MODULE_RUN_AS/);
  assert.match(install, /runuser -u "\$service_user"/);
  assert.match(install, /chromium/);
  assert.doesNotMatch(install, /--no-sandbox/);
});

test("Browser Retrieval leaves Chromium networking unrestricted", async () => {
  const browser = await (await import("node:fs/promises")).readFile(path.join(moduleDirectory, "browser.py"), "utf8");
  assert.doesNotMatch(browser, /Fetch\.enable|Network\.setBlockedURLs|Browser\.setDownloadBehavior|dns-over-https|disable-background-networking/);
  const policy = await (await import("node:fs/promises")).readFile(path.join(moduleDirectory, "network_policy.py"), "utf8");
  assert.match(policy, /http:\/\/127\.0\.0\.1:8080/);
});
