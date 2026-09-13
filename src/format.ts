import type { ExecResult } from "./ssh.js";

export interface ToolText {
  // The SDK's CallToolResult carries an index signature; without it these results are not assignable.
  [x: string]: unknown;
  content: { type: "text"; text: string }[];
  isError?: boolean;
}

export function text(body: string, isError = false): ToolText {
  return { content: [{ type: "text", text: body }], ...(isError ? { isError: true } : {}) };
}

export function json(value: unknown, isError = false): ToolText {
  return text(JSON.stringify(value, null, 2), isError);
}

export function errorText(err: unknown): ToolText {
  const message = err instanceof Error ? err.message : String(err);
  return text(`ERROR: ${message}`, true);
}

function trimBlock(s: string): string {
  return s.replace(/\s+$/, "");
}

/** Render per-node command output as compact, agent-friendly markdown. */
export function renderResults(title: string, results: ExecResult[]): string {
  const failed = results.filter((r) => !r.ok);
  const header = `# ${title}\n${results.length} node(s) - ${results.length - failed.length} ok, ${failed.length} failed\n`;

  const blocks = results.map((r) => {
    const status = r.error ? `ERROR (${r.error})` : r.ok ? "ok" : `exit ${r.code}${r.signal ? ` signal ${r.signal}` : ""}`;
    const parts = [`## ${r.node} (${r.host}) - ${status} - ${r.durationMs}ms`];
    const stdout = trimBlock(r.stdout);
    const stderr = trimBlock(r.stderr);
    if (stdout) parts.push("```\n" + stdout + "\n```");
    if (stderr) parts.push("stderr:\n```\n" + stderr + "\n```");
    if (!stdout && !stderr && !r.error) parts.push("_(no output)_");
    if (r.truncated) parts.push("_output truncated_");
    return parts.join("\n");
  });

  return [header, ...blocks].join("\n");
}

/** Parse `key|value` lines emitted by the status/inspect scripts. */
export function parseKeyValueLines(stdout: string): Record<string, string | string[]> {
  const result: Record<string, string | string[]> = {};
  for (const line of stdout.split(/\r?\n/)) {
    const sep = line.indexOf("|");
    if (sep <= 0) continue;
    const key = line.slice(0, sep).trim();
    const value = line.slice(sep + 1).trim();
    const existing = result[key];
    if (existing === undefined) result[key] = value;
    else if (Array.isArray(existing)) existing.push(value);
    else result[key] = [existing, value];
  }
  return result;
}
