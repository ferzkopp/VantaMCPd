import { z } from "zod";
import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { resolveTargets, type ResolvedNode } from "../config.js";
import { errorText, renderResults, text } from "../format.js";
import { guardDestructiveDevice, q, validateAbsPath, validateCidr, validateMountOptions } from "../security.js";
import { targetsSchema, timeoutSchema, type ToolContext } from "./context.js";

function storageNode(ctx: ToolContext, name?: string): ResolvedNode {
  if (name) {
    const [n] = resolveTargets(ctx.config, [name]);
    if (!n) throw new Error(`Unknown node: ${name}`);
    return n;
  }
  const candidates = ctx.config.nodes.filter((n) => n.storage);
  if (candidates.length === 0) throw new Error('No node in the config has a "storage" section.');
  if (candidates.length > 1) throw new Error(`Multiple storage nodes configured (${candidates.map((c) => c.name).join(", ")}); specify "node".`);
  return candidates[0] as ResolvedNode;
}

export function registerStorageTools(server: McpServer, ctx: ToolContext): void {
  server.registerTool(
    "cluster_storage",
    {
      title: "Manage the external SSD and NFS share",
      description:
        "Manage the USB SSD on the storage node and share it to the rest of the cluster over NFS.\n" +
        "- inspect: show block devices, filesystems, fstab and mounts (safe, read-only)\n" +
        "- format: DESTROYS ALL DATA on the device; requires confirmDevice to exactly match the device path and explicit user approval\n" +
        "- mount: create the mountpoint, add a UUID-based fstab entry (noatime,nofail) and mount it\n" +
        "- unmount: unmount, optionally removing the fstab entry\n" +
        "- export_nfs: install nfs-kernel-server and export the mountpoint to the configured network\n" +
        "- mount_clients: install nfs-common on the other nodes and mount the share via fstab\n" +
        "- unmount_clients: unmount the NFS share on the client nodes\n" +
        "- status: mounts, free space, exports and client visibility",
      inputSchema: {
        action: z.enum(["inspect", "format", "mount", "unmount", "export_nfs", "mount_clients", "unmount_clients", "status"]),
        node: z.string().optional().describe("Storage node name. Defaults to the only node whose role includes storage."),
        device: z.string().optional().describe("Override the block device, e.g. /dev/sda1."),
        mountpoint: z.string().optional().describe("Override the mountpoint, e.g. /mnt/ssd."),
        fsType: z.string().optional().describe('Filesystem type for format/mount. Default from config ("ext4").'),
        label: z.string().optional().describe("Filesystem label to apply on format."),
        network: z.string().optional().describe('NFS export network in CIDR form, e.g. "10.0.0.0/24" (defaults to config).'),
        exportOptions: z.string().optional().describe('NFS export options. Default "rw,sync,no_subtree_check".'),
        clientTargets: targetsSchema,
        removeFstab: z.boolean().optional().describe("For unmount actions: also remove the fstab entry."),
        confirmDevice: z.string().optional().describe('For action="format": must exactly equal the device path. Requires explicit user approval.'),
        timeoutMs: timeoutSchema,
      },
    },
    async (args) => {
      try {
        const node = storageNode(ctx, args.node);
        const cfg = node.storage;
        const device = args.device ?? cfg?.device;
        const mountpoint = args.mountpoint ?? cfg?.mountpoint ?? "/mnt/ssd";
        const fsType = args.fsType ?? cfg?.fsType ?? "ext4";
        const label = args.label ?? cfg?.label ?? "clusterssd";
        const network = validateCidr(args.network ?? cfg?.nfs.network ?? ctx.config.nfsNetwork);
        const exportOptions = validateMountOptions(args.exportOptions ?? cfg?.nfs.options ?? "rw,sync,no_subtree_check");
        validateAbsPath(mountpoint, "mountpoint");
        if (!/^[a-z0-9]{2,12}$/i.test(label)) throw new Error(`Invalid filesystem label: ${label}`);
        if (!/^(ext4|ext3|xfs|btrfs|f2fs)$/.test(fsType)) throw new Error(`Unsupported filesystem type: ${fsType}`);

        const timeoutMs = args.timeoutMs;

        switch (args.action) {
          case "inspect": {
            const script = [
              `echo "== block devices =="`,
              `lsblk -o NAME,PATH,SIZE,TYPE,FSTYPE,LABEL,UUID,MOUNTPOINT,MODEL,TRAN,ROTA 2>/dev/null || lsblk`,
              `echo; echo "== blkid =="; blkid 2>/dev/null || true`,
              `echo; echo "== usb devices =="; lsusb 2>/dev/null || echo "(usbutils not installed)"`,
              `echo; echo "== mounts =="; findmnt -t ext4,ext3,xfs,btrfs,f2fs,nfs,nfs4 2>/dev/null || mount`,
              `echo; echo "== disk usage =="; df -hPT -x tmpfs -x devtmpfs`,
              `echo; echo "== fstab =="; cat /etc/fstab`,
              `echo; echo "== recent usb/sd kernel messages =="; dmesg 2>/dev/null | grep -iE 'usb|sd [a-z]|scsi' | tail -n 20 || true`,
            ].join("\n");
            const r = await ctx.pool.exec(node, `${script} 2>&1`, { sudo: true, timeoutMs: timeoutMs ?? 60_000 });
            return text(renderResults(`storage inspect on ${node.name}`, [r]));
          }

          case "format": {
            if (!device) throw new Error("No device configured or supplied.");
            guardDestructiveDevice(device);
            if (args.confirmDevice !== device) {
              throw new Error(
                `Refusing to format. This ERASES ALL DATA on ${device}.\n` +
                  `Ask the user to confirm, then call again with confirmDevice="${device}".`,
              );
            }
            const script = [
              `set -e`,
              `if findmnt -S ${q(device)} >/dev/null 2>&1; then echo "Device is mounted; unmount first." >&2; exit 1; fi`,
              `if lsblk -no MOUNTPOINT ${q(device)} 2>/dev/null | grep -q '^/$'; then echo "Refusing: device hosts /" >&2; exit 1; fi`,
              `wipefs -a ${q(device)}`,
              `mkfs.${fsType} -F -L ${q(label)} ${q(device)}`,
              `blkid ${q(device)}`,
            ].join("\n");
            const r = await ctx.pool.exec(node, `${script} 2>&1`, { sudo: true, timeoutMs: timeoutMs ?? 900_000 });
            return text(renderResults(`format ${device} as ${fsType}`, [r]));
          }

          case "mount": {
            if (!device) throw new Error("No device configured or supplied.");
            guardDestructiveDevice(device);
            const script = [
              `set -e`,
              `uuid=$(blkid -s UUID -o value ${q(device)})`,
              `[ -n "$uuid" ] || { echo "No filesystem UUID on ${device} - format it first." >&2; exit 1; }`,
              `mkdir -p ${q(mountpoint)}`,
              `line="UUID=$uuid ${mountpoint} ${fsType} defaults,noatime,nofail,x-systemd.device-timeout=30 0 2"`,
              `if grep -q "UUID=$uuid" /etc/fstab; then`,
              `  echo "fstab already has an entry for $uuid"`,
              `else`,
              `  cp -a /etc/fstab /etc/fstab.bak-$(date +%Y%m%d%H%M%S)`,
              `  printf '%s\\n' "$line" >> /etc/fstab`,
              `  echo "added: $line"`,
              `fi`,
              `systemctl daemon-reload 2>/dev/null || true`,
              `mountpoint -q ${q(mountpoint)} || mount ${q(mountpoint)}`,
              `chown ${q(node.user)}:${q(node.user)} ${q(mountpoint)} 2>/dev/null || true`,
              `findmnt ${q(mountpoint)}`,
              `df -hP ${q(mountpoint)}`,
            ].join("\n");
            const r = await ctx.pool.exec(node, `${script} 2>&1`, { sudo: true, timeoutMs: timeoutMs ?? 120_000 });
            return text(renderResults(`mount ${device} at ${mountpoint}`, [r]));
          }

          case "unmount": {
            const script = [
              `umount ${q(mountpoint)} 2>&1 || echo "not mounted or busy"`,
              args.removeFstab
                ? `cp -a /etc/fstab /etc/fstab.bak-$(date +%Y%m%d%H%M%S); sed -i ${q(`\\# ${mountpoint} #d`)} /etc/fstab; grep -v '^#' /etc/fstab`
                : `echo "fstab left unchanged"`,
              `findmnt ${q(mountpoint)} || echo "unmounted"`,
            ].join("\n");
            const r = await ctx.pool.exec(node, `${script} 2>&1`, { sudo: true, timeoutMs: timeoutMs ?? 60_000 });
            return text(renderResults(`unmount ${mountpoint}`, [r]));
          }

          case "export_nfs": {
            const exportLine = `${mountpoint} ${network}(${exportOptions})`;
            const script = [
              `set -e`,
              `mountpoint -q ${q(mountpoint)} || { echo "${mountpoint} is not mounted - run action=mount first." >&2; exit 1; }`,
              `export DEBIAN_FRONTEND=noninteractive`,
              `dpkg -s nfs-kernel-server >/dev/null 2>&1 || apt-get install -y -o DPkg::Lock::Timeout=300 nfs-kernel-server`,
              `if grep -qF ${q(exportLine)} /etc/exports; then`,
              `  echo "export already present"`,
              `else`,
              `  cp -a /etc/exports /etc/exports.bak-$(date +%Y%m%d%H%M%S)`,
              `  printf '%s\\n' ${q(exportLine)} >> /etc/exports`,
              `  echo "added: ${exportLine}"`,
              `fi`,
              `exportfs -ra`,
              `systemctl enable --now nfs-kernel-server`,
              `exportfs -v`,
            ].join("\n");
            const r = await ctx.pool.exec(node, `${script} 2>&1`, { sudo: true, timeoutMs: timeoutMs ?? 600_000 });
            return text(renderResults(`export ${mountpoint} to ${network}`, [r]));
          }

          case "mount_clients": {
            const clients = (args.clientTargets ? resolveTargets(ctx.config, args.clientTargets) : ctx.config.nodes).filter(
              (n) => n.name !== node.name,
            );
            if (clients.length === 0) throw new Error("No client nodes to mount on.");
            const remote = `${node.host}:${mountpoint}`;
            const script = [
              `set -e`,
              `export DEBIAN_FRONTEND=noninteractive`,
              `dpkg -s nfs-common >/dev/null 2>&1 || apt-get install -y -o DPkg::Lock::Timeout=300 nfs-common`,
              `mkdir -p ${q(mountpoint)}`,
              // autofs: mount on first access. A plain boot-time NFS mount races the network coming up
              // ("mount.nfs: Network is unreachable") and nofail means it is never retried.
              `line="${remote} ${mountpoint} nfs _netdev,nofail,soft,timeo=100,retrans=3,x-systemd.automount,x-systemd.idle-timeout=600,x-systemd.mount-timeout=30 0 0"`,
              `if grep -qF ${q(remote)} /etc/fstab; then`,
              `  echo "fstab already has an entry for ${remote}"`,
              `else`,
              `  cp -a /etc/fstab /etc/fstab.bak-$(date +%Y%m%d%H%M%S)`,
              `  printf '%s\\n' "$line" >> /etc/fstab`,
              `  echo "added: $line"`,
              `fi`,
              `systemctl daemon-reload 2>/dev/null || true`,
              `systemctl start $(systemd-escape -p --suffix=automount ${q(mountpoint)}) 2>/dev/null || mount ${q(mountpoint)}`,
              `ls ${q(mountpoint)} >/dev/null 2>&1 || true`,
              `findmnt ${q(mountpoint)}`,
              `df -hP ${q(mountpoint)}`,
            ].join("\n");
            const results = await ctx.pool.execMany(clients, `${script} 2>&1`, { sudo: true, timeoutMs: timeoutMs ?? 600_000 });
            return text(renderResults(`mount ${remote} on clients`, results));
          }

          case "unmount_clients": {
            const clients = (args.clientTargets ? resolveTargets(ctx.config, args.clientTargets) : ctx.config.nodes).filter(
              (n) => n.name !== node.name,
            );
            const remote = `${node.host}:${mountpoint}`;
            const script = [
              `umount -f -l ${q(mountpoint)} 2>&1 || echo "not mounted"`,
              args.removeFstab
                ? `cp -a /etc/fstab /etc/fstab.bak-$(date +%Y%m%d%H%M%S); sed -i ${q(`\\#${remote}#d`)} /etc/fstab; echo "fstab entry removed"`
                : `echo "fstab left unchanged"`,
            ].join("\n");
            const results = await ctx.pool.execMany(clients, `${script} 2>&1`, { sudo: true, timeoutMs: timeoutMs ?? 120_000 });
            return text(renderResults(`unmount ${remote} on clients`, results));
          }

          case "status": {
            const serverScript = [
              `echo "== ${node.name} (server) =="`,
              `df -hPT ${q(mountpoint)} 2>&1 || echo "${mountpoint} not present"`,
              `findmnt ${q(mountpoint)} 2>&1 || true`,
              `echo "-- exports --"; exportfs -v 2>&1 || echo "(nfs-kernel-server not installed)"`,
              `echo "-- nfs service --"; systemctl is-active nfs-kernel-server 2>&1 || true`,
              `echo "-- disk health --"; lsblk -o NAME,SIZE,FSTYPE,MOUNTPOINT ${device ? q(device) : ""} 2>&1 || true`,
            ].join("\n");
            const serverResult = await ctx.pool.exec(node, `${serverScript} 2>&1`, { sudo: true, timeoutMs: timeoutMs ?? 60_000 });

            const clients = ctx.config.nodes.filter((n) => n.name !== node.name);
            const clientScript = [
              `findmnt -t nfs,nfs4 2>&1 || echo "no nfs mounts"`,
              `df -hP ${q(mountpoint)} 2>&1 || echo "${mountpoint} not mounted"`,
            ].join("\n");
            const clientResults = await ctx.pool.execMany(clients, `${clientScript} 2>&1`, { timeoutMs: timeoutMs ?? 30_000 });
            return text(renderResults("storage status", [serverResult, ...clientResults]));
          }
        }
      } catch (err) {
        return errorText(err);
      }
    },
  );
}
