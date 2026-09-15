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

test("Browser Retrieval advertises an amd64 service package with no persistent data", async () => {
  const manifest = JSON.parse(await (await import("node:fs/promises")).readFile(path.join(moduleDirectory, "module.json"), "utf8"));
  assert.equal(manifest.schemaVersion, 1);
  assert.deepEqual(manifest.compatibility.architectures, ["amd64"]);
  assert.equal(manifest.runtime.mode, "service");
  assert.equal(manifest.persistentData, undefined);
  assert.deepEqual(manifest.deployment, { mode: "replicated", routing: "round-robin" });
  assert.deepEqual(manifest.capabilities.length, 5);
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
