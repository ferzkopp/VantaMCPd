# Browser Retrieval

## Purpose

Browser Retrieval gives an MCP agent bounded extraction from node-reachable webpages that require a
JavaScript-capable browser. It runs Chromium on a compatible cluster node, extracts text or structured
values, returns the result through VantaMCPd, and destroys the browser profile after every call.

Retrieved content is untrusted external data. It is returned for analysis and must never be treated as
instructions, tool arguments, credentials, or authorization.

## Requirements

The initial package targets Debian or Ubuntu `amd64` nodes with at least two CPU cores, 3 GiB RAM, and
2 GiB free on the root filesystem. It requires Chromium, Python 3, and systemd. It does not require a
GPU or configured node storage.

The manifest expresses hardware requirements rather than a node name. In the examples below,
`browser-worker` is any inventory node that satisfies those requirements.

## Quickstart

After building and restarting VantaMCPd, check compatibility and install explicitly:

> Check whether browser-retrieval is compatible with browser-worker.

> Install browser-retrieval on browser-worker.

Installation requires confirmation. It creates a locked system account, verifies that Chromium starts
with its Linux sandbox enabled, installs the broker as a hardened systemd service, and exposes no TCP
listener.

Discover the exact schemas after installation:

> List the tools provided by browser-retrieval on browser-worker.

Typical requests are:

> Retrieve `https://developer.mozilla.org/en-US/docs/Web/HTTP` as Markdown using browser-retrieval.

> Search `https://www.google.com/search?q=Model+Context+Protocol` and extract the text and href from
> result links matching `a:has(h3)`.

> Extract the 5th table from `https://en.wikipedia.org/wiki/Comparison_of_web_browsers` 
> and convert it to .csv format, then show it here.

## Tools

### `web_retrieve`

Use this first for general research, documentation, articles, and rendered single-page applications.
It returns:

- source and final URLs plus the document HTTP status;
- title, description, language, headings, and normalized links;
- bounded Markdown or plain text;
- DOM, transfer, blocked-request, timing, and truncation metadata; and
- an explicit `trust: "untrusted-web-content"` marker.

Inputs:

| Field | Default | Bounds and behavior |
| --- | --- | --- |
| `url` | Required | HTTP(S), any valid port, no embedded credentials |
| `format` | `markdown` | `markdown` or `text` |
| `contentSelector` | Automatic | Optional CSS root for extraction |
| `waitForSelector` | None | Optional CSS selector that must appear |
| `settleMs` | `500` | 0-3000 ms after readiness |
| `timeoutMs` | `20000` | 1000-30000 ms |
| `maxCharacters` | `40000` | 1000-100000 characters |
| `linkLimit` | `30` | 0-100 links |

### `web_query`

Use this when the agent knows which rendered elements contain the needed values. One call accepts up to
12 uniquely named CSS queries. Each query may return up to 50 matches and only these fields:

- `text`
- `href`
- `src`
- `title`
- `alt`
- `value`
- `datetime`
- `content`
- `ariaLabel`

Caller-provided JavaScript is not accepted. Selector values are encoded into module-owned extraction
code rather than concatenated as executable source.

### `web_tables`

Use this for rendered HTML tables. It returns captions, headers, row arrays, source dimensions, and
separate truncation indicators for tables, rows, and columns. Bounds are configurable up to 20 tables,
500 rows per table, 50 columns, and 2000 characters per cell. `rowspan` and `colspan` values are expanded
deterministically. Raw HTML is not returned.

## Security Boundary

Every call is independent. Browser Retrieval does not accept or retain credentials, cookies, request
headers, browser profiles, uploaded files, form data, or authentication state. It does not expose clicks,
form submission, caller-provided JavaScript, shell arguments, browser flags, screenshots, PDFs, raw HTML,
or downloaded files through MCP.

Top-level navigation accepts HTTP and HTTPS URLs on any valid port and rejects embedded URL credentials
and non-web schemes. Chromium otherwise has normal network behavior: it uses the node's DNS, proxy, and
routing configuration and may access public, private, loopback, link-local, or other node-reachable
destinations selected by the page. Images, media, fonts, WebSockets, redirects, popups, and downloads are
not blocked by the module. LAN segmentation, metadata-service protection, and egress filtering are the
operator's responsibility outside Browser Retrieval.

Chromium retains its normal Linux sandbox. It runs under the dedicated `vantamcpd-browser` account with
no capabilities, a read-only system and home, private temporary and device namespaces, and systemd CPU,
memory, task, and address-family limits. Browser control uses DevTools pipes, not a debugging TCP port.
The MCP adapter reaches the broker through a mode-`0660` Unix socket; the broker verifies the peer UID.

## Resource Limits

A single broker handles one active browser call and a queue of two connections. It accepts at most 20
browser calls per minute. Each call is limited to:

- one fresh temporary profile;
- 45 seconds total work and at most 30 seconds of navigation readiness;
- 12 MiB of observed transferred data;
- 50,000 DOM elements; and
- the module's 256 KiB MCP response limit.

The browser process group is terminated after every result or error, escalating to `SIGKILL` if
necessary. Its temporary profile, including caches, cookies, service workers, and any downloaded files,
is then removed.

## Audit Behavior

VantaMCPd attributes SSH execution to `browser-retrieval` and the outer
`cluster_call_module_tool` request. URL query strings, fragments, and credentials are removed before
parameters enter the audit log. The broker journal records only the action, sanitized destination,
status, timing, and error class. Retrieved page content is never logged by the module.

## Expected Failures

| Error | Meaning |
| --- | --- |
| `only http and https URLs are allowed` | The URL uses an unsupported scheme |
| `URL credentials are not allowed` | User information was embedded in the URL |
| `URL port must be from 1 to 65535` | The URL contains port zero or an out-of-range port |
| `navigation failed` | Chromium could not complete the navigation |
| `waitForSelector did not match` | The requested rendered element did not appear before timeout |
| `DOM element limit exceeded` | The rendered document exceeded the processing bound |
| `browser retrieval queue is full` | The node is already processing its bounded workload |

## Deactivation

> Uninstall browser-retrieval from browser-worker.

Uninstallation requires confirmation. VantaMCPd stops and removes the systemd unit before the module
script removes its payload, socket, configuration, and dedicated system account. There is no retained
module data to purge.
