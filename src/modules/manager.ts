import { randomUUID } from "node:crypto";
import path from "node:path";
import type { SFTPWrapper } from "ssh2";
import { z } from "zod";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { aptGet, aptUpdate } from "../apt.js";
import { setCurrentAuditResult } from "../audit.js";
import type { ClusterConfig, ResolvedNode } from "../config.js";
import { parseKeyValueLines } from "../format.js";
import { isTerminalJobStatus } from "../jobs/types.js";
import type { JobManager } from "../jobs/manager.js";
import { q } from "../security.js";
import { mapLimit, type ExecResult, type SshPool } from "../ssh.js";
import { loadModuleCatalog, type ModuleCatalog, type ModulePackage } from "./catalog.js";
import { evaluateCompatibility, type CompatibilityResult } from "./compatibility.js";
import { SshMcpTransport } from "./ssh-transport.js";

export interface ModuleNodeCheck {
  node: string;
  compatibility: CompatibilityResult;
  reachable?: boolean;
  diskAvailableMb?: number;
  storageAvailableMb?: number;
  storageMountpoint?: string;
  storageMounted?: boolean;
  storageWritable?: boolean;
  storageDistinctFromRoot?: boolean;
  artifactRoot?: string;
  artifactStoreReady?: boolean;
  artifactStoreWritable?: boolean;
  artifactProtocolVersion?: number;
  missingCommands?: string[];
  error?: string;
}

export interface ModuleInstallResult {
  node: string;
  ok: boolean;
  moduleId: string;
  version: string;
  state?: "installed" | "provisioning";
  jobId?: string;
  error?: string;
}

export interface ModuleUninstallResult {
  node: string;
  ok: boolean;
  moduleId: string;
  version?: string;
  removed: boolean;
  error?: string;
}

export interface ModuleToolListResult {
  node: string;
  moduleId: string;
  version: string;
  deployment: ModulePackage["manifest"]["deployment"];
  selection: "explicit" | "automatic";
  server: { name: string; version: string } | undefined;
  tools: unknown[];
}

export interface ModuleToolCallResult {
  ok: boolean;
  node: string;
  moduleId: string;
  moduleVersion: string;
  toolName: string;
  deployment: ModulePackage["manifest"]["deployment"];
  selection: "explicit" | "automatic";
  output: unknown;
}

export interface NodeModuleInventory {
  node: string;
  reachable: boolean;
  count?: number;
  modules?: string[];
  moduleVersions?: Record<string, string>;
  invalidReceipts?: string[];
  error?: string;
}

export interface ModuleUpdateResult {
  node: string;
  moduleId: string;
  fromVersion: string;
  toVersion: string;
  updated: boolean;
  provisioning?: boolean;
  jobId?: string;
  oldVersionRemoved?: boolean;
  error?: string;
}

type ModuleInventoryChangeListener = () => void;

const ARTIFACT_RELATIVE_PATH = "vantamcpd/artifacts";
const ARTIFACT_PROTOCOL_VERSION = "1";

const InstallationReceiptSchema = z.object({
  schemaVersion: z.literal(1),
  moduleId: z.string(),
  version: z.string().regex(/^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$/),
  installedAt: z.string(),
  installDirectory: z.string(),
  currentLink: z.string(),
  entrypoint: z.array(z.string()),
  files: z.array(z.object({ path: z.string(), size: z.number(), sha256: z.string() }).strict()),
  runtime: z.discriminatedUnion("mode", [
    z.object({ mode: z.literal("on-demand") }).strict(),
    z.object({ mode: z.literal("service"), systemdUnit: z.string() }).strict(),
  ]).default({ mode: "on-demand" }),
}).strict();

type InstallationReceipt = z.infer<typeof InstallationReceiptSchema>;

function resultError(result: ExecResult): string {
  return result.error ?? (result.stderr.trim() || result.stdout.trim() || `exit ${result.code}`);
}

function fastPut(sftp: SFTPWrapper, localPath: string, remotePath: string): Promise<void> {
  return new Promise((resolve, reject) => {
    sftp.fastPut(localPath, remotePath, (err) => (err ? reject(err) : resolve()));
  });
}

function normalizeModuleOutput(result: unknown): unknown {
  if (typeof result !== "object" || result === null) return result;
  const record = result as Record<string, unknown>;
  if (record.structuredContent !== undefined) return record.structuredContent;
  if (record.toolResult !== undefined) return record.toolResult;
  if (!Array.isArray(record.content) || record.content.length !== 1) return record.content;
  const item = record.content[0] as { type?: unknown; text?: unknown };
  if (item.type !== "text" || typeof item.text !== "string") return record.content;
  try {
    return JSON.parse(item.text);
  } catch {
    return item.text;
  }
}

function installOptionEnvironment(modulePackage: ModulePackage, provided: Record<string, unknown>): Record<string, string> {
  const definitions = modulePackage.manifest.installOptions;
  const unknown = Object.keys(provided).filter((name) => definitions[name] === undefined);
  if (unknown.length > 0) {
    throw new Error(`Unknown install option${unknown.length === 1 ? "" : "s"} for ${modulePackage.manifest.id}: ${unknown.join(", ")}.`);
  }

  const environment: Record<string, string> = {};
  for (const [name, definition] of Object.entries(definitions)) {
    const value = provided[name] ?? (definition.type === "string-list" ? undefined : definition.default);
    if (value === undefined) continue;
    if (definition.type === "integer") {
      if (typeof value !== "number" || !Number.isInteger(value) || value < definition.minimum || value > definition.maximum) {
        throw new Error(`Install option ${name} must be an integer from ${definition.minimum} to ${definition.maximum}.`);
      }
      environment[`VANTA_MODULE_OPTION_${name.replace(/([a-z0-9])([A-Z])/g, "$1_$2").toUpperCase()}`] = String(value);
      continue;
    }
    if (definition.type === "string") {
      if (typeof value !== "string" || !definition.values.includes(value)) {
        throw new Error(`Install option ${name} must be one of: ${definition.values.join(", ")}.`);
      }
      environment[`VANTA_MODULE_OPTION_${name.replace(/([a-z0-9])([A-Z])/g, "$1_$2").toUpperCase()}`] = value;
      continue;
    }
    if (!Array.isArray(value) || value.length < definition.minItems || value.length > definition.maxItems) {
      throw new Error(`Install option ${name} must contain from ${definition.minItems} to ${definition.maxItems} strings.`);
    }
    const pattern = new RegExp(definition.itemPattern);
    if (!value.every((item) => typeof item === "string" && pattern.test(item))) {
      throw new Error(`Install option ${name} contains an invalid value.`);
    }
    environment[`VANTA_MODULE_OPTION_${name.replace(/([a-z0-9])([A-Z])/g, "$1_$2").toUpperCase()}`] = JSON.stringify(value);
  }
  return environment;
}

