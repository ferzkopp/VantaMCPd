import {
  saveNodeHardware,
  type ClusterConfig,
  type DiskInfo,
  type DiskRole,
  type NodeHardware,
  type PartitionInfo,
  type ResolvedNode,
  type SwapDevice,
} from "./config.js";
import { parseKeyValueLines } from "./format.js";
import type { SshPool } from "./ssh.js";

/**
 * Static-ish facts only: sizes, models, core counts, OS release. Anything that changes minute to
 * minute (free memory, disk usage, temperature) belongs in cluster_status, not in the inventory.
 */
const HARDWARE_SCRIPT = String.raw`
echo "discovered_at|$(date -Is 2>/dev/null)"
echo "kernel|$(uname -r)"
echo "arch|$(uname -m)"
echo "package_arch|$(dpkg --print-architecture 2>/dev/null)"
# Brace-form shell expansions are banned here: they would be read as template-literal interpolation.
if [ -r /etc/os-release ]; then
  . /etc/os-release
  echo "os_name|$PRETTY_NAME"
  echo "os_id|$ID"
  echo "os_version|$VERSION_ID"
fi
if [ -r /etc/armbian-release ]; then
  . /etc/armbian-release
  echo "armbian|$VERSION"
  echo "board|$BOARD_NAME"
fi
[ -r /proc/device-tree/model ] && echo "board_model|$(tr -d '\000' < /proc/device-tree/model)"
echo "cpu_cores|$(nproc)"
echo "cpu_model|$(awk -F': ' '/^model name/{print $2; exit}' /proc/cpuinfo)"
echo "cpu_soc|$(awk -F': ' '/^Hardware/{print $2; exit}' /proc/cpuinfo)"
f=/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq
[ -r "$f" ] && echo "cpu_max_mhz|$(( $(cat "$f") / 1000 ))"
if command -v nvidia-smi >/dev/null 2>&1; then
  cuda_version=$(nvidia-smi 2>/dev/null | sed -n 's/.*CUDA Version: *\([0-9.]*\).*/\1/p' | head -n1)
  nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits 2>/dev/null |
    awk -F', *' -v version="$cuda_version" '{printf "accelerator|kind=gpu vendor=nvidia model=\"%s\" memory_mb=%s runtime=cuda runtime_version=%s\n",$1,$2,version}'
fi
if command -v rocm-smi >/dev/null 2>&1; then
  rocm_version=$(rocm-smi --showdriverversion 2>/dev/null | awk -F': ' '/Driver version/{print $2; exit}')
  rocm-smi --showproductname --showmeminfo vram --csv 2>/dev/null |
    awk -F',' -v version="$rocm_version" 'NR>1 && $1 ~ /^card/ {printf "accelerator|kind=gpu vendor=amd model=\"%s\" memory_mb=%d runtime=rocm runtime_version=%s\n",$3,$5/1048576,version}'
fi
awk '/^MemTotal:/{printf "mem_total_mb|%d\n",$2/1024}
     /^SwapTotal:/{printf "swap_total_mb|%d\n",$2/1024}' /proc/meminfo
# /proc/swaps sizes are in KiB; column 5 is the priority.
awk 'NR>1{printf "swapdev|name=%s type=%s size_mb=%d priority=%s\n",$1,$2,$3/1024,$5}' /proc/swaps 2>/dev/null
# Resolve UUID=/LABEL= fstab specs via /dev/disk symlinks - readable without root, unlike blkid.
awk '$0 !~ /^[ \t]*#/ && $3=="swap" {print $1}' /etc/fstab 2>/dev/null | while read -r spec; do
  dev="$spec"
  case "$spec" in
    UUID=*)  dev=$(readlink -f "/dev/disk/by-uuid/$(printf '%s' "$spec" | sed 's/^UUID=//')" 2>/dev/null) ;;
    LABEL=*) dev=$(readlink -f "/dev/disk/by-label/$(printf '%s' "$spec" | sed 's/^LABEL=//')" 2>/dev/null) ;;
  esac
  [ -n "$dev" ] || dev="$spec"
  echo "fstab_swap|$dev"
done
rootsrc=$(findmnt -no SOURCE / 2>/dev/null)
if [ -n "$rootsrc" ]; then
  echo "root_device|$rootsrc"
  echo "root_disk|$(lsblk -no PKNAME "$rootsrc" 2>/dev/null | head -n1 | tr -d ' ')"
fi
if command -v lsblk >/dev/null 2>&1; then
  # -P emits key="value" for every column, so empty fields never shift the parse.
  lsblk -bnP -o NAME,TYPE,SIZE,ROTA,RM,MODEL 2>/dev/null | grep 'TYPE="disk"' | sed 's/^/disk|/'
  lsblk -bnP -o NAME,PKNAME,TYPE,SIZE,FSTYPE,LABEL,MOUNTPOINT,PARTTYPE,PARTTYPENAME 2>/dev/null | grep 'TYPE="part"' | sed 's/^/part|/'
fi
df -PT -x tmpfs -x devtmpfs -x squashfs -x overlay 2>/dev/null |
  awk 'NR>1{printf "fs|mount=%s device=%s type=%s size_gb=%.1f\n",$7,$1,$2,$3/1048576}'
# Network shares are often x-systemd.automount, so df misses them while idle; fstab is the durable record.
if command -v findmnt >/dev/null 2>&1; then
  findmnt --fstab -t nfs,nfs4,cifs,smb3 -no TARGET,SOURCE,FSTYPE 2>/dev/null |
    awk '{printf "netfs|mount=%s source=%s type=%s\n",$1,$2,$3}'
fi
[ -r /etc/machine-id ] && echo "machine_id|$(cat /etc/machine-id)"
exit 0
`;

