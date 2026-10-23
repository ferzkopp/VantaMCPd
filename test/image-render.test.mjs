import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

const moduleDirectory = path.resolve(import.meta.dirname, "..", "modules", "image-render");
const pythonCommand = ["python3", "python"].find((command) => spawnSync(command, ["--version"], { encoding: "utf8" }).status === 0);
const stateDirectory = mkdtempSync(path.join(tmpdir(), "vanta-render-state-"));
const pythonEnv = {
  ...process.env,
  PYTHONDONTWRITEBYTECODE: "1",
  PYTHONPATH: path.dirname(moduleDirectory),
  VANTA_RENDER_STATE_DIR: stateDirectory,
};

test.after(() => rmSync(stateDirectory, { recursive: true, force: true }));

function runPython(args, extraEnv = {}, input = undefined) {
  assert.ok(pythonCommand, "Python 3 is required to test image-render");
  return spawnSync(pythonCommand, ["-B", ...args], {
    cwd: moduleDirectory,
    encoding: "utf8",
    timeout: 60_000,
    input,
    env: { ...pythonEnv, ...extraEnv },
  });
}

test("Image Render validates schemas, scene parsing, library, assets, sandbox, broker and adapter", () => {
  for (const args of [
    ["schemas.py"],
    ["scene.py"],
    ["artifact_io.py"],
    ["library.py"],
    ["assets.py"],
    ["gltf_mesh2.py"],
    ["polyhaven.py"],
    ["sandbox.py"],
    ["service.py", "--self-test"],
    ["server.py", "--self-test"],
  ]) {
    const result = runPython(args);
    assert.equal(result.status, 0, `${args[0]}: ${result.stderr || result.error?.message}`);
    assert.equal(result.stderr, "", args[0]);
  }
});

test("Image Render advertises its manifest version and seven strict tools", () => {
  const manifest = JSON.parse(readFileSync(path.join(moduleDirectory, "module.json"), "utf8"));
  const script = [
    "import json, server",
    "server.broker_call = lambda *args, **kwargs: {}",
    "request = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18'}}",
    "print(json.dumps(server.handle_request(request)))",
  ].join("\n");
  const initialized = runPython(["-c", script]);
  assert.equal(initialized.status, 0, initialized.stderr);
  assert.equal(JSON.parse(initialized.stdout).result.serverInfo.version, manifest.version);

  const listed = runPython(["server.py"], {}, `${JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/list", params: {} })}\n`);
  assert.equal(listed.status, 0, listed.stderr);
  const tools = JSON.parse(listed.stdout).result.tools;
  assert.deepEqual(tools.map((tool) => tool.name), ["render_environment", "render_docs", "render_library", "render_polyhaven", "render_check", "render_scene", "render_benchmark"]);
  for (const tool of tools) {
    assert.equal(tool.inputSchema.type, "object");
    assert.equal(tool.annotations.openWorldHint, false);
    assert.equal(tool.annotations.destructiveHint, false);
  }
  const render = tools.find((tool) => tool.name === "render_scene").inputSchema;
  assert.equal(render.additionalProperties, false);
  assert.equal(render.properties.width.maximum, 8192);
  assert.deepEqual(render.properties.scene.oneOf.map((schema) => Object.keys(schema.properties)[0]), ["text", "artifactId", "example"]);
});

test("Image Render rewrites artifact references and reports parse errors with context", () => {
  const script = [
    "import json, scene",
    "a = 'c' * 32",
    "text = 'plane { y, 0 pigment { image_map { png \"artifact:' + a + '\" } } }'",
    "ids = scene.references([text])",
    "rewritten = scene.rewrite(text, {a: '/inputs/cccccccc-photo.png'})",
    "lines = scene.clean_output(\"File '/work/scene.pov' line 2: Parse Error: Expected 'object', undeclared identifier 'spher' found instead\\nFatal error in parser: Cannot parse input.\\n\")",
    "found = scene.diagnostics(lines, {'/work/scene.pov': 'camera {}\\nspher { 0, 1 }\\n'})",
    "print(json.dumps({'ids': ids, 'rewritten': rewritten, 'found': found}))",
  ].join("\n");
  const result = runPython(["-c", script]);
  assert.equal(result.status, 0, result.stderr);
  const output = JSON.parse(result.stdout);
  assert.deepEqual(output.ids, ["c".repeat(32)]);
  assert.match(output.rewritten, /png "\/inputs\/cccccccc-photo\.png"/);
  assert.equal(output.found[0].file, "scene.pov");
  assert.equal(output.found[0].line, 2);
  assert.ok(output.found[0].context.some((row) => row.error && row.text.startsWith("spher")));
});