export class ModuleManager {
  readonly catalog: ModuleCatalog;
  private readonly moduleMutations = new Map<string, Promise<void>>();
  private readonly routeCursors = new Map<string, number>();
  private readonly inventoryChangeListeners = new Set<ModuleInventoryChangeListener>();

  constructor(
    private readonly config: ClusterConfig,
    private readonly pool: SshPool,
    moduleRoot?: string,
    private readonly jobs?: JobManager,
  ) {
    this.catalog = loadModuleCatalog(moduleRoot);
  }

  get(moduleId: string): ModulePackage {
    const modulePackage = this.catalog.modules.find((item) => item.manifest.id === moduleId);
    if (modulePackage) return modulePackage;
    const known = this.catalog.modules.map((item) => item.manifest.id).join(", ") || "none";
    throw new Error(`Unknown module: ${moduleId}. Available modules: ${known}.`);
  }

  private artifactEnvironment(modulePackage: ModulePackage, node: ResolvedNode): Record<string, string> {
    const access = modulePackage.manifest.artifactAccess;
    if (!access || !this.config.artifacts?.enabled) return {};
    const storageNode = this.config.artifacts.storageNode
      ? this.config.nodes.find((candidate) => candidate.name === this.config.artifacts.storageNode)
      : this.config.nodes.find((candidate) => candidate.storage);
    if (!storageNode?.storage) return {};
    return {
      VANTA_ARTIFACT_ROOT: path.posix.join(storageNode.storage.mountpoint, ARTIFACT_RELATIVE_PATH),
      VANTA_ARTIFACT_PROTOCOL_VERSION: ARTIFACT_PROTOCOL_VERSION,
      VANTA_ARTIFACT_READ: access.read ? "1" : "0",
      VANTA_ARTIFACT_WRITE: access.write ? "1" : "0",
      VANTA_ARTIFACT_NODE: storageNode.name,
      VANTA_ARTIFACT_LOCAL_MOUNT: node.storage?.mountpoint ?? storageNode.storage.mountpoint,
    };
  }

  onInventoryChanged(listener: ModuleInventoryChangeListener): () => void {
    this.inventoryChangeListeners.add(listener);
    return () => this.inventoryChangeListeners.delete(listener);
  }

  async list(nodes: ResolvedNode[]): Promise<object> {
    const inventory = await this.installedModules(nodes);
    const jobInventory = this.jobs ? await this.jobs.list(nodes) : undefined;
    const byNode = new Map(inventory.map((item) => [item.node, item]));
    return {
      errors: this.catalog.errors,
      inventoryComplete: inventory.every((item) => item.reachable),
      unreachableNodes: inventory.filter((item) => !item.reachable).map((item) => item.node),
      modules: this.catalog.modules.map((item) => ({
        id: item.manifest.id,
        name: item.manifest.name,
        version: item.manifest.version,
        description: item.manifest.description,
        packageFiles: item.files.length,
        packageBytes: item.totalBytes,
        deployment: item.manifest.deployment,
        installedNodes: inventory
          .filter((state) => state.reachable && state.modules?.includes(item.manifest.id))
          .map((state) => state.node),
        installedVersions: Object.fromEntries(
          inventory
            .filter((state) => state.moduleVersions?.[item.manifest.id] !== undefined)
            .map((state) => [state.node, state.moduleVersions?.[item.manifest.id]]),
        ),
        requirements: item.manifest.compatibility,
        artifactAccess: item.manifest.artifactAccess,
        installOptions: item.manifest.installOptions,
        nodes: nodes.map((node) => {
          const state = byNode.get(node.name);
          const lifecycleJob = jobInventory?.jobs.find(
            (job) => job.targetNode === node.name && job.moduleId === item.manifest.id && !isTerminalJobStatus(job.status),
          );
          return {
            node: node.name,
            ...evaluateCompatibility(item.manifest, node),
            inventoryReachable: state?.reachable,
            installed: state?.reachable ? state.modules?.includes(item.manifest.id) ?? false : undefined,
            installedVersion: state?.moduleVersions?.[item.manifest.id],
            updateAvailable: state?.moduleVersions?.[item.manifest.id] === undefined
              ? undefined
              : compareSemanticVersions(state.moduleVersions[item.manifest.id] as string, item.manifest.version) < 0,
            inventoryError: state?.error,
            provisioningJob: lifecycleJob,
          };
        }),
      })),
    };
  }