export interface HardwareProbe {
  node: string;
  host: string;
  hardware?: NodeHardware;
  error?: string;
}

/** Split `key=value key=value` records. Values may be quoted and may contain spaces. */
function parsePairs(line: string): Record<string, string> {
  const marks: { key: string; keyStart: number; valueStart: number }[] = [];
  const re = /(?:^|\s)([A-Za-z_]+)=/g;
  for (let m = re.exec(line); m !== null; m = re.exec(line)) {
    marks.push({ key: (m[1] as string).toLowerCase(), keyStart: m.index, valueStart: m.index + m[0].length });
  }
  const out: Record<string, string> = {};
  marks.forEach((mark, i) => {
    const end = i + 1 < marks.length ? (marks[i + 1] as (typeof marks)[number]).keyStart : line.length;
    out[mark.key] = line.slice(mark.valueStart, end).trim().replace(/^"(.*)"$/s, "$1");
  });
  return out;
}

const BYTES_PER_GB = 1024 ** 3;

/** MBR partition types that only contain logical partitions: extended CHS, extended LBA, Linux extended. */
const EXTENDED_PARTITION_TYPES = new Set(["0x5", "0x05", "0xf", "0x0f", "0x85"]);

function first(value: string | string[] | undefined): string | undefined {
  const s = Array.isArray(value) ? value[0] : value;
  return s === undefined || s === "" || s === "unknown" ? undefined : s.replace(/^"|"$/g, "");
}

function num(value: string | string[] | undefined): number | undefined {
  const s = first(value);
  if (s === undefined) return undefined;
  const n = Number(s);
  return Number.isFinite(n) ? n : undefined;
}

function bool(value: string | undefined): boolean | undefined {
  return value === "1" ? true : value === "0" ? false : undefined;
}

function list(value: string | string[] | undefined): string[] {
  return value === undefined ? [] : Array.isArray(value) ? value : [value];
}

/** Drop undefined members, and the whole object when nothing survived. */
function prune<T extends Record<string, unknown>>(obj: T): T | undefined {
  const entries = Object.entries(obj).filter(([, v]) => v !== undefined);
  return entries.length > 0 ? (Object.fromEntries(entries) as T) : undefined;
}

/**
 * Decide what each whole disk is for. Detection is reliable for system (hosts /) and swap (holds a
 * swap-formatted partition); `storage` needs the node's configured device or mountpoint, so a disk
 * with data on it that matches neither is reported as `data` for the operator to confirm.
 */