test("Image Render stages named and referenced artifacts and publishes PNG renders", () => {
  const storeRoot = mkdtempSync(path.join(tmpdir(), "vanta-render-artifacts-"));
  try {
    const script = [
      "import json, os, tempfile",
      "import artifact_io",
      "from artifact_protocol import publish_unreserved",
      "root = os.environ['TEST_STORE_ROOT']",
      "for name in ('objects', '.uploads', '.reservations'): os.makedirs(os.path.join(root, name), exist_ok=True)",
      "policy = {'totalQuotaBytes': 10_000_000, 'producerQuotaBytes': 10_000_000, 'maxArtifactBytes': 2_000_000, 'defaultRetentionDays': 7, 'maxRetentionDays': 90, 'freeReserveBytes': 0}",
      "open(os.path.join(root, '.store.lock'), 'a').close()",
      "json.dump({'protocolVersion': 1, 'policy': policy}, open(os.path.join(root, '.store.json'), 'w'))",
      "with tempfile.TemporaryDirectory() as work:",
      "    source = os.path.join(work, 'source.png')",
      "    open(source, 'wb').write(b'\\x89PNG\\r\\n\\x1a\\nbody')",
      "    stored = publish_unreserved(root, 'test', [{'source': source, 'name': 'my.photo.png', 'mimeType': 'image/png'}], 1_000, 1)[0]",
      "    scene_source = os.path.join(work, 'scene.pov')",
      "    open(scene_source, 'w').write('sphere { 0, 1 }')",
      "    scene_artifact = publish_unreserved(root, 'test', [{'source': scene_source, 'name': 'scene.pov', 'mimeType': 'text/plain'}], 1_000, 1)[0]",
      "    inputs = os.path.join(work, 'inputs')",
      "    os.makedirs(inputs)",
      "    budget = artifact_io.InputBudget(10_000)",
      "    named = artifact_io.stage_named(root, [{'artifactId': stored['id'], 'name': 'photo.png'}], inputs, budget)",
      "    referenced = artifact_io.stage_references(root, [stored['id']], inputs, budget, {'photo.png'})",
      "    text = artifact_io.read_text(root, scene_artifact['id'], work, 1_000)",
      "    reservation = artifact_io.reserve(root, 'image-render', 1_000)",
      "    published = artifact_io.publish(root, reservation, source, 'render.png', 3)",
      "    print(json.dumps({'named': named, 'referenced': referenced, 'text': text, 'published': published, 'files': sorted(os.listdir(inputs))}))",
    ].join("\n");
    const result = runPython(["-c", script], { TEST_STORE_ROOT: storeRoot });
    assert.equal(result.status, 0, result.stderr);
    const output = JSON.parse(result.stdout);
    assert.equal(output.named[0].path, "/inputs/photo.png");
    assert.match(output.referenced[0].path, /^\/inputs\/[0-9a-f]{8}-my\.photo\.png$/);
    assert.equal(output.text, "sphere { 0, 1 }");
    assert.equal(output.published.producer, "image-render");
    assert.equal(output.published.mimeType, "image/png");
    assert.equal(output.files.length, 2);
  } finally {
    rmSync(storeRoot, { recursive: true, force: true });
  }
});

