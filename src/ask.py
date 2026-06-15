"""'Ask MergeGuard' — natural-language Q&A over the live collision graph (Phase 4.3).

A deterministic, intent-matching responder that answers developer questions
straight from the collision registry, merge plan, and analytics — no LLM round
trip required, so answers are fast, free, and reproducible in the demo. The same
entry point backs the Duo chat skill (agent/skills/ask_mergeguard.yml).

Examples it handles:
  * "Which of my open MRs are safe to merge today?"
  * "What does !142 break?"
  * "Who do I need to coordinate with to merge !142?"
  * "What's the riskiest collision right now?"
  * "How much has MergeGuard saved us?"
  * "What's the merge order?"
"""
from __future__ import annotations

import re


def _find_iid(question: str) -> int | None:
    m = re.search(r"!?\b(\d{1,7})\b", question)
    return int(m.group(1)) if m else None


def answer(question: str, engine, cost_model=None) -> dict:
    """Answer a natural-language question. Returns {answer, intent, data}."""
    q = question.lower().strip()
    iid = _find_iid(question)

    # ── safe-to-merge ──
    if any(w in q for w in ("safe to merge", "safe today", "which", "can i merge", "ready to merge")) \
            and "merge" in q:
        return _safe_to_merge(engine)

    # ── what does !X break / collide with ──
    if iid is not None and any(w in q for w in ("break", "collide", "conflict", "affect", "impact", "what does")):
        return _what_breaks(engine, iid)

    # ── who to coordinate with ──
    if iid is not None and any(w in q for w in ("coordinate", "who", "owner", "notify", "talk to")):
        return _who_to_coordinate(engine, iid)

    # ── riskiest ──
    if any(w in q for w in ("riskiest", "most dangerous", "highest severity", "worst", "top collision")):
        return _riskiest(engine)

    # ── savings ──
    if any(w in q for w in ("saved", "savings", "cost", "money", "hours", "roi")):
        return _savings(engine, cost_model)

    # ── merge order / plan ──
    if any(w in q for w in ("merge order", "sequence", "plan", "order to merge", "what order")):
        return _merge_plan(engine)

    # ── fallback: per-MR if an iid was given, else overview ──
    if iid is not None:
        return _what_breaks(engine, iid)
    return _overview(engine, cost_model)


# ── Intent handlers ──────────────────────────────────────────────────────────

def _br_by_iid(engine, iid: int):
    for br in engine.all_open_mrs():
        if br.mr_iid == iid:
            return br
    return None


def _safe_to_merge(engine) -> dict:
    plan = engine.get_merge_plan()
    safe = [m for m in plan.independent]
    # The first step in the plan is also safe to merge now.
    if plan.steps:
        first = plan.steps[0]
        safe.append({"mr_id": first.mr_id, "mr_iid": first.mr_iid,
                     "project": first.project, "title": first.title})
    if not safe:
        if plan.cycles:
            return {"intent": "safe_to_merge", "answer":
                    "⚠️ Nothing is safe to merge alone right now — there are mutually-"
                    "colliding MRs that must be coordinated together. Check the merge plan.",
                    "data": plan.to_dict()}
        return {"intent": "safe_to_merge",
                "answer": "✅ No open MRs are tracked, so there's nothing blocked.",
                "data": {}}
    names = ", ".join(f"!{m['mr_iid']} ({m['project'].split('/')[-1]})" for m in safe)
    return {"intent": "safe_to_merge",
            "answer": f"✅ Safe to merge now: {names}.",
            "data": {"safe": safe}}


