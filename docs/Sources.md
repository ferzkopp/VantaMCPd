# SMB File Sources

The Windows or Linux MCP host browses approved authenticated SMB shares, imports selected files through the
artifact upload protocol, and publishes verified result artifacts back to writable shares. Nodes
continue using local artifact storage and its existing NFS distribution. No node SMB mounts or
credentials, SMB-to-NFS re-export, consumer changes, or continuous synchronization are needed.

## Configure And Authenticate

Add an inventory entry under `sources`:

```json
{
  "sources": [{
    "id": "vanta-share", "type": "smb",
    "server": "10.0.0.50", "share": "VantaShare", "root": "",
    "credentialRef": "vanta-share", "access": "read-write",
    "enabled": true, "maxImportMb": 128
  }]
}
```

The example inventory keeps this optional source disabled and read-only by default. The local test
inventory enables the approved share as read-write. `root` optionally restricts access to a directory
within the share. All tool paths are slash-separated and relative to that root. Credentials never go
in the inventory. Use a dedicated account with appropriate share AND filesystem permissions.

### Windows Host

Run this yourself in a PowerShell terminal as the Windows identity that starts MCP:

```powershell
.\scripts\bootstrap.ps1 -ConfigureSources -SourceIds vanta-share
.\scripts\bootstrap.ps1 -VerifySources -SourceIds vanta-share
```

The first command prompts for the share account and password, then checks read access. These source-only
modes do not provision nodes or require an SSH key. `-ConfigPath` selects another inventory. Credentials
use user-bound Windows DPAPI under `%LOCALAPPDATA%\VantaMCPd\sources\<credentialRef>.credential.xml`,
outside the repository, with a restricted directory ACL. They cannot simply be moved to another account
or machine. DPAPI is not isolation from arbitrary code running as the same user. Never enter secrets
in chat. Rerun configuration to rotate them; Vanta never disconnects unrelated Windows SMB sessions.

Normal bootstrap, storage-role node preparation and artifact-storage installation check source read
access and provide advisory diagnostics. Installation returns `sourceChecks`; a failed source does not
block storage setup or unrelated artifacts. Rebuild after code changes and restart MCP after inventory
changes. The Windows connector requires Windows PowerShell and the Windows SmbShare module.

### Linux Host

Linux uses an administrator-managed kernel CIFS mount, not PowerShell or DPAPI. Install the host's
`cifs-utils` package yourself, then mount the **whole approved share** at a dedicated directory such
as `/mnt/vanta-share`. Set `mountPath` on the same source entry:

```json
{
  "id": "vanta-share", "type": "smb", "server": "10.0.0.50", "share": "VantaShare",
  "root": "", "mountPath": "/mnt/vanta-share", "credentialRef": "vanta-share",
  "access": "read-write", "enabled": true, "maxImportMb": 128
}
```

`credentialRef` remains the shared inventory identifier; Linux does not resolve it to a credential
file. Configure the mount separately under your own administrator identity. A typical `/etc/fstab`
entry is below; replace the numeric UID/GID with those of the MCP process user:

```fstab
//10.0.0.50/VantaShare /mnt/vanta-share cifs credentials=/etc/vantamcpd/vanta-share.credentials,vers=3.1.1,seal,uid=1000,gid=1000,file_mode=0600,dir_mode=0700,nosuid,nodev,noexec,_netdev,nofail 0 0
```

Create the mount directory and the root-owned credential file yourself using an editor such as
`sudoedit`. The credential file contains `username=...`, `password=...`, and optionally `domain=...`;
restrict it to root with mode `0600`, keep its parent directory private, and never put its contents in
chat, the inventory or the repository. Mount the configured entry in your own terminal. For read-only
use, add the mount option `ro` as well as configuring `access: "read-only"`.

Vanta reads `/proc/self/mountinfo` in the MCP process's own mount namespace. It requires the exact
configured mountpoint, matching `//server/share`, an SMB3 dialect and the `seal` encryption option.
Missing mounts, local-directory fallbacks, bind mounts, older/unencrypted SMB and nested mounts are
rejected. Mount the share before starting MCP; containers/services must see it in their own namespace.
Use `root` for a permitted subdirectory rather than mounting a share subdirectory. Windows ignores
`mountPath`; Linux requires it. Neither source tools nor setup scripts mount shares or read credentials.

Verify access after building:

