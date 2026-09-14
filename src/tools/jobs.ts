import { z } from "zod";
import { resolveTargets } from "../config.js";
import { errorText, json } from "../format.js";
import { JOB_ID } from "../jobs/types.js";
import { targetsSchema, type ToolContext, type ToolServer } from "./context.js";

export function registerJobTools(server: ToolServer, ctx: ToolContext): void {
  server.registerTool(
    "cluster_list_jobs",
    {
      title: "List durable cluster jobs",
      description: "List durable background jobs and their progress. Job state remains on the target node across MCP disconnects and daemon restarts.",
      inputSchema: { targets: targetsSchema },
    },
    async ({ targets }) => {
      try {
        return json(await ctx.jobs.list(resolveTargets(ctx.config, targets)));
      } catch (error) {
        return errorText(error);
      }
    },
  );

  server.registerTool(
    "cluster_get_job",
    {
      title: "Get durable cluster job",
      description: "Refresh and return one durable job's state, phase, progress, and terminal result.",
      inputSchema: { jobId: z.string().regex(JOB_ID) },
    },
    async ({ jobId }) => {
      try {
        return json(await ctx.jobs.get(jobId));
      } catch (error) {
        return errorText(error);
      }
    },
  );

  server.registerTool(
    "cluster_get_job_log",
    {
      title: "Read durable cluster job log",
      description: "Return the bounded tail of one durable job's remote log.",
      inputSchema: {
        jobId: z.string().regex(JOB_ID),
        maxBytes: z.number().int().min(1).max(1_000_000).default(65_536),
      },
    },
    async ({ jobId, maxBytes }) => {
      try {
        return json(await ctx.jobs.log(jobId, maxBytes));
      } catch (error) {
        return errorText(error);
      }
    },
  );

  server.registerTool(
    "cluster_cancel_job",
    {
      title: "Cancel durable cluster job",
      description: "Stop one running durable job and record a canceled terminal state. Requires confirm=true.",
      inputSchema: {
        jobId: z.string().regex(JOB_ID),
        confirm: z.boolean().optional().describe("Must be true to authorize job cancellation."),
      },
    },
    async ({ jobId, confirm }) => {
      try {
        if (confirm !== true) throw new Error("Job cancellation requires confirm: true.");
        return json(await ctx.jobs.cancel(jobId));
      } catch (error) {
        return errorText(error);
      }
    },
  );
}