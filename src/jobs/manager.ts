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
const RUNNER_VERSION = "1";
const RUNNER_PATH = `/opt/vantamcpd/job-runner/${RUNNER_VERSION}/remote-runner.py`;
const QUEUED_START_GRACE_MS = 60_000;

export interface SubmitJobInput {
  kind: string;
  moduleId?: string;
  resourceKeys: string[];
  command: string[];
  cwd: string;
  environment?: Record<string, string>;
  timeoutMs: number;
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

export class JobManager {
  private readonly cache = new Map<string, JobState>();
  private readonly settledListeners = new Set<(job: JobState) => void>();
  private refreshedAt?: string;
  private timer?: NodeJS.Timeout;
  private readonly runner: string;

  constructor(
    private readonly config: ClusterConfig,
    private readonly pool: SshPool,
    private readonly registry: JobRegistry,
    runnerPath = path.join(path.dirname(fileURLToPath(import.meta.url)), "remote-runner.py"),
  ) {
    this.runner = readFileSync(runnerPath, "utf8");
  }

  async submit(node: ResolvedNode, input: SubmitJobInput): Promise<JobState> {
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

    const active = (await this.list([node])).jobs.find(
      (job) => !isTerminalJobStatus(job.status) && job.resourceKeys.some((key) => spec.resourceKeys.includes(key)),
    );
    if (active) throw new Error(`Resource is busy with job ${active.jobId} (${active.kind}, ${active.status}).`);

    const directory = `${JOB_ROOT}/${jobId}`;
    const runnerHash = createHash("sha256").update(this.runner).digest("hex");
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
    const command = [
      "set -e",
      `install -d -m 0755 ${q(path.posix.dirname(RUNNER_PATH))} ${q(JOB_ROOT)}`,
      `if [ ! -f ${q(RUNNER_PATH)} ] || [ "$(sha256sum ${q(RUNNER_PATH)} | awk '{print $1}')" != ${q(runnerHash)} ]; then ` +
        `printf '%s' ${q(encode(this.runner))} | base64 -d | install -m 0755 /dev/stdin ${q(RUNNER_PATH)}; fi`,
      `install -d -m 0700 ${q(directory)}`,
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
    const command = `systemctl stop ${q(unitName(job.jobId))} || true\n/usr/bin/python3 ${q(RUNNER_PATH)} ${q(specPath)} --cancel`;
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
        const command = `state=$(systemctl is-active ${q(unitName(job.jobId))} 2>/dev/null || true); ` +
          `case "$state" in ${activeStates}) printf '%s' '__ACTIVE__';; *) ` +
          `/usr/bin/python3 ${q(RUNNER_PATH)} ${q(`${JOB_ROOT}/${job.jobId}/spec.json`)} --fail ${q(message)}; ` +
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