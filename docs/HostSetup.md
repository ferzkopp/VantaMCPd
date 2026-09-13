# Host Setup

## Roles

VantaMCPd has three distinct roles:

| Role | Responsibility |
| --- | --- |
| Agent | An MCP-capable client that discovers and calls VantaMCPd tools |
| Vanta host | Runs `node dist/index.js`, owns the inventory and SSH key, and serves the loopback monitoring UI |
| Managed node | A Debian/Armbian machine reached from the Vanta host over SSH |

For the current stdio transport, the agent must be able to start a process on the Vanta host. A desktop
agent and VantaMCPd commonly run on the same Windows or Linux machine. A cloud-only agent cannot launch
this local stdio server unless its runtime is hosted on the same network and filesystem.

## Support Matrix

“Validated” means exercised by this repository’s current development setup. “Compatible” means the
implementation and upstream client support the required stdio contract, but that combination is not in
the automated test matrix yet. Both use the same daemon; “compatible” is a test-coverage distinction,
not a reduced feature mode.

### Vanta host

| Host | Daemon | Initial node enrollment | Status |
| --- | --- | --- | --- |
| Windows 10/11 | Node.js 20.11+ | Automated PowerShell helpers | Validated |
| Linux x86-64 / ARM64 | Node.js 20.11+ | Standard SSH commands documented below | Compatible |
| macOS | Portable Node.js code path | Not documented or tested | Future |

The TypeScript daemon has no Windows shell dependency. Configuration, home-directory expansion, SSH,
stdio MCP, and the loopback dashboard use cross-platform Node.js APIs. The `.ps1` files are Windows
convenience scripts, not daemon dependencies. Native Bash equivalents remain future work.

### MCP agents

| Vanta host | VS Code/Copilot | Claude Code | Hermes Agent | OpenClaw | Other local stdio clients |
| --- | --- | --- | --- | --- | --- |
| Windows 10/11 | Validated | Compatible | Compatible | Compatible | Compatible |
| Linux x86-64 / ARM64 | Compatible | Compatible | Compatible | Compatible | Compatible |

“Compatible” in this matrix means that both sides implement local stdio MCP and the exact configuration
is documented, but that host/client pair has not been exercised by this repository’s test environment.
Clients that expose only remote HTTP MCP cannot connect directly in the current release.

See [MCP client integration](Clients.md) for exact configurations and verification commands.

### Managed nodes

| Node OS | Status |
| --- | --- |
| Debian / Armbian based on Debian | Validated |
| Ubuntu with `apt`, `dpkg`, systemd, and GNU/Linux administration tools | Declared by compatible modules; not validated end to end |
| RPM-based Linux, BSD, Windows | Future |

CPU architecture is not a host restriction. Managed nodes may be ARM, x86, or accelerator-equipped when
the requested tool or module declares compatible requirements.

## Host Prerequisites

Install these on the Vanta host:

- Node.js 20.11 or newer and npm;
- an OpenSSH client with `ssh`, `ssh-keygen`, and, for the Linux flow, `ssh-copy-id`.

Git is needed to clone or update the checkout, but not to run an existing build.

Then clone the repository and build it:

```bash
git clone <repository-url> VantaMCPd
cd VantaMCPd
npm install
npm run build
```

On Windows, `scripts/install-prereqs.ps1` checks and installs the Node.js and OpenSSH prerequisites and
runs the build:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install-prereqs.ps1
```

## Create The Inventory

Create the private inventory once, then edit it with the real node addresses, user, roles, and storage.
It is gitignored.

```bash
test -e cluster.config.local.json || cp cluster.config.example.json cluster.config.local.json
```

PowerShell equivalent:

```powershell
if (-not (Test-Path .\cluster.config.local.json)) {
    Copy-Item .\cluster.config.example.json .\cluster.config.local.json
}
```

The default private key path is `~/.ssh/vanta_cluster_ed25519`. Change
`defaults.privateKeyPath` if the host should use another key.

## Windows Node Enrollment

The Windows helper creates the key, installs it on explicit inventory nodes, validates and installs the
NOPASSWD sudo rule, and records hardware:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap.ps1
```

Useful checks:

```powershell
.\scripts\bootstrap.ps1 -Verify
.\scripts\bootstrap.ps1 -Nodes cluster4
```

Passwords are entered directly into `ssh` and `sudo`; the script does not read or retain them.

## Linux Node Enrollment

The current Linux path uses standard OpenSSH tools. Run it once per managed node, substituting the user,
host, and port from the inventory.

Create the cluster key if it does not exist:

```bash
KEY="$HOME/.ssh/vanta_cluster_ed25519"
test -f "$KEY" || ssh-keygen -t ed25519 -a 100 -N '' -C 'vantamcpd cluster' -f "$KEY"
```

Install the public key. Verify the host fingerprint shown by OpenSSH before accepting it:

```bash
ssh-copy-id -i "$KEY.pub" -p 22 configure@192.168.1.101
```

Install a sudoers rule. The temporary file is validated before the atomic root-owned installation; this
prompts for the remote sudo password once:

```bash
ssh -t -p 22 -i "$KEY" configure@192.168.1.101 \
  'tmp=$(mktemp); printf "%s ALL=(ALL) NOPASSWD:ALL\n" "$USER" > "$tmp"; \
   sudo visudo -cf "$tmp" && sudo install -m 0440 -o root -g root "$tmp" /etc/sudoers.d/99-vanta; \
   rc=$?; rm -f "$tmp"; exit $rc'
```

Verify key login and non-interactive sudo:

```bash
ssh -p 22 -i "$KEY" -o BatchMode=yes configure@192.168.1.101 \
  'id -un; hostname; sudo -n true; uname -srm'
```

Repeat those two remote commands for each inventory node. VantaMCPd maintains its own TOFU fingerprint
store at `~/.vanta/known_hosts.json` and pins each node on its first daemon connection. A later key change
is rejected while `strictHostKeyChecking` is enabled.

After building, record hardware through the same code path the daemon uses:

```bash
VANTA_CONFIG="$PWD/cluster.config.local.json" npm run discover -- --assign
```

Hardware is also discovered automatically when the daemon first connects, so this command is optional.

## Prepare Managed Nodes

All managed nodes need this baseline:

```text
ca-certificates curl jq lsb-release procps iproute2 usbutils nfs-common parted
```

Storage nodes additionally need:

```text
e2fsprogs nfs-kernel-server smartmontools
```

On Windows, install only missing packages with:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\prepare-nodes.ps1
```

From Linux, run the following once per ordinary node after enrollment:

```bash
ssh -p 22 -i "$KEY" configure@192.168.1.101 \
  'sudo -n env DEBIAN_FRONTEND=noninteractive apt-get update -q -o DPkg::Lock::Timeout=300 && \
   sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y -q \
     -o DPkg::Lock::Timeout=300 -o Dpkg::Use-Pty=0 \
     -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold \
     ca-certificates curl jq lsb-release procps iproute2 usbutils nfs-common parted </dev/null'
```

Append `e2fsprogs nfs-kernel-server smartmontools` for storage nodes. Packages already installed are left
in place. This prepares dependencies only; it does not perform a distribution upgrade.

## Start Through An Agent

Do not normally run `npm start` in a separate terminal. A local stdio MCP client owns the child process
and starts `node <absolute-path>/dist/index.js` itself. Configure the client as described in
[Clients.md](Clients.md), then ask it to list the cluster nodes.

The dashboard is available on the Vanta host at <http://127.0.0.1:7420> while the MCP process is running.
Because it binds to loopback, open it on that host or use a deliberate SSH tunnel; it is not exposed to
the LAN.
