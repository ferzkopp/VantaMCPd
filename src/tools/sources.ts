import path from "node:path";
import { z } from "zod";
import { uploadLocalArtifact } from "../artifact-upload.js";
import { downloadArtifact } from "../artifact-download.js";
import { errorText, json } from "../format.js";
import { SourceClient } from "../sources.js";
import type { ToolContext, ToolServer } from "./context.js";

export function registerSourceTools(server: ToolServer, ctx: ToolContext, client = new SourceClient(ctx.config)) {
  const sourceId = z.string().regex(/^[a-z0-9][a-z0-9-]{0,63}$/);
  const relativePath = z.string().max(1024).default("").describe("Slash-separated path relative to the configured source root; never a UNC or local path.");
  const pagination = { limit: z.number().int().min(1).max(200).default(50), offset: z.number().int().min(0).max(5000).default(0) };
  let removeExport: (() => void) | undefined;
  const syncExport = () => {
    const available = ctx.config.artifacts?.enabled && (ctx.config.sources ?? []).some((source) => client.canExport(source.id));
    if (!available) { removeExport?.(); removeExport = undefined; return; }
    if (removeExport) return;
    const registered = server.registerTool("cluster_export_source", {
      title: "Publish an artifact to a writable SMB source",
      description: "Export a verified artifact to an approved writable SMB share. Rechecks write access, verifies checksums and publishes with a no-overwrite rename. Parent directory must exist. Foreground; never replaces existing files.",
      inputSchema: { sourceId, artifactId: z.string().regex(/^[0-9a-f]{32}$/), path: z.string().min(1).max(1024) },
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
    inputSchema: {},
  }, async () => json({ sources: (ctx.config.sources ?? []).map(({ id, type, server: host, share, root, enabled, access, maxImportMb }) =>
    ({ id, type, server: host, share, root, enabled, access, maxImportMb, writable: client.canExport(id),
      operations: [...(enabled ? ["browse", "search", "stat", "import"] : []), ...(client.canExport(id) && ctx.config.artifacts?.enabled ? ["export"] : [])] })) }));

  server.registerTool("cluster_check_source", {
    title: "Check SMB source access",
    description: "Check host-to-share TCP 445, stored credentials, SMB encryption and directory listing. Returns sanitized firewall and authentication troubleshooting guidance; never changes firewall rules.",
    inputSchema: { sourceId, writeProbe: z.boolean().default(false).describe("Create/write/rename/delete a tiny unique probe file to enable export on a read-write source.") },
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
    inputSchema: { sourceId, path: relativePath, ...pagination },
  }, async ({ sourceId: id, ...args }, extra) => {
    try { return json(await client.request(id, "browse", args, extra.signal)); } catch (error) { return errorText(error); }
  });

  server.registerTool("cluster_search_source", {
    title: "Search SMB source filenames",
    description: "Search filenames case-insensitively beneath an approved source directory, including subfolders. Bounded to 5000 scanned entries, 8 nested levels and 30 seconds of enumeration; reports truncation. Does not read file contents.",
    inputSchema: { sourceId, path: relativePath, query: z.string().min(1).max(128), ...pagination },
  }, async ({ sourceId: id, ...args }, extra) => {
    try { return json(await client.request(id, "search", args, extra.signal)); } catch (error) { return errorText(error); }
  });

  server.registerTool("cluster_stat_source", {
    title: "Inspect SMB source file metadata",
    description: "Return file or directory metadata under an approved source root without reading content or exposing credentials.",
    inputSchema: { sourceId, path: relativePath },
  }, async ({ sourceId: id, ...args }, extra) => {
    try { return json(await client.request(id, "stat", args, extra.signal)); } catch (error) { return errorText(error); }
  });

  server.registerTool("cluster_import_source", {
    title: "Import an SMB file as a shared artifact",
    description: "Copy one approved source file into private bounded host staging and upload through the verified artifact protocol. Returns an immutable artifact ID for existing compute modules. Foreground and cancellable; the SMB copy has a 120-second timeout. Does not modify the share.",
    inputSchema: { sourceId, path: relativePath, name: z.string().min(1).max(128).regex(/^[^/\\\u0000-\u001f\u007f]+$/).optional(),
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
    description: "Import multiple approved SMB files using case-insensitive source-relative globs (*, ?, ** and character classes). Scan bounded to 5000 entries/32 nested levels. Rejects incomplete scans and exceeded batch limits before copying. Returns individual successes/failures; no rollback of successful artifacts.",
    inputSchema: { sourceId, patterns: z.array(z.string().min(1).max(1024)).min(1).max(16),
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