# Scientific Corpus Search

`corpus-search` provisions a sampled arXiv descriptive-metadata corpus on a node's configured storage
volume and exposes bounded SQLite FTS5/BM25 lookup through MCP. It stores metadata and external links;
it does not download or serve papers, PDFs, or source archives.

![Corpus-search retrieval-augmented generation research workflow](corpus-search-sample.png)

*A "retrieval-augmented generation" analysis using automatically routed `corpus-search` tools.*

It uses singleton deployment, so the cluster may have one installed instance. Its on-demand runtime
starts a fresh Python MCP process over SSH stdio for each discovery or tool call and closes it afterward.
The SQLite database remains on node storage between calls, daemon restarts, and node reboots.

## Quickstart

The shortest path from an available storage node to a verified first search is:

1. Check compatibility and free storage:

	> Check whether corpus-search is compatible with cluster4.

2. Install a profile. Small is the 1% default; Medium is a practical broader starting point:

	> Install corpus-search on cluster4 using the Medium profile.

3. Keep the returned job ID and follow the durable installation:

	> Show the status and recent log output for job `<job-id>`.

4. Wait for `status: succeeded`, `phase: complete`, and the message `Corpus database activated.` The
	module receipt is not activated before the replacement database passes integrity and FTS checks.

5. Verify the installed profile and coverage:

	> Describe the installed corpus-search dataset, including its profile, sample percentage, topics,
	> cutoff, record count, database size, and largest categories.

	Confirm that `profileId` is the requested profile, `records` is greater than zero, and `cutoff` is
	recent enough for the intended analysis.

6. Run a first search, then retrieve complete records for useful results:

	> Search corpus-search for `retrieval augmented generation` and return the top 5 papers.
	> Retrieve the complete corpus record for arXiv `<result-id>`.

The target node must have configured node-local storage with at least 10 GiB free. All profiles download
and retain the same ZIP and extracted JSON, so Small reduces database ingestion but does not avoid the
source-data storage requirement or the initial download/extraction work.

## Corpus Configuration

The profile selects how much of the matching bulk snapshot is ingested:

| Profile ID | Snapshot sample | Intended use |
| --- | ---: | --- |
| `small-arxiv-cs` | 1% | Default, lightweight local search |
| `medium-arxiv-cs` | 25% | Broader research coverage |
| `large-arxiv-cs` | 100% | Every matching record in the snapshot |

All three profiles use the same deterministic sample seed. Selection hashes each arXiv ID, making the
sample stable across reinstalls and ensuring that the 1% population is contained in the 25% population,
which is contained in the 100% population. Percentages apply after topic filtering, so record counts
depend on the current snapshot and selected topics rather than a fixed target.

The packaged topic list covers `cs.AI`, `cs.LG`, `cs.CL`, `cs.IR`, `stat.ML`, `cs.CV`, `cs.RO`, `cs.SE`,
`cs.DB`, `cs.DC`, `cs.CR`, `cs.NE`, `cs.HC`, `cs.PL`, and `cs.OS`. Records may carry additional cross-list
categories. The active configuration combines a `profileId` with an optional topic override:

| Option | Bounds | Meaning |
| --- | --- | --- |
| `profileId` | `small-arxiv-cs`, `medium-arxiv-cs`, or `large-arxiv-cs`; default Small | Snapshot sampling percentage |
| `categories` | 1-50 arXiv category names | Replace the packaged topic list; use identifiers such as `cs.AI`, `stat.ML`, or `math.GT` |

Every installation starts from the Cornell University arXiv metadata snapshot ZIP. The installer caches
the ZIP, extracts its JSONL member, streams matching records into SQLite, and retains both source files
beside the database. It then uses arXiv's OAI-PMH feed to add records newer than the snapshot, applying
the same topics and sampling rule. OAI requests are serialized with at least three seconds between them;
JSON offsets and OAI resumption tokens are checkpointed for recovery.

## Install

