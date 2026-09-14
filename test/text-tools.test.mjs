import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import path from "node:path";
import test from "node:test";

const moduleDirectory = path.resolve(import.meta.dirname, "..", "modules", "text-tools");
const pythonCommand = ["python3", "python"].find((command) => spawnSync(command, ["--version"], { encoding: "utf8" }).status === 0);

function runProtocol(calls) {
  assert.ok(pythonCommand, "Python 3 is required to test text-tools");
  const requests = [
    { jsonrpc: "2.0", id: 1, method: "initialize", params: { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "test", version: "1" } } },
    { jsonrpc: "2.0", id: 2, method: "tools/list", params: {} },
    ...calls.map((call, index) => ({ jsonrpc: "2.0", id: index + 3, method: "tools/call", params: { name: call.tool, arguments: call.arguments } })),
  ];
  const result = spawnSync(pythonCommand, ["server.py"], {
    cwd: moduleDirectory,
    input: `${requests.map((request) => JSON.stringify(request)).join("\n")}\n`,
    encoding: "utf8",
    timeout: 30_000,
    env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" },
  });
  assert.equal(result.status, 0, result.stderr || result.error?.message);
  assert.equal(result.stderr, "");
  return result.stdout.trim().split(/\r?\n/).map((line) => JSON.parse(line));
}

const categoryOperations = {
  text_transform: ["case_convert", "slugify", "identifier_normalize", "whitespace_normalize", "unicode_normalize", "punctuation_normalize", "deduplicate", "line_sort", "line_filter", "wrap", "split_join", "template_fill", "line_slice", "replace_literal", "truncate", "pad_align", "ascii_fold", "number_format", "bytes_humanize", "pluralize"],
  text_extract: ["emails", "urls", "numbers", "ip_addresses", "datetimes", "code_blocks", "quoted_text", "uuids", "hashes", "semvers"],
  text_analyze: ["statistics", "readability", "keyword_frequency", "similarity", "ngrams", "sentence_split", "token_estimate", "text_inspect", "duplicate_lines", "chunk", "tfidf"],
  text_codec: ["base64", "url", "html", "unicode", "hex", "binary", "jwt_decode"],
  data_convert: ["json_format", "csv_normalize", "csv_to_json", "json_to_csv", "kv_to_json", "html_to_json", "jsonl_to_json", "json_to_jsonl", "json_flatten", "json_unflatten", "json_diff", "json_merge", "json_schema_infer", "env_to_json", "yaml_to_json", "json_to_yaml", "toml_to_json", "json_to_toml", "ini_to_json", "json_to_ini", "xml_to_json", "query_to_json", "json_to_query"],
  text_security: ["digest", "hmac", "checksum", "uuid_validate", "secret_scan", "redact", "password_strength"],
  text_generate: ["uuid", "password", "lorem", "passphrase"],
  developer_text: ["regex_extract", "regex_replace", "diff", "semver", "regex_test", "strip_comments"],
  document_process: ["markdown_to_text", "toc", "normalize_fences", "frontmatter_parse", "split_sections", "tables_extract", "links_extract", "heading_shift", "markdown_to_html"],
  table_transform: ["to_markdown", "sort"],
  datetime_text: ["parse", "format", "convert_timezone", "difference", "now", "duration_humanize", "duration_parse"],
  command_text: ["rg_search", "jq_filter", "awk_columns", "awk_stats", "sed_replace"],
};

/** Operations whose Debian package may be absent; they must then fail with a message naming it. */
const optionalOperations = new Set(["yaml_to_json", "json_to_yaml", "json_to_toml", "markdown_to_html", "pluralize", "passphrase", "convert_timezone"]);

