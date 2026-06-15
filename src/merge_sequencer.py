"""Global merge sequencing across all open MRs (Phase 1.1).

Given the cross-MR collision graph, compute the **optimal global merge order**
via topological sort. When MR-A changes symbol S and MR-B depends on the old S,
MR-B should merge first (its callers are then updated before MR-A lands), so we
add a dependency edge B → A ("B must merge before A").

Mutually-colliding MRs form a cycle (A breaks something B needs *and* vice
versa). These cannot be linearly ordered, so we surface them as a
**coordination group** ("these must be merged together / in lockstep") rather
than failing.

The algorithm:
  1. Build a directed "merge-before" graph over MR nodes.
  2. Find strongly-connected components (Tarjan, iterative) — any SCC with >1
     node is a cycle / coordination group.
  3. Condense the graph (each SCC → super-node) and topologically sort the
     resulting DAG (Kahn, deterministic tie-break by smallest MR iid).
  4. Emit an ordered, human-readable merge plan with reasons.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class MRNode:
    mr_id: int
    mr_iid: int
    project: str
    title: str
    url: str = ""


@dataclass
class MergeEdge:
    """`before_id` must merge before `after_id` (because of `symbol`)."""
    before_id: int
    after_id: int
    symbol: str
    severity_label: str = ""


@dataclass
class MergeStep:
    order: int
    mr_id: int
    mr_iid: int
    project: str
    title: str
    reason: str
    unblocks: list[int] = field(default_factory=list)   # MR ids that wait on this
    waits_on: list[int] = field(default_factory=list)    # MR ids this waits on

    def to_dict(self) -> dict:
        return {
            "order": self.order,
            "mr_id": self.mr_id,
            "mr_iid": self.mr_iid,
            "project": self.project,
            "title": self.title,
            "reason": self.reason,
            "unblocks": self.unblocks,
            "waits_on": self.waits_on,
        }


@dataclass
class CoordinationGroup:
    """A set of mutually-colliding MRs that must be coordinated together."""
    mr_ids: list[int]
    members: list[dict]
    symbols: list[str]
    reason: str

    def to_dict(self) -> dict:
        return {
            "mr_ids": self.mr_ids,
            "members": self.members,
            "symbols": self.symbols,
            "reason": self.reason,
        }


@dataclass
class MergePlan:
    steps: list[MergeStep] = field(default_factory=list)
    cycles: list[CoordinationGroup] = field(default_factory=list)
    independent: list[dict] = field(default_factory=list)   # MRs with no collisions

    def to_dict(self) -> dict:
        return {
            "steps": [s.to_dict() for s in self.steps],
            "cycles": [c.to_dict() for c in self.cycles],
            "independent": self.independent,
            "has_cycles": bool(self.cycles),
            "total_ordered": len(self.steps),
        }

    def summary_text(self) -> str:
        """A compact one-line-per-step plan for MR comments / CLI."""
        if not self.steps and not self.cycles:
            return "No sequencing needed — no cross-MR collisions detected."
        lines: list[str] = []
        for s in self.steps:
            lines.append(f"{s.order}. !{s.mr_iid} ({s.project}) — {s.reason}")
        for g in self.cycles:
            iids = ", ".join(f"!{m['mr_iid']}" for m in g.members)
            lines.append(f"⚠️ Coordinate together: {iids} — {g.reason}")
        return "\n".join(lines)


def compute_merge_plan(mrs: list[MRNode], edges: list[MergeEdge]) -> MergePlan:
    """Compute the global merge order from the collision dependency graph."""
    node_by_id = {n.mr_id: n for n in mrs}

    # Adjacency for "merge-before": before → {after}
    adj: dict[int, set[int]] = {n.mr_id: set() for n in mrs}
    symbols_between: dict[tuple[int, int], set[str]] = {}
    involved: set[int] = set()
    for e in edges:
        if e.before_id not in node_by_id or e.after_id not in node_by_id:
            continue
        if e.before_id == e.after_id:
            continue
        adj[e.before_id].add(e.after_id)
        symbols_between.setdefault((e.before_id, e.after_id), set()).add(e.symbol)
        involved.add(e.before_id)
        involved.add(e.after_id)

    # MRs not touched by any collision edge are independent / safe anytime.
    independent = [
        {"mr_id": n.mr_id, "mr_iid": n.mr_iid, "project": n.project, "title": n.title}
        for n in sorted(mrs, key=lambda x: x.mr_iid)
        if n.mr_id not in involved
    ]

    # ── Strongly-connected components (Tarjan, iterative) ──
    sccs = _tarjan_scc(adj)
    comp_of: dict[int, int] = {}
    for idx, comp in enumerate(sccs):
        for node in comp:
            comp_of[node] = idx

    # ── Condensation DAG over components that contain involved nodes ──
    comp_adj: dict[int, set[int]] = {i: set() for i in range(len(sccs))}
    comp_indeg: dict[int, int] = {i: 0 for i in range(len(sccs))}
    seen_edges: set[tuple[int, int]] = set()
    for before, afters in adj.items():
        cb = comp_of[before]
        for after in afters:
            ca = comp_of[after]
            if cb != ca and (cb, ca) not in seen_edges:
                seen_edges.add((cb, ca))
                comp_adj[cb].add(ca)
                comp_indeg[ca] += 1

    # Only sequence components that are part of the collision graph.
    relevant_comps = {comp_of[n] for n in involved}

    # ── Kahn topological sort with deterministic tie-break ──
    def comp_min_iid(ci: int) -> int:
        return min(node_by_id[n].mr_iid for n in sccs[ci])

    ready = sorted(
        [ci for ci in relevant_comps if comp_indeg[ci] == 0],
        key=comp_min_iid,
    )
    topo: list[int] = []
    indeg = dict(comp_indeg)
    while ready:
        ci = ready.pop(0)
        topo.append(ci)
        for nxt in sorted(comp_adj[ci], key=comp_min_iid):
            if nxt not in relevant_comps:
                continue
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                ready.append(nxt)
        ready.sort(key=comp_min_iid)

    # Any relevant components not in topo are inside cycles already captured.
    plan = MergePlan(independent=independent)
    order_counter = 1

    for ci in topo:
        comp = sccs[ci]
        if len(comp) == 1:
            node = node_by_id[comp[0]]
            waits_on = sorted(
                {b for (b, a) in symbols_between if a == node.mr_id},
                key=lambda i: node_by_id[i].mr_iid,
            )
            unblocks = sorted(
                {a for (b, a) in symbols_between if b == node.mr_id},
                key=lambda i: node_by_id[i].mr_iid,
            )
            reason = _step_reason(node, waits_on, unblocks, symbols_between, node_by_id)
            plan.steps.append(MergeStep(
                order=order_counter,
                mr_id=node.mr_id,
                mr_iid=node.mr_iid,
                project=node.project,
                title=node.title,
                reason=reason,
                unblocks=unblocks,
                waits_on=waits_on,
            ))
            order_counter += 1
        else:
            members = [
                {"mr_id": node_by_id[n].mr_id, "mr_iid": node_by_id[n].mr_iid,
                 "project": node_by_id[n].project, "title": node_by_id[n].title}
                for n in sorted(comp, key=lambda i: node_by_id[i].mr_iid)
            ]
            syms = sorted({
                s for (b, a), ss in symbols_between.items()
                if b in comp and a in comp for s in ss
            })
            iids = ", ".join(f"!{m['mr_iid']}" for m in members)
            reason = (
                f"These MRs mutually break each other's contracts "
                f"(on {', '.join('`'+s+'`' for s in syms) or 'shared symbols'}). "
                f"There is no safe linear order — coordinate {iids} as a single "
                f"change set (land together, or split the shared contract out first)."
            )
            plan.cycles.append(CoordinationGroup(
                mr_ids=[m["mr_id"] for m in members],
                members=members,
                symbols=syms,
                reason=reason,
            ))

    return plan


def _step_reason(node, waits_on, unblocks, symbols_between, node_by_id) -> str:
    if not waits_on and unblocks:
        syms = sorted({s for (b, a), ss in symbols_between.items()
                       if b == node.mr_id for s in ss})
        unblocked = ", ".join(f"!{node_by_id[i].mr_iid}" for i in unblocks)
        return (
            f"Merge first — it depends on the current contract of "
            f"{', '.join('`'+s+'`' for s in syms)}; landing it now lets "
            f"{unblocked} update callers before the contract changes."
        )
    if waits_on and unblocks:
        after = ", ".join(f"!{node_by_id[i].mr_iid}" for i in waits_on)
        before = ", ".join(f"!{node_by_id[i].mr_iid}" for i in unblocks)
        return f"Merge after {after} (whose callers it touches) and before {before}."
    if waits_on:
        after = ", ".join(f"!{node_by_id[i].mr_iid}" for i in waits_on)
        syms = sorted({s for (b, a), ss in symbols_between.items()
                       if a == node.mr_id for s in ss})
        return (
            f"Merge last — it changes {', '.join('`'+s+'`' for s in syms)}; "
            f"merge {after} first, then update the affected callers as part of this MR."
        )
    return "No collisions — safe to merge anytime."


def _tarjan_scc(adj: dict[int, set[int]]) -> list[list[int]]:
    """Iterative Tarjan's SCC. Returns components in reverse-topological order."""
    index_counter = [0]
    stack: list[int] = []
    on_stack: set[int] = set()
    indices: dict[int, int] = {}
    lowlink: dict[int, int] = {}
    result: list[list[int]] = []

    for start in adj:
        if start in indices:
            continue
        # Each work item: (node, iterator over neighbours)
        work: list[tuple[int, list[int]]] = [(start, sorted(adj[start]))]
        indices[start] = lowlink[start] = index_counter[0]
        index_counter[0] += 1
        stack.append(start)
        on_stack.add(start)

        while work:
            node, neighbours = work[-1]
            progressed = False
            while neighbours:
                w = neighbours.pop(0)
                if w not in indices:
                    indices[w] = lowlink[w] = index_counter[0]
                    index_counter[0] += 1
                    stack.append(w)
                    on_stack.add(w)
                    work.append((w, sorted(adj[w])))
                    progressed = True
                    break
                elif w in on_stack:
                    lowlink[node] = min(lowlink[node], indices[w])
            if progressed:
                continue
            # Done exploring `node`
            if lowlink[node] == indices[node]:
                comp: list[int] = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp.append(w)
                    if w == node:
                        break
                result.append(comp)
            work.pop()
            if work:
                parent = work[-1][0]
                lowlink[parent] = min(lowlink[parent], lowlink[node])

    return result
