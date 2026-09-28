import assert from "node:assert/strict";
import { createHash } from "node:crypto";
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
      { name: "corpus_categories", arguments: { contains: "retrieval" } },
    ]);
    assert.deepEqual(responses[1].result.tools.map((tool) => tool.name), ["corpus_search", "corpus_get", "corpus_info", "corpus_categories", "corpus_refresh"]);
    assert.equal(responses[2].result.structuredContent.results[0].id, "2609.00001");
    assert.equal(responses[2].result.structuredContent.results[0].source, "arxiv");
    assert.match(responses[2].result.structuredContent.results[0].license, /arXiv API terms/);
    assert.equal(responses[3].result.structuredContent.authors[0], "Ada Example");
    assert.equal(responses[4].result.structuredContent.records, 2);
    assert.equal(responses[4].result.structuredContent.samplePercent, 100);
    assert.equal(responses[4].result.structuredContent.contentMode, "profile");
    assert.deepEqual(responses[4].result.structuredContent.topics, ["cs.IR", "cs.DB"]);
    assert.match(responses[4].result.structuredContent.source, /bulk metadata snapshot/);
    assert.match(responses[4].result.structuredContent.catchUpSourceUrl, /oaipmh\.arxiv\.org/);
    assert.deepEqual(responses[4].result.structuredContent.sources.map((entry) => entry.source), ["arxiv"]);
    assert.equal(responses[4].result.structuredContent.sources[0].records, 2);
    assert.equal(responses[5].result.isError, true);

    // A plain-language subject must resolve to the identifier corpus_search accepts.
    const categories = responses[6].result.structuredContent;
    assert.equal(categories.countsIncludeCrossLists, true);
    assert.deepEqual(categories.categories.map((entry) => entry.category), ["cs.IR"]);
    assert.equal(categories.categories[0].name, "Information Retrieval");
    assert.equal(categories.categories[0].group, "Computer Science");
    assert.equal(categories.categories[0].ingestionTopic, true);
    assert.ok(categories.categories[0].records > 0);
  } finally {
    rmSync(dataDirectory, { recursive: true, force: true });
  }
});

test("reports coverage from recorded totals instead of scanning the corpus", { skip: !pythonCommand }, () => {
  const dataDirectory = mkdtempSync(path.join(tmpdir(), "vanta-corpus-"));
  try {
    provisionFixture(dataDirectory);
    // Emptying the table proves the counts come from recorded totals: a scan would now report zero.
    const script = [
      "from pathlib import Path",
      "from corpus import connect",
      `with connect(Path(${JSON.stringify(path.join(dataDirectory, "corpus.db"))})) as connection:`,
      "    connection.execute('DELETE FROM papers')",
    ].join("\n");
    const emptied = spawnSync(pythonCommand, ["-c", script], {
      cwd: moduleDirectory,
      encoding: "utf8",
      timeout: 30_000,
      env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" },
    });
    assert.equal(emptied.status, 0, emptied.stderr || emptied.error?.message);

    const responses = runProtocol(dataDirectory, [
      { name: "corpus_info", arguments: {} },
      { name: "corpus_categories", arguments: { contains: "retrieval" } },
    ]);
    const info = responses[2].result.structuredContent;
    assert.equal(info.records, 2);
    assert.equal(info.sources[0].records, 2);
    assert.deepEqual(info.categories, [{ category: "cs.DB", records: 1 }, { category: "cs.IR", records: 1 }]);
    assert.equal(responses[3].result.structuredContent.categories[0].primaryRecords, 1);
  } finally {
    rmSync(dataDirectory, { recursive: true, force: true });
  }
});

