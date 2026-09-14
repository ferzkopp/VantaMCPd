# Node Modules

## Purpose

VantaMCPd manages a small cluster of resource-constrained Debian/Armbian nodes. In addition to cluster administration, those nodes should be able to provide local compute, retrieval, transformation, and knowledge services to an MCP client.

This document defines:

- the architecture for packaging and deploying node-side MCP modules;
- the minimum implementation needed to prove the complete lifecycle;
- feasibility on the currently connected nodes; and
- the catalog of possible future modules and data sources.

## Using Modules

Module packages are trusted code shipped in this repository. VantaMCPd evaluates each package against
the selected node's recorded hardware and a live preflight before installation. Nodes can be ARM, x86,
GPU-equipped, or another supported profile; eligibility comes from declared capabilities rather than
node names or roles.

The currently implemented user-facing management tools are:

| Tool | Status | Use |
| --- | --- | --- |
| `cluster_list_modules` | Available | List packages, install options, deployment policy, compatibility, and live installation state |
| `cluster_check_module` | Available | Check recorded capabilities, required commands, free disk, and reachability |
| `cluster_install_module` | Available | Install a compatible package on explicit targets after `confirm: true` |
| `cluster_uninstall_module` | Available | Remove the payload and receipt safely from explicit targets after `confirm: true` |
| `cluster_purge_module_data` | Available | Permanently remove marked retained data after uninstall and `confirm: true` |
| `cluster_list_module_tools` | Available | Discover tools on an explicit or automatically selected installation |
| `cluster_call_module_tool` | Available | Call a tool on an explicit installation or use manifest-defined routing |
| `cluster_list_jobs` | Available | List durable jobs with phase, progress, heartbeat, and result |
| `cluster_get_job` | Available | Refresh one durable job by ID |
| `cluster_get_job_log` | Available | Read a bounded remote job-log tail |
| `cluster_cancel_job` | Available | Cancel a running job after `confirm: true` |

After pulling module changes, run `npm run build` and restart the `vanta` MCP server in your client so it
reloads the local catalog.

### Implemented Modules

| Module | Package version | Requirements | Included tools | Guide |
| --- | --- | --- | --- | --- |
| Text Tools (`text-tools`) | `0.4.0` | Debian/Ubuntu, `armhf`/`arm64`/`amd64`, Python 3, 256 MB RAM, 40 MB disk; `ripgrep`, `jq`, `mawk`, `sed` | 111 bounded operations across twelve category tools | [Text Tools](../modules/text-tools/TextTools.md) |
| Scientific Corpus Search (`corpus-search`) | `0.4.0` | Debian/Ubuntu, `armhf`/`arm64`/`amd64`, configured node storage with 10 GiB free, Python 3, SQLite 3 | Phrase/exclusion/field search, exact record lookup, category resolution, and corpus metadata | [Scientific Corpus Search](../modules/corpus-search/CorpusSearch.md) |

### Activate a Module

Activation installs the versioned package on one or more nodes. Runtime can be on demand or a persistent
service, and a manifest can move a long-running installation into a durable job. Start with these
requests in any connected MCP agent:

> List the available node modules for cluster1.

> Check whether text-tools is compatible with cluster1.

> Install text-tools on cluster1.

> Install text-tools on cluster1 and cluster2.

Installation never defaults to the entire cluster. The agent must use explicit node names or tags and
must obtain approval before calling `cluster_install_module` with `confirm: true`. VantaMCPd stages the
package over SFTP, verifies every SHA-256 hash, runs its trusted installer, switches the active version,
and writes a root-owned receipt. When commands are missing, declared apt packages are installed first
and preflight is repeated. A failed activation retains the previous active version.

A schema-v2 module may declare typed `installOptions`. `cluster_list_modules` returns their descriptions,
defaults, and bounds. Pass selected values in the `options` object of `cluster_install_module`; unknown
names, invalid types, and out-of-range values are rejected before any remote work. VantaMCPd forwards
only declared values to the trusted lifecycle script through namespaced environment variables.

Persistent installation defaults may be set in `cluster.config.local.json`; explicit tool arguments
override them. Defaults use the same manifest validation before any SSH work. Restart VantaMCPd after
editing the inventory because configuration is loaded once at startup:

```json
{
  "modules": {
    "corpus-search": {
      "installOptions": { "profileId": "medium-arxiv-cs" }
    }
  }
}
```

These defaults apply to the next install that runs; they do not reconfigure an already-installed node.
Automatic updates compare versions only, so a node already running the catalog version is skipped before
options are read. To apply changed options to a current installation, uninstall and reinstall it.

