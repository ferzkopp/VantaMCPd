import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { Client, type ClientChannel, type ConnectConfig, type SFTPWrapper } from "ssh2";
import { currentAuditAttribution, type AuditLog } from "./audit.js";
import { ENV, type ClusterConfig, type ResolvedNode } from "./config.js";
import { q } from "./security.js";

export interface ExecResult {
  node: string;
  host: string;
  ok: boolean;
  code: number | null;
  signal?: string;
  stdout: string;
  stderr: string;
  durationMs: number;
  truncated: boolean;
  timedOut: boolean;
  error?: string;
}

export interface ExecOptions {
  sudo?: boolean;
  timeoutMs?: number;
  maxOutputBytes?: number;
  stdin?: string;
  cwd?: string;
  env?: Record<string, string>;
}

/** Host-key trust-on-first-use store. Prevents silent MITM on the LAN. */
class KnownHosts {
  private readonly map: Record<string, string>;

  constructor(private readonly file: string) {
    this.map = KnownHosts.load(file);
  }

  /** A corrupt or BOM-prefixed store must not stop the daemon from starting. */
  private static load(file: string): Record<string, string> {
    if (!existsSync(file)) return {};
    try {
      return JSON.parse(readFileSync(file, "utf8").replace(/^\uFEFF/, "")) as Record<string, string>;
    } catch (err) {
      process.stderr.write(
        `known_hosts store ${file} is unreadable (${(err as Error).message}); starting empty and re-pinning on first connect.\n`,
      );
      return {};
    }
  }

  static fingerprint(key: Buffer): string {
    return `SHA256:${createHash("sha256").update(key).digest("base64").replace(/=+$/, "")}`;
  }

  verify(hostId: string, key: Buffer, strict: boolean): true | string {
    const fp = KnownHosts.fingerprint(key);
    const known = this.map[hostId];
    if (!known) {
      this.map[hostId] = fp;
      mkdirSync(path.dirname(this.file), { recursive: true });
      writeFileSync(this.file, JSON.stringify(this.map, null, 2), { mode: 0o600 });
      return true;
    }
    if (known === fp) return true;
    if (!strict) return true;
    return (
      `Host key mismatch for ${hostId}.\n  expected ${known}\n  got      ${fp}\n` +
      `This can mean the node was reimaged - or that traffic is being intercepted. ` +
      `If the change is expected, remove the entry from ${this.file}.`
    );
  }
}

class NodeSession {
  private client?: Client;
  private connecting?: Promise<Client>;

  constructor(
    readonly node: ResolvedNode,
    private readonly knownHosts: KnownHosts,
  ) {}

  private buildConnectConfig(): ConnectConfig {
    const hostId = `${this.node.host}:${this.node.port}`;
    const base: ConnectConfig = {
      host: this.node.host,
      port: this.node.port,
      username: this.node.user,
      readyTimeout: this.node.connectTimeoutMs,
      keepaliveInterval: 15_000,
      keepaliveCountMax: 4,
      hostVerifier: (key: Buffer) => {
        const result = this.knownHosts.verify(hostId, key, this.node.strictHostKeyChecking);
        if (result === true) return true;
        throw new Error(result);
      },
    };

    if (this.node.auth === "password") {
      const password = process.env[ENV.sshPassword];
      if (!password) {
        throw new Error(`Node ${this.node.name} uses password auth but ${ENV.sshPassword} is not set.`);
      }
      return { ...base, password };
    }

    if (!existsSync(this.node.privateKeyPath)) {
      throw new Error(
        `Private key not found for ${this.node.name}: ${this.node.privateKeyPath}. ` +
          `Generate and deploy the configured SSH key before starting VantaMCPd.`,
      );
    }
    const passphrase = process.env[ENV.keyPassphrase];
    return {
      ...base,
      privateKey: readFileSync(this.node.privateKeyPath),
      ...(passphrase ? { passphrase } : {}),
    };
  }

