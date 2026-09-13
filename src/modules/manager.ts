import { randomUUID } from "node:crypto";
import path from "node:path";
import type { SFTPWrapper } from "ssh2";
import { z } from "zod";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import type { ClusterConfig, ResolvedNode } from "../config.js";
import { parseKeyValueLines } from "../format.js";
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
  missingCommands?: string[];
  error?: string;
}

export interface ModuleInstallResult {
  node: string;
  ok: boolean;
  moduleId: string;
  version: string;
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
  oldVersionRemoved?: boolean;
  error?: string;
}

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

export class ModuleManager {
  readonly catalog: ModuleCatalog;
  private readonly moduleMutations = new Map<string, Promise<void>>();
  private readonly routeCursors = new Map<string, number>();

  constructor(
    private readonly config: ClusterConfig,
    private readonly pool: SshPool,
    moduleRoot?: string,
  ) {
    this.catalog = loadModuleCatalog(moduleRoot);
  }

  get(moduleId: string): ModulePackage {
    const modulePackage = this.catalog.modules.find((item) => item.manifest.id === moduleId);
    if (modulePackage) return modulePackage;
    const known = this.catalog.modules.map((item) => item.manifest.id).join(", ") || "none";
    throw new Error(`Unknown module: ${moduleId}. Available modules: ${known}.`);
  }

