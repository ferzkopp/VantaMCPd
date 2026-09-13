(function () {
  "use strict";

  const MAX_ROWS = 800;
  const logEl = document.getElementById("log");
  const nodeSel = document.getElementById("node");
  const statusSel = document.getElementById("status");
  const qEl = document.getElementById("q");
  const followBtn = document.getElementById("follow");
  const clearBtn = document.getElementById("clear");
  const countEl = document.getElementById("count");
  const scopeEl = document.getElementById("scope");
  const dot = document.getElementById("dot");
  const state = document.getElementById("state");
  const clusterCountEl = document.getElementById("cluster-count");
  const dlg = document.getElementById("detail");
  const dlgBody = document.getElementById("dlg-body");

  const knownNodes = new Set();
  const knownStatuses = new Set();
  let following = true;
  let shown = 0;

  // Undefined locale => the browser's own, so separators match what the reader expects.
  const num = new Intl.NumberFormat();

  /** Must match statusKey() on the server so filtering agrees between the two. */
  function statusOf(e) {
    return e.ok ? "ok" : e.code === null ? "error" : "exit " + e.code;
  }

  function matches(e) {
    if (nodeSel.value && e.node !== nodeSel.value) return false;
    if (statusSel.value && statusOf(e) !== statusSel.value) return false;
    const needle = qEl.value.trim().toLowerCase();
    if (!needle) return true;
    const hay = [e.node, e.tool || "", e.parameters || "", e.command || "", e.error || "", e.preview || ""]
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
    row.title =
      `${e.ts.replace("T", " ").slice(0, 19)}  ·  ${e.node}  ·  ${e.tool || e.kind}` +
      (e.parameters ? `\nparameters: ${e.parameters}` : "") +
      `\n${e.command || ""}` +
      (e.error ? `\n\nerror: ${e.error}` : "") +
      (e.preview ? `\n\noutput: ${e.preview}` : "");
    row.appendChild(cell(e.ts.slice(11, 19), "dim"));
    row.appendChild(cell(e.node));
    row.appendChild(cell(e.tool || e.kind, "dim"));
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

  function updateScope() {
    const bits = [];
    if (nodeSel.value) bits.push(nodeSel.value);
    if (statusSel.value) bits.push(statusSel.value);
    if (qEl.value.trim()) bits.push(`"${qEl.value.trim()}"`);
    scopeEl.textContent = bits.length ? "— filtered by " + bits.join(" · ") : "";
  }

  function load() {
    resetLog();
    updateScope();
    const p = new URLSearchParams({ limit: "500" });
    if (nodeSel.value) p.set("node", nodeSel.value);
    if (statusSel.value) p.set("status", statusSel.value);
    if (qEl.value.trim()) p.set("q", qEl.value.trim());

    fetch("/api/events?" + p)
      .then((r) => r.json())
      .then((d) => {
        d.nodes.forEach((n) => addOption(nodeSel, knownNodes, n));
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
            n.lastTs ? n.lastTs.slice(11, 19) : "-",
          ].forEach((v, i) => {
            const td = document.createElement("td");
            td.textContent = v;
            if (i === 2) {
              td.title = n.moduleError || (n.moduleNames || []).join(", ") || "No modules installed";
              if (n.moduleReachable === false) td.className = "warn";
            }
            if (i === 4 && n.failed > 0) td.className = "bad";
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

  function renderNode(n) {
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
    addOption(statusSel, knownStatuses, statusOf(e));
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
  nodeSel.onchange = load;
  statusSel.onchange = load;

  let debounce;
  qEl.oninput = () => {
    clearTimeout(debounce);
    debounce = setTimeout(load, 250);
  };

  load();
  summary();
  setInterval(summary, 4000);
})();