  async check(moduleId: string, nodes: ResolvedNode[], timeoutMs = 30_000): Promise<ModuleNodeCheck[]> {
    const modulePackage = this.get(moduleId);
    const requiredCommands = modulePackage.manifest.compatibility.requiredCommands;
    const commandChecks = requiredCommands
      .map((command) => `if command -v ${q(command)} >/dev/null 2>&1; then echo ${q(`command_${command}|present`)}; else echo ${q(`command_${command}|missing`)}; fi`)
      .join("\n");
    const storageChecks = (node: ResolvedNode) => modulePackage.manifest.persistentData
      ? [
          `storage_mountpoint=${q(node.storage?.mountpoint ?? "/__vantamcpd_missing_storage__")}`,
          "if findmnt -rn -M \"$storage_mountpoint\" >/dev/null 2>&1; then echo 'storage_mounted|yes'; else echo 'storage_mounted|no'; fi",
          "echo \"storage_available_mb|$(df -Pm \"$storage_mountpoint\" 2>/dev/null | awk 'NR==2{print $4}')\"",
          "if [ -w \"$storage_mountpoint\" ]; then echo 'storage_writable|yes'; else echo 'storage_writable|no'; fi",
          "root_source=$(findmnt -rn -o SOURCE -M / 2>/dev/null || true)",
          "storage_source=$(findmnt -rn -o SOURCE -M \"$storage_mountpoint\" 2>/dev/null || true)",
          "if [ -n \"$root_source\" ] && [ \"$root_source\" != \"$storage_source\" ]; then echo 'storage_distinct|yes'; else echo 'storage_distinct|no'; fi",
        ].join("\n")
      : "";
    const artifactChecks = (node: ResolvedNode) => {
      const environment = this.artifactEnvironment(modulePackage, node);
      if (!environment.VANTA_ARTIFACT_ROOT) return "";
      return [
        `artifact_root=${q(environment.VANTA_ARTIFACT_ROOT)}`,
        "if [ -f \"$artifact_root/.store.json\" ] && [ ! -L \"$artifact_root/.store.json\" ]; then echo 'artifact_ready|yes'; else echo 'artifact_ready|no'; fi",
        "if [ -w \"$artifact_root\" ]; then echo 'artifact_writable|yes'; else echo 'artifact_writable|no'; fi",
        "echo \"artifact_protocol|$(sed -n 's/.*\\\"protocolVersion\\\":\\([0-9][0-9]*\\).*/\\1/p' \"$artifact_root/.store.json\" 2>/dev/null | head -n1)\"",
      ].join("\n");
    };
    const baseScript = `${commandChecks}\necho "disk_available_mb|$(df -Pm / | awk 'NR==2{print $4}')"`;
    const needsPerNodeScript = Boolean(
      modulePackage.manifest.persistentData ||
      (modulePackage.manifest.artifactAccess && this.config.artifacts?.enabled),
    );
    const results = needsPerNodeScript
      ? await mapLimit(nodes, this.config.maxConcurrency, (node) =>
          this.pool.exec(node, `${baseScript}\n${storageChecks(node)}\n${artifactChecks(node)}`, { timeoutMs }))
      : await this.pool.execMany(nodes, baseScript, { timeoutMs });

    return results.map((result, index) => {
      const node = nodes[index] as ResolvedNode;
      const compatibility = evaluateCompatibility(modulePackage.manifest, node);
      if (!result.ok) {
        return {
          node: node.name,
          compatibility,
          reachable: false,
          error: result.error ?? (result.stderr.trim() || `exit ${result.code}`),
        };
      }

      const values = parseKeyValueLines(result.stdout);
      const diskAvailableMb = Number(values.disk_available_mb);
      const missingCommands = requiredCommands.filter((command) => values[`command_${command}`] !== "present");
      const reasons = [...compatibility.reasons];
      if (missingCommands.length > 0) {
        reasons.push(`missing required commands: ${missingCommands.join(", ")}`);
      }
      const minDiskMb = modulePackage.manifest.compatibility.minDiskMb;
      if (minDiskMb !== undefined && Number.isFinite(diskAvailableMb) && diskAvailableMb < minDiskMb) {
        reasons.push(
          `requires ${minDiskMb} MB free on the root filesystem; node has ${diskAvailableMb} MB`,
        );
      }
      const data = modulePackage.manifest.persistentData;
      const storageAvailableMb = Number(values.storage_available_mb);
      const storageMounted = values.storage_mounted === "yes";
      const storageWritable = values.storage_writable === "yes";
      const storageDistinctFromRoot = values.storage_distinct === "yes";
      const artifactEnvironment = this.artifactEnvironment(modulePackage, node);
      const artifactProtocolVersion = Number(values.artifact_protocol);
      if (data) {
        if (!node.storage) {
          if (!reasons.includes("requires configured node-local storage")) reasons.push("requires configured node-local storage");
        } else {
          if (!storageMounted) reasons.push(`configured storage mount ${node.storage.mountpoint} is not mounted`);
          if (storageMounted && !storageDistinctFromRoot) reasons.push(`configured storage mount ${node.storage.mountpoint} resolves to the root filesystem`);
          if (storageMounted && !storageWritable) reasons.push(`configured storage mount ${node.storage.mountpoint} is not writable by ${node.user}`);
          if (Number.isFinite(storageAvailableMb) && storageAvailableMb < data.minFreeMb) {
            reasons.push(`requires ${data.minFreeMb} MB free storage; node has ${storageAvailableMb} MB`);
          }
          if (!Number.isFinite(storageAvailableMb)) reasons.push(`cannot determine free space on ${node.storage.mountpoint}`);
        }
      }
      return {
        node: node.name,
        compatibility: {
          ...compatibility,
          status: reasons.length > 0 ? "incompatible" : compatibility.status,
          reasons,
        },
        reachable: true,
        diskAvailableMb: Number.isFinite(diskAvailableMb) ? diskAvailableMb : undefined,
        storageAvailableMb: Number.isFinite(storageAvailableMb) ? storageAvailableMb : undefined,
        storageMountpoint: node.storage?.mountpoint,
        storageMounted: data ? storageMounted : undefined,
        storageWritable: data ? storageWritable : undefined,
        storageDistinctFromRoot: data ? storageDistinctFromRoot : undefined,
        artifactRoot: artifactEnvironment.VANTA_ARTIFACT_ROOT,
        artifactStoreReady: modulePackage.manifest.artifactAccess ? values.artifact_ready === "yes" && artifactProtocolVersion === Number(ARTIFACT_PROTOCOL_VERSION) : undefined,
        artifactStoreWritable: modulePackage.manifest.artifactAccess?.write ? values.artifact_writable === "yes" : undefined,
        artifactProtocolVersion: Number.isFinite(artifactProtocolVersion) ? artifactProtocolVersion : undefined,
        missingCommands,
      };
    });
  }

  async install(
    moduleId: string,
    nodes: ResolvedNode[],
    timeoutMs = 300_000,
    options: Record<string, unknown> = {},
  ): Promise<ModuleInstallResult[]> {
    return this.withModuleMutation(moduleId, async () => {
      const modulePackage = this.get(moduleId);
      const configured = this.config.modules?.[moduleId];
      // Resolved per node and before any remote work, so an invalid value fails the whole request early.
      const optionEnvironments = new Map(nodes.map((node) => [
        node.name,
        installOptionEnvironment(modulePackage, {
          ...(configured?.installOptions ?? {}),
          ...(configured?.nodes?.[node.name]?.installOptions ?? {}),
          ...options,
        }),
      ]));
      await this.assertInstallPlacement(modulePackage, nodes, timeoutMs);
      const checks = await this.check(moduleId, nodes, Math.min(timeoutMs, 30_000));
      const results = await mapLimit(nodes, this.config.maxConcurrency, async (node, index) => {
        let check = checks[index] as ModuleNodeCheck;
        if (!check.reachable) {
          return { node: node.name, ok: false, moduleId, version: modulePackage.manifest.version, error: check.error };
        }
        const commandReason = `missing required commands: ${check.missingCommands?.join(", ")}`;
        const blockingReasons = check.compatibility.reasons.filter((reason) => reason !== commandReason);
        if (blockingReasons.length > 0 || check.compatibility.unknown.length > 0) {
          const detail = [...blockingReasons, ...check.compatibility.unknown].join("; ");
          return {
            node: node.name,
            ok: false,
            moduleId,
            version: modulePackage.manifest.version,
            error: `preflight ${check.compatibility.status}: ${detail}`,
          };
        }
        if ((check.missingCommands?.length ?? 0) > 0) {
          const dependencyResult = await this.installAptDependencies(modulePackage, node, timeoutMs);
          if (!dependencyResult.ok) {
            return {
              node: node.name,
              ok: false,
              moduleId,
              version: modulePackage.manifest.version,
              error: dependencyResult.error,
            };
          }
          const recheck = await this.check(moduleId, [node], Math.min(timeoutMs, 30_000));
          check = recheck[0] as ModuleNodeCheck;
        }
        if (!check.reachable || check.compatibility.status !== "compatible") {
          const detail = check.error ?? [...check.compatibility.reasons, ...check.compatibility.unknown].join("; ");
          return {
            node: node.name,
            ok: false,
            moduleId,
            version: modulePackage.manifest.version,
            error: `post-dependency preflight ${check.compatibility.status}: ${detail}`,
          };
        }
        return this.installOnNode(modulePackage, node, timeoutMs, optionEnvironments.get(node.name) ?? {});
      });
      this.routeCursors.delete(moduleId);
      if (results.some((result) => result.ok)) this.notifyInventoryChanged();
      return results;
    });
  }