test("Image Render confines POV-Ray and keeps the hardened service contract", () => {
  const manifest = JSON.parse(readFileSync(path.join(moduleDirectory, "module.json"), "utf8"));
  const installer = readFileSync(path.join(moduleDirectory, "install.sh"), "utf8");
  const uninstaller = readFileSync(path.join(moduleDirectory, "uninstall.sh"), "utf8");
  const unit = readFileSync(path.join(moduleDirectory, "image-render.service"), "utf8");
  const policy = readFileSync(path.join(moduleDirectory, "povray.conf"), "utf8");
  const benchmark = readFileSync(path.join(moduleDirectory, "benchmark.ini"), "utf8");
  const assets = JSON.parse(readFileSync(path.join(moduleDirectory, "assets.json"), "utf8"));

  assert.equal(manifest.schemaVersion, 2);
  assert.deepEqual(manifest.background.tools, { render_scene: "optional", render_benchmark: "required" });
  assert.ok(manifest.background.maxTimeoutMs > manifest.installOptions.maxBackgroundTimeoutMs.maximum);
  assert.ok(manifest.limits.callMs > manifest.installOptions.maxTimeoutMs.maximum);
  assert.deepEqual(manifest.artifactAccess, { read: true, write: true });
  assert.match(policy, /^\[File I\/O Security\]\s+restricted/m);
  assert.match(policy, /^\[Shellout Security\]\s+forbidden/m);
  assert.match(policy, /^read\+write\* = \/work$/m);
  assert.ok(!/read\+write.*(\/tmp|\/inputs|%HOME%|= \.)/.test(policy), "only /work may be writable");
  assert.ok(!policy.includes("%HOME%"));
  assert.match(installer, /apt-get install.*povray povray-includes/s);
  assert.match(installer, /apt-get download -q povray-doc povray-examples/);
  assert.match(installer, /sandbox\.py" --smoke-test/);
  assert.match(installer, /library\.py" --build/);
  assert.match(uninstaller, /vantamcpd-image-render\.service\.d/);
  for (const setting of ["NoNewPrivileges=yes", "CapabilityBoundingSet=", "ProtectSystem=strict", "ProtectHome=yes", "PrivateDevices=yes", "RestrictAddressFamilies=AF_UNIX AF_NETLINK"]) {
    assert.ok(unit.includes(setting), `missing ${setting}`);
  }
  for (const setting of ["SystemCallFilter", "RestrictNamespaces", "RestrictSUIDSGID=yes"]) {
    assert.ok(!unit.includes(setting), `${setting} prevents bubblewrap startup`);
  }
  assert.deepEqual(manifest.installOptions.assets.default, ["hdri", "textures", "models"]);
  for (const pack of Object.values(assets.packs)) {
    for (const file of pack.files) {
      assert.match(file.identifier, /^[A-Za-z][A-Za-z0-9_]*$/);
      for (const pinned of [...(file.files ?? [file]), ...(file.detail ?? [])]) {
        assert.match(pinned.sha256, /^[0-9a-f]{64}$/);
        assert.match(pinned.url, /^https:\/\/dl\.polyhaven\.org\//);
        assert.ok(Number.isInteger(pinned.bytes) && pinned.bytes > 0);
      }
    }
  }
  assert.equal(assets.license, "CC0-1.0");
  for (const setting of ["Width=512", "Height=512", "Antialias_Threshold=0.3", "Antialias_Depth=3", "Jitter=On", "Quality=9", "Output_To_File=Off"]) {
    assert.match(benchmark, new RegExp(`^${setting}$`, "m"));
  }
  assert.ok(!/_Command=/.test(benchmark), "benchmark settings must not configure shell-out hooks");
  assert.match(unit, /^ReadWritePaths=-\/var\/lib\/vantamcpd\/module-reports\/image-render$/m);
  for (const file of ["install.sh", "uninstall.sh", "image-render.service", "povray.conf", "povray.ini", "benchmark.ini"]) {
    assert.ok(!readFileSync(path.join(moduleDirectory, file), "utf8").includes("\r"), `${file} must use LF line endings`);
  }
});
