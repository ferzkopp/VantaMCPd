# Installation

This guide expands the six steps in the [QuickStart](../README.md#quickstart). Run commands from the
repository root unless stated otherwise. Managed nodes also need a one-time OS and account prerequisite
before enrollment.

| Step | Action | Where it acts | What it needs from you |
| --- | --- | --- | --- |
| 1 | Install Node.js, npm, and OpenSSH; build VantaMCPd | Vanta host | Windows helper or standard Linux packages |
| 2 | Create and edit the private inventory | Vanta host | real node names, addresses, users, roles, and storage |
| 3 | Bootstrap key authentication and passwordless sudo | Vanta host and managed nodes | login and sudo password once per node |
| 4 | Install baseline packages | managed nodes | Windows helper or documented SSH command |
| 5 | Register VantaMCPd with the agent | Vanta host / agent | absolute daemon and inventory paths |
| 6 | Discover and install node modules | agent and selected nodes | explicit targets and installation approval |

The Windows and Linux support matrix and standalone enrollment commands are in
[Host setup](HostSetup.md).

## 1. Install host prerequisites and build

On every supported host, Node.js 20.11+, npm, and an OpenSSH client are required. Git is needed to
clone or update the checkout, but is not required to run an existing build.

### Windows

The Windows helper checks or installs those prerequisites and runs the build:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install-prereqs.ps1
```

It verifies and, where missing, installs:

- **Windows OpenSSH client** (`ssh`, `ssh-keygen`) via `Add-WindowsCapability`; this needs an elevated
  shell, and the script tells you the exact command if you are not an administrator.
- **Node.js LTS 20.11 or newer and npm** via `winget install OpenJS.NodeJS.LTS`. The session `PATH` is
  refreshed in-process, so no terminal restart is normally needed.
- **npm dependencies and the TypeScript build** with `npm install` and `npm run build`.
- **TCP/22 reachability** of every node in the local inventory, once it exists, and the presence of the
  cluster SSH key.

A summary table is printed and the exit code is non-zero if anything is still missing.

```powershell
.\scripts\install-prereqs.ps1 -Check            # report only, install nothing
.\scripts\install-prereqs.ps1 -SkipBuild        # skip npm install / build
.\scripts\install-prereqs.ps1 -SkipNetworkTest  # skip the cluster probe (off-site)
```

Windows manual equivalents are `winget install OpenJS.NodeJS.LTS`, plus the OpenSSH client from
Settings > System > Optional features, followed by `npm install; npm run build`.

### Linux

Install Node.js 20.11+, npm, Git, and `openssh-client` through the distribution or vendor packages,
then verify the tools and build:

```bash
node --version
ssh -V
npm install
npm run build
```

No PowerShell runtime is required for the daemon.

Other requirements are network reachability to the cluster subnet and an account with sudo access on
each node (`configure` by default).

Portable npm scripts are `npm run build`, `npm test`, `npm start`, and `npm run discover`. The
`prereqs`, `bootstrap`, and `prepare-nodes` npm scripts invoke the current Windows PowerShell helpers.

## Managed-node prerequisite

VantaMCPd manages nodes; it does not image them. Each node needs an OS, a name, and an account before
enrollment. Do this once per node.

**1. Flash the OS.** Download an [Armbian](https://www.armbian.com/download/) image for the exact board
(or Raspberry Pi OS Lite or plain Debian where applicable) and write it to the SD card with
[balenaEtcher](https://etcher.balena.io/) or Raspberry Pi Imager. Prefer a **minimal / CLI** image.
Debian 12 (bookworm) is what the tools are tested against.

**2. Complete the first boot.** Connect Ethernet and power. Log in over serial, HDMI, or SSH as the
image's default account. Armbian uses `root` / `1234`, forces a password change, and then walks through
creating a normal user. Record that username for `defaults.user` in the inventory.

**3. Set the hostname** to the name used in the inventory so logs and prompts match:

```bash
sudo hostnamectl set-hostname cluster1
sudo sed -i "s/127.0.1.1.*/127.0.1.1\tcluster1/" /etc/hosts
```

**4. Give it a stable address.** Use a DHCP reservation on the router or a static lease. The inventory
stores an IP address for each node, so it must not move.

**5. Make sure SSH is running and the account can use sudo:**

```bash
sudo systemctl enable --now ssh
sudo timedatectl set-ntp true      # apt and TLS both depend on an accurate clock
id -nG                             # must include sudo (or wheel)
sudo apt-get update && sudo apt-get -y upgrade
```

**6. Attach any extra disks**, such as a USB stick for swap or an SSD for shared storage. They do not
need to be partitioned or formatted in advance: `cluster_swap` and `cluster_storage` handle that, and
discovery classifies them automatically.

Record these values for the inventory:

| Field | Example | Goes into |
| --- | --- | --- |
| node name | `cluster1` | `nodes[].name` (match the hostname) |
| IP address | `10.0.0.11` | `nodes[].host` |
| username | `configure` | `defaults.user` (or `nodes[].user`) |
| role | `worker`, `worker+storage` | `nodes[].role` |
| storage device | `/dev/sda1` | `nodes[].storage.device` on storage nodes |

After enrollment, VantaMCPd uses a dedicated key and a NOPASSWD sudo rule; it never asks the agent for
a node password.

## 2. Create and edit the inventory

Create the private inventory without overwriting an existing one:

```powershell
# PowerShell
if (-not (Test-Path .\cluster.config.local.json)) {
  Copy-Item .\cluster.config.example.json .\cluster.config.local.json
}
```

```bash
# Bash
test -e cluster.config.local.json || cp cluster.config.example.json cluster.config.local.json
```

Set the real node names, addresses, user, roles, and storage. The file is gitignored and is the only
record of the cluster, so keep a backup outside the checkout.

## 3. Bootstrap key authentication and passwordless sudo

### Windows

The Windows helper reads the inventory, creates and deploys the SSH key, configures sudo, and records
hardware:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap.ps1
```

If `cluster.config.local.json` does not exist, the script stops before touching any device and offers to
copy the template. Edit the resulting file, then run the helper again. For each node it:

1. Generates `~/.ssh/vanta_cluster_ed25519` once and installs the public key in
   `~/.ssh/authorized_keys`. You enter the login password once per node.
2. Installs `/etc/sudoers.d/99-vanta`, validated with `visudo -c`, granting the SSH user NOPASSWD sudo.
   You enter the sudo password once per node.
3. Prints host, kernel, free disk, and free memory information, then records hardware in the inventory.

Passwords go directly to the `ssh` and `sudo` prompts. The script never reads, stores, or forwards them.

Useful variants:

```powershell
.\scripts\bootstrap.ps1 -Verify                      # check state, change nothing
.\scripts\bootstrap.ps1 -Nodes cluster4              # single node
.\scripts\bootstrap.ps1 -InstallBaseline             # also install usbutils, jq, curl, nfs-common
.\scripts\bootstrap.ps1 -ConfigPath D:\other.json    # explicit inventory, skips the gate
```

### Linux

Use the standard `ssh-keygen`, `ssh-copy-id`, and validated `visudo` flow in
[Linux node enrollment](HostSetup.md#linux-node-enrollment). It creates the same key and remote sudo
policy; the daemon itself is identical on both hosts. The Linux instructions also show how to record
hardware explicitly with `npm run discover`; otherwise, the daemon discovers it when it first connects.

## 4. Install baseline packages on the managed nodes

Enrollment handles access only. On Windows, `scripts/prepare-nodes.ps1` installs the packages used by
the tools over key-authenticated SSH:

```powershell
.\scripts\prepare-nodes.ps1 -Check      # inventory + what is missing, install nothing
.\scripts\prepare-nodes.ps1             # install the missing packages
```

| Applies to | Packages |
| --- | --- |
| every node | `ca-certificates curl jq lsb-release procps iproute2 usbutils nfs-common parted` |
| roles including `storage` | `e2fsprogs nfs-kernel-server smartmontools` |

For each node, the helper also reports OS, architecture, kernel, free disk and memory, NTP synchronization,
whether a reboot is pending, and whether passwordless sudo works. It is idempotent: installed packages
are left alone and nothing is upgraded. Use `cluster_packages` for upgrades.

```powershell
.\scripts\prepare-nodes.ps1 -Nodes cluster4 -ExtraPackages tmux,htop
```

Exit code `0` means all nodes are ready. Exit code `1` means at least one node needs attention; its
summary reason is `missing-packages`, `no-sudo`, `apt-failed`, or `unreachable`.

Linux hosts use the equivalent idempotent SSH and apt command in
[Prepare managed nodes](HostSetup.md#prepare-managed-nodes).

## 5. Register VantaMCPd with an agent

Configure the agent to launch `node <absolute-repo-path>/dist/index.js` over stdio and pass an absolute
`VANTA_CONFIG` path. See [MCP client integration](Clients.md) for VS Code/Copilot, Claude Code, Hermes
Agent, OpenClaw, and generic MCP clients.

For VS Code, [`.vscode/mcp.json`](../.vscode/mcp.json) already registers the daemon. Open the Command
Palette, select **MCP: List Servers**, select `vanta`, and then choose **Start / Restart**.

> The daemon reads the inventory once at startup. After editing `cluster.config.local.json`, or after
> completing enrollment for the first time, restart it so it loads the current hosts and keys.

## 6. Discover and install node modules

Start with these requests in the connected agent:

> List the available node modules and their deployment policies.
>
> Check whether text-tools is compatible with cluster1 and cluster2.
>
> Install text-tools on cluster1 and cluster2.
>
> List the tools provided by text-tools.

The install request requires approval before VantaMCPd calls `cluster_install_module` with
`confirm: true`. Installations always use explicit node names or tags; they never default to the entire
cluster. Replicated modules such as Text Tools may be installed on multiple compatible nodes, while
singleton modules reject a second installation.

Replicated modules route targetless calls round-robin across reachable installations. A single call runs
on one node; parallel work requires independent calls. Set an explicit `target` when work must stay on a
specific node. See [Node modules](Modules.md#deployment-and-routing) for the manifest contract and failure
behavior.

At daemon startup, validated module receipts are compared with the local catalog. Installed older
versions are upgraded automatically after hardware discovery; absent modules are not installed and
newer node versions are not downgraded. Set `defaults.autoUpdateModules` to `false` to opt out.
