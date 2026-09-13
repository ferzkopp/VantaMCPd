import { createHash } from "node:crypto";
import { existsSync, statSync } from "node:fs";
import path from "node:path";
import { z } from "zod";
import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { resolveTargets, type ResolvedNode } from "../config.js";
import { errorText, json, renderResults, text } from "../format.js";
import { q, validateAbsPath } from "../security.js";
import { mapLimit } from "../ssh.js";
import { targetsSchema, timeoutSchema, type ToolContext } from "./context.js";

const MAX_READ_BYTES = 1_000_000;

function localPath(p: string): string {
  if (p.includes("\u0000")) throw new Error("Local path contains a NUL byte.");
  return path.resolve(p);
}

export function registerFileTools(server: McpServer, ctx: ToolContext): void {
  server.registerTool(
    "cluster_list_dir",
    {
      title: "List a remote directory",
      description: "List the contents of a directory on the target nodes, with sizes, permissions and modification times.",
      inputSchema: {
        path: z.string().describe("Absolute directory path on the node."),
        targets: targetsSchema,
        all: z.boolean().optional().describe("Include dotfiles. Default true."),
        sudo: z.boolean().optional().describe("List with root privileges. Default false."),
        recursiveDepth: z.number().int().min(0).max(4).optional().describe("If set, use find to this depth instead of ls."),
      },
    },
    async ({ path: dir, targets, all, sudo, recursiveDepth }) => {
      try {
        const nodes = resolveTargets(ctx.config, targets);
        validateAbsPath(dir, "directory");
        const command =
          recursiveDepth === undefined
            ? `ls -l${all === false ? "" : "A"}h --time-style=long-iso -- ${q(dir)}`
            : `find ${q(dir)} -maxdepth ${recursiveDepth} -printf '%M %8s %TY-%Tm-%Td %TH:%TM %p\\n' | head -n 500`;
        const results = await ctx.pool.execMany(nodes, `${command} 2>&1`, { sudo: sudo === true, timeoutMs: 30_000 });
        return text(renderResults(`ls ${dir}`, results));
      } catch (err) {
        return errorText(err);
      }
    },
  );

  server.registerTool(
    "cluster_read_file",
    {
      title: "Read a remote file",
      description:
        "Read the contents of a text file from a single node (config files, scripts, etc). Binary-safe transport, size-capped at 1MB.",
      inputSchema: {
        node: z.string().describe("Single node name or host."),
        path: z.string().describe("Absolute file path on the node."),
        sudo: z.boolean().optional().describe("Read with root privileges (needed for /etc/shadow-like files). Default false."),
        maxBytes: z.number().int().min(1).max(MAX_READ_BYTES).optional().describe(`Maximum bytes to read. Default ${MAX_READ_BYTES}.`),
      },
    },
    async ({ node, path: file, sudo, maxBytes }) => {
      try {
        const [target] = resolveTargets(ctx.config, [node]);
        if (!target) throw new Error(`Unknown node: ${node}`);
        validateAbsPath(file, "file");
        const cap = maxBytes ?? MAX_READ_BYTES;
        const result = await ctx.pool.exec(
          target,
          `if [ ! -f ${q(file)} ]; then echo "NOTFOUND" >&2; exit 2; fi; head -c ${cap} -- ${q(file)} | base64 -w0`,
          { sudo: sudo === true, timeoutMs: 60_000, maxOutputBytes: Math.ceil(cap * 1.4) + 1024 },
        );
        if (!result.ok) {
          return text(`Failed to read ${file} on ${target.name}: ${result.error ?? (result.stderr.trim() || `exit ${result.code}`)}`, true);
        }
        const buffer = Buffer.from(result.stdout.trim(), "base64");
        return text(`# ${target.name}:${file} (${buffer.length} bytes)\n\n\`\`\`\n${buffer.toString("utf8")}\n\`\`\``);
      } catch (err) {
        return errorText(err);
      }
    },
  );

  server.registerTool(
    "cluster_write_file",
    {
      title: "Write a remote file",
      description:
        "Write text content to a file on one or more nodes, optionally as root and optionally keeping a timestamped backup of the previous version. " +
        "Content is streamed base64-encoded over stdin, so any characters are safe.",
      inputSchema: {
        path: z.string().describe("Absolute destination path on the node."),
        content: z.string().describe("Full file content to write."),
        targets: targetsSchema,
        sudo: z.boolean().optional().describe("Write as root. Default false."),
        mode: z
          .string()
          .regex(/^[0-7]{3,4}$/)
          .optional()
          .describe('Octal file mode, e.g. "644" or "0600".'),
        owner: z
          .string()
          .regex(/^[a-z_][a-z0-9_-]*(:[a-z_][a-z0-9_-]*)?$/i)
          .optional()
          .describe('user or user:group to chown to (requires sudo).'),
        backup: z.boolean().optional().describe("Back up an existing file to <path>.bak-<timestamp>. Default true."),
        createDirs: z.boolean().optional().describe("Create parent directories if missing. Default true."),
      },
    },
    async ({ path: file, content, targets, sudo, mode, owner, backup, createDirs }) => {
      try {
        const nodes = resolveTargets(ctx.config, targets);
        validateAbsPath(file, "file");
        const payload = Buffer.from(content, "utf8").toString("base64");
        const sha = createHash("sha256").update(content, "utf8").digest("hex");

        const script = [
          `set -e`,
          `tmp=$(mktemp /tmp/.vanta-write-XXXXXX)`,
          `trap 'rm -f "$tmp"' EXIT`,
          createDirs === false ? "" : `mkdir -p -- "$(dirname ${q(file)})"`,
          `printf '%s' ${q(payload)} | base64 -d > "$tmp"`,
          backup === false ? "" : `if [ -f ${q(file)} ]; then cp -a -- ${q(file)} ${q(file)}.bak-$(date +%Y%m%d%H%M%S); fi`,
          `cat "$tmp" > ${q(file)}`,
          `rm -f "$tmp"`,
          mode ? `chmod ${mode} -- ${q(file)}` : "",
          owner ? `chown ${q(owner)} -- ${q(file)}` : "",
          `echo "wrote $(stat -c '%s bytes, mode %a, owner %U:%G' ${q(file)})"`,
          `echo "sha256 $(sha256sum ${q(file)} | cut -d' ' -f1)"`,
        ]
          .filter(Boolean)
          .join("\n");

        const results = await ctx.pool.execMany(nodes, `${script} 2>&1`, { sudo: sudo === true, timeoutMs: 60_000 });
        return text(`Expected sha256: ${sha}\n\n` + renderResults(`write ${file}`, results));
      } catch (err) {
        return errorText(err);
      }
    },
  );

  server.registerTool(
    "cluster_upload",
    {
      title: "Upload a local file to nodes",
      description:
        "Copy a file from this machine to one or more nodes over SFTP. The remote path must be writable by the SSH user; " +
        "to place a file in a root-owned location, upload to /tmp and then move it with cluster_run (sudo=true).",
      inputSchema: {
        localPath: z.string().describe("Path to the local file on this machine."),
        remotePath: z.string().describe("Absolute destination path on the node."),
        targets: targetsSchema,
        mode: z
          .string()
          .regex(/^[0-7]{3,4}$/)
          .optional()
          .describe('Octal mode to apply after upload, e.g. "755".'),
        timeoutMs: timeoutSchema,
      },
    },
    async ({ localPath: local, remotePath, targets, mode }) => {
      try {
        const nodes = resolveTargets(ctx.config, targets);
        const resolved = localPath(local);
        if (!existsSync(resolved) || !statSync(resolved).isFile()) throw new Error(`Local file not found: ${resolved}`);
        validateAbsPath(remotePath, "remotePath");
        const size = statSync(resolved).size;

        const outcomes = await mapLimit(nodes, ctx.config.maxConcurrency, async (node: ResolvedNode) => {
          let sftp: Awaited<ReturnType<typeof ctx.pool.sftp>> | undefined;
          try {
            sftp = await ctx.pool.sftp(node);
            const handle = sftp;
            if (!handle) throw new Error("SFTP session did not open.");
            await new Promise<void>((resolve, reject) => {
              handle.fastPut(resolved, remotePath, (err) => (err ? reject(err) : resolve()));
            });
            if (mode) {
              await new Promise<void>((resolve, reject) => {
                handle.chmod(remotePath, parseInt(mode, 8), (err) => (err ? reject(err) : resolve()));
              });
            }
            return { node: node.name, ok: true, bytes: size, remotePath };
          } catch (err) {
            return { node: node.name, ok: false, error: (err as Error).message };
          } finally {
            try {
              sftp?.end();
            } catch {
              /* ignore */
            }
          }
        });
        return json({ localPath: resolved, remotePath, results: outcomes });
      } catch (err) {
        return errorText(err);
      }
    },
  );

  server.registerTool(
    "cluster_download",
    {
      title: "Download a file from a node",
      description: "Copy a file from one node to this machine over SFTP.",
      inputSchema: {
        node: z.string().describe("Single node name or host."),
        remotePath: z.string().describe("Absolute source path on the node."),
        localPath: z.string().describe("Destination path on this machine."),
      },
    },
    async ({ node, remotePath, localPath: local }) => {
      try {
        const [target] = resolveTargets(ctx.config, [node]);
        if (!target) throw new Error(`Unknown node: ${node}`);
        validateAbsPath(remotePath, "remotePath");
        const dest = localPath(local);
        const sftp = await ctx.pool.sftp(target);
        try {
          await new Promise<void>((resolve, reject) => {
            sftp.fastGet(remotePath, dest, (err) => (err ? reject(err) : resolve()));
          });
          const size = statSync(dest).size;
          return text(`Downloaded ${target.name}:${remotePath} -> ${dest} (${size} bytes)`);
        } finally {
          try {
            sftp.end();
          } catch {
            /* ignore */
          }
        }
      } catch (err) {
        return errorText(err);
      }
    },
  );
}
