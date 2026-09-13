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
  text_transform: ["case_convert", "slugify", "identifier_normalize", "whitespace_normalize", "unicode_normalize", "punctuation_normalize", "deduplicate", "line_sort", "line_filter", "wrap", "split_join", "template_fill"],
  text_extract: ["emails", "urls", "numbers", "ip_addresses", "datetimes", "code_blocks", "quoted_text"],
  text_analyze: ["statistics", "readability", "keyword_frequency", "similarity", "ngrams", "sentence_split", "token_estimate"],
  text_codec: ["base64", "url", "html", "unicode", "hex", "binary", "jwt_decode"],
  data_convert: ["json_format", "csv_to_json", "json_to_csv", "toml_to_json", "ini_to_json", "xml_to_json", "query_to_json", "json_to_query"],
  text_security: ["digest", "hmac", "checksum", "uuid_validate"],
  text_generate: ["uuid", "password", "lorem"],
  developer_text: ["regex_replace", "diff", "semver"],
  document_process: ["markdown_to_text", "toc", "normalize_fences", "frontmatter_parse"],
  table_transform: ["to_markdown", "sort"],
  command_text: ["rg_search", "jq_filter", "awk_columns", "sed_replace"],
};

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
  ["text_extract", { operation: "emails", text: "a@example.com" }],
  ["text_extract", { operation: "urls", text: "See https://example.com/x." }],
  ["text_extract", { operation: "numbers", text: "1,234.5 and -2" }],
  ["text_extract", { operation: "ip_addresses", text: "10.0.0.1 and 2001:db8::1" }],
  ["text_extract", { operation: "datetimes", text: "2026-04-01T10:30:00Z" }],
  ["text_extract", { operation: "code_blocks", text: "Use `x` or ```js\ny\n```" }],
  ["text_extract", { operation: "quoted_text", text: "She said \"hello\"." }],
  ["text_analyze", { operation: "statistics", text: "One sentence." }],
  ["text_analyze", { operation: "readability", text: "This is one sentence. This is another sentence." }],
  ["text_analyze", { operation: "keyword_frequency", text: "red red blue" }],
  ["text_analyze", { operation: "similarity", text: "red blue", otherText: "red green" }],
  ["text_analyze", { operation: "ngrams", text: "one two three", n: 2 }],
  ["text_analyze", { operation: "sentence_split", text: "One. Two!" }],
  ["text_analyze", { operation: "token_estimate", text: "Estimate me" }],
  ["text_codec", { operation: "base64", text: "hello", action: "encode" }],
  ["text_codec", { operation: "url", text: "a b", action: "encode" }],
  ["text_codec", { operation: "html", text: "<b>", action: "encode" }],
  ["text_codec", { operation: "unicode", text: "caf\u00e9", action: "encode" }],
  ["text_codec", { operation: "hex", text: "hi", action: "encode" }],
  ["text_codec", { operation: "binary", text: "A", action: "encode" }],
  ["text_codec", { operation: "jwt_decode", text: "eyJhbGciOiJub25lIn0.eyJzdWIiOiIxIn0." }],
  ["data_convert", { operation: "json_format", text: "{\"b\":2,\"a\":1}", sortKeys: true }],
  ["data_convert", { operation: "csv_to_json", text: "a,b\n1,2" }],
  ["data_convert", { operation: "json_to_csv", text: "[{\"a\":1,\"b\":2}]" }],
  ["data_convert", { operation: "toml_to_json", text: "name = \"test\"" }],
  ["data_convert", { operation: "ini_to_json", text: "[main]\nname=test" }],
  ["data_convert", { operation: "xml_to_json", text: "<root><item id=\"1\">x</item></root>" }],
  ["data_convert", { operation: "query_to_json", text: "a=1&a=2" }],
  ["data_convert", { operation: "json_to_query", text: "{\"a\":[1,2]}" }],
  ["text_security", { operation: "digest", text: "abc", algorithm: "sha256" }],
  ["text_security", { operation: "hmac", text: "abc", key: "key", algorithm: "sha256" }],
  ["text_security", { operation: "checksum", text: "abc", algorithm: "crc32" }],
  ["text_security", { operation: "uuid_validate", text: "123e4567-e89b-12d3-a456-426614174000" }],
  ["text_generate", { operation: "uuid", count: 2 }],
  ["text_generate", { operation: "password", length: 16 }],
  ["text_generate", { operation: "lorem", paragraphs: 1, sentencesPerParagraph: 1 }],
  ["developer_text", { operation: "regex_replace", text: "a1b2", pattern: "\\d", replacement: "x" }],
  ["developer_text", { operation: "diff", before: "a\n", after: "b\n" }],
  ["developer_text", { operation: "semver", version: "1.2.3", action: "compare", otherVersion: "1.2.4" }],
  ["document_process", { operation: "markdown_to_text", text: "# Hello\n\n**world**" }],
  ["document_process", { operation: "toc", text: "# One\n## Two" }],
  ["document_process", { operation: "normalize_fences", text: "~~~js\nx\n~~~", marker: "```" }],
  ["document_process", { operation: "frontmatter_parse", text: "+++\nname = \"test\"\n+++\nBody" }],
  ["table_transform", { operation: "to_markdown", text: "[{\"a\":1,\"b\":2}]" }],
  ["table_transform", { operation: "sort", text: "[{\"a\":2},{\"a\":1}]", column: "a", numeric: true }],
].map(([tool, args]) => ({ tool, arguments: args }));

test("Text Tools advertises strict category schemas and executes every built-in operation", () => {
  const responses = runProtocol(calls);
  assert.equal(responses[0].result.serverInfo.version, "0.2.0");
  const advertised = new Map(responses[1].result.tools.map((tool) => [tool.name, tool]));
  assert.equal(advertised.size, 15);
  for (const [category, operations] of Object.entries(categoryOperations)) {
    const inputSchema = advertised.get(category).inputSchema;
    assert.equal(inputSchema.type, "object");
    const schemas = inputSchema.oneOf;
    assert.deepEqual(schemas.map((schema) => schema.properties.operation.const), operations);
    assert.ok(schemas.every((schema) => schema.additionalProperties === false));
  }
  for (const [index, call] of calls.entries()) {
    const response = responses[index + 2];
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
