import { existsSync, readFileSync, renameSync, unlinkSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { z } from "zod";

const SudoMode = z.enum(["nopasswd", "password", "none"]);
export type SudoMode = z.infer<typeof SudoMode>;

/** A node's job in the cluster. Each "+"-separated token also becomes a targetable tag. */
const RoleSchema = z.enum(["worker", "worker+storage", "control", "control+worker", "control+storage", "storage"]);
export type NodeRole = z.infer<typeof RoleSchema>;

const NfsSchema = z.object({
  enabled: z.boolean().default(false),
  network: z.string().optional(),
  options: z.string().default("rw,sync,no_subtree_check"),
});

const StorageSchema = z.object({
  device: z.string(),
  mountpoint: z.string().default("/mnt/ssd"),
  fsType: z.string().default("ext4"),
  label: z.string().default("clusterssd"),
  nfs: NfsSchema.default({}),
});

// Everything below is discovered on the node and written back into the inventory; every field is
// optional so a hand-written or half-populated block still loads.
const DiskRoleSchema = z.enum(["system", "swap", "storage", "data", "unassigned"]);
export type DiskRole = z.infer<typeof DiskRoleSchema>;

const SwapDeviceSchema = z.object({
  name: z.string(),
  type: z.string().optional(),
  sizeMb: z.number().int().nonnegative().optional(),
  priority: z.number().int().optional(),
  persistent: z.boolean().optional(),
});

const CpuSchema = z.object({
  model: z.string().optional(),
  soc: z.string().optional(),
  arch: z.string().optional(),
  packageArch: z.string().optional(),
  cores: z.number().int().nonnegative().optional(),
  maxMhz: z.number().nonnegative().optional(),
});

const MemorySchema = z.object({
  totalMb: z.number().int().nonnegative().optional(),
  swapTotalMb: z.number().int().nonnegative().optional(),
  swapDevices: z.array(SwapDeviceSchema).optional(),
  /** Swap that would be active if every swap-formatted partition were enabled. */
  swapPotentialMb: z.number().int().nonnegative().optional(),
  /** False when some active swap has no /etc/fstab entry, i.e. it is lost on reboot. */
  swapPersistent: z.boolean().optional(),
});

const PartitionSchema = z.object({
  name: z.string(),
  sizeGb: z.number().nonnegative().optional(),
  fsType: z.string().optional(),
  label: z.string().optional(),
  partitionType: z.string().optional(),
  /** MBR extended partition: a container for logical partitions, never a filesystem. */
  container: z.boolean().optional(),
  mountpoint: z.string().optional(),
});

const DiskSchema = z.object({
  name: z.string(),
  sizeGb: z.number().nonnegative().optional(),
  model: z.string().optional(),
  rotational: z.boolean().optional(),
  removable: z.boolean().optional(),
  role: DiskRoleSchema.optional(),
  /** "configured" means it came from the node's diskRoles map and survives a refresh. */
  roleSource: z.enum(["detected", "configured"]).optional(),
  partitions: z.array(PartitionSchema).optional(),
});

const FilesystemSchema = z.object({
  mountpoint: z.string(),
  device: z.string().optional(),
  fsType: z.string().optional(),
  sizeGb: z.number().nonnegative().optional(),
});

const OsSchema = z.object({
  name: z.string().optional(),
  id: z.string().optional(),
  version: z.string().optional(),
  kernel: z.string().optional(),
  armbian: z.string().optional(),
  board: z.string().optional(),
  boardModel: z.string().optional(),
});

const AcceleratorSchema = z.object({
  kind: z.string(),
  vendor: z.string().optional(),
  model: z.string().optional(),
  memoryMb: z.number().int().nonnegative().optional(),
  runtime: z.string().optional(),
  runtimeVersion: z.string().optional(),
});

const HardwareSchema = z.object({
  cpu: CpuSchema.optional(),
  memory: MemorySchema.optional(),
  /** Present (possibly empty) after accelerator discovery; absent in older or partial inventories. */
  accelerators: z.array(AcceleratorSchema).optional(),
  disks: z.array(DiskSchema).optional(),
  filesystems: z.array(FilesystemSchema).optional(),
  os: OsSchema.optional(),
  machineId: z.string().optional(),
  discoveredAt: z.string().optional(),
});

const NodeSchema = z.object({
  name: z.string().regex(/^[A-Za-z0-9_.-]+$/, "node name must be alphanumeric/._-"),
  host: z.string().min(1),
  port: z.number().int().positive().max(65535).optional(),
  user: z.string().optional(),
  auth: z.enum(["key", "password"]).optional(),
  privateKeyPath: z.string().optional(),
  sudo: SudoMode.optional(),
  role: RoleSchema.default("worker"),
  tags: z.array(z.string()).default([]),
  description: z.string().optional(),
  storage: StorageSchema.optional(),
  /** Manual disk-role assignments, keyed by kernel name ("sda"). Overrides detection, survives refresh. */
  diskRoles: z.record(DiskRoleSchema).optional(),
  hardware: HardwareSchema.optional(),
});

const DefaultsSchema = z.object({
  user: z.string().default("configure"),
  port: z.number().int().positive().max(65535).default(22),
  auth: z.enum(["key", "password"]).default("key"),
  privateKeyPath: z.string().default("~/.ssh/vanta_cluster_ed25519"),
  sudo: SudoMode.default("nopasswd"),
  connectTimeoutMs: z.number().int().positive().default(15_000),
  commandTimeoutMs: z.number().int().positive().default(120_000),
  maxConcurrency: z.number().int().positive().max(32).default(4),
  strictHostKeyChecking: z.boolean().default(true),
  autoDiscoverHardware: z.boolean().default(true),
  autoUpdateModules: z.boolean().default(true),
  nfsNetwork: z.string().default("192.168.0.0/16"),
});

const SecuritySchema = z.object({
  allowArbitraryCommands: z.boolean().default(true),
  requireConfirmForDangerous: z.boolean().default(true),
  maxOutputBytes: z.number().int().positive().default(200_000),
  extraDenyPatterns: z.array(z.string()).default([]),
});

const MonitoringSchema = z.object({
  /** Record every SSH interaction to ~/.vanta/logs and keep a rolling window in memory. */
  enabled: z.boolean().default(true),
  /** Serve the dashboard. Always bound to loopback - the log contains hosts, users and commands. */
  web: z.boolean().default(true),
  port: z.number().int().min(1).max(65535).default(7420),
  logDir: z.string().default("~/.vanta/logs"),
  maxEvents: z.number().int().positive().max(100_000).default(5_000),
  /** Total budget for the log directory in MB. Oldest daily files are pruned once it is exceeded. */
  maxLogMb: z.number().int().positive().max(10_000).default(64),
  /** Include a short stdout/stderr preview per event. Off by default: output can contain file contents. */
  logOutput: z.boolean().default(false),
});

const JobsSchema = z.object({
  retentionDays: z.number().int().min(1).max(365).default(7),
  pollIntervalMs: z.number().int().min(1_000).max(300_000).default(10_000),
  cancelGraceMs: z.number().int().min(1_000).max(120_000).default(5_000),
  maxLogBytes: z.number().int().min(1_024).max(100_000_000).default(1_000_000),
});

const ModuleDefaultsSchema = z.record(
  z.string().regex(/^[a-z0-9]+(?:-[a-z0-9]+)*$/),
  z.object({ installOptions: z.record(z.unknown()).default({}) }).strict(),
).default({});

const ConfigSchema = z.object({
  $schema: z.string().optional(),
  defaults: DefaultsSchema.default({}),
  security: SecuritySchema.default({}),
  monitoring: MonitoringSchema.default({}),
  jobs: JobsSchema.default({}),
  modules: ModuleDefaultsSchema,
  nodes: z.array(NodeSchema).min(1),
});

export type RawConfig = z.infer<typeof ConfigSchema>;
export type StorageConfig = z.infer<typeof StorageSchema>;
export type SecurityConfig = z.infer<typeof SecuritySchema>;
export type MonitoringConfig = z.infer<typeof MonitoringSchema>;
export type JobsConfig = z.infer<typeof JobsSchema>;
export type ModuleDefaultsConfig = z.infer<typeof ModuleDefaultsSchema>;
export type NodeHardware = z.infer<typeof HardwareSchema>;
export type AcceleratorInfo = z.infer<typeof AcceleratorSchema>;
export type DiskInfo = z.infer<typeof DiskSchema>;
export type PartitionInfo = z.infer<typeof PartitionSchema>;
export type SwapDevice = z.infer<typeof SwapDeviceSchema>;

export interface ResolvedNode {
  name: string;
  host: string;
  port: number;
  user: string;
  auth: "key" | "password";
  privateKeyPath: string;
  sudo: SudoMode;
  role: NodeRole;
  tags: string[];
  description?: string;
  storage?: StorageConfig;
  diskRoles?: Record<string, DiskRole>;
  hardware?: NodeHardware;
  connectTimeoutMs: number;
  commandTimeoutMs: number;
  strictHostKeyChecking: boolean;
}

export interface ClusterConfig {
  nodes: ResolvedNode[];
  security: SecurityConfig;
  monitoring: MonitoringConfig;
  jobs: JobsConfig;
  modules: ModuleDefaultsConfig;
  maxConcurrency: number;
  autoDiscoverHardware: boolean;
  autoUpdateModules: boolean;
  nfsNetwork: string;
  configPath: string;
  knownHostsPath: string;
}

export const ENV = {
  keyPassphrase: "VANTA_KEY_PASSPHRASE",
  sshPassword: "VANTA_SSH_PASSWORD",
  sudoPassword: "VANTA_SUDO_PASSWORD",
} as const;

/** Inventory file names, most specific first. The shipped example is deliberately not a candidate. */
export const LOCAL_CONFIG_NAME = "cluster.config.local.json";
export const EXAMPLE_CONFIG_NAME = "cluster.config.example.json";
const CONFIG_NAMES = [LOCAL_CONFIG_NAME, "cluster.config.json"];

export function expandHome(p: string): string {
  if (p === "~") return homedir();
  if (p.startsWith("~/") || p.startsWith("~\\")) return path.join(homedir(), p.slice(2));
  return p;
}

/** Minimal .env loader so we don't pull in a dependency. Never overrides existing env vars. */
export function loadEnvFile(file: string | undefined): void {
  if (!file || !existsSync(file)) return;
  for (const rawLine of readFileSync(file, "utf8").split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#")) continue;
    const eq = line.indexOf("=");
    if (eq <= 0) continue;
    const key = line.slice(0, eq).trim();
    let value = line.slice(eq + 1).trim();
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
      value = value.slice(1, -1);
    }
    if (value !== "" && process.env[key] === undefined) process.env[key] = value;
  }
}

