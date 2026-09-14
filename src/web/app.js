import { formatDuration, formatRelativeTime, formatUtcTimestamp } from "./time.js";

(function () {
  "use strict";

  const MAX_ROWS = 800;
  const RELATIVE_TIME_UPDATE_MS = 15_000;
  const logEl = document.getElementById("log");
  const nodeSel = document.getElementById("node");
  const moduleSel = document.getElementById("module");
  const statusSel = document.getElementById("status");
  const pollingToggle = document.getElementById("polling");
  const pollingLabel = document.getElementById("polling-label");
  const qEl = document.getElementById("q");
  const followBtn = document.getElementById("follow");
  const clearBtn = document.getElementById("clear");
  const countEl = document.getElementById("count");
  const scopeEl = document.getElementById("scope");
  const logFileEl = document.getElementById("log-file");
  const jobPollEl = document.getElementById("job-poll");
  const dot = document.getElementById("dot");
  const state = document.getElementById("state");
  const clusterCountEl = document.getElementById("cluster-count");
  const dlg = document.getElementById("detail");
  const dlgBody = document.getElementById("dlg-body");

  const knownNodes = new Set();
  const knownModules = new Set();
  const knownStatuses = new Set();
  let following = true;
  let shown = 0;
  let logFileUrl = "";
  let latestModules = { modules: [], pending: true };
  let latestJobs = { jobs: [] };

  // Undefined locale => the browser's own, so separators match what the reader expects.
  const num = new Intl.NumberFormat();

  /** Must match statusKey() on the server so filtering agrees between the two. */
  function statusOf(e) {
    return e.ok ? "ok" : e.code === null ? "error" : "exit " + e.code;
  }

  function matches(e) {
    if (!pollingToggle.checked && e.origin === "engine") return false;
    if (nodeSel.value && e.node !== nodeSel.value) return false;
    if (moduleSel.value && (e.module || "core") !== moduleSel.value) return false;
    if (statusSel.value && statusOf(e) !== statusSel.value) return false;
    const needle = qEl.value.trim().toLowerCase();
    if (!needle) return true;
    const hay = [e.node, e.module || "core", e.tool || "", e.parameters || "", e.command || "", e.error || "", e.preview || ""]
      .join(" ")
      .toLowerCase();
    return hay.includes(needle);
  }

  function cell(text, cls) {
    const d = document.createElement("div");
    if (cls) d.className = cls;
    d.textContent = text; // textContent, never innerHTML: command text is untrusted
    return d;
  }

  function addRow(e, prepend) {
    const row = document.createElement("div");
    row.className = "row";
    const timestamp = formatUtcTimestamp(e.ts);
    row.title =
      `${timestamp}  ·  ${e.node}  ·  ${e.tool || e.kind}` +
      (e.parameters ? `\nparameters: ${e.parameters}` : "") +
      `\n${e.command || ""}` +
      (e.error ? `\n\nerror: ${e.error}` : "") +
      (e.preview ? `\n\noutput: ${e.preview}` : "");
    row.appendChild(cell(timestamp, "dim"));
    row.appendChild(cell(e.node));
    row.appendChild(cell(e.module || "core", "dim"));
    row.appendChild(cell(e.tool || e.kind, "dim"));
    row.appendChild(cell(e.origin || "agent", "dim"));
    row.appendChild(cell(statusOf(e), e.ok ? "ok" : "bad"));
    row.appendChild(cell(e.durationMs + "ms" + (e.sudo ? " sudo" : ""), e.sudo ? "warn" : "dim"));
    row.appendChild(cell(e.parameters || "-", "params"));
    row.appendChild(e.error ? cell(e.error, "err") : cell((e.command || "").replace(/\s+/g, " ").trim(), "cmd"));

    if (prepend) logEl.insertBefore(row, logEl.firstChild);
    else logEl.appendChild(row);
    shown++;
    while (shown > MAX_ROWS && logEl.lastChild) {
      logEl.removeChild(logEl.lastChild);
      shown--;
    }
    countEl.textContent = shown;
  }

  function resetLog() {
    logEl.textContent = "";
    shown = 0;
    countEl.textContent = "0";
  }

  function placeholder(text) {
    resetLog();
    const d = document.createElement("div");
    d.className = "empty";
    d.textContent = text;
    logEl.appendChild(d);
  }

  function addOption(sel, known, value) {
    if (known.has(value)) return;
    known.add(value);
    const o = document.createElement("option");
    o.value = value;
    o.textContent = value;
    sel.appendChild(o);
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch {
      const input = document.createElement("textarea");
      input.value = text;
      input.style.position = "fixed";
      input.style.opacity = "0";
      document.body.appendChild(input);
      input.select();
      const copied = document.execCommand("copy");
      input.remove();
      if (!copied) throw new Error("copy failed");
    }
  }

  function updateRelativeTimes() {
    document.querySelectorAll("#summary [data-last-seen], #jobs [data-last-seen]").forEach((element) => {
      element.textContent = formatRelativeTime(element.dataset.lastSeen);
    });
  }

  function formatBytes(bytes) {
    if (bytes < 1024) return num.format(bytes) + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KiB";
    return (bytes / (1024 * 1024)).toFixed(1) + " MiB";
  }

  function renderModules(modules, pending) {
    latestModules = { modules, pending };
    const tb = document.querySelector("#modules tbody");
    tb.textContent = "";
    const filteredModules = modules.filter((module) => !moduleSel.value || module.id === moduleSel.value);
    filteredModules.forEach((module) => {
      const tr = document.createElement("tr");
      tr.dataset.module = module.id;
      tr.dataset.name = module.name;
      tr.title = module.description;
      const deployment = module.deployment.routing
        ? `${module.deployment.mode} · ${module.deployment.routing}`
        : module.deployment.mode;
      const runtime = module.runtime.mode === "service" ? "service" : "on demand";
      const installedVersions = module.installedVersions.join(", ") || "-";
      const values = [
        module.id,
        module.name,
        installedVersions,
        `${module.nodeCount} / ${module.configuredNodeCount}`,
        deployment,
        runtime,
        `${num.format(module.packageFiles)} files · ${formatBytes(module.packageBytes)}`,
      ];
      values.forEach((value, index) => {
        const td = document.createElement("td");
        td.textContent = value;
        if (index === 0) td.title = module.description;
        if (index === 1) td.title = module.description;
        if (index === 2 && module.installedVersions.some((version) => version !== module.catalogVersion)) {
          td.className = "warn";
          td.title = `Local catalog version: ${module.catalogVersion}`;
        }
        if (index === 3) {
          td.title = module.installedNodes
            .map((node) => `${node.node}@${node.version}${node.stale ? " (stale)" : ""}`)
            .join(", ");
          if (module.installedNodes.some((node) => node.stale || !node.reachable)) td.className = "warn";
        }
        tr.appendChild(td);
      });
      tb.appendChild(tr);
    });

    if (!filteredModules.length) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 7;
      td.className = "empty";
      td.textContent = pending
        ? "discovering installed modules…"
        : modules.length ? "no modules match the selected module" : "no active modules";
      tr.appendChild(td);
      tb.appendChild(tr);
    }
  }

  function renderJobs(data) {
    latestJobs = data;
    const tb = document.querySelector("#jobs tbody");
    tb.textContent = "";
    const jobs = (data.jobs || []).filter((job) => {
      if (moduleSel.value && (job.moduleId || "core") !== moduleSel.value) return false;
      return !statusSel.value || job.displayStatus === statusSel.value;
    });
    jobs.forEach((job) => {
      const tr = document.createElement("tr");
      const progress = job.progress;
      const progressText = progress
        ? `${num.format(progress.current || 0)}${progress.total ? ` / ${num.format(progress.total)}` : ""}${progress.unit ? ` ${progress.unit}` : ""}`
        : "-";
      const started = Date.parse(job.startedAt || "");
      const ended = Date.parse(job.finishedAt || new Date().toISOString());
      const duration = Number.isFinite(started) && Number.isFinite(ended) ? formatDuration(ended - started) : "-";
      const values = [
        job.targetNode,
        job.moduleId || "core",
        job.kind,
        job.displayStatus,
        job.phase || "-",
        progressText,
        job.startedAt ? formatUtcTimestamp(job.startedAt) : "-",
        duration,
        job.heartbeatAt ? formatRelativeTime(job.heartbeatAt) : "-",
      ];
      values.forEach((value, index) => {
        const td = document.createElement("td");
        td.textContent = value;
        if (index === 3) td.className = job.displayStatus === "ok" ? "ok" : job.displayStatus === "running" || job.displayStatus === "queued" ? "warn" : "bad";
        if (index === 5 && progress?.message) td.title = progress.message;
        if (index === 8 && job.heartbeatAt) {
          td.dataset.lastSeen = job.heartbeatAt;
          td.title = formatUtcTimestamp(job.heartbeatAt);
        }
        tr.appendChild(td);
      });
      tr.title = `${job.jobId} · lifecycle status: ${job.status}` + (job.error ? `\n${job.error}` : job.result?.summary ? `\n${job.result.summary}` : "");
      tb.appendChild(tr);
    });
    if (!jobs.length) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 9;
      td.className = "empty";
      td.textContent = (data.jobs || []).length ? "no jobs match the selected status" : data.refreshedAt ? "no durable jobs" : "discovering durable jobs…";
      tr.appendChild(td);
      tb.appendChild(tr);
    }
  }

  function loadJobs() {
    fetch("/api/jobs")
      .then((response) => response.json())
      .then((data) => {
        (data.jobs || []).forEach((job) => addOption(moduleSel, knownModules, job.moduleId || "core"));
        (data.jobs || []).forEach((job) => addOption(statusSel, knownStatuses, job.displayStatus));
        if (jobPollEl && data.pollIntervalMs) {
          // An interval reads better without the trailing zero seconds that elapsed times keep.
          const interval = formatDuration(data.pollIntervalMs).replace(/ 0s$/, "");
          jobPollEl.textContent = ` \u00b7 daemon polls every ${interval}`;
          jobPollEl.title = `jobs.pollIntervalMs in the inventory file (${data.pollIntervalMs} ms). The dashboard refreshes this table more often than the daemon re-reads remote job state.`;
        }
        renderJobs(data);
      })
      .catch(() => void 0);
  }

  function updateScope() {
    const bits = [];
    if (nodeSel.value) bits.push(nodeSel.value);
    if (moduleSel.value) bits.push(moduleSel.value);
    if (statusSel.value) bits.push(statusSel.value);
    if (qEl.value.trim()) bits.push(`"${qEl.value.trim()}"`);
    if (pollingToggle.checked) bits.push("polling shown");
    scopeEl.textContent = bits.length ? "— filtered by " + bits.join(" · ") : "";
  }

  function load() {
    resetLog();
    updateScope();
    const p = new URLSearchParams({ limit: "500" });
    if (nodeSel.value) p.set("node", nodeSel.value);
    if (moduleSel.value) p.set("module", moduleSel.value);
    if (statusSel.value) p.set("status", statusSel.value);
    if (qEl.value.trim()) p.set("q", qEl.value.trim());
    p.set("includeEngine", String(pollingToggle.checked));

    fetch("/api/events?" + p)
      .then((r) => r.json())
      .then((d) => {
        logFileUrl = d.logFileUrl;
        logFileEl.disabled = !logFileUrl;
        logFileEl.title = `Copy ${logFileUrl}`;
        d.nodes.forEach((n) => addOption(nodeSel, knownNodes, n));
        (d.modules || []).forEach((module) => addOption(moduleSel, knownModules, module));
        (d.statuses || []).forEach((s) => addOption(statusSel, knownStatuses, s));
        if (!d.events.length) {
          placeholder("no matching interactions yet…");
          return;
        }
        d.events
          .slice()
          .reverse()
          .forEach((e) => addRow(e, false));
      })
      .catch(() => placeholder("could not reach the daemon"));
  }

  function summary() {
    fetch("/api/summary")
      .then((r) => r.json())
      .then((d) => {
        clusterCountEl.textContent = num.format(d.configured.length) + (d.configured.length === 1 ? " node" : " nodes");
        d.configured.forEach((c) => {
          addOption(nodeSel, knownNodes, c.name);
        });
        (d.availableModules || []).forEach((module) => addOption(moduleSel, knownModules, module));

        const tb = document.querySelector("#summary tbody");
        tb.textContent = "";
        d.nodes.forEach((n) => {
          const tr = document.createElement("tr");
          tr.setAttribute("data-node", n.node);
          tr.title = "Show configuration and hardware for " + n.node;
          [
            n.node,
            n.role || "-",
            n.moduleCount === undefined ? "…" : n.moduleCount,
            n.total,
            n.failed,
            num.format(n.avgMs),
            num.format(n.totalMs),
            (n.bytes / 1024).toFixed(1) + "k",
            n.lastTool || "-",
            n.lastTs ? formatRelativeTime(n.lastTs) : "-",
          ].forEach((v, i) => {
            const td = document.createElement("td");
            td.textContent = v;
            if (i === 2) {
              td.title = n.moduleError || (n.moduleNames || []).join(", ") || "No modules installed";
              if (n.moduleReachable === false) td.className = "warn";
            }
            if (i === 4 && n.failed > 0) td.className = "bad";
            if (i === 9 && n.lastTs) {
              td.dataset.lastSeen = n.lastTs;
              td.title = formatUtcTimestamp(n.lastTs);
            }
            tr.appendChild(td);
          });
          tb.appendChild(tr);
        });

        if (!d.nodes.length) {
          const tr = document.createElement("tr");
          const td = document.createElement("td");
          td.colSpan = 10;
          td.className = "empty";
          td.textContent = "no interactions recorded yet";
          tr.appendChild(td);
          tb.appendChild(tr);
        }
        renderModules(d.modules || [], d.moduleInventoryPending === true);
      })
      .catch(() => void 0);
  }

  // ---- node detail dialog ---------------------------------------------------

  function section(title) {
    const s = document.createElement("div");
    s.className = "sec";
    const h = document.createElement("h3");
    h.textContent = title;
    s.appendChild(h);
    const dl = document.createElement("dl");
    dl.className = "kv";
    s.appendChild(dl);
    s.dl = dl;
    return s;
  }

  function kv(dl, label, value) {
    if (value === undefined || value === null || value === "") return;
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.textContent = String(value);
    dl.appendChild(dt);
    dl.appendChild(dd);
  }

  function box(parent, title, lines) {
    const b = document.createElement("div");
    b.className = "sub";
    const t = document.createElement("div");
    t.className = "t";
    t.textContent = title;
    b.appendChild(t);
    lines.forEach((l) => {
      const d = document.createElement("div");
      d.className = "dim";
      d.textContent = l;
      b.appendChild(d);
    });
    parent.appendChild(b);
  }

  function apiTool(parent, tool) {
    const item = document.createElement("div");
    item.className = "sub api-tool";
    const name = document.createElement("div");
    name.className = "t";
    name.textContent = tool.name || "unnamed tool";
    item.appendChild(name);
    const description = document.createElement("p");
    description.textContent = tool.description || "No description advertised.";
    item.appendChild(description);

    const details = document.createElement("details");
    details.className = "api-schema";
    const summary = document.createElement("summary");
    summary.textContent = "input schema";
    details.appendChild(summary);
    const schema = document.createElement("pre");
    try {
      schema.textContent = JSON.stringify(tool.inputSchema || {}, null, 2);
    } catch {
      schema.textContent = "schema unavailable";
    }
    details.appendChild(schema);
    item.appendChild(details);
    parent.appendChild(item);
  }

  function renderNode(n) {
    dlg.classList.add("node-detail-dialog");
    dlgBody.classList.add("node-detail");
    document.getElementById("dlg-title").textContent = n.name;
    document.getElementById("dlg-role").textContent = n.role;
    dlgBody.textContent = "";

    const access = section("Access");
    kv(access.dl, "port", n.port);
    kv(access.dl, "auth", n.auth);
    kv(access.dl, "sudo", n.sudo);
    kv(access.dl, "tags", (n.tags || []).join(", "));
    kv(access.dl, "description", n.description);
    dlgBody.appendChild(access);

    const hw = n.hardware || {};

    const mods = section("Modules");
    const moduleState = n.modules;
    kv(mods.dl, "installed", moduleState?.count === undefined ? "discovering" : moduleState.count);
    const installedModules = moduleState?.modules?.map((id) =>
      moduleState.moduleVersions?.[id] ? `${id}@${moduleState.moduleVersions[id]}` : id,
    );
    kv(mods.dl, "modules", installedModules?.join(", ") || (moduleState?.count === 0 ? "none" : undefined));
    kv(mods.dl, "invalid receipts", moduleState?.invalidReceipts?.join(", "));
    kv(mods.dl, "inventory", moduleState?.stale ? "stale" : moduleState?.reachable === false ? "unreachable" : "current");
    kv(mods.dl, "last refresh", moduleState?.refreshedAt?.replace("T", " ").slice(0, 19));
    kv(mods.dl, "error", moduleState?.error);
    dlgBody.appendChild(mods);

    if (hw.cpu) {
      const cpu = section("CPU");
      kv(cpu.dl, "model", hw.cpu.model);
      kv(cpu.dl, "soc", hw.cpu.soc);
      kv(cpu.dl, "architecture", hw.cpu.arch);
      kv(cpu.dl, "package arch", hw.cpu.packageArch);
      kv(cpu.dl, "cores", hw.cpu.cores);
      kv(cpu.dl, "max MHz", hw.cpu.maxMhz);
      dlgBody.appendChild(cpu);
    }

    const gpu = section("GPU");
    const gpus = (hw.accelerators || []).filter((accelerator) => accelerator.kind.toLowerCase() === "gpu");
    kv(gpu.dl, "detected", gpus.length || "none");
    gpus.forEach((device, index) => {
      const details = [
        device.vendor,
        device.memoryMb === undefined ? undefined : `${device.memoryMb} MB VRAM`,
        device.runtime && device.runtimeVersion ? `${device.runtime} ${device.runtimeVersion}` : device.runtime,
      ].filter(Boolean);
      box(gpu, device.model || `GPU ${index + 1}`, details);
    });
    dlgBody.appendChild(gpu);

    if (hw.memory) {
      const mem = section("Memory");
      kv(mem.dl, "RAM", hw.memory.totalMb + " MB");
      kv(mem.dl, "swap active", hw.memory.swapTotalMb + " MB");
      kv(mem.dl, "swap if all on", hw.memory.swapPotentialMb + " MB");
      kv(
        mem.dl,
        "survives reboot",
        hw.memory.swapPersistent === undefined ? undefined : hw.memory.swapPersistent ? "yes" : "NO - not in fstab",
      );
      dlgBody.appendChild(mem);
      (hw.memory.swapDevices || []).forEach((s) => {
        box(mem, s.name, [
          `${s.type || "?"} · ${s.sizeMb || "?"} MB · priority ${s.priority} · ${s.persistent ? "in fstab" : "not in fstab"}`,
        ]);
      });
    }

    if (n.storage) {
      const st = section("Configured storage");
      kv(st.dl, "device", n.storage.device);
      kv(st.dl, "mountpoint", n.storage.mountpoint);
      kv(st.dl, "filesystem", n.storage.fsType);
      kv(st.dl, "label", n.storage.label);
      if (n.storage.nfs) {
        kv(st.dl, "NFS export", n.storage.nfs.enabled ? "enabled" : "disabled");
        kv(st.dl, "NFS options", n.storage.nfs.options);
      }
      dlgBody.appendChild(st);
    }

    if (hw.disks && hw.disks.length) {
      const ds = section("Disks");
      dlgBody.appendChild(ds);
      hw.disks.forEach((d) => {
        const head = `${d.name}  —  ${d.role || "?"}${d.roleSource === "configured" ? " (set by you)" : ""}`;
        const lines = [
          `${d.sizeGb || "?"} GB${d.model ? " · " + d.model : ""}${d.removable ? " · removable" : ""}` +
            `${d.rotational === false ? " · non-rotational" : ""}`,
        ];
        (d.partitions || []).forEach((p) => {
          lines.push(
            `  ${p.name} · ${p.sizeGb || "?"} GB · ${p.fsType || "no filesystem"}` +
              `${p.label ? ' · "' + p.label + '"' : ""}${p.mountpoint ? " → " + p.mountpoint : ""}`,
          );
        });
        box(ds, head, lines);
      });
    }

    if (hw.filesystems && hw.filesystems.length) {
      const fs = section("Mounted filesystems");
      hw.filesystems.forEach((f) => {
        kv(fs.dl, f.mountpoint, `${f.fsType || "?"} · ${f.sizeGb || "?"} GB · ${f.device || "?"}`);
      });
      dlgBody.appendChild(fs);
    }

    if (hw.os) {
      const os = section("Operating system");
      kv(os.dl, "distribution", hw.os.name);
      kv(os.dl, "version", hw.os.version);
      kv(os.dl, "kernel", hw.os.kernel);
      kv(os.dl, "armbian", hw.os.armbian);
      kv(os.dl, "board", hw.os.boardModel || hw.os.board);
      kv(os.dl, "machine id", hw.machineId);
      kv(os.dl, "last probed", hw.discoveredAt ? hw.discoveredAt.replace("T", " ").slice(0, 19) : undefined);
      dlgBody.appendChild(os);
    }

    if (!n.hardware) {
      const none = document.createElement("div");
      none.className = "empty";
      none.textContent = 'No hardware recorded yet - run "npm run discover" or cluster_hardware { refresh: true }.';
      dlgBody.appendChild(none);
    }
  }

  function renderModule(data) {
    dlg.classList.remove("node-detail-dialog");
    dlgBody.classList.remove("node-detail");
    const module = data.module;
    const api = data.api;
    document.getElementById("dlg-title").textContent = module.name;
    document.getElementById("dlg-role").textContent = module.id;
    dlgBody.textContent = "";

    const overview = section("Overview");
    kv(overview.dl, "description", module.description);
    kv(overview.dl, "catalog version", module.catalogVersion);
    kv(overview.dl, "installed versions", module.installedVersions.join(", "));
    kv(
      overview.dl,
      "deployment",
      module.deployment.routing
        ? `${module.deployment.mode} (${module.deployment.routing})`
        : module.deployment.mode,
    );
    kv(overview.dl, "runtime", module.runtime.mode);
    kv(overview.dl, "package", `${num.format(module.packageFiles)} files · ${formatBytes(module.packageBytes)}`);
    dlgBody.appendChild(overview);

    const installations = section("Installations");
    kv(installations.dl, "nodes", `${module.nodeCount} of ${module.configuredNodeCount}`);
    module.installedNodes.forEach((node) => {
      kv(
        installations.dl,
        node.node,
        `${node.version}${node.stale ? " · stale" : node.reachable ? " · reachable" : " · unreachable"}`,
      );
    });
    dlgBody.appendChild(installations);

    const requirements = section("Requirements");
    kv(requirements.dl, "operating systems", module.compatibility.os?.join(", "));
    kv(requirements.dl, "architectures", module.compatibility.architectures?.join(", "));
    kv(requirements.dl, "minimum cores", module.compatibility.minCores);
    kv(requirements.dl, "minimum RAM", module.compatibility.minRamMb ? `${module.compatibility.minRamMb} MB` : undefined);
    kv(requirements.dl, "minimum disk", module.compatibility.minDiskMb ? `${module.compatibility.minDiskMb} MB` : undefined);
    kv(requirements.dl, "commands", module.compatibility.requiredCommands?.join(", "));
    dlgBody.appendChild(requirements);

    const tools = Array.isArray(api.tools) ? api.tools : [];
    const mcp = section(`MCP API · ${tools.length} ${tools.length === 1 ? "tool" : "tools"}`);
    kv(mcp.dl, "served by", api.node);
    kv(mcp.dl, "server", api.server ? `${api.server.name}@${api.server.version}` : undefined);
    kv(mcp.dl, "module version", api.version);
    dlgBody.appendChild(mcp);
    tools.forEach((tool) => apiTool(mcp, tool));
  }

  // ---- wiring ---------------------------------------------------------------

  document.querySelector("#summary tbody").addEventListener("click", (ev) => {
    const tr = ev.target.closest("tr[data-node]");
    if (!tr) return;
    fetch("/api/node?name=" + encodeURIComponent(tr.getAttribute("data-node")))
      .then((r) => r.json())
      .then((n) => {
        if (n.error) return;
        renderNode(n);
        dlg.showModal();
      })
      .catch(() => void 0);
  });
  document.querySelector("#modules tbody").addEventListener("click", (ev) => {
    const tr = ev.target.closest("tr[data-module]");
    if (!tr) return;
    dlg.classList.remove("node-detail-dialog");
    dlgBody.classList.remove("node-detail");
    document.getElementById("dlg-title").textContent = tr.dataset.name;
    document.getElementById("dlg-role").textContent = "MCP API";
    dlgBody.textContent = "";
    const loading = document.createElement("div");
    loading.className = "empty";
    loading.textContent = "loading module API…";
    dlgBody.appendChild(loading);
    dlg.showModal();
    fetch("/api/module?id=" + encodeURIComponent(tr.dataset.module))
      .then(async (response) => {
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "could not load module API");
        return data;
      })
      .then(renderModule)
      .catch((error) => {
        dlgBody.textContent = "";
        const message = document.createElement("div");
        message.className = "empty";
        message.textContent = error.message;
        dlgBody.appendChild(message);
      });
  });
  document.getElementById("dlg-x").onclick = () => dlg.close();
  dlg.addEventListener("click", (ev) => {
    if (ev.target === dlg) dlg.close();
  });

  const es = new EventSource("/api/stream");
  es.onopen = () => {
    dot.className = "dot on";
    state.textContent = "live";
  };
  es.onerror = () => {
    dot.className = "dot";
    state.textContent = "reconnecting…";
  };
  es.onmessage = (ev) => {
    const e = JSON.parse(ev.data);
    addOption(nodeSel, knownNodes, e.node);
    addOption(moduleSel, knownModules, e.module || "core");
    if (e.origin !== "engine" || pollingToggle.checked) addOption(statusSel, knownStatuses, statusOf(e));
    if (!following || !matches(e)) return;
    if (logEl.querySelector(".empty")) resetLog();
    addRow(e, true);
  };

  followBtn.onclick = () => {
    following = !following;
    followBtn.classList.toggle("active", following);
    followBtn.textContent = following ? "following" : "paused";
    if (following) load();
  };
  clearBtn.onclick = resetLog;
  logFileEl.onclick = () => {
    if (!logFileUrl) return;
    copyText(logFileUrl)
      .then(() => {
        logFileEl.textContent = "copied";
        setTimeout(() => { logFileEl.textContent = "copy log path"; }, 1500);
      })
      .catch(() => {
        logFileEl.textContent = "copy failed";
        setTimeout(() => { logFileEl.textContent = "copy log path"; }, 1500);
      });
  };
  nodeSel.onchange = load;
  moduleSel.onchange = () => {
    load();
    renderModules(latestModules.modules, latestModules.pending);
    renderJobs(latestJobs);
  };
  statusSel.onchange = () => {
    load();
    renderJobs(latestJobs);
  };
  pollingToggle.onchange = () => {
    pollingLabel.textContent = pollingToggle.checked ? "polling on" : "polling off";
    load();
  };

  let debounce;
  qEl.oninput = () => {
    clearTimeout(debounce);
    debounce = setTimeout(load, 250);
  };

  load();
  summary();
  loadJobs();
  setInterval(summary, 4000);
  setInterval(loadJobs, 4000);
  setInterval(updateRelativeTimes, RELATIVE_TIME_UPDATE_MS);
})();
