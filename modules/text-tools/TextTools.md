# Text Tools

`text-tools` is a bounded node-side text-processing MCP module. Its core uses only the Python standard
library, its constrained command wrappers reuse Debian packages (`ripgrep`, `jq`, `mawk`, and `sed`),
and a handful of operations use additional Debian-packaged libraries and data files. This guide covers
installation, routing, every operation and argument, security boundaries, and design decisions.

![Text-tools automatic routing and incident report](text-tools-sample.png)

*A release incident investigated with parallel, automatically routed `text-tools` operations.*

The module accepts caller-provided text and, when shared storage is enabled, opaque artifact IDs for
its two streamed CSV operations. It does not accept node paths, fetch URLs, run a shell, or execute
caller-supplied awk/sed programs.

It uses replicated deployment, so the cluster may have any number of interchangeable instances and
targetless calls are routed round-robin. Its on-demand runtime starts a fresh Python MCP process over
SSH stdio for each discovery or tool call and closes it afterward. Nothing runs continuously, and the
next call works normally after either VantaMCPd disconnects or the node reboots.

## Quickstart

The shortest path from an available worker node to a verified first operation is:

1. Check compatibility and free disk:

	> Check whether text-tools is compatible with worker-a.

2. Install it on one or more explicit nodes. Two installations give you interchangeable replicas:

	> Install text-tools on worker-a and worker-b.

3. Confirm the operation. Installation performs live preflight, installs any missing declared apt
	packages, repeats preflight, verifies staged file hashes, runs the module self-test, activates the
	version, and writes a receipt. It returns when the nodes are ready; there is no background job.

4. Verify the installed surface:

	> List the tools provided by text-tools.

	Confirm that the response lists the twelve category tools below.

5. Run a first operation. Neither the module name nor a node is needed:

	> Pull the numeric IDs out of `item=12 item=37`.

Each target node needs Debian or Ubuntu on `armhf`, `arm64`, or `amd64`, at least 256 MB RAM, and 40 MB
free on the root filesystem. No secondary storage is required and no data is retained between calls.

## Install

The module declares `bash`, Python 3, `rg`, `jq`, `awk`, and `sed`; VantaMCPd installs the missing
declared apt packages (`python3`, `ripgrep`, `jq`, `mawk`, `sed`) during preflight. It also declares
`python3-yaml`, `python3-markdown`, `python3-inflect`, `python3-tomli-w`, and `wamerican`, which unlock
seven optional operations; everything else works without them. Shared apt packages are never removed
during module uninstall.

The implementation favors maintained standard-library parsers and primitives over custom format code.
The command wrappers use fixed invocations of Debian's `ripgrep`, `jq`, `mawk`, and `sed` packages.

| Package | Operations | Note |
| --- | --- | --- |
| `python3-yaml` | `yaml_to_json`, `json_to_yaml`, YAML `frontmatter_parse` | Often already present on Armbian images |
| `python3-markdown` | `markdown_to_html` | |
| `python3-inflect` | `pluralize` | |
| `python3-tomli-w` | `json_to_toml` | |
| `wamerican` | `passphrase` | Supplies `/usr/share/dict/words` |
| `tzdata` | `convert_timezone`, zone-aware `format` and `now` | Standard on Debian |

These packages are installed by default when available. If one is absent, only the operations that
need it fail, with an error naming the exact package. Packages remain after module uninstall because
other software may share them.

In Copilot Chat Agent mode, check placement before installing:

> Check whether text-tools is compatible with worker-a.

Then install it on explicit nodes:

> Install text-tools on worker-a.

To provide two interchangeable instances:

> Install text-tools on worker-a and worker-b.

The corresponding MCP arguments are:

```json
{
	"moduleId": "text-tools",
	"targets": ["worker-a", "worker-b"],
	"confirm": true
}
```

Installation requires approval and an explicit target list; `all` is rejected. Because deployment is
replicated, adding or removing a replica needs no change to the others.

## Tools

Every capability is reached through twelve category tools holding 113 operations. Each takes an
`operation` discriminator and that operation's own fields; the advertised JSON Schema is a `oneOf` over
the operations, so the agent sees exactly what each one requires.