const calls = [
  ["text_transform", { operation: "case_convert", text: "hello world", style: "camel" }],
  ["text_transform", { operation: "slugify", text: "Hello, world!" }],
  ["text_transform", { operation: "identifier_normalize", text: "42 things" }],
  ["text_transform", { operation: "whitespace_normalize", text: "  a   b  ", mode: "collapse" }],
  ["text_transform", { operation: "unicode_normalize", text: "e\u0301", form: "NFC" }],
  ["text_transform", { operation: "punctuation_normalize", text: "\u201chello\u201d\u2014yes" }],
  ["text_transform", { operation: "deduplicate", text: "a\na\nb" }],
  ["text_transform", { operation: "line_sort", text: "item10\nitem2", mode: "natural" }],
  ["text_transform", { operation: "line_filter", text: "keep\nskip", pattern: "keep" }],
  ["text_transform", { operation: "wrap", text: "one two three four five", width: 10 }],
  ["text_transform", { operation: "split_join", text: "a, b", inputDelimiter: ",", outputDelimiter: "|" }],
  ["text_transform", { operation: "template_fill", text: "Hello ${name}", values: { name: "Ada" } }],
  ["text_transform", { operation: "line_slice", text: "a\nb\nc\nd", start: 2, end: 3, numbered: true }],
  ["text_transform", { operation: "line_slice", text: "a\nb\nc\nd", tail: 2 }],
  ["text_transform", { operation: "replace_literal", text: "a.b.c", search: ".", replacement: "-" }],
  ["text_transform", { operation: "truncate", text: "one two three four", maxCharacters: 10 }],
  ["text_transform", { operation: "pad_align", text: "a\nbb", width: 4, align: "right" }],
  ["text_transform", { operation: "ascii_fold", text: "caf\u00e9 na\u00efve" }],
  ["text_transform", { operation: "number_format", value: 1234567.891, decimals: 2 }],
  ["text_transform", { operation: "bytes_humanize", bytes: 1536 }],
  ["text_transform", { operation: "pluralize", text: "index" }],
  ["text_extract", { operation: "emails", text: "a@example.com" }],
  ["text_extract", { operation: "urls", text: "See https://example.com/x." }],
  ["text_extract", { operation: "numbers", text: "1,234.5 and -2" }],
  ["text_extract", { operation: "ip_addresses", text: "10.0.0.1 and 2001:db8::1" }],
  ["text_extract", { operation: "datetimes", text: "2026-04-01T10:30:00Z" }],
  ["text_extract", { operation: "code_blocks", text: "Use `x` or ```js\ny\n```" }],
  ["text_extract", { operation: "quoted_text", text: "She said \"hello\"." }],
  ["text_extract", { operation: "uuids", text: "id 123e4567-e89b-12d3-a456-426614174000" }],
  ["text_extract", { operation: "hashes", text: "sha256 e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855" }],
  ["text_extract", { operation: "semvers", text: "v1.2.3 and 4.5.6-rc.1" }],
  ["text_analyze", { operation: "statistics", text: "One sentence." }],
  ["text_analyze", { operation: "readability", text: "This is one sentence. This is another sentence." }],
  ["text_analyze", { operation: "keyword_frequency", text: "red red blue" }],
  ["text_analyze", { operation: "similarity", text: "red blue", otherText: "red green" }],
  ["text_analyze", { operation: "ngrams", text: "one two three", n: 2 }],
  ["text_analyze", { operation: "sentence_split", text: "One. Two!" }],
  ["text_analyze", { operation: "token_estimate", text: "Estimate me" }],
  ["text_analyze", { operation: "text_inspect", text: "a\t b \u200b\r\nc\n" }],
  ["text_analyze", { operation: "duplicate_lines", text: "a\nb\na\nb\nc" }],
  ["text_analyze", { operation: "chunk", text: "one\n\ntwo\n\nthree\n\nfour", maxCharacters: 120, boundary: "paragraph" }],
  ["text_analyze", { operation: "chunk", text: "abcdefghij", maxCharacters: 100, overlapCharacters: 2, boundary: "character" }],
  ["text_analyze", { operation: "tfidf", documents: ["alpha alpha beta", "beta beta gamma"], maxResults: 3 }],
  ["text_codec", { operation: "base64", text: "hello", action: "encode" }],
  ["text_codec", { operation: "url", text: "a b", action: "encode" }],
  ["text_codec", { operation: "html", text: "<b>", action: "encode" }],
  ["text_codec", { operation: "unicode", text: "caf\u00e9", action: "encode" }],
  ["text_codec", { operation: "hex", text: "hi", action: "encode" }],
  ["text_codec", { operation: "binary", text: "A", action: "encode" }],
  ["text_codec", { operation: "jwt_decode", text: "eyJhbGciOiJub25lIn0.eyJzdWIiOiIxIn0." }],
  ["data_convert", { operation: "json_format", text: "{\"b\":2,\"a\":1}", sortKeys: true }],
  ["data_convert", { operation: "csv_normalize", text: "a;b\n1;2" }],
  ["data_convert", { operation: "csv_normalize", text: "a;b;c\n1;2" }],
  ["data_convert", { operation: "csv_to_json", text: "a,b\n1,2" }],
  ["data_convert", { operation: "json_to_csv", text: "[{\"a\":1,\"b\":2}]" }],
  ["data_convert", { operation: "kv_to_json", text: "level=info code:200 message=\"ready\"" }],
  ["data_convert", { operation: "html_to_json", text: "<h1>Docs</h1><a href=\"/api\">API</a>" }],
  ["data_convert", { operation: "toml_to_json", text: "name = \"test\"" }],
  ["data_convert", { operation: "ini_to_json", text: "[main]\nname=test" }],
  ["data_convert", { operation: "xml_to_json", text: "<root><item id=\"1\">x</item></root>" }],
  ["data_convert", { operation: "query_to_json", text: "a=1&a=2" }],
  ["data_convert", { operation: "json_to_query", text: "{\"a\":[1,2]}" }],
  ["data_convert", { operation: "jsonl_to_json", text: "{\"a\":1}\n{\"a\":2}" }],
  ["data_convert", { operation: "json_to_jsonl", text: "[{\"a\":1},{\"a\":2}]" }],
  ["data_convert", { operation: "json_flatten", text: "{\"a\":{\"b\":[1,2]}}" }],
  ["data_convert", { operation: "json_unflatten", text: "{\"a.b[0]\":1,\"a.b[1]\":2}" }],
  ["data_convert", { operation: "json_diff", text: "{\"a\":1,\"b\":2}", otherText: "{\"a\":9,\"c\":3}" }],
  ["data_convert", { operation: "json_merge", text: "{\"a\":{\"x\":1}}", otherText: "{\"a\":{\"y\":2}}", arrays: "concat" }],
  ["data_convert", { operation: "json_schema_infer", text: "[{\"a\":1},{\"a\":2,\"b\":\"x\"}]", samplesAreArray: true }],
  ["data_convert", { operation: "env_to_json", text: "# comment\nexport A=1\nB=\"two\"" }],
  ["data_convert", { operation: "yaml_to_json", text: "name: test\nitems:\n  - 1\n" }],
  ["data_convert", { operation: "json_to_yaml", text: "{\"name\":\"test\"}" }],
  ["data_convert", { operation: "json_to_toml", text: "{\"name\":\"test\"}" }],
  ["data_convert", { operation: "json_to_ini", text: "{\"main\":{\"name\":\"test\"}}" }],
  ["text_security", { operation: "digest", text: "abc", algorithm: "sha256" }],
  ["text_security", { operation: "hmac", text: "abc", key: "key", algorithm: "sha256" }],
  ["text_security", { operation: "checksum", text: "abc", algorithm: "crc32" }],
  ["text_security", { operation: "uuid_validate", text: "123e4567-e89b-12d3-a456-426614174000" }],
  ["text_security", { operation: "secret_scan", text: "aws_key = AKIAIOSFODNN7EXAMPLE\nplain text" }],
  ["text_security", { operation: "redact", text: "mail a@b.com from 10.0.0.1", categories: ["emails", "ip_addresses"] }],
  ["text_security", { operation: "password_strength", text: "correct-horse-battery" }],
  ["text_generate", { operation: "uuid", count: 2 }],
  ["text_generate", { operation: "password", length: 16 }],
  ["text_generate", { operation: "lorem", paragraphs: 1, sentencesPerParagraph: 1 }],
  ["text_generate", { operation: "passphrase", words: 4 }],
  ["developer_text", { operation: "regex_extract", text: "id=12 id=34", pattern: "id=(\\d+)" }],
  ["developer_text", { operation: "regex_replace", text: "a1b2", pattern: "\\d", replacement: "x" }],
  ["developer_text", { operation: "diff", before: "a\n", after: "b\n" }],
  ["developer_text", { operation: "semver", version: "1.2.3", action: "compare", otherVersion: "1.2.4" }],
  ["developer_text", { operation: "regex_test", pattern: "^a(\\d+)$", samples: ["a12", "b"] }],
  ["developer_text", { operation: "strip_comments", text: "x = 1  # note\ny = '# not a comment'\n" }],
  ["document_process", { operation: "markdown_to_text", text: "# Hello\n\n**world**" }],
  ["document_process", { operation: "toc", text: "# One\n## Two" }],
  ["document_process", { operation: "normalize_fences", text: "~~~js\nx\n~~~", marker: "```" }],
  ["document_process", { operation: "frontmatter_parse", text: "+++\nname = \"test\"\n+++\nBody" }],
  ["document_process", { operation: "split_sections", text: "# T\n\nlead\n\n## A\n\nbody\n\n## B\n", level: 2 }],
  ["document_process", { operation: "tables_extract", text: "| a | b |\n| --- | ---: |\n| 1 | 2 |\n" }],
  ["document_process", { operation: "links_extract", text: "[x](https://e.com \"t\") and ![i](/p.png)" }],
  ["document_process", { operation: "heading_shift", text: "# A\n\n## B\n", by: 1 }],
  ["document_process", { operation: "markdown_to_html", text: "# Title\n\ntext\n" }],
  ["datetime_text", { operation: "parse", text: "2026-09-13T12:00:00Z" }],
  ["datetime_text", { operation: "parse", text: "1789300800" }],
  ["datetime_text", { operation: "format", text: "2026-09-13T12:00:00Z", format: "%Y-%m-%d" }],
  ["datetime_text", { operation: "convert_timezone", text: "2026-09-13T12:00:00Z", toTimezone: "Europe/Berlin" }],
  ["datetime_text", { operation: "difference", text: "2026-01-01T00:00:00Z", otherText: "2026-01-02T06:00:00Z", unit: "hours" }],
  ["datetime_text", { operation: "now" }],
  ["datetime_text", { operation: "duration_humanize", seconds: 3661, parts: 3 }],
  ["datetime_text", { operation: "duration_parse", text: "1h 30m" }],
  ["table_transform", { operation: "to_markdown", text: "[{\"a\":1,\"b\":2}]" }],
  ["table_transform", { operation: "sort", text: "[{\"a\":2},{\"a\":1}]", column: "a", numeric: true }],
].map(([tool, args]) => ({ tool, arguments: args }));