Schema-v2 job-backed activation returns `state: "provisioning"` and a `jobId` after verified staging.
The remote systemd oneshot owns the lifecycle operation from that point, so it survives an MCP or SSH
disconnect and a local daemon restart. The module receipt and active symlink are written only after the
installer succeeds. An existing version remains active while an update job provisions its replacement.

After installation, discover and use its tools with requests such as:

> List the tools provided by text-tools.

> Use text-tools to extract the numeric IDs from `item=12 item=37` with the pattern `item=(\d+)`.

VantaMCPd verifies the receipt and active version, launches the module through SSH stdio, completes the
MCP handshake, checks that the requested tool is advertised, returns the result, and closes the process.
An unrecognized tool name is rejected with the module's advertised names, so a wrong guess is corrected
without a separate discovery call.

### Capability Discovery

A module's own tools live behind `cluster_call_module_tool` and never appear in the daemon's tool list,
so an agent has no way to guess that the cluster can convert YAML or redact secrets. Each manifest
therefore declares up to twelve short `capabilities` phrases. At start-up VantaMCPd composes them from
the local catalog into two places the model reads on its own:

| Destination | When the model sees it |
| --- | --- |
| The server `instructions` returned by `initialize` | Once per session, usually as part of the system prompt |
| The `cluster_call_module_tool` and `cluster_list_module_tools` descriptions | On every request, while deciding which tool to call |

That is what lets *"redact the IPs in this log"* reach the cluster without the user naming `text-tools`.
Write each phrase as the work a user would ask for, not as an implementation detail, and keep it short:
this text is in context for the whole session. Capability text is descriptive only. It never affects
compatibility, routing, or installation, and a module that omits it simply stays undiscoverable until
asked for by name.

### Deployment and Routing

Every module manifest declares its cluster placement policy:

```json
{ "deployment": { "mode": "replicated", "routing": "round-robin" } }
```

A `replicated` module may be installed on any number of compatible nodes. When `target` is omitted from
`cluster_call_module_tool`, VantaMCPd discovers reachable installations from their receipts and advances
a process-local round-robin cursor. The response identifies `ok`, the selected `node`, `moduleId`,
`moduleVersion`, `toolName`, `deployment`, `selection`, and normalized `output`. An explicit `target` bypasses routing, which is useful for diagnostics or
node-specific work. Tool discovery without a target uses the first reachable installation without
advancing the call cursor.

`cluster_list_modules` performs a live, validated receipt scan. Each catalog entry includes
`installedNodes` and an `installedVersions` node-to-version map. Each requested node includes
`inventoryReachable`, `installed`, `installedVersion`, and `updateAvailable` when its state is known.
Treat `inventoryComplete: false` as incomplete knowledge: names in `unreachableNodes` may still have an
installation, so absence from `installedNodes` is not proof of absence there. Invalid receipts are not
treated as installations and are reported separately in node inventory.

On startup, `defaults.autoUpdateModules` defaults to `true`. After initial hardware discovery,
VantaMCPd compares validated installed receipt versions with its local catalog using Semantic Versioning.
It updates only older known installations; equal versions are left alone and newer remote versions are
never downgraded. The new version is staged, verified, self-tested, and activated first. Only then is the
old version directory removed, so a failed update retains the previous active version. Set
`defaults.autoUpdateModules` to `false` to disable this policy. This configured startup policy is the
only exception to interactive lifecycle confirmation; manual install and uninstall tools still require
`confirm: true`.

```json
{ "deployment": { "mode": "singleton" } }
```

A `singleton` module may have one installation in the configured cluster. Before installation,
VantaMCPd scans every configured node for its receipt. It rejects multiple targets, an existing receipt
on another node, or any unreachable node because uniqueness cannot then be proven. Install and uninstall
operations for the same module are serialized, so concurrent requests through one daemon cannot race
past the check. This is not distributed consensus between independent VantaMCPd processes; operate one
manager instance per cluster when singleton placement is required.

The round-robin cursor resets after daemon restart or a successful lifecycle operation. Candidate nodes
are rediscovered from remote receipts, so rebooting the local daemon does not require a routing database.

Routing operates at the MCP-call boundary: one call is never split between replicas. To use replicas in
parallel, submit independent calls concurrently. For example:

> Using text-tools with automatic routing, run these independent operations in parallel and report the
> node for each result: extract `OPS-142` and `OPS-207` from
> `release=2026.09 tickets=OPS-142,OPS-207`; normalize
> `node;status\ncluster1;ready\ncluster2;ready` as CSV; parse
> `level=info module=text-tools replicas:2 routing=round-robin`; and extract content from
> `<h2>Deployment report</h2><p>Two replicas ready.</p>`.