  async list(nodes: ResolvedNode[]): Promise<object> {
    const inventory = await this.installedModules(nodes);
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
        nodes: nodes.map((node) => {
          const state = byNode.get(node.name);
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
    const script = `${commandChecks}\necho "disk_available_mb|$(df -Pm / | awk 'NR==2{print $4}')"`;
    const results = await this.pool.execMany(nodes, script, { timeoutMs });

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
          `requires ${minDiskMb} MB free disk; node has ${diskAvailableMb} MB`,
        );
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
        missingCommands,
      };
    });
  }

  async install(moduleId: string, nodes: ResolvedNode[], timeoutMs = 300_000): Promise<ModuleInstallResult[]> {
    return this.withModuleMutation(moduleId, async () => {
      const modulePackage = this.get(moduleId);
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
        return this.installOnNode(modulePackage, node, timeoutMs);
      });
      this.routeCursors.delete(moduleId);
      return results;
    });
  }

  async uninstall(moduleId: string, nodes: ResolvedNode[], timeoutMs = 300_000): Promise<ModuleUninstallResult[]> {
    return this.withModuleMutation(moduleId, async () => {
      const modulePackage = this.get(moduleId);
      const results = await mapLimit(
        nodes,
        this.config.maxConcurrency,
        (node) => this.uninstallOnNode(modulePackage, node, timeoutMs),
      );
      this.routeCursors.delete(moduleId);
      return results;
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
    const result = await this.withClient(modulePackage, selectedNode, async (client, timeout) => {
      const listed = await client.listTools({}, { timeout, maxTotalTimeout: timeout });
      if (!listed.tools.some((tool) => tool.name === toolName)) {
        throw new Error(`Module ${moduleId} does not advertise tool ${toolName}.`);
      }
      return client.callTool(
        { name: toolName, arguments: args },
        undefined,
        { timeout, maxTotalTimeout: timeout },
      );
    });
    return {
      ok: result.isError !== true,
      node: selectedNode.name,
      moduleId,
      moduleVersion: modulePackage.manifest.version,
      toolName,
      deployment: modulePackage.manifest.deployment,
      selection,
      output: normalizeModuleOutput(result),
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

  private async withClient<T>(
    modulePackage: ModulePackage,
    node: ResolvedNode,
    operation: (client: Client, timeoutMs: number) => Promise<T>,
  ): Promise<T> {
    const receipt = await this.readInstalledReceipt(modulePackage, node);
    const { limits } = modulePackage.manifest;
    const command = receipt.entrypoint.map(q).join(" ");
    const transport = new SshMcpTransport(
      () => this.pool.openProcess(node, command, { cwd: receipt.installDirectory }),
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
      receipt.version !== version ||
      receipt.installDirectory !== expectedInstallDirectory ||
      receipt.currentLink !== currentLink ||
      JSON.stringify(receipt.entrypoint) !== JSON.stringify(modulePackage.manifest.entrypoint)
    ) {
      throw new Error(`Installed ${id} receipt does not match local catalog version ${version}.`);
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
  ): Promise<ModuleInstallResult> {
    const { id, version } = modulePackage.manifest;
    const stage = `/tmp/vantamcpd-${id}-${randomUUID()}`;
    const moduleBase = `/opt/vantamcpd/modules/${id}`;
    const installDirectory = `${moduleBase}/${version}`;
    const currentLink = `${moduleBase}/current`;
    const receiptPath = `/var/lib/vantamcpd/modules/${id}.json`;
    const baseResult = { node: node.name, moduleId: id, version };

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
      const serviceActivation = modulePackage.manifest.runtime.mode === "service"
        ? `if ! (set -e; install -m 0644 ${q(`${installDirectory}/${modulePackage.manifest.runtime.systemdUnit}`)} ` +
          `${q(serviceUnitPath)}; systemctl daemon-reload; systemctl enable --now ${q(serviceUnitName)}); then ` +
          `systemctl disable --now ${q(serviceUnitName)} 2>/dev/null || true; rm -f -- ${q(serviceUnitPath)}; ` +
          `rm -f -- ${q(receiptPath)}; systemctl daemon-reload; rollback; exit 1; fi; `
        : "";
      const lifecycleCommand =
        `previous=$(readlink ${q(currentLink)} 2>/dev/null || true); ` +
        `rollback() { if [ -n "$previous" ]; then ln -sfn "$previous" ${q(currentLink)}; else rm -f ${q(currentLink)}; fi; }; ` +
        `if ! bash ${q(modulePackage.manifest.lifecycle.install)}; then rollback; exit 1; fi; ` +
        `if ! chown -R root:root ${q(installDirectory)} || ! chmod 0755 ${q(installDirectory)}; then rollback; exit 1; fi; ` +
        `if ! (set -e; install -d -m 0755 ${q(receiptDirectory)}; ` +
        `tmp=$(mktemp ${q(`${receiptDirectory}/.${id}.XXXXXX`)}); trap 'rm -f "$tmp"' EXIT; ` +
        `printf '%s' ${q(encodedReceipt)} | base64 -d > "$tmp"; chmod 0644 "$tmp"; ` +
        `mv -f "$tmp" ${q(receiptPath)}; trap - EXIT); then rollback; exit 1; fi; ` +
        serviceActivation;
      const installResult = await this.pool.exec(
        node,
        lifecycleCommand,
        {
          sudo: true,
          cwd: stage,
          env: {
            VANTA_MODULE_STAGE: stage,
            VANTA_MODULE_INSTALL_DIR: installDirectory,
            VANTA_MODULE_CURRENT_LINK: currentLink,
          },
          timeoutMs,
        },
      );
      if (!installResult.ok) throw new Error(`installation lifecycle failed: ${resultError(installResult)}`);

      return { ...baseResult, ok: true };
    } catch (err) {
      return { ...baseResult, ok: false, error: (err as Error).message };
    } finally {
      await this.pool.exec(node, `rm -rf -- ${q(stage)}`, { timeoutMs: 30_000 });
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
    const aptOptions =
      "-y -q -o Dpkg::Use-Pty=0 -o DPkg::Lock::Timeout=300 " +
      "-o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold";
    const command =
      "set -e; apt-get update -q -o Dpkg::Use-Pty=0 -o DPkg::Lock::Timeout=300 </dev/null; " +
      `env DEBIAN_FRONTEND=noninteractive apt-get install --no-upgrade ${aptOptions} -- ${packages.map(q).join(" ")} </dev/null`;
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