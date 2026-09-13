import { z } from "zod";

export const JOB_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
export const JOB_KIND = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
export const JOB_RESOURCE = /^[A-Za-z0-9]+(?::[A-Za-z0-9][A-Za-z0-9._-]*)+$/;

export const JobStatusSchema = z.enum(["queued", "running", "succeeded", "failed", "canceled"]);

const JobProgressSchema = z
  .object({
    current: z.number().nonnegative().optional(),
    total: z.number().positive().optional(),
    unit: z.string().min(1).max(32).optional(),
    message: z.string().max(500).optional(),
  })
  .strict();

const JobResultSchema = z
  .object({
    summary: z.string().max(2_000).optional(),
    exitCode: z.number().int().optional(),
  })
  .strict();

export const JobStateSchema = z
  .object({
    schemaVersion: z.literal(1),
    jobId: z.string().regex(JOB_ID),
    kind: z.string().regex(JOB_KIND).max(100),
    status: JobStatusSchema,
    targetNode: z.string().min(1).max(100),
    moduleId: z.string().regex(JOB_KIND).max(100).optional(),
    resourceKeys: z.array(z.string().regex(JOB_RESOURCE).max(300)).max(16),
    phase: z.string().min(1).max(100).optional(),
    progress: JobProgressSchema.optional(),
    createdAt: z.string().datetime(),
    startedAt: z.string().datetime().optional(),
    heartbeatAt: z.string().datetime().optional(),
    finishedAt: z.string().datetime().optional(),
    expiresAt: z.string().datetime().optional(),
    result: JobResultSchema.optional(),
    error: z.string().max(4_000).optional(),
    logTruncated: z.boolean().optional(),
  })
  .strict();

export const TrustedJobSpecSchema = z
  .object({
    schemaVersion: z.literal(1),
    jobId: z.string().regex(JOB_ID),
    kind: z.string().regex(JOB_KIND).max(100),
    targetNode: z.string().min(1).max(100),
    moduleId: z.string().regex(JOB_KIND).max(100).optional(),
    resourceKeys: z.array(z.string().regex(JOB_RESOURCE).max(300)).min(1).max(16),
    command: z.array(z.string().min(1).max(1_000)).min(1).max(64),
    cwd: z.string().min(1).max(1_000),
    environment: z.record(z.string().max(4_000)).default({}),
    timeoutMs: z.number().int().min(1_000).max(7 * 24 * 60 * 60 * 1_000),
    retentionMs: z.number().int().min(60_000).max(365 * 24 * 60 * 60 * 1_000),
    maxLogBytes: z.number().int().min(1_024).max(100_000_000),
    createdAt: z.string().datetime(),
  })
  .strict();

export type JobStatus = z.infer<typeof JobStatusSchema>;
export type JobState = z.infer<typeof JobStateSchema>;
export type TrustedJobSpec = z.infer<typeof TrustedJobSpecSchema>;

export function isTerminalJobStatus(status: JobStatus): boolean {
  return status === "succeeded" || status === "failed" || status === "canceled";
}

export function parseJobState(value: unknown, source = "job state"): JobState {
  const result = JobStateSchema.safeParse(value);
  if (result.success) return result.data;
  const issues = result.error.issues.map((issue) => `${issue.path.join(".") || "(root)"}: ${issue.message}`).join("; ");
  throw new Error(`Invalid ${source}: ${issues}`);
}