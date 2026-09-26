import { createHash, randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import type { ClusterConfig, ResolvedNode } from "../config.js";
import { q } from "../security.js";
import type { SshPool } from "../ssh.js";
import type { JobRegistry } from "./registry.js";
import { isTerminalJobStatus, parseJobState, TrustedJobSpecSchema, type JobState, type TrustedJobSpec } from "./types.js";

const JOB_ROOT = "/var/lib/vantamcpd/jobs";
const RUNNER_VERSION = "2";
const RUNNER_DIR = `/opt/vantamcpd/job-runner/${RUNNER_VERSION}`;
const RUNNER_PATH = `${RUNNER_DIR}/remote-runner.py`;
export const MODULE_CALL_PATH = `${RUNNER_DIR}/module-call.py`;
// Units written before the v2 runner still reference this path; cancel and recovery fall back to it.
const LEGACY_RUNNER_PATH = "/opt/vantamcpd/job-runner/1/remote-runner.py";
const QUEUED_START_GRACE_MS = 60_000;

export interface SubmitJobInput {
  kind: string;
  moduleId?: string;
  resourceKeys: string[];
  command: string[];
  cwd: string;
  environment?: Record<string, string>;
  timeoutMs: number;
  /** Run the command as this node's SSH user instead of root. */
  runAsNodeUser?: boolean;
  /** Mark false for non-idempotent work: a runner restarted mid-job then fails instead of re-running it. */
  rerunOnRestart?: boolean;
  /** Reject submission unless the node reports at least this much MemAvailable. */
  minFreeMemoryMb?: number;
  /** Collect `out/result.json` written by the command, bounded to this size. */
  maxResultBytes?: number;
  /** JSON text written to `out/input.json`, readable only by the job's user. */
  input?: string;
}

export interface ListedJobs {
  jobs: JobState[];
  unreachableNodes: string[];
  invalidStates: { node: string; error: string }[];
}

function encode(value: string): string {
  return Buffer.from(value, "utf8").toString("base64");
}

function unitName(jobId: string): string {
  return `vantamcpd-job-${jobId}.service`;
}

function runnerSelection(): string {
  return `runner=${q(RUNNER_PATH)}; [ -f "$runner" ] || runner=${q(LEGACY_RUNNER_PATH)}`;
}

function installHelper(target: string, content: string): string {
  const hash = createHash("sha256").update(content).digest("hex");
  return `if [ ! -f ${q(target)} ] || [ "$(sha256sum ${q(target)} | awk '{print $1}')" != ${q(hash)} ]; then ` +
    `printf '%s' ${q(encode(content))} | base64 -d | install -m 0755 /dev/stdin ${q(target)}; fi`;
}

export class JobManager {
  private readonly cache = new Map<string, JobState>();
  private readonly settledListeners = new Set<(job: JobState) => void>();
  private readonly submissions = new Map<string, Promise<unknown>>();
  private refreshedAt?: string;
  private timer?: NodeJS.Timeout;
  private readonly runner: string;
  private readonly moduleCall: string;

  constructor(
    private readonly config: ClusterConfig,
    private readonly pool: SshPool,
    private readonly registry: JobRegistry,
    runnerPath = path.join(path.dirname(fileURLToPath(import.meta.url)), "remote-runner.py"),
    moduleCallPath = path.join(path.dirname(runnerPath), "module-call.py"),
  ) {
    this.runner = readFileSync(runnerPath, "utf8");
    this.moduleCall = readFileSync(moduleCallPath, "utf8");
  }

  maxConcurrent(node: ResolvedNode): number {
    return this.config.jobs.perNode?.[node.name]?.maxConcurrent ?? this.config.jobs.maxConcurrentPerNode ?? 2;
  }

  /** Submissions to one node are serialized so two concurrent calls cannot both pass the per-node limit. */
  submit(node: ResolvedNode, input: SubmitJobInput): Promise<JobState> {
    const previous = this.submissions.get(node.name) ?? Promise.resolve();
    const next = previous.catch(() => undefined).then(() => this.submitNow(node, input));
    this.submissions.set(node.name, next);
    void next.finally(() => {
      if (this.submissions.get(node.name) === next) this.submissions.delete(node.name);
    }).catch(() => undefined);
    return next;
  }

  private async submitNow(node: ResolvedNode, input: SubmitJobInput): Promise<JobState> {
    this.registry.assertRegistered(input.kind);
    const now = new Date().toISOString();
    const jobId = randomUUID();
    const spec = TrustedJobSpecSchema.parse({
      schemaVersion: 1,
      jobId,
      kind: input.kind,
      targetNode: node.name,
      moduleId: input.moduleId,
      resourceKeys: input.resourceKeys,
      command: input.command,
      cwd: input.cwd,
      environment: input.environment ?? {},
      timeoutMs: input.timeoutMs,
      retentionMs: this.config.jobs.retentionDays * 24 * 60 * 60 * 1_000,
      maxLogBytes: this.config.jobs.maxLogBytes,
      runAs: input.runAsNodeUser ? node.user : undefined,
      rerunOnRestart: input.rerunOnRestart,
      maxResultBytes: input.maxResultBytes,
      createdAt: now,
    });
    const state = parseJobState({
      schemaVersion: 1,
      jobId,
      kind: spec.kind,
      status: "queued",
      targetNode: node.name,
      moduleId: spec.moduleId,
      resourceKeys: spec.resourceKeys,
      phase: "queued",
      createdAt: now,
    });

    const active = (await this.list([node])).jobs.filter((job) => !isTerminalJobStatus(job.status));
    const conflict = active.find((job) => job.resourceKeys.some((key) => spec.resourceKeys.includes(key)));
    if (conflict) throw new Error(`Resource is busy with job ${conflict.jobId} (${conflict.kind}, ${conflict.status}).`);
    const limit = this.maxConcurrent(node);
    if (active.length >= limit) {
      throw new Error(
        `${node.name} is already running ${active.length}/${limit} durable job(s); retry after one finishes ` +
          `or raise jobs.maxConcurrentPerNode.`,
      );
    }
    if (input.minFreeMemoryMb !== undefined) {
      const available = await this.availableMemoryMb(node);
      if (available < input.minFreeMemoryMb) {
        throw new Error(
          `${node.name} has ${available} MB available memory; this job requires ${input.minFreeMemoryMb} MB. ` +
            `Retry when the node is less busy or choose another node.`,
        );
      }
    }

    const directory = `${JOB_ROOT}/${jobId}`;
    const outDirectory = `${directory}/out`;
    const unit = [
      "[Unit]",
      `Description=VantaMCPd durable job ${jobId}`,
      "After=network-online.target",
      "Wants=network-online.target",
      "",
      "[Service]",
      "Type=oneshot",
      `ExecStart=/usr/bin/python3 ${RUNNER_PATH} ${directory}/spec.json`,
      "Restart=on-failure",
      "RestartSec=10",
      `RuntimeMaxSec=${Math.ceil(spec.timeoutMs / 1_000)}`,
      `TimeoutStopSec=${Math.ceil(this.config.jobs.cancelGraceMs / 1_000)}`,
      "KillMode=control-group",
      "",
      "[Install]",
      "WantedBy=multi-user.target",
      "",
    ].join("\n");
    // A job running as the SSH user must traverse its job directory to reach out/.
    const directoryMode = spec.runAs ? "0711" : "0700";
    const owner = q(spec.runAs ?? "root");
    const command = [
      "set -e",
      `install -d -m 0755 ${q(RUNNER_DIR)} ${q(JOB_ROOT)}`,
      installHelper(RUNNER_PATH, this.runner),
      installHelper(MODULE_CALL_PATH, this.moduleCall),
      `install -d -m ${directoryMode} ${q(directory)}`,
      `install -d -m 0700 -o ${owner} ${q(outDirectory)}`,
      ...(input.input === undefined
        ? []
        : [
            `printf '%s' ${q(encode(input.input))} | base64 -d | install -m 0600 -o ${owner} /dev/stdin ${q(`${outDirectory}/input.json`)}`,
          ]),
      `spec_tmp=$(mktemp ${q(`${directory}/.spec.XXXXXX`)}); state_tmp=$(mktemp ${q(`${directory}/.state.XXXXXX`)})`,
      `printf '%s' ${q(encode(`${JSON.stringify(spec)}\n`))} | base64 -d > "$spec_tmp"`,
      `printf '%s' ${q(encode(`${JSON.stringify(state)}\n`))} | base64 -d > "$state_tmp"`,
      `chmod 0600 "$spec_tmp"; chmod 0644 "$state_tmp"`,
      `mv -f "$spec_tmp" ${q(`${directory}/spec.json`)}; mv -f "$state_tmp" ${q(`${directory}/state.json`)}`,
      `printf '%s' ${q(encode(unit))} | base64 -d > ${q(`/etc/systemd/system/${unitName(jobId)}`)}`,
      "systemctl daemon-reload",
      `systemctl enable ${q(unitName(jobId))}`,
      `systemctl start --no-block ${q(unitName(jobId))}`,
    ].join("\n");
    const result = await this.pool.exec(node, command, { sudo: true, timeoutMs: 60_000, maxOutputBytes: 64 * 1024 });
    if (!result.ok) throw new Error(result.error ?? (result.stderr.trim() || `Failed to submit job ${jobId}.`));
    this.cache.set(jobId, state);
    return state;
  }

  async availableMemoryMb(node: ResolvedNode): Promise<number> {
    const result = await this.pool.exec(node, "awk '/^MemAvailable:/ {print int($2 / 1024)}' /proc/meminfo", {
      timeoutMs: 15_000,
      maxOutputBytes: 1_024,
    });
    const value = Number.parseInt(result.stdout.trim(), 10);
    if (!result.ok || !Number.isFinite(value)) {
      throw new Error(`Could not read available memory on ${node.name}: ${result.error ?? (result.stderr.trim() || "no MemAvailable value")}`);
    }
    return value;
  }

  /** Return the bounded result file a job's command published, if the job produced one. */
  async result(jobId: string): Promise<{ job: JobState; result?: string }> {
    const job = await this.get(jobId);
    if (job.resultBytes === undefined) return { job };
    const node = this.config.nodes.find((candidate) => candidate.name === job.targetNode)!;
    const result = await this.pool.exec(node, `cat ${q(`${JOB_ROOT}/${job.jobId}/result.json`)}`, {
      sudo: true,
      timeoutMs: 30_000,
      maxOutputBytes: job.resultBytes + 1_024,
    });
    if (!result.ok) throw new Error(result.error ?? (result.stderr.trim() || `Failed to read job ${jobId} result.`));
    return { job, result: result.stdout };
  }

  /** Fires once when a job this process has seen running reaches a terminal state. */
  onJobSettled(listener: (job: JobState) => void): () => void {
    this.settledListeners.add(listener);
    return () => this.settledListeners.delete(listener);
  }

  async list(nodes = this.config.nodes): Promise<ListedJobs> {
    const command = `for file in ${JOB_ROOT}/*/state.json; do [ -f "$file" ] || continue; base64 -w0 "$file"; printf '\\n'; done`;
    const results = await this.pool.execMany(nodes, command, { sudo: true, timeoutMs: 30_000, maxOutputBytes: 2_000_000 });
    const previousStatus = new Map([...this.cache].map(([jobId, state]) => [jobId, state.status]));
    const settled: JobState[] = [];
    const jobs: JobState[] = [];
    const unreachableNodes: string[] = [];
    const invalidStates: { node: string; error: string }[] = [];
    for (const result of results) {
      if (!result.ok) {
        unreachableNodes.push(result.node);
        continue;
      }
      for (const [jobId, state] of this.cache) {
        if (state.targetNode === result.node) this.cache.delete(jobId);
      }
      for (const line of result.stdout.split(/\r?\n/).filter(Boolean)) {
        try {
          const state = parseJobState(JSON.parse(Buffer.from(line, "base64").toString("utf8")), `job state on ${result.node}`);
          if (state.targetNode !== result.node) throw new Error(`targetNode is ${state.targetNode}`);
          jobs.push(state);
          this.cache.set(state.jobId, state);
          const previous = previousStatus.get(state.jobId);
          if (previous !== undefined && !isTerminalJobStatus(previous) && isTerminalJobStatus(state.status)) settled.push(state);
        } catch (error) {
          invalidStates.push({ node: result.node, error: (error as Error).message });
        }
      }
    }
    jobs.sort((left, right) => right.createdAt.localeCompare(left.createdAt) || left.jobId.localeCompare(right.jobId));
    this.refreshedAt = new Date().toISOString();
    for (const job of settled) {
      for (const listener of this.settledListeners) listener(job);
    }
    return { jobs, unreachableNodes, invalidStates };
  }

  snapshot(): { jobs: JobState[]; refreshedAt?: string } {
    const jobs = [...this.cache.values()].sort(
      (left, right) => right.createdAt.localeCompare(left.createdAt) || left.jobId.localeCompare(right.jobId),
    );
    return { jobs, refreshedAt: this.refreshedAt };
  }

  async get(jobId: string): Promise<JobState> {
    const parsedId = TrustedJobSpecSchema.shape.jobId.parse(jobId);
    const cached = this.cache.get(parsedId);
    const nodes = cached ? this.config.nodes.filter((node) => node.name === cached.targetNode) : this.config.nodes;
    const found = (await this.list(nodes)).jobs.find((job) => job.jobId === parsedId);
    if (!found) throw new Error(`Job not found: ${parsedId}`);
    return found;
  }

  async log(jobId: string, maxBytes = 64 * 1024): Promise<{ job: JobState; log: string }> {
    const job = await this.get(jobId);
    const node = this.config.nodes.find((candidate) => candidate.name === job.targetNode)!;
    const bytes = Math.min(Math.max(maxBytes, 1), this.config.jobs.maxLogBytes);
    const result = await this.pool.exec(node, `tail -c ${bytes} ${q(`${JOB_ROOT}/${job.jobId}/job.log`)} 2>/dev/null || true`, {
      sudo: true,
      timeoutMs: 30_000,
      maxOutputBytes: bytes,
    });
    if (!result.ok) throw new Error(result.error ?? (result.stderr.trim() || `Failed to read job ${jobId} log.`));
    return { job, log: result.stdout };
  }

  async cancel(jobId: string): Promise<JobState> {
    const job = await this.get(jobId);
    if (isTerminalJobStatus(job.status)) return job;
    const node = this.config.nodes.find((candidate) => candidate.name === job.targetNode)!;
    const specPath = `${JOB_ROOT}/${job.jobId}/spec.json`;
    const command = `systemctl stop ${q(unitName(job.jobId))} || true\n${runnerSelection()}\n/usr/bin/python3 "$runner" ${q(specPath)} --cancel`;
    const result = await this.pool.exec(node, command, {
      sudo: true,
      timeoutMs: this.config.jobs.cancelGraceMs + 30_000,
      maxOutputBytes: 64 * 1024,
    });
    if (!result.ok) throw new Error(result.error ?? (result.stderr.trim() || `Failed to cancel job ${jobId}.`));
    return this.get(jobId);
  }

  async reconcile(): Promise<ListedJobs> {
    const listed = await this.list();
    const now = Date.now();
    for (const job of listed.jobs) {
      const node = this.config.nodes.find((candidate) => candidate.name === job.targetNode);
      if (!node) continue;
      if (!isTerminalJobStatus(job.status)) {
        if (job.status === "queued" && now - Date.parse(job.createdAt) < QUEUED_START_GRACE_MS) continue;
        const message = "Job runner is not active; recovered during reconciliation.";
        const activeStates = "active|activating|reloading|deactivating";
        const command = `${runnerSelection()}; state=$(systemctl is-active ${q(unitName(job.jobId))} 2>/dev/null || true); ` +
          `case "$state" in ${activeStates}) printf '%s' '__ACTIVE__';; *) ` +
          `/usr/bin/python3 "$runner" ${q(`${JOB_ROOT}/${job.jobId}/spec.json`)} --fail ${q(message)}; ` +
          `printf '%s' '__FAILED__';; esac`;
        const result = await this.pool.exec(node, command, { sudo: true, timeoutMs: 30_000, maxOutputBytes: 64 * 1024 });
        if (result.ok && result.stdout === "__FAILED__") {
          const finishedAt = new Date().toISOString();
          Object.assign(job, {
            status: "failed" as const,
            heartbeatAt: finishedAt,
            finishedAt,
            expiresAt: new Date(now + this.config.jobs.retentionDays * 24 * 60 * 60 * 1_000).toISOString(),
            error: message,
          });
          this.cache.set(job.jobId, job);
        }
        continue;
      }
      if (!job.expiresAt || Date.parse(job.expiresAt) > now) continue;
      const command = `systemctl disable --now ${q(unitName(job.jobId))} 2>/dev/null || true\n` +
        `spec=${q(`${JOB_ROOT}/${job.jobId}/spec.json`)}\n` +
        `if [ -f "$spec" ]; then cwd=$(/usr/bin/python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["cwd"])' "$spec" 2>/dev/null || true); ` +
        `case "$cwd" in /var/lib/vantamcpd/module-staging/*) rm -rf -- "$cwd";; esac; fi\n` +
        `rm -f ${q(`/etc/systemd/system/${unitName(job.jobId)}`)}\nrm -rf ${q(`${JOB_ROOT}/${job.jobId}`)}\nsystemctl daemon-reload`;
      const result = await this.pool.exec(node, command, { sudo: true, timeoutMs: 30_000, maxOutputBytes: 64 * 1024 });
      if (result.ok) this.cache.delete(job.jobId);
    }
    return listed;
  }

  start(): void {
    if (this.timer) return;
    this.timer = setInterval(() => void this.reconcile().catch(() => void 0), this.config.jobs.pollIntervalMs);
    this.timer.unref();
  }

  dispose(): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = undefined;
  }
}