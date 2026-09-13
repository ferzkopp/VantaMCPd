#!/usr/bin/env node
// Standalone hardware discovery: probes the nodes and records CPU/memory/disk/OS in the inventory.
// Used by scripts/bootstrap.ps1 after access is established; the daemon does the same on startup.
// With --assign, prompts for any disk whose purpose could not be detected.
import { createInterface } from "node:readline/promises";
import { loadConfig, loadEnvFile, resolveTargets, saveNodeDiskRoles, type DiskInfo, type DiskRole } from "./config.js";
import { refreshHardware, type HardwareProbe } from "./hardware.js";
import { SshPool } from "./ssh.js";

const ROLE_CHOICES: DiskRole[] = ["system", "swap", "storage", "data", "unassigned"];

function describeDisk(disk: DiskInfo): string {
  const parts = (disk.partitions ?? [])
    .map(
      (p) =>
        `${p.name} ${p.sizeGb ?? "?"}GB ${p.fsType ?? "no-fs"}` +
        `${p.label ? ` "${p.label}"` : ""}${p.mountpoint ? ` -> ${p.mountpoint}` : ""}`,
    )
    .join("; ");
  return `${disk.name} ${disk.sizeGb ?? "?"}GB${disk.model ? ` (${disk.model})` : ""}${parts ? ` [${parts}]` : " [no partitions]"}`;
}

async function promptForRoles(configPath: string, probes: HardwareProbe[]): Promise<void> {
  const pending = probes.flatMap((p) =>
    (p.hardware?.disks ?? [])
      .filter((d) => d.roleSource !== "configured" && (d.role === "data" || d.role === "unassigned"))
      .map((d) => ({ node: p.node, disk: d })),
  );
  if (pending.length === 0) {
    console.log("    all disks classified automatically - nothing to assign");
    return;
  }

  console.log("");
  console.log("==> Some disks could not be classified automatically. Assign a purpose to each");
  console.log(`    (${ROLE_CHOICES.map((r, i) => `${i + 1}=${r}`).join("  ")}; Enter keeps the detected value):`);

  const rl = createInterface({ input: process.stdin, output: process.stdout });
  const updates = new Map<string, Record<string, DiskRole>>();
  try {
    for (const { node, disk } of pending) {
      const answer = (await rl.question(`    ${node}  ${describeDisk(disk)}\n      role [${disk.role}]: `)).trim();
      if (!answer) continue;
      const picked = /^[1-5]$/.test(answer) ? ROLE_CHOICES[Number(answer) - 1] : ROLE_CHOICES.find((r) => r === answer);
      if (!picked) {
        console.log(`      ignored: "${answer}" is not one of ${ROLE_CHOICES.join(", ")}`);
        continue;
      }
      updates.set(node, { ...updates.get(node), [disk.name]: picked });
    }
  } finally {
    rl.close();
  }

  const written = saveNodeDiskRoles(configPath, updates);
  console.log(written.length > 0 ? `    disk roles saved for ${written.join(", ")}` : "    no disk roles changed");
}

async function main(): Promise<void> {
  loadEnvFile(process.env.VANTA_ENV_FILE ?? ".env");

  const args = process.argv.slice(2);
  const assign = args.includes("--assign");
  const names = args.filter((a) => !a.startsWith("--"));

  const config = loadConfig();
  const nodes = resolveTargets(config, names.length > 0 ? names : undefined);
  const pool = new SshPool(config);

  try {
    const { probes, saved } = await refreshHardware(config, pool, nodes, { timeoutMs: 45_000 });
    for (const p of probes) {
      if (p.error) {
        console.log(`    !!   ${p.node} (${p.host}): ${p.error}`);
        continue;
      }
      const hw = p.hardware ?? {};
      const cpu = `${hw.cpu?.cores ?? "?"} x ${hw.cpu?.arch ?? "?"}`;
      const mem = `${hw.memory?.totalMb ?? "?"}MB RAM, ${hw.memory?.swapTotalMb ?? 0}MB swap`;
      console.log(`    OK   ${p.node}: ${cpu}, ${mem}, ${hw.os?.name ?? "unknown OS"}`);
      for (const d of hw.disks ?? []) {
        console.log(`           ${d.role ?? "?"}${d.roleSource === "configured" ? "*" : ""}: ${describeDisk(d)}`);
      }
      if (hw.memory?.swapPersistent === false) {
        console.log("           !! active swap is missing an /etc/fstab entry - it is lost on reboot");
      }
    }
    console.log(`    recorded ${saved.length} node(s) in ${config.configPath}`);

    if (assign) await promptForRoles(config.configPath, probes);
    process.exitCode = probes.some((p) => p.error) ? 1 : 0;
  } finally {
    pool.disposeAll();
  }
}

main().catch((err: unknown) => {
  console.error(`hardware discovery failed: ${err instanceof Error ? err.message : String(err)}`);
  process.exit(1);
});
