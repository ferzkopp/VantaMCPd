import { z } from "zod";
import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { resolveTargets } from "../config.js";
import { errorText, renderResults, text } from "../format.js";
import { guardDestructiveDevice, q } from "../security.js";
import { targetsSchema, timeoutSchema, type ToolContext } from "./context.js";

const STATUS_SCRIPT = [
  `echo "== active swap =="`,
  `cat /proc/swaps`,
  `echo; echo "== totals (MB) =="`,
  `free -m | awk 'NR==1 || /^Swap:/'`,
  `echo; echo "== swap-formatted partitions =="`,
  `lsblk -o NAME,PATH,SIZE,FSTYPE,UUID,LABEL 2>/dev/null | awk 'NR==1 || /swap/'`,
  `echo; echo "== fstab swap entries =="`,
  `awk '$0 !~ /^[ \\t]*#/ && $3=="swap"' /etc/fstab || echo "(none)"`,
  `echo; echo "== swappiness =="`,
  `cat /proc/sys/vm/swappiness`,
].join("\n");

/** Refuse to touch the disk that carries the root filesystem, whatever its name is. */
const ROOT_DISK_GUARD = [
  `rootsrc=$(findmnt -no SOURCE /)`,
  `rootdisk=$(lsblk -no PKNAME "$rootsrc" 2>/dev/null | head -n1 | tr -d ' ')`,
];

function fstabAppend(devVar: string, mountLabel: string): string[] {
  return [
    `uuid=$(blkid -s UUID -o value ${devVar})`,
    `[ -n "$uuid" ] || { echo "no UUID on ${mountLabel} after mkswap" >&2; exit 1; }`,
    `if awk '$0 !~ /^[ \\t]*#/ && $3=="swap" {print $1}' /etc/fstab | grep -qx "UUID=$uuid"; then`,
    `  echo "fstab already has UUID=$uuid"`,
    `else`,
    `  cp -a /etc/fstab /etc/fstab.bak-$(date +%Y%m%d%H%M%S)`,
    // nofail: a missing USB stick must never stop these boards from booting.
    `  printf 'UUID=%s none swap sw,nofail 0 0\\n' "$uuid" >> /etc/fstab`,
    `  echo "added: UUID=$uuid none swap sw,nofail 0 0"`,
    `fi`,
  ];
}