| Tool | Implemented operations |
| --- | --- |
| `text_transform` | `case_convert`, `slugify`, `identifier_normalize`, `whitespace_normalize`, `unicode_normalize`, `punctuation_normalize`, `deduplicate`, `line_sort`, `line_filter`, `wrap`, `split_join`, `template_fill`, `line_slice`, `replace_literal`, `truncate`, `pad_align`, `ascii_fold`, `number_format`, `bytes_humanize`, `pluralize` |
| `text_extract` | `emails`, `urls`, `numbers`, `ip_addresses`, `datetimes`, `code_blocks`, `quoted_text`, `uuids`, `hashes`, `semvers` |
| `text_analyze` | `statistics`, `readability`, `keyword_frequency`, `similarity`, `ngrams`, `sentence_split`, `token_estimate`, `text_inspect`, `duplicate_lines`, `chunk`, `tfidf` |
| `text_codec` | `base64`, `url`, `html`, `unicode`, `hex`, `binary`, `jwt_decode` |
| `data_convert` | `json_format`, `csv_normalize`, `csv_to_json`, `csv_normalize_artifact`, `csv_to_json_artifact`, `json_to_csv`, `kv_to_json`, `html_to_json`, `jsonl_to_json`, `json_to_jsonl`, `json_flatten`, `json_unflatten`, `json_diff`, `json_merge`, `json_schema_infer`, `env_to_json`, `yaml_to_json`, `json_to_yaml`, `toml_to_json`, `json_to_toml`, `ini_to_json`, `xml_to_json`, `query_to_json`, `json_to_query` |
| `text_security` | `digest`, `hmac`, `checksum`, `uuid_validate`, `secret_scan`, `redact`, `password_strength` |
| `text_generate` | `uuid`, `password`, `lorem`, `passphrase` |
| `developer_text` | `regex_extract`, `regex_replace`, `diff`, `semver`, `regex_test`, `strip_comments` |
| `document_process` | `markdown_to_text`, `toc`, `normalize_fences`, `frontmatter_parse`, `split_sections`, `tables_extract`, `links_extract`, `heading_shift`, `markdown_to_html` |
| `table_transform` | `to_markdown`, `sort` |
| `datetime_text` | `parse`, `format`, `convert_timezone`, `difference`, `now`, `duration_humanize`, `duration_parse` |
| `command_text` | `rg_search`, `jq_filter`, `awk_columns`, `awk_stats`, `sed_replace` |

List the tools advertised by the installed module:

> List the tools provided by text-tools.

To ask a specific replica instead, name it:

> List the tools provided by text-tools on worker-a.

Most requests need neither the module name nor a node. The manifest declares the module's capabilities,
VantaMCPd puts them in the instructions and proxy-tool descriptions it sends the agent, and routing
picks a reachable replica:

> Check this config for anything that looks like a credential before I paste it.

> Redact the email addresses and IPs from this log excerpt.

> These two lines look identical but don't compare equal - tell me why.

> Convert this YAML to JSON, then show me what changed against the previous version.

> What is `2026-09-13T12:00:00Z` in Europe/Berlin, and how long ago was that?

> Split this Markdown into sections at level 2 and chunk each one to 2000 characters.

> Does `^\d{3}-\d{4}$` match `555-1234` and `5551234`?

> Pull the numeric IDs out of `item=12 item=37`.

> Normalize `name;score\nAda;10\nLinus;9` as CSV.

> Sort the JSON table `[{"name":"b","score":2},{"name":"a","score":10}]` by score numerically.

Name the module when you want to force the cluster rather than let the agent answer locally:

> Use text-tools to search `level=info\nlevel=error code=42` with ripgrep for `error|code=[0-9]+`, with one line of context.

Name a node as well only to reproduce a result on a particular replica or to isolate a suspected node
problem.

### Routing and responses

The underlying generic proxy call for the regex example is:

```json
{
	"moduleId": "text-tools",
	"toolName": "developer_text",
	"arguments": {
		"operation": "regex_extract",
		"text": "item=12 item=37",
		"pattern": "item=(\\d+)"
	}
}
```

With no `target`, successive calls round-robin over reachable nodes where `text-tools` is installed.
Set `"target": "worker-a"` to pin a call. The proxy response identifies the selected node and module
version, then places the tool's structured result in `output`:

```json
{
	"ok": true,
	"node": "worker-a",
	"moduleId": "text-tools",
	"moduleVersion": "0.5.3",
	"toolName": "developer_text",
	"deployment": { "mode": "replicated", "routing": "round-robin" },
	"selection": "automatic",
	"output": {
		"matches": [
			{ "match": "item=12", "groups": ["12"], "namedGroups": {}, "start": 0, "end": 7 },
			{ "match": "item=37", "groups": ["37"], "namedGroups": {}, "start": 8, "end": 15 }
		],
		"count": 2,
		"truncated": false
	}
}
```

The proxy prefers the module's `structuredContent`, so the same result is not repeated as escaped JSON
inside `content[].text`. For text-only modules, a sole JSON text item is parsed when possible; plain text
and multi-part MCP content are preserved. Text-producing operations use `{ "text": "..." }`;
collection operations use `items`, `count`, and, where relevant, `truncated`. Module-reported parse,
validation, and dependency errors return `ok: false` with a bounded diagnostic. No operation silently
replays on another node after it starts.

## JSON Schema Contract

Every category advertises a strict `oneOf` branch for each operation. Properties, required fields, and
dispatch are generated from the same registry, so the live `tools/list` response is the canonical
expanded schema and cannot drift from the implementation.

```json
{
	"type": "object",
	"oneOf": [
		{
			"type": "object",
			"title": "Operation description.",
			"properties": {
				"operation": { "const": "operation_name" },
				"text": {
					"type": "string",
					"description": "UTF-8 text, at most 262144 bytes."
				}
			},
			"required": ["operation", "text"],
			"additionalProperties": false
		}
	]
}
```

The server independently enforces required and additional properties at dispatch. In the compact
signatures below, `?` means optional and `=` gives the runtime default. Enumerations use `|`. Text
arguments share the UTF-8 limit even when named `before`, `after`, `otherText`, or `key`.

## Operation Reference

### `text_transform`

```text
case_convert(text, style: camel|snake|kebab|pascal|constant|title|lower|upper)
slugify(text, separator?="-", lowercase?=true)
identifier_normalize(text, style?=snake|camel|pascal|constant, prefix?="_")
whitespace_normalize(text, mode: trim|collapse|dedent|indent, indent?="  ")
unicode_normalize(text, form: NFC|NFD|NFKC|NFKD)
punctuation_normalize(text)
deduplicate(text, unit?=lines|paragraphs, caseSensitive?=true)
line_sort(text, mode?=lexical|natural|numeric, descending?=false, unique?=false)
line_filter(text, pattern[1..2048], action?=include|exclude, regex?=false, ignoreCase?=false)
wrap(text, width?=80[10..1000], initialIndent?="", subsequentIndent?="")
split_join(text, inputDelimiter, outputDelimiter, trimItems?=true, omitEmpty?=false)
template_fill(text, values: object[<=100], missing?=error|keep|empty)
line_slice(text, start?=1, end?=last, tail?, numbered?=false)
replace_literal(text, search[1..16384], replacement?="", count?=0[0..1000], ignoreCase?=false)
truncate(text, maxCharacters?=280[1..100000], boundary?=word|character, suffix?="\u2026")
pad_align(text, width?=20[1..1000], align?=left|right|center, fill?=" ")
ascii_fold(text, asciiOnly?=false)
number_format(value, decimals?=0|2, groupSeparator?=",", decimalSeparator?=".")
bytes_humanize(bytes, standard?=binary|decimal, decimals?=1[0..3])
pluralize(text[<=200], action?=plural|singular, count?)
```

`line_slice` takes either a `start`/`end` range or `tail`, never both. `replace_literal` does not
interpret regular-expression syntax in `search` or backslashes in `replacement`. `ascii_fold` maps
ligatures NFKD leaves intact (such as ss, ae, oe, and thorn). `pluralize` needs `python3-inflect`.

### `text_extract`

All operations accept `(text, maxResults?=100[1..1000])`:

```text
emails  urls  numbers  ip_addresses  datetimes  code_blocks  quoted_text  uuids  hashes  semvers
```

`uuids` returns `{value, version}` for identifiers that parse. `hashes` recognizes 32, 40, 64, 96,
and 128 hex-digit runs and reports likely algorithms. `semvers` accepts and strips an optional `v`.

### `text_analyze`

