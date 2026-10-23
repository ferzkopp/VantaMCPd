import { createHash, randomUUID } from "node:crypto";
import { constants } from "node:fs";
import { lstat, open, opendir, readFile, link, unlink } from "node:fs/promises";
import path from "node:path";
import type { ClusterConfig, SourceConfig } from "./config.js";
import { configuredSource, sourcePath, SourceError, type SourceRunner } from "./sources.js";

function unescapeMount(value: string): string {
  return value.replace(/\\(040|011|012|134)/g, (_match, octal: string) => String.fromCharCode(parseInt(octal, 8)));
}

export function validateLinuxMount(source: SourceConfig, mountInfo: string): string {
  if (!source.mountPath) throw new SourceError("SOURCE_MOUNT_INVALID");
  const expected = path.resolve(source.mountPath);
  let verified = false;
  for (const line of mountInfo.trim().split("\n")) {
    const [before, after] = line.split(" - ");
    if (!before || !after) continue;
    const fields = before.split(" ");
    const [filesystem, device, options = ""] = after.split(" ");
    const mountpoint = path.resolve(unescapeMount(fields[4] ?? ""));
    if (mountpoint.startsWith(expected + path.sep)) throw new SourceError("SOURCE_MOUNT_INVALID");
    if (mountpoint !== expected) continue;
    const settings = new Set(options.split(","));
    verified = filesystem === "cifs" && fields[3] === "/" &&
      unescapeMount(device ?? "").toLowerCase() === `//${source.server}/${source.share}`.toLowerCase() &&
      settings.has("seal") && [...settings].some((option) => /^vers=3(?:\.0(?:\.2)?|\.1\.1)?$/.test(option));
    if (!verified) throw new SourceError("SOURCE_MOUNT_INVALID");
  }
  if (!verified) throw new SourceError("SOURCE_MOUNT_INVALID");
  return expected;
}

async function safePath(base: string, relative: string): Promise<string> {
  sourcePath(relative);
  const target = path.join(base, ...relative.split("/").filter(Boolean));
  const parsed = path.parse(target);
  let current = parsed.root;
  for (const component of target.slice(parsed.root.length).split(path.sep)) {
    current = path.join(current, component);
    const info = await lstat(current);
    if (info.isSymbolicLink() || (!info.isDirectory() && current !== target)) throw new SourceError("SMB_ACCESS_FAILED");
  }
  return target;
}

function metadata(relative: string, info: Awaited<ReturnType<typeof lstat>>) {
  return { name: path.basename(relative), path: relative, directory: info.isDirectory(),
    bytes: info.isDirectory() ? 0 : info.size, modifiedAt: info.mtime.toISOString() };
}

async function copyVerified(input: string, output: string, maximum: number, signal?: AbortSignal,
  onCreated?: () => void, verifyOutput = false) {
  const reader = await open(input, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0));
  let writer;
  try {
    const before = await reader.stat();
    if (!before.isFile() || before.size > maximum) throw new SourceError("SMB_ACCESS_FAILED");
    writer = await open(output, "wx+", 0o600);
    onCreated?.();
    const digest = createHash("sha256");
    const buffer = Buffer.alloc(512 * 1024);
    let bytes = 0;
    while (true) {
      signal?.throwIfAborted();
      const chunk = await reader.read(buffer, 0, buffer.length, null);
      if (!chunk.bytesRead) break;
      bytes += chunk.bytesRead;
      if (bytes > maximum) throw new SourceError("SMB_ACCESS_FAILED");
      digest.update(buffer.subarray(0, chunk.bytesRead));
      let written = 0;
      while (written < chunk.bytesRead) {
        const result = await writer.write(buffer, written, chunk.bytesRead - written, null);
        if (!result.bytesWritten) throw new SourceError("SMB_ACCESS_FAILED");
        written += result.bytesWritten;
      }
    }
    await writer.sync();
    const after = await reader.stat();
    if (bytes !== before.size || after.size !== before.size || after.mtimeMs !== before.mtimeMs || after.ctimeMs !== before.ctimeMs) {
      throw new SourceError("SMB_ACCESS_FAILED");
    }
    const sha256 = digest.digest("hex");
    if (verifyOutput) {
      const copiedDigest = createHash("sha256");
      let position = 0;
      while (true) {
        signal?.throwIfAborted();
        const chunk = await writer.read(buffer, 0, buffer.length, position);
        if (!chunk.bytesRead) break;
        position += chunk.bytesRead;
        if (position > maximum) throw new SourceError("SMB_ACCESS_FAILED");
        copiedDigest.update(buffer.subarray(0, chunk.bytesRead));
      }
      if (position !== bytes || copiedDigest.digest("hex") !== sha256) throw new SourceError("SMB_ACCESS_FAILED");
    }
    return { bytes, sha256, modifiedAt: before.mtime.toISOString() };
  } finally {
    await writer?.close();
    await reader.close();
  }
}

