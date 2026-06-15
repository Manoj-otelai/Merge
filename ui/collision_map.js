/* ──────────────────────────────────────────────────────────────────────────
   MergeGuard Collision Map — D3.js force-directed graph (light theme)
   ────────────────────────────────────────────────────────────────────────── */
(function () {
  "use strict";

  const POLL_INTERVAL = 30_000;
  const SEV_RANK  = { low: 1, medium: 2, high: 3, critical: 4 };
  const SEV_COLOR = { low: "#12b76a", medium: "#dc9a04", high: "#e8590c", critical: "#d92d20" };
  const HAS_D3 = typeof d3 !== "undefined";

  let svg, root, linkLayer, labelLayer, nodeLayer, zoom, simulation, tooltip;
  let width = 0, height = 0;
  let currentData = { nodes: [], edges: [] };
  let currentAnalytics = null;
  let currentPlan = null;
  let nodeMap = new Map();              // id → node, updated on each render
  let selected = null;                 // { type:'node'|'edge', id }
  let currentView = "graph";

  // Filter state (list view)
  let filterSev = "all";
  let searchQuery = "";
  let listSelectedIdx = -1;

  // Stat animation state
  const statPrev = {};

  // "Ask MergeGuard" — persists the last answer across live re-renders
  let lastAskAnswer = "";
  let lastAskQuestion = "";

  // ── Init ─────────────────────────────────────────────────────────────────
  function init() {
    document.getElementById("refresh-btn").onclick = fetchAndRender;

    document.querySelectorAll("#view-toggle button").forEach((btn) => {
      btn.onclick = () => switchView(btn.dataset.view);
    });

    // Collisions stat card → switch to list view
    const collCard = document.getElementById("stat-collisions-card");
    if (collCard) {
      collCard.onclick = () => switchView("list");
      collCard.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); switchView("list"); } };
    }

    // Cost-saved stat card → switch to insights view
    const savedCard = document.getElementById("stat-saved-card");
    if (savedCard) {
      savedCard.onclick = () => switchView("insights");
      savedCard.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); switchView("insights"); } };
    }

    // Filter pills
    document.querySelectorAll(".filter-pill").forEach((pill) => {
      pill.onclick = () => {
        filterSev = pill.dataset.sev;
        document.querySelectorAll(".filter-pill").forEach((p) => p.classList.remove("active"));
        pill.classList.add("active");
        listSelectedIdx = -1;
        renderList(currentData);
      };
    });

    // Search input
    const searchEl = document.getElementById("list-search");
    if (searchEl) {
      searchEl.oninput = (e) => {
        searchQuery = e.target.value.toLowerCase().trim();
        listSelectedIdx = -1;
        renderList(currentData);
      };
    }

    // Export button
    const exportBtn = document.getElementById("export-btn");
    if (exportBtn) exportBtn.onclick = () => downloadExport();

    // Shortcuts modal
    const modal = document.getElementById("shortcuts-modal");
    const modalClose = document.getElementById("modal-close");
    if (modal && modalClose) {
      modalClose.onclick = () => modal.classList.add("hidden");
      modal.addEventListener("click", (e) => { if (e.target === modal) modal.classList.add("hidden"); });
    }

    // Keyboard shortcuts
    document.addEventListener("keydown", handleKeyDown);

    if (HAS_D3) {
      initGraph();
    } else {
      degradeToList();
    }

    fetchAndRender();
    setInterval(fetchAndRender, POLL_INTERVAL);
    connectSSE();
  }

  function connectSSE() {
    if (typeof EventSource === "undefined") return;
    const es = new EventSource("/api/stream");
    let reconnectMs = 2000;

    es.onopen = () => {
      const mode = document.getElementById("live-mode");
      if (mode) mode.textContent = "Live (SSE)";
      const dot = document.getElementById("live-dot");
      if (dot) dot.style.background = "";  // reset to green
    };

    es.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data);
        if (msg.type === "update" || msg.type === "snapshot") {
          currentData = msg;
          updateStats(msg);
          updateSavedStat(currentAnalytics);
          if (HAS_D3) render(msg);
          if (!HAS_D3 || currentView === "list") renderList(msg);
          document.getElementById("last-updated").textContent = timeNow();
          reconnectMs = 2000;
        }
      } catch { /* ignore parse errors */ }
    };

    es.onerror = () => {
      const dot = document.getElementById("live-dot");
      if (dot) dot.style.background = "var(--medium)";
      const mode = document.getElementById("live-mode");
      if (mode) mode.textContent = "Polling";
      // On disconnect, fall back to polling; try to reconnect SSE after a delay
      setTimeout(() => {
        if (es.readyState === EventSource.CLOSED) connectSSE();
      }, reconnectMs);
      reconnectMs = Math.min(reconnectMs * 2, 30000);
    };
  }

  function handleKeyDown(e) {
    const tag = (e.target?.tagName || "").toLowerCase();
    const isInput = tag === "input" || tag === "textarea";
    const modal = document.getElementById("shortcuts-modal");
    const modalVisible = modal && !modal.classList.contains("hidden");

    if (e.key === "Escape") {
      if (modalVisible) { modal.classList.add("hidden"); e.preventDefault(); return; }
      if (selected) { clearSelection(); e.preventDefault(); return; }
    }

    // Don't capture shortcuts when typing in an input
    if (isInput) return;

    switch (e.key) {
      case "?":
        if (modal) { modal.classList.toggle("hidden"); e.preventDefault(); }
        return;
      case "g": case "G":
        if (HAS_D3) { switchView("graph"); e.preventDefault(); } return;
      case "l": case "L":
        switchView("list"); e.preventDefault(); return;
      case "p": case "P":
        switchView("plan"); e.preventDefault(); return;
      case "t": case "T":
        switchView("timeline"); e.preventDefault(); return;
      case "i": case "I":
        switchView("insights"); e.preventDefault(); return;
      case "r": case "R":
        fetchAndRender(); e.preventDefault(); return;
      case "f": case "F":
        if (HAS_D3 && currentView === "graph") { fitToView(); e.preventDefault(); } return;
      case "+": case "=":
        if (HAS_D3 && currentView === "graph") {
          svg.transition().duration(220).call(zoom.scaleBy, 1.3); e.preventDefault();
        } return;
      case "-":
        if (HAS_D3 && currentView === "graph") {
          svg.transition().duration(220).call(zoom.scaleBy, 1 / 1.3); e.preventDefault();
        } return;
    }

    if (currentView === "list") {
      const rows = Array.from(document.querySelectorAll(".collision-row"));
      if (!rows.length) return;
      if (e.key === "ArrowDown") {
        e.preventDefault();
        listSelectedIdx = Math.min(listSelectedIdx + 1, rows.length - 1);
        focusListRow(rows, listSelectedIdx);
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        listSelectedIdx = Math.max(listSelectedIdx - 1, 0);
        focusListRow(rows, listSelectedIdx);
      } else if (e.key === "Enter" && listSelectedIdx >= 0 && rows[listSelectedIdx]) {
        rows[listSelectedIdx].click();
      }
    }
  }

  async function downloadExport() {
    const btn = document.getElementById("export-btn");
    if (btn) { btn.textContent = "⏳ Exporting…"; btn.disabled = true; }
    try {
      const resp = await fetch("/api/export");
      if (!resp.ok) throw new Error("API " + resp.status);
      const data = await resp.json();
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `mergeguard-export-${new Date().toISOString().slice(0, 10)}.json`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch (err) {
      console.error("Export failed:", err);
    } finally {
      if (btn) { btn.textContent = "⬇ Export"; btn.disabled = false; }
    }
  }

  function focusListRow(rows, idx) {
    rows.forEach((r, i) => r.classList.toggle("selected", i === idx));
    rows[idx]?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  function initGraph() {
    svg = d3.select("#collision-graph");
    tooltip = d3.select("#tooltip");

    sizeSvg();

    zoom = d3.zoom().scaleExtent([0.25, 3]).on("zoom", (e) => root.attr("transform", e.transform));
    svg.call(zoom).on("dblclick.zoom", null);
    svg.on("click", (e) => { if (e.target === svg.node()) clearSelection(); });

    root = svg.append("g");
    linkLayer  = root.append("g").attr("class", "links");
    labelLayer = root.append("g").attr("class", "link-labels");
    nodeLayer  = root.append("g").attr("class", "nodes");

    simulation = d3.forceSimulation()
      .force("link", d3.forceLink().id((d) => d.id).distance(190).strength(0.45))
      .force("charge", d3.forceManyBody().strength(-680))
      .force("center", d3.forceCenter(width / 2, height / 2))
      .force("x", d3.forceX(width / 2).strength(0.06))
      .force("y", d3.forceY(height / 2).strength(0.06))
      .force("collide", d3.forceCollide().radius((d) => nodeRadius(d) + 34))
      .on("tick", onTick);

    document.getElementById("zoom-in").onclick    = () => svg.transition().duration(220).call(zoom.scaleBy, 1.3);
    document.getElementById("zoom-out").onclick   = () => svg.transition().duration(220).call(zoom.scaleBy, 1 / 1.3);
    document.getElementById("zoom-reset").onclick = fitToView;

    window.addEventListener("resize", debounce(() => {
      sizeSvg();
      simulation.force("center", d3.forceCenter(width / 2, height / 2));
      simulation.alpha(0.3).restart();
    }, 200));
  }

  function degradeToList() {
    currentView = "list";
    document.querySelectorAll("#view-toggle button").forEach((b) => {
      if (b.dataset.view === "graph") b.disabled = true;
      b.classList.toggle("active", b.dataset.view === "list");
    });
    document.getElementById("graph-view").classList.add("hidden");
    document.getElementById("list-view").classList.remove("hidden");
    document.getElementById("list-toolbar").classList.remove("hidden");
    document.getElementById("loading-state").classList.add("hidden");
  }

  function sizeSvg() {
    const container = document.getElementById("graph-view");
    width  = container.clientWidth  || 800;
    height = container.clientHeight || 520;
    svg.attr("viewBox", `0 0 ${width} ${height}`);
  }

  // ── Data fetch ────────────────────────────────────────────────────────────
  let currentTimeline = null;

  async function fetchAndRender() {
    try {
      const [mapResp, anResp, planResp, tlResp] = await Promise.all([
        fetch("/api/collision-map"),
        fetch("/api/analytics").catch(() => null),
        fetch("/api/merge-plan").catch(() => null),
        fetch("/api/timeline").catch(() => null),
      ]);
      if (!mapResp.ok) throw new Error("API " + mapResp.status);
      const data = await mapResp.json();
      currentData = data;
      currentAnalytics = anResp && anResp.ok ? await anResp.json() : null;
      currentPlan = planResp && planResp.ok ? await planResp.json() : null;
      currentTimeline = tlResp && tlResp.ok ? (await tlResp.json()).timeline || [] : null;

      updateStats(data);
      updateSavedStat(currentAnalytics);
      if (HAS_D3) render(data);
      if (!HAS_D3 || currentView === "list") renderList(data);
      if (currentView === "plan") renderPlan(currentPlan);
      if (currentView === "timeline") renderTimeline(currentTimeline);
      if (currentView === "insights") renderInsights(currentAnalytics);
    } catch (err) {
      console.error("MergeGuard fetch error:", err);
    } finally {
      document.getElementById("loading-state").classList.add("hidden");
      document.getElementById("last-updated").textContent = timeNow();
    }
  }

  function updateSavedStat(analytics) {
    const el = document.getElementById("stat-saved");
    const meta = document.getElementById("stat-saved-meta");
    if (!el) return;
    if (!analytics || !analytics.cost_saved) { el.textContent = "—"; return; }
    el.textContent = compactMoney(analytics.cost_saved.dollars, analytics.cost_saved.currency);
    if (meta) {
      const hrs = Math.round(analytics.cost_saved.engineer_hours || 0);
      meta.textContent = `~${hrs} eng-hours saved`;
    }
  }

  // ── Stats (with count-up animation) ──────────────────────────────────────
  function animateCount(id, to) {
    const el = document.getElementById(id);
    if (!el) return;
    const from = statPrev[id] ?? 0;
    statPrev[id] = to;
    if (from === to) { el.textContent = to; return; }
    const dur = 520;
    const t0 = performance.now();
    const step = (now) => {
      const t = Math.min((now - t0) / dur, 1);
      const eased = 1 - Math.pow(1 - t, 3);   // ease-out cubic
      el.textContent = Math.round(from + (to - from) * eased);
      if (t < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }

  function updateStats(data) {
    const mrs = data.open_mrs ?? 0;
    const collisions = data.total_collisions ?? 0;
    animateCount("stat-mrs", mrs);
    animateCount("stat-collisions", collisions);

    let topSev = null;
    (data.edges || []).forEach((e) => {
      if (!topSev || SEV_RANK[e.severity_label] > SEV_RANK[topSev]) topSev = e.severity_label;
    });
    const sevEl  = document.getElementById("stat-severity");
    const metaEl = document.getElementById("stat-severity-meta");
    if (topSev) {
      sevEl.textContent  = topSev.toUpperCase();
      sevEl.style.color  = SEV_COLOR[topSev];
      metaEl.textContent = "needs coordination";
    } else {
      sevEl.textContent  = "—";
      sevEl.style.color  = "";
      metaEl.textContent = "no active risk";
    }

    const callers = (data.nodes || []).reduce((s, n) => s + (n.caller_count || 0), 0);
    animateCount("stat-callers", callers);

    // Update filter pill counts whenever data refreshes
    updateFilterCounts(data);
  }

  function updateFilterCounts(data) {
    const counts = { all: 0, critical: 0, high: 0, medium: 0, low: 0 };
    (data.edges || []).forEach((e) => {
      counts.all++;
      if (counts.hasOwnProperty(e.severity_label)) counts[e.severity_label]++;
    });
    document.querySelectorAll(".filter-pill").forEach((p) => {
      const sev = p.dataset.sev;
      const n = counts[sev] || 0;
      const label = sev === "all" ? "All" : cap(sev);
      p.textContent = `${label}${n > 0 ? ` (${n})` : ""}`;
    });
  }

  // ── Graph render ───────────────────────────────────────────────────────────
  function render(data) {
    const hasMrs = (data.nodes || []).length > 0;
    document.getElementById("empty-state").classList.toggle("hidden", hasMrs || currentView !== "graph");
    svg.classed("hidden", !hasMrs);
    if (!hasMrs) {
      linkLayer.selectAll("*").remove();
      nodeLayer.selectAll("*").remove();
      labelLayer.selectAll("*").remove();
      return;
    }

    // Preserve positions across refreshes
    const posById = new Map(simulation.nodes().map((n) => [n.id, n]));
    const nodes = data.nodes.map((n) => {
      const prev = posById.get(n.id);
      return Object.assign({}, n, prev ? { x: prev.x, y: prev.y, vx: prev.vx, vy: prev.vy } : {});
    });
    const edges = data.edges.map((e) => Object.assign({}, e));

    // Build node lookup for tooltips
    nodeMap = new Map(nodes.map((n) => [n.id, n]));

    // ── Links (curved arcs) ──
    linkLayer.selectAll("path.link")
      .data(edges, (d) => `${d.source}::${d.target}::${d.symbol}`)
      .join(
        (enter) => enter.append("path")
          .attr("class", (d) => `link ${d.severity_label}`)
          .attr("stroke-width", (d) => linkWidth(d))
          .on("click", (e, d) => { e.stopPropagation(); onEdgeClick(d); })
          .on("mouseover", (e, d) => {
            const sid = typeof d.source === "object" ? d.source.id : d.source;
            const tid = typeof d.target === "object" ? d.target.id : d.target;
            const src = nodeMap.get(sid) || {};
            const tgt = nodeMap.get(tid) || {};
            showTip(e, `<div class="tt-title">⚡ ${esc(d.symbol)}</div><div class="tt-sub">${cap(d.severity_label)} collision · !${src.iid || "?"} ↔ !${tgt.iid || "?"}</div>`);
          })
          .on("mousemove", moveTip)
          .on("mouseout", hideTip),
        (update) => update
          .attr("class", (d) => `link ${d.severity_label}`)
          .attr("stroke-width", (d) => linkWidth(d)),
        (exit) => exit.remove()
      );

    // ── Link labels ──
    labelLayer.selectAll("g.link-label-group")
      .data(edges, (d) => `${d.source}::${d.target}::${d.symbol}`)
      .join(
        (enter) => {
          const g = enter.append("g").attr("class", "link-label-group").style("pointer-events", "none");
          g.append("rect").attr("class", "link-label-bg").attr("rx", 5).attr("height", 16);
          g.append("text").attr("class", "link-label").attr("text-anchor", "middle").attr("dy", "0.32em");
          return g;
        },
        (update) => update,
        (exit) => exit.remove()
      )
      .select("text").text((d) => d.symbol);

    // ── Nodes ──
    nodeLayer.selectAll("g.node")
      .data(nodes, (d) => d.id)
      .join(
        (enter) => {
          const g = enter.append("g")
            .attr("class", (d) => `node ${isColliding(d.id) ? "warn" : "safe"}`)
            .call(d3.drag().on("start", dragStart).on("drag", dragged).on("end", dragEnd))
            .on("click", (e, d) => { e.stopPropagation(); onNodeClick(d); })
            .on("mouseover", (e, d) => {
              hoverHighlight(d.id, true);
              const dblHint = d.url ? ` · double-click to open` : "";
              showTip(e, `<div class="tt-title">!${d.iid} · ${esc(truncate(d.title, 40))}</div><div class="tt-sub">${esc(d.project)}${dblHint}</div>`);
            })
            .on("mousemove", moveTip)
            .on("mouseout", (e, d) => { hoverHighlight(d.id, false); hideTip(); });
          g.append("circle");
          g.append("text").attr("class", "node-iid").attr("dy", "0.32em");
          g.append("text").attr("class", "node-label");
          // Double-click opens the MR in a new tab
          g.on("dblclick", (e, nd) => {
            e.stopPropagation();
            if (nd.url) window.open(nd.url, "_blank", "noopener,noreferrer");
          });
          return g;
        },
        (update) => update.attr("class", (d) => `node ${isColliding(d.id) ? "warn" : "safe"}`),
        (exit) => exit.remove()
      );

    nodeLayer.selectAll("g.node").select("circle").attr("r", (d) => nodeRadius(d));
    nodeLayer.selectAll("g.node").select(".node-iid").text((d) => `!${d.iid}`);
    nodeLayer.selectAll("g.node").select(".node-label")
      .attr("y", (d) => nodeRadius(d) + 15)
      .text((d) => truncate(projectShort(d.project), 18));

    simulation.nodes(nodes);
    simulation.force("link").links(edges);
    simulation.alpha(0.7).restart();

    applySelectionStyles();
  }

  function onTick() {
    linkLayer.selectAll("path.link").attr("d", linkArc);
    labelLayer.selectAll("g.link-label-group").attr("transform", labelTransform);
    labelLayer.selectAll("g.link-label-group").each(function (d) {
      const t = d3.select(this).select("text");
      const txt = t.node();
      if (txt) {
        const w = txt.getComputedTextLength() + 12;
        d3.select(this).select("rect").attr("x", -w / 2).attr("y", -8).attr("width", w);
      }
    });
    nodeLayer.selectAll("g.node").attr("transform", (d) => `translate(${d.x},${d.y})`);
  }

  // ── Geometry helpers ───────────────────────────────────────────────────────
  function linkArc(d) {
    const s = d.source, t = d.target;
    if (s.x == null || t.x == null) return "M0,0";
    const dx = t.x - s.x, dy = t.y - s.y;
    const dr = Math.sqrt(dx * dx + dy * dy) * 1.9 || 1;
    return `M${s.x},${s.y}A${dr},${dr} 0 0,1 ${t.x},${t.y}`;
  }
  function labelTransform(d) {
    const s = d.source, t = d.target;
    if (s.x == null) return "translate(0,0)";
    const mx = (s.x + t.x) / 2, my = (s.y + t.y) / 2;
    const dx = t.x - s.x, dy = t.y - s.y;
    const len = Math.sqrt(dx * dx + dy * dy) || 1;
    const off = Math.min(len * 0.12, 26);
    return `translate(${mx + (-dy / len) * off},${my + (dx / len) * off})`;
  }
  function nodeRadius(d) { return 20 + Math.min(d.caller_count || 0, 24) * 0.7; }
  function linkWidth(d)   { return 1.5 + (SEV_RANK[d.severity_label] || 1) * 1.1; }

  function isColliding(id) {
    return (currentData.edges || []).some((e) => edgeHas(e, id));
  }
  function edgeHas(e, id) {
    const s = typeof e.source === "object" ? e.source.id : e.source;
    const t = typeof e.target === "object" ? e.target.id : e.target;
    return s === id || t === id;
  }

  // ── Hover highlight ──────────────────────────────────────────────────────
  function hoverHighlight(id, on) {
    if (selected) return;
    const neighbors = new Set([id]);
    (currentData.edges || []).forEach((e) => {
      const s = typeof e.source === "object" ? e.source.id : e.source;
      const t = typeof e.target === "object" ? e.target.id : e.target;
      if (s === id) neighbors.add(t);
      if (t === id) neighbors.add(s);
    });
    nodeLayer.selectAll("g.node").classed("dimmed", (d) => on && !neighbors.has(d.id));
    linkLayer.selectAll("path.link").classed("dimmed", (d) => on && !edgeHas(d, id));
  }

  // ── Selection ────────────────────────────────────────────────────────────
  function applySelectionStyles() {
    nodeLayer.selectAll("g.node").classed("selected", (d) => selected?.type === "node" && selected.id === d.id);
    linkLayer.selectAll("path.link").classed("selected", (d) => selected?.type === "edge" && selected.id === edgeKey(d));
  }
  function edgeKey(d) {
    const s = typeof d.source === "object" ? d.source.id : d.source;
    const t = typeof d.target === "object" ? d.target.id : d.target;
    return `${s}::${t}::${d.symbol}`;
  }
  function clearSelection() {
    selected = null;
    if (HAS_D3) {
      nodeLayer.selectAll("g.node").classed("selected", false).classed("dimmed", false);
      linkLayer.selectAll("path.link").classed("selected", false).classed("dimmed", false);
    }
    // Clear row selection in list
    document.querySelectorAll(".collision-row").forEach((r) => r.classList.remove("selected"));
    listSelectedIdx = -1;
    showPlaceholder();
  }

  // ── Click handlers ─────────────────────────────────────────────────────────
  async function onNodeClick(d) {
    selected = { type: "node", id: d.id };
    if (HAS_D3) {
      applySelectionStyles();
      hoverHighlight(d.id, false);
      const neighbors = new Set([d.id]);
      (currentData.edges || []).forEach((e) => {
        const s = typeof e.source === "object" ? e.source.id : e.source;
        const t = typeof e.target === "object" ? e.target.id : e.target;
        if (s === d.id) neighbors.add(t);
        if (t === d.id) neighbors.add(s);
      });
      nodeLayer.selectAll("g.node").classed("dimmed", (n) => !neighbors.has(n.id));
      linkLayer.selectAll("path.link").classed("dimmed", (e) => !edgeHas(e, d.id));
    }

    try {
      const resp = await fetch(`/api/collisions/${d.id}`);
      if (!resp.ok) throw new Error();
      renderNodeDetail(await resp.json(), d);
    } catch {
      renderError("Could not load collision details for this MR.");
    }
  }

  async function onEdgeClick(d) {
    selected = { type: "edge", id: edgeKey(d) };
    if (HAS_D3) {
      applySelectionStyles();
      linkLayer.selectAll("path.link").classed("dimmed", (e) => edgeKey(e) !== selected.id);
      nodeLayer.selectAll("g.node").classed("dimmed", (n) => !edgeHas(d, n.id));
    }

    const sId = typeof d.source === "object" ? d.source.id : d.source;
    const tId = typeof d.target === "object" ? d.target.id : d.target;

    try {
      const resp = await fetch(`/api/collisions/${sId}`);
      if (!resp.ok) throw new Error();
      const info = await resp.json();
      const collision = (info.collisions || []).find((c) => c.other_mr_id === tId && c.symbol === d.symbol)
        || (info.collisions || []).find((c) => c.symbol === d.symbol);
      renderEdgeDetail(info, collision, d);
    } catch {
      renderError("Could not load collision details.");
    }
  }

  // ── Detail panel ─────────────────────────────────────────────────────────
  function showPlaceholder() {
    document.getElementById("detail-placeholder").classList.remove("hidden");
    document.getElementById("detail-content").classList.add("hidden");
  }
  function showContent(html) {
    document.getElementById("detail-placeholder").classList.add("hidden");
    const c = document.getElementById("detail-content");
    c.classList.remove("hidden");
    c.innerHTML = html;
  }
  function renderError(msg) {
    showContent(`<div class="detail-pad"><div class="advice" style="color:var(--critical);background:var(--critical-bg);border-color:#fecdca">${esc(msg)}</div></div>`);
  }

  function renderNodeDetail(info, node) {
    const collisions = info.collisions || [];
    let html = `
      <div class="detail-hero">
        <div class="eyebrow">Merge Request</div>
        <div class="detail-symbol">!${info.mr_iid} · ${esc(truncate(info.mr_title || node.title || "", 48))}</div>
        <div style="margin-top:8px;display:flex;gap:14px;flex-wrap:wrap;font-size:12px;color:var(--text-muted)">
          <span>📁 ${esc(info.project || node.project || "")}</span>
          ${info.author ? `<span>👤 ${esc(info.author)}</span>` : ""}
        </div>
        <div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap">
          <span class="owner-chip" style="padding-left:11px">⚡ ${info.changed_symbols || 0} changed symbols</span>
          <span class="owner-chip" style="padding-left:11px">📡 ${info.downstream_callers || 0} callers</span>
        </div>
      </div>`;

    if (collisions.length === 0) {
      html += `
        <div class="section">
          <div class="empty-inline">✅ No collisions — safe to merge.</div>
        </div>`;
    } else {
      html += `<div class="section"><div class="section-title">Collisions (${collisions.length})</div>`;
      collisions.forEach((c) => {
        html += `
          <div class="mr-card" style="cursor:default;margin-bottom:10px">
            <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px;margin-bottom:8px">
              <div class="detail-symbol" style="font-size:13.5px">⚡ ${esc(c.symbol)}</div>
              ${badge(c.severity_label)}
            </div>
            <div class="mr-iid">collides with</div>
            <div class="mr-title">${c.other_mr_url
              ? `<a href="${esc(c.other_mr_url)}" target="_blank" rel="noopener">!${c.other_mr_iid} ${esc(truncate(c.other_mr_title || "", 36))}</a>`
              : `!${c.other_mr_iid} ${esc(truncate(c.other_mr_title || "", 36))}`}</div>
            <div class="mr-project">${esc(c.other_mr_project || "")}</div>
            ${c.suggested_order ? `<div class="advice" style="margin-top:10px"><span class="advice-icon">🧭</span><span>${esc(c.suggested_order)}</span></div>` : ""}
          </div>`;
      });
      html += `</div>`;
    }
    showContent(html);
  }

  function renderEdgeDetail(info, c, edge) {
    _lastEdge = edge;  // store for dismiss re-render
    if (!c) { renderError("Collision detail unavailable."); return; }
    const callers = c.affected_callers || [];
    const owners  = c.affected_owners  || [];

    const conf = Math.round((c.confidence ?? 1) * 100);
    const savings = c.savings || null;
    const copyText = `Collision: ${c.symbol} (${c.severity_label}) — !${info.mr_iid} ↔ !${c.other_mr_iid}\n${c.explanation || ""}`.trim();
    let html = `
      <div class="detail-hero">
        <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:8px">
          <div>
            <div class="eyebrow">Semantic Collision</div>
            <div class="detail-symbol">⚡ ${esc(c.symbol)} ${badge(c.severity_label)}</div>
          </div>
          <button class="copy-btn" id="copy-btn" data-text="${esc(copyText)}" title="Copy summary">⎘</button>
        </div>
        <div style="margin-top:8px;font-size:12px;color:var(--text-muted)">
          Severity score: <strong style="color:${SEV_COLOR[c.severity_label]}">${Math.round((c.severity || 0) * 100)}%</strong>
        </div>
        <div class="confidence-meter">
          <span class="confidence-track"><span class="confidence-fill" style="width:${conf}%"></span></span>
          <span class="confidence-val">${conf}% confidence</span>
        </div>
      </div>`;

    if (c.explanation) {
      html += `<div class="section"><div class="section-title">Why this is dangerous</div><div class="action-text">${mdCode(c.explanation)}</div></div>`;
    }

    if (savings) {
      html += `
        <div class="section">
          <div class="section-title">Estimated impact prevented</div>
          <div style="display:flex;gap:16px;flex-wrap:wrap">
            <div><div style="font-size:20px;font-weight:700;color:#0a7c66">${compactMoney(savings.dollars, currentAnalytics?.cost_saved?.currency)}</div><div style="font-size:11.5px;color:var(--text-muted)">cost avoided</div></div>
            <div><div style="font-size:20px;font-weight:700;color:var(--accent)">${Math.round(savings.hours)}h</div><div style="font-size:11.5px;color:var(--text-muted)">eng-time avoided</div></div>
          </div>
        </div>`;
    }

    html += `
      <div class="section">
        <div class="section-title">What's colliding</div>
        <div class="mr-card">
          <div class="mr-iid">!${info.mr_iid} — this MR</div>
          <div class="mr-title">${esc(truncate(info.mr_title || "", 40))}</div>
        </div>
        <div class="vs-divider">collides with</div>
        <div class="mr-card">
          <div class="mr-iid">!${c.other_mr_iid}</div>
          <div class="mr-title">${c.other_mr_url
            ? `<a href="${esc(c.other_mr_url)}" target="_blank" rel="noopener">${esc(truncate(c.other_mr_title || "", 40))}</a>`
            : esc(truncate(c.other_mr_title || "", 40))}</div>
          <div class="mr-project">${esc(c.other_mr_project || "")}</div>
        </div>
        <div style="margin-top:12px">
          <div class="action-row"><span class="action-tag">!${info.mr_iid}</span><span class="action-text">${mdCode(c.this_action || "")}</span></div>
          <div class="action-row"><span class="action-tag">!${c.other_mr_iid}</span><span class="action-text">${mdCode(c.other_action || "")}</span></div>
        </div>
      </div>`;

    if (owners.length) {
      html += `<div class="section"><div class="section-title">Owners to notify</div><div class="owner-chips">`;
      owners.forEach((o) => {
        html += `<span class="owner-chip"><span class="caller-avatar">${esc(initials(o))}</span>@${esc(o)}</span>`;
      });
      html += `</div></div>`;
    }

    if (callers.length) {
      html += `<div class="section"><div class="section-title">Affected callers (${callers.length})</div><ul class="caller-list">`;
      callers.slice(0, 8).forEach((ca) => {
        html += `
          <li>
            <span class="caller-avatar">${esc(initials(ca.owner || ca.function_name))}</span>
            <span class="caller-info">
              <span class="caller-fn">${esc(ca.function_name)}()</span>
              <span class="caller-loc">${esc(ca.file_path)} · ${esc(projectShort(ca.project_path))}</span>
            </span>
            ${ca.owner ? `<span class="caller-owner">@${esc(ca.owner)}</span>` : ""}
          </li>`;
      });
      if (callers.length > 8) {
        html += `<li style="padding:9px 0;font-size:12px;color:var(--text-muted)">+ ${callers.length - 8} more caller${callers.length - 8 > 1 ? "s" : ""}…</li>`;
      }
      html += `</ul></div>`;
    }

    if (c.suggested_order) {
      html += `<div class="section"><div class="section-title">Suggested merge order</div><div class="advice"><span class="advice-icon">🧭</span><span>${esc(c.suggested_order)}</span></div></div>`;
    }

    // Auto-fix preview (works against the live registry / local source mirror)
    html += `
      <div class="section">
        <div class="section-title">Auto-fix</div>
        <button class="autofix-btn" id="autofix-btn" data-mr="${info.mr_id}" data-symbol="${esc(c.symbol)}">
          🔧 Preview consumer-side fix
        </button>
        <div class="autofix-result" id="autofix-result"></div>
      </div>`;

    // Dismiss / acknowledge (only shown when we have a tracked event_key)
    const dismissed = c.dismissed || false;
    const evKey = c.event_key || "";
    if (evKey) {
      html += `
        <div class="section">
          <div class="section-title">Acknowledgement</div>
          <button class="dismiss-btn ${dismissed ? "undismiss" : ""}" id="dismiss-btn"
                  data-key="${esc(evKey)}" data-dismissed="${dismissed}">
            ${dismissed ? "↩ Reopen collision" : "✓ Mark as reviewed"}
          </button>
          ${dismissed ? `<div class="dismiss-note">This collision has been acknowledged — it won't block the merge gate while dismissed.</div>` : ""}
        </div>`;
    }

    showContent(html);

    // Copy button
    const copyBtn = document.getElementById("copy-btn");
    if (copyBtn) {
      copyBtn.onclick = async () => {
        try {
          await navigator.clipboard.writeText(copyBtn.dataset.text);
          copyBtn.textContent = "✓";
          setTimeout(() => { copyBtn.textContent = "⎘"; }, 1500);
        } catch { /* clipboard unavailable */ }
      };
    }

    const btn = document.getElementById("autofix-btn");
    if (btn) btn.onclick = () => previewAutofix(btn.dataset.mr, btn.dataset.symbol, btn);

    const dismissBtn = document.getElementById("dismiss-btn");
    if (dismissBtn) {
      dismissBtn.onclick = async () => {
        const key = dismissBtn.dataset.key;
        const wasDismissed = dismissBtn.dataset.dismissed === "true";
        const endpoint = wasDismissed ? "/api/undismiss" : "/api/dismiss";
        try {
          const resp = await fetch(endpoint, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ event_key: key }),
          });
          if (!resp.ok) throw new Error("API " + resp.status);
          // Re-fetch and re-render with updated state
          await fetchAndRender();
          const sId = typeof edge.source === "object" ? edge.source.id : edge.source;
          const tId = typeof edge.target === "object" ? edge.target.id : edge.target;
          const updatedResp = await fetch(`/api/collisions/${sId}`);
          if (updatedResp.ok) {
            const updatedInfo = await updatedResp.json();
            const updatedCollision = (updatedInfo.collisions || []).find(
              (col) => col.event_key === key
            ) || c;
            renderEdgeDetail(updatedInfo, updatedCollision, _lastEdge || edge);
          }
        } catch (err) {
          console.error("Dismiss failed:", err);
        }
      };
    }
  }

  // Store `edge` reference in renderEdgeDetail closure
  let _lastEdge = null;

  async function previewAutofix(mrId, symbol, btn) {
    const out = document.getElementById("autofix-result");
    btn.disabled = true;
    btn.textContent = "⏳ Generating patch…";
    try {
      const resp = await fetch(`/api/autofix/${mrId}?symbol=${encodeURIComponent(symbol)}`, { method: "POST" });
      if (!resp.ok) throw new Error("API " + resp.status);
      const data = await resp.json();
      const plan = data.plan || {};
      btn.disabled = false;
      btn.textContent = "🔧 Preview consumer-side fix";

      if (!plan.fixable) {
        const reasons = (plan.unfixable || []).map((u) => `<li>${esc(u)}</li>`).join("");
        out.innerHTML = `<div class="advice" style="margin-top:0"><span class="advice-icon">ℹ️</span><span>No automatic patch could be generated.${reasons ? `<ul style="margin:6px 0 0 16px">${reasons}</ul>` : ""}</span></div>`;
        return;
      }

      const applied = data.applied
        ? `<div class="patch-badge" style="margin-bottom:8px;display:inline-block">✅ Draft MR(s) opened</div>`
        : `<div class="pf-meta" style="margin-bottom:8px">Dry-run preview — set <code>MERGEGUARD_AUTOFIX_ENABLED=true</code> to open the draft MR automatically.</div>`;

      const files = (plan.patches || []).map((p) => `
        <div class="patch-file">
          <div style="display:flex;justify-content:space-between;gap:8px;align-items:center">
            <span class="pf-path">${esc(projectShort(p.project_path))}/${esc(p.file_path)}</span>
            <span class="patch-badge">${p.edits} edit${p.edits > 1 ? "s" : ""}</span>
          </div>
          <div class="pf-meta">${esc(p.project_path)}</div>
        </div>`).join("");

      out.innerHTML = `
        ${applied}
        <div class="pf-meta" style="margin-bottom:8px">Branch <code>${esc(plan.branch_name)}</code> · ${plan.total_edits} call site(s) across ${(plan.patches || []).length} file(s), each AST-validated.</div>
        ${files}`;
    } catch (err) {
      btn.disabled = false;
      btn.textContent = "🔧 Preview consumer-side fix";
      out.innerHTML = `<div class="advice" style="color:var(--critical);background:var(--critical-bg);border-color:#fecdca;margin-top:0">Could not generate the fix preview.</div>`;
    }
  }

  // ── Timeline view ─────────────────────────────────────────────────────────
  function renderTimeline(events) {
    const el = document.getElementById("timeline-view");
    if (!el) return;

    if (!events || !events.length) {
      el.innerHTML = emptyBlock("🕐", "No history yet",
        "Collision events will appear here as they are detected and resolved.");
      return;
    }

    const statuses = { active: 0, dismissed: 0, resolved: 0 };
    events.forEach((e) => { if (statuses.hasOwnProperty(e.status)) statuses[e.status]++; });

    let html = `
      <div class="tl-summary">
        <span class="tl-stat"><span class="tl-dot active"></span>${statuses.active} active</span>
        <span class="tl-stat"><span class="tl-dot dismissed"></span>${statuses.dismissed} acknowledged</span>
        <span class="tl-stat"><span class="tl-dot resolved"></span>${statuses.resolved} resolved</span>
      </div>
      <div class="tl-list">`;

    events.forEach((ev) => {
      const when = relTime(ev.last_seen);
      const firstWhen = relTime(ev.first_seen);
      const ownerStr = (ev.owners || []).slice(0, 3).map((o) => `@${o}`).join(", ");
      const canInspect = ev.status === "active" && ev.mr_a_id;
      html += `
        <div class="tl-item status-${ev.status}${canInspect ? " tl-clickable" : ""}"
             data-mr-a="${ev.mr_a_id || ""}" data-symbol="${esc(ev.symbol)}" tabindex="${canInspect ? 0 : -1}"
             role="${canInspect ? "button" : ""}" aria-label="${canInspect ? `Inspect collision on ${esc(ev.symbol)}` : ""}">
          <div class="tl-marker"><span class="tl-dot ${ev.status}"></span></div>
          <div class="tl-body">
            <div class="tl-top">
              <span class="tl-symbol">⚡ ${esc(ev.symbol)}</span>
              ${badge(ev.severity_label)}
              ${ev.status === "dismissed" ? `<span class="tl-dismissed-tag">acknowledged</span>` : ""}
              ${ev.status === "resolved" ? `<span class="tl-resolved-tag">resolved</span>` : ""}
              ${canInspect ? `<span class="tl-inspect-hint">click to inspect →</span>` : ""}
            </div>
            <div class="tl-mrs">
              <span class="mr-ref">!${ev.mr_a_iid}</span>
              <span class="tl-sep">${esc(ev.project_a?.split("/").pop() || "")}</span>
              <span class="tl-arrow">↔</span>
              <span class="mr-ref">!${ev.mr_b_iid}</span>
              <span class="tl-sep">${esc(ev.project_b?.split("/").pop() || "")}</span>
            </div>
            <div class="tl-meta">
              ${ev.caller_count ? `${ev.caller_count} caller${ev.caller_count !== 1 ? "s" : ""} at risk` : ""}
              ${ownerStr ? ` · ${esc(ownerStr)}` : ""}
            </div>
            <div class="tl-times">
              First seen ${esc(firstWhen)} · last seen ${esc(when)}
              ${ev.resolved_at ? ` · resolved ${esc(relTime(ev.resolved_at))}` : ""}
            </div>
          </div>
        </div>`;
    });

    html += `</div>`;
    el.innerHTML = html;

    // Wire up clickable timeline items to navigate to collision detail
    el.querySelectorAll(".tl-clickable").forEach((item) => {
      const mrAId = Number(item.dataset.mrA);
      const symbol = item.dataset.symbol;
      const handler = async () => {
        if (!mrAId) return;
        try {
          const resp = await fetch(`/api/collisions/${mrAId}`);
          if (!resp.ok) return;
          const info = await resp.json();
          const collision = (info.collisions || []).find((c) => c.symbol === symbol) || null;
          if (collision) {
            switchView("list");
            // Show detail in side panel
            renderEdgeDetail(info, collision, {
              source: { id: mrAId },
              target: { id: collision.other_mr_id },
              symbol,
            });
          }
        } catch { /* silent */ }
      };
      item.addEventListener("click", handler);
      item.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); handler(); } });
    });
  }

  // ── List view ──────────────────────────────────────────────────────────────
  function switchView(view) {
    if (view === "graph" && !HAS_D3) return;
    currentView = view;
    listSelectedIdx = -1;
    document.querySelectorAll("#view-toggle button").forEach((b) => b.classList.toggle("active", b.dataset.view === view));

    const views = {
      graph: document.getElementById("graph-view"),
      list: document.getElementById("list-view"),
      plan: document.getElementById("plan-view"),
      timeline: document.getElementById("timeline-view"),
      insights: document.getElementById("insights-view"),
    };
    const toolbar = document.getElementById("list-toolbar");

    Object.entries(views).forEach(([name, el]) => {
      if (el) el.classList.toggle("hidden", name !== view);
    });
    toolbar.classList.toggle("hidden", view !== "list");

    if (view === "graph") {
      sizeSvg();
      if (simulation) simulation.alpha(0.3).restart();
    } else if (view === "list") {
      renderList(currentData);
    } else if (view === "plan") {
      renderPlan(currentPlan);
    } else if (view === "timeline") {
      renderTimeline(currentTimeline);
    } else if (view === "insights") {
      renderInsights(currentAnalytics);
    }
  }

  function renderList(data) {
    const listView = document.getElementById("list-view");
    const nodeById = new Map((data.nodes || []).map((n) => [n.id, n]));
    let edges = (data.edges || []).slice().sort((a, b) => (b.severity || 0) - (a.severity || 0));

    // Apply severity filter
    if (filterSev !== "all") {
      edges = edges.filter((e) => e.severity_label === filterSev);
    }

    // Apply search filter
    if (searchQuery) {
      edges = edges.filter((e) => {
        const s = nodeById.get(typeof e.source === "object" ? e.source.id : e.source) || {};
        const t = nodeById.get(typeof e.target === "object" ? e.target.id : e.target) || {};
        return (
          (e.symbol || "").toLowerCase().includes(searchQuery) ||
          (s.title   || "").toLowerCase().includes(searchQuery) ||
          (t.title   || "").toLowerCase().includes(searchQuery) ||
          (s.project || "").toLowerCase().includes(searchQuery) ||
          (t.project || "").toLowerCase().includes(searchQuery)
        );
      });
    }

    if (!edges.length) {
      const noCollisions = (data.edges || []).length === 0;
      listView.innerHTML = noCollisions
        ? `<div class="list-empty">
            <div class="empty-icon">✅</div>
            <div class="empty-title">No semantic collisions</div>
            <div class="empty-sub">All open merge requests have non-overlapping blast radii. You're clear to merge.</div>
           </div>`
        : `<div class="list-empty">
            <div class="empty-icon">🔍</div>
            <div class="empty-title">No matches</div>
            <div class="empty-sub">No collisions match your current filter. Try adjusting the search or selecting a different severity.</div>
           </div>`;
      return;
    }

    listView.innerHTML = edges.map((e, i) => {
      const s = nodeById.get(typeof e.source === "object" ? e.source.id : e.source) || {};
      const t = nodeById.get(typeof e.target === "object" ? e.target.id : e.target) || {};
      return `
        <div class="collision-row" data-idx="${i}" data-source="${s.id}" data-target="${t.id}" data-symbol="${esc(e.symbol)}" tabindex="0" role="button" aria-label="Collision on ${esc(e.symbol)} between !${s.iid} and !${t.iid}">
          <span class="row-sev ${e.severity_label}"></span>
          <span class="row-main">
            <span class="row-symbol">⚡ ${esc(e.symbol)}</span>
            <span class="row-pair"><span class="mr-ref">!${s.iid}</span> ${esc(projectShort(s.project))} ↔ <span class="mr-ref">!${t.iid}</span> ${esc(projectShort(t.project))}</span>
            <span class="row-meta">${esc(truncate(s.title || "", 38))}</span>
          </span>
          ${badge(e.severity_label)}
        </div>`;
    }).join("");

    listView.querySelectorAll(".collision-row").forEach((row) => {
      row.onclick = () => {
        listSelectedIdx = parseInt(row.dataset.idx, 10);
        listView.querySelectorAll(".collision-row").forEach((r) => r.classList.remove("selected"));
        row.classList.add("selected");
        const edge = {
          source: Number(row.dataset.source),
          target: Number(row.dataset.target),
          symbol: row.dataset.symbol,
        };
        const full = (currentData.edges || []).find(
          (e) => (typeof e.source === "object" ? e.source.id : e.source) === edge.source && e.symbol === edge.symbol
        ) || edge;
        onEdgeClick(full);
      };
      row.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); row.click(); } };
    });
  }

  // ── Plan view ───────────────────────────────────────────────────────────────
  function renderPlan(plan) {
    const el = document.getElementById("plan-view");
    if (!el) return;
    if (!plan || (!plan.steps?.length && !plan.cycles?.length)) {
      el.innerHTML = emptyBlock("🗺️", "No sequencing needed",
        "There are no cross-MR collisions, so every open MR is safe to merge independently.");
      return;
    }

    let html = `<div class="plan-intro">🧭 Optimal global merge order — ${plan.total_ordered} sequenced${plan.has_cycles ? `, ${plan.cycles.length} need coordination` : ""}.</div>`;

    plan.steps.forEach((s, i) => {
      html += `
        <div class="plan-step">
          <span class="step-num">${s.order}</span>
          <div class="step-body">
            <div class="step-title"><span class="mr-ref">!${s.mr_iid}</span> <span class="step-project">${esc(projectShort(s.project))}</span></div>
            <div class="step-reason">${mdCode(s.reason)}</div>
          </div>
        </div>`;
      if (i < plan.steps.length - 1) html += `<div class="plan-connector"></div>`;
    });

    (plan.cycles || []).forEach((g) => {
      const members = g.members.map((m) => `<span class="badge high"><span class="badge-dot"></span>!${m.mr_iid}</span>`).join("");
      html += `
        <div class="coord-group">
          <div class="coord-head">⚠️ Coordinate together (${g.members.length} MRs)</div>
          <div class="coord-members">${members}</div>
          <div class="coord-reason">${mdCode(g.reason)}</div>
        </div>`;
    });

    el.innerHTML = html;
  }

  // ── Insights view ─────────────────────────────────────────────────────────
  function renderInsights(a) {
    const el = document.getElementById("insights-view");
    if (!el) return;
    if (!a) {
      el.innerHTML = emptyBlock("📊", "No analytics yet",
        "Once collisions are detected they're tracked here with cost, hotspots, and owner load.");
      return;
    }
    const cs = a.cost_saved || {};
    const totals = a.totals || {};
    const hrs = Math.round(cs.engineer_hours || 0);

    let html = `
      <div class="ask-box">
        <div class="insight-title">Ask MergeGuard</div>
        <div class="ask-input-row">
          <input id="ask-input" class="search-input" type="text" autocomplete="off"
                 placeholder="e.g. What does !23 break? · Which MRs are safe to merge?"
                 value="${esc(lastAskQuestion)}" />
          <button id="ask-send" class="autofix-btn" style="width:auto;margin-top:0;white-space:nowrap">Ask</button>
        </div>
        <div class="ask-suggestions">
          <button class="ask-chip" data-q="Which of my open MRs are safe to merge today?">Safe to merge?</button>
          <button class="ask-chip" data-q="What's the riskiest collision right now?">Riskiest collision</button>
          <button class="ask-chip" data-q="What's the merge order?">Merge order</button>
          <button class="ask-chip" data-q="Which files are the hotspots?">Hotspot files</button>
          <button class="ask-chip" data-q="Who is most affected by current collisions?">Owner load</button>
          <button class="ask-chip" data-q="How much has MergeGuard saved us?">Savings</button>
        </div>
        <div class="ask-answer ${lastAskAnswer ? "" : "hidden"}" id="ask-answer">${lastAskAnswer}</div>
      </div>

      <div class="cost-hero">
        <div class="cost-amount">${compactMoney(cs.dollars || 0, cs.currency)}</div>
        <div class="cost-label">estimated cost of incidents prevented before merge</div>
        <div class="cost-sub"><strong>${hrs}</strong> engineer-hours · <strong>${totals.total_collisions || 0}</strong> collisions caught · <strong>${totals.resolved || 0}</strong> resolved</div>
      </div>`;

    // Severity breakdown chips
    const bySev = totals.by_severity || {};
    const sevColors = { critical: "var(--critical)", high: "var(--high)", medium: "var(--medium)", low: "var(--low)" };
    const sevBg = { critical: "var(--critical-bg)", high: "var(--high-bg)", medium: "var(--medium-bg)", low: "var(--low-bg)" };
    const chips = ["critical", "high", "medium", "low"]
      .filter((s) => bySev[s])
      .map((s) => `<span class="sev-chip" style="color:${sevColors[s]};background:${sevBg[s]}">${cap(s)} · ${bySev[s]}</span>`)
      .join("");
    if (chips) html += `<div class="insight-block"><div class="insight-title">By severity</div><div class="sev-chips">${chips}</div></div>`;

    html += barBlock("Hotspot services", a.hotspot_projects, projectShort);
    html += barBlock("Hotspot files", a.hotspot_files, (s) => s.split("/").pop());
    html += barBlock("Owner load (reviewers pulled in)", a.owner_load, (s) => "@" + s);

    if (a.avg_resolution_hours != null) {
      html += `<div class="insight-block"><div class="insight-title">Avg resolution time</div><div style="font-size:13px;color:var(--text-soft)">${a.avg_resolution_hours} hours from first detection to merge/close</div></div>`;
    }

    el.innerHTML = html;
    wireAsk();
  }

  function wireAsk() {
    const input = document.getElementById("ask-input");
    const send = document.getElementById("ask-send");
    if (!input || !send) return;
    const run = () => askMergeGuard(input.value);
    send.onclick = run;
    input.onkeydown = (e) => { if (e.key === "Enter") run(); };
    document.querySelectorAll(".ask-chip").forEach((chip) => {
      chip.onclick = () => { input.value = chip.dataset.q; askMergeGuard(chip.dataset.q); };
    });
  }

  async function askMergeGuard(question) {
    question = (question || "").trim();
    if (!question) return;
    lastAskQuestion = question;
    const out = document.getElementById("ask-answer");
    if (out) { out.classList.remove("hidden"); out.innerHTML = `<span class="ask-thinking">Thinking…</span>`; }
    try {
      const resp = await fetch("/api/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question }),
      });
      if (!resp.ok) throw new Error("API " + resp.status);
      const data = await resp.json();
      lastAskAnswer = `<div class="ask-q">${esc(question)}</div><div class="ask-a">${mdCode(data.answer || "")}</div>`;
    } catch {
      lastAskAnswer = `<div class="ask-a" style="color:var(--critical)">Sorry — couldn't reach the collision graph.</div>`;
    }
    if (out) out.innerHTML = lastAskAnswer;
  }

  function barBlock(title, items, labelFn) {
    if (!items || !items.length) return "";
    const max = Math.max(...items.map((i) => i.count));
    const rows = items.map((i) => {
      const pct = max > 0 ? Math.round((i.count / max) * 100) : 0;
      return `
        <div class="bar-row">
          <span class="bar-label" title="${esc(i.name)}">${esc(labelFn ? labelFn(i.name) : i.name)}</span>
          <span class="bar-track"><span class="bar-fill" style="width:${pct}%"></span></span>
          <span class="bar-count">${i.count}</span>
        </div>`;
    }).join("");
    return `<div class="insight-block"><div class="insight-title">${esc(title)}</div>${rows}</div>`;
  }

  function emptyBlock(icon, title, sub) {
    return `<div class="list-empty"><div class="empty-icon">${icon}</div><div class="empty-title">${esc(title)}</div><div class="empty-sub">${esc(sub)}</div></div>`;
  }

  // ── Fit to view ─────────────────────────────────────────────────────────────
  function fitToView() {
    const nodes = simulation.nodes();
    if (!nodes.length) return;
    const xs = nodes.map((n) => n.x), ys = nodes.map((n) => n.y);
    const minX = Math.min(...xs), maxX = Math.max(...xs);
    const minY = Math.min(...ys), maxY = Math.max(...ys);
    const pad = 80;
    const w = (maxX - minX) + pad * 2, h = (maxY - minY) + pad * 2;
    const scale = Math.min(width / w, height / h, 1.4);
    const tx = width  / 2 - scale * (minX + maxX) / 2;
    const ty = height / 2 - scale * (minY + maxY) / 2;
    svg.transition().duration(450).call(zoom.transform, d3.zoomIdentity.translate(tx, ty).scale(scale));
  }

  // ── Drag ───────────────────────────────────────────────────────────────────
  function dragStart(e, d) { if (!e.active) simulation.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; }
  function dragged(e, d)   { d.fx = e.x; d.fy = e.y; }
  function dragEnd(e, d)   { if (!e.active) simulation.alphaTarget(0); d.fx = null; d.fy = null; }

  // ── Tooltip ──────────────────────────────────────────────────────────────
  function showTip(e, html) { tooltip.html(html).style("opacity", 1); moveTip(e); }
  function moveTip(e)       { tooltip.style("left", e.pageX + 14 + "px").style("top", e.pageY - 10 + "px"); }
  function hideTip()        { tooltip.style("opacity", 0); }

  // ── Utils ─────────────────────────────────────────────────────────────────
  function badge(label) {
    if (!label) return "";
    return `<span class="badge ${label}"><span class="badge-dot"></span>${cap(label)}</span>`;
  }
  function mdCode(s) {
    return esc(s).replace(/`([^`]+)`/g, "<code>$1</code>");
  }
  function projectShort(p) { return (p || "").split("/").pop() || p || ""; }
  function initials(s) {
    if (!s) return "?";
    const parts = String(s).replace(/[_\-]/g, " ").split(" ").filter(Boolean);
    return ((parts[0]?.[0] || "") + (parts[1]?.[0] || "")).toUpperCase() || s[0].toUpperCase();
  }
  function truncate(s, n) { s = String(s || ""); return s.length > n ? s.slice(0, n - 1) + "…" : s; }
  function cap(s) { return s ? s[0].toUpperCase() + s.slice(1) : ""; }
  function compactMoney(amount, currency) {
    const sym = currency === "USD" || !currency ? "$" : currency + " ";
    const n = Number(amount) || 0;
    if (n >= 1e6) return `${sym}${(n / 1e6).toFixed(1)}M`;
    if (n >= 1e3) return `${sym}${(n / 1e3).toFixed(0)}K`;
    return `${sym}${n.toFixed(0)}`;
  }
  function esc(s) {
    return String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  function setText(id, v) { const el = document.getElementById(id); if (el) el.textContent = v; }
  function timeNow() { return new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); }
  function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }
  function relTime(epoch) {
    if (!epoch) return "unknown";
    const diff = (Date.now() / 1000) - epoch;
    if (diff < 60) return "just now";
    if (diff < 3600) return `${Math.round(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.round(diff / 3600)}h ago`;
    return `${Math.round(diff / 86400)}d ago`;
  }

  document.addEventListener("DOMContentLoaded", init);
})();
