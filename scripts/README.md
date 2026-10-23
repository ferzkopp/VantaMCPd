# Host Setup Scripts

Run these scripts on the **MCP host**, not on managed cluster nodes. Windows uses the existing
PowerShell helpers; Linux uses the Node.js setup dispatcher through small POSIX-shell wrappers.
Node.js 20.11+, npm and an OpenSSH client must already be installed on Linux. Linux enrollment also
requires `ssh-copy-id`. No PowerShell installation is needed on Linux.

Linux support has local dispatcher, configuration and filesystem regression tests, but live Linux
enrollment, package preparation and encrypted CIFS access have not yet been validated. See
[Host setup](../docs/HostSetup.md) for manual alternatives and the support matrix.

## Shared Entry Points

These npm commands select the host implementation automatically:

| Command | Purpose |
| --- | --- |
| `npm run prereqs:check` | Check host prerequisites without installing or building |
| `npm run prereqs` | Windows prerequisite helper; Linux prerequisite checks followed by npm install/build |
| `npm run bootstrap` | Create the local inventory if missing, then stop for editing; otherwise enroll nodes |
| `npm run prepare-nodes:check` | Report missing packages without installing |
| `npm run prepare-nodes` | Install missing baseline packages on enrolled nodes |
| `npm run sources:check` | Verify enabled host-side SMB sources; no write probe |

On Linux, a missing Node.js/OpenSSH prerequisite produces setup guidance, not an automatic host
package installation. Windows retains the behavior of its PowerShell prerequisite helper. Setup is
interactive: execute enrollment in your own terminal, verify SSH fingerprints, and enter login/sudo
passwords directly into OpenSSH. Bootstrap grants the selected SSH user broad passwordless sudo using
a `visudo`-validated rule, just like the Windows helper; use only trusted cluster administration accounts.
Neither platform replaces an existing private key or local inventory. Package preparation installs
only missing dependencies and does not perform a distribution upgrade.

## Linux

Invoke wrappers with `sh`; no executable-bit change is necessary:

```bash
sh scripts/install-prereqs.sh --check
sh scripts/install-prereqs.sh
sh scripts/bootstrap.sh --nodes worker-a,storage-a
sh scripts/bootstrap.sh --nodes worker-a --verify
sh scripts/prepare-nodes.sh --nodes storage-a --check
sh scripts/prepare-nodes.sh --nodes storage-a --extra-packages tmux,htop
sh scripts/smb-source.sh --source-ids vanta-share
```

`--config /absolute/path/inventory.json` selects an inventory; otherwise `VANTA_CONFIG` or the
checkout's local inventory is used. Node and source lists are comma-separated exact configured names.
`--extra-packages` is supported only by preparation. `--install-baseline` optionally follows Linux
bootstrap with preparation. `--verify` checks existing node access without enrolling or discovering
hardware. `--verify-sources` runs only source verification during bootstrap. Linux `--check` reports
missing packages with exit status zero if the check itself succeeds; missing host executables, SSH
errors and failed installations return nonzero. Windows retains its existing check exit statuses.

Linux helpers reject unknown/action-inappropriate flags. Windows `-ResetHostKey` and `-KeyPath` do not
have Linux helper equivalents: use the documented manual host-key recovery and configure
`privateKeyPath` in the inventory instead. Linux bootstrap enrolls and discovers only the selected
nodes when `--nodes` is supplied.

SMB authentication on Linux is administrator-managed through a kernel CIFS mount. Configure
`sources[].mountPath` and follow [Linux SMB setup](../docs/Sources.md#linux-host). Vanta checks the
mount and accesses files as the MCP process user; it never invokes sudo/mount, reads a mount credential
file, or installs an SMB client on a cluster node. Source verification does not configure credentials.

## Windows

Direct PowerShell commands remain supported, including their existing native flags:

```powershell
.\scripts\install-prereqs.ps1 -Check
.\scripts\bootstrap.ps1 -Nodes worker-a -Verify
.\scripts\prepare-nodes.ps1 -Nodes storage-a -Check
.\scripts\bootstrap.ps1 -ConfigureSources -SourceIds vanta-share
.\scripts\bootstrap.ps1 -VerifySources -SourceIds vanta-share
```

Portable npm flags such as `npm run bootstrap -- --nodes worker-a --verify` are translated to native
PowerShell flags. Linux wrappers use long `--flags`; they do not accept PowerShell's `-Flags` syntax.
Windows source credentials remain user-bound DPAPI files, not Linux mount credentials.

## Files

`host-setup.mjs` owns platform dispatch and Linux setup. The paired `.sh` and `.ps1` files are host
entry points; `common.ps1` contains Windows helper functions, and `copy-assets.mjs` is the shared
platform-neutral build step. `smb-source.ps1` implements Windows source operations; the Linux source
runtime is implemented in [src/sources-linux.ts](../src/sources-linux.ts), not a node-side shell script.