That prompt should produce four `cluster_call_module_tool` requests without `target`. With two healthy
installations, two calls are assigned to each node. Completion order can differ from submission order,
which is normal for parallel work. Round-robin does not imply shared session state, failover of a call
already in progress, or aggregation of results; the caller owns those concerns.

### Deactivate a Module

Deactivate an installed module with a request such as:

> Uninstall text-tools from cluster1.

The agent must use explicit node names or tags and obtain approval before calling
`cluster_uninstall_module` with `confirm: true`. VantaMCPd validates the root-owned receipt before it
runs the package's trusted uninstall script, removes the active payload and receipt, and leaves unrelated
versions and declared retained data untouched. Repeating the request when the module is already absent
succeeds without changing the node. Service modules are stopped by their uninstaller.

For a module with `retainOnUninstall: true`, purge is a separate destructive operation. It is allowed
only after uninstall, requires `confirm: true`, rejects symbolic-link targets, and removes only a data
directory carrying the expected VantaMCPd module marker.

### Runtime and Restart Behavior

Every manifest declares one of two runtime modes:

- `on-demand`: VantaMCPd starts a fresh module process for discovery or a tool call and closes it when
  the operation finishes. A local MCP disconnect or node reboot leaves no process to recover; the next
  call reads the persistent receipt and launches the module again. `text-tools` uses this mode.
- `service`: the package includes a systemd unit. During installation VantaMCPd installs it as
  `vantamcpd-<module-id>.service`, runs `systemctl enable --now`, and records the runtime in the receipt.
  The node restarts the service automatically after boot, independently of the local MCP connection.
  Uninstallation disables and stops the service, removes the unit, reloads systemd, and then removes the
  payload and receipt.

A persistent service manifest uses:

```json
{
  "runtime": {
    "mode": "service",
    "systemdUnit": "module.service"
  }
}
```

The unit should reference the stable `/opt/vantamcpd/modules/<module-id>/current/` path and define its
own restart policy, resource limits, user, and writable directories. On-demand modules declare
`"runtime": { "mode": "on-demand" }`.

## Hardware Model and Current Environment

The module architecture is hardware-neutral. A cluster may mix ARMv7, arm64, x86-64, GPU-equipped, and other accelerator-backed nodes. Adding a more capable node must expand the set of eligible modules without requiring a new package format, lifecycle, or proxy protocol.

The currently connected cluster happens to consist of four ARMv7 nodes with approximately two CPU cores and 1 GB RAM each. They run Debian 12 on Armbian. The system disk is relatively small; one node also provides larger shared storage. This is the first compatibility profile to test, not the target architecture of the module system.

These constraints are part of the design, not exceptional conditions. A module must declare its requirements, and VantaMCPd must reject installation when known hardware or platform facts do not satisfy them.

Compatibility is determined from both:

1. Recorded hardware inventory, such as CPU architecture and features, package architecture, RAM, cores, operating system, configured storage, GPUs, accelerators, and their available memory or runtime capabilities.
2. A live preflight, such as available disk space, required commands, device availability, drivers, and runtime versions.

Missing or stale required facts produce an `unknown` result and a request to refresh hardware. They must not be treated as compatible by default.

## Goals

- Store each distributable module under a local `modules/<module-id>/` directory.
- List available and installed modules through VantaMCPd.
- Evaluate compatibility before making changes to a node.
- Install and uninstall a module on explicitly selected nodes.
- Expose an installed module's MCP tools through the existing VantaMCPd connection.
- Record module deployment and execution in the existing audit log.
- Show read-only module status for each node in the monitoring dashboard.
- Prove the design with one small module that runs on the current cluster.

## Non-goals for the First Release

- A public or remote module registry.
- Downloading or executing untrusted packages.
- Package signing or third-party module distribution.
- Explicit rollback commands or dependency sharing between modules.
- Opening module service ports on cluster nodes.
- Browser-based install or uninstall actions.
- Shared artifact upload/download APIs.
- Vector search or local embedding generation.

These are future capabilities and are listed in the roadmap.

## Selected Architecture

### One client connection

The configured agent connects only to the local VantaMCPd stdio server. VantaMCPd then acts as an MCP
client for installed node modules and proxies module discovery and tool calls over its existing SSH
connections.

For on-demand modules, a module server is launched through an SSH exec channel and communicates using MCP stdio framing. The process is closed after tool discovery or a tool call completes. Service modules use a package-provided systemd unit installed and enabled by the same lifecycle manager.

This design:

- keeps the agent's single VantaMCPd connection unchanged as modules are added;
- does not expose unauthenticated services on the cluster network;
- reuses SSH host verification, authentication, concurrency, and audit logging;
- works without installing Node.js on every cluster node; and
- allows each module to choose a runtime supported by its target nodes.

