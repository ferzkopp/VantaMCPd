import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";

const directory = path.resolve(import.meta.dirname, "..", "modules", "document-ocr");
const shared = path.resolve(directory, "..");
const python = ["python3", "python"].find((command) => spawnSync(command, ["--version"], { encoding: "utf8" }).status === 0);

function run(script) {
  assert.ok(python, "Python 3 is required for document-ocr tests");
  const result = spawnSync(python, ["-B", "-c", script], {
    cwd: directory,
    encoding: "utf8",
    timeout: 30_000,
    env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1", PYTHONPATH: `${directory}${path.delimiter}${shared}` },
  });
  assert.equal(result.status, 0, result.stderr || result.error?.message);
  return result.stdout;
}

test("document-ocr advertises two bounded, closed-world tools", () => {
  const output = run(`import json, server
request = {'id': 1, 'method': 'tools/list'}
print(json.dumps(server.handle_request(request)['result']))`);
  const tools = JSON.parse(output).tools;
  assert.deepEqual(tools.map((tool) => tool.name), ["ocr_environment", "ocr_extract"]);
  assert.ok(tools.every((tool) => tool.inputSchema.additionalProperties === false && tool.annotations.openWorldHint === false));
  assert.equal(tools[1].inputSchema.properties.pages.maxItems, 20);
  assert.deepEqual(tools[1].inputSchema.properties.language.enum, ["auto", "en"]);
  assert.equal(tools[1].inputSchema.properties.artifactId.pattern, "^[0-9a-f]{32}$");
  assert.equal(JSON.parse(readFileSync(path.join(directory, "module.json"), "utf8")).runtime.mode, "on-demand");
});

test("document-ocr rejects invalid inputs and recognizes background calls", () => {
  run(`import server
for args in ({'artifactId': '../secret'}, {'artifactId': 'a' * 32, 'pages': [True]},
             {'artifactId': 'a' * 32, 'pages': [1, 1]}, {'artifactId': 'a' * 32, 'url': 'https://example.com'}):
    try: server.validate('ocr_extract', args)
    except ValueError: pass
    else: raise AssertionError('invalid input was accepted')
server.extract = lambda args, background: {'background': background}
response = server.handle_request({'id': 1, 'method': 'tools/call', 'params': {'name': 'ocr_extract',
    'arguments': {'artifactId': 'a' * 32}, '_meta': {'vantamcpd/execution': 'background'}}})
assert response['result']['structuredContent']['background'] is True
assert server.handle_request({'id': 1, 'method': 'tools/call', 'params': {'name': 'missing'}})['result']['isError']`);
});

test("document-ocr stages only verified artifacts and publishes bounded results", () => {
  run(`import json, os, tempfile, types, server
with tempfile.TemporaryDirectory() as state:
    server.STATE = state
    server.MODEL_DIR = os.path.join(state, 'models')
    server.ARTIFACT_ROOT = state
    server.fcntl = types.SimpleNamespace(LOCK_EX=1, LOCK_NB=4, flock=lambda *args: None)
    server.available = lambda root: True
    def stage(root, artifact_id, target, limit):
        assert limit == server.MAX_INPUT
        with open(target, 'wb') as output: output.write(b'\\x89PNG\\r\\n\\x1a\\n')
    server.copy_verified = stage
    reservations = []
    server.reserve = lambda root, producer, size: reservations.append((producer, size)) or 'reservation'
    server.release = lambda *args: reservations.append('released')
    server.publish_reserved = lambda root, rid, producer, items, days: [{'id': 'b' * 32}]
    def container(argv, **kwargs):
        assert '--network=none' in argv and 'nvidia.com/gpu=all' in argv
        assert '--cgroup-manager=cgroupfs' in argv
        work = next(item.rsplit(':', 2)[0] for item in argv if item.endswith(':/work:rw'))
        with open(os.path.join(work, 'result.json'), 'w') as output:
            json.dump({'pages': [{'page': 1, 'text': 'hello'}], 'pageCount': 1, 'model': 'test'}, output)
        return types.SimpleNamespace(returncode=0, stderr=b'')
    server.subprocess.run = container
    value = server.extract({'artifactId': 'a' * 32, 'pages': [], 'language': 'en',
                'outputMode': 'artifact', 'retentionDays': 7}, True)
    assert value['pageCount'] == 1 and value['artifact']['id'] == 'b' * 32
    assert reservations == [('document-ocr', server.MAX_RESULT)]
    assert not os.listdir(os.path.join(state, 'calls'))`);
});

test("document-ocr releases artifact quota and workspace when a worker times out", () => {
  run(`import os, subprocess, tempfile, types, server
with tempfile.TemporaryDirectory() as state:
  server.STATE = state
  server.ARTIFACT_ROOT = state
  server.fcntl = types.SimpleNamespace(LOCK_EX=1, LOCK_NB=4, flock=lambda *args: None)
  server.available = lambda root: True
  def stage(root, artifact_id, target, limit):
    with open(target, 'wb') as output: output.write(b'\\x89PNG\\r\\n\\x1a\\n')
  server.copy_verified = stage
  released = []
  server.reserve = lambda *args: 'reservation'
  server.release = lambda root, rid: released.append(rid)
  def timeout(argv, **kwargs):
    raise subprocess.TimeoutExpired(argv, 1)
  server.subprocess.run = timeout
  try: server.extract({'artifactId': 'a' * 32, 'pages': [], 'language': 'auto',
             'outputMode': 'artifact', 'retentionDays': 7})
  except ValueError as error: assert str(error) == 'OCR worker exceeded the 125-second limit; retry with execution: background or select fewer pages', error
  else: raise AssertionError('worker timeout was accepted')
  assert released == ['reservation']
  assert not os.listdir(os.path.join(state, 'calls'))`);
});