The target needs Debian or Ubuntu on `armhf`, `arm64`, or `amd64`, at least 256 MB RAM, and configured
node-local storage with at least 10 GiB free. This accommodates the approximately 1.71 GiB compressed
metadata snapshot, its approximately 5.15 GiB uncompressed form, and the generated SQLite database.
The module declares `bash`, Python 3, SQLite, and CA certificates; VantaMCPd installs missing declared
packages during preflight.

The durable job timeout is six hours. Actual duration depends on network, CPU, storage, profile, and
snapshot size. Small and Medium still download and extract the complete source snapshot; profile size
primarily changes ingestion and FTS indexing time.

In Copilot Chat Agent mode, check placement before starting the download:

> Check whether corpus-search is compatible with cluster4.

Then install it on one explicit storage-backed node:

> Install corpus-search on cluster4.

The default Small profile ingests a 1% sample. Select Medium for a 25% sample:

> Install corpus-search on cluster4 using the Medium profile.

To ingest every matching record for a custom topic list:

> Install corpus-search on cluster4 using the Large profile with only `cs.AI`, `cs.LG`, and `cs.CL`.

The corresponding MCP arguments use manifest-declared install options:

```json
{
	"moduleId": "corpus-search",
	"targets": ["cluster4"],
	"options": {
		"profileId": "large-arxiv-cs",
		"categories": ["cs.AI", "cs.LG", "cs.CL"]
	},
	"confirm": true
}
```

Installation requires approval and runs as a durable background job. The initial tool response contains
a job ID rather than waiting for provisioning to finish, and the job continues if the MCP client
disconnects. Progress changes units by phase:

| Phase | Progress unit | Meaning |
| --- | --- | --- |
| `download` | bytes | Download or resume the cached snapshot ZIP |
| `extract` | bytes | Decompress the JSONL snapshot beside the ZIP |
| `ingest` | JSON bytes scanned | Filter topics, apply deterministic sampling, and insert metadata into SQLite |
| `catchup` | topics | Harvest post-snapshot metadata through OAI-PMH |
| `complete` | records | Activate the verified database and module receipt |

After `catchup` reaches its topic total, the displayed phase may remain there while SQLite rebuilds FTS,
runs `ANALYZE`, and performs integrity checks. Completion is indicated only by the terminal job status and
`Corpus database activated.` message.

> Show the status and recent log output for the corpus-search installation job.

Because deployment is singleton, change its profile, topics, or node by uninstalling the current instance
first and then reinstalling with the desired options. Uninstall retains the corpus data. A matching
profile and snapshot can seed OAI catch-up from the retained database; changing either rebuilds and
verifies the sampled corpus from the extracted snapshot JSON before activation.

To make a profile the default for manual and automatic installs, add it to `cluster.config.local.json`:

```json
{
	"modules": {
		"corpus-search": {
			"installOptions": { "profileId": "medium-arxiv-cs" }
		}
	}
}
```

Restart the VantaMCPd MCP server after editing the inventory; configuration is loaded once at startup.
Explicit `cluster_install_module` options override configured defaults.

## Tools

| Tool | Purpose |
| --- | --- |
| `corpus_search` | Search titles, abstracts, authors, and categories with optional category/date filters |
| `corpus_get` | Retrieve one exact arXiv metadata record |
| `corpus_info` | Inspect profile, provenance, record counts, storage size, and refresh time |

List the tools advertised by the installed module:

> List the tools provided by corpus-search on cluster4.

The target may be omitted because the module has exactly one active installation:

> List the tools provided by corpus-search.

### `corpus_search`

| Input | Required | Bounds | Meaning |
| --- | --- | --- | --- |
| `query` | yes | 1-500 characters, 1-32 searchable terms | Terms matched across title, abstract, authors, and categories |
| `category` | no | 1-40 characters | Exact arXiv category membership, including cross-lists |
| `publishedFrom` | no | `YYYY-MM-DD` | Inclusive lower bound on original publication date |
| `publishedTo` | no | `YYYY-MM-DD` | Inclusive upper bound on original publication date |
| `limit` | no | 1-50, default 10 | Maximum results in this page |
| `offset` | no | 0-10,000, default 0 | Results to skip for pagination |