function resolveConfigPath(explicit?: string): string {
  // Launched by an MCP client, cwd is arbitrary - also look next to the installed package.
  const packageRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
  const searchDirs = [process.cwd(), packageRoot];

  const candidates: string[] = [];
  if (explicit) candidates.push(explicit);
  if (process.env.VANTA_CONFIG) candidates.push(process.env.VANTA_CONFIG);
  for (const dir of searchDirs) {
    for (const name of CONFIG_NAMES) candidates.push(path.resolve(dir, name));
  }

  for (const c of candidates) {
    const resolved = path.resolve(expandHome(c));
    if (existsSync(resolved)) return resolved;
  }

  throw new Error(
    `No cluster inventory found.\n` +
      `Create ${LOCAL_CONFIG_NAME} from ${EXAMPLE_CONFIG_NAME}, then edit the node names, hosts and roles.\n` +
      `Or set VANTA_CONFIG to an explicit path.\n` +
      `Looked at:\n${candidates.map((c) => `  - ${c}`).join("\n")}`,
  );
}

/** "worker+storage" -> ["worker", "storage"], so roles are targetable like tags. */
function tagsForRole(role: NodeRole): string[] {
  return role.split("+");
}

export function loadConfig(explicitPath?: string): ClusterConfig {
  const configPath = resolveConfigPath(explicitPath);
  let parsedJson: unknown;
  try {
    parsedJson = JSON.parse(readFileSync(configPath, "utf8"));
  } catch (err) {
    throw new Error(`Failed to parse ${configPath}: ${(err as Error).message}`);
  }

  const result = ConfigSchema.safeParse(parsedJson);
  if (!result.success) {
    const issues = result.error.issues.map((i) => `  - ${i.path.join(".") || "(root)"}: ${i.message}`).join("\n");
    throw new Error(`Invalid cluster config (${configPath}):\n${issues}`);
  }
  const raw = result.data;
  const d = raw.defaults;

  const seen = new Set<string>();
  const nodes: ResolvedNode[] = raw.nodes.map((n) => {
    if (seen.has(n.name)) throw new Error(`Duplicate node name in config: ${n.name}`);
    seen.add(n.name);
    const storage: StorageConfig | undefined = n.storage
      ? {
          ...n.storage,
          nfs: {
            ...n.storage.nfs,
            network: n.storage.nfs.network || d.nfsNetwork,
          },
        }
      : undefined;
    return {
      name: n.name,
      host: n.host,
      port: n.port ?? d.port,
      user: n.user ?? d.user,
      auth: n.auth ?? d.auth,
      privateKeyPath: expandHome(n.privateKeyPath ?? d.privateKeyPath),
      sudo: n.sudo ?? d.sudo,
      role: n.role,
      tags: [...new Set([...tagsForRole(n.role), ...n.tags])],
      description: n.description,
      storage,
      diskRoles: n.diskRoles,
      hardware: n.hardware,
      connectTimeoutMs: d.connectTimeoutMs,
      commandTimeoutMs: d.commandTimeoutMs,
      strictHostKeyChecking: d.strictHostKeyChecking,
    };
  });

  for (const n of nodes) {
    if (n.tags.includes("storage") && !n.storage) {
      throw new Error(`Node ${n.name} has role "${n.role}" but no "storage" block in ${configPath}.`);
    }
    if (!n.tags.includes("storage") && n.storage) {
      throw new Error(
        `Node ${n.name} has a "storage" block but its role "${n.role}" does not include storage. ` +
          `Change the role in ${configPath} or remove the block; storage tools select nodes by that block.`,
      );
    }
  }

  const knownHostsPath = path.resolve(
    expandHome(process.env.VANTA_KNOWN_HOSTS ?? path.join(homedir(), ".vanta", "known_hosts.json")),
  );

  return {
    nodes,
    security: raw.security,
    monitoring: { ...raw.monitoring, logDir: path.resolve(expandHome(raw.monitoring.logDir)) },
    jobs: raw.jobs,
    modules: raw.modules,
    maxConcurrency: d.maxConcurrency,
    autoDiscoverHardware: d.autoDiscoverHardware,
    autoUpdateModules: d.autoUpdateModules,
    nfsNetwork: d.nfsNetwork,
    configPath,
    knownHostsPath,
  };
}

