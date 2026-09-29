import { aptGet } from "./apt.js";
import type { ResolvedNode } from "./config.js";
import { q } from "./security.js";

export function nfsClientMountScript(storageNode: ResolvedNode, mountpoint = storageNode.storage?.mountpoint): string {
  if (!mountpoint) throw new Error(`No NFS mountpoint configured on ${storageNode.name}.`);
  const remote = `${storageNode.host}:${mountpoint}`;
  return [
    "set -e",
    `mountpoint=${q(mountpoint)}`,
    `remote=${q(remote)}`,
    `source=$(findmnt -rn -t nfs,nfs4 -o SOURCE -M "$mountpoint" 2>/dev/null || true)`,
    `if [ "$source" = "$remote" ]; then exit 0; fi`,
    `if [ -n "$source" ] || { findmnt -rn -M "$mountpoint" >/dev/null 2>&1 && ! findmnt -rn -t autofs -M "$mountpoint" >/dev/null 2>&1; }; then`,
    `  echo "Refusing to replace an existing mount at $mountpoint" >&2; exit 1`,
    `fi`,
    `fstab_source=$(awk -v target="$mountpoint" '$1 !~ /^#/ && $2 == target { print $1; exit }' /etc/fstab)`,
    `if [ -n "$fstab_source" ] && [ "$fstab_source" != "$remote" ]; then`,
    `  echo "Refusing to replace an existing fstab entry at $mountpoint" >&2; exit 1`,
    `fi`,
    `dpkg -s nfs-common >/dev/null 2>&1 || ${aptGet("install", { packages: ["nfs-common"] })}`,
    `mkdir -p "$mountpoint"`,
    `if [ -z "$fstab_source" ]; then`,
    `  cp -a /etc/fstab /etc/fstab.bak-$(date +%Y%m%d%H%M%S)`,
    `  printf '%s\\n' "$remote $mountpoint nfs _netdev,nofail,soft,timeo=100,retrans=3,x-systemd.automount,x-systemd.idle-timeout=600,x-systemd.mount-timeout=30 0 0" >> /etc/fstab`,
    `fi`,
    `systemctl daemon-reload`,
    `systemctl start "$(systemd-escape -p --suffix=automount "$mountpoint")" || mount "$mountpoint"`,
    `ls "$mountpoint" >/dev/null`,
    `source=$(findmnt -rn -t nfs,nfs4 -o SOURCE -M "$mountpoint" 2>/dev/null || true)`,
    `[ "$source" = "$remote" ] || { echo "NFS mount at $mountpoint is not $remote" >&2; exit 1; }`,
    `df -hP "$mountpoint"`,
  ].join("\n");
}