  async connect(): Promise<Client> {
    if (this.client) return this.client;
    if (this.connecting) return this.connecting;

    this.connecting = new Promise<Client>((resolve, reject) => {
      let cfg: ConnectConfig;
      try {
        cfg = this.buildConnectConfig();
      } catch (err) {
        reject(err);
        return;
      }
      const client = new Client();
      const onError = (err: Error) => {
        client.removeAllListeners();
        client.end();
        this.client = undefined;
        reject(new Error(`SSH connect to ${this.node.name} (${this.node.host}:${this.node.port}) failed: ${err.message}`));
      };
      client.once("error", onError);
      client.once("ready", () => {
        client.removeListener("error", onError);
        client.on("error", () => void 0);
        client.once("close", () => {
          this.client = undefined;
        });
        this.client = client;
        resolve(client);
      });
      client.connect(cfg);
    }).finally(() => {
      this.connecting = undefined;
    });

    return this.connecting;
  }

  async exec(rawCommand: string, opts: ExecOptions, maxOutputBytes: number): Promise<ExecResult> {
    const started = Date.now();
    const base: Omit<ExecResult, "code" | "stdout" | "stderr" | "ok"> = {
      node: this.node.name,
      host: this.node.host,
      durationMs: 0,
      truncated: false,
      timedOut: false,
    };

    let client: Client;
    try {
      client = await this.connect();
    } catch (err) {
      return {
        ...base,
        ok: false,
        code: null,
        stdout: "",
        stderr: "",
        durationMs: Date.now() - started,
        error: (err as Error).message,
      };
    }

    const { command, stdin } = this.wrap(rawCommand, opts);
    const limit = opts.maxOutputBytes ?? maxOutputBytes;
    const timeoutMs = opts.timeoutMs ?? this.node.commandTimeoutMs;

    return new Promise<ExecResult>((resolve) => {
      client.exec(command, (err, stream) => {
        if (err) {
          // The cached client is unusable once it refuses to open a channel; force a fresh handshake.
          this.dispose();
          resolve({
            ...base,
            ok: false,
            code: null,
            stdout: "",
            stderr: "",
            durationMs: Date.now() - started,
            error: err.message,
          });
          return;
        }

        const out: Buffer[] = [];
        const errOut: Buffer[] = [];
        let outLen = 0;
        let errLen = 0;
        let truncated = false;
        let timedOut = false;
        let settled = false;
        let killTimer: NodeJS.Timeout | undefined;

        const timer = setTimeout(() => {
          timedOut = true;
          // Closing the channel leaves the remote process running, so ask the server to signal it.
          // OpenSSH ignores signal requests, which is why close() stays as the backstop.
          try {
            stream.signal("TERM");
          } catch {
            /* server does not support signal requests */
          }
          killTimer = setTimeout(() => {
            try {
              stream.signal("KILL");
            } catch {
              /* server does not support signal requests */
            }
            stream.close();
          }, 2_000);
          killTimer.unref();
        }, timeoutMs);

        const push = (chunks: Buffer[], chunk: Buffer, len: number): number => {
          if (len >= limit) {
            truncated = true;
            return len;
          }
          const room = limit - len;
          chunks.push(chunk.length > room ? chunk.subarray(0, room) : chunk);
          if (chunk.length > room) truncated = true;
          return len + Math.min(chunk.length, room);
        };

        const finish = (code: number | null, signal?: string) => {
          if (settled) return;
          settled = true;
          clearTimeout(timer);
          if (killTimer) clearTimeout(killTimer);
          resolve({
            ...base,
            ok: !timedOut && code === 0,
            code,
            signal,
            stdout: Buffer.concat(out).toString("utf8"),
            stderr: Buffer.concat(errOut).toString("utf8"),
            durationMs: Date.now() - started,
            truncated,
            timedOut,
            ...(timedOut ? { error: `Command timed out after ${timeoutMs}ms` } : {}),
          });
        };

        stream.on("data", (chunk: Buffer) => {
          outLen = push(out, chunk, outLen);
        });
        stream.stderr.on("data", (chunk: Buffer) => {
          errLen = push(errOut, chunk, errLen);
        });
        stream.on("close", (code: number | null, signal?: string) => finish(code, signal));
        stream.on("error", (e: Error) => {
          if (settled) return;
          settled = true;
          clearTimeout(timer);
          if (killTimer) clearTimeout(killTimer);
          // A half-open socket poisons the cached client: every later call would fail the same way.
          this.dispose();
          resolve({
            ...base,
            ok: false,
            code: null,
            stdout: Buffer.concat(out).toString("utf8"),
            stderr: Buffer.concat(errOut).toString("utf8"),
            durationMs: Date.now() - started,
            truncated,
            timedOut,
            error: e.message,
          });
        });

        if (stdin !== undefined) stream.end(stdin);
        else stream.end();
      });
    });
  }

