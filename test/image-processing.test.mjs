import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

const moduleDirectory = path.resolve(import.meta.dirname, "..", "modules", "image-processing");
const pythonCommand = ["python3", "python"].find((command) => spawnSync(command, ["--version"], { encoding: "utf8" }).status === 0);

function runPython(file, args = [], extraEnv = {}) {
  assert.ok(pythonCommand, "Python 3 is required to test image-processing");
  return spawnSync(pythonCommand, [file, ...args], {
    cwd: moduleDirectory,
    encoding: "utf8",
    timeout: 60_000,
    env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1", ...extraEnv },
  });
}

test("Image Processing validates schemas, adapters, worker, sandbox, and broker", () => {
  for (const [file, args] of [
    ["schemas.py", []],
    ["artifact_io.py", []],
    ["processing.py", []],
    ["worker.py", ["--self-test"]],
    ["sandbox.py", []],
    ["service.py", ["--self-test"]],
    ["server.py", ["--self-test"]],
  ]) {
    const result = runPython(file, args);
    assert.equal(result.status, 0, result.stderr || result.error?.message);
    assert.equal(result.stderr, "");
  }
});

test("Image Processing advertises its manifest version", () => {
  const manifest = JSON.parse(readFileSync(path.join(moduleDirectory, "module.json"), "utf8"));
  const script = [
    "import json, server",
    "server.broker_call = lambda *args, **kwargs: {}",
    "request = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18'}}",
    "print(json.dumps(server.handle_request(request)))",
  ].join("\n");
  const initialized = runPython("-B", ["-c", script]);
  assert.equal(initialized.status, 0, initialized.stderr || initialized.error?.message);
  assert.equal(JSON.parse(initialized.stdout).result.serverInfo.version, manifest.version);
});

test("Image Processing advertises five strict semantic tools", () => {
  assert.ok(pythonCommand, "Python 3 is required to test image-processing");
  const request = JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/list", params: {} });
  const result = spawnSync(pythonCommand, ["server.py"], {
    cwd: moduleDirectory,
    input: `${request}\n`,
    encoding: "utf8",
    timeout: 30_000,
    env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" },
  });
  assert.equal(result.status, 0, result.stderr || result.error?.message);
  assert.equal(result.stderr, "");
  const response = JSON.parse(result.stdout);
  assert.deepEqual(response.result.tools.map((tool) => tool.name), [
    "image_environment",
    "image_inspect",
    "image_edit",
    "image_compose",
    "image_compare",
  ]);
  for (const tool of response.result.tools) {
    assert.equal(tool.annotations.openWorldHint, false);
    assert.equal(tool.annotations.destructiveHint, false);
    assert.equal(typeof tool.inputSchema, "object");
  }
  assert.equal(response.result.tools.find((tool) => tool.name === "image_inspect").inputSchema.additionalProperties, false);
  const editSchema = response.result.tools.find((tool) => tool.name === "image_edit").inputSchema;
  assert.equal(editSchema.properties.edits.maxItems, 8);
  assert.ok(editSchema.properties.edits.items.oneOf.every((schema) => schema.additionalProperties === false));
  assert.deepEqual(editSchema.properties.edits.items.oneOf.map((schema) => schema.properties.operation.const), [
    "auto_orient", "resize", "crop", "rotate", "flip", "trim", "pad", "grayscale", "invert", "gamma",
    "auto_contrast", "equalize", "threshold", "posterize", "solarize", "hue", "colorize", "opacity",
    "brightness", "contrast", "saturation", "blur", "sharpen",
  ]);
});

test("Image Processing rejects disguised and oversized inline sources", () => {
  assert.ok(pythonCommand, "Python 3 is required to test image-processing");
  const script = [
    "import base64, os, tempfile",
    "import processing, schemas",
    "with tempfile.TemporaryDirectory() as root:",
    "    path = os.path.join(root, 'fake.png')",
    "    open(path, 'wb').write(b'<svg xmlns=\\\"http://www.w3.org/2000/svg\\\"></svg>')",
    "    try: processing.verify_source(path, 'image/png')",
    "    except ValueError as error: assert 'supported' in str(error)",
    "    else: raise AssertionError('SVG content was accepted')",
    "try: schemas.validate_source({'data': base64.b64encode(b'x' * (schemas.MAX_INLINE_BYTES + 1)).decode(), 'mimeType': 'image/png'})",
    "except ValueError as error: assert 'decoded bytes' in str(error)",
    "else: raise AssertionError('oversized inline source was accepted')",
  ].join("\n");
  const result = spawnSync(pythonCommand, ["-B", "-c", script], { cwd: moduleDirectory, encoding: "utf8", timeout: 30_000 });
  assert.equal(result.status, 0, result.stderr || result.error?.message);
});