  async uninstall(moduleId: string, nodes: ResolvedNode[], timeoutMs = 300_000): Promise<ModuleUninstallResult[]> {
    return this.withModuleMutation(moduleId, async () => {
      const modulePackage = this.get(moduleId);
      await this.assertNoActiveLifecycleJobs(moduleId, nodes);
      const results = await mapLimit(
        nodes,
        this.config.maxConcurrency,
        (node) => this.uninstallOnNode(modulePackage, node, timeoutMs),
      );
      this.routeCursors.delete(moduleId);
      if (results.some((result) => result.ok && result.removed)) this.notifyInventoryChanged();
      return results;
    });
  }

  async purgeData(moduleId: string, node: ResolvedNode): Promise<{ node: string; moduleId: string; removed: boolean }> {
    return this.withModuleMutation(moduleId, async () => {
      const modulePackage = this.get(moduleId);
      const data = modulePackage.manifest.persistentData;
      if (!data) throw new Error(`Module ${moduleId} does not declare persistent data.`);
      if (!node.storage) throw new Error(`Node ${node.name} does not have configured storage.`);
      await this.assertNoActiveLifecycleJobs(moduleId, [node]);
      const inventory = await this.installedModules([node]);
      if (inventory[0]?.modules?.includes(moduleId)) {
        throw new Error(`Uninstall ${moduleId} from ${node.name} before purging its retained data.`);
      }
      const dataDirectory = path.posix.join(node.storage.mountpoint, data.relativePath);
      const marker = `${dataDirectory}/.vantamcpd-module`;
      const command =
        `set -e; if [ ! -e ${q(dataDirectory)} ]; then printf '%s' '__ABSENT__'; exit 0; fi; ` +
        `test -d ${q(dataDirectory)} && test ! -L ${q(dataDirectory)} && test -f ${q(marker)} && test ! -L ${q(marker)}; ` +
        `[ "$(cat ${q(marker)})" = ${q(moduleId)} ]; rm -rf -- ${q(dataDirectory)}; printf '%s' '__REMOVED__'`;
      const result = await this.pool.exec(node, command, { sudo: true, timeoutMs: 120_000, maxOutputBytes: 64 * 1024 });
      if (!result.ok) throw new Error(`Refusing to purge unmarked module data: ${resultError(result)}`);
      return { node: node.name, moduleId, removed: result.stdout === "__REMOVED__" };
    });
  }

  async listTools(moduleId: string, node?: ResolvedNode): Promise<ModuleToolListResult> {
    const modulePackage = this.get(moduleId);
    const selection = node === undefined ? "automatic" : "explicit";
    await this.waitForModuleMutation(moduleId);
    const selectedNode = node ?? await this.selectInstalledNode(modulePackage, false);
    return this.withClient(modulePackage, selectedNode, async (client, timeout) => {
      const result = await client.listTools({}, { timeout, maxTotalTimeout: timeout });
      return {
        node: selectedNode.name,
        moduleId,
        version: modulePackage.manifest.version,
        deployment: modulePackage.manifest.deployment,
        selection,
        server: client.getServerVersion(),
        tools: result.tools,
      };
    });
  }

  async installedModules(nodes: ResolvedNode[], timeoutMs = 15_000): Promise<NodeModuleInventory[]> {
    const command =
      "if [ -d /var/lib/vantamcpd/modules ]; then " +
      "for receipt in /var/lib/vantamcpd/modules/*.json; do " +
      "[ -f \"$receipt\" ] || continue; " +
      "printf '%s|' \"$(basename \"$receipt\" .json)\"; base64 -w 0 \"$receipt\"; printf '\\n'; done; fi";
    const results = await this.pool.execMany(nodes, command, { timeoutMs, maxOutputBytes: 64 * 1024 });
    return results.map((result, index) => {
      const node = nodes[index] as ResolvedNode;
      if (!result.ok) {
        return { node: node.name, reachable: false, error: resultError(result) };
      }
      const moduleVersions: Record<string, string> = {};
      const invalidReceipts: string[] = [];
      for (const line of result.stdout.split(/\r?\n/).filter(Boolean)) {
        const separator = line.indexOf("|");
        const id = separator < 0 ? line.trim() : line.slice(0, separator);
        if (!/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(id)) continue;
        if (separator < 0) {
          invalidReceipts.push(id);
          continue;
        }
        try {
          const receipt = InstallationReceiptSchema.parse(
            JSON.parse(Buffer.from(line.slice(separator + 1), "base64").toString("utf8")),
          );
          if (receipt.moduleId !== id) throw new Error("receipt module ID does not match its filename");
          moduleVersions[id] = receipt.version;
        } catch {
          invalidReceipts.push(id);
        }
      }
      const modules = Object.keys(moduleVersions).sort();
      return {
        node: node.name,
        reachable: true,
        count: modules.length,
        modules,
        moduleVersions,
        ...(invalidReceipts.length > 0 ? { invalidReceipts: invalidReceipts.sort() } : {}),
      };
    });
  }

  async updateOutdatedModules(timeoutMs = 300_000): Promise<ModuleUpdateResult[]> {
    const inventory = await this.installedModules(this.config.nodes);
    const results: ModuleUpdateResult[] = [];
    for (const modulePackage of this.catalog.modules) {
      const previousVersions = new Map(
        inventory.map((state) => [state.node, state.moduleVersions?.[modulePackage.manifest.id]]),
      );
      const targets = this.config.nodes.filter((node) => {
        const installedVersion = previousVersions.get(node.name);
        return installedVersion !== undefined && compareSemanticVersions(installedVersion, modulePackage.manifest.version) < 0;
      });
      if (targets.length === 0) continue;

      const installs = await this.install(modulePackage.manifest.id, targets, timeoutMs);
      for (const install of installs) {
        const fromVersion = previousVersions.get(install.node) as string;
        if (!install.ok) {
          results.push({
            node: install.node,
            moduleId: install.moduleId,
            fromVersion,
            toVersion: install.version,
            updated: false,
            error: install.error,
          });
          continue;
        }

        if (install.state === "provisioning") {
          results.push({
            node: install.node,
            moduleId: install.moduleId,
            fromVersion,
            toVersion: install.version,
            updated: false,
            provisioning: true,
            jobId: install.jobId,
          });
          continue;
        }

        const node = targets.find((candidate) => candidate.name === install.node) as ResolvedNode;
        const oldDirectory = `/opt/vantamcpd/modules/${install.moduleId}/${fromVersion}`;
        const currentLink = `/opt/vantamcpd/modules/${install.moduleId}/current`;
        const cleanup = await this.pool.exec(
          node,
          `if [ "$(readlink ${q(currentLink)})" = ${q(install.version)} ]; then rm -rf -- ${q(oldDirectory)}; else exit 1; fi`,
          { sudo: true, timeoutMs: Math.min(timeoutMs, 30_000) },
        );
        results.push({
          node: install.node,
          moduleId: install.moduleId,
          fromVersion,
          toVersion: install.version,
          updated: true,
          oldVersionRemoved: cleanup.ok,
          ...(!cleanup.ok ? { error: `updated, but old version cleanup failed: ${resultError(cleanup)}` } : {}),
        });
      }
    }
    return results;
  }