/**
 * Merge discovered hardware back into the inventory file, leaving every other key (including comment
 * fields) untouched. Returns the node names that were written.
 */
export function saveNodeHardware(configPath: string, updates: Map<string, NodeHardware>): string[] {
  if (updates.size === 0) return [];
  const doc = JSON.parse(readFileSync(configPath, "utf8")) as { nodes?: { name?: string; hardware?: NodeHardware }[] };
  if (!Array.isArray(doc.nodes)) throw new Error(`Cannot persist hardware: ${configPath} has no "nodes" array.`);

  const written: string[] = [];
  for (const entry of doc.nodes) {
    const hardware = entry.name === undefined ? undefined : updates.get(entry.name);
    if (!hardware) continue;
    entry.hardware = hardware;
    written.push(entry.name as string);
  }
  if (written.length > 0) writeInventory(configPath, doc);
  return written;
}

/** Persist manual disk-role assignments. Unlike `hardware`, these are never overwritten by discovery. */
export function saveNodeDiskRoles(configPath: string, updates: Map<string, Record<string, DiskRole>>): string[] {
  if (updates.size === 0) return [];
  const doc = JSON.parse(readFileSync(configPath, "utf8")) as {
    nodes?: { name?: string; diskRoles?: Record<string, DiskRole> }[];
  };
  if (!Array.isArray(doc.nodes)) throw new Error(`Cannot persist disk roles: ${configPath} has no "nodes" array.`);

  const written: string[] = [];
  for (const entry of doc.nodes) {
    const roles = entry.name === undefined ? undefined : updates.get(entry.name);
    if (!roles || Object.keys(roles).length === 0) continue;
    entry.diskRoles = { ...entry.diskRoles, ...roles };
    written.push(entry.name as string);
  }
  if (written.length > 0) writeInventory(configPath, doc);
  return written;
}

