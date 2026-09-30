import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

const moduleDirectory = path.resolve(import.meta.dirname, "..", "modules", "browser-retrieval");
const pythonCommand = ["python3", "python"].find((command) => spawnSync(command, ["--version"], { encoding: "utf8" }).status === 0);
// artifact_protocol.py is a shared file staged beside the module only at install time.
const pythonPath = path.dirname(moduleDirectory);

function runPython(file, args = [], extraEnv = {}) {
  assert.ok(pythonCommand, "Python 3 is required to test browser-retrieval");
  return spawnSync(pythonCommand, [file, ...args], {
    cwd: moduleDirectory,
    encoding: "utf8",
    timeout: 30_000,
    env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1", PYTHONPATH: pythonPath, ...extraEnv },
  });
}

test("Browser Retrieval validates its schemas, extraction limits, and HTTP(S) URL policy", () => {
  for (const file of ["server.py", "browser.py", "network_policy.py", "extraction.py", "download.py"]) {
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
    "  browser._discover_links_expression(10, 3),",
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

test("Browser Retrieval discovers a repeated primary link selector", () => {
  assert.ok(pythonCommand, "Python 3 is required to test browser-retrieval");
  const generated = spawnSync(pythonCommand, ["-B", "-c", [
    "import browser",
    "print(browser._discover_links_expression(10, 2))",
  ].join("\n")], {
    cwd: moduleDirectory,
    encoding: "utf8",
    timeout: 30_000,
    env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" },
  });
  assert.equal(generated.status, 0, generated.stderr || generated.error?.message);

  class Element {
    constructor(tagName, { classes = [], text = "", href = "" } = {}) {
      this.tagName = tagName.toUpperCase();
      this.classList = classes;
      this.innerText = text;
      this.textContent = text;
      this.href = href;
      this.parentElement = null;
      this.children = [];
    }
    append(...children) {
      for (const child of children) {
        child.parentElement = this;
        this.children.push(child);
      }
      return this;
    }
    closest(selector) {
      if (selector === "header,nav,footer,[role=\"navigation\"]") return null;
      if (selector === "h1,h2,h3,h4,h5,h6") {
        for (let node = this; node; node = node.parentElement) {
          if (/^H[1-6]$/.test(node.tagName)) return node;
        }
      }
      return null;
    }
    querySelector(selector) {
      if (selector !== "h1,h2,h3,h4,h5,h6") return null;
      const pending = [...this.children];
      while (pending.length) {
        const node = pending.shift();
        if (/^H[1-6]$/.test(node.tagName)) return node;
        pending.push(...node.children);
      }
      return null;
    }
    getClientRects() { return [{}]; }
  }

  const main = new Element("main");
  const links = ["First result", "Second result", "Third result"].map((text, index) => {
    const link = new Element("a", { text, href: `https://example.com/${index}` });
    main.append(new Element("li", { classes: ["b_algo"] }).append(new Element("h2").append(link)));
    return link;
  });
  const document = {
    querySelectorAll(selector) {
      if (selector === "a[href]" || selector.endsWith("h2 a")) return links;
      return [];
    },
  };
  const result = new Function("document", `return ${generated.stdout}`)(document);
  assert.equal(result.candidates[0].selector, "li.b_algo h2 a");
  assert.equal(result.candidates[0].matchCount, 3);
  assert.deepEqual(result.candidates[0].samples.map((sample) => sample.text), ["First result", "Second result"]);
});

test("Browser Retrieval applies bounded browser controls through CDP", () => {
  assert.ok(pythonCommand, "Python 3 is required to test browser-retrieval");
  const result = spawnSync(pythonCommand, ["-B", "-c", [
    "import json",
    "import browser",
    "class Recorder:",
    "  def __init__(self): self.calls = []",
    "  def command(self, method, params=None, session_id=None, timeout=10.0):",
    "    self.calls.append({'method': method, 'params': params or {}, 'sessionId': session_id})",
    "    return {}",
    "recorder = Recorder()",
    "browser_call = browser.BrowserCall()",
    "browser_call.cdp = recorder",
    "browser_call.session_id = 'session-1'",
    "browser_call._configure_browser({'userAgent':'Example/1.0','language':'de-DE','timezone':'Europe/Berlin','viewport':{'width':390,'height':844,'deviceScaleFactor':3,'mobile':True},'colorScheme':'dark','reducedMotion':'reduce','javascriptEnabled':False})",
    "print(json.dumps(recorder.calls))",
  ].join("\n")], {
    cwd: moduleDirectory,
    encoding: "utf8",
    timeout: 30_000,
    env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" },
  });
  assert.equal(result.status, 0, result.stderr || result.error?.message);
  const calls = JSON.parse(result.stdout);
  assert.deepEqual(calls.map((call) => call.method), [
    "Network.setUserAgentOverride",
    "Network.setExtraHTTPHeaders",
    "Emulation.setLocaleOverride",
    "Emulation.setTimezoneOverride",
    "Emulation.setDeviceMetricsOverride",
    "Emulation.setTouchEmulationEnabled",
    "Emulation.setEmulatedMedia",
    "Emulation.setScriptExecutionDisabled",
  ]);
  assert.ok(calls.every((call) => call.sessionId === "session-1"));
  assert.deepEqual(calls[1].params, { headers: { "Accept-Language": "de-DE" } });
  assert.deepEqual(calls[4].params, { width: 390, height: 844, deviceScaleFactor: 3, mobile: true });
  assert.deepEqual(calls[6].params.features, [
    { name: "prefers-color-scheme", value: "dark" },
    { name: "prefers-reduced-motion", value: "reduce" },
  ]);
  assert.deepEqual(calls[7].params, { value: true });
});

test("Browser Retrieval paginates one table and reports semantic truncation", () => {
  assert.ok(pythonCommand, "Python 3 is required to test browser-retrieval");
  const generated = spawnSync(pythonCommand, ["-B", "-c", [
    "import browser",
    "from extraction import validate_tables",
    "print(browser._tables_expression(validate_tables({'url':'https://example.com','tableIndex':1,'rowOffset':1,'rowLimit':2,'maxCellCharacters':10})))",
  ].join("\n")], {
    cwd: moduleDirectory,
    encoding: "utf8",
    timeout: 30_000,
    env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" },
  });
  assert.equal(generated.status, 0, generated.stderr || generated.error?.message);

  const makeCell = (text, tagName = "TD") => ({ innerText: text, textContent: text, tagName, rowSpan: 1, colSpan: 1 });
  const makeRow = (...cells) => ({ cells });
  const ignored = { rows: [makeRow(makeCell("ignored"))] };
  const selected = {
    caption: { innerText: "Selected" },
    rows: [
      makeRow(makeCell("Name", "TH")),
      makeRow(makeCell("zero")),
      makeRow(makeCell("one")),
      makeRow(makeCell("two is clipped")),
      makeRow(makeCell("three")),
    ],
  };
  const document = { querySelectorAll: () => [ignored, selected] };
  const result = new Function("document", `return ${generated.stdout}`)(document);

  assert.equal(result.sourceTableCount, 2);
  assert.equal(result.returnedTableCount, 1);
  assert.equal(result.complete, false);
  assert.deepEqual(result.truncationReasons, ["row-pagination", "cell-character-limit"]);
  assert.equal(result.truncatedCellCount, 1);
  assert.equal(result.tables[0].index, 1);
  assert.deepEqual(result.tables[0].headers, ["Name"]);
  assert.deepEqual(result.tables[0].rows, [["one"], ["two is cli"]]);
  assert.equal(result.tables[0].sourceRowCount, 4);
  assert.equal(result.tables[0].returnedRowCount, 2);
  assert.equal(result.tables[0].nextRowOffset, 3);
});

test("Browser Retrieval merges a multi-row table header instead of returning it as data", () => {
  assert.ok(pythonCommand, "Python 3 is required to test browser-retrieval");
  const generated = spawnSync(pythonCommand, ["-B", "-c", [
    "import browser",
    "from extraction import validate_tables",
    "print(browser._tables_expression(validate_tables({'url':'https://example.com','tableIndex':0})))",
  ].join("\n")], {
    cwd: moduleDirectory,
    encoding: "utf8",
    timeout: 30_000,
    env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" },
  });
  assert.equal(generated.status, 0, generated.stderr || generated.error?.message);

  const makeCell = (text, tagName = "TD", rowSpan = 1, colSpan = 1) => ({ innerText: text, textContent: text, tagName, rowSpan, colSpan });
  const makeRow = (...cells) => ({ cells });
  const table = {
    caption: { innerText: "Releases" },
    rows: [
      makeRow(makeCell("Browser", "TH", 2, 1), makeCell("Latest release", "TH", 1, 2)),
      makeRow(makeCell("Version", "TH"), makeCell("Date", "TH")),
      makeRow(makeCell("Amaya"), makeCell("11.4.4"), makeCell("2012-01-18")),
      makeRow(makeCell("Lynx"), makeCell("2.9.0"), makeCell("2024-01-01")),
    ],
  };
  const document = { querySelectorAll: () => [table] };
  const result = new Function("document", `return ${generated.stdout}`)(document);

  assert.equal(result.tables[0].headerRowCount, 2);
  assert.deepEqual(result.tables[0].headers, ["Browser", "Latest release Version", "Latest release Date"]);
  assert.deepEqual(result.tables[0].rows, [
    ["Amaya", "11.4.4", "2012-01-18"],
    ["Lynx", "2.9.0", "2024-01-01"],
  ]);
  assert.equal(result.tables[0].sourceRowCount, 2);
  assert.equal(result.tables[0].nextRowOffset, null);
  assert.equal(result.tables[0].complete, true);
});

test("Browser Retrieval launches Chromium without forking Python from the threaded broker", async () => {
  const browser = await (await import("node:fs/promises")).readFile(path.join(moduleDirectory, "browser.py"), "utf8");
  assert.doesNotMatch(browser, /preexec_fn/);
  assert.match(browser, /exec "\$0" "\$@"/);
  assert.match(browser, /--remote-debugging-pipe/);
});

test("Browser Retrieval advertises an amd64 service package with artifact publishing and no persistent data", async () => {
  const manifest = JSON.parse(await (await import("node:fs/promises")).readFile(path.join(moduleDirectory, "module.json"), "utf8"));
  assert.equal(manifest.schemaVersion, 2);
  assert.deepEqual(manifest.compatibility.architectures, ["amd64"]);
  assert.equal(manifest.runtime.mode, "service");
  assert.equal(manifest.persistentData, undefined);
  assert.deepEqual(manifest.deployment, { mode: "replicated", routing: "round-robin" });
  assert.deepEqual(manifest.capabilities.length, 6);
  assert.deepEqual(manifest.artifactAccess, { write: true });
  assert.deepEqual(manifest.sharedFiles, ["artifact_protocol.py"]);
  assert.equal(manifest.installOptions.downloadContact.type, "string");
  assert.ok(new RegExp(manifest.installOptions.downloadContact.pattern).test("https://example.org/contact"));
  assert.ok(!new RegExp(manifest.installOptions.downloadContact.pattern).test("Mozilla/5.0 (Windows NT 10.0)"));
  assert.deepEqual(manifest.background.tools, { web_download: "optional" });
  assert.ok(manifest.background.maxTimeoutMs > 660_000);
});

test("Browser Retrieval routes background downloads and returns structured download errors", () => {
  const script = [
    "import json",
    "import download, server",
    "calls = []",
    "def fake(action, arguments, timeout=55.0, execution='immediate'):",
    "    calls.append((action, timeout, execution))",
    "    if arguments.get('fail'): raise server.BrokerError('HTTP 429 Too Many Requests', {'status': 429, 'retryAfterSeconds': 30})",
    "    return {'artifactId': 'a' * 32}",
    "server.broker_call = fake",
    "def call(name, arguments, meta=None):",
    "    params = {'name': name, 'arguments': arguments, **({'_meta': meta} if meta else {})}",
    "    return server.handle_request({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': params})['result']",
    "background = {'vantamcpd/execution': 'background'}",
    "assert call('web_download', {'url': 'https://example.com/a.pdf'}, background).get('isError') is None",
    "assert calls[-1] == ('web_download', server.BACKGROUND_SOCKET_TIMEOUT, 'background'), calls",
    "call('web_download', {'url': 'https://example.com/a.pdf'})",
    "assert calls[-1] == ('web_download', 55.0, 'immediate'), calls",
    "failed = call('web_download', {'fail': True})",
    "assert failed['isError'] is True and json.loads(failed['content'][0]['text']) == {'error': 'HTTP 429 Too Many Requests', 'status': 429, 'retryAfterSeconds': 30}, failed",
    "rejected = call('web_retrieve', {'url': 'https://example.com'}, background)",
    "assert rejected['isError'] is True and 'background' in rejected['content'][0]['text'] and len(calls) == 3, rejected",
    "assert server.MAX_BACKGROUND_DOWNLOAD_MS == download.MAX_BACKGROUND_TIMEOUT_MS",
  ].join("\n");
  const result = spawnSync(pythonCommand, ["-B", "-c", script], {
    cwd: moduleDirectory,
    encoding: "utf8",
    timeout: 30_000,
    env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1", PYTHONPATH: pythonPath },
  });
  assert.equal(result.status, 0, result.stderr || result.error?.message);
});

test("Browser Retrieval downloads files into artifact storage with bounded, re-validated HTTP", () => {
  const storeRoot = mkdtempSync(path.join(tmpdir(), "vanta-browser-download-"));
  try {
    const script = [
      "import http.server, json, os, threading",
      "import download",
      "root = os.environ['TEST_STORE_ROOT']",
      "for name in ('objects', '.uploads', '.reservations'): os.makedirs(os.path.join(root, name), exist_ok=True)",
      "open(os.path.join(root, '.store.lock'), 'a').close()",
      "policy = {'totalQuotaBytes': 10_000_000, 'producerQuotaBytes': 10_000_000, 'maxArtifactBytes': 1_000_000, 'defaultRetentionDays': 7, 'maxRetentionDays': 90, 'freeReserveBytes': 0}",
      "with open(os.path.join(root, '.store.json'), 'w', encoding='utf-8') as handle: json.dump({'protocolVersion': 1, 'policy': policy}, handle)",
      "PNG = b'\\x89PNG\\r\\n\\x1a\\n' + b'\\x00' * 100",
      "PDF = b'%PDF-1.4\\n%%EOF\\n'",
      "LARGE = b'\\x89PNG\\r\\n\\x1a\\n' + bytes(range(256)) * 4",
      "seen = {'agents': [], 'limited': 0, 'flaky': 0, 'ranges': [], 'patient': 0, 'reuse': 0}",
      "class Handler(http.server.BaseHTTPRequestHandler):",
      "    def log_message(self, *args): pass",
      "    def reply(self, status, body=b'', content_type='application/octet-stream', headers=()):",
      "        self.send_response(status)",
      "        for key, value in headers: self.send_header(key, value)",
      "        self.send_header('Content-Type', content_type)",
      "        self.send_header('Content-Length', str(len(body)))",
      "        self.end_headers()",
      "        self.wfile.write(body)",
      "    def do_GET(self):",
      "        seen['agents'].append(self.headers.get('User-Agent'))",
      "        if self.path == '/redirect': return self.reply(302, headers=[('Location', '/files/Letter%2C_1975.png')])",
      "        if self.path == '/to-file': return self.reply(302, headers=[('Location', 'file:///etc/passwd')])",
      "        if self.path == '/files/reuse.png': seen['reuse'] += 1",
      "        if self.path.startswith('/files/'): return self.reply(200, PNG, 'image/png')",
      "        if self.path == '/page': return self.reply(200, b'<!doctype html><title>File page</title>', 'text/html; charset=utf-8')",
      "        if self.path == '/big': return self.reply(200, b'x' * 2000)",
      "        if self.path == '/denied': return self.reply(403, b'<html><body><h1>Forbidden</h1><p>Please respect our robot policy &amp; retry</p><script>track()</script></body></html>', 'text/html')",
      "        if self.path == '/busy': return self.reply(429, headers=[('Retry-After', '120')])",
      "        if self.path == '/limited':",
      "            seen['limited'] += 1",
      "            if seen['limited'] == 1: return self.reply(429, headers=[('Retry-After', '1')])",
      "            return self.reply(200, PDF, 'application/octet-stream')",
      "        if self.path == '/flaky':",
      "            seen['flaky'] += 1",
      "            if seen['flaky'] == 1: return self.reply(502)",
      "            return self.reply(200, PDF, 'application/pdf')",
      "        if self.path == '/patient':",
      "            seen['patient'] += 1",
      "            if seen['patient'] == 1: return self.reply(503, headers=[('Retry-After', '2')])",
      "            return self.reply(200, PNG, 'image/png')",
      "        if self.path == '/drop':",
      "            seen['ranges'].append((self.headers.get('Range'), self.headers.get('If-Range')))",
      "            if self.headers.get('Range') == 'bytes=100-' and self.headers.get('If-Range') == '\"v1\"':",
      "                return self.reply(206, LARGE[100:], 'image/png', [('Content-Range', f'bytes 100-{len(LARGE) - 1}/{len(LARGE)}'), ('ETag', '\"v1\"')])",
      "            self.send_response(200)",
      "            for key, value in (('Content-Type', 'image/png'), ('Content-Length', str(len(LARGE))), ('Accept-Ranges', 'bytes'), ('ETag', '\"v1\"')): self.send_header(key, value)",
      "            self.end_headers()",
      "            self.wfile.write(LARGE[:100])",
      "            self.wfile.flush()",
      "            self.close_connection = True",
      "            return",
      "        return self.reply(404, b'Not here', 'text/plain')",
      "server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)",
      "threading.Thread(target=server.serve_forever, daemon=True).start()",
      "base = f'http://127.0.0.1:{server.server_address[1]}'",
      "result = download.download({'url': base + '/redirect', 'expectedTypes': ['png'], 'retentionDays': 3}, root)",
      "artifact = result['artifact']",
      "assert result['redirects'] == 1 and result['finalUrl'].endswith('/files/Letter%2C_1975.png'), result",
      "assert result['detectedType'] == 'png' and result['bytes'] == len(PNG) and result['trust'] == 'untrusted-web-content'",
      "assert artifact['name'] == 'Letter_1975.png' and artifact['mimeType'] == 'image/png' and artifact['producer'] == 'browser-retrieval', artifact",
      "assert round((artifact['expiresEpoch'] - artifact['createdEpoch']) / 86400) == 3",
      "with open(os.path.join(root, 'objects', artifact['id'][:2], artifact['id'], 'content'), 'rb') as handle: assert handle.read() == PNG",
      "retried = download.download({'url': base + '/limited', 'name': 'report.pdf'}, root)",
      "assert retried['retriedAfterSeconds'] == 1 and retried['detectedType'] == 'pdf' and retried['attempts'] == 2, retried",
      "assert retried['retries'] == [{'reason': 'HTTP 429', 'waitSeconds': 1.0}], retried['retries']",
      "assert retried['artifact']['name'] == 'report.pdf' and retried['artifact']['mimeType'] == 'application/pdf'",
      "flaky = download.download({'url': base + '/flaky'}, root)",
      "assert flaky['attempts'] == 2 and flaky['retries'][0]['reason'] == 'HTTP 502' and flaky['detectedType'] == 'pdf', flaky",
      "resumed = download.download({'url': base + '/drop', 'expectedTypes': ['png']}, root)",
      "assert resumed['resumed'] == 1 and resumed['bytes'] == len(LARGE), resumed",
      "assert seen['ranges'] == [(None, None), ('bytes=100-', '\"v1\"')], seen['ranges']",
      "with open(os.path.join(root, 'objects', resumed['artifactId'][:2], resumed['artifactId'], 'content'), 'rb') as handle: assert handle.read() == LARGE",
      "first = download.download({'url': base + '/files/reuse.png', 'retentionDays': 2}, root)",
      "again = download.download({'url': base + '/files/reuse.png', 'retentionDays': 2, 'expectedTypes': ['png']}, root)",
      "assert again['reused'] is True and again['artifactId'] == first['artifactId'] and seen['reuse'] == 1, again",
      "for fresh in ({'reuse': False}, {'retentionDays': 5}, {'name': 'other.png'}, {'expectedTypes': ['pdf']}):",
      "    try: result = download.download({'url': base + '/files/reuse.png', 'retentionDays': 2, **fresh}, root)",
      "    except ValueError as error: assert 'not one of: pdf' in str(error), error",
      "    else: assert result['reused'] is False and result['artifactId'] != first['artifactId'], (fresh, result)",
      "assert seen['reuse'] == 5, seen['reuse']",
      "download.MAX_RETRY_AFTER_SECONDS = 1",
      "try: download.download({'url': base + '/patient'}, root)",
      "except download.DownloadError as error: assert error.details['status'] == 503 and error.details['retryAfterSeconds'] == 2, error.details",
      "else: raise AssertionError('an immediate call waited longer than its Retry-After cap')",
      "patient = download.download({'url': base + '/patient'}, root, background=True)",
      "assert patient['attempts'] == 1 and seen['patient'] == 2, patient",
      "download.MAX_RETRY_AFTER_SECONDS = 10",
      "page = download.download({'url': base + '/page'}, root)",
      "assert page['detectedType'] is None and page['artifact']['mimeType'] == 'text/html' and len(page['warnings']) == 2, page",
      "try: download.download({'url': base + '/denied'}, root)",
      "except download.DownloadError as error: assert error.details['status'] == 403 and error.details['bodyExcerpt'] == 'Forbidden Please respect our robot policy & retry', error.details",
      "else: raise AssertionError('a 403 was accepted')",
      "for arguments, expected in (",
      "    ({'url': base + '/page', 'expectedTypes': ['pdf']}, 'downloaded content is text/html'),",
      "    ({'url': base + '/big', 'maxBytes': 100}, 'exceeds the 100-byte limit'),",
      "    ({'url': base + '/missing'}, 'HTTP 404'),",
      "    ({'url': base + '/to-file'}, 'only http and https'),",
      "    ({'url': base + '/busy'}, 'Retry-After: 120'),",
      "):",
      "    try: download.download(arguments, root)",
      "    except ValueError as error: assert expected in str(error), (expected, str(error))",
      "    else: raise AssertionError(f'download was accepted: {arguments}')",

      "requests_before = len(seen['agents'])",
      "try: download.download({'url': base + '/files/cooling.png'}, root)",
      "except download.DownloadError as error: assert error.details['cooldown'] is True and error.details['status'] == 429 and 'slow down' in str(error), error.details",
      "else: raise AssertionError('a host in cooldown was contacted')",
      "assert len(seen['agents']) == requests_before",
      "download.HOST_COOLDOWNS.clear()",
      "try: download.download({'url': base + '/files/a.png'}, os.path.join(root, 'missing'))",
      "except ValueError as error: assert 'unavailable' in str(error)",
      "else: raise AssertionError('a missing store was accepted')",
      "assert set(seen['agents']) == {'VantaMCPd-browser-retrieval/0.7.0 (+ops@example.org)'}, seen['agents']",
      "server.shutdown()",
    ].join("\n");
    const result = spawnSync(pythonCommand, ["-B", "-c", script], {
      cwd: moduleDirectory,
      encoding: "utf8",
      timeout: 60_000,
      env: {
        ...process.env,
        PYTHONDONTWRITEBYTECODE: "1",
        PYTHONPATH: pythonPath,
        TEST_STORE_ROOT: storeRoot,
        VANTA_BROWSER_DOWNLOAD_CONTACT: "ops@example.org",
      },
    });
    assert.equal(result.status, 0, result.stderr || result.error?.message);
  } finally {
    rmSync(storeRoot, { recursive: true, force: true });
  }
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
  assert.match(install, /download\.py/);
  assert.match(install, /artifact_protocol\.py/);
  assert.match(install, /ReadWritePaths=-%s/);
  assert.match(install, /VANTA_MODULE_OPTION_DOWNLOAD_CONTACT/);
  const uninstall = await (await import("node:fs/promises")).readFile(path.join(moduleDirectory, "uninstall.sh"), "utf8");
  assert.match(uninstall, /vantamcpd-browser-retrieval\.service\.d/);
});

test("Browser Retrieval leaves Chromium networking unrestricted", async () => {
  const browser = await (await import("node:fs/promises")).readFile(path.join(moduleDirectory, "browser.py"), "utf8");
  assert.doesNotMatch(browser, /Fetch\.enable|Network\.setBlockedURLs|Browser\.setDownloadBehavior|dns-over-https|disable-background-networking/);
  const policy = await (await import("node:fs/promises")).readFile(path.join(moduleDirectory, "network_policy.py"), "utf8");
  assert.match(policy, /http:\/\/127\.0\.0\.1:8080/);
});