  async callTool(
    moduleId: string,
    node: ResolvedNode | undefined,
    toolName: string,
    args: Record<string, unknown>,
  ): Promise<ModuleToolCallResult> {
    const modulePackage = this.get(moduleId);
    const selection = node === undefined ? "automatic" : "explicit";
    await this.waitForModuleMutation(moduleId);
    const selectedNode = node ?? await this.selectInstalledNode(modulePackage, true);
    let output: unknown;
    const result = await this.withClient(modulePackage, selectedNode, async (client, timeout) => {
      const listed = await client.listTools({}, { timeout, maxTotalTimeout: timeout });
      if (!listed.tools.some((tool) => tool.name === toolName)) {
        // The list is already in hand, so name the alternatives rather than forcing a discovery round trip.
        const advertised = listed.tools.map((tool) => tool.name).sort().join(", ");
        throw new Error(
          `Module ${moduleId} does not advertise tool ${toolName}. Available tools: ${advertised}. ` +
          `Call cluster_list_module_tools for their argument schemas.`,
        );
      }
      const called = await client.callTool(
        { name: toolName, arguments: args },
        undefined,
        { timeout, maxTotalTimeout: timeout },
      );
      output = normalizeModuleOutput(called);
      const outputRecord = typeof output === "object" && output !== null && !Array.isArray(output)
        ? output as Record<string, unknown>
        : undefined;
      const truncationReasons = Array.isArray(outputRecord?.truncationReasons)
        ? outputRecord.truncationReasons.filter((reason): reason is string => typeof reason === "string")
        : undefined;
      setCurrentAuditResult({
        ...(typeof outputRecord?.complete === "boolean" ? { complete: outputRecord.complete } : {}),
        ...(truncationReasons === undefined ? {} : { truncationReasons }),
        responseLimitBytes: modulePackage.manifest.limits.maxOutputBytes,
      });
      return called;
    });
    return {
      ok: result.isError !== true,
      node: selectedNode.name,
      moduleId,
      moduleVersion: modulePackage.manifest.version,
      toolName,
      deployment: modulePackage.manifest.deployment,
      selection,
      output: output ?? normalizeModuleOutput(result),
    };
  }

  private async assertInstallPlacement(
    modulePackage: ModulePackage,
    nodes: ResolvedNode[],
    timeoutMs: number,
  ): Promise<void> {
    if (modulePackage.manifest.deployment.mode !== "singleton") return;
    if (nodes.length !== 1) {
      throw new Error(`Module ${modulePackage.manifest.id} is singleton and must be installed on exactly one node.`);
    }

    const inventory = await this.installedModules(this.config.nodes, Math.min(timeoutMs, 15_000));
    const unreachable = inventory.filter((item) => !item.reachable).map((item) => item.node);
    if (unreachable.length > 0) {
      throw new Error(
        `Cannot verify singleton placement for ${modulePackage.manifest.id}; unreachable nodes: ${unreachable.join(", ")}.`,
      );
    }
    const installed = inventory
      .filter((item) => item.modules?.includes(modulePackage.manifest.id))
      .map((item) => item.node);
    const target = nodes[0] as ResolvedNode;
    const elsewhere = installed.filter((nodeName) => nodeName !== target.name);
    if (elsewhere.length > 0) {
      throw new Error(
        `Module ${modulePackage.manifest.id} is singleton and is already installed on ${elsewhere.join(", ")}.`,
      );
    }
  }

  private async assertNoActiveLifecycleJobs(moduleId: string, nodes: ResolvedNode[]): Promise<void> {
    if (!this.jobs) return;
    const inventory = await this.jobs.list(nodes);
    const active = inventory.jobs.find(
      (job) => job.moduleId === moduleId && !isTerminalJobStatus(job.status),
    );
    if (active) throw new Error(`Module ${moduleId} is busy with job ${active.jobId} (${active.status}).`);
  }

  private async selectInstalledNode(modulePackage: ModulePackage, advance: boolean): Promise<ResolvedNode> {
    const { id, deployment } = modulePackage.manifest;
    const inventory = await this.installedModules(this.config.nodes);
    const candidates = inventory
      .filter((item) => item.reachable && item.modules?.includes(id))
      .map((item) => this.config.nodes.find((node) => node.name === item.node))
      .filter((node): node is ResolvedNode => node !== undefined);

    if (candidates.length === 0) {
      const unreachable = inventory.filter((item) => !item.reachable).map((item) => item.node);
      const detail = unreachable.length > 0 ? `; unreachable nodes: ${unreachable.join(", ")}` : "";
      throw new Error(`Module ${id} has no reachable installation${detail}.`);
    }
    if (deployment.mode === "singleton" && candidates.length > 1) {
      throw new Error(`Singleton module ${id} has duplicate installations on ${candidates.map((node) => node.name).join(", ")}.`);
    }
    if (!advance || candidates.length === 1) return candidates[0] as ResolvedNode;

    const cursor = this.routeCursors.get(id) ?? 0;
    const selected = candidates[cursor % candidates.length] as ResolvedNode;
    this.routeCursors.set(id, (cursor + 1) % candidates.length);
    return selected;
  }

  private async withModuleMutation<T>(moduleId: string, operation: () => Promise<T>): Promise<T> {
    const previous = this.moduleMutations.get(moduleId) ?? Promise.resolve();
    let release!: () => void;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    const tail = previous.catch(() => undefined).then(() => gate);
    this.moduleMutations.set(moduleId, tail);
    await previous.catch(() => undefined);
    try {
      return await operation();
    } finally {
      release();
      if (this.moduleMutations.get(moduleId) === tail) this.moduleMutations.delete(moduleId);
    }
  }

  private async waitForModuleMutation(moduleId: string): Promise<void> {
    await this.moduleMutations.get(moduleId)?.catch(() => undefined);
  }

