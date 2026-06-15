"""Tests for the refined severity scorer (Phase 1.3)."""
from src.models import CallerInfo, ChangeType, Language, Severity, SymbolChange
from src.severity_scorer import (
    CollisionScore,
    score_collision,
    score_collision_detailed,
)


def make_symbol(name="charge_user", change_type=ChangeType.SIGNATURE_CHANGED) -> SymbolChange:
    return SymbolChange(
        name=name,
        old_signature=f"def {name}(amount)",
        new_signature=f"def {name}(amount, currency)",
        file_path=f"src/{name}.py",
        language=Language.PYTHON,
        change_type=change_type,
    )


def make_callers(n: int, projects: int = 1) -> list[CallerInfo]:
    out = []
    for i in range(n):
        proj = f"group/svc-{i % projects}"
        out.append(CallerInfo(f"caller_{i}", f"src/c{i}.py", proj, 10 + i, owner=f"dev{i % projects}"))
    return out


class TestDetailedScore:
    def test_backwards_compatible_tuple(self):
        score, label = score_collision(make_symbol(), make_callers(4, 2), 12.0)
        assert 0.0 <= score <= 1.0
        assert isinstance(label, Severity)

    def test_returns_rich_object(self):
        result = score_collision_detailed(make_symbol(), make_callers(12, 3), 12.0)
        assert isinstance(result, CollisionScore)
        assert result.score > 0
        assert 0.0 <= result.confidence <= 1.0
        assert result.explanation
        assert "centrality_factor" in result.factors

    def test_cross_team_signature_change_is_high_or_critical(self):
        result = score_collision_detailed(make_symbol(), make_callers(12, 3), 12.0)
        assert result.label in (Severity.HIGH, Severity.CRITICAL)

    def test_no_callers_low_confidence(self):
        result = score_collision_detailed(make_symbol(), [], 0.0)
        assert result.label == Severity.LOW
        assert result.confidence <= 0.4

    def test_removed_is_riskier_than_signature_change(self):
        callers = make_callers(8, 2)
        removed = score_collision_detailed(make_symbol(change_type=ChangeType.REMOVED), callers, 10.0)
        sig = score_collision_detailed(make_symbol(change_type=ChangeType.SIGNATURE_CHANGED), callers, 10.0)
        assert removed.score >= sig.score

    def test_coverage_dampens_score(self):
        callers = make_callers(8, 2)
        uncovered = score_collision_detailed(make_symbol(), callers, 10.0, has_ci_coverage=False)
        covered = score_collision_detailed(make_symbol(), callers, 10.0, has_ci_coverage=True)
        assert covered.score < uncovered.score

    def test_confidence_rises_with_signals(self):
        weak = score_collision_detailed(make_symbol(), make_callers(1, 1), 0.0)
        strong = score_collision_detailed(make_symbol(), make_callers(6, 3), 12.0)
        assert strong.confidence > weak.confidence

    def test_explanation_mentions_symbol_and_teams(self):
        result = score_collision_detailed(make_symbol("charge_user"), make_callers(6, 3), 12.0)
        assert "charge_user" in result.explanation
        assert "team" in result.explanation
