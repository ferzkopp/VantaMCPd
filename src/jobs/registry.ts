import { JOB_KIND } from "./types.js";

/** Allowlist for trusted internal job producers. MCP callers cannot register or submit job kinds. */
export class JobRegistry {
  private readonly kinds = new Set<string>();

  register(kind: string): void {
    if (!JOB_KIND.test(kind)) throw new Error(`Invalid job kind: ${kind}`);
    if (this.kinds.has(kind)) throw new Error(`Job kind already registered: ${kind}`);
    this.kinds.add(kind);
  }

  assertRegistered(kind: string): void {
    if (!this.kinds.has(kind)) throw new Error(`Unregistered job kind: ${kind}`);
  }
}