  notifyInventoryChanged(): void {
    for (const listener of this.inventoryChangeListeners) listener();
  }

  private async withClient<T>(
    modulePackage: ModulePackage,
    node: ResolvedNode,
    operation: (client: Client, timeoutMs: number) => Promise<T>,
  ): Promise<T> {
    const receipt = await this.readInstalledReceipt(modulePackage, node);
    const { limits } = modulePackage.manifest;
    const command = receipt.entrypoint.map(q).join(" ");
    const data = modulePackage.manifest.persistentData;
    if (data && !node.storage) {
      throw new Error(`Module ${modulePackage.manifest.id} requires configured node-local storage on ${node.name}.`);
    }
    const environment: Record<string, string> = {};
    if (data && node.storage) {
      environment.VANTA_MODULE_DATA_DIR = path.posix.join(node.storage.mountpoint, data.relativePath);
      environment.VANTA_MODULE_DATA_MOUNT = node.storage.mountpoint;
      environment.VANTA_MODULE_RUN_AS = node.user;
    }
    Object.assign(environment, this.artifactEnvironment(modulePackage, node));
    const transport = new SshMcpTransport(
      () => this.pool.openProcess(node, command, {
        cwd: receipt.installDirectory,
        env: Object.keys(environment).length > 0 ? environment : undefined,
      }),
      { maxInputBytes: limits.maxInputBytes, maxOutputBytes: limits.maxOutputBytes },
    );
    const client = new Client({ name: "vantamcpd-module-proxy", version: "0.1.0" });
    try {
      await client.connect(transport, { timeout: limits.startupMs, maxTotalTimeout: limits.startupMs });
      return await operation(client, limits.callMs);
    } catch (err) {
      const stderr = transport.stderr.trim();
      throw new Error(`${(err as Error).message}${stderr ? `; module stderr: ${stderr}` : ""}`);
    } finally {
      await client.close().catch(() => transport.close());
    }
  }

  private async readInstalledReceipt(modulePackage: ModulePackage, node: ResolvedNode): Promise<InstallationReceipt> {
    const { id, version } = modulePackage.manifest;
    const moduleBase = `/opt/vantamcpd/modules/${id}`;
    const currentLink = `${moduleBase}/current`;
    const receiptPath = `/var/lib/vantamcpd/modules/${id}.json`;
    const result = await this.pool.exec(node, `cat ${q(receiptPath)}`, {
      timeoutMs: Math.min(modulePackage.manifest.limits.startupMs, 30_000),
    });
    if (!result.ok) throw new Error(`Module ${id} is not installed on ${node.name}: ${resultError(result)}`);

    let receipt: InstallationReceipt;
    try {
      receipt = InstallationReceiptSchema.parse(JSON.parse(result.stdout));
    } catch (err) {
      throw new Error(`Invalid installation receipt for ${id} on ${node.name}: ${(err as Error).message}`);
    }
    const expectedInstallDirectory = `${moduleBase}/${receipt.version}`;
    if (
      receipt.moduleId !== id ||
      receipt.installDirectory !== expectedInstallDirectory ||
      receipt.currentLink !== currentLink ||
      JSON.stringify(receipt.entrypoint) !== JSON.stringify(modulePackage.manifest.entrypoint)
    ) {
      throw new Error(`Installed ${id} receipt on ${node.name} is inconsistent with the local catalog.`);
    }
    // A pending update is an expected, temporary state and must not read as a corrupted installation.
    if (receipt.version !== version) {
      throw new Error(
        compareSemanticVersions(receipt.version, version) < 0
          ? `Module ${id} on ${node.name} is running ${receipt.version} and the catalog provides ${version}: ` +
            `a version update is in progress. Tool calls resume once the update activates; follow it with cluster_list_jobs.`
          : `Module ${id} on ${node.name} is running ${receipt.version}, which is newer than the local catalog version ${version}.`,
      );
    }
    const activeResult = await this.pool.exec(
      node,
      `test -d ${q(expectedInstallDirectory)} && test "$(readlink ${q(currentLink)})" = ${q(receipt.version)}`,
      { timeoutMs: Math.min(modulePackage.manifest.limits.startupMs, 30_000) },
    );
    if (!activeResult.ok) throw new Error(`Module ${id} ${receipt.version} is not active on ${node.name}.`);
    return receipt;
  }

  private async uninstallOnNode(
    modulePackage: ModulePackage,
    node: ResolvedNode,
    timeoutMs: number,
  ): Promise<ModuleUninstallResult> {
    const { id } = modulePackage.manifest;
    const moduleBase = `/opt/vantamcpd/modules/${id}`;
    const currentLink = `${moduleBase}/current`;
    const receiptPath = `/var/lib/vantamcpd/modules/${id}.json`;
    const inspectResult = await this.pool.exec(
      node,
      `if [ -f ${q(receiptPath)} ]; then cat ${q(receiptPath)}; ` +
        `elif [ -e ${q(currentLink)} ] || [ -d ${q(moduleBase)} ]; then printf '%s' '__ORPHANED__'; ` +
        `else printf '%s' '__ABSENT__'; fi`,
      { timeoutMs: Math.min(timeoutMs, 30_000) },
    );
    if (!inspectResult.ok) {
      return { node: node.name, ok: false, moduleId: id, removed: false, error: resultError(inspectResult) };
    }
    if (inspectResult.stdout === "__ABSENT__") {
      return { node: node.name, ok: true, moduleId: id, removed: false };
    }
    if (inspectResult.stdout === "__ORPHANED__") {
      return {
        node: node.name,
        ok: false,
        moduleId: id,
        removed: false,
        error: "module payload exists without a valid installation receipt; refusing automatic removal",
      };
    }

    let receipt: InstallationReceipt;
    try {
      receipt = InstallationReceiptSchema.parse(JSON.parse(inspectResult.stdout));
    } catch (err) {
      return {
        node: node.name,
        ok: false,
        moduleId: id,
        removed: false,
        error: `invalid installation receipt: ${(err as Error).message}`,
      };
    }
    const expectedInstallDirectory = `${moduleBase}/${receipt.version}`;
    if (
      receipt.moduleId !== id ||
      receipt.installDirectory !== expectedInstallDirectory ||
      receipt.currentLink !== currentLink
    ) {
      return {
        node: node.name,
        ok: false,
        moduleId: id,
        version: receipt.version,
        removed: false,
        error: "installation receipt paths do not match the requested module",
      };
    }

    const uninstallScript = `${expectedInstallDirectory}/${modulePackage.manifest.lifecycle.uninstall}`;
    const serviceUnitName = `vantamcpd-${id}.service`;
    const serviceCleanup = receipt.runtime.mode === "service"
      ? `systemctl disable --now ${q(serviceUnitName)} 2>/dev/null || true; ` +
        `rm -f -- ${q(`/etc/systemd/system/${serviceUnitName}`)}; systemctl daemon-reload; `
      : "";
    const lifecycleCommand =
      `set -e; ${serviceCleanup}if [ -f ${q(uninstallScript)} ]; then bash ${q(uninstallScript)}; else ` +
      `if [ -L ${q(currentLink)} ] && [ "$(readlink ${q(currentLink)})" = ${q(receipt.version)} ]; then rm -f ${q(currentLink)}; fi; ` +
      `rm -rf -- ${q(expectedInstallDirectory)}; fi; rm -f -- ${q(receiptPath)}; rmdir ${q(moduleBase)} 2>/dev/null || true`;
    const uninstallResult = await this.pool.exec(node, lifecycleCommand, {
      sudo: true,
      env: {
        VANTA_MODULE_INSTALL_DIR: expectedInstallDirectory,
        VANTA_MODULE_CURRENT_LINK: currentLink,
      },
      timeoutMs,
    });
    if (!uninstallResult.ok) {
      return {
        node: node.name,
        ok: false,
        moduleId: id,
        version: receipt.version,
        removed: false,
        error: `uninstallation lifecycle failed: ${resultError(uninstallResult)}`,
      };
    }
    return { node: node.name, ok: true, moduleId: id, version: receipt.version, removed: true };
  }