```text
statistics(text)
readability(text)
keyword_frequency(text, stopwords?=[], minLength?=3[1..100], maxResults?=25[1..1000])
similarity(text, otherText, metric?=jaccard|levenshtein|sequence)
ngrams(text, n?=2[1..5], maxResults?=100[1..1000])
sentence_split(text, maxResults?=100[1..1000])
token_estimate(text)
text_inspect(text, maxResults?=20[1..1000])
duplicate_lines(text, caseSensitive?=true, ignoreWhitespace?=false, maxResults?=100[1..1000])
chunk(text, maxCharacters?=2000[100..20000], overlapCharacters?=0[0..2000], boundary?=paragraph|sentence|line|character, maxResults?=1000)
tfidf(documents: string[2..50], stopwords?=[], minLength?=3[1..100], maxResults?=10[1..100])
```

Readability is an English-oriented heuristic. Token estimation uses four characters per token and is
not model-specific. `text_inspect` reports line endings, BOMs, mixed indentation, trailing whitespace,
and control, zero-width, and bidi character offsets. `chunk` never exceeds `maxCharacters`; oversized
units are split on characters. `tfidf` uses its document set as the corpus with smoothed IDF.

### `text_codec`

```text
base64(text, action?=encode|decode)
url(text, action?=encode|decode, safe?="")
html(text, action?=encode|decode)
unicode(text, action?=encode|decode)
hex(text, action?=encode|decode)
binary(text, action?=encode|decode)
jwt_decode(text)
```

### `data_convert`

```text
json_format(text, indent?=2[0..8], compact?=false, sortKeys?=false)
csv_normalize(text, delimiter?=sniffed from ",;\t|")
csv_to_json(text, delimiter?=",")
csv_normalize_artifact(artifactId, delimiter?=sniffed, maxRows?=100000, outputBudgetBytes?=33554432, retentionDays?=7)
csv_to_json_artifact(artifactId, delimiter?=",", maxRows?=100000, outputBudgetBytes?=33554432, retentionDays?=7)
json_to_csv(text, delimiter?=",")
kv_to_json(text, maxResults?=100[1..1000])
html_to_json(text, maxResults?=1000[1..1000])
jsonl_to_json(text)
json_to_jsonl(text)
json_flatten(text, separator?=".")
json_unflatten(text, separator?=".")
json_diff(text, otherText)
json_merge(text, otherText, arrays?=replace|concat)
json_schema_infer(text, samplesAreArray?=false)
env_to_json(text, strict?=false)
yaml_to_json(text, allDocuments?=false)
json_to_yaml(text, sortKeys?=false, indent?=2[2..8])
toml_to_json(text)
json_to_toml(text)
ini_to_json(text)
json_to_ini(text)
xml_to_json(text)
query_to_json(text, strict?=false)
json_to_query(text)
```

CSV and JSON tables are limited to 1,000 rows and 100 columns and permit scalar cells only.
`csv_normalize` needs no header and pads short records. `kv_to_json` returns one record per non-blank
line. `html_to_json` renders and fetches nothing and drops `script` and `style` content. XML becomes an
explicit `{tag, attributes, text, children}` tree.

`json_flatten` uses the separator for object keys and `[n]` for arrays; `json_unflatten` reverses it.
`json_diff` reports `added`, `removed`, and `changed` paths. `json_to_ini` requires a
`{section: {key: scalar}}` shape. `json_to_toml` needs `python3-tomli-w`; YAML conversion needs
`python3-yaml`.

The artifact CSV operations accept only an opaque artifact ID. They verify source size and SHA-256,
stream up to 32 MiB and 1,000,000 rows, and return a new artifact instead of embedding transformed data.

### `text_security` and `text_generate`

```text
digest(text, algorithm: md5|sha1|sha256|sha512|blake2b)
hmac(text, key, algorithm: md5|sha1|sha256|sha512)
checksum(text, algorithm: crc32|md5|sha1|sha256|sha512|blake2b)
uuid_validate(text)
secret_scan(text, maxResults?=100[1..1000], includeHighEntropy?=true, minEntropy?=4.0[0..8])
redact(text, categories?=[emails, ip_addresses, urls, card_numbers, secrets])
password_strength(text[<=4096])
uuid(version?=4, count?=1[1..100])
password(length?=24[8..256], symbols?=true)
passphrase(words?=5[3..20], separator?="-", capitalize?=false, minWordLength?=4, maxWordLength?=9)
lorem(paragraphs?=1[1..20], sentencesPerParagraph?=5[1..20])
```

`secret_scan` detects known credential shapes and optional high-entropy strings, returning only masked
previews. `redact` gives repeated values stable placeholders; card numbers must pass Luhn validation.
`passphrase` needs `wamerican`.

