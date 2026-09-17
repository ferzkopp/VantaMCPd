import { AsyncLocalStorage } from "node:async_hooks";
import { appendFile, mkdirSync, readdirSync, statSync, unlinkSync } from "node:fs";
import path from "node:path";

export type AuditKind = "exec" | "sftp" | "connect";
export type AuditOrigin = "agent" | "engine";

export interface AuditResultMetadata {
  complete?: boolean;
  truncationReasons?: string[];
  responseBytes?: number;
  responseLimitBytes?: number;
  responseLimitPercent?: number;
}

export interface AuditEvent {
  seq: number;
  ts: string;
  node: string;
  host: string;
  kind: AuditKind;
  origin: AuditOrigin;
  module: string;
  tool?: string;
  parameters?: string;
  command?: string;
  sudo: boolean;
  ok: boolean;
  code: number | null;
  durationMs: number;
  bytesOut: number;
  bytesErr: number;
  truncated?: boolean;
  timedOut?: boolean;
  error?: string;
  preview?: string;
  result?: AuditResultMetadata;
}

export interface AuditOptions {
  logDir: string;
  maxEvents: number;
  maxLogMb: number;
  logOutput: boolean;
}

/** Which MCP tool is on the stack, so an SSH call can be attributed without threading a parameter through every call site. */
const toolContext = new AsyncLocalStorage<{
  tool: string;
  module: string;
  origin: AuditOrigin;
  parameters?: string;
  result: AuditResultMetadata;
}>();
export const CORE_MODULE = "core";

function moduleFromParameters(parameters: unknown): string {
  if (typeof parameters !== "object" || parameters === null || Array.isArray(parameters)) return CORE_MODULE;
  const moduleId = (parameters as Record<string, unknown>).moduleId;
  return typeof moduleId === "string" && moduleId.length > 0 ? moduleId : CORE_MODULE;
}

export function withTool<T>(tool: string, fn: () => T): T {
  return toolContext.run({ tool, module: CORE_MODULE, origin: "engine", result: {} }, fn);
}

export function withToolParameters<T>(tool: string, parameters: unknown, fn: () => T): T {
  return toolContext.run({
    tool,
    module: moduleFromParameters(parameters),
    origin: "agent",
    parameters: formatParameters(parameters),
    result: {},
  }, fn);
}

export function currentTool(): string | undefined {
  return toolContext.getStore()?.tool;
}

export function currentAuditAttribution(): Pick<AuditEvent, "module" | "tool" | "origin" | "parameters" | "result"> {
  const context = toolContext.getStore();
  return {
    module: context?.module ?? CORE_MODULE,
    tool: context?.tool,
    origin: context?.origin ?? "engine",
    parameters: context?.parameters,
    result: context?.result,
  };
}

export function setCurrentAuditResult(result: AuditResultMetadata): void {
  const current = toolContext.getStore()?.result;
  if (current) Object.assign(current, result);
}

const COMMAND_MAX = 2000;
const PARAMETERS_MAX = 1000;
const PREVIEW_MAX = 400;
const LOG_FILE_RE = /^vanta-\d{4}-\d{2}-\d{2}\.jsonl$/;
const SENSITIVE_PARAMETER_KEY = /pass(?:word|wd)?|secret|token|api[_-]?key|authorization|private[_-]?key/i;
const URL_PARAMETER_KEY = /^(?:url|uri)$|(?:Url|URL|Uri|URI)$/;

/**
 * Secrets normally travel on stdin rather than the command line, but a caller can still paste one
 * into a heredoc or an env assignment. Blunt-force redact the usual shapes before anything is stored.
 */