test("Text Tools advertises strict category schemas and executes every built-in operation", () => {
  const responses = runProtocol(calls);
  assert.equal(responses[0].result.serverInfo.version, "0.4.1");
  const advertised = new Map(responses[1].result.tools.map((tool) => [tool.name, tool]));
  assert.deepEqual([...advertised.keys()], Object.keys(categoryOperations));
  for (const [category, operations] of Object.entries(categoryOperations)) {
    const inputSchema = advertised.get(category).inputSchema;
    assert.equal(inputSchema.type, "object");
    const schemas = inputSchema.oneOf;
    assert.deepEqual(schemas.map((schema) => schema.properties.operation.const), operations);
    assert.ok(schemas.every((schema) => schema.additionalProperties === false));
  }
  for (const [index, call] of calls.entries()) {
    const response = responses[index + 2];
    const optional = optionalOperations.has(call.arguments.operation);
    if (optional && response.result.isError === true) {
      assert.match(response.result.content[0].text, /which is not installed on this node/, `${call.tool}.${call.arguments.operation} failed for the wrong reason`);
      continue;
    }
    assert.notEqual(response.result.isError, true, `${call.tool}.${call.arguments.operation}: ${response.result.content?.[0]?.text}`);
    assert.equal(typeof response.result.structuredContent, "object");
  }
});