test("document-ocr reports the worker's own error instead of raw Paddle logs", () => {
  run(`import json, os, tempfile, server
log = ('\\x1b[33mConnectivity check to the model hoster has been skipped\\x1b[0m\\n'
       '\\x1b[32mCreating model: (\\'PP-OCRv5_mobile_det\\', None)\\x1b[0m\\n'
       'Traceback (most recent call last):\\n  File "/app/worker.py", line 24, in page_images\\n'
       '    raise ValueError("requested page is outside the PDF")\\n'
       'ValueError: requested page is outside the PDF\\n').encode()
with tempfile.TemporaryDirectory() as work:
    fallback = server.worker_failure(work, 1, log)
    assert fallback == 'OCR worker failed (exit 1): ValueError: requested page is outside the PDF', fallback
    assert server.worker_failure(work, 1, b'') == 'OCR worker failed (exit 1): no diagnostic output'
    assert 'memory limit' in server.worker_failure(work, 137, log)
    with open(os.path.join(work, 'error.json'), 'w') as handle:
        json.dump({'error': 'requested page is outside the PDF\\x1b[0m\\n' + 'x' * 900}, handle)
    reported = server.worker_failure(work, 1, log)
    assert reported.startswith('OCR failed: requested page is outside the PDF x'), reported
    assert len(reported) <= len('OCR failed: ') + server.MAX_ERROR and '\\x1b' not in reported`);
});

test("document-ocr worker records a concise error before exiting", () => {
  run(`import builtins, io, json, worker
files = {'/work/request.json': io.StringIO(json.dumps({'pages': [4], 'language': 'en'}))}
class Capture(io.StringIO):
    def close(self): files['saved'] = self.getvalue()
real_open = builtins.open
builtins.open = lambda path, mode='r', **kw: files[path] if mode == 'r' else Capture()
def fail(*args): raise ValueError('requested page is outside the PDF')
worker.extract = fail
try: worker.main()
except ValueError: pass
else: raise AssertionError('worker swallowed the failure')
finally: builtins.open = real_open
assert json.loads(files['saved']) == {'error': 'requested page is outside the PDF'}`);
});

test("document-ocr worker exposes a dependency-free self-test", () => {
  assert.ok(python);
  const result = spawnSync(python, ["-B", "worker.py", "--self-test"], {
    cwd: directory, encoding: "utf8", env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" },
  });
  assert.equal(result.status, 0, result.stderr);
});

test("document-ocr warmup uses isolated networking instead of pasta", () => {
  const installer = readFileSync(path.join(directory, "install.sh"), "utf8");
  assert.match(installer, /podman --cgroup-manager=cgroupfs run --rm --network=slirp4netns --device nvidia\.com\/gpu=all/);
  assert.ok(installer.includes('XDG_RUNTIME_DIR="/run/user/$uid"'));
  assert.ok(readFileSync(path.join(directory, "uninstall.sh"), "utf8").includes('XDG_RUNTIME_DIR="/run/user/$uid"'));
  assert.ok(readFileSync(path.join(directory, "server.py"), "utf8").includes('f"/run/user/{os.getuid()}"'));
  assert.ok(JSON.parse(readFileSync(path.join(directory, "module.json"), "utf8")).compatibility.requiredCommands.includes("slirp4netns"));
});

test("document-ocr persists models where PaddleX reads them and avoids ID-mapped image copies", () => {
  const installer = readFileSync(path.join(directory, "install.sh"), "utf8");
  const server = readFileSync(path.join(directory, "server.py"), "utf8");
  for (const source of [installer, server]) {
    assert.ok(source.includes("PADDLE_PDX_CACHE_HOME=/models"));
    assert.ok(!source.includes("PADDLEX_HOME"));
  }
  assert.match(installer, /--network=none --tmpfs[^\n]*\\\n[^\n]*\\\n\s+-v "\$state\/models:\/models:ro" "\$image" --warmup/);
  assert.ok(!server.includes('"--userns=keep-id"'));
});

test("document-ocr worker includes every selected page", () => {
  run(`import sys, types, worker
sys.modules['numpy'] = types.SimpleNamespace(asarray=lambda image: image)
pages = [(number, types.SimpleNamespace(width=100, height=100, close=lambda: None)) for number in (1, 2)]
worker.page_images = lambda source, requested: iter(pages)
result = types.SimpleNamespace(json={'res': {'rec_texts': ['text'], 'rec_scores': [0.9],
                                       'rec_polys': [[[0, 0], [1, 0], [1, 1], [0, 1]]]}})
worker.create_engine = lambda language: types.SimpleNamespace(predict=lambda image: [result])
output = worker.extract('document.pdf', [1, 2])
assert output['pageCount'] == 2
assert [page['page'] for page in output['pages']] == [1, 2]`);
});