import { z } from "zod";
import { resolveTargets, type ResolvedNode } from "../config.js";
import { errorText, json, parseKeyValueLines, renderResults } from "../format.js";
import {
  buildGpuSetupScript,
  buildGpuTestScript,
  summarizeGpuInspection,
  GPU_INSPECT_SCRIPT,
  GPU_JOB_KIND,
  GPU_SETUP_SCRIPT_PATH,
  validateContainerImage,
} from "../gpu.js";
import { q } from "../security.js";
import { targetsSchema, timeoutSchema, type ToolContext, type ToolServer } from "./context.js";

const SETUP_TIMEOUT_MS = 90 * 60 * 1_000;
const SETUP_MIN_FREE_MEMORY_MB = 200;
const DEFAULT_TEST_IMAGE = "docker.io/library/debian:13";

function singleNode(ctx: ToolContext, targets: string[] | undefined, action: string): ResolvedNode {
  if (targets === undefined || targets.length === 0) {
    throw new Error(`action="${action}" changes one node and needs an explicit target, e.g. targets: ["cluster6"].`);
  }
  const nodes = resolveTargets(ctx.config, targets);
  if (nodes.length !== 1) {
    throw new Error(`action="${action}" acts on exactly one node; "${targets.join(", ")}" resolves to ${nodes.length}.`);
  }
  return nodes[0] as ResolvedNode;
}

export function registerGpuTools(server: ToolServer, ctx: ToolContext): void {
  server.registerTool(
    "cluster_gpu",
    {
      title: "Promote a node to CUDA workloads",
      description:
        "Turn an ordinary enrolled node into a CUDA node, and report GPU readiness. Enrollment is unchanged: prepare, bootstrap and discover the node like any other, then promote it here.\n" +
        '- check: read-only probe of GPU presence, distribution, Secure Boot, kernel headers, driver, container toolkit and CDI state; reports blockers and the next step. Also the way to see the state after promotion.\n' +
        '- enable: install the NVIDIA driver (open kernel modules by default), a container runtime and the NVIDIA Container Toolkit, then generate the CDI spec. Runs as one durable job per node, is safe to re-run, and requires explicit targets plus confirm:true. If the kernel module cannot load in place it reports that a reboot is owed - reboot with cluster_power and call enable again.\n' +
        '- test: end-to-end proof that a container can reach the GPU, plus the resident compute processes.\n' +
        "Debian 12 and 13 on amd64 or arm64 are supported. The CUDA toolkit itself is not installed on the node: GPU modules carry their own CUDA runtime in their container image.",
      inputSchema: {
        action: z.enum(["check", "enable", "test"]),
        targets: targetsSchema,
        driver: z
          .enum(["open", "proprietary"])
          .default("open")
          .describe('Kernel module flavour for enable. "open" (nvidia-open) suits Turing and newer; "proprietary" installs cuda-drivers.'),
        containerRuntime: z
          .enum(["podman", "docker", "none"])
          .default("podman")
          .describe('Runtime to wire up for GPU containers. "docker" must already be installed; "none" installs the driver only.'),
        image: z
          .string()
          .optional()
          .describe(`Container image for action="test". Default ${DEFAULT_TEST_IMAGE}; the toolkit injects nvidia-smi, so no CUDA image is needed.`),
        confirm: z.boolean().optional().describe('Required for action="enable": it installs kernel modules and changes apt sources.'),
        timeoutMs: timeoutSchema,
      },
    },
    async ({ action, targets, driver, containerRuntime, image, confirm, timeoutMs }) => {
      try {
        // Schema defaults apply to MCP callers; resolve again for direct in-process invocation.
        const flavour = driver ?? "open";
        const runtime = containerRuntime ?? "podman";
        switch (action) {
          case "check": {
            const nodes = resolveTargets(ctx.config, targets);
            const results = await ctx.pool.execMany(nodes, `${GPU_INSPECT_SCRIPT}`, {
              sudo: true,
              timeoutMs: timeoutMs ?? 60_000,
            });
            return json(
              results.map((result) => {
                if (result.error || !result.ok) {
                  return { node: result.node, host: result.host, error: result.error ?? result.stderr.trim() ?? `exit ${result.code}` };
                }
                return { node: result.node, host: result.host, ...summarizeGpuInspection(parseKeyValueLines(result.stdout)) };
              }),
            );
          }

          case "enable": {
            const node = singleNode(ctx, targets, action);
            if (confirm !== true) {
              throw new Error(
                `Refusing to promote ${node.name} without confirmation. This installs the NVIDIA driver and builds a kernel ` +
                  `module on that node, enables the apt "contrib" component and adds two NVIDIA repositories.\n` +
                  `Ask the user to approve, then call again with confirm: true.`,
              );
            }

            const probe = await ctx.pool.exec(node, GPU_INSPECT_SCRIPT, { sudo: true, timeoutMs: 60_000 });
            if (!probe.ok) throw new Error(`cannot inspect ${node.name}: ${probe.error ?? probe.stderr.trim()}`);
            const inspection = summarizeGpuInspection(parseKeyValueLines(probe.stdout));
            if (inspection.blockers.length > 0) {
              throw new Error(`${node.name} cannot run CUDA workloads: ${inspection.blockers.join("; ")}`);
            }

            const script = buildGpuSetupScript({ driver: flavour, containerRuntime: runtime });
            const staged = await ctx.pool.exec(
              node,
              `set -e; install -d -m 0755 /var/lib/vantamcpd; ` +
                `printf '%s' ${q(Buffer.from(script, "utf8").toString("base64"))} | base64 -d > ${q(GPU_SETUP_SCRIPT_PATH)}; ` +
                `chown root:root ${q(GPU_SETUP_SCRIPT_PATH)}; chmod 0700 ${q(GPU_SETUP_SCRIPT_PATH)}`,
              { sudo: true, timeoutMs: 30_000 },
            );
            if (!staged.ok) throw new Error(`cannot stage the promotion script: ${staged.error ?? staged.stderr.trim()}`);

            const job = await ctx.jobs.submit(node, {
              kind: GPU_JOB_KIND,
              // apt is shared with the package tool, so the two cannot run on one node at the same time.
              resourceKeys: [`gpu:${node.name}`, `apt:${node.name}`],
              command: ["/bin/bash", GPU_SETUP_SCRIPT_PATH],
              cwd: "/",
              timeoutMs: timeoutMs ?? SETUP_TIMEOUT_MS,
              minFreeMemoryMb: SETUP_MIN_FREE_MEMORY_MB,
            });
            return json({
              node: node.name,
              action: "enable",
              driver: flavour,
              containerRuntime: runtime,
              jobId: job.jobId,
              status: job.status,
              stateBefore: inspection.state,
              warnings: inspection.warnings,
              next:
                "Follow the job with cluster_get_job and cluster_get_job_log. The hardware inventory is refreshed " +
                'automatically when it succeeds. If the log ends with INCOMPLETE, reboot the node and run enable again, ' +
                'then prove it with action="test".',
            });
          }

          case "test": {
            const node = singleNode(ctx, targets, action);
            if (runtime === "none") throw new Error('action="test" needs containerRuntime "podman" or "docker".');
            const script = buildGpuTestScript(runtime, image ? validateContainerImage(image) : DEFAULT_TEST_IMAGE);
            const result = await ctx.pool.exec(node, `${script} 2>&1`, { sudo: true, timeoutMs: timeoutMs ?? 900_000 });
            return { content: [{ type: "text" as const, text: renderResults(`GPU smoke test on ${node.name}`, [result]) }] };
          }
        }
      } catch (err) {
        return errorText(err);
      }
    },
  );
}
