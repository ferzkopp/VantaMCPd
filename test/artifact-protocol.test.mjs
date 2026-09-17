import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

const root = path.resolve(import.meta.dirname, "..");
const protocolPath = path.join(root, "modules", "artifact_protocol.py");
const pythonCommand = ["python3", "python"].find((command) => spawnSync(command, ["--version"], { encoding: "utf8" }).status === 0);

function runPython(script, extraEnv = {}) {
  assert.ok(pythonCommand, "Python 3 is required to test the shared artifact protocol");
  return spawnSync(pythonCommand, ["-B", "-c", script], {
    cwd: root,
    encoding: "utf8",
    timeout: 30_000,
    env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1", PYTHONPATH: path.join(root, "modules"), ...extraEnv },
  });
}

test("every shared artifact protocol function has a docstring", () => {
  const script = [
    "import ast, pathlib",
    `tree = ast.parse(pathlib.Path(${JSON.stringify(protocolPath)}).read_text(encoding='utf-8'))`,
    "missing = [node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not ast.get_docstring(node)]",
    "assert not missing, f'missing docstrings: {missing}'",
  ].join("\n");
  const result = runPython(script);
  assert.equal(result.status, 0, result.stderr || result.error?.message);
});

test("shared artifact protocol validates boundaries and cleans failed copies", () => {
  const storeRoot = mkdtempSync(path.join(tmpdir(), "vanta-shared-artifacts-"));
  try {
    const script = [
      "import hashlib, json, os, time",
      "import artifact_protocol as protocol",
      "root = os.environ['TEST_STORE_ROOT']",
      "for name in ('objects', '.uploads', '.reservations'): os.makedirs(os.path.join(root, name), exist_ok=True)",
      "policy = {'totalQuotaBytes': 10_000, 'producerQuotaBytes': 10_000, 'maxArtifactBytes': 2_000, 'defaultRetentionDays': 7, 'maxRetentionDays': 90, 'freeReserveBytes': 0}",
      "open(os.path.join(root, '.store.lock'), 'a').close()",
      "with open(os.path.join(root, '.store.json'), 'w', encoding='utf-8') as handle: json.dump({'protocolVersion': 1, 'policy': policy}, handle)",
      "artifact_id = 'a' * 32",
      "directory = protocol.object_dir(root, artifact_id)",
      "os.makedirs(directory)",
      "content = os.path.join(directory, 'content')",
      "open(content, 'wb').write(b'payload')",
      "metadata = {'id': artifact_id, 'bytes': 7, 'sha256': '0' * 64, 'expiresEpoch': time.time() + 60}",
      "with open(os.path.join(directory, 'metadata.json'), 'w', encoding='utf-8') as handle: json.dump(metadata, handle)",
      "target = os.path.join(root, 'staged.bin')",
      "try: protocol.copy_verified(root, artifact_id, target, 100)",
      "except ValueError: pass",
      "else: raise AssertionError('invalid digest was accepted')",
      "assert not os.path.exists(target)",
      "for call in (lambda: protocol.reserve(root, '../producer', 1), lambda: protocol.reserve(root, 'producer', -1), lambda: protocol.release(root, '../reservation')):",
      "    try: call()",
      "    except ValueError: pass",
      "    else: raise AssertionError('invalid boundary value was accepted')",
      "source = os.path.join(root, 'output.bin')",
      "open(source, 'wb').write(b'ok')",
      "reservation = protocol.reserve(root, 'producer', 10)",
      "try: protocol.publish_reserved(root, reservation, 'producer', [{'source': source, 'name': 'output.bin', 'mimeType': 'bad mime'}], 7)",
      "except ValueError: pass",
      "else: raise AssertionError('invalid MIME type was accepted')",
      "assert os.path.isfile(os.path.join(root, '.reservations', reservation))",
      "protocol.release(root, reservation)",
      "malformed = 'b' * 32",
      "with open(os.path.join(root, '.reservations', malformed), 'w', encoding='utf-8') as handle: json.dump({'id': malformed, 'producer': 'producer', 'declaredBytes': True}, handle)",
      "try: protocol.publish_reserved(root, malformed, 'producer', [{'source': source, 'name': 'output.bin', 'mimeType': 'application/octet-stream'}], 7)",
      "except ValueError: pass",
      "else: raise AssertionError('boolean reservation budget was accepted')",
      "assert os.path.isfile(os.path.join(root, '.reservations', malformed))",
      "protocol.release(root, malformed)",
    ].join("\n");
    const result = runPython(script, { TEST_STORE_ROOT: storeRoot });
    assert.equal(result.status, 0, result.stderr || result.error?.message);
  } finally {
    rmSync(storeRoot, { recursive: true, force: true });
  }
});