The first release uses generic proxy tools. Dynamic registration of every remote tool as a top-level VantaMCPd tool is deferred until the lifecycle and transport are proven.

### Sources of truth

| Information | Source of truth |
| --- | --- |
| Available modules | Valid local packages under `modules/` |
| Hardware capability | Cluster inventory, refreshed by hardware discovery |
| Live free capacity | Installation preflight on the target node |
| Installed module/version | Receipt on the target node |
| Module health and tools | Probe of the installed entrypoint |
| Durable job state and log | `/var/lib/vantamcpd/jobs/<job-id>/` on the target node |

Installed-module state is mutable remote state and must not be copied into `cluster.config.local.json`.

## Module Package Contract

Each module is a trusted, repository-owned package with this minimum layout:

```text
modules/
  text-tools/
    module.json
    install.sh
    uninstall.sh
    server.py
    operation_common.py
    operations.py
    operations_*.py
    TextTools.md
```

The manifest will be validated with Zod before any remote operation. Its initial schema contains:

```json
{
  "schemaVersion": 1,
  "id": "text-tools",
  "name": "Text Tools",
  "version": "0.4.0",
  "description": "Bounded text processing and constrained command-wrapper tools.",
  "capabilities": [
    "convert between JSON, JSONL, YAML, CSV, TOML, INI, XML, dotenv and query strings",
    "scan text for credentials and redact emails, IPs and secrets"
  ],
  "entrypoint": ["python3", "server.py"],
  "compatibility": {
    "os": ["debian"],
    "architectures": ["armhf", "arm64", "amd64"],
    "minCores": 1,
    "minRamMb": 256,
    "minDiskMb": 40,
    "requiredCommands": ["bash", "python3", "rg", "jq", "awk", "sed"],
    "accelerators": []
  },
  "packages": { "apt": ["python3", "ripgrep", "jq", "mawk", "sed"] },
  "lifecycle": { "install": "install.sh", "uninstall": "uninstall.sh" },
  "deployment": { "mode": "replicated", "routing": "round-robin" },
  "runtime": { "mode": "on-demand" },
  "limits": {
    "startupMs": 10000,
    "callMs": 30000,
    "maxInputBytes": 262144,
    "maxOutputBytes": 262144
  }
}
```

Manifest IDs and relative paths use conservative character sets. Package loading rejects unsupported schema versions, duplicate IDs, missing files, symbolic links, path traversal, malformed versions, oversized packages, and unknown manifest properties. Schema v2 adds `lifecycle.execution` for job-backed installation and `persistentData` for a contained path under the target node's configured storage mount; schema v1 remains supported unchanged.

Compatibility fields are optional constraints rather than a fixed list of node classes. Future manifest revisions may describe CPU instruction sets, GPU vendor/model, minimum VRAM, CUDA/ROCm versions, neural accelerators, container runtimes, or other named capabilities. Hardware discovery and compatibility evaluation must version these facts explicitly; they must not infer capability from node names, roles, or architecture alone.

## Installation Layout and Lifecycle

Versioned payloads and receipts use root-owned paths:

```text
/opt/vantamcpd/modules/<module-id>/<version>/
/opt/vantamcpd/modules/<module-id>/current -> <version>/
/var/lib/vantamcpd/modules/<module-id>.json
```

The lifecycle is:

1. Resolve explicit target nodes. Install and uninstall never default to all nodes.
2. Load and validate the local package.
3. Enforce the manifest deployment policy across the configured cluster.
4. Evaluate recorded compatibility and run a live preflight.
5. Require `confirm: true` before changing a node.
6. If required commands are missing, install the manifest's declared apt packages without upgrading
  already-present packages, then require a clean live preflight.
7. Upload regular package files to a random, user-owned staging directory.
8. Compare a generated SHA-256 file manifest locally and remotely.
9. Run the trusted lifecycle script through the audited sudo execution path.
10. Install into a new versioned directory and run a smoke check.
11. Atomically update `current` and write the installation receipt.
12. Always remove staging files.

Installation and uninstallation are idempotent. A failed installation leaves the previous active version intact. Uninstallation removes the executable payload and receipt but preserves explicitly declared writable data paths by default.

Initial module states are:

- `available`: package exists locally but is not installed;
- `compatible`, `incompatible`, or `unknown`: compatibility result for a node;
- `installed`: receipt and expected versioned payload exist;
- `unhealthy`: receipt exists but the entrypoint probe fails;
- `version-mismatch`: installed and local catalog versions differ; and
- `unreachable`: remote state cannot be inspected.

## MCP Management and Proxy Tools

