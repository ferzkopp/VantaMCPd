# MCP Client Integration

## Launch Contract

VantaMCPd is a local stdio MCP server. Every compatible agent ultimately needs the same process
definition:

```text
command: node
args:    ["<absolute-repo-path>/dist/index.js"]
env:
  VANTA_CONFIG: <absolute-repo-path>/cluster.config.local.json
  VANTA_ENV_FILE: <absolute-repo-path>/.env
```

Use absolute paths unless the client explicitly supports a stable workspace variable. `VANTA_ENV_FILE`
is optional. Keep secrets in that untracked file or in the launching process environment, not in a
committed client configuration.

The client owns the stdio process lifecycle. Start or restart the server through the client after a
build or inventory change. Do not point a client at `http://127.0.0.1:7420`; that address is the human
monitoring dashboard, not an MCP transport endpoint.

## VS Code And Copilot

The repository ships [.vscode/mcp.json](../.vscode/mcp.json):

```json
{
  "servers": {
    "vanta": {
      "type": "stdio",
      "command": "node",
      "args": ["${workspaceFolder}/dist/index.js"],
      "env": {
        "VANTA_CONFIG": "${workspaceFolder}/cluster.config.local.json",
        "VANTA_ENV_FILE": "${workspaceFolder}/.env"
      }
    }
  }
}
```

Run **MCP: List Servers**, select `vanta`, then choose **Start** or **Restart**. The configuration works
when VS Code itself runs on Windows or Linux. If VS Code is attached to a remote environment, place the
configuration where VS Code launches the server on the Vanta host and ensure that environment can reach
the managed nodes.

## Claude Code

From the repository root, add a project-local stdio server. Replace the example paths with absolute
paths on the Vanta host:

```bash
claude mcp add \
  --scope local \
  --env VANTA_CONFIG=/srv/VantaMCPd/cluster.config.local.json \
  --env VANTA_ENV_FILE=/srv/VantaMCPd/.env \
  --transport stdio vanta -- node /srv/VantaMCPd/dist/index.js
```

Verify it with:

```bash
claude mcp get vanta
claude mcp list
```

Inside Claude Code, `/mcp` shows connection status and available tools. Use `--scope project` instead of
`--scope local` only when every user has a shared installation path or the resulting `.mcp.json` uses
environment-variable expansion for machine-specific absolute paths.

## Hermes Agent

Hermes reads local stdio servers from `~/.hermes/config.yaml`. Add:

```yaml
mcp_servers:
  vanta:
    command: "node"
    args:
      - "/srv/VantaMCPd/dist/index.js"
    env:
      VANTA_CONFIG: "/srv/VantaMCPd/cluster.config.local.json"
      VANTA_ENV_FILE: "/srv/VantaMCPd/.env"
    enabled: true
    supports_parallel_tool_calls: true
```

Current Hermes releases can also add and probe the server from the CLI:

```bash
hermes mcp add vanta \
  --command node \
  --env VANTA_CONFIG=/srv/VantaMCPd/cluster.config.local.json VANTA_ENV_FILE=/srv/VantaMCPd/.env \
  --args /srv/VantaMCPd/dist/index.js
hermes mcp test vanta
```

`--args` consumes the remaining arguments, so keep it last. Start a new Hermes session after changing
the configuration.

## OpenClaw

Register the local stdio process with OpenClaw’s MCP client registry:

```bash
openclaw mcp add vanta \
  --command node \
  --arg /srv/VantaMCPd/dist/index.js \
  --cwd /srv/VantaMCPd \
  --env VANTA_CONFIG=/srv/VantaMCPd/cluster.config.local.json \
  --env VANTA_ENV_FILE=/srv/VantaMCPd/.env
```

Then perform a live probe rather than relying only on saved configuration:

```bash
openclaw mcp doctor vanta --probe
```

OpenClaw can also store the equivalent object under `mcp.servers.vanta`:

```json
{
  "command": "node",
  "args": ["/srv/VantaMCPd/dist/index.js"],
  "cwd": "/srv/VantaMCPd",
  "env": {
    "VANTA_CONFIG": "/srv/VantaMCPd/cluster.config.local.json",
    "VANTA_ENV_FILE": "/srv/VantaMCPd/.env"
  }
}
```

## Other MCP Clients

For any client that supports local stdio MCP servers, translate the launch contract into its
`command`, `args`, and `env` fields. The process must run on a host that can:

- read the inventory and SSH private key;
- reach every managed node over TCP port 22;
- bind the dashboard to `127.0.0.1` on its configured monitoring port.

Clients that support only remote HTTP MCP cannot connect directly today. A native Streamable HTTP
transport with authentication is future work; do not expose the loopback dashboard as a substitute.

## First Request And Approval

After the client reports that `vanta` is connected, begin with:

> List the configured cluster nodes and their recorded hardware.

Then try a read-only status request:

> Check the status of all cluster nodes.

VantaMCPd marks destructive operations and module installation with explicit `confirm` parameters, but
the client decides how and when to present its own tool-approval UI. Review the target list and proposed
action before approving a modifying call.

## Multiple Clients

Stdio clients each launch their own VantaMCPd process, SSH connection pool, in-memory event window, and
dashboard. Module receipts live on the managed nodes and daily audit files live on the Vanta host, so
those are shared. Only one process can bind the default dashboard port `7420`. Prefer one active client
at a time. If simultaneous clients are required, give each inventory a different `monitoring.port` or
disable web monitoring for all but one process.