```bash
sh scripts/smb-source.sh --source-ids vanta-share
# Or, on either host:
npm run sources:check
```

Browse, search, single/glob imports and conditional export use the same MCP tools on both platforms.
Linux exports use an atomic, no-replace hard-link publication followed by temporary-name removal;
the SMB server/filesystem must support hard links. The write probe tests this capability. If it fails,
imports can still work but export stays unavailable; there is no overwrite-prone copy fallback.
The existing Windows implementation uses a no-replace rename instead. All mounts and their
administrators/writers must remain trusted. Live Linux CIFS operation has not yet been validated;
automated tests exercise mount validation and filesystem behavior using injected mount metadata.

## Tool Discovery And Results

Source tools run on the MCP host, not on nodes. `cluster_list_sources` returns each source's ID,
`maxImportMb`, `writable` state and available `operations`: browse/search/stat always, import when
artifacts are enabled, and export after a successful write probe.

Omit `path` for the source root. Search matches filename substrings, not globs or contents; check
`truncated`. Single imports return `artifact.artifactId`; batches return per-file `results` with IDs at
`results[].result.artifact.artifactId`. Retry only failed paths. Pass IDs to compute modules, then
export the output artifact ID. See the
[tool reference](../modules/artifact-storage/ArtifactStorage.md#smb-source-tools).

## Conditional Export

At MCP startup, enabled read-write sources receive a small create/write/rename/delete probe in their
configured root. This is authorized by `access: "read-write"`; read-only sources are never probed for
writes. `cluster_list_sources` reports verified writability and per-source operations. The global
`cluster_export_source` tool appears only when artifacts are enabled and at least one source passes.

Refresh access with `cluster_check_source` and `writeProbe: true`. Ordinary checks are read-only.
Export rechecks writability on every call. A failed write probe/publication removes that source's
eligibility; the tool is withdrawn when no eligible sources remain. Cached discovery is advisory, not
a guarantee that permissions cannot change. Destination subdirectory ACLs may differ from the root.

Export takes `sourceId`, `artifactId`, and a relative destination `path`. Parent directories must
already exist. It downloads the immutable artifact in bounded chunks, pins reads to the selected store,
verifies chunk and whole-file SHA-256, and stages it locally. Publication uses a uniquely named file
beside the destination, checks its checksum, then publishes atomically without replacement. Existing files are
never overwritten, including when another writer wins the filename race. No overwrite option exists.

## Image Workflow

1. Search `vanta-share` with `cluster_search_source`, for example `query: ".png"`; discovery includes subfolders.
2. Import selected matches using `cluster_import_source`. Each response contains `artifact.artifactId`.
3. Discover the image-processing montage/contact-sheet operation with `cluster_list_module_tools` and pass those artifact IDs.
4. Use its output artifact ID with `cluster_export_source`, for example `path: "combined-images.png"`.
5. Inspect or re-import the published file to verify its checksum. Use a new name for repeat tests.

### Batch Imports

Use `cluster_import_sources` to select multiple files in one call:

```json
{
  "sourceId": "vanta-share",
  "patterns": ["Screenshot*.png", "Funnies/**/*.jpg"],
  "maxFiles": 32,
  "maxTotalMb": 256,
  "retentionDays": 1
}
```

Patterns are case-insensitive, source-relative globs: `*` and `?` match within a filename, `**`
crosses directories, and character classes such as `[ab]` are supported. Hidden names are excluded.
Brace expansion, extglobs, and negation are disabled. Overlapping patterns import each selected path
once. Root-only patterns do not recurse; patterns containing `/` scan recursively to at most 32 levels.
Selection scans at most 5000 entries for at most 30 seconds. Incomplete scans are rejected, not silently
imported. Narrow the configured source root for larger trees.

The default batch limit is 32 files and 256 MiB; explicit maxima are 128 files and 1024 MiB. Each file
also respects `maxImportMb`. Selection limits are checked before copying, and actual staged sizes are
checked again if files changed. Imports run sequentially with one temporary file at a time. Responses
contain `results` with per-path status and successful `result.artifact.artifactId` values. A failure
does not roll back earlier successful artifacts; `complete` indicates whether every import succeeded.

### Transfer Performance

Each artifact upload/download routes directly to the configured storage node and reuses one validated
SSH/MCP process and tool discovery across all chunks, rather than scanning the cluster and reconnecting
per chunk. Imports compute SHA-256 during SMB staging and reuse it
for the artifact protocol's full-file verification, avoiding a second local read just for hashing.
Upload chunks remain at most 512 KiB; download chunks remain 256 KiB because the current artifact
server repeats base64 in text and structured output within an 800 KB response budget. The transport
enforces its budget per response rather than over the lifetime of the reusable session.

Encryption, quotas, expiry, chunk/full checksums and no-overwrite publication remain enforced.
This is still verified copying through the artifact API, not a raw SMB-to-NFS shortcut. Rebuild and
restart the MCP host for these host-side improvements. Image-processing version `0.2.7` separately
raises side and montage-cell limits to 32768 pixels while retaining its 16 MiPixel area budget and
per-node memory/time limits; install/update that module before relying on the relaxed image dimensions.

After credential/mount setup, run the opt-in live integration test from your own terminal:

```powershell
$env:VANTA_SMB_WORKFLOW = '1'
$env:VANTA_SMB_QUERY = '.png'
node --test test/sources.test.mjs
Remove-Item Env:VANTA_SMB_WORKFLOW
```

Linux equivalent:

```bash
VANTA_SMB_WORKFLOW=1 VANTA_SMB_QUERY='.png' node --test test/sources.test.mjs
```

`VANTA_SMB_SOURCE` optionally selects a different source ID; `VANTA_CONFIG` selects an inventory.
The test selects two to four matching images, imports them, composes a montage through the installed
image-processing module, publishes a uniquely named `vanta-combined-*.png`, and re-imports it to verify
the checksum and size. It does not modify inputs, install modules, or delete published results. Input
artifacts use the store's default retention; result and verification artifacts retain for one day.
Without the opt-in variable the test is skipped, so ordinary test runs never write to live shares.

## Limits And Safety

`maxImportMb` bounds each import AND export: default 128 MiB, configurable from 1 to 512. Artifact
quotas, expiry and retention policies still apply. Transfers are cancellable foreground operations,
not durable node jobs; the Windows SMB helper has a 120-second timeout. Linux checks cancellation
between filesystem operations; a blocked kernel CIFS operation follows the administrator's mount and
network timeout policy and cannot be forcibly interrupted by Node.js. Ordinary failures clean up only the
operation's own temporary paths. A forcibly killed process or network failure can leave a uniquely
named `.vanta-*` temporary file; review those exact paths before manual cleanup. If cancellation or a
disconnect occurs around final publication, inspect the destination before retrying.

Discovery returns up to 200 entries per page, scans up to 5000 entries, and searches up to eight nested
levels with a 30-second enumeration budget. `truncated` reports incomplete results; offsets are
best-effort while directory contents/order change. Search matches filenames, not file contents.

Traversal, UNC paths, alternate data streams, device names and observed reparse points are rejected.
Windows imports exclude concurrent write/delete sharing during copying. Linux imports detect size,
mtime and ctime changes during copying but cannot lock out all remote writers. Approved shares and their writers must
remain trusted: component checks are not a race-proof sandbox against hostile directory replacement.
Imports are snapshots, not live links; provenance accompanies the response, not the artifact metadata.

## Firewall And Access

If TCP 445 is unreachable, check server power/address, SMB service, VLAN/router routing, and host
outbound rules. On the SHARE SERVER check Windows Defender Firewall's File and Printer Sharing
(SMB-In) rules and active network profile. An administrator can explicitly add a scoped rule on a
trusted private/domain network, replacing the placeholder with the MCP host's actual IP:

```powershell
New-NetFirewallRule -DisplayName 'Vanta SMB from MCP host' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 445 -RemoteAddress '<MCP-host-IP>' -Profile Private,Domain
```

Review existing rules too: a narrow rule does not restrict an existing broad allow. Never disable the
firewall, expose SMB to the Internet, or mark an untrusted network private. For Samba/appliances, use
the equivalent scoped server rule. Vanta does not change firewall rules automatically.

If TCP works, verify the share/root, stored identity, both share and filesystem ACLs, and SMB3 encryption
support. Write probes require create/write/rename/delete permissions. Encryption is mandatory; there
is no SMB1 or unencrypted fallback. Windows error 1219 means conflicting connections: review
`Get-SmbMapping` and `Get-SmbConnection` yourself without disrupting unrelated users. Raw exceptions
and credential content are deliberately excluded from tool responses.