The minimum VantaMCPd API consists of:

| Tool | Purpose |
| --- | --- |
| `cluster_list_modules` | List catalog entries, compatibility, and live per-node receipt state |
| `cluster_check_module` | Refresh compatibility, live preflight, installation state, and health |
| `cluster_install_module` | Install one catalog module on explicit targets with confirmation |
| `cluster_uninstall_module` | Remove one installed module from explicit targets with confirmation |
| `cluster_list_module_tools` | Return `tools/list` from an explicit or automatically selected installation |
| `cluster_call_module_tool` | Call a tool on an explicit node or route it according to the manifest |

The proxy verifies the installation receipt and expected entrypoint before launch. Only catalog module IDs and tools returned by `tools/list` may be called. Inputs are sent as MCP data rather than interpolated into a shell command. Startup, call, stderr, and result sizes are bounded. Structured module output is returned once under `output`; a sole JSON text result is parsed, plain text remains text, and multi-part or non-text MCP content remains an array. A remote module `isError` result sets `ok: false` and is propagated as an error by the outer MCP tool.

The installed MCP SDK supports removable registered tools and `notifications/tools/list_changed`. A future release may therefore promote remote module tools to first-class VantaMCPd tools without changing the package contract.

## Security and Resource Policy

- Local packages are trusted code maintained with VantaMCPd. No arbitrary URL or uploaded archive may be installed in the first release.
- Manual install and uninstall require explicit targets and confirmation. Default-enabled startup
  reconciliation may update an already-installed module to a newer trusted local catalog version; it
  never creates a new installation or downgrades a newer node receipt.
- Singleton installation fails closed unless uniqueness can be verified across every configured node.
- There is no force-install bypass for an incompatible node in the first release.
- Module processes run as the configured SSH user unless a narrowly scoped lifecycle step requires sudo.
- Modules receive no VantaMCPd credentials or SSH keys.
- Module stdout is MCP protocol data only; bounded stderr is retained as diagnostic output.
- Existing audit attribution records deployment, probes, and module execution.
- Package count, package bytes, execution time, input bytes, and output bytes are capped.
- A module that processes untrusted input must implement its own domain limits.
- Long-running lifecycle work uses allowlisted systemd jobs with durable state, bounded logs, resource
  locks, heartbeat monitoring, cancellation, timeout enforcement, and expiration cleanup.

## Minimal Module: Text Tools

`text-tools` proves package discovery, compatibility, dependency installation, MCP proxying, dashboard
reporting, and removal without architecture-specific wheels. Its Python implementation uses the
standard library, while constrained wrappers reuse distribution packages for `rg`, `jq`, `awk`, and
`sed`. It accepts caller-provided text through MCP/stdin only.

Its MCP surface is twelve category tools holding 111 operations. Each takes an `operation` discriminator
and that operation's own fields, so `tools/list` stays compact while every operation keeps a strict
schema:

| Tool | Behavior |
| --- | --- |
| `text_transform`, `text_extract`, `text_analyze` | Bounded cleanup, structured-value extraction, statistics, invisible-character inspection, chunking, and TF-IDF |
| `text_codec`, `data_convert` | Encodings, and conversion between JSON, JSONL, CSV, `key=value` logs, dotenv, HTML, YAML, TOML, INI, XML, and query strings, plus structural diff, merge, and schema inference |
| `text_security`, `text_generate` | Digests, HMACs, checksums, secret scanning, redaction, password strength, UUIDs, passwords, and passphrases |
| `developer_text`, `document_process`, `table_transform` | Regex extraction/replacement/testing, diffs, semantic versions, Markdown structure, and flat JSON or CSV tables |
| `datetime_text` | Timestamp parsing, reformatting, timezone conversion, intervals, and durations |
| `command_text` | Constrained stdin-only wrappers for `rg`, `jq`, `awk`, and `sed` |

