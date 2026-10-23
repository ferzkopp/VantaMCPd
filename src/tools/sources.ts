import path from "node:path";
import { z } from "zod";
import { uploadLocalArtifact } from "../artifact-upload.js";
import { downloadArtifact } from "../artifact-download.js";
import { errorText, json } from "../format.js";
import { SourceClient } from "../sources.js";
import type { ToolContext, ToolServer } from "./context.js";

export function registerSourceTools(server: ToolServer, ctx: ToolContext, client = new SourceClient(ctx.config)) {
  const sourceId = z.string().regex(/^[a-z0-9][a-z0-9-]{0,63}$/).describe("Configured source ID from cluster_list_sources; not a node name or share path.");
  const sourcePath = z.string().max(1024).describe("Slash-separated path relative to the configured source root; never a UNC, absolute or local path. No traversal or wildcards.");
  const relativePath = sourcePath.default("").describe("Directory relative to the configured source root; omit or use an empty string for the root. No absolute paths, traversal or wildcards.");
  const filePath = sourcePath.min(1).describe("Nonempty file path relative to the configured source root, including its filename; not a UNC or local path.");
  const pagination = { limit: z.number().int().min(1).max(200).default(50), offset: z.number().int().min(0).max(5000).default(0) };
  let removeExport: (() => void) | undefined;
  const syncExport = () => {
    const available = ctx.config.artifacts?.enabled && (ctx.config.sources ?? []).some((source) => client.canExport(source.id));
    if (!available) { removeExport?.(); removeExport = undefined; return; }
    if (removeExport) return;
    const registered = server.registerTool("cluster_export_source", {
      title: "Publish an artifact to a writable SMB source",
      description: "Export a verified artifact to an approved writable SMB source on a Windows or Linux MCP host. Available only with artifacts enabled and a successful write probe; refresh with cluster_check_source(writeProbe=true). Rechecks write access and checksums, then publishes atomically without replacement (Windows rename; Linux hard link). Parent directory must exist. Foreground and cancellable; never overwrites files. After cancellation around publication, inspect the destination before retrying. Each file is bounded by the source's maxImportMb.",
      annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: false, openWorldHint: true },
      inputSchema: { sourceId, artifactId: z.string().regex(/^[0-9a-f]{32}$/).describe("Immutable output artifact ID returned by an import or compute module."), path: filePath },
    }, async ({ sourceId: id, artifactId, path: relative }, extra) => {
      try {
        if (!ctx.config.artifacts?.enabled || !client.canExport(id)) throw new Error("Export is unavailable for this source; run cluster_check_source with writeProbe first.");
        return json(await client.exportFile(id, relative, (file, maximum) => downloadArtifact(ctx, artifactId, file, maximum, extra.signal), extra.signal));
      } catch (error) { return errorText(error); } finally { syncExport(); }
    });
    removeExport = () => registered.remove();
  };
  server.registerTool("cluster_list_sources", {
    title: "List configured file sources",
    description: "List approved host-side SMB sources without credentials. Export is advertised per source only after a successful write probe. Use cluster_check_source to refresh access.",
    annotations: { readOnlyHint: true, openWorldHint: false },
    inputSchema: {},
  }, async () => json({ sources: (ctx.config.sources ?? []).map(({ id, type, server: host, share, root, enabled, access, maxImportMb }) =>
    ({ id, type, server: host, share, root, enabled, access, maxImportMb, writable: client.canExport(id),
      operations: [...(enabled ? ["browse", "search", "stat"] : []), ...(enabled && ctx.config.artifacts?.enabled ? ["import"] : []),
        ...(client.canExport(id) && ctx.config.artifacts?.enabled ? ["export"] : [])] })) }));

  server.registerTool("cluster_check_source", {
    title: "Check SMB source access",
    description: "Check SMB access from the MCP host: Windows checks TCP 445, host-managed credentials and encryption; Linux validates the administrator-managed encrypted CIFS mount. Checks directory access and returns sanitized troubleshooting guidance. Read-only by default; writeProbe=true creates and removes a tiny unique file and tests atomic no-overwrite publication to enable export on a configured read-write source. Never mounts Linux shares, exposes credentials or changes firewall rules.",
    annotations: { readOnlyHint: false, destructiveHint: false, openWorldHint: true },
    inputSchema: { sourceId, writeProbe: z.boolean().default(false).describe("Create/write/publish/delete a tiny unique probe file to refresh export eligibility on a read-write source. False performs only read checks.") },
  }, async (args, extra) => {
    try {
      const read = await client.check(args.sourceId, extra.signal);
      const write = args.writeProbe ? await client.checkWritable(args.sourceId, extra.signal) : undefined;
      syncExport();
      return json({ ...read, writable: client.canExport(args.sourceId), ...(write ? { writeCheck: write } : {}) });
    } catch (error) { return errorText(error); }
  });

  server.registerTool("cluster_browse_source", {
    title: "Browse an SMB source directory",
    description: "List files and directories in an approved source, including subfolders. Bounded pages scan at most 5000 entries; offsets are best-effort if the share changes. Reparse points are excluded.",
    annotations: { readOnlyHint: true, openWorldHint: true },
    inputSchema: { sourceId, path: relativePath, ...pagination },
  }, async ({ sourceId: id, ...args }, extra) => {
    try { return json(await client.request(id, "browse", args, extra.signal)); } catch (error) { return errorText(error); }
  });

  server.registerTool("cluster_search_source", {
    title: "Search SMB source filenames",
    description: "Search filenames case-insensitively beneath an approved source directory, including subfolders. Bounded to 5000 scanned entries, 8 nested levels and 30 seconds of enumeration; reports truncation. Does not read file contents.",
    annotations: { readOnlyHint: true, openWorldHint: true },
    inputSchema: { sourceId, path: relativePath, query: z.string().min(1).max(128).describe("Case-insensitive filename substring, such as .png; not a glob or file-content query."), ...pagination },
  }, async ({ sourceId: id, ...args }, extra) => {
    try { return json(await client.request(id, "search", args, extra.signal)); } catch (error) { return errorText(error); }
  });

  server.registerTool("cluster_stat_source", {
    title: "Inspect SMB source file metadata",
    description: "Return file or directory metadata under an approved source root without reading content or exposing credentials.",
    annotations: { readOnlyHint: true, openWorldHint: true },
    inputSchema: { sourceId, path: sourcePath.default("") },
  }, async ({ sourceId: id, ...args }, extra) => {
    try { return json(await client.request(id, "stat", args, extra.signal)); } catch (error) { return errorText(error); }
  });

  server.registerTool("cluster_import_source", {
    title: "Import an SMB file as a shared artifact",
    description: "Import one approved SMB file through private MCP-host staging and the verified artifact protocol. Requires artifacts enabled; each file is bounded by the source's maxImportMb. Returns artifact.artifactId for artifact-aware compute modules and source provenance. Creates an immutable snapshot; never modifies the share. Foreground and cancellable, not a durable job. Windows helper timeout is 120 seconds; Linux blocked CIFS calls follow administrator mount timeouts.",
    annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: false, openWorldHint: true },
    inputSchema: { sourceId, path: filePath, name: z.string().min(1).max(128).regex(/^[^/\\\u0000-\u001f\u007f]+$/).optional().describe("Artifact display filename; defaults to the source basename. Does not rename the source."),
      retentionDays: z.number().int().positive().optional() },
  }, async ({ sourceId: id, path: relative, name, retentionDays }, extra) => {
    try {
      if (!ctx.config.artifacts?.enabled) throw new Error("Enable artifact storage before importing source files.");
      return json(await client.importFile(id, relative, (file, sha256) => uploadLocalArtifact(ctx, {
        localPath: file, name: name ?? path.posix.basename(relative), producer: id, retentionDays, sha256,
      }, extra.signal), extra.signal));
    } catch (error) { return errorText(error); }
  });
  server.registerTool("cluster_import_sources", {
    title: "Import files matching source globs",
    description: "Import multiple approved SMB files as immutable artifacts using case-insensitive source-relative globs. Requires artifacts enabled. Scan bounded to 5000 entries/32 nested levels/30 seconds; rejects incomplete scans and exceeded batch limits before copying. Each file also respects the source's maxImportMb. Foreground, sequential and cancellable; does not modify source files. Inspect complete and each results[].ok; successful IDs are results[].result.artifact.artifactId. Failures do not roll back successful artifacts; retry only failed paths to avoid duplicate uploads.",
    annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: false, openWorldHint: true },
    inputSchema: { sourceId, patterns: z.array(z.string().min(1).max(1024)).min(1).max(16).describe("Source-relative globs: Screenshot*.png matches only the root; **/*.jpg includes subfolders. Supports *, ?, ** and character classes, not braces, extglobs or negation. Hidden names excluded; overlapping matches deduplicated."),
      maxFiles: z.number().int().min(1).max(128).default(32), maxTotalMb: z.number().int().min(1).max(1024).default(256),
      retentionDays: z.number().int().positive().optional() },
  }, async ({ sourceId: id, patterns, maxFiles, maxTotalMb, retentionDays }, extra) => {
    try {
      if (!ctx.config.artifacts?.enabled) throw new Error("Enable artifact storage before importing source files.");
      return json(await client.importFiles(id, patterns, (file, relative, sha256) => uploadLocalArtifact(ctx, {
        localPath: file, name: path.posix.basename(relative), producer: id, retentionDays, sha256,
      }, extra.signal), { maxFiles, maxTotalMb }, extra.signal));
    } catch (error) { return errorText(error); }
  });
  return { refresh: async () => {
    for (const source of ctx.config.sources ?? []) {
      if (source.enabled && source.access === "read-write") await client.checkWritable(source.id);
    }
    syncExport();
  } };
}