export function linuxSourceRunner(config: ClusterConfig,
  readMountInfo: () => Promise<string> = () => readFile("/proc/self/mountinfo", "utf8")): SourceRunner {
  return async (request, signal) => {
    signal?.throwIfAborted();
    try {
      const source = configuredSource(config, String(request.sourceId));
      const mount = validateLinuxMount(source, await readMountInfo());
      const root = await safePath(mount, source.root);
      if (!(await lstat(root)).isDirectory()) throw new SourceError("SMB_ACCESS_FAILED");
      const relative = sourcePath(String(request.path ?? ""));
      const operation = String(request.operation);
      const maximum = source.maxImportMb * 1024 * 1024;
      if (operation === "check-write" || operation === "export") {
        if (source.access !== "read-write") throw new SourceError("SOURCE_READ_ONLY");
        const parent = operation === "export" ? path.posix.dirname(relative) : ".";
        if (operation === "export" && !relative) throw new SourceError("SMB_ACCESS_FAILED");
        const directory = await safePath(root, parent === "." ? "" : parent);
        const temporary = path.join(directory, `.vanta-${operation}-${randomUUID()}`);
        const probe = path.join(directory, `.vanta-probe-${randomUUID()}`);
        let created = false;
        let probeCreated = false;
        try {
          let copied;
          if (operation === "check-write") {
            const handle = await open(temporary, "wx", 0o600);
            created = true;
            try { await handle.writeFile("vanta-write-probe"); await handle.sync(); } finally { await handle.close(); }
          } else {
            copied = await copyVerified(String(request.stagePath), temporary, maximum, signal, () => { created = true; }, true);
            if (copied.bytes !== request.bytes || copied.sha256 !== request.sha256) throw new SourceError("SMB_ACCESS_FAILED");
          }
          signal?.throwIfAborted();
          await safePath(root, parent === "." ? "" : parent);
          await link(temporary, operation === "check-write" ? probe : path.join(directory, path.posix.basename(relative)));
          probeCreated = operation === "check-write";
          return operation === "check-write" ? { writable: true } :
            { sourceId: source.id, path: relative, exported: true, bytes: copied!.bytes, sha256: copied!.sha256 };
        } finally {
          if (probeCreated) await unlink(probe);
          if (created) await unlink(temporary).catch((error: NodeJS.ErrnoException) => { if (error.code !== "ENOENT") throw error; });
        }
      }
      const target = await safePath(root, relative);
      const info = await lstat(target);
      if (operation === "stat") return metadata(relative, info);
      if (operation === "stage") return copyVerified(target, String(request.stagePath), maximum, signal);
      if (!["check", "browse", "search", "scan"].includes(operation) || !info.isDirectory()) throw new SourceError("SMB_ACCESS_FAILED");
      const scan = operation === "scan";
      const limit = Math.max(1, Math.min(scan ? 5000 : 200, Number(request.limit) || 50));
      const offset = Math.max(0, Math.min(5000, Number(request.offset) || 0));
      const queue = [{ relative, depth: 0 }];
      const entries: ReturnType<typeof metadata>[] = [];
      const deadline = Date.now() + 30_000;
      let scanned = 0, matched = 0, truncated = false;
      outer: while (queue.length) {
        const current = queue.shift()!;
        const directory = await opendir(await safePath(root, current.relative));
        for await (const entry of directory) {
          signal?.throwIfAborted();
          if (++scanned > 5000 || Date.now() > deadline) { truncated = true; break outer; }
          const entryPath = current.relative ? `${current.relative}/${entry.name}` : entry.name;
          try { sourcePath(entryPath); } catch { continue; }
          if (entry.isSymbolicLink()) continue;
          const entryInfo = await lstat(await safePath(root, entryPath));
          if (!entryInfo.isDirectory() && !entryInfo.isFile()) continue;
          if (operation === "check") return { readable: true };
          if (entryInfo.isDirectory() && (operation === "search" || (scan && request.recursive !== false))) {
            if (current.depth < (scan ? 32 : 8)) queue.push({ relative: entryPath, depth: current.depth + 1 });
            else truncated = true;
          }
          if (scan && entryInfo.isDirectory()) continue;
          if (operation === "search" && !entry.name.toLowerCase().includes(String(request.query ?? "").toLowerCase())) continue;
          if (matched++ < offset) continue;
          if (entries.length === limit) { truncated = true; break outer; }
          entries.push(metadata(entryPath, entryInfo));
        }
      }
      return { sourceId: source.id, path: relative, entries, truncated,
        nextOffset: truncated && entries.length === limit ? offset + entries.length : null };
    } catch (error) {
      if (signal?.aborted) throw error;
      if (error instanceof SourceError) throw error;
      throw new SourceError((error as NodeJS.ErrnoException).code === "EEXIST" ? "DESTINATION_EXISTS" : "SMB_ACCESS_FAILED");
    }
  };
}