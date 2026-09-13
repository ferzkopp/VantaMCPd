# Text Tools

`text-tools` is a bounded node-side text-processing MCP module. Its Python code uses only the standard
library and its constrained command wrappers reuse Debian packages (`ripgrep`, `jq`, `mawk`, and
`sed`). See [Operations](Operations.md) for the full matrix, schemas, signatures, and deferrals.

![Text-tools automatic routing and incident report](text-tools-sample.png)

*A release incident investigated with parallel, automatically routed `text-tools` operations.*

The module accepts caller-provided text only. It does not accept paths, fetch URLs, run a shell, execute
caller-supplied awk/sed programs, or create artifacts.

It is a replicated, round-robin, on-demand module: VantaMCPd starts it for each discovery or tool call and closes it afterward.
Nothing needs to run continuously, and the next call works normally after either VantaMCPd disconnects
or the node reboots.

## Install

In Copilot Chat Agent mode, select a node explicitly:

> Check whether text-tools is compatible with cluster1.

> Install text-tools on cluster1.

To provide two interchangeable instances:

> Install text-tools on cluster1 and cluster2.

Installation requires approval. VantaMCPd performs live hardware and disk checks, installs declared apt
dependencies when required commands are missing, repeats preflight, verifies staged file hashes, runs
the module self-test, activates the version, and writes an installation receipt. Shared apt packages are
not removed during module uninstall.

## Tools

- `regex_extract`: bounded regex matches and capture groups.
- `csv_normalize`: delimiter detection or selection and normalized CSV output.
- `log_parse_kv`: `key=value` and `key:value` extraction by line.
- `html_extract`: visible text, headings, and links from HTML.
- `text_transform`: case/identifier conversion, normalization, line operations, wrapping, and templates.
- `text_extract`: emails, URLs, numbers, IPs, date/time strings, code blocks, and quotations.
- `text_analyze`: statistics, readability, keywords, similarity, n-grams, sentences, and token estimates.
- `text_codec`: Base64, URL, HTML, Unicode, hex, binary, and unverified JWT decoding.
- `data_convert`: bounded JSON, CSV, TOML, INI, XML, and query-string conversion.
- `text_security`: digests, HMACs, checksums, and UUID validation.
- `text_generate`: UUIDv4, secure passwords, and placeholder text.
- `developer_text`: regex replacement, unified diffs, and semantic versions.
- `document_process`: Markdown text/TOC/fences and JSON or TOML frontmatter.
- `table_transform`: Markdown rendering and sorting of flat JSON tables.
- `command_text`: stdin-only constrained wrappers for `rg`, `jq`, `awk`, and `sed`.

List the tools advertised by the installed module:

> List the tools provided by text-tools on cluster1.

Omit the node to select an installed instance automatically:

> List the tools provided by text-tools.

Example requests:

> Use text-tools to extract numeric IDs from `item=12 item=37` using `item=(\d+)`.

> Use text-tools on cluster1 to normalize `name;score\nAda;10\nLinus;9` as CSV.

> Use text-tools on cluster1 to parse `level=info code:200 message="ready"` as key/value fields.

> Use text-tools on cluster1 to extract visible text, headings, and links from `<h1>Docs</h1><a href="/api">API</a>`.

> Use text-tools to search `level=info\nlevel=error code=42` with ripgrep for `error|code=[0-9]+`.

> Use text-tools to sort the JSON table `[{"name":"b","score":2},{"name":"a","score":10}]` by score numerically.

The underlying generic proxy call for the regex example is:

```json
{
	"moduleId": "text-tools",
	"toolName": "regex_extract",
	"arguments": {
		"text": "item=12 item=37",
		"pattern": "item=(\\d+)"
	}
}
```

With no `target`, successive calls round-robin over reachable nodes where `text-tools` is installed.
Set `"target": "cluster1"` to pin a call. The generic proxy response reports which node ran it:

```json
{
	"ok": true,
	"node": "cluster1",
	"moduleId": "text-tools",
	"moduleVersion": "0.2.0",
	"toolName": "regex_extract",
	"deployment": { "mode": "replicated", "routing": "round-robin" },
	"selection": "automatic",
	"output": { "matches": [], "truncated": false }
}
```

The proxy prefers the module's `structuredContent`, so the same result is not repeated as escaped JSON
inside `content[].text`. For text-only modules, a sole JSON text item is parsed when possible; plain text
and multi-part MCP content are preserved. Module-reported errors return `ok: false` and an MCP error.

Each call runs wholly on one node. To exercise both replicas concurrently, ask Copilot:

> Using text-tools with automatic routing, run these independent operations in parallel and report the
> node for each result: extract `OPS-142` and `OPS-207` from
> `release=2026.09 tickets=OPS-142,OPS-207`; normalize
> `node;status\ncluster1;ready\ncluster2;ready` as CSV; parse
> `level=info module=text-tools replicas:2 routing=round-robin`; and extract content from
> `<h2>Deployment report</h2><p>Two replicas ready.</p>`.

With two reachable installations, the four targetless calls are assigned two per node. Results can
finish in any order. A failed in-progress call is returned as a failure rather than silently replayed on
another node.

Its structured result contains:

```json
{
	"matches": [
		{ "match": "item=12", "groups": ["12"], "start": 0, "end": 7 },
		{ "match": "item=37", "groups": ["37"], "start": 8, "end": 15 }
	],
	"truncated": false
}
```

Each call validates the remote receipt and active version, starts a fresh Python MCP process over SSH
stdio, invokes only a tool returned by `tools/list`, and applies the manifest's input, output, startup,
and call limits.

## Uninstall

> Uninstall text-tools from cluster1.

Uninstallation requires approval and an explicit target. It validates the receipt before removing the
active payload and receipt. Repeating the operation after removal is safe.

## Local Development

Run the local smoke test with:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 server.py --self-test
```

Run the complete stdio protocol matrix from the repository root with:

```bash
node --test test/text-tools.test.mjs
```