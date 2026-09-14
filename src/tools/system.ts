import { z } from "zod";
import { resolveTargets } from "../config.js";
import { errorText, json, parseKeyValueLines, renderResults } from "../format.js";
import { refreshHardware } from "../hardware.js";
import { targetsSchema, timeoutSchema, type ToolContext, type ToolServer } from "./context.js";

const STATUS_SCRIPT = String.raw`
echo "hostname|$(hostname)"
echo "time|$(date -Is 2>/dev/null)"
echo "kernel|$(uname -r)"
echo "arch|$(uname -m)"
# Brace-form shell expansions are banned here: they would be read as template-literal interpolation.
if [ -r /etc/os-release ]; then . /etc/os-release; [ -n "$PRETTY_NAME" ] || PRETTY_NAME=unknown; echo "os|$PRETTY_NAME"; fi
[ -r /etc/armbian-release ] && echo "armbian|$(awk -F= '/^VERSION=/{print $2}' /etc/armbian-release 2>/dev/null)"
echo "uptime|$(uptime -p 2>/dev/null)"
echo "boot_time|$(uptime -s 2>/dev/null)"
echo "load|$(cut -d' ' -f1-3 /proc/loadavg)"
echo "cpu_cores|$(nproc)"
echo "cpu_model|$(awk -F': ' '/^model name|^Hardware/{print $2; exit}' /proc/cpuinfo)"
echo "ip|$(hostname -I 2>/dev/null | tr -s ' ')"
awk '/^MemTotal:/{printf "mem_total_mb|%d\n",$2/1024}
     /^MemAvailable:/{printf "mem_available_mb|%d\n",$2/1024}
     /^SwapTotal:/{printf "swap_total_mb|%d\n",$2/1024}
     /^SwapFree:/{printf "swap_free_mb|%d\n",$2/1024}' /proc/meminfo
for z in /sys/class/thermal/thermal_zone*/temp; do
  [ -r "$z" ] || continue
  t=$(cat "$z" 2>/dev/null) || continue
  case "$t" in ''|*[!0-9]*) continue;; esac
  [ "$t" -gt 1000 ] && t=$((t/1000))
  echo "temp_c|$(basename "$(dirname "$z")")=$t"
done
f=/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq
[ -r "$f" ] && echo "cpu_mhz|$(( $(cat "$f") / 1000 ))"
g=/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor
[ -r "$g" ] && echo "cpu_governor|$(cat "$g")"
df -PT -x tmpfs -x devtmpfs -x squashfs -x overlay 2>/dev/null |
  awk 'NR>1{printf "disk|%s type=%s size=%dM used=%dM avail=%dM use=%s\n",$7,$2,$3/1024,$4/1024,$5/1024,$6}'
echo "failed_unit_count|$(systemctl --failed --no-legend --plain 2>/dev/null | wc -l)"
systemctl --failed --no-legend --plain 2>/dev/null | awk '{print "failed_unit|"$1}'
echo "reboot_required|$([ -f /var/run/reboot-required ] && echo yes || echo no)"
echo "upgradable_count|$(apt-get -s -o Debug::NoLocking=1 upgrade 2>/dev/null | grep -c '^Inst ')"
echo "apt_cache_age|$(stat -c %y /var/cache/apt/pkgcache.bin 2>/dev/null || echo unknown)"
echo "sessions|$(who 2>/dev/null | wc -l)"
ps -eo pcpu,pmem,comm --sort=-pcpu 2>/dev/null | awk 'NR>1 && NR<=4 {printf "top_cpu|%s cpu=%s%% mem=%s%%\n",$3,$1,$2}'
exit 0
`;

const PING_SCRIPT = String.raw`
echo "user|$(id -un)"
echo "groups|$(id -Gn)"
if sudo -n true 2>/dev/null; then echo "sudo|nopasswd"; else echo "sudo|password-required-or-denied"; fi
echo "hostname|$(hostname)"
echo "kernel|$(uname -r) $(uname -m)"
exit 0
`;