### `developer_text`, `document_process`, and `table_transform`

```text
regex_extract(text, pattern[1..2048], maxMatches?=100[1..1000], ignoreCase?=false, multiline?=false, dotAll?=false)
regex_replace(text, pattern[<=2048], replacement[<=16384], count?=0[0..1000], ignoreCase?=false, multiline?=false, dotAll?=false)
regex_test(pattern[1..2048], samples: string[1..100], ignoreCase?=false, multiline?=false, dotAll?=false)
strip_comments(text, language?=python, docstrings?=false)
diff(before, after, fromLabel?="before", toLabel?="after", context?=3[0..100])
semver(version, action?=parse|compare, otherVersion? required for compare)
markdown_to_text(text)
toc(text, minLevel?=1[1..6], maxLevel?=6[1..6])
normalize_fences(text, marker?=```|~~~)
frontmatter_parse(text: JSON, TOML, or YAML frontmatter plus body)
split_sections(text, level?=2[1..6], maxResults?=100[1..1000])
tables_extract(text, maxResults?=20[1..1000])
links_extract(text, maxResults?=100[1..1000])
heading_shift(text, by: integer[-5..5])
markdown_to_html(text, extensions?=true)
to_markdown(text: JSON array of flat objects, or CSV; format?=json|csv, delimiter?=",", align?=default|left|right|center)
sort(text: JSON array of flat objects, column, numeric?=false, descending?=false)
```

`regex_test` returns `valid: false` with a compiler message rather than raising. `strip_comments` uses
Python's tokenizer, so `#` inside a string is preserved. `split_sections` includes each heading path.
`markdown_to_html` needs `python3-markdown` and does not sanitize its output.

### `datetime_text`

```text
parse(text, assumeTimezone?, inputFormat?, epochUnit?=auto|seconds|milliseconds)
format(text, format?="%Y-%m-%d %H:%M:%S %Z", timezone?, assumeTimezone?, inputFormat?, epochUnit?)
convert_timezone(text, toTimezone, assumeTimezone?, inputFormat?, epochUnit?)
difference(text, otherText, unit?=seconds|minutes|hours|days|weeks, assumeTimezone?, inputFormat?)
now(timezone?)
duration_humanize(seconds, parts?=2[1..4])
duration_parse(text[<=200])
```

Timestamps may be ISO-8601, epoch seconds or milliseconds, or RFC 2822; other forms need
`inputFormat`. Offset-free values default to UTC unless `assumeTimezone` is set. IANA zone names are
resolved through `zoneinfo`, and `strftime` directives are allow-listed.

### `command_text`

```text
rg_search(text, pattern[1..2048], fixedStrings?=false, ignoreCase?=false, wordRegexp?=false, contextBefore?=0[0..20], contextAfter?=0[0..20], maxResults?=100[1..1000])
jq_filter(text, filter[1..2048], compact?=false, rawOutput?=false, slurp?=false)
awk_columns(text, columns: integer[1..1000][1..100], inputDelimiter?=" ", outputDelimiter?="\t")
awk_stats(text, column[1..1000], inputDelimiter?=" ", skipLines?=0[0..100])
sed_replace(text, pattern[1..2048], replacement[<=16384], global?=true)
```

Each command receives input through stdin, runs for at most five seconds, and returns at most 240,000
bytes. The module generates awk programs only from validated column numbers and builds a fixed sed
substitution. `jq` environment and module features are rejected, and its subprocess receives a minimal
environment and neutral working directory.

## Example Parallel Workflow

Each call runs wholly on one node, so independent calls can use the replicas concurrently without the
prompt naming either the module or a node:

> Using text-tools with automatic routing, run these independent operations in parallel and report the
> node for each result: extract `OPS-142` and `OPS-207` from
> `release=2026.09 tickets=OPS-142,OPS-207`; normalize
> `node;status\nworker-a;ready\nworker-b;ready` as CSV; parse
> `level=info module=text-tools replicas:2 routing=round-robin`; and extract content from
> `<h2>Deployment report</h2><p>Two replicas ready.</p>`.

With two reachable installations, the four targetless calls are assigned two per node. Results can
finish in any order. A failed in-progress call is returned as a failure rather than silently replayed on
another node.

## Limits and Safety