function classifyDisk(
  disk: DiskInfo,
  rootDisk: string | undefined,
  storageDevice: string | undefined,
  storageMountpoint: string | undefined,
): DiskRole {
  const parts = disk.partitions ?? [];
  if (rootDisk === disk.name || parts.some((p) => p.mountpoint === "/")) return "system";
  if (parts.some((p) => p.fsType === "swap")) return "swap";
  const storageName = storageDevice?.replace(/^\/dev\//, "");
  if (storageName && (storageName === disk.name || parts.some((p) => p.name === storageName))) return "storage";
  if (storageMountpoint && parts.some((p) => p.mountpoint === storageMountpoint)) return "storage";
  if (parts.some((p) => p.fsType)) return "data";
  return "unassigned";
}

export function parseHardware(stdout: string, node?: ResolvedNode): NodeHardware {
  const kv = parseKeyValueLines(stdout);

  const accelerators = list(kv.accelerator)
    .map((line) => parsePairs(line))
    .filter((item) => item.kind)
    .map((item) => ({
      kind: item.kind as string,
      vendor: first(item.vendor),
      model: first(item.model),
      memoryMb: num(item.memory_mb),
      runtime: first(item.runtime),
      runtimeVersion: first(item.runtime_version),
    }));

  const partitionsByDisk = new Map<string, PartitionInfo[]>();
  for (const line of list(kv.part)) {
    const p = parsePairs(line);
    if (!p.name || !p.pkname) continue;
    const info = prune({
      name: p.name,
      sizeGb: p.size ? Math.round((Number(p.size) / BYTES_PER_GB) * 10) / 10 : undefined,
      fsType: first(p.fstype),
      label: first(p.label),
      partitionType: first(p.parttypename),
      // An MBR extended partition is a container for logical partitions; it holds only an EBR
      // descriptor, so its ~1 KiB size and absent filesystem are expected rather than a fault.
      container: EXTENDED_PARTITION_TYPES.has((first(p.parttype) ?? "").toLowerCase()) || undefined,
      // lsblk reports "[SWAP]" in the MOUNTPOINT column; that is a state, not a path.
      mountpoint: p.mountpoint === "[SWAP]" ? undefined : first(p.mountpoint),
    });
    if (!info) continue;
    const existing = partitionsByDisk.get(p.pkname);
    if (existing) existing.push(info as PartitionInfo);
    else partitionsByDisk.set(p.pkname, [info as PartitionInfo]);
  }

  const rootDisk = first(kv.root_disk);
  const overrides = node?.diskRoles ?? {};

  const disks = list(kv.disk)
    .map((line) => parsePairs(line))
    .filter((p) => p.name)
    .map((p) => {
      const disk = {
        name: p.name as string,
        sizeGb: p.size ? Math.round((Number(p.size) / BYTES_PER_GB) * 10) / 10 : undefined,
        model: first(p.model),
        rotational: bool(p.rota),
        removable: bool(p.rm),
        partitions: partitionsByDisk.get(p.name as string),
      } as DiskInfo;
      const override = overrides[disk.name];
      disk.role = override ?? classifyDisk(disk, rootDisk, node?.storage?.device, node?.storage?.mountpoint);
      disk.roleSource = override ? "configured" : "detected";
      return prune(disk) as DiskInfo;
    });

  const fstabSwap = new Set(list(kv.fstab_swap).map((s) => s.trim()));
  const swapDevices = list(kv.swapdev)
    .map((line) => parsePairs(line))
    .filter((p) => p.name)
    .map((p) =>
      prune({
        name: p.name as string,
        type: first(p.type),
        sizeMb: num(p.size_mb),
        priority: num(p.priority),
        persistent: fstabSwap.has(p.name as string),
      }) as SwapDevice,
    );

  const swapPartitionMb = disks
    .flatMap((d) => d.partitions ?? [])
    .filter((p) => p.fsType === "swap")
    .reduce((sum, p) => sum + Math.round((p.sizeGb ?? 0) * 1024), 0);
  const activeSwapMb = num(kv.swap_total_mb) ?? 0;
  const swapFileMb = swapDevices.filter((s) => s.type === "file").reduce((sum, s) => sum + (s.sizeMb ?? 0), 0);

  const filesystems = list(kv.fs)
    .map((line) => parsePairs(line))
    .filter((p) => p.mount)
    .map((p) =>
      prune({
        mountpoint: p.mount as string,
        device: first(p.device),
        fsType: first(p.type),
        sizeGb: num(p.size_gb),
      }),
    )
    .filter((f): f is NonNullable<typeof f> => f !== undefined);

  const mountedPoints = new Set(filesystems.map((f) => f.mountpoint));
  const networkMounts = list(kv.netfs)
    .map((line) => parsePairs(line))
    .filter((p) => p.mount && p.source)
    .map((p) =>
      prune({
        mountpoint: p.mount as string,
        source: p.source as string,
        fsType: first(p.type),
        mounted: mountedPoints.has(p.mount as string),
      }),
    )
    .filter((m): m is NonNullable<typeof m> => m !== undefined);

  return {
    cpu: prune({
      model: first(kv.cpu_model),
      soc: first(kv.cpu_soc),
      arch: first(kv.arch),
      packageArch: first(kv.package_arch),
      cores: num(kv.cpu_cores),
      maxMhz: num(kv.cpu_max_mhz),
    }),
    memory: prune({
      totalMb: num(kv.mem_total_mb),
      swapTotalMb: activeSwapMb,
      swapDevices: swapDevices.length > 0 ? swapDevices : undefined,
      swapPotentialMb: swapPartitionMb + swapFileMb,
      swapPersistent: swapDevices.length > 0 ? swapDevices.every((s) => s.persistent !== false) : undefined,
    }),
    accelerators,
    disks: disks.length > 0 ? disks : undefined,
    filesystems: filesystems.length > 0 ? filesystems : undefined,
    networkMounts: networkMounts.length > 0 ? networkMounts : undefined,
    os: prune({
      name: first(kv.os_name),
      id: first(kv.os_id),
      version: first(kv.os_version),
      kernel: first(kv.kernel),
      armbian: first(kv.armbian),
      board: first(kv.board),
      boardModel: first(kv.board_model),
    }),
    machineId: first(kv.machine_id),
    discoveredAt: first(kv.discovered_at) ?? new Date().toISOString(),
  };
}

/** Probe the given nodes. Never throws: unreachable nodes come back with an `error`. */
export async function probeHardware(pool: SshPool, nodes: ResolvedNode[], timeoutMs = 45_000): Promise<HardwareProbe[]> {
  const byName = new Map(nodes.map((n) => [n.name, n]));
  const results = await pool.execMany(nodes, HARDWARE_SCRIPT, { timeoutMs });
  return results.map((r) => {
    if (r.error || !r.ok) {
      return { node: r.node, host: r.host, error: r.error ?? (r.stderr.trim() || `exit ${r.code}`) };
    }
    return { node: r.node, host: r.host, hardware: parseHardware(r.stdout, byName.get(r.node)) };
  });
}

/**
 * Probe, update the in-memory inventory, and (unless `save` is false) write the result back to the
 * config file so the facts survive a restart.
 */
export async function refreshHardware(
  config: ClusterConfig,
  pool: SshPool,
  nodes: ResolvedNode[],
  opts: { save?: boolean; timeoutMs?: number } = {},
): Promise<{ probes: HardwareProbe[]; saved: string[] }> {
  const probes = await probeHardware(pool, nodes, opts.timeoutMs);
  const updates = new Map<string, NodeHardware>();

  for (const probe of probes) {
    if (!probe.hardware) continue;
    const node = config.nodes.find((n) => n.name === probe.node);
    if (node) node.hardware = probe.hardware;
    updates.set(probe.node, probe.hardware);
  }

  const saved = opts.save === false ? [] : saveNodeHardware(config.configPath, updates);
  return { probes, saved };
}

/** Startup fill-in for nodes that have never been probed. Failures are logged, never fatal. */
export async function discoverMissingHardware(config: ClusterConfig, pool: SshPool): Promise<void> {
  const pending = config.nodes.filter((n) => !n.hardware);
  if (pending.length === 0) return;
  try {
    const { probes, saved } = await refreshHardware(config, pool, pending, { timeoutMs: 30_000 });
    const failed = probes.filter((p) => p.error).map((p) => p.node);
    process.stderr.write(
      `hardware discovery: ${saved.length} node(s) recorded in ${config.configPath}` +
        `${failed.length > 0 ? `, unreachable: ${failed.join(", ")}` : ""}\n`,
    );
  } catch (err) {
    process.stderr.write(`hardware discovery failed: ${(err as Error).message}\n`);
  }
}