def _what_breaks(engine, iid: int) -> dict:
    br = _br_by_iid(engine, iid)
    if not br:
        return {"intent": "what_breaks", "answer": f"I don't have !{iid} in the registry.", "data": {}}
    collisions = engine.find_collisions(br.mr_id)
    if not collisions:
        return {"intent": "what_breaks",
                "answer": f"✅ !{iid} has no semantic collisions — it's safe to merge.",
                "data": {"collisions": []}}
    parts = []
    for c in collisions:
        other = c.mr_b_iid if c.mr_a_id == br.mr_id else c.mr_a_iid
        parts.append(f"`{c.intersecting_symbol}` ({c.severity_label.value}, "
                     f"{len(c.affected_callers)} callers) — collides with !{other}")
    return {"intent": "what_breaks",
            "answer": f"!{iid} has {len(collisions)} collision(s): " + "; ".join(parts) + ".",
            "data": {"collisions": [_collision_brief(c, br.mr_id) for c in collisions]}}


def _who_to_coordinate(engine, iid: int) -> dict:
    br = _br_by_iid(engine, iid)
    if not br:
        return {"intent": "who_to_coordinate", "answer": f"I don't have !{iid} in the registry.", "data": {}}
    collisions = engine.find_collisions(br.mr_id)
    owners: set[str] = set()
    other_mrs: set[int] = set()
    for c in collisions:
        owners.update(c.affected_owners)
        other_mrs.add(c.mr_b_iid if c.mr_a_id == br.mr_id else c.mr_a_iid)
    if not collisions:
        return {"intent": "who_to_coordinate",
                "answer": f"!{iid} doesn't collide with anything — no coordination needed.", "data": {}}
    owner_str = ", ".join(f"@{o}" for o in sorted(owners)) if owners else "no specific owners found"
    mr_str = ", ".join(f"!{m}" for m in sorted(other_mrs))
    return {"intent": "who_to_coordinate",
            "answer": f"To merge !{iid}, coordinate with {owner_str} and align with {mr_str}.",
            "data": {"owners": sorted(owners), "other_mrs": sorted(other_mrs)}}


def _riskiest(engine) -> dict:
    worst = None
    for br in engine.all_open_mrs():
        for c in engine.find_collisions(br.mr_id):
            if worst is None or c.severity > worst.severity:
                worst = c
    if not worst:
        return {"intent": "riskiest", "answer": "✅ No collisions detected right now.", "data": {}}
    return {"intent": "riskiest",
            "answer": (f"🔴 The riskiest collision is `{worst.intersecting_symbol}` "
                       f"({worst.severity_label.value}, {round(worst.severity*100)}%) between "
                       f"!{worst.mr_a_iid} and !{worst.mr_b_iid}. {worst.explanation}"),
            "data": _collision_brief(worst, worst.mr_a_id)}


def _savings(engine, cost_model) -> dict:
    a = engine.get_analytics(cost_model)
    cs = a["cost_saved"]
    t = a["totals"]
    return {"intent": "savings",
            "answer": (f"💰 MergeGuard has prevented an estimated {cs['currency']} "
                       f"{cs['dollars']:,.0f} and ~{round(cs['engineer_hours'])} engineer-hours "
                       f"across {t['total_collisions']} collisions caught before merge."),
            "data": a}


def _merge_plan(engine) -> dict:
    plan = engine.get_merge_plan()
    return {"intent": "merge_plan",
            "answer": plan.summary_text(),
            "data": plan.to_dict()}


def _overview(engine, cost_model) -> dict:
    cmap = engine.get_collision_map()
    a = engine.get_analytics(cost_model)
    return {"intent": "overview",
            "answer": (f"Tracking {cmap.open_mrs} open MR(s) with {cmap.total_collisions} "
                       f"collision(s). Ask me what a specific MR breaks (e.g. \"what does !7 break?\"), "
                       f"which MRs are safe to merge, who to coordinate with, or how much we've saved."),
            "data": {"open_mrs": cmap.open_mrs, "collisions": cmap.total_collisions,
                     "cost_saved": a["cost_saved"]}}


def _collision_brief(c, perspective_id: int) -> dict:
    other = c.mr_b_iid if c.mr_a_id == perspective_id else c.mr_a_iid
    return {
        "symbol": c.intersecting_symbol,
        "severity": c.severity,
        "severity_label": c.severity_label.value,
        "other_mr_iid": other,
        "callers": len(c.affected_callers),
        "owners": c.affected_owners,
    }