  /** Wrap a command with sudo / env / cwd. Secrets are passed on stdin, never on the command line. */
  private wrap(command: string, opts: ExecOptions): { command: string; stdin?: string } {
    let inner = command;
    if (opts.cwd) inner = `cd ${q(opts.cwd)} && ${inner}`;
    if (opts.env) {
      const assigns = Object.entries(opts.env)
        .map(([k, v]) => `${k}=${q(v)}`)
        .join(" ");
      if (assigns) inner = `export ${assigns}; ${inner}`;
    }

    const payload = Buffer.from(inner, "utf8").toString("base64");
    const decode = `printf '%s' ${q(payload)} | base64 -d`;

    if (!opts.sudo) {
      return { command: `${decode} | bash -s` };
    }
    if (this.node.sudo === "none") {
      throw new Error(`Node ${this.node.name} is configured with sudo: "none" but a privileged command was requested.`);
    }
    if (this.node.sudo === "nopasswd") {
      return { command: `${decode} | sudo -n -H bash -s` };
    }
    const pw = process.env[ENV.sudoPassword];
    if (!pw) {
      throw new Error(`Node ${this.node.name} needs a sudo password; set ${ENV.sudoPassword} in .env.`);
    }
    // Password first on stdin, then the script, both consumed by sudo/bash respectively.
    return {
      command: `sudo -S -p '' -H bash -c ${q(`payload=$(cat); printf '%s' "$payload" | base64 -d | bash -s`)}`,
      stdin: `${pw}\n${payload}`,
    };
  }

  async sftp(): Promise<SFTPWrapper> {
    const client = await this.connect();
    return new Promise((resolve, reject) => {
      client.sftp((err, sftp) => {
        if (err) {
          this.dispose();
          reject(err);
          return;
        }
        resolve(sftp);
      });
    });
  }

  async openProcess(rawCommand: string, opts: Pick<ExecOptions, "cwd" | "env">): Promise<ClientChannel> {
    const client = await this.connect();
    let inner = rawCommand;
    if (opts.cwd) inner = `cd ${q(opts.cwd)} && ${inner}`;
    if (opts.env) {
      const assigns = Object.entries(opts.env)
        .map(([key, value]) => `${key}=${q(value)}`)
        .join(" ");
      if (assigns) inner = `export ${assigns}; ${inner}`;
    }
    const payload = Buffer.from(inner, "utf8").toString("base64");
    const command = `bash -c "$(printf '%s' ${q(payload)} | base64 -d)"`;
    return new Promise((resolve, reject) => {
      client.exec(command, (err, stream) => {
        if (err) {
          this.dispose();
          reject(err);
          return;
        }
        resolve(stream);
      });
    });
  }

  dispose(): void {
    this.client?.end();
    this.client = undefined;
  }
}

export class SshPool {
  private readonly sessions = new Map<string, NodeSession>();
  private readonly knownHosts: KnownHosts;

  constructor(
    private readonly config: ClusterConfig,
    private readonly audit?: AuditLog,
  ) {
    this.knownHosts = new KnownHosts(config.knownHostsPath);
  }

  private session(node: ResolvedNode): NodeSession {
    let s = this.sessions.get(node.name);
    if (!s) {
      s = new NodeSession(node, this.knownHosts);
      this.sessions.set(node.name, s);
    }
    return s;
  }

