# Text Tools Operations

This document defines the Text Tools API. The module targets small Debian and Ubuntu nodes, including
32-bit ARM systems with 1 GB RAM. It therefore favors deterministic, bounded transformations built from
maintained Python standard-library modules.

## API Layout

All functionality is grouped into twelve discoverable category tools. Each accepts an `operation`
discriminator and an operation-specific payload. Its MCP `inputSchema` uses JSON Schema `oneOf`, so an
agent sees the fields required by each operation rather than a bag of unrelated optional parameters.
There is exactly one way to reach each capability.

| Tool | Implemented operations |
| --- | --- |
| `text_transform` | `case_convert`, `slugify`, `identifier_normalize`, `whitespace_normalize`, `unicode_normalize`, `punctuation_normalize`, `deduplicate`, `line_sort`, `line_filter`, `wrap`, `split_join`, `template_fill`, `line_slice`, `replace_literal`, `truncate`, `pad_align`, `ascii_fold`, `number_format`, `bytes_humanize`, `pluralize` |
| `text_extract` | `emails`, `urls`, `numbers`, `ip_addresses`, `datetimes`, `code_blocks`, `quoted_text`, `uuids`, `hashes`, `semvers` |
| `text_analyze` | `statistics`, `readability`, `keyword_frequency`, `similarity`, `ngrams`, `sentence_split`, `token_estimate`, `text_inspect`, `duplicate_lines`, `chunk`, `tfidf` |
| `text_codec` | `base64`, `url`, `html`, `unicode`, `hex`, `binary`, `jwt_decode` |
| `data_convert` | `json_format`, `csv_normalize`, `csv_to_json`, `json_to_csv`, `kv_to_json`, `html_to_json`, `jsonl_to_json`, `json_to_jsonl`, `json_flatten`, `json_unflatten`, `json_diff`, `json_merge`, `json_schema_infer`, `env_to_json`, `yaml_to_json`, `json_to_yaml`, `toml_to_json`, `json_to_toml`, `ini_to_json`, `json_to_ini`, `xml_to_json`, `query_to_json`, `json_to_query` |
| `text_security` | `digest`, `hmac`, `checksum`, `uuid_validate`, `secret_scan`, `redact`, `password_strength` |
| `text_generate` | `uuid`, `password`, `lorem`, `passphrase` |
| `developer_text` | `regex_extract`, `regex_replace`, `diff`, `semver`, `regex_test`, `strip_comments` |
| `document_process` | `markdown_to_text`, `toc`, `normalize_fences`, `frontmatter_parse`, `split_sections`, `tables_extract`, `links_extract`, `heading_shift`, `markdown_to_html` |
| `table_transform` | `to_markdown`, `sort` |
| `datetime_text` | `parse`, `format`, `convert_timezone`, `difference`, `now`, `duration_humanize`, `duration_parse` |
| `command_text` | `rg_search`, `jq_filter`, `awk_columns`, `awk_stats`, `sed_replace` |

One MCP call performs one operation on one routed node. Independent calls can be submitted concurrently
to use replicated installations. Category tools keep `tools/list` compact while preserving strict
operation schemas and useful descriptions.

## Resource And Security Policy

- Input text remains capped at 262,144 UTF-8 bytes.
- Collection results remain capped at 1,000 items.
- Regex evaluation, replacement, and testing run in killable worker processes with a two-second limit.
- XML parsing rejects DTD and entity declarations and never fetches external resources.
- YAML is parsed with `safe_load` and written with `safe_dump`; arbitrary object construction is refused.
- JWT decoding only exposes header and payload. It does not verify signatures and says so in its result.
- MD5 and SHA-1 are available for compatibility/checksums and are labelled unsuitable for security.
- Password and passphrase generation uses `secrets`, not `random`.
- `secret_scan` reports type, line, column, and a masked preview; it never echoes the matched secret.
- `markdown_to_html` does not sanitize. Raw HTML in the source passes through and the result carries a
  warning when that happens; never inject it into a browser without a maintained sanitizer.
- `strftime` patterns are checked against an allow-list of directives before reaching the C library.
- Template filling only substitutes values; it never evaluates expressions.
- Command wrappers receive text through stdin and use fixed argv/program shapes. They accept no paths,
  shell fragments, arbitrary flags, caller-supplied awk programs, or caller-supplied sed commands.
- No operation accepts file paths, accesses the network, invokes a shell, or mutates node state.

