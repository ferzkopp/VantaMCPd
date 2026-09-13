# Text Tools Operations

This document defines the Text Tools 0.2 API before implementation. The module targets small Debian
and Ubuntu nodes, including 32-bit ARM systems with 1 GB RAM. It therefore favors deterministic,
bounded transformations built from maintained Python standard-library modules.

## API Layout

The four 0.1 tools remain available for compatibility:

- `regex_extract`
- `csv_normalize`
- `log_parse_kv`
- `html_extract`

New functionality is grouped into eleven discoverable category tools. Each accepts an `operation`
discriminator and an operation-specific payload. Its MCP `inputSchema` uses JSON Schema `oneOf`, so an
agent sees the fields required by each operation rather than a bag of unrelated optional parameters.

| Tool | Implemented operations |
| --- | --- |
| `text_transform` | `case_convert`, `slugify`, `identifier_normalize`, `whitespace_normalize`, `unicode_normalize`, `punctuation_normalize`, `deduplicate`, `line_sort`, `line_filter`, `wrap`, `split_join`, `template_fill` |
| `text_extract` | `emails`, `urls`, `numbers`, `ip_addresses`, `datetimes`, `code_blocks`, `quoted_text` |
| `text_analyze` | `statistics`, `readability`, `keyword_frequency`, `similarity`, `ngrams`, `sentence_split`, `token_estimate` |
| `text_codec` | `base64`, `url`, `html`, `unicode`, `hex`, `binary`, `jwt_decode` |
| `data_convert` | `json_format`, `csv_to_json`, `json_to_csv`, `toml_to_json`, `ini_to_json`, `xml_to_json`, `query_to_json`, `json_to_query` |
| `text_security` | `digest`, `hmac`, `checksum`, `uuid_validate` |
| `text_generate` | `uuid`, `password`, `lorem` |
| `developer_text` | `regex_replace`, `diff`, `semver` |
| `document_process` | `markdown_to_text`, `toc`, `normalize_fences`, `frontmatter_parse` |
| `table_transform` | `to_markdown`, `sort` |
| `command_text` | `rg_search`, `jq_filter`, `awk_columns`, `sed_replace` |

One MCP call performs one operation on one routed node. Independent calls can be submitted concurrently
to use replicated installations. Category tools keep `tools/list` compact while preserving strict
operation schemas and useful descriptions.

## Resource And Security Policy

- Input text remains capped at 262,144 UTF-8 bytes.
- Collection results remain capped at 1,000 items.
- Regex evaluation and replacement run in killable worker processes with a two-second limit.
- XML parsing rejects DTD and entity declarations and never fetches external resources.
- JWT decoding only exposes header and payload. It does not verify signatures and says so in its result.
- MD5 and SHA-1 are available for compatibility/checksums and are labelled unsuitable for security.
- Password generation uses `secrets`, not `random`.
- Template filling only substitutes values; it never evaluates expressions.
- Command wrappers receive text through stdin and use fixed argv/program shapes. They accept no paths,
  shell fragments, arbitrary flags, caller-supplied awk programs, or caller-supplied sed commands.
- No operation accepts file paths, accesses the network, invokes a shell, or mutates node state.

## Dependency Decision

The Python implementation remains standard-library only. It reuses `base64`, `configparser`, `csv`, `difflib`,
`hashlib`, `hmac`, `html`, `ipaddress`, `json`, `secrets`, `string`, `textwrap`, `tomllib`,
`unicodedata`, `urllib.parse`, `uuid`, `xml.etree.ElementTree`, and `zlib` rather than reimplementing
their formats or primitives. The command wrappers reuse Debian's `ripgrep`, `jq`, `mawk`, and `sed`
packages. VantaMCPd installs declared apt dependencies after confirmation and recorded compatibility
checks, then repeats live command checks before activating the module. Packages are not removed on
module uninstall because they may be shared by other software.

## Deferred Or Excluded

| Requested area | Status | Reason |
| --- | --- | --- |
| Pluralization/singularization | Deferred | Correct language-aware inflection needs a maintained dictionary/package |
| Profanity filtering | Deferred | Requires an explicit language, policy, and maintained word list |
| HTML sanitization | Deferred | Security-sensitive; use a maintained sanitizer such as Bleach after dependency provisioning exists |
| YAML conversion/frontmatter | Deferred | Requires PyYAML or ruamel.yaml; unsafe ad hoc parsing is rejected |
| Markdown to HTML and linting | Deferred | Requires a CommonMark parser and lint rule set |
| Named entities, language detection, sentiment, topics, classification | Deferred | Useful quality requires model data and more CPU/RAM than this module budget |
| Grammar correction, paraphrasing, abstractive summaries | Excluded on current nodes | Requires large language models or external services |
| Extractive summarization | Deferred | Needs a separately specified quality contract and language policy |
| TF-IDF | Deferred | Meaningful IDF needs a corpus rather than one input document |
| Fake identities, addresses, random quotes | Deferred | Locale datasets and provenance policy are required |
| Regex generation | Deferred | Automatic intent-to-regex generation is not deterministic without a constrained DSL |
| JavaScript/CSS/HTML minification and comment stripping | Deferred | Correctness requires language-aware parsers |
| Diff patch application | Deferred | Patch semantics and conflict handling need a dedicated contract |
| TOML/INI/XML generation | Deferred | Generic object mappings are lossy and ambiguous |