  async exec(node: ResolvedNode, command: string, opts: ExecOptions = {}): Promise<ExecResult> {
    let result: ExecResult;
    try {
      result = await this.session(node).exec(command, opts, this.config.security.maxOutputBytes);
    } catch (err) {
      result = {
        node: node.name,
        host: node.host,
        ok: false,
        code: null,
        stdout: "",
        stderr: "",
        durationMs: 0,
        truncated: false,
        timedOut: false,
        error: (err as Error).message,
      };
    }
    this.audit?.record({
      node: result.node,
      host: result.host,
      kind: "exec",
      command,
      sudo: opts.sudo === true,
      ok: result.ok,
      code: result.code,
      durationMs: result.durationMs,
      bytesOut: Buffer.byteLength(result.stdout, "utf8"),
      bytesErr: Buffer.byteLength(result.stderr, "utf8"),
      truncated: result.truncated || undefined,
      timedOut: result.timedOut || undefined,
      error: result.error,
      preview: result.stdout || result.stderr || undefined,
    });
    return result;
  }

  /** Run the same command on many nodes with bounded concurrency. */
  async execMany(nodes: ResolvedNode[], command: string, opts: ExecOptions = {}): Promise<ExecResult[]> {
    return mapLimit(nodes, this.config.maxConcurrency, (n) => this.exec(n, command, opts));
  }

  async sftp(node: ResolvedNode): Promise<SFTPWrapper> {
    const started = Date.now();
    try {
      const handle = await this.session(node).sftp();
      this.audit?.record({
        node: node.name,
        host: node.host,
        kind: "sftp",
        command: "open sftp session",
        sudo: false,
        ok: true,
        code: 0,
        durationMs: Date.now() - started,
        bytesOut: 0,
        bytesErr: 0,
      });
      return handle;
    } catch (err) {
      this.audit?.record({
        node: node.name,
        host: node.host,
        kind: "sftp",
        command: "open sftp session",
        sudo: false,
        ok: false,
        code: null,
        durationMs: Date.now() - started,
        bytesOut: 0,
        bytesErr: 0,
        error: (err as Error).message,
      });
      throw err;
    }
  }

  async openProcess(
    node: ResolvedNode,
    command: string,
    opts: Pick<ExecOptions, "cwd" | "env"> = {},
  ): Promise<ClientChannel> {
    const started = Date.now();
    const attribution = currentAuditAttribution();
    try {
      const stream = await this.session(node).openProcess(command, opts);
      let code: number | null = null;
      let bytesOut = 0;
      let bytesErr = 0;
      const stderr: Buffer[] = [];
      let stderrBytes = 0;
      stream.on("data", (chunk: Buffer) => {
        bytesOut += chunk.length;
      });
      stream.stderr.on("data", (chunk: Buffer) => {
        bytesErr += chunk.length;
        const room = Math.max(0, this.config.security.maxOutputBytes - stderrBytes);
        if (room > 0) {
          stderr.push(chunk.length > room ? chunk.subarray(0, room) : chunk);
          stderrBytes += Math.min(chunk.length, room);
        }
      });
      stream.on("exit", (exitCode: number | null) => {
        code = exitCode;
      });
      stream.once("close", () => {
        this.audit?.record({
          ...attribution,
          node: node.name,
          host: node.host,
          kind: "exec",
          command,
          sudo: false,
          ok: code === 0,
          code,
          durationMs: Date.now() - started,
          bytesOut,
          bytesErr,
          preview: Buffer.concat(stderr).toString("utf8") || undefined,
        });
      });
      return stream;
    } catch (err) {
      this.audit?.record({
        ...attribution,
        node: node.name,
        host: node.host,
        kind: "exec",
        command,
        sudo: false,
        ok: false,
        code: null,
        durationMs: Date.now() - started,
        bytesOut: 0,
        bytesErr: 0,
        error: (err as Error).message,
      });
      throw err;
    }
  }

  disposeAll(): void {
    for (const s of this.sessions.values()) s.dispose();
    this.sessions.clear();
  }
}

export async function mapLimit<T, R>(items: T[], limit: number, fn: (item: T, index: number) => Promise<R>): Promise<R[]> {
  const results = new Array<R>(items.length);
  let cursor = 0;
  const workers = Array.from({ length: Math.min(Math.max(1, limit), items.length || 1) }, async () => {
    for (;;) {
      const index = cursor++;
      if (index >= items.length) return;
      results[index] = await fn(items[index] as T, index);
    }
  });
  await Promise.all(workers);
  return results;
}
