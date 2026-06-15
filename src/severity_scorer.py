"""Score collision severity based on Orbit graph signals.

Refined model (Phase 1.3):

    severity = centrality × untested_blast_zone × owner_spread × risk

Every collision also carries a **confidence** value (how much we trust the
score given the available signals) and a plain-language **"why this is
dangerous"** explanation rendered from the same factors.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import CallerInfo, Severity, SymbolChange


@dataclass
class CollisionScore:
    """Rich scoring result for a single collision."""
    score: float
    label: Severity
    confidence: float
    explanation: str
    factors: dict = field(default_factory=dict)


def _label_for(score: float) -> Severity:
    if score >= 0.75:
        return Severity.CRITICAL
    if score >= 0.5:
        return Severity.HIGH
    if score >= 0.25:
        return Severity.MEDIUM
    return Severity.LOW


def score_collision(
    changed_symbol: SymbolChange,
    callers: list[CallerInfo],
    centrality: float,
    has_ci_coverage: bool = False,
) -> tuple[float, Severity]:
    """Backwards-compatible entry point: returns just (score, label).

    Internally delegates to :func:`score_collision_detailed`.
    """
    detailed = score_collision_detailed(changed_symbol, callers, centrality, has_ci_coverage)
    return detailed.score, detailed.label


def score_collision_detailed(
    changed_symbol: SymbolChange,
    callers: list[CallerInfo],
    centrality: float,
    has_ci_coverage: bool = False,
) -> CollisionScore:
    """Compute a 0.0–1.0 severity score with confidence and explanation.

    Formula:
        severity = centrality_factor × untested_blast_zone × spread_term × risk_factor

    - centrality_factor: "fragile core" signal — how many places call this
      symbol on the default branch (Orbit graph centrality blended with the
      count of concretely-affected callers).
    - untested_blast_zone: callers behind a coverage job are likelier to catch
      breakage in CI, so a covered blast zone is less dangerous.
    - spread_term: distinct teams (project paths) affected; cross-team = riskier.
    - risk_factor: REMOVED/RENAMED changes are riskier than SIGNATURE_CHANGED.
    """
    from .models import ChangeType

    n_callers = len(callers)
    distinct_projects = len({c.project_path for c in callers}) if callers else 1

    if not callers and centrality == 0.0:
        return CollisionScore(
            score=0.1,
            label=Severity.LOW,
            confidence=0.3,
            explanation=(
                f"No downstream callers of `{changed_symbol.name}` were found on the "
                f"default branch via Orbit, so the blast radius looks contained. "
                f"Confidence is low — the symbol may be too new to be indexed, or "
                f"called dynamically."
            ),
            factors={
                "centrality_factor": 0.0,
                "untested_blast_zone": 1.0,
                "spread_term": 0.55,
                "risk_factor": 0.0,
                "caller_count": 0,
                "distinct_projects": 0,
            },
        )

    # Centrality ("fragile core"): normalize graph centrality against 15 callers
    # ≈ "highly central", blended with concretely-affected caller count.
    centrality_factor = min(centrality / 15.0, 1.0)
    if n_callers > 0:
        caller_factor = min(n_callers / 8.0, 1.0)
        centrality_factor = max(centrality_factor, caller_factor)

    # Untested blast zone: covered callers are likelier to catch breakage in CI.
    untested_blast_zone = 0.7 if has_ci_coverage else 1.0

    # Owner spread: distinct project paths represent team boundaries. Blend so a
    # single-team collision still scores on its own merits.
    owner_spread_factor = min(distinct_projects / 3.0, 1.0)
    spread_term = 0.55 + 0.45 * owner_spread_factor

    # Risk factor by change type.
    change_risk = {
        ChangeType.REMOVED: 1.0,
        ChangeType.RENAMED: 0.95,
        ChangeType.SIGNATURE_CHANGED: 0.8,
        ChangeType.ADDED: 0.1,
    }
    risk_factor = change_risk.get(changed_symbol.change_type, 0.5)

    raw_score = centrality_factor * untested_blast_zone * spread_term * risk_factor
    score = round(min(max(raw_score, 0.0), 1.0), 3)
    label = _label_for(score)

    confidence = _confidence(changed_symbol, n_callers, centrality)
    explanation = _explain(changed_symbol, callers, distinct_projects, has_ci_coverage)

    return CollisionScore(
        score=score,
        label=label,
        confidence=confidence,
        explanation=explanation,
        factors={
            "centrality_factor": round(centrality_factor, 3),
            "untested_blast_zone": untested_blast_zone,
            "spread_term": round(spread_term, 3),
            "risk_factor": risk_factor,
            "caller_count": n_callers,
            "distinct_projects": distinct_projects,
        },
    )


def _confidence(changed_symbol: SymbolChange, n_callers: int, centrality: float) -> float:
    """How much to trust this score given the available Orbit signals.

    Confidence is highest when Orbit returned concrete callers AND a non-zero
    centrality, and the change is a well-defined contract break.
    """
    from .models import ChangeType

    conf = 0.4
    if n_callers > 0:
        conf += 0.30
    if centrality > 0:
        conf += 0.15
    if n_callers >= 3:
        conf += 0.05
    if changed_symbol.change_type in (
        ChangeType.REMOVED,
        ChangeType.RENAMED,
        ChangeType.SIGNATURE_CHANGED,
    ):
        conf += 0.10
    return round(min(conf, 1.0), 2)


def _explain(
    changed_symbol: SymbolChange,
    callers: list[CallerInfo],
    distinct_projects: int,
    has_ci_coverage: bool,
) -> str:
    """Plain-language 'why this is dangerous' summary."""
    from .models import ChangeType

    sym = changed_symbol.name
    n = len(callers)
    team_word = "team" if distinct_projects == 1 else "teams"
    projects = sorted({_project_short(c.project_path) for c in callers if c.project_path})
    team_list = ", ".join(projects[:3]) + ("…" if len(projects) > 3 else "")

    change_phrase = {
        ChangeType.REMOVED: f"removes `{sym}`",
        ChangeType.RENAMED: f"renames `{sym}`",
        ChangeType.SIGNATURE_CHANGED: f"changes the signature of `{sym}`",
    }.get(changed_symbol.change_type, f"changes `{sym}`")

    consequence = {
        ChangeType.REMOVED: "every remaining call site will fail to resolve the symbol",
        ChangeType.RENAMED: "every call site using the old name will fail to resolve",
        ChangeType.SIGNATURE_CHANGED: "any caller not updated to the new signature will raise at runtime",
    }.get(changed_symbol.change_type, "callers may break")

    parts = [
        f"`{sym}` is called by {n} site{'s' if n != 1 else ''} across "
        f"{distinct_projects} {team_word}" + (f" ({team_list})" if team_list else "") + ".",
        f"This MR {change_phrase}, so after merge {consequence} — even though Git "
        f"reports no text conflict and CI on each MR is green in isolation.",
    ]
    if has_ci_coverage:
        parts.append("Some affected callers sit behind a coverage job, which lowers the risk slightly.")
    else:
        parts.append("None of the affected callers sit behind a coverage job, so CI won't catch the break.")
    return " ".join(parts)


def _project_short(path: str) -> str:
    return path.split("/")[-1] if path else path


def suggest_merge_order(
    mr_a_id: int,
    mr_a_iid: int,
    mr_a_project: str,
    mr_b_id: int,
    mr_b_iid: int,
    mr_b_project: str,
    symbol_name: str,
    mr_a_changes: bool,
) -> str:
    """Suggest which MR to merge first to minimize breakage.

    If MR-A changes the symbol and MR-B calls it:
      → MR-B should merge first (to establish the caller context),
        then MR-A must update callers as part of its change.
    If MR-A adds a new caller and MR-B removes/renames the symbol:
      → MR-B must merge first; MR-A must be updated to use the new API.
    """
    if mr_a_changes:
        return (
            f"Merge `{mr_b_project}!{mr_b_iid}` first (it calls `{symbol_name}` with the current signature). "
            f"Then update `{mr_b_project}!{mr_b_iid}`'s callers to use the new API before merging "
            f"`{mr_a_project}!{mr_a_iid}`."
        )
    else:
        return (
            f"Merge `{mr_b_project}!{mr_b_iid}` (which changes `{symbol_name}`) first, "
            f"then update `{mr_a_project}!{mr_a_iid}` to use the new `{symbol_name}` API."
        )