export function registerSwapTools(server: McpServer, ctx: ToolContext): void {
  server.registerTool(
    "cluster_swap",
    {
      title: "Inspect and manage swap space",
      description:
        "Manage swap on the cluster nodes. These boards have 1GB of RAM, so a swap partition on an external USB disk matters.\n" +
        "- status: active swap, totals, swap-formatted partitions, fstab entries and swappiness (safe, read-only)\n" +
        "- persist: add a UUID fstab entry (sw,nofail) for every active swap device that lacks one, so it survives a reboot. Non-destructive; fstab is backed up first\n" +
        "- enable: mkswap an EXISTING partition, add it to fstab and swapon. DESTROYS that partition's contents; requires confirmDevice\n" +
        "- create: repartition a WHOLE disk as a single maximum-size swap partition, then mkswap/fstab/swapon. DESTROYS THE ENTIRE DISK; requires confirmDevice\n" +
        "- disable: swapoff a device, optionally removing its fstab entry\n" +
        "Never operates on the disk holding / and rejects /dev/mmcblk* outright.",
      inputSchema: {
        action: z.enum(["status", "persist", "enable", "create", "disable"]),
        targets: targetsSchema,
        device: z.string().optional().describe('Block device, e.g. "/dev/sdb1" for enable/disable or "/dev/sdb" for create.'),
        label: z.string().optional().describe('Swap label applied by mkswap. Default "clusterswap".'),
        swappiness: z.number().int().min(0).max(200).optional().describe("Also set vm.swappiness persistently."),
        removeFstab: z.boolean().optional().describe('For action="disable": also remove the fstab entry.'),
        confirmDevice: z
          .string()
          .optional()
          .describe('For enable/create: must exactly equal "device". Requires explicit user approval first.'),
        timeoutMs: timeoutSchema,
      },
    },
    async (args) => {
      try {
        const nodes = resolveTargets(ctx.config, args.targets);
        const label = args.label ?? "clusterswap";
        if (!/^[A-Za-z0-9_-]{1,15}$/.test(label)) throw new Error(`Invalid swap label: ${label}`);
        const timeoutMs = args.timeoutMs;

        const swappinessLines =
          args.swappiness === undefined
            ? []
            : [
                `sysctl -w vm.swappiness=${args.swappiness}`,
                `printf 'vm.swappiness=%s\\n' ${args.swappiness} > /etc/sysctl.d/60-vanta-swap.conf`,
              ];

        switch (args.action) {
          case "status": {
            const results = await ctx.pool.execMany(nodes, `${STATUS_SCRIPT} 2>&1`, { timeoutMs: timeoutMs ?? 30_000 });
            return text(renderResults("swap status", results));
          }

          case "persist": {
            const script = [
              `set -e`,
              `changed=0`,
              `for dev in $(awk 'NR>1 && $2=="partition" {print $1}' /proc/swaps); do`,
              `  uuid=$(blkid -s UUID -o value "$dev" 2>/dev/null)`,
              `  [ -n "$uuid" ] || { echo "skip $dev: no UUID"; continue; }`,
              `  if awk '$0 !~ /^[ \\t]*#/ && $3=="swap" {print $1}' /etc/fstab | grep -qx "UUID=$uuid"; then`,
              `    echo "$dev already persistent (UUID=$uuid)"`,
              `    continue`,
              `  fi`,
              `  [ "$changed" = "0" ] && cp -a /etc/fstab /etc/fstab.bak-$(date +%Y%m%d%H%M%S)`,
              `  changed=1`,
              `  printf 'UUID=%s none swap sw,nofail 0 0\\n' "$uuid" >> /etc/fstab`,
              `  echo "$dev -> added UUID=$uuid none swap sw,nofail 0 0"`,
              `done`,
              ...swappinessLines,
              `systemctl daemon-reload 2>/dev/null || true`,
              `echo "-- fstab swap entries --"`,
              `awk '$0 !~ /^[ \\t]*#/ && $3=="swap"' /etc/fstab || echo "(none)"`,
            ].join("\n");
            const results = await ctx.pool.execMany(nodes, `${script} 2>&1`, { sudo: true, timeoutMs: timeoutMs ?? 60_000 });
            return text(renderResults("persist swap in fstab", results));
          }

          case "enable":
          case "create": {
            const device = args.device;
            if (!device) throw new Error(`action="${args.action}" requires "device".`);
            guardDestructiveDevice(device);
            const whole = /^\/dev\/(sd[a-z]|nvme\d+n\d+)$/.test(device);
            if (args.action === "create" && !whole) {
              throw new Error(`action="create" repartitions a whole disk; pass e.g. /dev/sdb, not ${device}.`);
            }
            if (args.action === "enable" && whole) {
              throw new Error(`action="enable" formats an existing partition; pass e.g. /dev/sdb1, not ${device}.`);
            }
            if (args.confirmDevice !== device) {
              throw new Error(
                `Refusing to continue. This ERASES ALL DATA on ${device}` +
                  `${args.action === "create" ? " (the entire disk, including every partition on it)" : ""}.\n` +
                  `Ask the user to confirm, then call again with confirmDevice="${device}".`,
              );
            }
            if (nodes.length !== 1) {
              throw new Error(`Refusing to ${args.action} swap on ${nodes.length} nodes at once; target exactly one node.`);
            }
            const node = nodes[0]!;
            const storageDevice = node.storage?.device;
            if (storageDevice && (storageDevice === device || storageDevice.startsWith(device) || device.startsWith(storageDevice))) {
              throw new Error(`Refusing: ${device} is (part of) ${node.name}'s configured storage device ${storageDevice}.`);
            }

            const partition = args.action === "create" ? `${device}1` : device;
            const script = [
              `set -e`,
              ...ROOT_DISK_GUARD,
              `target=$(basename ${q(device)})`,
              `parent=$(lsblk -no PKNAME ${q(device)} 2>/dev/null | head -n1 | tr -d ' ')`,
              `if [ "$target" = "$rootdisk" ] || [ "$parent" = "$rootdisk" ]; then`,
              `  echo "Refusing: ${device} is on the root disk ($rootdisk)." >&2; exit 1`,
              `fi`,
              `if lsblk -nro MOUNTPOINT ${q(device)} | grep -q .; then`,
              `  echo "Refusing: a partition of ${device} is mounted." >&2; lsblk ${q(device)} >&2; exit 1`,
              `fi`,
              `for p in $(lsblk -nro NAME ${q(device)} | tail -n +2) $target; do`,
              `  swapoff "/dev/$p" 2>/dev/null || true`,
              `  umount "/dev/$p" 2>/dev/null || true`,
              `done`,
              ...(args.action === "create"
                ? [
                    `wipefs -a ${q(device)}`,
                    // Debian 12 ships sfdisk/fdisk in the separate "fdisk" package; parted is the fallback.
                    `if command -v sfdisk >/dev/null 2>&1; then`,
                    `  printf 'label: dos\\n,,82\\n' | sfdisk ${q(device)}`,
                    `elif command -v parted >/dev/null 2>&1; then`,
                    `  parted -s ${q(device)} mklabel msdos`,
                    `  parted -s ${q(device)} mkpart primary linux-swap 1MiB 100%`,
                    `  parted -s ${q(device)} set 1 swap on 2>/dev/null || true`,
                    `else`,
                    `  echo "Neither sfdisk nor parted is installed (apt-get install parted)." >&2; exit 1`,
                    `fi`,
                    `partprobe ${q(device)} 2>/dev/null || true`,
                    `udevadm settle 2>/dev/null || sleep 2`,
                    `[ -b ${q(partition)} ] || { echo "${partition} did not appear after partitioning" >&2; exit 1; }`,
                  ]
                : []),
              `mkswap -L ${q(label)} ${q(partition)}`,
              ...fstabAppend(q(partition), partition),
              `swapon ${q(partition)}`,
              ...swappinessLines,
              `echo "-- /proc/swaps --"`,
              `cat /proc/swaps`,
              `free -m | awk 'NR==1 || /^Swap:/'`,
            ].join("\n");
            const r = await ctx.pool.exec(node, `${script} 2>&1`, { sudo: true, timeoutMs: timeoutMs ?? 300_000 });
            return text(renderResults(`${args.action} swap on ${partition}`, [r]));
          }

          case "disable": {
            const device = args.device;
            if (!device) throw new Error('action="disable" requires "device".');
            guardDestructiveDevice(device);
            const script = [
              `swapoff ${q(device)} 2>&1 || echo "not active"`,
              args.removeFstab
                ? [
                    `uuid=$(blkid -s UUID -o value ${q(device)} 2>/dev/null || true)`,
                    `cp -a /etc/fstab /etc/fstab.bak-$(date +%Y%m%d%H%M%S)`,
                    `[ -n "$uuid" ] && sed -i "\\#UUID=$uuid[[:space:]]\\+none[[:space:]]\\+swap#d" /etc/fstab`,
                    `sed -i ${q(`\\#^${device}[[:space:]]#d`)} /etc/fstab`,
                    `echo "fstab entry removed"`,
                  ].join("\n")
                : `echo "fstab left unchanged"`,
              `cat /proc/swaps`,
            ].join("\n");
            const results = await ctx.pool.execMany(nodes, `${script} 2>&1`, { sudo: true, timeoutMs: timeoutMs ?? 60_000 });
            return text(renderResults(`disable swap on ${device}`, results));
          }
        }
      } catch (err) {
        return errorText(err);
      }
    },
  );
}
