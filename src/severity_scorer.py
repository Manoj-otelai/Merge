"""Score collision severity based on Orbit graph signals."""
from __future__ import annotations

from .models import CallerInfo, Severity, SymbolChange


def score_collision(
    changed_symbol: SymbolChange,
    callers: list[CallerInfo],
    centrality: float,
    has_ci_coverage: bool = False,
) -> tuple[float, Severity]:
    """Compute a 0.0–1.0 severity score and label.

    Formula:
        severity = centrality_factor × owner_spread_factor × risk_factor

    - centrality_factor: normalized caller count (more callers = higher risk)
    - owner_spread_factor: distinct teams affected (cross-team = higher risk)
    - risk_factor: REMOVED/RENAMED changes are riskier than SIGNATURE_CHANGED;
      lower test coverage raises risk
    """
    if not callers and centrality == 0.0:
        return 0.1, Severity.LOW

    # Centrality: how many places call this symbol on the default branch.
    # Normalize against 15 callers ≈ "highly central". Blend graph centrality
    # (from Orbit) with the count of concretely-affected callers.
    centrality_factor = min(centrality / 15.0, 1.0)
    if len(callers) > 0:
        caller_factor = min(len(callers) / 8.0, 1.0)
        centrality_factor = max(centrality_factor, caller_factor)

    # Owner spread: distinct project paths represent team boundaries. A change
    # that breaks 3+ teams is materially riskier than one contained to one repo.
    distinct_projects = len({c.project_path for c in callers}) if callers else 1
    owner_spread_factor = min(distinct_projects / 3.0, 1.0)

    # Risk factor by change type
    from .models import ChangeType
    change_risk = {
        ChangeType.REMOVED: 1.0,
        ChangeType.RENAMED: 0.95,
        ChangeType.SIGNATURE_CHANGED: 0.8,
        ChangeType.ADDED: 0.1,
    }
    risk_factor = change_risk.get(changed_symbol.change_type, 0.5)

    # Test coverage dampens risk (covered callers are likelier to catch breakage)
    coverage_multiplier = 0.85 if has_ci_coverage else 1.0

    # Blend owner spread so a single-team collision still scores on its own merits.
    spread_term = 0.55 + 0.45 * owner_spread_factor
    raw_score = centrality_factor * risk_factor * spread_term * coverage_multiplier
    score = round(min(max(raw_score, 0.0), 1.0), 3)

    if score >= 0.75:
        label = Severity.CRITICAL
    elif score >= 0.5:
        label = Severity.HIGH
    elif score >= 0.25:
        label = Severity.MEDIUM
    else:
        label = Severity.LOW

    return score, label


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