test("Image Processing confines workers and keeps artifact storage outside the sandbox", () => {
  assert.ok(pythonCommand, "Python 3 is required to test image-processing");
  const script = [
    "import json",
    "from sandbox import build_bwrap_argv, build_limited_argv",
    "present = {'/usr', '/bin', '/lib', '/etc/fonts'}",
    "argv = build_bwrap_argv('/state/calls/x', '/opt/image', exists=lambda p: p in present, islink=lambda p: p in {'/bin', '/lib'}, readlink=lambda p: 'usr/' + p.rsplit('/', 1)[-1])",
    "print(json.dumps({'sandbox': argv, 'limits': build_limited_argv(256, 10)}))",
  ].join("\n");
  const result = spawnSync(pythonCommand, ["-B", "-c", script], { cwd: moduleDirectory, encoding: "utf8", timeout: 30_000 });
  assert.equal(result.status, 0, result.stderr || result.error?.message);
  const { sandbox, limits } = JSON.parse(result.stdout);
  for (const flag of ["--unshare-net", "--unshare-user", "--unshare-pid", "--die-with-parent", "--clearenv"]) {
    assert.ok(sandbox.includes(flag), `missing ${flag}`);
  }
  assert.ok(sandbox.includes("/inputs") && sandbox.includes("/work") && sandbox.includes("/vanta-policy/policy.xml"));
  assert.ok(!sandbox.includes("/mnt/ssd"), "the artifact root must remain outside the sandbox");
  assert.ok(limits.includes("--as=268435456") && limits.includes("--core=0"));
});

test("Image Processing stages and publishes integrity-checked shared artifacts", () => {
  assert.ok(pythonCommand, "Python 3 is required to test image-processing");
  const storeRoot = mkdtempSync(path.join(tmpdir(), "vanta-image-artifacts-"));
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
      "    source = os.path.join(workspace, 'result.png')",
      "    open(source, 'wb').write(b'\\x89PNG\\r\\n\\x1a\\ncontent')",
      "    reservation = artifact_io.reserve(root, 'image-processing', 1024)",
      "    published = artifact_io.publish(root, reservation, source, 'result.png', 'image/png', 7)",
      "    inputs = os.path.join(workspace, 'inputs')",
      "    staged = artifact_io.stage_inputs(root, [{'artifactId': published['id'], 'name': 'input.img'}], inputs, 4096)",
      "    assert open(os.path.join(inputs, 'input.img'), 'rb').read() == b'\\x89PNG\\r\\n\\x1a\\ncontent'",
      "    print(json.dumps({'artifact': published, 'staged': staged}))",
    ].join("\n");
    const result = spawnSync(pythonCommand, ["-B", "-c", script], {
      cwd: moduleDirectory,
      encoding: "utf8",
      timeout: 30_000,
      env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1", TEST_STORE_ROOT: storeRoot },
    });
    assert.equal(result.status, 0, result.stderr || result.error?.message);
    const output = JSON.parse(result.stdout);
    assert.equal(output.artifact.producer, "image-processing");
    assert.equal(output.artifact.mimeType, "image/png");
    assert.equal(output.staged[0].path, "/inputs/input.img");
  } finally {
    rmSync(storeRoot, { recursive: true, force: true });
  }
});