/**
 * The inventory holds the only record of the cluster, including its credentials. Serialize into a
 * sibling temporary file and rename over the original, so a crash mid-write cannot truncate it.
 */
function writeInventory(configPath: string, doc: unknown): void {
  const tmp = path.join(path.dirname(configPath), `.${path.basename(configPath)}.${process.pid}.tmp`);
  try {
    writeFileSync(tmp, `${JSON.stringify(doc, null, 2)}\n`, { encoding: "utf8", mode: 0o600 });
    renameSync(tmp, configPath);
  } catch (err) {
    try {
      if (existsSync(tmp)) unlinkSync(tmp);
    } catch {
      /* the temporary file is already gone or unreadable */
    }
    throw err;
  }
}

/** Resolve `targets` (node names, tags, or "all") to concrete nodes. */
export function resolveTargets(config: ClusterConfig, targets?: string[]): ResolvedNode[] {
  if (!targets || targets.length === 0 || targets.includes("all") || targets.includes("*")) {
    return config.nodes;
  }
  const picked = new Map<string, ResolvedNode>();
  const unknown: string[] = [];
  for (const t of targets) {
    const byName = config.nodes.find((n) => n.name === t || n.host === t);
    if (byName) {
      picked.set(byName.name, byName);
      continue;
    }
    const byTag = config.nodes.filter((n) => n.tags.includes(t));
    if (byTag.length > 0) {
      for (const n of byTag) picked.set(n.name, n);
      continue;
    }
    unknown.push(t);
  }
  if (unknown.length > 0) {
    const known = config.nodes.map((n) => n.name).join(", ");
    const tags = [...new Set(config.nodes.flatMap((n) => n.tags))].join(", ");
    throw new Error(`Unknown target(s): ${unknown.join(", ")}. Known nodes: ${known}. Known tags: ${tags}. Or "all".`);
  }
  return config.nodes.filter((n) => picked.has(n.name));
}