## Dependency Decision

The core of the module is standard-library only. It reuses `base64`, `configparser`, `csv`, `datetime`,
`difflib`, `email.utils`, `hashlib`, `hmac`, `html`, `ipaddress`, `json`, `secrets`, `string`,
`textwrap`, `tokenize`, `tomllib`, `unicodedata`, `urllib.parse`, `uuid`, `xml.etree.ElementTree`,
`zlib`, and `zoneinfo` rather than reimplementing their formats or primitives. The command wrappers
reuse Debian's `ripgrep`, `jq`, `mawk`, and `sed` packages.

Seven operations need a Debian-packaged Python library or data file. They are **optional**: the module
installs and every other operation works without them, and a call that needs a missing one fails with a
message naming the exact package.

| Package | Operations | Note |
| --- | --- | --- |
| `python3-yaml` | `yaml_to_json`, `json_to_yaml`, YAML `frontmatter_parse` | Already present on Armbian images |
| `python3-markdown` | `markdown_to_html` | |
| `python3-inflect` | `pluralize` | |
| `python3-tomli-w` | `json_to_toml` | |
| `wamerican` | `passphrase` | Supplies `/usr/share/dict/words` |
| `tzdata` | `convert_timezone`, zone-aware `format`/`now` | Standard on Debian |

VantaMCPd installs declared apt dependencies after confirmation and recorded compatibility checks, then
repeats live command checks before activating the module. Packages are not removed on module uninstall
because they may be shared by other software.

## Deferred Or Excluded

These areas were evaluated against the module's constraints and left out. Each entry records why, so
the decision can be revisited when a constraint changes.

| Requested area | Status | Reason |
| --- | --- | --- |
| HTML sanitization | Deferred | `python3-bleach` is packaged but archived upstream since 2023, and its successor is not in bookworm. Writing our own allow-list sanitizer would mean owning a security-critical component on a 1 GB board |
| Extractive summarization | Deferred | Deterministic sentence scoring is easy, but the caller is a language model that summarizes far better than any heuristic shipped here |
| Diff patch application | Deferred | There is no standard-library applier, and `patch(1)` needs real files, which would break the module's no-filesystem guarantee |
| JavaScript/CSS/HTML minification | Deferred | Correctness requires language-aware parsers. Python comment stripping is implemented instead, using the real tokenizer |
| XML generation | Deferred | Generic object-to-XML mappings are genuinely ambiguous, unlike the INI and TOML subsets now supported |
| Regex generation | Excluded | Intent-to-regex is not deterministic. `regex_test` delivers the practical benefit instead |
| Markdown linting | Deferred | Requires a rule set and a quality contract of its own |
| Profanity filtering | Deferred | Requires an explicit language, policy, and maintained word list |
| Named entities, language detection, sentiment, topics, classification | Deferred | Useful quality requires model data and more CPU/RAM than this module budget |
| Grammar correction, paraphrasing, abstractive summaries | Excluded on current nodes | Requires large language models or external services |
| Fake identities, addresses, random quotes | Deferred | Locale datasets and provenance policy are required |

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
line_slice(text, start?=1, end?=last, tail?, numbered?=false)
replace_literal(text, search[1..16384], replacement?="", count?=0[0..1000], ignoreCase?=false)
truncate(text, maxCharacters?=280[1..100000], boundary?=word|character, suffix?="\u2026")
pad_align(text, width?=20[1..1000], align?=left|right|center, fill?=" ")
ascii_fold(text, asciiOnly?=false)
number_format(value, decimals?=0|2, groupSeparator?=",", decimalSeparator?=".")
bytes_humanize(bytes, standard?=binary|decimal, decimals?=1[0..3])
pluralize(text[<=200], action?=plural|singular, count?)
```

`line_slice` takes either a `start`/`end` range or `tail`, never both. `replace_literal` never
interprets regular-expression syntax in `search` or backslashes in `replacement`. `ascii_fold` maps
ligatures NFKD leaves intact (ß, æ, ø, ł, þ). `pluralize` needs `python3-inflect`.

### `text_extract`

All operations accept `(text, maxResults?=100[1..1000])`:

```text
emails  urls  numbers  ip_addresses  datetimes  code_blocks  quoted_text  uuids  hashes  semvers
```

`uuids` returns `{value, version}` for identifiers that actually parse. `hashes` returns
`{value, bits, likelyAlgorithm}` for 32, 40, 64, 96, and 128 hex-digit runs. `semvers` accepts an
optional `v` prefix and strips it.

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
not a model-specific tokenizer. `text_inspect` reports line-ending style, BOM, indentation mix,
trailing whitespace, and the offsets of control, zero-width, and bidi characters. `chunk` never emits a
piece larger than `maxCharacters`: a single oversized unit is split on characters. `tfidf` takes the
document set as its corpus and uses smoothed IDF, `ln(N / (1 + df)) + 1`.

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

CSV/JSON tables are limited to 1,000 rows and 100 columns and permit scalar cells only. `csv_normalize`
needs no header row and pads short records to the widest row. `kv_to_json` returns one record per
non-blank line as `{line, fields, raw}`. `html_to_json` returns `{text, headings, links}` using a
conservative parser that renders nothing, fetches nothing, and drops `script`/`style` content. XML is
returned as an explicit `{tag, attributes, text, children}` tree.

`json_flatten` writes object keys with the separator and array elements as `[n]`, and `json_unflatten`
is its inverse. `json_diff` returns `{path, change, before?, after?}` entries where `change` is `added`,
`removed`, or `changed`. `json_to_ini` requires a `{section: {key: scalar}}` shape; `json_to_toml` needs
`python3-tomli-w`; the YAML operations need `python3-yaml`.

### `text_security` And `text_generate`

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

`secret_scan` matches known credential shapes (private-key blocks, AWS/GitHub/Slack/Google keys, JWTs,
bearer tokens, credentials in URLs, `key = value` assignments) and, optionally, high-entropy strings. It
reports `{type, line, column, length, preview}` with the value masked. `redact` replaces each distinct
value with a stable `[LABEL_n]` placeholder (`EMAIL`, `IP`, `URL`, `CARD`, `SECRET`), so repeated values
stay comparable and the output remains diffable; card numbers must pass a Luhn check before being
treated as such. `passphrase` needs `wamerican`.

### `developer_text`, `document_process`, And `table_transform`

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

`regex_test` reports `valid: false` with the compiler message rather than raising, so an agent can
iterate on a pattern. `strip_comments` uses Python's own tokenizer, so a `#` inside a string literal is
never mistaken for a comment. `split_sections` returns each section with its heading path, making it a
natural pre-step for `text_analyze.chunk`. `markdown_to_html` needs `python3-markdown` and does not
sanitize.

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

