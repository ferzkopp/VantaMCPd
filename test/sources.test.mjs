import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
import { mkdtempSync, mkdirSync, readFileSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { access, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { configuredSource, sourcePath, SourceClient, SourceError } from "../dist/sources.js";
import { downloadArtifact } from "../dist/artifact-download.js";
import { withArtifactSession } from "../dist/artifact-upload.js";
import { linuxSourceRunner, validateLinuxMount } from "../dist/sources-linux.js";

const source = { id: "images", type: "smb", server: "192.168.42.32", share: "VantaShare", root: "", credentialRef: "images",
  enabled: true, access: "read-only", maxImportMb: 1 };
const config = { sources: [source] };

const mountLine = (mount, options = "vers=3.1.1,seal", device = "//192.168.42.32/VantaShare", filesystem = "cifs") =>
  `41 30 0:55 / ${mount.replaceAll("\\", "\\134").replaceAll(" ", "\\040")} rw - ${filesystem} ${device} rw,${options}`;

test("Linux mounts require the exact approved share, SMB3 encryption and no nested mounts", () => {
  const configured = { ...source, mountPath: "/mnt/vanta share" };
  assert.equal(validateLinuxMount(configured, mountLine(configured.mountPath)), path.resolve(configured.mountPath));
  for (const info of ["", mountLine("/elsewhere"), mountLine(configured.mountPath, "vers=3.1.1"),
    mountLine(configured.mountPath, "vers=2.1,seal"), mountLine(configured.mountPath, "vers=3.1.1,seal", "//other/share"),
    mountLine(configured.mountPath, "vers=3.1.1,seal", "//192.168.42.32/VantaShare", "ext4"),
    mountLine(configured.mountPath) + "\n" + mountLine(configured.mountPath + "/nested")]) {
    assert.throws(() => validateLinuxMount(configured, info), /SOURCE_MOUNT_INVALID/);
  }
  assert.throws(() => validateLinuxMount(source, mountLine("/mnt/share")), /SOURCE_MOUNT_INVALID/);
});

test("Linux source backend browses, imports glob matches, exports without overwrite and cleans exact temporaries", async () => {
  const directory = mkdtempSync(path.join(tmpdir(), "vanta-linux-source-"));
  const root = path.join(directory, "share");
  mkdirSync(path.join(root, "sub"), { recursive: true });
  mkdirSync(path.join(directory, "outside"));
  symlinkSync(path.join(directory, "outside"), path.join(root, "escape"), process.platform === "win32" ? "junction" : "dir");
  writeFileSync(path.join(root, "image.png"), Buffer.alloc(600000, 42));
  writeFileSync(path.join(root, "sub", "nested.png"), "nested");
  const linuxConfig = { sources: [{ ...source, mountPath: root, access: "read-write" }] };
  const run = linuxSourceRunner(linuxConfig, async () => mountLine(root));
  const client = new SourceClient(linuxConfig, run);
  try {
    assert.equal((await client.check("images")).ok, true);
    const results = await client.importFiles("images", ["**/*.png"], async (file, relative, sha256) => {
      assert.equal(createHash("sha256").update(readFileSync(file)).digest("hex"), sha256);
      return { artifactId: relative };
    });
    assert.equal(results.imported, 2);
    assert.equal(results.stagedBytes, 600006);
    const browse = await client.request("images", "browse", { path: "", limit: 1 });
    assert.equal(browse.entries.length, 1);
    assert.equal(browse.truncated, true);
    const search = await client.request("images", "search", { path: "", query: "nested" });
    assert.deepEqual(search.entries.map((entry) => entry.path), ["sub/nested.png"]);
    await assert.rejects(client.request("images", "stat", { path: "escape" }), /SMB_ACCESS_FAILED/);
    const download = async (file) => {
      await writeFile(file, "output");
      return { bytes: 6, sha256: createHash("sha256").update("output").digest("hex") };
    };
    assert.equal((await client.exportFile("images", "sub/result.png", download)).exported, true);
    await assert.rejects(client.exportFile("images", "sub/result.png", download), /DESTINATION_EXISTS/);
    await assert.rejects(client.exportFile("images", "sub/corrupt.png", async (file) => {
      await writeFile(file, "output"); return { bytes: 6, sha256: "0".repeat(64) };
    }), /SMB_ACCESS_FAILED/);
    assert.equal(readFileSync(path.join(root, "sub", "result.png"), "utf8"), "output");
    const entries = (await client.request("images", "browse", { path: "sub" })).entries;
    assert.deepEqual(entries.map((entry) => entry.name).sort(), ["nested.png", "result.png"]);
    const readonly = linuxSourceRunner({ sources: [{ ...source, mountPath: root }] }, async () => mountLine(root));
    await assert.rejects(readonly({ sourceId: "images", operation: "check-write" }), /SOURCE_READ_ONLY/);
    await assert.rejects(run({ sourceId: "images", operation: "check" }, AbortSignal.abort()), /abort/i);
  } finally { rmSync(directory, { recursive: true, force: true }); }
});

test("source paths reject Windows escapes and device names", () => {
  for (const value of ["../x", "/root", "folder\\x", "x:stream", "x/", "NUL.png", "a./x", "a /x", "a//x", "x\u0000", "a/*"]) {
    assert.throws(() => sourcePath(value), /safe slash-separated/);
  }
  assert.equal(sourcePath("subfolder/image 1.png"), "subfolder/image 1.png");
  assert.throws(() => configuredSource(config, "unknown"), /unknown or disabled/);
});

test("source discovery passes only fixed source operations and supports cancellation", async () => {
  let request;
  const client = new SourceClient(config, async (value) => { request = value; return { entries: [] }; });
  await client.request("images", "browse", { path: "subfolder", limit: 20 });
  assert.deepEqual(request, { sourceId: "images", operation: "browse", path: "subfolder", limit: 20 });
  assert.equal(JSON.stringify(request).includes("credential"), false);
  await assert.rejects(client.request("images", "stat", {}, AbortSignal.abort()), /abort/i);
});

test("source checks return firewall or credential guidance without exception contents", async () => {
  for (const code of ["TCP_UNREACHABLE", "CREDENTIAL_MISSING", "SMB_ACCESS_FAILED"]) {
    const client = new SourceClient(config, async () => { throw new SourceError(code); });
    const result = await client.check("images");
    assert.equal(result.ok, false);
    assert.equal(result.message, code);
    assert.ok(result.guidance.length > 0);
    if (code === "TCP_UNREACHABLE") assert.match(result.guidance.join(" "), /firewall/i);
  }
  const result = await new SourceClient(config, async () => { throw new Error("secret-do-not-return"); }).check("images");
  assert.ok(!JSON.stringify(result).includes("secret-do-not-return"));
});

test("source import stages exact bytes and cleans its temporary directory on success or failure", async () => {
  let staged;
  const client = new SourceClient(config, async (request) => {
    staged = request.stagePath;
    await writeFile(staged, "image");
    return { bytes: 5, modifiedAt: "2026-10-22T00:00:00Z" };
  });
  const result = await client.importFile("images", "sub/image.png", async (file) => {
    assert.equal(file, staged);
    return { artifactId: "abc" };
  });
  assert.equal(result.artifact.artifactId, "abc");
  assert.equal(result.source.path, "sub/image.png");
  await assert.rejects(access(staged));
  await assert.rejects(client.importFile("images", "image.png", async () => { throw new Error("upload failed"); }), /upload failed/);
  await assert.rejects(access(staged));
});

test("invalid staging size and concurrent operations fail closed", async () => {
  const client = new SourceClient(config, async (request) => {
    await writeFile(request.stagePath, "image");
    return { bytes: 99 };
  });
  await assert.rejects(client.importFile("images", "image.png", async () => assert.fail("must not upload")), /invalid size/);
  let release;
  const blocked = new SourceClient(config, () => new Promise((resolve) => { release = resolve; }));
  const pending = blocked.request("images", "stat", {});
  await assert.rejects(blocked.request("images", "stat", {}), /Another SMB/);
  release({});
  await pending;
});

test("Windows helper discovers subfolders, stages exact bytes, and rejects reparse escapes", { skip: process.platform !== "win32" }, () => {
  const directory = mkdtempSync(path.join(tmpdir(), "vanta-smb-test-"));
  const root = path.join(directory, "share");
  mkdirSync(path.join(root, "sub"), { recursive: true });
  mkdirSync(path.join(directory, "outside"));
  writeFileSync(path.join(root, "image.png"), Buffer.from([0, 255, 12, 34]));
  writeFileSync(path.join(root, "sub", "nested.png"), "nested");
  symlinkSync(path.join(directory, "outside"), path.join(root, "escape"), "junction");
  const quote = (value) => `'${value.replaceAll("'", "''")}'`;
  const helper = path.resolve("scripts/smb-source.ps1");
  const stage = path.join(directory, "staged");
  const script = `
    . ${quote(helper)} -ConfigPath unused
    function Get-Source { return [pscustomobject]@{ id = 'images'; root = ''; maxImportMb = 1; access = 'read-write' } }
    function Connect-Source { return ${quote(root)} }
    $browse = Invoke-SourceRequest @{ sourceId='images'; operation='browse'; path=''; limit=1; offset=0 }
    $search = Invoke-SourceRequest @{ sourceId='images'; operation='search'; path=''; query='.png'; limit=50; offset=0 }
    $stage = Invoke-SourceRequest @{ sourceId='images'; operation='stage'; path='image.png'; stagePath=${quote(stage)} }
    $scan = Invoke-SourceRequest @{ sourceId='images'; operation='scan'; path=''; limit=5000; offset=0; recursive=$true }
    if ($scan.entries.Count -ne 2 -or $scan.truncated) { throw 'Recursive batch scan failed.' }
    $shallow = Invoke-SourceRequest @{ sourceId='images'; operation='scan'; path=''; limit=5000; offset=0; recursive=$false }
    if ($shallow.entries.Count -ne 1) { throw 'Shallow batch scan failed.' }
    $writeCheck = Invoke-SourceRequest @{ sourceId='images'; operation='check-write'; path='' }
    $hash = (Get-FileHash -LiteralPath ${quote(stage)} -Algorithm SHA256).Hash.ToLowerInvariant()
    $published = Invoke-SourceRequest @{ sourceId='images'; operation='export'; path='sub/combined.png'; stagePath=${quote(stage)}; bytes=4; sha256=$hash }
    $collision = $false
    try { $null = Invoke-SourceRequest @{ sourceId='images'; operation='export'; path='sub/combined.png'; stagePath=${quote(stage)}; bytes=4; sha256=$hash } } catch { $collision = $_.Exception.Message -eq 'DESTINATION_EXISTS' }
    $corrupt = $false
    try { $null = Invoke-SourceRequest @{ sourceId='images'; operation='export'; path='sub/corrupt.png'; stagePath=${quote(stage)}; bytes=4; sha256=('0' * 64) } } catch { $corrupt = $true }
    $rejected = $false
    try { $null = Invoke-SourceRequest @{ sourceId='images'; operation='stat'; path='escape' } } catch { $rejected = $true }
    $leftovers = @(Get-ChildItem -LiteralPath ${quote(path.join(root, "sub"))} -Force | Where-Object Name -Like '.vanta-*').Count
    @{ browse=$browse; search=$search; stage=$stage; rejected=$rejected; writable=$writeCheck.writable; published=$published; collision=$collision; corrupt=$corrupt; leftovers=$leftovers } | ConvertTo-Json -Depth 12 -Compress
  `;
  try {
    const result = spawnSync("powershell.exe", ["-NoProfile", "-NonInteractive", "-EncodedCommand", Buffer.from(script, "utf16le").toString("base64")], { encoding: "utf8", timeout: 15_000 });
    assert.equal(result.status, 0, result.stderr);
    const response = JSON.parse(result.stdout.replace(/^\uFEFF/, ""));
    assert.equal(response.browse.entries.length, 1);
    assert.equal(response.browse.nextOffset, 1);
    assert.equal(response.browse.truncated, true);
    assert.deepEqual(response.search.entries.map((entry) => entry.path).sort(), ["image.png", "sub/nested.png"]);
    assert.equal(response.stage.bytes, 4);
    assert.equal(response.stage.sha256, createHash("sha256").update(Buffer.from([0, 255, 12, 34])).digest("hex"));
    assert.equal(response.rejected, true);
    assert.equal(response.writable, true);
    assert.equal(response.published.exported, true);
    assert.equal(response.collision, true);
    assert.equal(response.corrupt, true);
    assert.equal(response.leftovers, 0);
    assert.deepEqual(readFileSync(path.join(root, "sub", "combined.png")), Buffer.from([0, 255, 12, 34]));
    assert.deepEqual(readFileSync(stage), Buffer.from([0, 255, 12, 34]));
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("export gates write access and cleans host staging after publication failure", async () => {
  const readonly = new SourceClient(config, async () => assert.fail("read-only sources must not be probed"));
  assert.equal((await readonly.checkWritable("images")).writable, false);
  await assert.rejects(readonly.exportFile("images", "out.png", async () => assert.fail("must not download")), /unavailable/);
  let staged;
  const client = new SourceClient({ sources: [{ ...source, access: "read-write" }] }, async (request) => {
    if (request.operation === "check-write") return { writable: true };
    throw new SourceError("DESTINATION_EXISTS");
  });
  assert.equal((await client.checkWritable("images")).writable, true);
  assert.equal(client.canExport("images"), true);
  await assert.rejects(client.exportFile("images", "out.png", async (file) => {
    staged = file;
    await writeFile(file, "test");
    return { bytes: 4, sha256: createHash("sha256").update("test").digest("hex") };
  }), /DESTINATION_EXISTS/);
  await assert.rejects(access(staged));
  assert.equal(client.canExport("images"), false);
});

test("artifact download checks chunks, whole checksum, limits, cancellation and storage pinning", async () => {
  const directory = mkdtempSync(path.join(tmpdir(), "vanta-download-test-"));
  const id = "a".repeat(32);
  const payload = Buffer.alloc(512 * 1024 + 3, 42);
  const sha256 = createHash("sha256").update(payload).digest("hex");
  const node = { name: "storage" };
  let corruptChunk = false;
  let corruptWhole = false;
  let calls = 0;
  const ctx = { config: { nodes: [node] }, modules: { callTool: async (_module, selected, _tool, args) => {
    calls++;
    if (args.operation === "info") return { ok: true, node: node.name, output: { id, bytes: payload.length, sha256: corruptWhole ? "0".repeat(64) : sha256 } };
    assert.equal(selected, node);
    assert.ok(args.length <= 256 * 1024, "base64 appears in both text and structured MCP output; reads must fit the transport budget");
    const chunk = payload.subarray(args.offset, args.offset + args.length);
    return { ok: true, node: node.name, output: { artifactId: id, encoding: "base64", offset: args.offset, bytes: chunk.length,
      nextOffset: args.offset + chunk.length, data: chunk.toString("base64"), sha256: corruptChunk ? "0".repeat(64) : createHash("sha256").update(chunk).digest("hex") } };
  } } };
  try {
    const file = path.join(directory, "valid");
    assert.deepEqual(await downloadArtifact(ctx, id, file, payload.length), { bytes: payload.length, sha256 });
    assert.deepEqual(readFileSync(file), payload);
    assert.equal(calls, 4);
    await assert.rejects(downloadArtifact(ctx, id, path.join(directory, "large"), 1), /limit/);
    corruptChunk = true;
    await assert.rejects(downloadArtifact(ctx, id, path.join(directory, "chunk"), payload.length), /chunk verification/);
    corruptChunk = false; corruptWhole = true;
    await assert.rejects(downloadArtifact(ctx, id, path.join(directory, "whole"), payload.length), /checksum verification/);
    await assert.rejects(downloadArtifact(ctx, id, path.join(directory, "cancel"), payload.length, AbortSignal.abort()), /abort/i);
  } finally { rmSync(directory, { recursive: true, force: true }); }
});

test("glob imports deduplicate overlapping matches and reject unsafe or over-budget selections before copying", async () => {
  let truncated = false;
  let stages = 0;
  const entries = [ { path: "Screenshot 1.PNG", directory: false, bytes: 4 }, { path: "sub/Screenshot 2.png", directory: false, bytes: 4 },
    { path: "sub/other.jpg", directory: false, bytes: 4 } ];
  const client = new SourceClient(config, async (request) => {
    if (request.operation === "scan") return { entries, truncated };
    stages++;
    await writeFile(request.stagePath, "test");
    return { bytes: 4, sha256: createHash("sha256").update("test").digest("hex") };
  });
  const uploaded = [];
  const upload = async (_file, relative, sha256) => { uploaded.push(relative); assert.equal(sha256.length, 64); return { artifactId: relative }; };
  const result = await client.importFiles("images", ["**/Screenshot*.png", "*.PNG"], upload);
  assert.equal(result.matched, 2);
  assert.equal(result.imported, 2);
  assert.equal(stages, 2);
  assert.deepEqual(uploaded.sort(), ["Screenshot 1.PNG", "sub/Screenshot 2.png"]);
  await assert.rejects(client.importFiles("images", ["**/*"], upload, { maxFiles: 1 }), /maximum/);
  for (const pattern of ["../*.png", "/*.png", "sub\\*.png", "a//*.png", "C:*.png"]) {
    await assert.rejects(client.importFiles("images", [pattern], upload), /safe source-relative/);
  }
  truncated = true;
  await assert.rejects(client.importFiles("images", ["**/*.png"], upload), /incomplete/);
  assert.equal(stages, 2);
});

test("batch imports report partial failures and enforce actual staged byte limits", async () => {
  let calls = 0;
  const sized = { ...source, maxImportMb: 2 };
  const client = new SourceClient({ sources: [sized] }, async (request) => {
    if (request.operation === "scan") return { truncated: false, entries: ["a.png", "b.png"].map((relative) => ({ path: relative, bytes: 1, directory: false })) };
    const content = Buffer.alloc(700 * 1024, 42);
    await writeFile(request.stagePath, content);
    return { bytes: content.length };
  });
  const result = await client.importFiles("images", ["*.png"], async () => { calls++; return {}; }, { maxTotalMb: 1 });
  assert.equal(calls, 1);
  assert.equal(result.imported, 1);
  assert.equal(result.complete, false);
  assert.equal(result.results[1].ok, false);
});

test("artifact transfers route directly to configured storage without cluster-wide discovery", async () => {
  const storage = { name: "storage", storage: {} };
  const explicit = { name: "explicit" };
  const selected = [];
  const ctx = { config: { nodes: [storage, explicit], artifacts: { enabled: true, storageNode: "storage" } }, modules: {
    withToolSession: async (_module, node, _tools, operation) => {
      selected.push(node);
      return operation(async () => ({ ok: true, node: node.name, output: {} }));
    },
  } };
  await withArtifactSession(ctx, undefined, "artifact_fetch", (call) => call({ operation: "info" }));
  await withArtifactSession(ctx, explicit, "artifact_upload", (call) => call({ operation: "begin" }));
  assert.deepEqual(selected, [storage, explicit]);
});

test("live SMB filename search -> artifact montage -> verified share export", {
  skip: process.env.VANTA_SMB_WORKFLOW !== "1", timeout: 600_000,
}, async () => {
  const { loadConfig } = await import("../dist/config.js");
  const { SshPool } = await import("../dist/ssh.js");
  const { ModuleManager } = await import("../dist/modules/manager.js");
  const { registerSourceTools } = await import("../dist/tools/sources.js");
  const liveConfig = loadConfig();
  const pool = new SshPool(liveConfig);
  const modules = new ModuleManager(liveConfig, pool);
  const tools = new Map();
  const controller = registerSourceTools({ registerTool: (name, schema, handler) => {
    tools.set(name, { schema, handler });
    return { remove: () => tools.delete(name) };
  } }, { config: liveConfig, pool, modules });
  const id = process.env.VANTA_SMB_SOURCE ?? "vanta-share";
  const query = process.env.VANTA_SMB_QUERY ?? ".png";
  const invoke = async (name, args) => {
    assert.ok(tools.has(name), `${name} is unavailable; check configured access and stored credentials`);
    const result = await tools.get(name).handler(args, { signal: new AbortController().signal });
    const text = result.content.filter((item) => item.type === "text").map((item) => item.text).join("\n");
    assert.notEqual(result.isError, true, text);
    return JSON.parse(text);
  };
  try {
    const access = await invoke("cluster_check_source", { sourceId: id, writeProbe: true });
    assert.equal(access.ok, true, JSON.stringify(access));
    assert.equal(access.writable, true, JSON.stringify(access));
    await controller.refresh();
    const matches = await invoke("cluster_search_source", { sourceId: id, path: "", query, limit: 200, offset: 0 });
    const inputs = matches.entries.filter((entry) => !entry.directory && /\.(png|jpe?g|webp|gif)$/i.test(entry.path) && !entry.name.startsWith("vanta-combined-")).slice(0, 4);
    assert.ok(inputs.length >= 2, `Need at least two matching images for query ${query}; found ${inputs.length}`);
    const sources = [];
    for (const input of inputs) {
      const imported = await invoke("cluster_import_source", { sourceId: id, path: input.path });
      sources.push({ artifactId: imported.artifact.artifactId });
    }
    const contract = await modules.listTools("image-processing", undefined, "image_compose");
    assert.equal(contract.tools[0].name, "image_compose");
    const composed = await modules.callTool("image-processing", undefined, "image_compose", {
      operation: "montage", sources, columns: 2, cellWidth: 384, cellHeight: 384,
      output: { mode: "artifact", format: "png", name: "combined-images.png", retentionDays: 1 },
    });
    assert.equal(composed.ok, true, JSON.stringify(composed.output));
    const artifact = composed.output.output;
    assert.match(artifact.id, /^[0-9a-f]{32}$/);
    const destination = `vanta-combined-${Date.now()}-${artifact.id.slice(0, 8)}.png`;
    const exported = await invoke("cluster_export_source", { sourceId: id, artifactId: artifact.id, path: destination });
    assert.equal(exported.exported, true);
    assert.equal(exported.sha256, artifact.sha256);
    const roundTrip = await invoke("cluster_import_source", { sourceId: id, path: destination, retentionDays: 1 });
    assert.equal(roundTrip.artifact.sha256, artifact.sha256);
    assert.equal(roundTrip.artifact.bytes, artifact.bytes);
    console.log(JSON.stringify({ sourceId: id, inputs: inputs.map((entry) => entry.path), destination, artifactId: artifact.id,
      bytes: exported.bytes, sha256: exported.sha256, verifiedRoundTrip: true }));
  } finally { pool.disposeAll(); }
});