The module does not accept node paths, fetch URLs, run a shell, or write artifacts. Inputs, patterns,
collections, and result bytes are bounded. Python regex matching/replacement runs in a killable worker;
external commands have fixed argument shapes and a five-second timeout. See the
[Text Tools quickstart](../modules/text-tools/TextTools.md#quickstart) for installation, routing, first
use, and lifecycle; the complete API and deferred features are documented in
[Text Tools operations](../modules/text-tools/Operations.md).

## Scientific Corpus Search

`corpus-search` is a singleton, on-demand module installed directly on a configured storage node. Its
Small, Medium, and Large profiles deterministically ingest 1%, 25%, or 100% of bulk-snapshot records
matching the configured arXiv topics into SQLite FTS5 and rank searches with BM25. It stores titles,
abstracts, authors, categories, identifiers, provenance, and external links; it does not mirror papers
or PDFs.

Provisioning is a durable job that downloads the Cornell University snapshot ZIP, extracts and streams
its JSONL metadata, then uses rate-limited OAI-PMH requests to add newer matching articles. ZIP download,
JSON ingestion, and OAI pagination are resumable, and only a validated replacement database is activated.
The tools are `corpus_search`, `corpus_get`, `corpus_categories`, and `corpus_info`. Searches accept
quoted phrases, exclusions, alternatives, prefix terms, and field restrictions, and retry once with
corrected spelling when a query finds nothing. All profiles require 10 GiB free because
the ZIP and extracted JSON are retained regardless of sample percentage. See the
[Scientific Corpus Search quickstart](../modules/corpus-search/CorpusSearch.md#quickstart) for profile
selection, installation progress, verification, first use, recovery, and data lifecycle.

## Current-node Feasibility Snapshot

Ratings below describe a useful implementation on the current ARMv7/1 GB nodes only. They are not global module ratings. The same manifests may evaluate differently on future x86-64, arm64, high-memory, GPU, or accelerator-backed nodes.

| Module | Feasibility | Practical first scope or blocker |
| --- | --- | --- |
| Regex, parsing, and extraction | High | Python standard library; selected MVP |
| Artifact storage | High | Local filesystem or existing NFS, with quotas and retention |
| Documentation/scientific corpus | Implemented with selectable sampling | SQLite FTS5/BM25 over 1%, 25%, or 100% of matching arXiv snapshot metadata; no embeddings |
| Image processing | High for basic transforms | Pillow or ImageMagick resize/crop/filter; no neural models |
| Python execution | Medium | Basic Python only; needs sandbox and resource limits |
| NumPy/SciPy compute | Medium | Prefer Debian armhf packages and OpenBLAS; memory limits apply |
| Screenshot and OCR | Medium for OCR | Tesseract on supplied images; browser capture is a separate blocker |
| PDF parsing | Medium for text extraction | pdfminer.six or command-line tools; tables/OCR can be expensive |
| Geospatial compute | Medium for basic operations | Shapely/GeoJSON only; PostGIS is too heavy for the MVP |
| Durable job runner | Implemented for trusted lifecycle work | systemd oneshots and node-local JSON state; no arbitrary command submission |
| Wikipedia knowledge store | Low for full English corpus | Use a curated SQLite/Kiwix subset on large storage first |
| Browserless webpage retrieval | Low | Current Playwright support covers x86-64/arm64, not ARMv7 |
| Vision/ML inference | Low on current nodes | PyTorch, Transformers, CLIP, YOLO, and vLLM exceed the current profile; capable GPU nodes may qualify |
| Vector database service | Low on current nodes | Milvus and Weaviate exceed the current profile; evaluate them normally on larger future nodes |

Intel MKL is not an option for the current ARM nodes; OpenBLAS is appropriate for this profile. A future x86-64 profile may select MKL or another optimized backend. Compatibility with a Python project also depends on whether its current releases provide artifacts for the node's architecture and accelerator stack or can use maintained distribution packages.

## Future Module Catalog

The sections below remain design candidates except where an implemented baseline is explicitly noted.

### Compute kernels

#### Python Kernel

Possible capabilities include bounded Python snippets, parameterized notebooks, stdout, structured JSON, plots, and artifact references. Candidate technologies include [Jupyter Kernel Gateway](https://github.com/jupyter-server/kernel_gateway), [Papermill](https://github.com/nteract/papermill), [uv](https://docs.astral.sh/uv/), and [micromamba](https://mamba.readthedocs.io/en/latest/user_guide/micromamba.html).

Potential stacks include pandas, NumPy, Polars, DuckDB, matplotlib, Plotly, scikit-learn, spaCy, Sentence Transformers, PyTorch, and Transformers. These stacks must be separate compatibility profiles; they are not one installable baseline. Arbitrary code execution requires filesystem, process, network, CPU, memory, and time isolation.

#### NumPy/SciPy Compute

Possible capabilities include matrix operations, optimization, signal processing, and statistical tests. See the [SciPy installation guide](https://scipy.org/install/) and [OpenBLAS](https://www.openblas.net/). The current cluster should use tested Debian armhf packages where possible and reject inputs that exceed memory budgets.

### Retrieval and web processing

#### Browserless Retrieval

Possible capabilities include HTML retrieval, rendered DOM extraction, scripted interaction, screenshots, and PDF rendering. Candidate projects are [Browserless](https://www.browserless.io/), [Playwright](https://playwright.dev/docs/intro), and [Puppeteer](https://pptr.dev/).

Current Playwright Linux support is limited to x86-64 and arm64 on supported Debian/Ubuntu releases. A practical deployment therefore requires a future arm64/x86-64 node or an external browser service rather than the current ARMv7 workers.

#### Screenshot and OCR

Possible capabilities include image capture, OCR, and structured text blocks. [Tesseract](https://github.com/tesseract-ocr/tesseract) is the practical CPU option for supplied images. [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR) and its model runtimes are unlikely to be practical on the current nodes. Screenshot capture depends on resolving the browser-runtime limitation separately.

#### PDF Parsing and Summarization

Possible capabilities include text and table extraction, Markdown conversion, chunking, OCR, and artifact output. Candidate projects include [pdfminer.six](https://pdfminersix.readthedocs.io/), [PyMuPDF](https://pymupdf.readthedocs.io/), and [Apache Tika](https://tika.apache.org/).

Text extraction can be lightweight, but table recognition, OCR, embedding, and summarization are separate resource profiles. PyMuPDF licensing must be reviewed for the intended distribution. Current Apache Tika versions require a Java runtime and carry a larger memory footprint.

### Knowledge stores

#### Wikipedia Knowledge Store

Possible capabilities include keyword or semantic search and article snippets with source metadata. Sources include [Wikimedia dumps](https://dumps.wikimedia.org/) and [Kiwix ZIM files](https://dumps.wikimedia.org/other/kiwix/zim).

A curated Kiwix or SQLite subset is more realistic than a full Wikipedia vector database on current storage. Wikimedia now recommends content exports over legacy XML database dumps for new bulk consumers. Downloads must follow Wikimedia's User-Agent and connection policies.

#### Documentation and Knowledge Corpus Extensions

The implemented `corpus-search` baseline uses SQLite FTS5/BM25 and selectable arXiv metadata sampling.
Future adapters could add curated technical documentation, internal engineering documents, or other
approved collections. Candidate orchestration libraries include [Haystack](https://haystack.deepset.ai/), [LangChain](https://python.langchain.com/), and [Hugging Face Datasets](https://huggingface.co/docs/datasets/).

The first storage backend should be SQLite FTS5 with BM25. Optional future backends include [Qdrant](https://qdrant.tech/documentation/), [Milvus](https://milvus.io/docs), and [Weaviate](https://docs.weaviate.io/weaviate). Those vector systems are not assumed to support or fit the current nodes, but remain candidates for compatible future nodes. Embeddings may be generated locally on a qualifying accelerator node or on another capable machine and copied with the corpus.

##### Scientific corpus adapters

A link-oriented scientific corpus stores approved metadata and external source links rather than mirroring every paper. It may store titles, abstracts, authors, identifiers, dates, categories, licenses, URLs, and optional externally generated embeddings. Storage size depends on corpus scope and can range from megabytes to terabytes.

Every adapter must record provenance, refresh date, source terms, and per-field license constraints. Storing links does not automatically remove copyright or database-right obligations for copied abstracts and metadata.

1. **ArXiv metadata**: title, authors, abstract, categories, DOI, links, and submission history. Use the current [ArXiv bulk-data documentation](https://info.arxiv.org/help/bulk_data.html).
2. **PubMed/Medline metadata**: title, abstract where licensed, authors, journal, PMID, DOI, and PubMed URL. See [PubMed data access](https://pubmed.ncbi.nlm.nih.gov/help/#download-pubmed-data).
3. **Semantic Scholar/S2ORC**: paper metadata, abstracts, identifiers, URLs, and citation relationships. Current bulk access is through the [Semantic Scholar datasets API](https://api.semanticscholar.org/api-docs/datasets) and requires an API key; older standalone S2ORC downloads are no longer the recommended path.
4. **Crossref/DOI metadata**: DOI resolution, titles, contributors, publishers, dates, URLs, licenses, and relationships. Prefer the [Crossref REST API](https://www.crossref.org/documentation/retrieve-metadata/rest-api/) for incremental ingestion. Some deposited abstracts may have separate rights.
5. **Conference proceedings**: venue-specific records for NeurIPS, ICML, ICLR, ACL, CVPR, SIGGRAPH, and other selected conferences. Potential sources include [OpenReview](https://docs.openreview.net/) and the [ACL Anthology data API](https://aclanthology.org/faq/api/).
6. **Dataset catalogs**: dataset names, descriptions, tags, licenses, download links, and related papers. Potential sources include the [Hugging Face Hub API](https://huggingface.co/docs/hub/api), [Kaggle API](https://github.com/Kaggle/kaggle-api), and available Papers with Code exports or APIs.
7. **Code repository metadata**: repository name, description, topics, URL, license, activity, and popularity metadata. Potential sources include the [GitHub REST API](https://docs.github.com/en/rest) and [GitHub Archive](https://www.gharchive.org/).

Later retrieval modes may add vectors or hybrid ranking, but SQLite FTS5/BM25 is the appropriate baseline for small nodes.

### Specialized utilities

#### Regex, Parsing, and Extraction

Text Tools provides regex extraction, replacement and testing, structured-value extraction, and bounded
parsing and conversion for JSON, JSONL, CSV, YAML, TOML, INI, XML, HTML, dotenv, and `key=value` logs,
across twelve strictly-dispatched operation categories. Future versions may add domain-specific parsers
and artifact inputs after the shared artifact API exists.

#### Geospatial Compute

Possible capabilities include GeoJSON operations, distance calculations, spatial joins, routing, and coordinate transforms. Candidate technologies include [Shapely](https://shapely.readthedocs.io/), [GeoPandas](https://geopandas.org/), and [PostGIS](https://postgis.net/). Basic Shapely operations are the likely ARMv7 starting point; PostGIS belongs on more capable persistent infrastructure.

#### Image Processing and Vision

Possible capabilities include resize, crop, format conversion, filtering, OCR, embeddings, and object detection. Candidate technologies include [Pillow](https://pillow.readthedocs.io/), [OpenCV](https://opencv.org/), [CLIP](https://github.com/openai/CLIP), and [Ultralytics YOLO](https://docs.ultralytics.com/).

Basic Pillow or ImageMagick transforms are feasible. CLIP and YOLO model inference are not practical on the current nodes and require a future accelerator or larger worker profile.

### Agent infrastructure

#### General Job Queue and Task Runner

Trusted module lifecycle work now returns a job ID and exposes status, cancel, logs, result, and cleanup
through a lightweight systemd runner. General caller-defined jobs and arbitrary command submission are
not supported. Candidate technologies for a broader queue include [Celery](https://docs.celeryq.dev/), [RQ](https://python-rq.org/), and [Dramatiq](https://dramatiq.io/).

For the current cluster, a small SQLite-backed runner managed by systemd may be a better first implementation than a resident Redis deployment. Queue semantics, recovery, cancellation, quotas, and artifact ownership must be specified first.

#### Artifact Storage

Compute and job modules need a shared way to return outputs too large for MCP tool results. Future tools should support put, inspect, list, fetch, and delete with content hashes, MIME types, owner/module attribution, quotas, expiration, and path containment.

Candidate backends include the local filesystem, the existing NFS storage node, [MinIO](https://min.io/), and [IPFS](https://ipfs.tech/). Local/NFS storage is the appropriate first backend; MinIO and IPFS add services and operational cost that are not justified for the lifecycle MVP.

## Implemented Lifecycle

The repository implements validated local packages, recorded and live compatibility checks, SFTP
staging with SHA-256 verification, root-owned receipts, replicated/singleton placement, MCP-over-SSH
proxying, startup updates, durable lifecycle jobs, retained storage, explicit purge, and read-only
module/job dashboard views. Automated tests use offline corpus fixtures; live acceptance remains an
operator action because it changes a configured node and the corpus source is networked.

## Acceptance Criteria

The first release is complete when:

- an invalid module package cannot be listed or installed;
- compatibility explains why a module is compatible, incompatible, or unknown;
- install and uninstall require explicit targets and confirmation;
- `text-tools` installs successfully on one current node without assuming that all future nodes share its architecture;
- VantaMCPd lists and calls its actual MCP tools over SSH stdio;
- all inputs, outputs, errors, and execution times remain bounded;
- deployment and calls appear in the existing audit stream;
- the dashboard shows read-only installed version and health per node;
- uninstall removes the receipt and executable payload; and
- a call after uninstall fails with a clear module-not-installed error.

## Deferred Roadmap

1. Promote healthy remote tools to dynamic first-class VantaMCPd tools and send `tools/list_changed` notifications after lifecycle changes.
2. Add explicit rollback and old-version garbage-collection policies.
3. Add signed packages and an authenticated remote registry.
4. Add persistent service modules and narrowly scoped network exposure rules.
5. Extend trusted lifecycle jobs with quotas and additional allowlisted job kinds.
6. Add shared artifact storage and retention controls.
7. Add dashboard lifecycle actions only after authentication, authorization, CSRF, and confirmation UX are designed.
8. Add corpus adapters beyond the implemented sampled arXiv metadata profiles.
9. Add precomputed embeddings and vector backends only for compatible node profiles.
10. Add more capable arm64/x86-64 or accelerator-backed nodes for browser and ML modules; discover their capabilities through the same inventory and evaluate them through the same manifest contract.
