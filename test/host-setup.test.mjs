import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import test from "node:test";
import { linuxOptions, preparationScript, windowsInvocation } from "../scripts/host-setup.mjs";

test("host helpers preserve native Windows switches and translate portable options", () => {
  const command = windowsInvocation("bootstrap", ["--config", "inventory with spaces.json", "--nodes", "storage-a", "-Verify"]);
  assert.ok(command[command.indexOf("-File") + 1].endsWith("bootstrap.ps1"));
  assert.deepEqual(command.slice(-5), ["-ConfigPath", "inventory with spaces.json", "-Nodes", "storage-a", "-Verify"]);
  assert.throws(() => windowsInvocation("unknown", []), /Unknown/);
});

test("Linux setup options are explicit and reject unknown or incomplete switches", () => {
  assert.deepEqual(linuxOptions(["--config", "/tmp/inventory.json", "--nodes", "worker-a,storage-a", "--verify-sources"]),
    { config: "/tmp/inventory.json", nodes: "worker-a,storage-a", "verify-sources": true });
  for (const args of [["--config"], ["--config", "--check"], ["--configure-sources"], ["--unknown"], ["-Check"]]) {
    assert.throws(() => linuxOptions(args), /Unsupported|requires/);
  }
  assert.throws(() => linuxOptions(["--check"], "bootstrap"), /Unsupported/);
  assert.throws(() => linuxOptions(["--verify"], "prepare-nodes"), /Unsupported/);
  assert.throws(() => linuxOptions(["--verify", "--install-baseline"], "bootstrap"), /cannot install/);
});

test("portable setup help performs no enrollment or package operations", () => {
  for (const action of ["prereqs", "bootstrap", "prepare-nodes", "sources"]) {
    const result = spawnSync(process.execPath, ["scripts/host-setup.mjs", action, "--help"], { encoding: "utf8" });
    assert.equal(result.status, 0, result.stderr);
    assert.match(result.stdout, /Windows.*delegates/);
    assert.match(result.stdout, /Linux SMB mounts and credentials are administrator-managed/);
  }
});

test("Linux wrappers retain arguments and remote preparation protects script stdin and sudo environment", () => {
  for (const filename of ["bootstrap", "prepare-nodes", "install-prereqs", "smb-source"]) {
    const script = readFileSync(`scripts/${filename}.sh`, "utf8");
    assert.ok(script.startsWith("#!/bin/sh\nset -eu\n"));
    assert.match(script, /exec node .*host-setup\.mjs.*"\$@"/);
  }
  assert.match(preparationScript, /sudo -n env DEBIAN_FRONTEND=noninteractive/);
  assert.equal(preparationScript.split("\n").filter((line) => line.includes("apt-get") && line.endsWith("</dev/null")).length, 2);
  assert.match(preparationScript, /\[ "\$mode" = install \] \|\| exit 0/);
});