export function registerSystemTools(server: ToolServer, ctx: ToolContext): void {
  server.registerTool(
    "cluster_list_nodes",
    {
      title: "List cluster nodes",
      description:
        "List the configured cluster nodes with host, user, role, tags, sudo mode, storage config and the recorded hardware inventory (CPU, GPU/accelerators, memory, disks, OS). Start here to discover valid target names and per-node capacity. Also reports the local monitoring dashboard URL, if one is running - worth telling the user about when they want to watch what you are doing.",
      inputSchema: {},
    },
    async () => {
      const nodes = ctx.config.nodes.map((n) => ({
        name: n.name,
        host: n.host,
        port: n.port,
        user: n.user,
        auth: n.auth,
        sudo: n.sudo,
        role: n.role,
        tags: n.tags,
        description: n.description,
        storage: n.storage,
        hardware: n.hardware,
      }));
      const m = ctx.config.monitoring;
      return json({
        configPath: ctx.config.configPath,
        nodeCount: nodes.length,
        monitoring: {
          logging: m.enabled,
          dashboard: ctx.monitor?.url,
          running: ctx.monitor !== undefined,
          logDir: m.enabled ? m.logDir : undefined,
          detail: ctx.monitor
            ? "Live view of every SSH interaction: per-node totals, tail-following log, filters by node/status/text. Localhost only."
            : m.enabled && m.web
              ? "Dashboard did not start - check the daemon's stderr (port already in use, or dist/web assets missing)."
              : "Dashboard disabled in the inventory's monitoring section.",
        },
        nodes,
      });
    },
  );

  server.registerTool(
    "cluster_hardware",
    {
      title: "Hardware inventory",
      description:
        "Report the recorded hardware of each target: CPU (model, SoC, architecture, cores, max MHz), GPUs/accelerators (vendor, model, memory, runtime), memory and swap size, block devices, mounted filesystems and OS/kernel/board. Nodes are probed automatically the first time the daemon connects; pass refresh:true to re-probe (e.g. after adding a GPU or disk, or upgrading the OS), which also writes the result back to the inventory file.",
      inputSchema: {
        targets: targetsSchema,
        refresh: z.boolean().optional().describe("Re-probe the nodes over SSH instead of returning the recorded values."),
        save: z
          .boolean()
          .optional()
          .describe("With refresh, persist the discovered hardware to the inventory file. Default true."),
        timeoutMs: timeoutSchema,
      },
    },
    async ({ targets, refresh, save, timeoutMs }) => {
      try {
        const nodes = resolveTargets(ctx.config, targets);
        if (refresh) {
          const { probes, saved } = await refreshHardware(ctx.config, ctx.pool, nodes, { save, timeoutMs });
          return json({ refreshed: true, savedTo: saved.length > 0 ? ctx.config.configPath : undefined, saved, nodes: probes });
        }
        return json({
          refreshed: false,
          nodes: nodes.map((n) => ({
            node: n.name,
            host: n.host,
            ...(n.hardware ? { hardware: n.hardware } : { error: "Not probed yet - call again with refresh:true." }),
          })),
        });
      } catch (err) {
        return errorText(err);
      }
    },
  );

  server.registerTool(
    "cluster_ping",
    {
      title: "Check connectivity and privileges",
      description:
        "Verify SSH connectivity, the effective remote user, group membership and whether passwordless sudo works on each target. Use this to diagnose setup problems.",
      inputSchema: { targets: targetsSchema },
    },
    async ({ targets }) => {
      try {
        const nodes = resolveTargets(ctx.config, targets);
        const results = await ctx.pool.execMany(nodes, PING_SCRIPT, { timeoutMs: 20_000 });
        const summary = results.map((r) => ({
          node: r.node,
          host: r.host,
          reachable: r.error === undefined,
          ...(r.error ? { error: r.error } : parseKeyValueLines(r.stdout)),
        }));
        return json(summary);
      } catch (err) {
        return errorText(err);
      }
    },
  );

  server.registerTool(
    "cluster_status",
    {
      title: "System status",
      description:
        "Collect a health snapshot from each target: OS/kernel, uptime, load, CPU temperature and governor, memory and swap, disk usage, failed systemd units, pending apt upgrades and reboot-required flag. Requires no sudo.",
      inputSchema: {
        targets: targetsSchema,
        raw: z.boolean().optional().describe("Return raw script output instead of parsed JSON."),
        timeoutMs: timeoutSchema,
      },
    },
    async ({ targets, raw, timeoutMs }) => {
      try {
        const nodes = resolveTargets(ctx.config, targets);
        const results = await ctx.pool.execMany(nodes, STATUS_SCRIPT, { timeoutMs: timeoutMs ?? 45_000 });
        if (raw) return { content: [{ type: "text" as const, text: renderResults("Cluster status", results) }] };

        const parsed = results.map((r) => {
          if (r.error || !r.ok) {
            return { node: r.node, host: r.host, online: false, error: r.error ?? r.stderr.trim() ?? `exit ${r.code}` };
          }
          const kv = parseKeyValueLines(r.stdout);
          const asArray = (v: string | string[] | undefined) => (v === undefined ? [] : Array.isArray(v) ? v : [v]);
          const memTotal = Number(kv.mem_total_mb ?? 0);
          const memAvail = Number(kv.mem_available_mb ?? 0);
          return {
            node: r.node,
            host: r.host,
            online: true,
            os: kv.os,
            armbian: kv.armbian,
            kernel: kv.kernel,
            arch: kv.arch,
            cpu: { model: kv.cpu_model, cores: Number(kv.cpu_cores ?? 0), mhz: kv.cpu_mhz ? Number(kv.cpu_mhz) : undefined, governor: kv.cpu_governor },
            temperatureC: asArray(kv.temp_c),
            uptime: kv.uptime,
            bootTime: kv.boot_time,
            loadAverage: kv.load,
            memory: {
              totalMb: memTotal,
              availableMb: memAvail,
              usedPct: memTotal ? Math.round(((memTotal - memAvail) / memTotal) * 100) : undefined,
              swapTotalMb: Number(kv.swap_total_mb ?? 0),
              swapFreeMb: Number(kv.swap_free_mb ?? 0),
            },
            disks: asArray(kv.disk),
            failedUnits: { count: Number(kv.failed_unit_count ?? 0), units: asArray(kv.failed_unit) },
            aptUpgradable: Number(kv.upgradable_count ?? 0),
            aptCacheAge: kv.apt_cache_age,
            rebootRequired: kv.reboot_required === "yes",
            topCpu: asArray(kv.top_cpu),
            addresses: kv.ip,
            sampledAt: kv.time,
          };
        });
        return json(parsed);
      } catch (err) {
        return errorText(err);
      }
    },
  );
}
