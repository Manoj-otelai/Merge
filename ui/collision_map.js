/* ──────────────────────────────────────────────────────────────────────────
   MergeGuard Collision Map — D3.js force-directed graph (light theme)
   ────────────────────────────────────────────────────────────────────────── */
(function () {
  "use strict";

  const POLL_INTERVAL = 30_000;
  const SEV_RANK = { low: 1, medium: 2, high: 3, critical: 4 };
  const SEV_COLOR = { low: "#12b76a", medium: "#dc9a04", high: "#e8590c", critical: "#d92d20" };
  const HAS_D3 = typeof d3 !== "undefined";

  let svg, root, linkLayer, labelLayer, nodeLayer, zoom, simulation, tooltip;
  let width = 0, height = 0;
  let currentData = { nodes: [], edges: [] };
  let selected = null;            // { type:'node'|'edge', id }
  let currentView = "graph";

  // ── Init ──────────────────────────────────────────────────────────────────
  function init() {
    // Controls that work regardless of D3 availability
    document.getElementById("refresh-btn").onclick = fetchAndRender;
    document.querySelectorAll("#view-toggle button").forEach((btn) => {
      btn.onclick = () => switchView(btn.dataset.view);
    });

    if (HAS_D3) {
      initGraph();
    } else {
      // Graceful degradation: no force graph, but stats + list still work.
      degradeToList();
    }

    fetchAndRender();
    setInterval(fetchAndRender, POLL_INTERVAL);
  }

  function initGraph() {
    svg = d3.select("#collision-graph");
    tooltip = d3.select("#tooltip");

    sizeSvg();

    zoom = d3.zoom().scaleExtent([0.25, 3]).on("zoom", (e) => root.attr("transform", e.transform));
    svg.call(zoom).on("dblclick.zoom", null);
    svg.on("click", (e) => { if (e.target === svg.node()) clearSelection(); });

    root = svg.append("g");
    linkLayer = root.append("g").attr("class", "links");
    labelLayer = root.append("g").attr("class", "link-labels");
    nodeLayer = root.append("g").attr("class", "nodes");

    simulation = d3.forceSimulation()
      .force("link", d3.forceLink().id((d) => d.id).distance(190).strength(0.45))
      .force("charge", d3.forceManyBody().strength(-680))
      .force("center", d3.forceCenter(width / 2, height / 2))
      .force("x", d3.forceX(width / 2).strength(0.06))
      .force("y", d3.forceY(height / 2).strength(0.06))
      .force("collide", d3.forceCollide().radius((d) => nodeRadius(d) + 34))
      .on("tick", onTick);

    document.getElementById("zoom-in").onclick = () => svg.transition().duration(220).call(zoom.scaleBy, 1.3);
    document.getElementById("zoom-out").onclick = () => svg.transition().duration(220).call(zoom.scaleBy, 1 / 1.3);
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
    document.getElementById("loading-state").classList.add("hidden");
  }

  function sizeSvg() {
    const container = document.getElementById("graph-view");
    width = container.clientWidth || 800;
    height = container.clientHeight || 520;
    svg.attr("viewBox", `0 0 ${width} ${height}`);
  }

  // ── Data fetch ──────────────────────────────────────────────────────────
  async function fetchAndRender() {
    try {
      const resp = await fetch("/api/collision-map");
      if (!resp.ok) throw new Error("API " + resp.status);
      const data = await resp.json();
      currentData = data;
      updateStats(data);
      if (HAS_D3) render(data);
      if (!HAS_D3 || currentView === "list") renderList(data);
    } catch (err) {
      console.error("MergeGuard fetch error:", err);
    } finally {
      document.getElementById("loading-state").classList.add("hidden");
      document.getElementById("last-updated").textContent = timeNow();
    }
  }

  // ── Stats ─────────────────────────────────────────────────────────────────
  function updateStats(data) {
    const mrs = data.open_mrs ?? 0;
    const collisions = data.total_collisions ?? 0;
    setText("stat-mrs", mrs);
    setText("stat-collisions", collisions);

    let topSev = null;
    (data.edges || []).forEach((e) => {
      if (!topSev || SEV_RANK[e.severity_label] > SEV_RANK[topSev]) topSev = e.severity_label;
    });
    const sevEl = document.getElementById("stat-severity");
    const metaEl = document.getElementById("stat-severity-meta");
    if (topSev) {
      sevEl.textContent = topSev.toUpperCase();
      sevEl.style.color = SEV_COLOR[topSev];
      metaEl.textContent = "needs coordination";
    } else {
      sevEl.textContent = "—";
      sevEl.style.color = "";
      metaEl.textContent = "no active risk";
    }

    const callers = (data.nodes || []).reduce((s, n) => s + (n.caller_count || 0), 0);
    setText("stat-callers", callers);
  }

  // ── Graph render ──────────────────────────────────────────────────────────
  function render(data) {
    const hasMrs = (data.nodes || []).length > 0;
    document.getElementById("empty-state").classList.toggle("hidden", hasMrs || currentView !== "graph");
    svg.classed("hidden", !hasMrs);
    if (!hasMrs) { linkLayer.selectAll("*").remove(); nodeLayer.selectAll("*").remove(); labelLayer.selectAll("*").remove(); return; }

    // Preserve positions across refreshes
    const posById = new Map(simulation.nodes().map((n) => [n.id, n]));
    const nodes = data.nodes.map((n) => {
      const prev = posById.get(n.id);
      return Object.assign({}, n, prev ? { x: prev.x, y: prev.y, vx: prev.vx, vy: prev.vy } : {});
    });
    const edges = data.edges.map((e) => Object.assign({}, e));

    // ── Links (curved arcs) ──
    const link = linkLayer.selectAll("path.link")
      .data(edges, (d) => `${d.source}::${d.target}::${d.symbol}`)
      .join(
        (enter) => enter.append("path")
          .attr("class", (d) => `link ${d.severity_label}`)
          .attr("stroke-width", (d) => linkWidth(d))
          .on("click", (e, d) => { e.stopPropagation(); onEdgeClick(d); })
          .on("mouseover", (e, d) => showTip(e, `<div class="tt-title">⚡ ${esc(d.symbol)}</div><div class="tt-sub">${cap(d.severity_label)} severity collision</div>`))
          .on("mousemove", moveTip)
          .on("mouseout", hideTip),
        (update) => update.attr("class", (d) => `link ${d.severity_label}`).attr("stroke-width", (d) => linkWidth(d)),
        (exit) => exit.remove()
      );

    // ── Link labels ──
    const labelSel = labelLayer.selectAll("g.link-label-group")
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
      );
    labelSel.select("text").text((d) => d.symbol);

    // ── Nodes ──
    const node = nodeLayer.selectAll("g.node")
      .data(nodes, (d) => d.id)
      .join(
        (enter) => {
          const g = enter.append("g")
            .attr("class", (d) => `node ${isColliding(d.id) ? "warn" : "safe"}`)
            .call(d3.drag().on("start", dragStart).on("drag", dragged).on("end", dragEnd))
            .on("click", (e, d) => { e.stopPropagation(); onNodeClick(d); })
            .on("mouseover", (e, d) => {
              hoverHighlight(d.id, true);
              showTip(e, `<div class="tt-title">!${d.iid} · ${esc(truncate(d.title, 40))}</div><div class="tt-sub">${esc(d.project)}</div>`);
            })
            .on("mousemove", moveTip)
            .on("mouseout", (e, d) => { hoverHighlight(d.id, false); hideTip(); });
          g.append("circle");
          g.append("text").attr("class", "node-iid").attr("dy", "0.32em");
          g.append("text").attr("class", "node-label");
          return g;
        },
        (update) => update.attr("class", (d) => `node ${isColliding(d.id) ? "warn" : "safe"}`),
        (exit) => exit.remove()
      );

    node.select("circle").attr("r", (d) => nodeRadius(d));
    node.select(".node-iid").text((d) => `!${d.iid}`);
    node.select(".node-label")
      .attr("y", (d) => nodeRadius(d) + 15)
      .text((d) => truncate(projectShort(d.project), 18));

    simulation.nodes(nodes);
    simulation.force("link").links(edges);
    simulation.alpha(0.7).restart();

    // Re-apply selection styling
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

  // ── Geometry helpers ──────────────────────────────────────────────────────
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
  function linkWidth(d) { return 1.5 + (SEV_RANK[d.severity_label] || 1) * 1.1; }

  function isColliding(id) {
    return (currentData.edges || []).some((e) => edgeHas(e, id));
  }
  function edgeHas(e, id) {
    const s = typeof e.source === "object" ? e.source.id : e.source;
    const t = typeof e.target === "object" ? e.target.id : e.target;
    return s === id || t === id;
  }

  // ── Hover highlight ────────────────────────────────────────────────────────
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

  // ── Selection ──────────────────────────────────────────────────────────────
  function applySelectionStyles() {
    nodeLayer.selectAll("g.node").classed("selected", (d) => selected && selected.type === "node" && selected.id === d.id);
    linkLayer.selectAll("path.link").classed("selected", (d) => selected && selected.type === "edge" && selected.id === edgeKey(d));
  }
  function edgeKey(d) {
    const s = typeof d.source === "object" ? d.source.id : d.source;
    const t = typeof d.target === "object" ? d.target.id : d.target;
    return `${s}::${t}::${d.symbol}`;
  }
  function clearSelection() {
    selected = null;
    nodeLayer.selectAll("g.node").classed("selected", false).classed("dimmed", false);
    linkLayer.selectAll("path.link").classed("selected", false).classed("dimmed", false);
    showPlaceholder();
  }

  // ── Click handlers ──────────────────────────────────────────────────────────
  async function onNodeClick(d) {
    selected = { type: "node", id: d.id };
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
    applySelectionStyles();
    const sId = typeof d.source === "object" ? d.source.id : d.source;
    linkLayer.selectAll("path.link").classed("dimmed", (e) => edgeKey(e) !== selected.id);
    nodeLayer.selectAll("g.node").classed("dimmed", (n) => !edgeHas(d, n.id));

    try {
      const resp = await fetch(`/api/collisions/${sId}`);
      if (!resp.ok) throw new Error();
      const info = await resp.json();
      const tId = typeof d.target === "object" ? d.target.id : d.target;
      const collision = (info.collisions || []).find((c) => c.other_mr_id === tId && c.symbol === d.symbol)
        || (info.collisions || []).find((c) => c.symbol === d.symbol);
      renderEdgeDetail(info, collision, d);
    } catch {
      renderError("Could not load collision details.");
    }
  }

  // ── Detail panel ────────────────────────────────────────────────────────────
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
      html += `<div class="section"><div class="empty-inline">✅ No collisions — safe to merge.</div></div>`;
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
            <div class="mr-title">${c.other_mr_url ? `<a href="${esc(c.other_mr_url)}" target="_blank" rel="noopener">!${c.other_mr_iid} ${esc(truncate(c.other_mr_title || "", 36))}</a>` : `!${c.other_mr_iid} ${esc(truncate(c.other_mr_title || "", 36))}`}</div>
            <div class="mr-project">${esc(c.other_mr_project || "")}</div>
            ${c.suggested_order ? `<div class="advice" style="margin-top:10px"><span class="advice-icon">🧭</span><span>${esc(c.suggested_order)}</span></div>` : ""}
          </div>`;
      });
      html += `</div>`;
    }
    showContent(html);
  }

  function renderEdgeDetail(info, c, edge) {
    if (!c) {
      renderError("Collision detail unavailable.");
      return;
    }
    const callers = c.affected_callers || [];
    const owners = c.affected_owners || [];

    let html = `
      <div class="detail-hero">
        <div class="eyebrow">Semantic Collision</div>
        <div class="detail-symbol">⚡ ${esc(c.symbol)} ${badge(c.severity_label)}</div>
        <div style="margin-top:8px;font-size:12px;color:var(--text-muted)">
          Severity score: <strong style="color:${SEV_COLOR[c.severity_label]}">${Math.round((c.severity || 0) * 100)}%</strong>
        </div>
      </div>

      <div class="section">
        <div class="section-title">What's colliding</div>
        <div class="mr-card">
          <div class="mr-iid">!${info.mr_iid} — this MR</div>
          <div class="mr-title">${esc(truncate(info.mr_title || "", 40))}</div>
        </div>
        <div class="vs-divider">collides with</div>
        <div class="mr-card">
          <div class="mr-iid">!${c.other_mr_iid}</div>
          <div class="mr-title">${c.other_mr_url ? `<a href="${esc(c.other_mr_url)}" target="_blank" rel="noopener">${esc(truncate(c.other_mr_title || "", 40))}</a>` : esc(truncate(c.other_mr_title || "", 40))}</div>
          <div class="mr-project">${esc(c.other_mr_project || "")}</div>
        </div>
        <div style="margin-top:12px">
          <div class="action-row"><span class="action-tag">!${info.mr_iid}</span><span class="action-text">${mdCode(c.this_action || "")}</span></div>
          <div class="action-row"><span class="action-tag">!${c.other_mr_iid}</span><span class="action-text">${mdCode(c.other_action || "")}</span></div>
        </div>
      </div>`;

    if (owners.length) {
      html += `<div class="section"><div class="section-title">Owners to notify</div><div class="owner-chips">`;
      owners.forEach((o) => { html += `<span class="owner-chip"><span class="caller-avatar">${esc(initials(o))}</span>@${esc(o)}</span>`; });
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
      html += `</ul></div>`;
    }

    if (c.suggested_order) {
      html += `<div class="section"><div class="section-title">Suggested merge order</div><div class="advice"><span class="advice-icon">🧭</span><span>${esc(c.suggested_order)}</span></div></div>`;
    }
    showContent(html);
  }

  // ── List view ───────────────────────────────────────────────────────────────
  function switchView(view) {
    if (view === "graph" && !HAS_D3) return;
    currentView = view;
    document.querySelectorAll("#view-toggle button").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
    const graphView = document.getElementById("graph-view");
    const listView = document.getElementById("list-view");
    if (view === "graph") {
      graphView.classList.remove("hidden");
      listView.classList.add("hidden");
      sizeSvg();
      simulation.alpha(0.3).restart();
    } else {
      graphView.classList.add("hidden");
      listView.classList.remove("hidden");
      renderList(currentData);
    }
  }

  function renderList(data) {
    const listView = document.getElementById("list-view");
    const nodeById = new Map((data.nodes || []).map((n) => [n.id, n]));
    const edges = (data.edges || []).slice().sort((a, b) => (b.severity || 0) - (a.severity || 0));

    if (!edges.length) {
      listView.innerHTML = `
        <div class="state-overlay" style="position:relative">
          <div class="state-icon ok">✅</div>
          <div class="state-title">No semantic collisions</div>
          <div class="state-sub">All open merge requests have non-overlapping blast radii.</div>
        </div>`;
      return;
    }

    listView.innerHTML = edges.map((e) => {
      const s = nodeById.get(typeof e.source === "object" ? e.source.id : e.source) || {};
      const t = nodeById.get(typeof e.target === "object" ? e.target.id : e.target) || {};
      return `
        <div class="collision-row" data-source="${s.id}" data-target="${t.id}" data-symbol="${esc(e.symbol)}">
          <span class="row-sev ${e.severity_label}"></span>
          <span class="row-main">
            <span class="row-symbol">⚡ ${esc(e.symbol)}</span>
            <span class="row-pair"><span class="mr-ref">!${s.iid}</span> ${esc(projectShort(s.project))} ↔ <span class="mr-ref">!${t.iid}</span> ${esc(projectShort(t.project))}</span>
            <span class="row-meta">${esc(truncate(s.title || "", 36))}</span>
          </span>
          ${badge(e.severity_label)}
        </div>`;
    }).join("");

    listView.querySelectorAll(".collision-row").forEach((row) => {
      row.onclick = () => {
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
    });
  }

  // ── Fit to view ──────────────────────────────────────────────────────────────
  function fitToView() {
    const nodes = simulation.nodes();
    if (!nodes.length) return;
    const xs = nodes.map((n) => n.x), ys = nodes.map((n) => n.y);
    const minX = Math.min(...xs), maxX = Math.max(...xs);
    const minY = Math.min(...ys), maxY = Math.max(...ys);
    const pad = 80;
    const w = (maxX - minX) + pad * 2, h = (maxY - minY) + pad * 2;
    const scale = Math.min(width / w, height / h, 1.4);
    const tx = width / 2 - scale * (minX + maxX) / 2;
    const ty = height / 2 - scale * (minY + maxY) / 2;
    svg.transition().duration(450).call(zoom.transform, d3.zoomIdentity.translate(tx, ty).scale(scale));
  }

  // ── Drag ──────────────────────────────────────────────────────────────────────
  function dragStart(e, d) { if (!e.active) simulation.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; }
  function dragged(e, d) { d.fx = e.x; d.fy = e.y; }
  function dragEnd(e, d) { if (!e.active) simulation.alphaTarget(0); d.fx = null; d.fy = null; }

  // ── Tooltip ─────────────────────────────────────────────────────────────────
  function showTip(e, html) { tooltip.html(html).style("opacity", 1); moveTip(e); }
  function moveTip(e) { tooltip.style("left", e.pageX + 14 + "px").style("top", e.pageY - 10 + "px"); }
  function hideTip() { tooltip.style("opacity", 0); }

  // ── Utils ───────────────────────────────────────────────────────────────────
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
  function esc(s) {
    return String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  function setText(id, v) { const el = document.getElementById(id); if (el) el.textContent = v; }
  function timeNow() { return new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); }
  function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

  document.addEventListener("DOMContentLoaded", init);
})();