test("applies query operators and corrects spelling only when a query finds nothing", { skip: !pythonCommand }, () => {
  const dataDirectory = mkdtempSync(path.join(tmpdir(), "vanta-corpus-query-"));
  try {
    provisionFixture(dataDirectory);
    const ids = (response) => response.result.structuredContent.results.map((paper) => paper.id);
    const responses = runProtocol(dataDirectory, [
      { name: "corpus_search", arguments: { query: '"document retrieval"' } },
      { name: "corpus_search", arguments: { query: "example -database" } },
      { name: "corpus_search", arguments: { query: "retrieval OR database" } },
      { name: "corpus_search", arguments: { query: "title:database" } },
      { name: "corpus_search", arguments: { query: "datab*" } },
      { name: "corpus_search", arguments: { query: "retreival" } },
      { name: "corpus_search", arguments: { query: "retreival", fuzzy: false } },
      { name: "corpus_search", arguments: { query: "retrieval" } },
      { name: "corpus_search", arguments: { query: "-database" } },
    ]);

    assert.deepEqual(ids(responses[2]), ["2609.00001"], "quoted phrase matches adjacent words");
    assert.deepEqual(ids(responses[3]), ["2609.00001"], "-term excludes a match");
    assert.deepEqual(ids(responses[4]).sort(), ["2609.00001", "2609.00002"], "OR admits either term");
    assert.deepEqual(ids(responses[5]), ["2609.00002"], "field prefix restricts to one column");
    assert.deepEqual(ids(responses[6]), ["2609.00002"], "trailing * matches by prefix");

    assert.deepEqual(ids(responses[7]), ["2609.00001"], "a misspelling still finds the paper");
    assert.deepEqual(responses[7].result.structuredContent.corrections, [{ from: "retreival", to: "retrieval" }]);
    assert.deepEqual(ids(responses[8]), [], "fuzzy: false leaves the misspelling uncorrected");
    assert.match(responses[8].result.structuredContent.hint, /combined with AND/, "an empty result explains the syntax");

    // A query that matches must never be silently rewritten.
    assert.equal(responses[9].result.structuredContent.corrections, undefined);
    assert.equal(responses[9].result.structuredContent.hint, undefined);
    assert.equal(responses[10].result.isError, true, "exclusions alone are not a search");
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
    // A reuse run skips the full index rebuild, so catch-up records must be indexed as they arrive.
    "    assert connection.execute(\"SELECT count(*) FROM papers_fts WHERE papers_fts MATCH 'new'\").fetchone()[0] == 1",
    "    assert connection.execute(\"SELECT count(*) FROM papers_fts WHERE papers_fts MATCH 'updated'\").fetchone()[0] == 1",
    "    assert connection.execute('SELECT count(*) FROM papers_fts').fetchone()[0] == 2",
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
    "import fetching",
    "from provision import ArxivClient",
    "sleeps = []",
    "clock = [100.0]",
    "attempts = 0",
    "fetching.time.monotonic = lambda: clock[0]",
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

// A packaged profile's bytes are its content identity. Changing one silently discards the corpus an
// installed node already holds, so the checksums are pinned rather than merely documented.
test("packaged arXiv profiles keep the identities installed corpora were built from", () => {
  const checksums = {
    "small-arxiv.json": "15f785048eb66fcd86e059bf2b370604af795fa235b1e704745735fd145ecbb7",
    "medium-arxiv.json": "6a3402cad17eacf7333c758bd4d616b4a76265da97c6e540036fc28ad75820d4",
    "large-arxiv.json": "ce4ece6c777b10971812f44abb9d313f9ccb94658de5b5529f136dd9b3aa7808",
  };
  for (const [name, expected] of Object.entries(checksums)) {
    const raw = readFileSync(path.join(moduleDirectory, "profiles", name));
    assert.equal(createHash("sha256").update(raw).digest("hex"), expected, `${name} must keep its content identity`);
  }
});

test("packaged profiles each configure exactly one source", { skip: !pythonCommand }, () => {
  const script = [
    "from pathlib import Path",
    "import sources",
    "from provision import configure_profiles, load_profiles",
    `directory = Path(${JSON.stringify(path.join(moduleDirectory, "profiles"))})`,
    "paths = sorted(directory.glob('*.json'))",
    "loaded = {}",
    "for candidate in paths:",
    "    profiles, base_hash = load_profiles([candidate])",
    "    configured, profile_hash, _ = configure_profiles(profiles, base_hash)",
    "    assert profile_hash == base_hash",
    "    loaded[configured[0]['id']] = configured[0]['source']",
    "assert loaded['small-arxiv-cs'] == 'arxiv-bulk-snapshot'",
    "assert loaded['wikipedia-en-titles'] == 'wikipedia-title-index'",
    "assert loaded['pubchemlite-exposomics'] == 'pubchemlite-compound-index'",
    "assert set(loaded) == {'small-arxiv-cs', 'medium-arxiv-cs', 'large-arxiv-cs', 'wikipedia-en-titles', 'pubchemlite-exposomics'}",
    // Composing independent profiles changes the identity, and the order they are named does not.
    "combined = [directory / 'small-arxiv.json', directory / 'wikipedia-en-titles.json']",
    "profiles, base_hash = load_profiles(combined)",
    "assert [profile['source'] for profile in profiles] == ['arxiv-bulk-snapshot', 'wikipedia-title-index']",
    "assert base_hash == load_profiles(list(reversed(combined)))[1]",
    "assert base_hash != load_profiles([combined[0]])[1]",
    "try:",
    "    load_profiles([combined[0], combined[0]])",
    "except ValueError as error:",
    "    assert 'more than once' in str(error), error",
    "else:",
    "    raise AssertionError('a repeated profile must be rejected')",
  ].join("\n");
  const result = spawnSync(pythonCommand, ["-c", script], {
    cwd: moduleDirectory,
    encoding: "utf8",
    timeout: 30_000,
    env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" },
  });
  assert.equal(result.status, 0, result.stderr || result.error?.message);
});

test("migrates a version 1 corpus in place instead of reingesting it", { skip: !pythonCommand }, () => {
  const dataDirectory = mkdtempSync(path.join(tmpdir(), "vanta-corpus-migrate-"));
  const profilePath = path.join(dataDirectory, "profile.json");
  writeFileSync(profilePath, JSON.stringify({
    schemaVersion: 2,
    id: "fixture",
    source: "arxiv-bulk-snapshot",
    samplePercent: 100,
    sampleSeed: "fixture-v1",
    topics: ["cs.IR"],
  }));
  const legacySchema = [
    "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);",
    "CREATE TABLE papers (id TEXT PRIMARY KEY, title TEXT NOT NULL, abstract TEXT NOT NULL, authors_json TEXT NOT NULL,",
    " authors_search TEXT NOT NULL, categories_json TEXT NOT NULL, categories_search TEXT NOT NULL, primary_category TEXT,",
    " published TEXT NOT NULL, updated TEXT NOT NULL, doi TEXT, journal_ref TEXT, comment TEXT, abstract_url TEXT NOT NULL,",
    " pdf_url TEXT, source_query TEXT NOT NULL, profile_slice TEXT NOT NULL, fetched_at TEXT NOT NULL);",
    "CREATE INDEX papers_published_idx ON papers(published);",
    "CREATE INDEX papers_primary_category_idx ON papers(primary_category);",
    "CREATE VIRTUAL TABLE papers_fts USING fts5(title, abstract, authors, categories, tokenize='unicode61 remove_diacritics 2');",
    "CREATE VIRTUAL TABLE papers_vocab USING fts5vocab(papers_fts, 'row');",
  ].join("");
  const script = [
    "import hashlib, json, sqlite3, zipfile",
    "from pathlib import Path",
    "from corpus import connect",
    "from provision import provision",
    `data = Path(${JSON.stringify(dataDirectory)})`,
    `profile_path = Path(${JSON.stringify(profilePath)})`,
    `archive = data / 'snapshot.zip'`,
    "snapshot_record = {'id':'2609.00001','authors':'Ada Example','authors_parsed':[['Example','Ada','']],'title':'Document Retrieval','abstract':'Snapshot metadata.','categories':'cs.IR','versions':[{'version':'v1','created':'Thu, 10 Sep 2026 00:00:00 GMT'}],'update_date':'2026-09-10'}",
    "with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as bundle:",
    "    bundle.writestr('arxiv-metadata-oai-snapshot.json', json.dumps(snapshot_record) + '\\n')",
    "with zipfile.ZipFile(archive) as bundle:",
    "    member = bundle.getinfo('arxiv-metadata-oai-snapshot.json')",
    "    identity = '%08x:%d:%d' % (member.CRC, member.file_size, member.compress_size)",
    // A corpus written by the previous release, holding a record the snapshot does not contain.
    "legacy = sqlite3.connect(data / 'corpus.db')",
    `legacy.executescript(${JSON.stringify(legacySchema)})`,
    "legacy.execute('INSERT INTO papers VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', ('2609.09999', 'Legacy Retrieval Record', 'Retained from the previous schema.', '[\"Ada Example\"]', 'Ada Example', '[\"cs.IR\"]', '|cs.IR|', 'cs.IR', '2026-09-01T00:00:00Z', '2026-09-01T00:00:00Z', None, None, None, 'https://arxiv.org/abs/2609.09999', None, 'bulk:sample=100%;topics=cs.IR', 'bulk-snapshot', '2026-09-01T00:00:00Z'))",
    "legacy.execute('INSERT INTO papers_fts(rowid, title, abstract, authors, categories) SELECT rowid, title, abstract, authors_search, categories_search FROM papers')",
    "legacy.executemany('INSERT INTO metadata VALUES(?, ?)', [",
    "    ('schema_version', '1'),",
    "    ('profile_id', 'fixture'),",
    "    ('profile_hash', hashlib.sha256(profile_path.read_bytes()).hexdigest()),",
    "    ('sample_percent', '100'),",
    "    ('content_mode', 'profile'),",
    "    ('configured_categories', '[\"cs.IR\"]'),",
    "    ('snapshot_identity', identity),",
    "    ('snapshot_cutoff', '2026-09-10T00:00:00Z'),",
    "    ('catchup_cutoff', '2026-09-13'),",
    "])",
    "legacy.commit()",
    "legacy.close()",
    "class Client:",
    "    def fetch(self, category, from_date, until_date, token=None):",
    "        assert from_date == '2026-09-13', from_date",
    "        return b'<OAI-PMH xmlns=\"http://www.openarchives.org/OAI/2.0/\"><ListRecords /></OAI-PMH>'",
    "result = provision(data, profile_path, Client(), '202609141200', snapshot_path=archive)",
    "assert result == {'records': 1, 'reused': True}, result",
    "with connect(data / 'corpus.db', readonly=True) as connection:",
    "    metadata = dict(connection.execute('SELECT key, value FROM metadata'))",
    "    assert metadata['schema_version'] == '2'",
    "    assert metadata['profile_hash'] == hashlib.sha256(profile_path.read_bytes()).hexdigest()",
    "    row = connection.execute('SELECT source, license, title FROM papers WHERE id = ?', ('2609.09999',)).fetchone()",
    "    assert row['source'] == 'arxiv' and row['title'] == 'Legacy Retrieval Record'",
    "    assert 'arXiv' in row['license']",
    "    assert connection.execute(\"SELECT count(*) FROM papers_fts WHERE papers_fts MATCH 'legacy'\").fetchone()[0] == 1",
    "    sources_row = connection.execute('SELECT * FROM sources').fetchone()",
    "    assert sources_row['source'] == 'arxiv' and sources_row['records'] == 1",
    "    assert sources_row['snapshot_cutoff'] == '2026-09-10T00:00:00Z'",
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

test("adding a source keeps the records the retained corpus already holds", { skip: !pythonCommand }, () => {
  const dataDirectory = mkdtempSync(path.join(tmpdir(), "vanta-corpus-add-"));
  const arxivProfile = path.join(dataDirectory, "arxiv-profile.json");
  const wikipediaProfile = path.join(dataDirectory, "wikipedia-profile.json");
  writeFileSync(arxivProfile, JSON.stringify({
    schemaVersion: 2,
    id: "fixture-arxiv",
    source: "arxiv-bulk-snapshot",
    samplePercent: 100,
    sampleSeed: "fixture-v1",
    topics: ["cs.IR"],
  }));
  writeFileSync(wikipediaProfile, JSON.stringify({
    schemaVersion: 2,
    id: "fixture-wikipedia",
    source: "wikipedia-title-index",
    wiki: "enwiki",
    samplePercent: 100,
    sampleSeed: "fixture-wiki-v1",
  }));
  const script = [
    "import gzip, json, zipfile",
    "from contextlib import closing",
    "from pathlib import Path",
    "from corpus import connect",
    "from provision import provision",
    `data = Path(${JSON.stringify(dataDirectory)})`,
    `arxiv = Path(${JSON.stringify(arxivProfile)})`,
    `wikipedia = Path(${JSON.stringify(wikipediaProfile)})`,
    "archive = data / 'snapshot.zip'",
    "record = {'id':'2609.00001','authors':'Ada Example','authors_parsed':[['Example','Ada','']],'title':'Document Retrieval','abstract':'Snapshot metadata.','categories':'cs.IR','versions':[{'version':'v1','created':'Thu, 10 Sep 2026 00:00:00 GMT'}],'update_date':'2026-09-10'}",
    "with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as bundle:",
    "    bundle.writestr('arxiv-metadata-oai-snapshot.json', json.dumps(record) + '\\n')",
    "titles = data / 'titles.gz'",
    "with gzip.open(titles, 'wb') as handle:",
    "    handle.write(b'page_title\\nRobot_learning\\nDatabase_index\\n')",
    "class Client:",
    "    def __init__(self): self.calls = 0",
    "    def fetch(self, category, from_date, until_date, token=None):",
    "        self.calls += 1",
    "        return b'<OAI-PMH xmlns=\"http://www.openarchives.org/OAI/2.0/\"><ListRecords /></OAI-PMH>'",
    // A corpus holding arXiv alone, as an installed node would already have.
    "assert provision(data, arxiv, Client(), '202609131200', snapshot_path=archive) == {'records': 1, 'reused': False}",
    "with closing(connect(data / 'corpus.db', readonly=True)) as connection:",
    "    fetched = connection.execute('SELECT fetched_at FROM papers WHERE id = ?', ('2609.00001',)).fetchone()[0]",
    // Adding an independent source must not disturb the source already present.
    "overrides = {'wikipedia': {'dump_path': titles}}",
    "result = provision(data, [arxiv, wikipedia], Client(), '202609141200', snapshot_path=archive, overrides=overrides)",
    "assert result == {'records': 3, 'reused': True}, result",
    "with closing(connect(data / 'corpus.db', readonly=True)) as connection:",
    "    rows = {row['source']: row for row in connection.execute('SELECT * FROM sources')}",
    "    assert sorted(rows) == ['arxiv', 'wikipedia'], sorted(rows)",
    "    assert rows['arxiv']['records'] == 1 and rows['wikipedia']['records'] == 2",
    "    assert rows['arxiv']['profile_id'] == 'fixture-arxiv'",
    "    assert rows['wikipedia']['profile_id'] == 'fixture-wikipedia'",
    "    kept = connection.execute('SELECT fetched_at FROM papers WHERE id = ?', ('2609.00001',)).fetchone()[0]",
    "    assert kept == fetched, 'the arXiv record was reingested instead of kept'",
    "    assert connection.execute(\"SELECT count(*) FROM papers_fts WHERE papers_fts MATCH 'retrieval'\").fetchone()[0] == 1",
    // Dropping a source removes its records and leaves the other one alone.
    "assert provision(data, arxiv, Client(), '202609151200', snapshot_path=archive) == {'records': 1, 'reused': True}",
    "with closing(connect(data / 'corpus.db', readonly=True)) as connection:",
    "    assert [row[0] for row in connection.execute('SELECT source FROM sources')] == ['arxiv']",
    "    assert connection.execute('SELECT count(*) FROM papers WHERE source = ?', ('wikipedia',)).fetchone()[0] == 0",
    "    assert connection.execute(\"SELECT count(*) FROM papers_fts WHERE papers_fts MATCH 'robot'\").fetchone()[0] == 0",
    "    assert connection.execute('SELECT fetched_at FROM papers WHERE id = ?', ('2609.00001',)).fetchone()[0] == fetched",
  ].join("\n");
  try {
    const result = spawnSync(pythonCommand, ["-c", script], {
      cwd: moduleDirectory,
      encoding: "utf8",
      timeout: 60_000,
      env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" },
    });
    assert.equal(result.status, 0, result.stderr || result.error?.message);
  } finally {
    rmSync(dataDirectory, { recursive: true, force: true });
  }
});

test("a newer adapter record format reingests the source it parses", { skip: !pythonCommand }, () => {
  const script = [
    "import json",
    "from pathlib import Path",
    "import sources",
    "from provision import load_profile, source_config_hash",
    `directory = Path(${JSON.stringify(path.join(moduleDirectory, "profiles"))})`,
    "profile, digest = load_profile(directory / 'pubchemlite-exposomics.json')",
    "profile['digest'] = digest",
    "adapter = sources.by_key(profile['source'])",
    "before = source_config_hash(profile)",
    // A corpus already holding these records must be rebuilt when the parser that produced them changes.
    "adapter.__class__.record_version += 1",
    "assert source_config_hash(profile) != before, 'a newer record format must invalidate the retained records'",
    "adapter.__class__.record_version -= 1",
    "assert source_config_hash(profile) == before",
    // Version 1 contributes nothing to the hash, so sources whose format never changed are undisturbed.
    "assert getattr(sources.by_key('arxiv-bulk-snapshot'), 'record_version', 1) == 1",
    "assert getattr(sources.by_key('wikipedia-title-index'), 'record_version', 1) == 1",
  ].join("\n");
  const result = spawnSync(pythonCommand, ["-c", script], {
    cwd: moduleDirectory,
    encoding: "utf8",
    timeout: 30_000,
    env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" },
  });
  assert.equal(result.status, 0, result.stderr || result.error?.message);
});

test("changing a source's own profile reingests only that source", { skip: !pythonCommand }, () => {
  const dataDirectory = mkdtempSync(path.join(tmpdir(), "vanta-corpus-change-"));
  const wide = path.join(dataDirectory, "wide.json");
  const narrow = path.join(dataDirectory, "narrow.json");
  writeFileSync(wide, JSON.stringify({
    schemaVersion: 2,
    id: "fixture-wide",
    source: "arxiv-bulk-snapshot",
    samplePercent: 100,
    sampleSeed: "fixture-v1",
    topics: ["cs.IR", "cs.DB"],
  }));
  writeFileSync(narrow, JSON.stringify({
    schemaVersion: 2,
    id: "fixture-narrow",
    source: "arxiv-bulk-snapshot",
    samplePercent: 100,
    sampleSeed: "fixture-v1",
    topics: ["cs.IR"],
  }));
  const script = [
    "import json, zipfile",
    "from contextlib import closing",
    "from pathlib import Path",
    "from corpus import connect",
    "from provision import provision",
    `data = Path(${JSON.stringify(dataDirectory)})`,
    "archive = data / 'snapshot.zip'",
    "records = [",
    "    {'id':'2609.00001','authors':'A','authors_parsed':[['A','A','']],'title':'Document Retrieval','abstract':'x','categories':'cs.IR','versions':[{'version':'v1','created':'Thu, 10 Sep 2026 00:00:00 GMT'}],'update_date':'2026-09-10'},",
    "    {'id':'2609.00002','authors':'B','authors_parsed':[['B','B','']],'title':'Database Search','abstract':'y','categories':'cs.DB','versions':[{'version':'v1','created':'Fri, 11 Sep 2026 00:00:00 GMT'}],'update_date':'2026-09-11'},",
    "]",
    "with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as bundle:",
    "    bundle.writestr('arxiv-metadata-oai-snapshot.json', ''.join(json.dumps(item) + '\\n' for item in records))",
    "class Client:",
    "    def fetch(self, category, from_date, until_date, token=None):",
    "        return b'<OAI-PMH xmlns=\"http://www.openarchives.org/OAI/2.0/\"><ListRecords /></OAI-PMH>'",
    `assert provision(data, Path(${JSON.stringify(wide)}), Client(), '202609131200', snapshot_path=archive) == {'records': 2, 'reused': False}`,
    // The same source under a different topic list is not the same content, so it is rebuilt.
    `result = provision(data, Path(${JSON.stringify(narrow)}), Client(), '202609141200', snapshot_path=archive)`,
    "assert result == {'records': 1, 'reused': False}, result",
    "with closing(connect(data / 'corpus.db', readonly=True)) as connection:",
    "    assert [row[0] for row in connection.execute('SELECT id FROM papers')] == ['2609.00001']",
    "    assert connection.execute(\"SELECT count(*) FROM papers_fts WHERE papers_fts MATCH 'database'\").fetchone()[0] == 0",
  ].join("\n");
  try {
    const result = spawnSync(pythonCommand, ["-c", script], {
      cwd: moduleDirectory,
      encoding: "utf8",
      timeout: 60_000,
      env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" },
    });
    assert.equal(result.status, 0, result.stderr || result.error?.message);
  } finally {
    rmSync(dataDirectory, { recursive: true, force: true });
  }
});

test("composes a second source into one corpus and refreshes only what has a feed", { skip: !pythonCommand }, () => {
  const dataDirectory = mkdtempSync(path.join(tmpdir(), "vanta-corpus-sources-"));
  const arxivProfile = path.join(dataDirectory, "arxiv-profile.json");
  const wikipediaProfile = path.join(dataDirectory, "wikipedia-profile.json");
  writeFileSync(arxivProfile, JSON.stringify({
    schemaVersion: 2,
    id: "fixture-arxiv",
    source: "arxiv-bulk-snapshot",
    samplePercent: 100,
    sampleSeed: "fixture-v1",
    topics: ["cs.IR", "cs.DB"],
  }));
  writeFileSync(wikipediaProfile, JSON.stringify({
    schemaVersion: 2,
    id: "fixture-wikipedia",
    source: "wikipedia-title-index",
    wiki: "enwiki",
    samplePercent: 100,
    sampleSeed: "fixture-wiki-v1",
  }));
  const records = [
    { id: "2609.00001", authors: "Ada Example", authors_parsed: [["Example", "Ada", ""]], title: "Document Retrieval", abstract: "A document retrieval example.", categories: "cs.IR cs.AI", versions: [{ version: "v1", created: "Thu, 10 Sep 2026 00:00:00 GMT" }], update_date: "2026-09-10" },
    { id: "2609.00002", authors: "Grace Sample", authors_parsed: [["Sample", "Grace", ""]], title: "Database Search", abstract: "A database metadata example.", categories: "cs.DB", versions: [{ version: "v1", created: "Fri, 11 Sep 2026 00:00:00 GMT" }], update_date: "2026-09-11" },
  ];
  const provisionScript = [
    "import gzip, json, zipfile",
    "from pathlib import Path",
    "from provision import provision",
    `data = Path(${JSON.stringify(dataDirectory)})`,
    "archive = data / 'snapshot.zip'",
    `records = json.loads(${JSON.stringify(JSON.stringify(records))})`,
    "with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as bundle:",
    "    bundle.writestr('arxiv-metadata-oai-snapshot.json', ''.join(json.dumps(item) + '\\n' for item in records))",
    "titles = data / 'titles.gz'",
    "with gzip.open(titles, 'wb') as handle:",
    "    handle.write(b'page_title\\nDocument_retrieval\\nDatabase_index\\nRobot_learning\\n')",
    "class Client:",
    "    def fetch(self, category, from_date, until_date, token=None):",
    "        return b'<OAI-PMH xmlns=\"http://www.openarchives.org/OAI/2.0/\"><ListRecords /></OAI-PMH>'",
    `result = provision(data, [Path(${JSON.stringify(arxivProfile)}), Path(${JSON.stringify(wikipediaProfile)})], Client(), '202609131200', snapshot_path=archive, overrides={'wikipedia': {'dump_path': titles}})`,
    "assert result == {'records': 5, 'reused': False}, result",
  ].join("\n");
  const refreshScript = [
    "import json",
    "from pathlib import Path",
    "from provision import refresh",
    `data = Path(${JSON.stringify(dataDirectory)})`,
    "catchup = b'''<OAI-PMH xmlns=\"http://www.openarchives.org/OAI/2.0/\"><ListRecords><record><header><identifier>oai:arXiv.org:2609.00003</identifier></header><metadata><arXiv xmlns=\"http://arxiv.org/OAI/arXiv/\"><id>2609.00003</id><created>2026-09-14</created><authors><author><keyname>Sample</keyname><forenames>Grace</forenames></author></authors><title>Later Retrieval Work</title><categories>cs.IR</categories><abstract>Harvested after installation.</abstract></arXiv></metadata></record></ListRecords></OAI-PMH>'''",
    "class Client:",
    "    def fetch(self, category, from_date, until_date, token=None):",
    "        return catchup",
    "result = refresh(data, data, cutoff='202609151200', overrides={'arxiv': {'oai_client': Client()}})",
    "assert [entry['source'] for entry in result['refreshed']] == ['arxiv'], result",
    "assert result['refreshed'][0]['added'] == 1, result",
    "assert result['skipped'][0]['source'] == 'wikipedia', result",
    "assert 'incremental feed' in result['skipped'][0]['reason'], result",
    "assert result['records'] == 6, result",
  ].join("\n");
  const runPython = (script) => {
    const result = spawnSync(pythonCommand, ["-c", script], {
      cwd: moduleDirectory,
      encoding: "utf8",
      timeout: 60_000,
      env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" },
    });
    assert.equal(result.status, 0, result.stderr || result.error?.message);
  };
  try {
    runPython(provisionScript);
    const responses = runProtocol(dataDirectory, [
      { name: "corpus_search", arguments: { query: "retrieval" } },
      { name: "corpus_search", arguments: { query: "retrieval", source: "wikipedia" } },
      { name: "corpus_get", arguments: { id: "wikipedia:Robot_learning" } },
      { name: "corpus_info", arguments: {} },
      { name: "corpus_categories", arguments: { source: "wikipedia" } },
      { name: "corpus_search", arguments: { query: "retrieval", source: "nowhere" } },
    ]);
    assert.deepEqual(
      responses[2].result.structuredContent.results.map((entry) => entry.id).sort(),
      ["2609.00001", "wikipedia:Document_retrieval"],
      "one query reaches every source in the corpus",
    );
    assert.deepEqual(responses[3].result.structuredContent.results.map((entry) => entry.id), ["wikipedia:Document_retrieval"]);
    // A title index carries no abstract, so the snippet falls back to the title.
    assert.equal(responses[3].result.structuredContent.results[0].snippet, "Document retrieval");

    const article = responses[4].result.structuredContent;
    assert.equal(article.title, "Robot learning");
    assert.equal(article.source, "wikipedia");
    assert.equal(article.license, "CC BY-SA 4.0");
    assert.equal(article.abstractUrl, "https://en.wikipedia.org/wiki/Robot_learning");

    const described = responses[5].result.structuredContent.sources;
    assert.deepEqual(responses[5].result.structuredContent.profileIds, ["fixture-arxiv", "fixture-wikipedia"]);
    assert.deepEqual(described.map((entry) => entry.source), ["arxiv", "wikipedia"]);
    assert.deepEqual(described.map((entry) => entry.records), [2, 3]);
    assert.equal(described[1].license, "CC BY-SA 4.0");
    assert.equal(described[1].cutoff, described[1].snapshotCutoff, "a source without a feed keeps its snapshot cutoff");
    assert.match(described[0].termsUrl, /info\.arxiv\.org/);

    // Wikipedia titles carry no subject scheme, so no category is attributable to them.
    assert.deepEqual(responses[6].result.structuredContent.categories, []);
    assert.equal(responses[7].result.isError, true, "an unknown source is rejected rather than ignored");

    runPython(refreshScript);
    const afterRefresh = runProtocol(dataDirectory, [
      { name: "corpus_search", arguments: { query: "harvested" } },
      { name: "corpus_info", arguments: {} },
    ]);
    assert.deepEqual(afterRefresh[2].result.structuredContent.results.map((entry) => entry.id), ["2609.00003"], "a refreshed record is indexed immediately");
    assert.equal(afterRefresh[3].result.structuredContent.records, 6);
    assert.equal(afterRefresh[3].result.structuredContent.cutoff, "2026-09-15T23:59:59Z");
  } finally {
    rmSync(dataDirectory, { recursive: true, force: true });
  }
});

test("adds PubChemLite compounds without reindexing the source beside them", { skip: !pythonCommand }, () => {
  const dataDirectory = mkdtempSync(path.join(tmpdir(), "vanta-corpus-pubchem-"));
  const pubchemProfile = path.join(dataDirectory, "pubchem-profile.json");
  writeFileSync(pubchemProfile, JSON.stringify({
    schemaVersion: 2,
    id: "fixture-pubchem",
    source: "pubchemlite-compound-index",
    samplePercent: 100,
    sampleSeed: "fixture-pubchem-v1",
  }));
  const columns = [
    "Identifier", "FirstBlock", "PubMed_Count", "Patent_Count", "Related_CIDs", "Synonym", "MolecularFormula",
    "SMILES", "InChI", "InChIKey", "MonoisotopicMass", "XLogP", "CompoundName", "AnnoTypeCount",
    "AgroChemInfo", "BioPathway", "DrugMedicInfo", "FoodRelated", "PharmacoInfo", "SafetyInfo",
    "ToxicityInfo", "KnownUse", "DisorderDisease", "Identification", "NORMANSLE",
  ];
  const compounds = [
    ["2244", "BSYNRYMUTXBXSQ", "1", "2", "", "acetylsalicylic acid", "C9H8O4", "CC(=O)OC1=CC=CC=C1C(=O)O",
      "InChI=1S/C9H8O4/c1-6(10)13-8-5-3-2-4-7(8)9(11)12/h2-5H,1H3,(H,11,12)", "BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
      "180.042258736", "1.2", "Aspirin", "6", "0", "1", "4", "0", "2", "7", "3", "0", "0", "0", "1"],
    ["702", "LFQSCWFLJHTTHZ", "5", "6", "", "ethyl alcohol", "C2H6O", "CCO",
      "InChI=1S/C2H6O/c1-2-3/h3H,2H2,1H3", "LFQSCWFLJHTTHZ-UHFFFAOYSA-N",
      "46.041864814", "-0.1", "Ethanol", "2", "0", "0", "0", "5", "0", "3", "0", "0", "0", "0", "0"],
    ["99999", "ZZZZZZZZZZZZZZ", "0", "0", "", "", "C1H1", "C", "InChI=1S/CH4/h1H4", "ZZZZZZZZZZZZZZ-UHFFFAOYSA-N",
      "16.0313", "0.0", "Unannotated Example", "0", "0", "0", "0", "0", "0", "0", "0", "0", "0", "0", "0"],
  ];
  const script = [
    "import csv, json",
    "from contextlib import closing",
    "from pathlib import Path",
    "from corpus import connect",
    "from provision import provision",
    `data = Path(${JSON.stringify(dataDirectory)})`,
    "archive = data / 'snapshot.zip'",
    "dataset = data / 'pubchemlite.csv'",
    `columns = json.loads(${JSON.stringify(JSON.stringify(columns))})`,
    `compounds = json.loads(${JSON.stringify(JSON.stringify(compounds))})`,
    "with dataset.open('w', newline='', encoding='utf-8') as handle:",
    "    writer = csv.writer(handle)",
    "    writer.writerow(columns)",
    "    writer.writerows(compounds)",
    "class Client:",
    "    def fetch(self, category, from_date, until_date, token=None):",
    "        return b'<OAI-PMH xmlns=\"http://www.openarchives.org/OAI/2.0/\"><ListRecords /></OAI-PMH>'",
    // Marking the retained index makes a rebuild visible: a rebuild would erase this token.
    "with closing(connect(data / 'corpus.db')) as connection:",
    "    connection.execute(\"UPDATE papers_fts SET title = 'sentineltoken' WHERE rowid = (SELECT min(rowid) FROM papers_fts)\")",
    "    connection.commit()",
    `result = provision(data, [Path(${JSON.stringify(path.join(dataDirectory, "profile.json"))}), Path(${JSON.stringify(pubchemProfile)})], Client(), '202609131200', snapshot_path=archive, overrides={'pubchem': {'dataset_path': dataset}})`,
    "assert result == {'records': 5, 'reused': True}, result",
    "with closing(connect(data / 'corpus.db', readonly=True)) as connection:",
    "    assert connection.execute(\"SELECT count(*) FROM papers_fts WHERE papers_fts MATCH 'sentineltoken'\").fetchone()[0] == 1",
    "    assert connection.execute(\"SELECT count(*) FROM papers_fts WHERE papers_fts MATCH 'aspirin'\").fetchone()[0] == 1",
    "    assert connection.execute('SELECT count(*) FROM papers_fts').fetchone()[0] == 5",
    "    row = connection.execute(\"SELECT primary_category, categories_search, comment FROM papers WHERE id = 'pubchem:2244'\").fetchone()",
    "    assert row['primary_category'] == 'SafetyInfo', row['primary_category']",
    "    assert row['categories_search'] == '|BioPathway|DrugMedicInfo|PharmacoInfo|SafetyInfo|ToxicityInfo|NORMANSLE|', row['categories_search']",
    "    assert row['comment'].startswith('SMILES CC(=O)OC1'), row['comment']",
    "    bare = connection.execute(\"SELECT primary_category, categories_search FROM papers WHERE id = 'pubchem:99999'\").fetchone()",
    "    assert bare['primary_category'] is None and bare['categories_search'] == '||', tuple(bare)",
    // Structure strings stay out of the index: they would enlarge it without answering any query.
    "    assert connection.execute(\"SELECT count(*) FROM papers_fts WHERE papers_fts MATCH 'InChI'\").fetchone()[0] == 0",
  ].join("\n");
  try {
    provisionFixture(dataDirectory);
    const result = spawnSync(pythonCommand, ["-c", script], {
      cwd: moduleDirectory,
      encoding: "utf8",
      timeout: 60_000,
      env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" },
    });
    assert.equal(result.status, 0, result.stderr || result.error?.message);

    const responses = runProtocol(dataDirectory, [
      { name: "corpus_get", arguments: { id: "https://pubchem.ncbi.nlm.nih.gov/compound/702" } },
      { name: "corpus_search", arguments: { query: "acetylsalicylic", source: "pubchem" } },
      { name: "corpus_categories", arguments: { source: "pubchem", contains: "safety" } },
      { name: "corpus_info", arguments: {} },
    ]);
    const ethanol = responses[2].result.structuredContent;
    assert.equal(ethanol.id, "pubchem:702", "a PubChem URL resolves to its stored identifier");
    assert.equal(ethanol.title, "Ethanol");
    assert.equal(ethanol.license, "CC BY 4.0");
    assert.equal(ethanol.abstractUrl, "https://pubchem.ncbi.nlm.nih.gov/compound/702");
    assert.deepEqual(ethanol.categories, ["FoodRelated", "SafetyInfo"]);
    assert.match(ethanol.abstract, /Also known as ethyl alcohol\. Molecular formula C2H6O\./);
    // Annotation names stay out of the abstract: including them made the best-annotated compounds,
    // whose abstracts grew longest, rank below sparse ones under BM25 length normalization.
    assert.doesNotMatch(ethanol.abstract, /Food Related|Safety and Hazards/);
    const aspirin = runProtocol(dataDirectory, [{ name: "corpus_get", arguments: { id: "pubchem:2244" } }])[2];
    assert.ok(
      aspirin.result.structuredContent.abstract.length < ethanol.abstract.length * 1.5,
      "a compound with six annotations must not carry a far longer abstract than one with two",
    );

    assert.deepEqual(responses[3].result.structuredContent.results.map((entry) => entry.id), ["pubchem:2244"]);

    const safety = responses[4].result.structuredContent.categories;
    assert.deepEqual(safety.map((entry) => entry.category), ["SafetyInfo"]);
    assert.equal(safety[0].name, "Safety and Hazards");
    assert.equal(safety[0].group, "PubChemLite Annotations");
    assert.equal(safety[0].records, 2, "both annotated compounds count toward the category");
    assert.equal(safety[0].primaryRecords, 1, "only one of them is filed under it primarily");

    // Counts are recorded per source, so restricting to one must not surface another's scheme.
    const scoped = runProtocol(dataDirectory, [
      { name: "corpus_categories", arguments: { source: "pubchem" } },
      { name: "corpus_categories", arguments: {} },
    ]);
    const owned = scoped[2].result.structuredContent.categories.map((entry) => entry.category);
    assert.ok(owned.every((entry) => !entry.startsWith("cs.")), `arXiv categories leaked into a pubchem query: ${owned}`);
    const everything = scoped[3].result.structuredContent.categories.map((entry) => entry.category);
    assert.ok(everything.includes("cs.IR") && everything.includes("SafetyInfo"), "an unfiltered call spans every scheme");

    const described = responses[5].result.structuredContent.sources;
    assert.deepEqual(described.map((entry) => entry.source), ["arxiv", "pubchem"]);
    assert.deepEqual(described.map((entry) => entry.records), [2, 3]);
    assert.equal(described[1].license, "CC BY 4.0");
    assert.match(described[1].sourceUrl, /zenodo\.5995885/);
  } finally {
    rmSync(dataDirectory, { recursive: true, force: true });
  }
});