# Scientific Corpus Search

`corpus-search` provisions a compact arXiv descriptive-metadata corpus on a node's configured storage
volume and exposes bounded SQLite FTS5/BM25 lookup through MCP. It stores metadata and external links;
it does not download or serve papers, PDFs, or source archives.

## Initial Profile

The `compact-arxiv-cs` profile contains at most 10,000 deduplicated records:

- 5,000 recent AI, machine-learning, language, and information-retrieval records; and
- 5,000 recent records from a broader set of computer-science categories.

The profile is data-driven so later module versions can add profiles, categories, or higher limits.
The installer makes one arXiv API request at a time and waits at least three seconds between requests.

## Tools

| Tool | Purpose |
| --- | --- |
| `corpus_search` | Search titles, abstracts, authors, and categories with optional category/date filters |
| `corpus_get` | Retrieve one exact arXiv metadata record |
| `corpus_info` | Inspect profile, provenance, record counts, storage size, and refresh time |

Search inputs, result counts, offsets, and output bytes are bounded. Raw SQL and arbitrary FTS syntax
are not accepted.

## Data Lifecycle

Installation is a durable background job. The module is activated only after the new database passes
SQLite integrity and FTS checks. Interrupted downloads resume from page checkpoints when the profile
and cutoff still match. A previous active database remains available until replacement succeeds.

Uninstallation retains the corpus under the configured storage mount. Use the separately confirmed
`cluster_purge_module_data` operation after uninstalling to remove the marked corpus directory.

Source metadata is retrieved from [arXiv](https://arxiv.org/) under the
[arXiv API terms](https://info.arxiv.org/help/api/tou.html). Search results retain links to the arXiv
abstract and PDF pages.