Input text, pattern length, result counts, and total MCP output bytes are bounded: 256 KiB in and out,
a 10-second startup budget, a 30-second call budget, and at most 1,000 collection results. Regular
expressions are evaluated in a separate process with a two-second limit, so a catastrophically
backtracking pattern is terminated rather than allowed to consume the node.

The service validates every argument, rejects undeclared fields, and exposes no paths, URLs, or shell.
The command wrappers pass caller text on stdin with fixed argument shapes; they never accept
caller-supplied awk or sed programs, and `jq` runs without environment access. YAML is read with
`safe_load`, XML rejects DTD and entity declarations, and `strftime` patterns are checked against an
allow-list before use. `secret_scan` reports a masked preview and never echoes the value it matched.
`markdown_to_html` deliberately does not sanitize and says so in its result. The module makes no changes
to node state outside explicitly requested shared-artifact outputs.

JWT decoding exposes the header and payload without verifying the signature and marks the result as
unverified. MD5 and SHA-1 remain available for compatibility and are labelled unsuitable for security.
Password and passphrase generation use `secrets`, not `random`, and template filling substitutes values
without evaluating expressions.

Each call validates the remote installation receipt and active version, starts a fresh Python MCP
process over SSH stdio, and invokes only a tool returned by `tools/list`.

## Deferred or Excluded

These areas were evaluated against the module's resource and security constraints and intentionally
left out. The reasons make the decisions explicit and revisitable.

| Area | Status | Reason |
| --- | --- | --- |
| HTML sanitization | Deferred | `python3-bleach` is packaged but archived upstream, and owning a custom allow-list sanitizer would create a security-critical maintenance burden |
| Extractive summarization | Deferred | The calling language model produces better summaries than a deterministic sentence-scoring heuristic |
| Diff patch application | Deferred | No standard-library applier exists, and `patch(1)` requires real files, conflicting with the no-path contract |
| JavaScript, CSS, and HTML minification | Deferred | Correctness requires language-aware parsers; Python comment stripping instead uses the real tokenizer |
| XML generation | Deferred | Generic object-to-XML mappings are ambiguous, unlike the supported INI and TOML subsets |
| Regex generation | Excluded | Intent-to-regex is not deterministic; `regex_test` provides the practical validation workflow |
| Markdown linting | Deferred | Requires an explicit rule set and quality contract |
| Profanity filtering | Deferred | Requires an explicit language, policy, and maintained word list |
| Named entities, language detection, sentiment, topics, and classification | Deferred | Useful quality requires model data and more CPU and RAM than the module budget |
| Grammar correction, paraphrasing, and abstractive summaries | Excluded | Requires large language models or external services |
| Fake identities, addresses, and random quotes | Deferred | Requires locale datasets and a provenance policy |

## Data Lifecycle

The module keeps no state: every call is independent and nothing is written outside the installation
directory. Uninstall the payload and receipt with:

> Uninstall text-tools from worker-a.

Uninstallation requires approval and an explicit target. It validates the receipt before removing the
active payload and receipt, and repeating it after removal is safe. Shared apt packages installed during
preflight are deliberately left in place, because other software on the node may depend on them.

## Troubleshooting

| Symptom | Check or action |
| --- | --- |
| Preflight reports missing commands | Let VantaMCPd install the declared apt packages, or install `ripgrep`, `jq`, `mawk`, and `sed` with `cluster_packages` first |
| `... requires the <package> package` | That operation is optional; install the named package with `cluster_packages` and retry |
| `unknown tool: <name>` | Every capability is an `operation` inside one of the twelve category tools; call `cluster_list_module_tools` to see the current surface |
| A call fails with a receipt or version error | Reinstall on that node; the receipt is validated on every call, so a partial installation fails closed |
| Targetless calls all land on one node | Only one installation is reachable; check `cluster_list_modules` for node coverage and connectivity |
| A regex call reports exceeding two seconds | Simplify the pattern; nested quantifiers over long input are terminated rather than allowed to run |
| Output is reported as truncated | Lower `maxMatches` or split the input; the module caps result counts and total output bytes |
| Dashboard shows an old module version | Run `npm run build`, restart the local VantaMCPd MCP server, then refresh the dashboard |

## Local Development

Run the standard-library smoke test from the module directory:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 server.py --self-test
```

Run the complete stdio protocol matrix from the repository root:

```bash
node --test test/text-tools.test.mjs
```