  private async installOnNode(
    modulePackage: ModulePackage,
    node: ResolvedNode,
    timeoutMs: number,
    optionEnvironment: Record<string, string>,
  ): Promise<ModuleInstallResult> {
    const { id, version } = modulePackage.manifest;
    const stage = `/tmp/vantamcpd-${id}-${randomUUID()}`;
    const moduleBase = `/opt/vantamcpd/modules/${id}`;
    const installDirectory = `${moduleBase}/${version}`;
    const currentLink = `${moduleBase}/current`;
    const receiptPath = `/var/lib/vantamcpd/modules/${id}.json`;
    const baseResult = { node: node.name, moduleId: id, version };
    let stageMoved = false;

    try {
      const directories = new Set(
        modulePackage.files
          .map((file) => path.posix.dirname(file.relativePath))
          .filter((directory) => directory !== "."),
      );
      const remoteDirectories = [stage, ...[...directories].map((directory) => `${stage}/${directory}`)];
      const createStage = await this.pool.exec(
        node,
        `umask 077; mkdir -p -- ${remoteDirectories.map(q).join(" ")}`,
        { timeoutMs: Math.min(timeoutMs, 30_000) },
      );
      if (!createStage.ok) throw new Error(`cannot create staging directory: ${resultError(createStage)}`);

      const sftp = await this.pool.sftp(node);
      try {
        for (const file of modulePackage.files) {
          await fastPut(sftp, file.absolutePath, `${stage}/${file.relativePath}`);
        }
      } finally {
        sftp.end();
      }

      const hashScript = modulePackage.files
        .map((file) => `printf '%s|' ${q(file.relativePath)}; sha256sum -- ${q(file.relativePath)} | awk '{print $1}'`)
        .join("\n");
      const hashResult = await this.pool.exec(node, hashScript, {
        cwd: stage,
        timeoutMs: Math.min(timeoutMs, 30_000),
      });
      if (!hashResult.ok) throw new Error(`cannot verify staged package: ${resultError(hashResult)}`);
      const remoteHashes = parseKeyValueLines(hashResult.stdout);
      for (const file of modulePackage.files) {
        if (remoteHashes[file.relativePath] !== file.sha256) {
          throw new Error(`staged hash mismatch for ${file.relativePath}`);
        }
      }

      const receipt: InstallationReceipt = {
        schemaVersion: 1,
        moduleId: id,
        version,
        installedAt: new Date().toISOString(),
        installDirectory,
        currentLink,
        entrypoint: modulePackage.manifest.entrypoint,
        files: modulePackage.files.map((file) => ({
          path: file.relativePath,
          size: file.size,
          sha256: file.sha256,
        })),
        runtime: modulePackage.manifest.runtime,
      };
      const encodedReceipt = Buffer.from(`${JSON.stringify(receipt, null, 2)}\n`, "utf8").toString("base64");
      const receiptDirectory = path.posix.dirname(receiptPath);
      const serviceUnitName = `vantamcpd-${id}.service`;
      const serviceUnitPath = `/etc/systemd/system/${serviceUnitName}`;
      const servicePreparation = modulePackage.manifest.runtime.mode === "service"
        ? `receipt_backup=$(mktemp); unit_backup=$(mktemp); had_receipt=0; had_unit=0; ` +
          `[ ! -f ${q(receiptPath)} ] || { cp -p ${q(receiptPath)} "$receipt_backup"; had_receipt=1; }; ` +
          `[ ! -f ${q(serviceUnitPath)} ] || { cp -p ${q(serviceUnitPath)} "$unit_backup"; had_unit=1; }; ` +
          `cleanup_service_backups() { rm -f -- "$receipt_backup" "$unit_backup"; }; trap cleanup_service_backups EXIT; ` +
          `rollback_service() { ` +
          `if [ "$had_receipt" = 1 ]; then cp -p "$receipt_backup" ${q(receiptPath)}; else rm -f -- ${q(receiptPath)}; fi; ` +
          `if [ "$had_unit" = 1 ]; then cp -p "$unit_backup" ${q(serviceUnitPath)}; else rm -f -- ${q(serviceUnitPath)}; fi; ` +
          `systemctl daemon-reload; if [ "$had_unit" = 1 ]; then systemctl restart ${q(serviceUnitName)} || true; ` +
          `else systemctl disable --now ${q(serviceUnitName)} 2>/dev/null || true; fi; }; `
        : "";
      const serviceActivation = modulePackage.manifest.runtime.mode === "service"
        ? `if ! (set -e; install -m 0644 ${q(`${installDirectory}/${modulePackage.manifest.runtime.systemdUnit}`)} ` +
          `${q(serviceUnitPath)}; systemctl daemon-reload; systemctl enable ${q(serviceUnitName)}; ` +
          `systemctl restart ${q(serviceUnitName)}; systemctl is-active --quiet ${q(serviceUnitName)}); then ` +
          `rollback; rollback_service; exit 1; fi; cleanup_service_backups; trap - EXIT; `
        : "";
      const data = modulePackage.manifest.persistentData;
      const dataDirectory = data && node.storage ? path.posix.join(node.storage.mountpoint, data.relativePath) : undefined;
      const dataSetup = dataDirectory
        ? `install -d -m 0750 ${q(dataDirectory)}; chown ${q(node.user)}:$(id -gn ${q(node.user)}) ${q(dataDirectory)}; ` +
          `printf '%s' ${q(id)} > ${q(`${dataDirectory}/.vantamcpd-module`)}; chmod 0644 ${q(`${dataDirectory}/.vantamcpd-module`)}; `
        : "";
      const lifecycleCommand =
        `previous=$(readlink ${q(currentLink)} 2>/dev/null || true); ` +
        `rollback() { if [ -n "$previous" ]; then ln -sfn "$previous" ${q(currentLink)}; else rm -f ${q(currentLink)}; fi; }; ` +
        servicePreparation +
        dataSetup +
        `if ! bash ${q(modulePackage.manifest.lifecycle.install)}; then rollback; exit 1; fi; ` +
        `if ! chown -R root:root ${q(installDirectory)} || ! chmod 0755 ${q(installDirectory)}; then rollback; exit 1; fi; ` +
        `if ! (set -e; install -d -m 0755 ${q(receiptDirectory)}; ` +
        `tmp=$(mktemp ${q(`${receiptDirectory}/.${id}.XXXXXX`)}); trap 'rm -f "$tmp"' EXIT; ` +
        `printf '%s' ${q(encodedReceipt)} | base64 -d > "$tmp"; chmod 0644 "$tmp"; ` +
        `mv -f "$tmp" ${q(receiptPath)}; trap - EXIT); then rollback; exit 1; fi; ` +
        serviceActivation;
      const lifecycleEnvironment = {
        VANTA_MODULE_STAGE: stage,
        VANTA_MODULE_INSTALL_DIR: installDirectory,
        VANTA_MODULE_CURRENT_LINK: currentLink,
        VANTA_MODULE_RUN_AS: node.user,
        ...optionEnvironment,
        ...this.artifactEnvironment(modulePackage, node),
        ...(dataDirectory && node.storage
          ? {
              VANTA_MODULE_DATA_DIR: dataDirectory,
              VANTA_MODULE_DATA_MOUNT: node.storage.mountpoint,
            }
          : {}),
      };

      if (modulePackage.manifest.lifecycle.execution?.mode === "job") {
        if (!this.jobs) throw new Error("durable job manager is unavailable");
        const durableStage = `/var/lib/vantamcpd/module-staging/${id}-${randomUUID()}`;
        const scriptPath = `${durableStage}/.vantamcpd-lifecycle.sh`;
        const script =
          `#!/usr/bin/env bash\nset -e\n` +
          `cleanup() { status=$?; if [ "$status" -ne 75 ]; then rm -rf -- ${q(durableStage)}; fi; exit "$status"; }\n` +
          `trap cleanup EXIT\ntrap 'exit 75' TERM INT\n${lifecycleCommand}\n`;
        const encodedScript = Buffer.from(script, "utf8").toString("base64");
        const moveResult = await this.pool.exec(
          node,
          `set -e; install -d -m 0700 /var/lib/vantamcpd/module-staging; rm -rf -- ${q(durableStage)}; ` +
            `mv ${q(stage)} ${q(durableStage)}; printf '%s' ${q(encodedScript)} | base64 -d > ${q(scriptPath)}; ` +
            `chmod 0700 ${q(scriptPath)}; chown -R root:root ${q(durableStage)}`,
          { sudo: true, timeoutMs: Math.min(timeoutMs, 30_000), maxOutputBytes: 64 * 1024 },
        );
        if (!moveResult.ok) throw new Error(`cannot preserve job staging directory: ${resultError(moveResult)}`);
        stageMoved = true;
        try {
          const job = await this.jobs.submit(node, {
            kind: "module-install",
            moduleId: id,
            resourceKeys: [`module:${id}:${node.name}`],
            command: ["/bin/bash", scriptPath],
            cwd: durableStage,
            environment: {
              ...lifecycleEnvironment,
              VANTA_MODULE_STAGE: durableStage,
            },
            timeoutMs: modulePackage.manifest.lifecycle.execution.timeoutMs,
          });
          return { ...baseResult, ok: true, state: "provisioning", jobId: job.jobId };
        } catch (error) {
          await this.pool.exec(node, `rm -rf -- ${q(durableStage)}`, { sudo: true, timeoutMs: 30_000 });
          throw error;
        }
      }
      const installResult = await this.pool.exec(
        node,
        lifecycleCommand,
        {
          sudo: true,
          cwd: stage,
          env: lifecycleEnvironment,
          timeoutMs,
        },
      );
      if (!installResult.ok) throw new Error(`installation lifecycle failed: ${resultError(installResult)}`);

      return { ...baseResult, ok: true, state: "installed" };
    } catch (err) {
      return { ...baseResult, ok: false, error: (err as Error).message };
    } finally {
      if (!stageMoved) await this.pool.exec(node, `rm -rf -- ${q(stage)}`, { timeoutMs: 30_000 });
    }
  }

