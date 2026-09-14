# Node Setup

A managed node is a Linux machine that VantaMCPd reaches over SSH. It does not need Node.js, npm, the
VantaMCPd repository, or an MCP client. Prepare the operating system, network, and login account once;
the Vanta host handles enrollment and installs the remaining baseline packages remotely.

## Recommended Platform

| Item | Recommendation |
| --- | --- |
| Operating system | Minimal Debian 12 (bookworm) or a current Debian-based Armbian CLI image |
| Service and package tools | `systemd`, `apt`, and `dpkg` |
| Architecture | ARM or x86; individual modules declare their supported architectures |
| Network | Wired Ethernet when available, TCP port 22 reachable from the Vanta host, and a stable address |
| Account | A normal login user that can run `sudo` |
| Time | NTP synchronization enabled for reliable apt and TLS operations |

Ubuntu may work when it provides the same administration tools, but Debian and Debian-based Armbian are
the validated node platforms. Module-specific CPU, memory, disk, command, and accelerator requirements
are checked before installation.

## First Boot

1. Install a minimal or CLI image for the exact board or machine and complete its first-boot account
   setup. Apply normal OS updates before enrolling the node.
2. Set a recognizable hostname. Matching the inventory node name makes logs easier to follow:

   ```bash
   sudo hostnamectl set-hostname cluster1
   ```

3. Give the node a stable address, preferably with a DHCP reservation on the router.
4. Ensure SSH, sudo, and time synchronization are available:

   ```bash
   sudo apt-get update
   sudo apt-get install -y openssh-server sudo
   sudo systemctl enable --now ssh
   sudo timedatectl set-ntp true
   id -nG
   ```

   The final command should list `sudo` for the login account. Log out and back in after changing group
   membership.
5. From the Vanta host, confirm that the account is reachable:

   ```bash
   ssh <user>@<node-address>
   ```

Extra disks for swap or shared storage may be attached before discovery, but do not need to be
partitioned or formatted manually. VantaMCPd can classify and prepare them after enrollment.

## Inventory Values

Record these values before editing `cluster.config.local.json`:

| Value | Example | Inventory field |
| --- | --- | --- |
| Node name | `cluster1` | `nodes[].name` |
| Stable IP address or hostname | `192.168.1.101` | `nodes[].host` |
| Login user | `configure` | `defaults.user` or `nodes[].user` |
| Role | `worker` or `worker+storage` | `nodes[].role` |
| Storage device, when applicable | `/dev/sda1` | `nodes[].storage.device` |

## Next Steps

1. Add the node to the [inventory](Installation.md#2-create-and-edit-the-inventory).
2. Configure key authentication and passwordless sudo using the
   [Windows](HostSetup.md#windows-node-enrollment) or [Linux](HostSetup.md#linux-node-enrollment)
   enrollment procedure.
3. Install the [managed-node baseline packages](HostSetup.md#prepare-managed-nodes).

The Vanta host stores the dedicated SSH key and inventory. Passwords are used only during enrollment and
are entered directly into SSH and sudo prompts; VantaMCPd and the agent do not receive them.