## JSON Schema Contract

Every category advertises the following complete schema shape. The `oneOf` array contains one branch
for every operation in the matrix; `properties` and `required` are generated from the same registry
that dispatches the call. Consequently, the live `tools/list` response is the canonical expanded JSON
Schema and cannot drift from the implementation.

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

The server also enforces required and additional properties at dispatch. The compact signatures below
define every expanded branch. `?` means optional and `=` gives the runtime default. Enumerations are
written with `|`. Text arguments are subject to the shared UTF-8 limit even when repeated as
`before`, `after`, `otherText`, or `key`.

## Function Signatures

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
```

### `text_extract`

All operations accept `(text, maxResults?=100[1..1000])`:

```text
emails  urls  numbers  ip_addresses  datetimes  code_blocks  quoted_text
```

### `text_analyze`

```text
statistics(text)
readability(text)
keyword_frequency(text, stopwords?=[], minLength?=3[1..100], maxResults?=25[1..1000])
similarity(text, otherText, metric?=jaccard|levenshtein|sequence)
ngrams(text, n?=2[1..5], maxResults?=100[1..1000])
sentence_split(text, maxResults?=100[1..1000])
token_estimate(text)
```

Readability is an English-oriented heuristic. Token estimation uses four characters per token and is
not a model-specific tokenizer.

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
csv_to_json(text, delimiter?=",")
json_to_csv(text, delimiter?=",")
toml_to_json(text)
ini_to_json(text)
xml_to_json(text)
query_to_json(text, strict?=false)
json_to_query(text)
```

CSV/JSON tables are limited to 1,000 rows and 100 columns and permit scalar cells only. XML is returned
as an explicit `{tag, attributes, text, children}` tree.

### `text_security` And `text_generate`

```text
digest(text, algorithm: md5|sha1|sha256|sha512|blake2b)
hmac(text, key, algorithm: md5|sha1|sha256|sha512)
checksum(text, algorithm: crc32|md5|sha1|sha256|sha512|blake2b)
uuid_validate(text)
uuid(version?=4, count?=1[1..100])
password(length?=24[8..256], symbols?=true)
lorem(paragraphs?=1[1..20], sentencesPerParagraph?=5[1..20])
```

### `developer_text`, `document_process`, And `table_transform`

```text
regex_replace(text, pattern[<=2048], replacement[<=16384], count?=0[0..1000], ignoreCase?=false, multiline?=false)
diff(before, after, fromLabel?="before", toLabel?="after", context?=3[0..100])
semver(version, action?=parse|compare, otherVersion? required for compare)
markdown_to_text(text)
toc(text, minLevel?=1[1..6], maxLevel?=6[1..6])
normalize_fences(text, marker?=```|~~~)
frontmatter_parse(text: JSON or TOML frontmatter plus body)
to_markdown(text: JSON array of flat objects)
sort(text: JSON array of flat objects, column, numeric?=false, descending?=false)
```

### `command_text`

```text
rg_search(text, pattern[1..2048], fixedStrings?=false, ignoreCase?=false, wordRegexp?=false, maxResults?=100[1..1000])
jq_filter(text, filter[1..2048], compact?=false)
awk_columns(text, columns: integer[1..1000][1..100], inputDelimiter?=" ", outputDelimiter?="\\t")
sed_replace(text, pattern[1..2048], replacement[<=16384], global?=true)
```

Each process receives input through stdin, runs for at most five seconds, and returns at most 240,000
bytes. `rg` flags are selected booleans. The awk program is generated only from validated column
numbers. Sed receives exactly one substitution expression without a shell. jq accepts its filter
language, but environment and module features (`env`, `$ENV`, `import`, `include`, `module`, and
`input_filename`) are rejected; the subprocess also gets a minimal environment and neutral working
directory.

## Responses And Errors

Successful calls return both MCP text content and identical `structuredContent`; VantaMCPd exposes the
latter under `output`. Text-producing operations use `{ "text": "..." }`. Collection operations use
`items` plus `count` and, where relevant, `truncated`. Parse and validation failures return MCP
`isError: true` with a bounded diagnostic. No category operation falls back to another node after it
has started.

Example category call:

```json
{
    "moduleId": "text-tools",
    "toolName": "command_text",
    "arguments": {
        "operation": "rg_search",
        "text": "level=info\nlevel=error code=42",
        "pattern": "error|code=[0-9]+",
        "maxResults": 20
    }
}
```