function commandAvailable(command) {
  return spawnSync(command, ["--version"], { encoding: "utf8" }).error?.code !== "ENOENT";
}

test("available command wrappers process stdin without shell execution", () => {
  const commandCalls = [
    ["rg", { tool: "command_text", arguments: { operation: "rg_search", text: "one\ntwo one", pattern: "one" } }],
    ["jq", { tool: "command_text", arguments: { operation: "jq_filter", text: "{\"a\":1}", filter: ".a" } }],
    ["awk", { tool: "command_text", arguments: { operation: "awk_columns", text: "a,b\n", columns: [2, 1], inputDelimiter: "," } }],
    ["sed", { tool: "command_text", arguments: { operation: "sed_replace", text: "one one", pattern: "one", replacement: "two" } }],
    ["awk", { tool: "command_text", arguments: { operation: "awk_stats", text: "a 1\nb 2\nc 3\n", column: 2 } }],
    ["rg", { tool: "command_text", arguments: { operation: "rg_search", text: "one\ntwo\nthree\nfour", pattern: "three", contextBefore: 1, contextAfter: 1 } }],
    ["jq", { tool: "command_text", arguments: { operation: "jq_filter", text: "{\"a\":\"x\"}", filter: ".a", rawOutput: true } }],
  ].filter(([command]) => commandAvailable(command)).map(([, call]) => call);
  const responses = runProtocol(commandCalls);
  for (const [index, call] of commandCalls.entries()) {
    const response = responses[index + 2];
    assert.notEqual(response.result.isError, true, `${call.arguments.operation}: ${response.result.content?.[0]?.text}`);
  }
});

test("category dispatch rejects undeclared fields and jq environment access", () => {
  const responses = runProtocol([
    { tool: "text_transform", arguments: { operation: "slugify", text: "hello", path: "/etc/passwd" } },
    { tool: "command_text", arguments: { operation: "jq_filter", text: "{}", filter: "env" } },
  ]);
  assert.equal(responses[2].result.isError, true);
  assert.match(responses[2].result.content[0].text, /unknown fields/);
  assert.equal(responses[3].result.isError, true);
  assert.match(responses[3].result.content[0].text, /disabled environment or module feature/);
});
