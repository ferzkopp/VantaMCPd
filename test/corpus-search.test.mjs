import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import test from "node:test";

const root = path.resolve(import.meta.dirname, "..");
const moduleDirectory = path.join(root, "modules", "corpus-search");
const fixture = path.join(root, "test", "fixtures", "arxiv-page.xml");
const pythonCommand = ["python3", "python"].find((command) => spawnSync(command, ["--version"], { encoding: "utf8" }).status === 0);

function provisionFixture(dataDirectory) {
  const profilePath = path.join(dataDirectory, "profile.json");
  writeFileSync(profilePath, JSON.stringify({
    schemaVersion: 1,
    id: "fixture",
    source: "arxiv-api",
    pageSize: 2,
    maxRecords: 2,
    slices: [{ id: "fixture", maxRecords: 2, categories: ["cs.IR", "cs.DB"] }],
  }));
  const script = [
    "from pathlib import Path",
    "from provision import provision",
    "class Client:",
    "    def __init__(self): self.calls = 0",
    "    def fetch(self, query, start, page_size):",
    "        self.calls += 1",
    `        return Path(${JSON.stringify(fixture)}).read_bytes() if self.calls == 1 else b'<?xml version=\"1.0\"?><feed xmlns=\"http://www.w3.org/2005/Atom\" />'`,
    `result = provision(Path(${JSON.stringify(dataDirectory)}), Path(${JSON.stringify(profilePath)}), Client(), '202609131200')`,
    "assert result == {'records': 2, 'reused': False}",
  ].join("\n");
  const result = spawnSync(pythonCommand, ["-c", script], {
    cwd: moduleDirectory,
    encoding: "utf8",
    timeout: 30_000,
    env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" },
  });
  assert.equal(result.status, 0, result.stderr || result.error?.message);
}

function runProtocol(dataDirectory, calls) {
  const requests = [
    { jsonrpc: "2.0", id: 1, method: "initialize", params: { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "test", version: "1" } } },
    { jsonrpc: "2.0", id: 2, method: "tools/list", params: {} },
    ...calls.map((call, index) => ({ jsonrpc: "2.0", id: index + 3, method: "tools/call", params: call })),
  ];
  const result = spawnSync(pythonCommand, ["server.py"], {
    cwd: moduleDirectory,
    input: `${requests.map(JSON.stringify).join("\n")}\n`,
    encoding: "utf8",
    timeout: 30_000,
    env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1", VANTA_MODULE_DATA_DIR: dataDirectory },
  });
  assert.equal(result.status, 0, result.stderr || result.error?.message);
  return result.stdout.trim().split(/\r?\n/).map(JSON.parse);
}

test("corpus profile remains capped and expandable", () => {
  const profile = JSON.parse(readFileSync(path.join(moduleDirectory, "profiles", "compact-arxiv.json"), "utf8"));
  assert.equal(profile.maxRecords, 10_000);
  assert.deepEqual(profile.slices.map((slice) => slice.maxRecords), [5_000, 5_000]);
  assert.ok(profile.slices[0].categories.includes("cs.LG"));
  assert.ok(profile.slices[1].categories.includes("cs.DB"));
});

test("provisions fixture metadata and serves bounded MCP search tools", { skip: !pythonCommand }, () => {
  const dataDirectory = mkdtempSync(path.join(tmpdir(), "vanta-corpus-"));
  try {
    provisionFixture(dataDirectory);
    const responses = runProtocol(dataDirectory, [
      { name: "corpus_search", arguments: { query: "document retrieval", category: "cs.IR", limit: 5 } },
      { name: "corpus_get", arguments: { id: "2609.00001v2" } },
      { name: "corpus_info", arguments: {} },
      { name: "corpus_search", arguments: { query: "x", limit: 500 } },
    ]);
    assert.deepEqual(responses[1].result.tools.map((tool) => tool.name), ["corpus_search", "corpus_get", "corpus_info"]);
    assert.equal(responses[2].result.structuredContent.results[0].id, "2609.00001");
    assert.equal(responses[3].result.structuredContent.authors[0], "Ada Example");
    assert.equal(responses[4].result.structuredContent.records, 2);
    assert.equal(responses[5].result.isError, true);
  } finally {
    rmSync(dataDirectory, { recursive: true, force: true });
  }
});