const REDACTIONS: { re: RegExp; to: string }[] = [
  { re: /(-{1,2}pass(word|wd)?[= ]\s*)(\S+)/gi, to: "$1***" },
  { re: /((?:password|passwd|secret|token|api[_-]?key)\s*[:=]\s*)(["']?)([^\s"']+)\2/gi, to: "$1$2***$2" },
  { re: /(Authorization:\s*(?:Bearer|Basic)\s+)(\S+)/gi, to: "$1***" },
  { re: /(-----BEGIN [A-Z ]*PRIVATE KEY-----)[\s\S]*?(-----END [A-Z ]*PRIVATE KEY-----)/g, to: "$1***$2" },
];

export function redact(text: string): string {
  let out = text;
  for (const r of REDACTIONS) out = out.replace(r.re, r.to);
  return out;
}

function clip(text: string, max: number): string {
  const t = redact(text);
  return t.length <= max ? t : `${t.slice(0, max)}… (+${t.length - max} bytes)`;
}

function sanitizeParameterUrl(value: string): string {
  try {
    const parsed = new URL(value);
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return `${parsed.protocol}[redacted]`;
    parsed.username = "";
    parsed.password = "";
    parsed.search = "";
    parsed.hash = "";
    return parsed.toString();
  } catch {
    return "[invalid-url]";
  }
}

function formatParameters(parameters: unknown): string | undefined {
  if (parameters === undefined) return undefined;
  const seen = new WeakSet<object>();
  try {
    const serialized = JSON.stringify(parameters, (key, value: unknown) => {
      if (key && SENSITIVE_PARAMETER_KEY.test(key)) return "***";
      if (key && URL_PARAMETER_KEY.test(key) && typeof value === "string") return sanitizeParameterUrl(value);
      if (typeof value === "bigint") return value.toString();
      if (typeof value === "object" && value !== null) {
        if (seen.has(value)) return "[Circular]";
        seen.add(value);
      }
      return value;
    });
    return serialized === undefined ? undefined : clip(serialized, PARAMETERS_MAX);
  } catch {
    return clip(String(parameters), PARAMETERS_MAX);
  }
}

type Listener = (event: AuditEvent) => void;

/** Single definition of an event's status label, shared by the filter, the list and the dropdown. */
export function statusKey(e: Pick<AuditEvent, "ok" | "code">): string {
  if (e.ok) return "ok";
  return e.code === null ? "error" : `exit ${e.code}`;
}

export class AuditLog {
  private readonly events: AuditEvent[] = [];
  private readonly listeners = new Set<Listener>();
  private seq = 0;
  private file?: string;
  private fileDay = "";

  constructor(private readonly opts: AuditOptions) {
    try {
      mkdirSync(opts.logDir, { recursive: true });
    } catch (err) {
      process.stderr.write(`audit log dir ${opts.logDir} unusable: ${(err as Error).message}; keeping events in memory only\n`);
    }
    this.prune();
  }

  /**
   * Keeps the log directory under its size budget by deleting whole days, oldest first. The file
   * currently being written is never removed, so a single very busy day can still exceed the budget -
   * it is reported rather than silently truncated.
   */
  private prune(): void {
    const budget = this.opts.maxLogMb * 1024 * 1024;
    let files: { path: string; size: number }[];
    try {
      files = readdirSync(this.opts.logDir)
        .filter((f) => LOG_FILE_RE.test(f))
        .sort()
        .map((f) => {
          const full = path.join(this.opts.logDir, f);
          return { path: full, size: statSync(full).size };
        });
    } catch {
      return;
    }

    let total = files.reduce((sum, f) => sum + f.size, 0);
    if (total <= budget) return;

    const removed: string[] = [];
    for (const f of files) {
      if (total <= budget) break;
      if (f.path === this.file) break;
      try {
        unlinkSync(f.path);
        total -= f.size;
        removed.push(path.basename(f.path));
      } catch {
        /* a locked file just stays */
      }
    }
    if (removed.length > 0) {
      process.stderr.write(`audit log pruned to ${this.opts.maxLogMb}MB: removed ${removed.join(", ")}\n`);
    } else if (total > budget) {
      process.stderr.write(
        `audit log is ${Math.round(total / 1024 / 1024)}MB, over the ${this.opts.maxLogMb}MB budget, ` +
          `but only today's file remains - raise monitoring.maxLogMb or lower the traffic\n`,
      );
    }
  }

  private target(now: Date): string | undefined {
    const day = now.toISOString().slice(0, 10);
    if (day !== this.fileDay) {
      this.fileDay = day;
      this.file = path.join(this.opts.logDir, `vanta-${day}.jsonl`);
      this.prune();
    }
    return this.file;
  }

  record(
    input: Omit<AuditEvent, "seq" | "ts" | "module" | "tool" | "origin" | "parameters"> & {
      module?: string;
      tool?: string;
      origin?: AuditOrigin;
      parameters?: string;
    },
  ): AuditEvent {
    const now = new Date();
    const context = toolContext.getStore();
    const inputResult = input.result ?? context?.result;
    const result = inputResult && Object.keys(inputResult).length > 0
      ? {
          ...inputResult,
          ...(inputResult.responseLimitBytes === undefined
            ? {}
            : {
                responseBytes: input.bytesOut,
                responseLimitPercent: Math.round(input.bytesOut * 10_000 / inputResult.responseLimitBytes) / 100,
              }),
        }
      : undefined;
    const event: AuditEvent = {
      ...input,
      module: input.module ?? context?.module ?? CORE_MODULE,
      tool: input.tool ?? context?.tool,
      origin: input.origin ?? context?.origin ?? "engine",
      parameters:
        input.parameters === undefined ? context?.parameters : clip(input.parameters, PARAMETERS_MAX),
      seq: ++this.seq,
      ts: now.toISOString(),
      command: input.command === undefined ? undefined : clip(input.command, COMMAND_MAX),
      preview: this.opts.logOutput && input.preview ? clip(input.preview, PREVIEW_MAX) : undefined,
      error: input.error === undefined ? undefined : clip(input.error, PREVIEW_MAX),
      result,
    };

    this.events.push(event);
    if (this.events.length > this.opts.maxEvents) this.events.splice(0, this.events.length - this.opts.maxEvents);

    const file = this.target(now);
    if (file) {
      // Fire-and-forget: a full disk must never break a cluster command.
      appendFile(file, `${JSON.stringify(event)}\n`, () => void 0);
    }
    for (const l of this.listeners) {
      try {
        l(event);
      } catch {
        /* a broken SSE client must not break logging */
      }
    }
    return event;
  }

  query(filter: {
    since?: number;
    node?: string;
    module?: string;
    status?: string;
    q?: string;
    includeEngine?: boolean;
    limit?: number;
  }): AuditEvent[] {
    const needle = filter.q?.toLowerCase();
    const limit = Math.min(Math.max(filter.limit ?? 200, 1), 2000);
    const matched = this.events.filter((e) => {
      if (filter.since !== undefined && e.seq <= filter.since) return false;
      if (filter.node && e.node !== filter.node) return false;
      if (filter.module && e.module !== filter.module) return false;
      if (filter.status && statusKey(e) !== filter.status) return false;
      if (filter.includeEngine === false && e.origin === "engine") return false;
      if (needle) {
        const hay = `${e.node} ${e.origin} ${e.module} ${e.tool ?? ""} ${e.parameters ?? ""} ${e.command ?? ""} ${e.error ?? ""} ${e.preview ?? ""} ${e.result?.complete ?? ""} ${e.result?.truncationReasons?.join(" ") ?? ""}`.toLowerCase();
        if (!hay.includes(needle)) return false;
      }
      return true;
    });
    return matched.slice(-limit);
  }

  /** Per-node rollup for the dashboard. */
  summary(): {
    node: string;
    host: string;
    total: number;
    failed: number;
    totalMs: number;
    avgMs: number;
    bytes: number;
    lastTs?: string;
    lastTool?: string;
  }[] {
    const byNode = new Map<string, ReturnType<AuditLog["summary"]>[number]>();
    for (const e of this.events) {
      let row = byNode.get(e.node);
      if (!row) {
        row = { node: e.node, host: e.host, total: 0, failed: 0, totalMs: 0, avgMs: 0, bytes: 0 };
        byNode.set(e.node, row);
      }
      row.total++;
      if (!e.ok) row.failed++;
      row.totalMs += e.durationMs;
      row.bytes += e.bytesOut + e.bytesErr;
      row.lastTs = e.ts;
      if (e.origin === "agent" && e.tool !== undefined) row.lastTool = e.tool;
    }
    for (const row of byNode.values()) row.avgMs = row.total ? Math.round(row.totalMs / row.total) : 0;
    return [...byNode.values()].sort((a, b) => a.node.localeCompare(b.node));
  }

  nodes(): string[] {
    return [...new Set(this.events.map((e) => e.node))].sort();
  }

  modules(): string[] {
    return [...new Set(this.events.map((event) => event.module))].sort((a, b) => {
      if (a === CORE_MODULE) return -1;
      if (b === CORE_MODULE) return 1;
      return a.localeCompare(b);
    });
  }

  /** Observed status labels, "ok" first then exit codes ascending. */
  statuses(includeEngine = true): string[] {
    const events = includeEngine ? this.events : this.events.filter((event) => event.origin !== "engine");
    const seen = [...new Set(events.map(statusKey))];
    return seen.sort((a, b) => {
      if (a === "ok") return -1;
      if (b === "ok") return 1;
      const na = Number(a.replace("exit ", ""));
      const nb = Number(b.replace("exit ", ""));
      if (Number.isFinite(na) && Number.isFinite(nb)) return na - nb;
      return a.localeCompare(b);
    });
  }

  get lastSeq(): number {
    return this.seq;
  }

  subscribe(listener: Listener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }
}