test("Image Processing performs a real Pillow edit when the Debian API is available", (context) => {
  assert.ok(pythonCommand, "Python 3 is required to test image-processing");
  const check = spawnSync(pythonCommand, ["-B", "-c", "import PIL"], { encoding: "utf8" });
  if (check.status !== 0) {
    context.skip("Pillow is not installed on the development host");
    return;
  }
  const directory = mkdtempSync(path.join(tmpdir(), "vanta-image-edit-"));
  try {
    const script = [
      "import json, os",
      "from PIL import Image",
      "import processing",
      "root = os.environ['TEST_IMAGE_ROOT']",
      "source, output = os.path.join(root, 'source.png'), os.path.join(root, 'output.webp')",
      "Image.new('RGB', (16, 8), '#336699').save(source)",
      "backend = processing.edit_image(source, [{'operation': 'resize', 'width': 4, 'height': 4, 'mode': 'stretch'}], output, {'format': 'webp', 'quality': 80, 'stripMetadata': True}, 'pillow', 10)",
      "with Image.open(output) as image: print(json.dumps({'backend': backend, 'size': image.size, 'format': image.format}))",
    ].join("\n");
    const result = spawnSync(pythonCommand, ["-B", "-c", script], {
      cwd: moduleDirectory,
      encoding: "utf8",
      timeout: 30_000,
      env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1", TEST_IMAGE_ROOT: directory },
    });
    assert.equal(result.status, 0, result.stderr || result.error?.message);
    assert.deepEqual(JSON.parse(result.stdout), { backend: "pillow", size: [4, 4], format: "WEBP" });
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("Image Processing preserves alpha across portable color edits", (context) => {
  assert.ok(pythonCommand, "Python 3 is required to test image-processing");
  const check = spawnSync(pythonCommand, ["-B", "-c", "import PIL"], { encoding: "utf8" });
  if (check.status !== 0) {
    context.skip("Pillow is not installed on the development host");
    return;
  }
  const directory = mkdtempSync(path.join(tmpdir(), "vanta-image-color-edits-"));
  try {
    const script = [
      "import json, os",
      "from PIL import Image",
      "import processing",
      "root = os.environ['TEST_IMAGE_ROOT']",
      "source = os.path.join(root, 'source.png')",
      "image = Image.new('RGBA', (2, 1))",
      "image.putdata([(51, 102, 153, 64), (204, 153, 102, 192)])",
      "image.save(source)",
      "edits = [",
      "    {'operation': 'invert'},",
      "    {'operation': 'gamma', 'gamma': 2.2},",
      "    {'operation': 'auto_contrast', 'cutoffPercent': 0},",
      "    {'operation': 'equalize'},",
      "    {'operation': 'threshold', 'thresholdPercent': 50},",
      "    {'operation': 'posterize', 'levels': 4},",
      "    {'operation': 'solarize', 'thresholdPercent': 50},",
      "    {'operation': 'hue', 'degrees': 90},",
      "    {'operation': 'colorize', 'color': '#336699', 'amount': 50},",
      "]",
      "alphas = {}",
      "for index, edit in enumerate(edits):",
      "    output = os.path.join(root, f'output-{index}.png')",
      "    processing.edit_image(source, [edit], output, {'format': 'png', 'quality': 85, 'stripMetadata': True}, 'pillow', 10)",
      "    with Image.open(output) as result: alphas[edit['operation']] = list(result.convert('RGBA').getchannel('A').getdata())",
      "opacity_output = os.path.join(root, 'opacity.png')",
      "processing.edit_image(source, [{'operation': 'opacity', 'amount': 50}], opacity_output, {'format': 'png', 'quality': 85, 'stripMetadata': True}, 'pillow', 10)",
      "with Image.open(opacity_output) as result: opacity = list(result.getchannel('A').getdata())",
      "print(json.dumps({'alphas': alphas, 'opacity': opacity}))",
    ].join("\n");
    const result = spawnSync(pythonCommand, ["-B", "-c", script], {
      cwd: moduleDirectory,
      encoding: "utf8",
      timeout: 30_000,
      env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1", TEST_IMAGE_ROOT: directory },
    });
    assert.equal(result.status, 0, result.stderr || result.error?.message);
    const output = JSON.parse(result.stdout);
    for (const alpha of Object.values(output.alphas)) assert.deepEqual(alpha, [64, 192]);
    assert.deepEqual(output.opacity, [32, 96]);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("Image Processing creates montages through the portable Pillow path", (context) => {
  assert.ok(pythonCommand, "Python 3 is required to test image-processing");
  const check = spawnSync(pythonCommand, ["-B", "-c", "import PIL"], { encoding: "utf8" });
  if (check.status !== 0) {
    context.skip("Pillow is not installed on the development host");
    return;
  }
  const directory = mkdtempSync(path.join(tmpdir(), "vanta-image-montage-"));
  try {
    const script = [
      "import json, os",
      "from PIL import Image",
      "import processing",
      "root = os.environ['TEST_IMAGE_ROOT']",
      "sources = []",
      "for index, color in enumerate(('#ff0000', '#00ff00', '#0000ff')):",
      "    source = os.path.join(root, f'source-{index}.png')",
      "    Image.new('RGB', (16, 8), color).save(source)",
      "    sources.append(source)",
      "output = os.path.join(root, 'montage.png')",
      "request = {'operation': 'montage', 'columns': 2, 'cellWidth': 20, 'cellHeight': 10, 'background': '#000000FF', 'output': {'format': 'png', 'quality': 85, 'stripMetadata': True}}",
      "backend = processing.compose_image(request, sources, output, 'imagemagick', 10)",
      "with Image.open(output) as image: print(json.dumps({'backend': backend, 'size': image.size, 'format': image.format}))",
    ].join("\n");
    const result = spawnSync(pythonCommand, ["-B", "-c", script], {
      cwd: moduleDirectory,
      encoding: "utf8",
      timeout: 30_000,
      env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1", TEST_IMAGE_ROOT: directory },
    });
    assert.equal(result.status, 0, result.stderr || result.error?.message);
    assert.deepEqual(JSON.parse(result.stdout), { backend: "pillow", size: [40, 20], format: "PNG" });
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("Image Processing lifecycle and policy keep the hardened service contract", () => {
  const manifest = JSON.parse(readFileSync(path.join(moduleDirectory, "module.json"), "utf8"));
  const installer = readFileSync(path.join(moduleDirectory, "install.sh"), "utf8");
  const uninstaller = readFileSync(path.join(moduleDirectory, "uninstall.sh"), "utf8");
  const unit = readFileSync(path.join(moduleDirectory, "image-processing.service"), "utf8");
  const policy = readFileSync(path.join(moduleDirectory, "policy.xml"), "utf8");

  assert.equal(manifest.version, "0.2.5");
  assert.equal(manifest.schemaVersion, 2);
  assert.deepEqual(manifest.artifactAccess, { read: true, write: true });
  assert.deepEqual(manifest.installOptions.backend.values, ["auto", "imagemagick", "pillow"]);
  assert.ok(manifest.compatibility.architectures.includes("armhf"));
  assert.match(installer, /apt-get install.*python3-pil.*fonts-dejavu-core/s);
  assert.match(installer, /apt-get install.*imagemagick/s);
  assert.match(installer, /MemTotal.*\/proc\/meminfo/s);
  assert.match(installer, /VANTA_ARTIFACT_ROOT/);
  assert.match(installer, /"\$state_dir" "\$state_dir\/calls"/);
  assert.match(installer, /sandbox\.py" --smoke-test/);
  assert.match(uninstaller, /vantamcpd-image-processing\.service\.d/);
  for (const setting of ["NoNewPrivileges=yes", "CapabilityBoundingSet=", "ProtectSystem=strict", "ProtectHome=yes", "PrivateDevices=yes", "RestrictAddressFamilies=AF_UNIX AF_NETLINK"]) {
    assert.ok(unit.includes(setting), `missing ${setting}`);
  }
  for (const setting of ["ProtectKernelTunables=yes", "ProtectKernelLogs=yes", "ProtectHostname=yes", "RestrictSUIDSGID=yes"]) {
    assert.ok(!unit.includes(setting), `${setting} prevents bubblewrap startup`);
  }
  assert.match(policy, /domain="delegate" rights="none" pattern="\*"/);
  assert.match(policy, /domain="coder" rights="none" pattern="\*"/);
  assert.match(policy, /\{PNG,JPEG,JPG,WEBP,GIF\}/);
  for (const forbidden of ["SVG", "PDF", "PS", "EPS", "HTTP", "HTTPS", "MVG"]) {
    assert.ok(!policy.includes(`pattern="${forbidden}"`), `${forbidden} must not be allowlisted`);
  }
});