Query punctuation is normalized into terms and every term must match. Callers cannot inject raw SQL or
FTS operators. Results are ordered by BM25 relevance with title matches weighted most heavily, then by
publication date. A higher returned `score` is a stronger match within that result set. Scores are
query-local ranking values and should not be compared across different queries. Spelling, hyphenation,
pluralization, and abbreviations are not expanded automatically; broaden the query explicitly when
those variants matter.

Common requests:

> Search corpus-search for `retrieval augmented generation` in category `cs.CL` and return the top 5 papers.

> Find papers matching `robot learning` in `cs.RO`, published from `2026-08-20` through `2026-09-10`.

> Search the scientific corpus for `language model`, return 10 results starting at offset 20, and include
> each paper's arXiv link.

> Find recent `database query optimization` papers across all available categories and group the results
> by primary category.

Each result includes the arXiv ID, title, authors, categories, dates, optional DOI/journal/comment fields,
abstract and PDF links, a highlighted abstract snippet, score, source query, profile slice, and fetch time.
Search results omit the complete abstract to keep pages compact. The response also echoes `query`,
`limit`, and `offset`; `hasMore` is true when a full page was returned and another page may exist.

An abbreviated structured result looks like:

```json
{
	"query": "retrieval augmented generation",
	"results": [
		{
			"id": "2608.21252",
			"title": "EnSI-RAG: Entity-Structure-Indexed Retrieval-Augmented Generation for Long-Document Question Answering",
			"authors": ["Xuanyu Meng", "Jiashuo Sun", "Jash Rajesh Parekh", "Jiawei Han"],
			"categories": ["cs.CL", "cs.AI", "cs.DB", "cs.IR"],
			"primaryCategory": "cs.CL",
			"published": "2026-08-21T00:00:00Z",
			"abstractUrl": "https://arxiv.org/abs/2608.21252",
			"pdfUrl": "https://arxiv.org/pdf/2608.21252",
			"snippet": "...Existing [retrieval]-[augmented] [generation]...",
			"score": 14.266
		}
	],
	"limit": 5,
	"offset": 0,
	"hasMore": true
}
```

The underlying generic proxy call is:

```json
{
	"moduleId": "corpus-search",
	"target": "cluster4",
	"toolName": "corpus_search",
	"arguments": {
		"query": "retrieval augmented generation",
		"category": "cs.CL",
		"limit": 5
	}
}
```

The proxy response identifies the selected node and module version, then places the tool's structured
result in `output`:

```json
{
	"ok": true,
	"node": "cluster4",
	"moduleId": "corpus-search",
	"moduleVersion": "0.3.0",
	"toolName": "corpus_search",
	"deployment": { "mode": "singleton" },
	"selection": "explicit",
	"output": { "query": "retrieval augmented generation", "results": [], "limit": 5, "offset": 0, "hasMore": false }
}
```

### `corpus_get`

Use `corpus_get` after search when the complete abstract or exact provenance fields are needed:

> Retrieve the complete corpus record for arXiv `2608.21252`.

The `id` may be a bare modern or legacy arXiv identifier, an `arxiv.org/abs/` URL, or a versioned ID such
as `2608.21252v2`. URL prefixes and version suffixes are normalized before lookup. The response contains
the full abstract in addition to the metadata returned by search. It does not fetch the linked paper.

```json
{
	"moduleId": "corpus-search",
	"toolName": "corpus_get",
	"arguments": { "id": "https://arxiv.org/abs/2608.21252v2" }
}
```

### `corpus_info`

Use `corpus_info` before analysis when corpus scope or freshness matters:

> Describe the installed corpus-search dataset, including its profile, cutoff, record count, size, and
> largest categories.

It takes no arguments and returns the schema/profile IDs, profile hash, sample percentage, content mode,
topics, snapshot and OAI source URLs, snapshot/catch-up cutoffs, refresh timestamp, database bytes, total
records, and counts by primary category. `contentMode` is `profile` for packaged topics and `topics` when
the install used a `categories` override.

## Example Research Workflow

An agent can discover and compose the bounded tools without the prompt naming either the module or its
installation node. VantaMCPd automatically routes each call to the singleton instance:

> Investigate recent work on retrieval-augmented generation in computational linguistics using the
> available scientific metadata corpus. First inspect the corpus coverage and freshness. Search for
> `retrieval augmented generation` in `cs.CL` with a limit of 5, then retrieve the complete records for
> the two strongest results in parallel. Produce a compact research brief with corpus provenance, a
> ranked comparison table, three synthesis bullets, and arXiv links. Use only the available local corpus;
> do not perform a web search or download the papers.

The initial info call establishes coverage and cutoff, search selects candidates, and the two exact-ID
lookups supply complete abstracts for synthesis. The module calls remain read-only, and VantaMCPd's
response metadata identifies the automatically selected node and module version.

## Limits and Safety

Search inputs, result counts, offsets, and total MCP output bytes are bounded. The service validates
every argument, opens the corpus database read-only, and exposes neither paths nor SQL. It does not
accept arbitrary FTS syntax, execute caller-provided code, contact arXiv during queries, or write search
artifacts. A missing database, invalid date/category/ID, out-of-range limit, or unknown record is returned
as an MCP tool error.

Each call validates the remote installation receipt and active version before launching the module.
The proxy prefers `structuredContent`, so successful JSON is not duplicated as escaped text.

## Data Lifecycle

Installation is a durable background job. The module is activated only after the new database passes
SQLite integrity and FTS checks. Partial ZIP downloads, JSONL byte offsets, and OAI resumption tokens
support recovery. A matching retained database and snapshot skip baseline reingestion while still
running OAI catch-up; a previous active database remains available until replacement succeeds.

Canceling or failing an install does not discard the source ZIP, extracted JSON, checkpoint, retained
database, or previously active version. Reissuing the same profile can resume a partial ZIP download,
reuse a completed extraction, continue JSON ingestion from its byte checkpoint, or continue OAI-PMH from
its resumption token. A changed profile or topic list rebuilds the sampled database because its content
identity differs.

Uninstall the executable payload and receipt with:

> Uninstall corpus-search from cluster4.

Uninstallation requires approval and retains the corpus under the configured storage mount, allowing a
later install to reuse the data. To remove it permanently, uninstall first and then use the separately
confirmed `cluster_purge_module_data` operation. Purge is restricted to the module's marked data
directory.

Baseline metadata comes from Cornell University's weekly
[arXiv metadata snapshot](https://www.kaggle.com/datasets/Cornell-University/arxiv), followed by newer
records from [arXiv OAI-PMH](https://info.arxiv.org/help/oa/index.html) under the
[arXiv API terms](https://info.arxiv.org/help/api/tou.html). Search results retain links to the arXiv
abstract and PDF pages.

## Troubleshooting

| Symptom | Check or action |
| --- | --- |
| Preflight reports insufficient storage | Configure a distinct node-local storage mount with at least 10 GiB free; root filesystem space does not satisfy this requirement |
| Job appears paused after `catchup` reaches all topics | Check its heartbeat and log; FTS rebuild, analysis, and integrity verification do not currently emit separate progress markers |
| Install fails or is canceled | Read the job log, correct the cause, and reinstall with the same profile to reuse retained downloads and checkpoints |
| Search returns no results | Inspect `corpus_info` for profile/topics, then try fewer terms or explicit spelling, hyphenation, singular/plural, and abbreviation variants |
| Dashboard shows an old catalog or module version | Run `npm run build`, restart the local VantaMCPd MCP server, then refresh the dashboard; the inventory and catalog are loaded by that process |
| A different profile or topic set is needed | Uninstall first, then reinstall with the new options; singleton placement rejects a second active instance |

## Local Development

Run the standard-library and SQLite FTS5 smoke test from the module directory:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 server.py --self-test
```

Run the fixture-backed provisioning and complete MCP protocol tests from the repository root:

```bash
node --test test/corpus-search.test.mjs
```

The fixture tests do not contact Kaggle or arXiv. They build a temporary snapshot ZIP and corpus,
exercise bulk ingestion, OAI catch-up, all three tools, and input bounds, then remove the data afterward.