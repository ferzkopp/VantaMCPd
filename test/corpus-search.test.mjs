import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import test from "node:test";

const root = path.resolve(import.meta.dirname, "..");
const moduleDirectory = path.join(root, "modules", "corpus-search");
const pythonCommand = ["python3", "python"].find((command) => spawnSync(command, ["--version"], { encoding: "utf8" }).status === 0);

function provisionFixture(dataDirectory) {
  const profilePath = path.join(dataDirectory, "profile.json");
  writeFileSync(profilePath, JSON.stringify({
    schemaVersion: 2,
    id: "fixture",
    source: "arxiv-bulk-snapshot",
    samplePercent: 100,
    sampleSeed: "fixture-v1",
    topics: ["cs.IR", "cs.DB"],
  }));
  const records = [
    { id: "2609.00001", authors: "Ada Example", authors_parsed: [["Example", "Ada", ""]], title: "Document Retrieval", abstract: "A document retrieval example.", categories: "cs.IR cs.AI", doi: "10.1/example", versions: [{ version: "v1", created: "Thu, 10 Sep 2026 00:00:00 GMT" }], update_date: "2026-09-10" },
    { id: "2609.00002", authors: "Grace Sample", authors_parsed: [["Sample", "Grace", ""]], title: "Database Search", abstract: "A database metadata example.", categories: "cs.DB", versions: [{ version: "v1", created: "Fri, 11 Sep 2026 00:00:00 GMT" }], update_date: "2026-09-11" },
    { id: "2609.00003", authors: "Out Of Scope", title: "Quantum Example", abstract: "Not selected.", categories: "quant-ph", versions: [{ version: "v1", created: "Sat, 12 Sep 2026 00:00:00 GMT" }], update_date: "2026-09-12" },
  ];
  const script = [
    "import json, zipfile",
    "from pathlib import Path",
    "from provision import provision",
    `archive = Path(${JSON.stringify(path.join(dataDirectory, "snapshot.zip"))})`,
    `records = json.loads(${JSON.stringify(JSON.stringify(records))})`,
    "with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as bundle:",
    "    bundle.writestr('arxiv-metadata-oai-snapshot.json', ''.join(json.dumps(item) + '\\n' for item in records))",
    "class Client:",
    "    def fetch(self, category, from_date, until_date, token=None):",
    "        return b'<OAI-PMH xmlns=\"http://www.openarchives.org/OAI/2.0/\"><ListRecords /></OAI-PMH>'",
    `result = provision(Path(${JSON.stringify(dataDirectory)}), Path(${JSON.stringify(profilePath)}), Client(), '202609131200', snapshot_path=archive)`,
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

test("corpus profiles select nested 1%, 25%, and 100% samples", () => {
  const profiles = ["small-arxiv.json", "medium-arxiv.json", "large-arxiv.json"]
    .map((name) => JSON.parse(readFileSync(path.join(moduleDirectory, "profiles", name), "utf8")));
  assert.deepEqual(profiles.map((profile) => profile.id), ["small-arxiv-cs", "medium-arxiv-cs", "large-arxiv-cs"]);
  assert.deepEqual(profiles.map((profile) => profile.samplePercent), [1, 25, 100]);
  assert.ok(profiles.every((profile) => profile.sampleSeed === profiles[0].sampleSeed));
  assert.ok(profiles.every((profile) => JSON.stringify(profile.topics) === JSON.stringify(profiles[0].topics)));
  assert.ok(profiles[0].topics.includes("cs.LG"));
  assert.ok(profiles[0].topics.includes("cs.DB"));
});

test("materializes profile sampling and custom topic content", { skip: !pythonCommand }, () => {
  const profile = path.join(moduleDirectory, "profiles", "small-arxiv.json");
  const script = [
    "from pathlib import Path",
    "from provision import configure_profile, load_profile, oai_set_spec, resolve_profile, sampled",
    `assert resolve_profile(Path(${JSON.stringify(path.join(moduleDirectory, "profiles"))}), 'small-arxiv-cs').name == 'small-arxiv.json'`,
    `profile, base_hash = load_profile(Path(${JSON.stringify(profile)}))`,
    "configured, profile_hash, content_hash = configure_profile(profile, base_hash)",
    "assert configured['samplePercent'] == 1",
    "assert profile_hash == base_hash",
    "custom, custom_hash, custom_content_hash = configure_profile(profile, base_hash, ['cs.AI', 'cs.LG'])",
    "assert custom['topics'] == ['cs.AI', 'cs.LG']",
    "assert custom_hash != profile_hash",
    "assert custom_content_hash != content_hash",
    "assert oai_set_spec('cs.AI') == 'cs:cs:AI'",
    "assert oai_set_spec('stat.ML') == 'stat:stat:ML'",
    "assert oai_set_spec('astro-ph.CO') == 'physics:astro-ph:CO'",
    "small = {value for value in map(str, range(10000)) if sampled(value, 1, profile['sampleSeed'])}",
    "medium = {value for value in map(str, range(10000)) if sampled(value, 25, profile['sampleSeed'])}",
    "large = {value for value in map(str, range(10000)) if sampled(value, 100, profile['sampleSeed'])}",
    "assert small < medium < large",
  ].join("\n");
  const result = spawnSync(pythonCommand, ["-c", script], {
    cwd: moduleDirectory,
    encoding: "utf8",
    timeout: 30_000,
    env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" },
  });
  assert.equal(result.status, 0, result.stderr || result.error?.message);
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
    assert.equal(responses[4].result.structuredContent.samplePercent, 100);
    assert.equal(responses[4].result.structuredContent.contentMode, "profile");
    assert.deepEqual(responses[4].result.structuredContent.topics, ["cs.IR", "cs.DB"]);
    assert.match(responses[4].result.structuredContent.source, /bulk metadata snapshot/);
    assert.match(responses[4].result.structuredContent.catchUpSourceUrl, /oaipmh\.arxiv\.org/);
    assert.equal(responses[5].result.isError, true);
  } finally {
    rmSync(dataDirectory, { recursive: true, force: true });
  }
});

test("reuses a matching snapshot and adds newer OAI records without duplicates", { skip: !pythonCommand }, () => {
  const dataDirectory = mkdtempSync(path.join(tmpdir(), "vanta-corpus-grow-"));
  const profilePath = path.join(dataDirectory, "profile.json");
  writeFileSync(profilePath, JSON.stringify({
    schemaVersion: 2,
    id: "fixture",
    source: "arxiv-bulk-snapshot",
    samplePercent: 100,
    sampleSeed: "fixture-v1",
    topics: ["cs.IR"],
  }));
  const script = [
    "import json, zipfile",
    "from pathlib import Path",
    "from corpus import connect",
    "from provision import provision",
    `archive = Path(${JSON.stringify(path.join(dataDirectory, "snapshot.zip"))})`,
    "snapshot_record = {'id':'2609.00001','authors':'Ada Example','authors_parsed':[['Example','Ada','']],'title':'Document Retrieval','abstract':'Snapshot metadata.','categories':'cs.IR','versions':[{'version':'v1','created':'Thu, 10 Sep 2026 00:00:00 GMT'}],'update_date':'2026-09-10'}",
    "with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as bundle:",
    "    bundle.writestr('arxiv-metadata-oai-snapshot.json', json.dumps(snapshot_record) + '\\n')",
    "empty = b'<OAI-PMH xmlns=\"http://www.openarchives.org/OAI/2.0/\"><ListRecords /></OAI-PMH>'",
    "catchup = b'''<OAI-PMH xmlns=\"http://www.openarchives.org/OAI/2.0/\"><ListRecords><record><header><identifier>oai:arXiv.org:2609.00001</identifier></header><metadata><arXiv xmlns=\"http://arxiv.org/OAI/arXiv/\"><id>2609.00001</id><created>2026-09-10</created><updated>2026-09-13</updated><authors><author><keyname>Example</keyname><forenames>Ada</forenames></author></authors><title>Document Retrieval Updated</title><categories>cs.IR</categories><abstract>Updated metadata.</abstract></arXiv></metadata></record><record><header><identifier>oai:arXiv.org:2609.00002</identifier></header><metadata><arXiv xmlns=\"http://arxiv.org/OAI/arXiv/\"><id>2609.00002</id><created>2026-09-13</created><authors><author><keyname>Sample</keyname><forenames>Grace</forenames></author></authors><title>New Retrieval Work</title><categories>cs.IR cs.AI</categories><abstract>New metadata.</abstract></arXiv></metadata></record></ListRecords></OAI-PMH>'''",
    "class Client:",
    "    def __init__(self, payload): self.payload, self.calls = payload, 0",
    "    def fetch(self, category, from_date, until_date, token=None):",
    "        self.calls += 1",
    "        return self.payload",
    "first = Client(empty)",
    `assert provision(Path(${JSON.stringify(dataDirectory)}), Path(${JSON.stringify(profilePath)}), first, '202609131200', snapshot_path=archive) == {'records': 1, 'reused': False}`,
    "second = Client(catchup)",
    `assert provision(Path(${JSON.stringify(dataDirectory)}), Path(${JSON.stringify(profilePath)}), second, '202609141200', snapshot_path=archive) == {'records': 2, 'reused': True}`,
    `with connect(Path(${JSON.stringify(path.join(dataDirectory, "corpus.db"))}), readonly=True) as connection:`,
    "    assert [row[0] for row in connection.execute('SELECT id FROM papers ORDER BY id')] == ['2609.00001', '2609.00002']",
    "    metadata = dict(connection.execute('SELECT key, value FROM metadata'))",
    "    assert metadata['sample_percent'] == '100'",
    "    assert metadata['configured_categories'] == '[\"cs.IR\"]'",
    "    assert metadata['snapshot_cutoff'] == '2026-09-10T00:00:00Z'",
    "    assert metadata['catchup_cutoff'] == '2026-09-14'",
    "assert second.calls == 1",
  ].join("\n");
  try {
    const result = spawnSync(pythonCommand, ["-c", script], {
      cwd: moduleDirectory,
      encoding: "utf8",
      timeout: 30_000,
      env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" },
    });
    assert.equal(result.status, 0, result.stderr || result.error?.message);
  } finally {
    rmSync(dataDirectory, { recursive: true, force: true });
  }
});

test("arXiv throttling uses bounded Retry-After and exponential backoff", { skip: !pythonCommand }, () => {
  const script = [
    "import urllib.error",
    "import provision",
    "from provision import ArxivClient",
    "sleeps = []",
    "clock = [100.0]",
    "attempts = 0",
    "provision.time.monotonic = lambda: clock[0]",
    "def sleep(seconds):",
    "    sleeps.append(seconds)",
    "    clock[0] += seconds",
    "class Response:",
    "    def __enter__(self): return self",
    "    def __exit__(self, *args): pass",
    "    def read(self, _size): return b'<feed />'",
    "def opener(_request, timeout):",
    "    global attempts",
    "    attempts += 1",
    "    if attempts == 1: raise urllib.error.HTTPError('url', 429, 'limited', {'Retry-After': '17'}, None)",
    "    return Response()",
    "client = ArxivClient(opener=opener, sleeper=sleep)",
    "assert client.fetch('cat:cs.IR', 0, 1) == b'<feed />'",
    "assert sleeps == [17.0]",
  ].join("\n");
  const result = spawnSync(pythonCommand, ["-c", script], {
    cwd: moduleDirectory,
    encoding: "utf8",
    timeout: 30_000,
    env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" },
  });
  assert.equal(result.status, 0, result.stderr || result.error?.message);
});

test("parses resumable arXiv OAI metadata pages", { skip: !pythonCommand }, () => {
  const script = [
    "from provision import parse_oai_feed",
    "payload = b'''<OAI-PMH xmlns=\"http://www.openarchives.org/OAI/2.0/\"><ListRecords><record><header><identifier>oai:arXiv.org:2609.12345</identifier></header><metadata><arXiv xmlns=\"http://arxiv.org/OAI/arXiv/\"><id>2609.12345</id><created>2026-09-10</created><updated>2026-09-12</updated><authors><author><keyname>Example</keyname><forenames>Ada</forenames></author></authors><title>Bounded Retrieval</title><categories>cs.IR cs.AI</categories><doi>10.1/example</doi><abstract>Metadata search.</abstract></arXiv></metadata></record><resumptionToken>next-token</resumptionToken></ListRecords></OAI-PMH>'''",
    "papers, token = parse_oai_feed(payload, 'oai:test', 'fixture', '2026-09-13T00:00:00Z')",
    "assert token == 'next-token'",
    "assert papers[0]['id'] == '2609.12345'",
    "assert papers[0]['authors_search'] == 'Ada Example'",
    "assert papers[0]['categories_search'] == '|cs.IR|cs.AI|'",
    "assert papers[0]['published'] == '2026-09-10T00:00:00Z'",
  ].join("\n");
  const result = spawnSync(pythonCommand, ["-c", script], {
    cwd: moduleDirectory,
    encoding: "utf8",
    timeout: 30_000,
    env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" },
  });
  assert.equal(result.status, 0, result.stderr || result.error?.message);
});