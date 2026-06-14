/* MergeGuard Collision Map — D3.js force-directed graph */
(function () {
  "use strict";

  const POLL_INTERVAL = 30_000;

  let simulation, svg, linkGroup, nodeGroup, tooltip;
  let currentData = { nodes: [], edges: [] };
  let selectedElement = null;

  // ── Init ──────────────────────────────────────────────────────────────────
  function init() {
    const container = document.getElementById("graph-container");
    const svgEl = document.getElementById("collision-graph");

    svg = d3.select(svgEl);
    const width = container.clientWidth;
    const height = container.clientHeight;

    svg.attr("viewBox", `0 0 ${width} ${height}`);

    // Zoom & pan
    const zoom = d3.zoom()
      .scaleExtent([0.3, 4])
      .on("zoom", (e) => root.attr("transform", e.transform));
    svg.call(zoom);

    const root = svg.append("g").attr("class", "root");
    linkGroup = root.append("g").attr("class", "links");
    nodeGroup = root.append("g").attr("class", "nodes");

    tooltip = d3.select("body").append("div")
      .attr("class", "tooltip")
      .style("opacity", 0)
      .style("position", "absolute");

    simulation = d3.forceSimulation()
      .force("link", d3.forceLink().id(d => d.id).distance(160).strength(0.5))
      .force("charge", d3.forceManyBody().strength(-400))
      .force("center", d3.forceCenter(width / 2, height / 2))
      .force("collision", d3.forceCollide().radius(50));

    fetchAndRender();
    setInterval(fetchAndRender, POLL_INTERVAL);
  }

  // ── Data fetch ────────────────────────────────────────────────────────────
  async function fetchAndRender() {
    try {
      const [mapResp] = await Promise.all([
        fetch("/api/collision-map"),
      ]);
      if (!mapResp.ok) throw new Error("API error " + mapResp.status);
      const data = await mapResp.json();
      currentData = data;
      updateStats(data);
      render(data);
    } catch (err) {
      console.error("MergeGuard fetch error:", err);
    } finally {
      document.getElementById("loading-state").classList.add("hidden");
    }
  }

  function updateStats(data) {
    document.getElementById("stat-mrs").textContent = data.open_mrs ?? 0;
    document.getElementById("stat-collisions").textContent = data.total_collisions ?? 0;
    document.getElementById("stat-updated").textContent = new Date().toLocaleTimeString();
  }

  // ── Render ────────────────────────────────────────────────────────────────
  function render(data) {
    const hasCollisions = data.edges && data.edges.length > 0;
    const hasMrs = data.nodes && data.nodes.length > 0;

    document.getElementById("empty-state").classList.toggle("hidden", hasMrs);
    document.getElementById("collision-graph").classList.toggle("hidden", !hasMrs);

    if (!hasMrs) return;

    const nodes = data.nodes.map(n => ({ ...n }));
    const edges = data.edges.map(e => ({ ...e }));

    // ── Links
    const link = linkGroup
      .selectAll(".link")
      .data(edges, d => `${d.source}-${d.target}-${d.symbol}`)
      .join(
        enter => enter.append("line")
          .attr("class", d => `link ${d.severity_label}`)
          .on("click", onEdgeClick)
          .on("mouseover", (e, d) => showTooltip(e, `<b>${d.symbol}</b><br/>Severity: ${d.severity_label}`))
          .on("mousemove", moveTooltip)
          .on("mouseout", hideTooltip),
        update => update.attr("class", d => `link ${d.severity_label}`),
        exit => exit.remove()
      );

    // ── Nodes
    const node = nodeGroup
      .selectAll(".node")
      .data(nodes, d => d.id)
      .join(
        enter => {
          const g = enter.append("g")
            .attr("class", "node")
            .call(d3.drag()
              .on("start", dragStart)
              .on("drag", dragged)
              .on("end", dragEnd))
            .on("click", onNodeClick)
            .on("mouseover", (e, d) => showTooltip(e, `<b>!${d.iid}</b> ${escapeHtml(d.title)}<br/><span style="color:#8b949e">${d.project}</span>`))
            .on("mousemove", moveTooltip)
            .on("mouseout", hideTooltip);

          g.append("circle");
          g.append("text").attr("dy", "0.35em").attr("y", d => nodeRadius(d) + 14);
          return g;
        },
        update => update,
        exit => exit.remove()
      );

    node.select("circle")
      .attr("r", nodeRadius)
      .attr("fill", nodeColor);

    node.select("text")
      .text(d => `!${d.iid}`)
      .attr("y", d => nodeRadius(d) + 14);

    // ── Simulation
    simulation.nodes(nodes).on("tick", () => {
      link
        .attr("x1", d => d.source.x)
        .attr("y1", d => d.source.y)
        .attr("x2", d => d.target.x)
        .attr("y2", d => d.target.y);

      node.attr("transform", d => `translate(${d.x},${d.y})`);
    });

    simulation.force("link").links(edges);
    simulation.alpha(0.5).restart();
  }

  // ── Node / edge helpers ───────────────────────────────────────────────────
  function nodeRadius(d) {
    return 18 + Math.min(d.caller_count || 0, 30) * 0.8;
  }

  function nodeColor(d) {
    // Color by whether the node has any collisions
    const hasCollision = currentData.edges?.some(
      e => e.source === d.id || e.target === d.id ||
           (typeof e.source === "object" && (e.source.id === d.id || e.target.id === d.id))
    );
    return hasCollision ? "#ff8800" : "#58a6ff";
  }

  // ── Click handlers ────────────────────────────────────────────────────────
  async function onNodeClick(event, d) {
    event.stopPropagation();
    clearSelection();
    d3.select(this).classed("selected", true);
    selectedElement = { type: "node", data: d };

    try {
      const resp = await fetch(`/api/collisions/${d.id}`);
      if (!resp.ok) throw new Error("API error");
      const info = await resp.json();
      showNodeDetail(info, d);
    } catch (err) {
      showErrorDetail("Could not load collision details.");
    }
  }

  function onEdgeClick(event, d) {
    event.stopPropagation();
    clearSelection();
    d3.select(this).classed("selected", true);
    selectedElement = { type: "edge", data: d };
    showEdgeDetail(d);
  }

  // ── Detail panel rendering ─────────────────────────────────────────────────
  function showNodeDetail(info, node) {
    const placeholder = document.getElementById("detail-placeholder");
    const content = document.getElementById("detail-content");
    placeholder.classList.add("hidden");
    content.classList.remove("hidden");

    const collisions = info.collisions || [];
    const severity = collisions.length > 0
      ? collisions.reduce((max, c) => Math.max(max, c.severity), 0)
      : 0;

    let html = `
      <div class="detail-section">
        <div class="detail-title">Merge Request</div>
        <div class="mr-card">
          <div class="mr-title">
            <a href="${escapeHtml(node.url || "#")}" target="_blank">!${node.iid} ${escapeHtml(node.title || "")}</a>
          </div>
          <div class="mr-meta">
            ${escapeHtml(node.project || "")} · by ${escapeHtml(node.author || "")}
          </div>
        </div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:8px;font-size:12px;color:var(--text-muted)">
          <span>⚡ ${info.changed_symbols || 0} changed symbols</span>
          <span>📡 ${info.downstream_callers || 0} downstream callers</span>
        </div>
      </div>`;

    if (collisions.length === 0) {
      html += `<div class="detail-section" style="color:var(--low);font-size:13px">✅ No collisions detected</div>`;
    } else {
      html += `<div class="detail-section"><div class="detail-title">Collisions (${collisions.length})</div>`;
      for (const c of collisions) {
        const labelClass = c.severity_label || "medium";
        html += `
          <div class="mr-card" style="margin-bottom:10px">
            <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:6px">
              <div class="mr-title">
                <a href="${escapeHtml(c.other_mr_url || "#")}" target="_blank">!${c.other_mr_iid} ${escapeHtml(c.other_mr_title || "")}</a>
              </div>
              <span class="collision-badge ${labelClass}">${labelClass}</span>
            </div>
            <div class="symbol-block">⚡ ${escapeHtml(c.symbol)}</div>
            <div class="order-hint">📋 ${escapeHtml(c.suggested_order || "")}</div>
          </div>`;
      }
      html += `</div>`;
    }

    content.innerHTML = html;
  }

  function showEdgeDetail(edge) {
    const placeholder = document.getElementById("detail-placeholder");
    const content = document.getElementById("detail-content");
    placeholder.classList.add("hidden");
    content.classList.remove("hidden");

    const labelClass = edge.severity_label || "medium";
    const severity = typeof edge.severity === "number" ? (edge.severity * 100).toFixed(0) : "—";

    content.innerHTML = `
      <div class="detail-section">
        <div class="detail-title">Semantic Collision</div>
        <div style="display:flex;gap:8px;align-items:center;margin-bottom:12px">
          <span class="collision-badge ${labelClass}">${labelClass}</span>
          <span style="font-size:12px;color:var(--text-muted)">severity score: ${severity}%</span>
        </div>
        <div class="symbol-block">⚡ ${escapeHtml(edge.symbol || "")}</div>
      </div>
      <div class="detail-section">
        <div class="detail-title">Colliding MRs</div>
        <div class="mr-card">
          <div class="mr-meta">MR ${typeof edge.source === "object" ? edge.source.id : edge.source}</div>
        </div>
        <div style="text-align:center;padding:4px;color:var(--text-muted);font-size:12px">⟷</div>
        <div class="mr-card">
          <div class="mr-meta">MR ${typeof edge.target === "object" ? edge.target.id : edge.target}</div>
        </div>
      </div>
      <div class="detail-section">
        <div class="detail-title">What this means</div>
        <div style="font-size:13px;line-height:1.6;color:var(--text-muted)">
          Both MRs interact with <code style="color:var(--accent)">${escapeHtml(edge.symbol || "")}</code>.
          Merging them in the wrong order will cause a runtime or compile-time break.
          See each MR for the suggested merge sequence.
        </div>
      </div>`;
  }

  function showErrorDetail(msg) {
    const content = document.getElementById("detail-content");
    document.getElementById("detail-placeholder").classList.add("hidden");
    content.classList.remove("hidden");
    content.innerHTML = `<div style="color:var(--critical);font-size:13px">${escapeHtml(msg)}</div>`;
  }

  function clearSelection() {
    nodeGroup.selectAll(".node").classed("selected", false);
    linkGroup.selectAll(".link").classed("selected", false);
  }

  // ── Drag ──────────────────────────────────────────────────────────────────
  function dragStart(event, d) {
    if (!event.active) simulation.alphaTarget(0.3).restart();
    d.fx = d.x; d.fy = d.y;
  }
  function dragged(event, d) { d.fx = event.x; d.fy = event.y; }
  function dragEnd(event, d) {
    if (!event.active) simulation.alphaTarget(0);
    d.fx = null; d.fy = null;
  }

  // ── Tooltip ───────────────────────────────────────────────────────────────
  function showTooltip(event, html) {
    tooltip.style("opacity", 1).html(html);
  }
  function moveTooltip(event) {
    tooltip
      .style("left", (event.pageX + 12) + "px")
      .style("top", (event.pageY - 28) + "px");
  }
  function hideTooltip() { tooltip.style("opacity", 0); }

  function escapeHtml(str) {
    return String(str ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  document.addEventListener("DOMContentLoaded", init);
})();
