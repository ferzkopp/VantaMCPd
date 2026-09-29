import assert from "node:assert/strict";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import { loadConfig } from "../dist/config.js";

/** loadConfig only reads the file it is given, so each case gets a throwaway inventory. */
function withConfig(nodes, run) {
  const directory = mkdtempSync(path.join(tmpdir(), "vanta-config-"));
  const file = path.join(directory, "cluster.config.json");
  writeFileSync(file, JSON.stringify({ defaults: { user: "vanta" }, nodes }, null, 2));
  try {
    return run(file);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
}

const storage = {
  device: "/dev/sda1",
  mountpoint: "/mnt/ssd",
  nfs: { enabled: true, network: "10.0.0.0/24" },
};

test("every role token becomes a tag, so gpu nodes are targetable like storage nodes", () => {
  withConfig(
    [
      { name: "a", host: "10.0.0.1", role: "worker+gpu" },
      { name: "b", host: "10.0.0.2", role: "control+worker+storage+gpu", storage },
      { name: "c", host: "10.0.0.3", role: "worker", tags: ["amd64"] },
    ],
    (file) => {
      const config = loadConfig(file);
      assert.deepEqual(config.nodes[0].tags, ["worker", "gpu"]);
      assert.deepEqual(config.nodes[1].tags, ["control", "worker", "storage", "gpu"]);
      assert.deepEqual(config.nodes[2].tags, ["worker", "amd64"]);
    },
  );
});

test("the roles that existed before gpu was a token still load", () => {
  const roles = ["worker", "worker+storage", "control", "control+worker", "control+storage", "storage"];
  withConfig(
    roles.map((role, index) => ({
      name: `n${index}`,
      host: `10.0.0.${index + 1}`,
      role,
      ...(role.includes("storage") ? { storage } : {}),
    })),
    (file) => {
      assert.deepEqual(loadConfig(file).nodes.map((node) => node.role), roles);
    },
  );
});

test("a malformed role is rejected at load rather than silently becoming a tag", () => {
  for (const [role, expected] of [
    ["worker+gpu+gpu", /repeated role token/],
    ["worker+cuda", /unknown role token\(s\) cuda/],
    ["gpu", /needs one of control, worker, storage/],
    ["", /too_small|String must contain at least 1/],
  ]) {
    withConfig([{ name: "a", host: "10.0.0.1", role }], (file) => {
      assert.throws(() => loadConfig(file), expected, `role ${JSON.stringify(role)} must be rejected`);
    });
  }
});

test("a gpu role does not imply a storage block, but a storage role still does", () => {
  withConfig([{ name: "a", host: "10.0.0.1", role: "worker+gpu" }], (file) => {
    assert.equal(loadConfig(file).nodes[0].storage, undefined);
  });
  withConfig([{ name: "a", host: "10.0.0.1", role: "worker+storage" }], (file) => {
    assert.throws(() => loadConfig(file), /no "storage" block/);
  });
});
