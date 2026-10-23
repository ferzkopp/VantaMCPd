import { execFile } from "node:child_process";
import { mkdtemp, rm, stat } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { Minimatch } from "minimatch";
import type { ClusterConfig, SourceConfig } from "./config.js";
import { linuxSourceRunner } from "./sources-linux.js";

export function sourcePath(value: string): string {
  if (value === "") return value;
  if (value.length > 1024 || value.split("/").some((part) => !part || part === "." || part === ".." ||
    /[\\:\x00-\x1f\x7f<>"|?*]/.test(part) || /[. ]$/.test(part) ||
    /^(?:con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\.|$)/i.test(part))) {
    throw new Error("Source path must be a safe slash-separated relative path.");
  }
  return value;
}

export function configuredSource(config: ClusterConfig, id: string): SourceConfig {
  const source = (config.sources ?? []).find((candidate) => candidate.id === id);
  if (!source || !source.enabled) throw new Error("Source is unknown or disabled.");
  return source;
}

const GUIDANCE: Record<string, string[]> = {
  SOURCE_READ_ONLY: ["Export requires a source explicitly configured read-write and a successful write probe."],
  DESTINATION_EXISTS: ["Choose a new destination filename. Export never overwrites existing files."],
  TCP_UNREACHABLE: [
    "Check that the SMB server is powered on, reachable from the MCP host, and listening on TCP 445.",
    "On the share server, allow inbound TCP 445 from the MCP host IP only on the trusted network profile. Check router/VLAN rules and host outbound rules too.",
    "On Windows, check File and Printer Sharing (SMB-In) in Windows Defender Firewall. Do not disable the firewall or expose SMB to the Internet.",
  ],
  CREDENTIAL_MISSING: ["On Windows, run scripts/bootstrap.ps1 -ConfigureSources under the account that runs MCP. On Linux, have an administrator configure the encrypted CIFS mount. Never paste credentials into chat."],
  SOURCE_MOUNT_INVALID: ["On Linux, configure mountPath as the exact mountpoint of the approved //server/share CIFS mount using vers=3.1.1,seal. Run MCP under the identity granted access. Vanta never mounts shares or reads mount credentials."],
  SMB_ACCESS_FAILED: [
    "Verify the server/share/root, credentials, SMB3 encryption support, and both share and filesystem read/list permissions.",
    "Windows error 1219 means conflicting credentials for this server. Review existing SMB connections in your terminal; Vanta does not disconnect them automatically.",
    "On Windows, run bootstrap.ps1 -ConfigureSources then -VerifySources. On Linux, check the administrator-managed CIFS mount and credentials. Do not enable SMB1 or disable encryption.",
  ],
};

export class SourceError extends Error {
  constructor(public code: string) { super(code); }
}

export type SourceRunner = (request: Record<string, unknown>, signal?: AbortSignal) => Promise<Record<string, unknown>>;

export function sourceRunner(config: ClusterConfig, platform: NodeJS.Platform = process.platform): SourceRunner {
  if (platform === "win32") return windowsSourceRunner(config);
  if (platform === "linux") return linuxSourceRunner(config);
  return async () => { throw new Error("SMB sources support Windows and Linux MCP hosts."); };
}

export function windowsSourceRunner(config: ClusterConfig): SourceRunner {
  return async (request, signal) => {
    if (process.platform !== "win32") throw new Error("SMB sources currently require a Windows MCP host.");
    const helper = fileURLToPath(new URL("../scripts/smb-source.ps1", import.meta.url));
    return new Promise((resolve, reject) => {
      const child = execFile("powershell.exe", ["-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-File", helper, "-ConfigPath", config.configPath], {
        windowsHide: true, timeout: 120_000, maxBuffer: 4 * 1024 * 1024, signal,
      }, (error, stdout) => {
        if (error) {
          reject(new Error(signal?.aborted ? "Source operation cancelled." : "SMB helper failed or timed out; run bootstrap -VerifySources in your terminal."));
          return;
        }
        try {
          const response = JSON.parse(stdout.replace(/^\uFEFF/, ""));
          if (response.ok !== true) throw new SourceError(Object.hasOwn(GUIDANCE, response.code) ? response.code : "SMB_ACCESS_FAILED");
          resolve(response.result);
        } catch (failure) {
          reject(failure instanceof SyntaxError ? new Error("SMB helper returned an invalid response.") : failure);
        }
      });
      child.stdin?.on("error", () => {});
      child.stdin?.end(JSON.stringify(request));
    });
  };
}

export class SourceClient {
  private busy = false;
  private writable = new Set<string>();
  constructor(private config: ClusterConfig, private run: SourceRunner = sourceRunner(config)) {}

  canExport(id: string): boolean {
    const source = (this.config.sources ?? []).find((candidate) => candidate.id === id);
    return Boolean(source?.enabled && source.access === "read-write" && this.writable.has(id));
  }

  async checkWritable(id: string, signal?: AbortSignal) {
    this.writable.delete(id);
    try {
      if (configuredSource(this.config, id).access !== "read-write") throw new SourceError("SOURCE_READ_ONLY");
      const result = await this.request(id, "check-write", {}, signal);
      if (result.writable !== true) throw new SourceError("SMB_ACCESS_FAILED");
      this.writable.add(id);
      return { sourceId: id, writable: true, message: "Create, write, rename and delete verified.", guidance: [] as string[] };
    } catch (error) {
      if (signal?.aborted) throw error;
      const code = error instanceof SourceError ? error.code : "SMB_ACCESS_FAILED";
      return { sourceId: id, writable: false, message: code, guidance: GUIDANCE[code] ?? GUIDANCE.SMB_ACCESS_FAILED! };
    }
  }

  async exportFile(id: string, relative: string,
    download: (file: string, maximumBytes: number) => Promise<{ bytes: number; sha256: string }>, signal?: AbortSignal) {
    const source = configuredSource(this.config, id);
    sourcePath(relative);
    if (!relative) throw new Error("Export requires a filename.");
    if (!(await this.checkWritable(id, signal)).writable) throw new Error("Source export is unavailable; check write access first.");
    const directory = await mkdtemp(path.join(tmpdir(), "vanta-export-"));
    try {
      const file = path.join(directory, "content");
      const metadata = await download(file, source.maxImportMb * 1024 * 1024);
      signal?.throwIfAborted();
      const staged = await stat(file);
      if (!staged.isFile() || staged.size !== metadata.bytes || staged.size > source.maxImportMb * 1024 * 1024) throw new Error("Invalid export staging size.");
      return await this.request(id, "export", { path: relative, stagePath: file, ...metadata }, signal);
    } catch (error) {
      this.writable.delete(id);
      throw error;
    } finally {
      await rm(directory, { recursive: true, force: true });
    }
  }

  async request(id: string, operation: string, args: Record<string, unknown>, signal?: AbortSignal): Promise<Record<string, unknown>> {
    configuredSource(this.config, id);
    sourcePath(String(args.path ?? ""));
    if (this.busy) throw new Error("Another SMB source operation is active; retry after it finishes.");
    this.busy = true;
    try {
      signal?.throwIfAborted();
      return await this.run({ ...args, sourceId: id, operation }, signal);
    } finally {
      this.busy = false;
    }
  }

  async check(id: string, signal?: AbortSignal) {
    try {
      await this.request(id, "check", {}, signal);
      return { sourceId: id, ok: true, message: "Authenticated encrypted SMB and directory access verified.", guidance: [] as string[] };
    } catch (error) {
      if (signal?.aborted) throw error;
      this.writable.delete(id);
      const code = error instanceof SourceError ? error.code : "SMB_ACCESS_FAILED";
      return { sourceId: id, ok: false, message: code, guidance: GUIDANCE[code] ?? GUIDANCE.SMB_ACCESS_FAILED! };
    }
  }

  async importFile(id: string, relative: string, upload: (file: string, sha256?: string) => Promise<unknown>, signal?: AbortSignal): Promise<unknown> {
    const source = configuredSource(this.config, id);
    sourcePath(relative);
    if (!relative) throw new Error("Import requires a file path.");
    const directory = await mkdtemp(path.join(tmpdir(), "vanta-source-"));
    const file = path.join(directory, "content");
    try {
      const metadata = await this.request(id, "stage", { path: relative, stagePath: file }, signal);
      signal?.throwIfAborted();
      const staged = await stat(file);
      if (!staged.isFile() || staged.size > source.maxImportMb * 1024 * 1024 || staged.size !== metadata.bytes) {
        throw new Error("Staged source file has an invalid size.");
      }
      const artifact = await upload(file, typeof metadata.sha256 === "string" ? metadata.sha256 : undefined);
      return { artifact, source: { sourceId: id, path: relative, bytes: metadata.bytes, modifiedAt: metadata.modifiedAt,
        importedAt: new Date().toISOString() } };
    } finally {
      await rm(directory, { recursive: true, force: true });
    }
  }

  async importFiles(id: string, patterns: string[], upload: (file: string, relative: string, sha256?: string) => Promise<unknown>,
    options: { maxFiles?: number; maxTotalMb?: number } = {}, signal?: AbortSignal) {
    const source = configuredSource(this.config, id);
    const maxFiles = options.maxFiles ?? 32;
    const maxTotalMb = options.maxTotalMb ?? 256;
    if (!Number.isInteger(maxFiles) || maxFiles < 1 || maxFiles > 128 || !Number.isInteger(maxTotalMb) || maxTotalMb < 1 || maxTotalMb > 1024) {
      throw new Error("Invalid batch import limits.");
    }
    if (!patterns.length || patterns.length > 16) throw new Error("Provide between 1 and 16 glob patterns.");
    const matchers = patterns.map((pattern) => {
      if (!pattern || pattern.length > 1024 || /[\\:\x00-\x1f\x7f<>"|]/.test(pattern) ||
          pattern.split("/").some((part) => !part || part === "." || part === "..")) throw new Error("Glob patterns must be safe source-relative paths.");
      return new Minimatch(pattern, { nocase: true, dot: false, nonegate: true, nocomment: true, nobrace: true, noext: true });
    });
    const listing = await this.request(id, "scan", { path: "", limit: 5000, offset: 0, recursive: patterns.some((pattern) => pattern.includes("/")) }, signal);
    if (listing.truncated !== false || !Array.isArray(listing.entries)) throw new Error("Source scan was incomplete; narrow the configured root before batch import.");
    const entries = listing.entries as { path: string; directory: boolean; bytes: number }[];
    const selected = entries.filter((entry) => !entry.directory && typeof entry.path === "string" && matchers.some((matcher) => matcher.match(entry.path)))
      .sort((left, right) => left.path.localeCompare(right.path));
    if (selected.length > maxFiles) throw new Error(`Glob matched ${selected.length} files; maximum is ${maxFiles}. Narrow the pattern or raise maxFiles.`);
    let totalBytes = 0;
    for (const entry of selected) {
      sourcePath(entry.path);
      if (!Number.isSafeInteger(entry.bytes) || entry.bytes < 0 || entry.bytes > source.maxImportMb * 1024 * 1024) throw new Error("A selected file exceeds the source transfer limit.");
      totalBytes += entry.bytes;
    }
    if (totalBytes > maxTotalMb * 1024 * 1024) throw new Error("Selected files exceed maxTotalMb; narrow the pattern or raise the batch limit.");
    const results: { path: string; ok: boolean; result?: unknown; error?: string }[] = [];
    let stagedBytes = 0;
    for (const entry of selected) {
      signal?.throwIfAborted();
      try {
        const result = await this.importFile(id, entry.path, async (file, sha256) => {
          const actualBytes = (await stat(file)).size;
          if (stagedBytes + actualBytes > maxTotalMb * 1024 * 1024) throw new Error("Files changed beyond the batch byte limit.");
          stagedBytes += actualBytes;
          return upload(file, entry.path, sha256);
        }, signal);
        results.push({ path: entry.path, ok: true, result });
      } catch (error) {
        if (signal?.aborted) throw error;
        results.push({ path: entry.path, ok: false, error: "Import failed; check source access, file changes and artifact-storage quotas." });
      }
    }
    return { sourceId: id, matched: selected.length, totalBytes, stagedBytes, imported: results.filter((result) => result.ok).length,
      complete: results.every((result) => result.ok), results };
  }
}