  private async installAptDependencies(
    modulePackage: ModulePackage,
    node: ResolvedNode,
    timeoutMs: number,
  ): Promise<{ ok: boolean; error?: string }> {
    const packages = modulePackage.manifest.packages.apt;
    if (packages.length === 0) {
      return { ok: false, error: "required commands are missing and the module declares no apt dependencies" };
    }
    const command = `set -e; ${aptUpdate()}; ${aptGet("install", { packages: packages.map(q), flags: ["--no-upgrade"] })}`;
    const result = await this.pool.exec(node, command, { sudo: true, timeoutMs });
    return result.ok
      ? { ok: true }
      : { ok: false, error: `apt dependency installation failed: ${resultError(result)}` };
  }
}

export function compareSemanticVersions(left: string, right: string): number {
  const parse = (value: string) => {
    const match = /^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$/.exec(value);
    if (!match) throw new Error(`Invalid semantic version: ${value}`);
    return {
      core: [Number(match[1]), Number(match[2]), Number(match[3])],
      prerelease: match[4]?.split(".") ?? [],
    };
  };
  const a = parse(left);
  const b = parse(right);
  for (let index = 0; index < 3; index += 1) {
    if (a.core[index] !== b.core[index]) return (a.core[index] as number) < (b.core[index] as number) ? -1 : 1;
  }
  if (a.prerelease.length === 0 || b.prerelease.length === 0) {
    return a.prerelease.length === b.prerelease.length ? 0 : a.prerelease.length === 0 ? 1 : -1;
  }
  const length = Math.max(a.prerelease.length, b.prerelease.length);
  for (let index = 0; index < length; index += 1) {
    const aPart = a.prerelease[index];
    const bPart = b.prerelease[index];
    if (aPart === undefined || bPart === undefined) return aPart === undefined ? -1 : 1;
    if (aPart === bPart) continue;
    const aNumeric = /^\d+$/.test(aPart);
    const bNumeric = /^\d+$/.test(bPart);
    if (aNumeric && bNumeric) return Number(aPart) < Number(bPart) ? -1 : 1;
    if (aNumeric !== bNumeric) return aNumeric ? -1 : 1;
    return aPart < bPart ? -1 : 1;
  }
  return 0;
}