Timestamps are accepted as ISO-8601 (including a trailing `Z`), epoch seconds or milliseconds, or
RFC 2822; anything else needs an explicit `inputFormat`. A value without an offset is treated as UTC
unless `assumeTimezone` is given. Zone names are IANA identifiers resolved through `zoneinfo`.
`strftime` patterns are validated against an allow-list before use.

### `command_text`

```text
rg_search(text, pattern[1..2048], fixedStrings?=false, ignoreCase?=false, wordRegexp?=false, contextBefore?=0[0..20], contextAfter?=0[0..20], maxResults?=100[1..1000])
jq_filter(text, filter[1..2048], compact?=false, rawOutput?=false, slurp?=false)
awk_columns(text, columns: integer[1..1000][1..100], inputDelimiter?=" ", outputDelimiter?="\\t")
awk_stats(text, column[1..1000], inputDelimiter?=" ", skipLines?=0[0..100])
sed_replace(text, pattern[1..2048], replacement[<=16384], global?=true)
```

Each process receives input through stdin, runs for at most five seconds, and returns at most 240,000
bytes. `rg` flags are selected booleans; requesting context adds a `lines` array holding matches and
their surrounding lines in order. The awk programs are generated only from validated column numbers,
and `awk_stats` ignores rows whose column is not numeric. Sed receives exactly one substitution
expression without a shell. jq accepts its filter language, but environment and module features (`env`,
`$ENV`, `import`, `include`, `module`, and `input_filename`) are rejected; the subprocess also gets a
minimal environment and neutral working directory.

## Responses And Errors

Successful calls return both MCP text content and identical `structuredContent`; VantaMCPd exposes the
latter under `output`. Text-producing operations use `{ "text": "..." }`. Collection operations use
`items` plus `count` and, where relevant, `truncated`. Parse and validation failures return MCP
`isError: true` with a bounded diagnostic. An operation whose optional Debian package is absent fails
with `... requires the <package> package, which is not installed on this node`, which is actionable
with `cluster_packages`. No category operation falls back to another node after it has started.

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