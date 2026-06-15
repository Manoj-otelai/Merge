"""Tests for the Ask MergeGuard Q&A engine."""
import pytest

from src import ask as ask_module
from src.collision_engine import CollisionEngine
from src.models import BlastRadius, CallerInfo, ChangeType, Language, SymbolChange


def make_engine(tmp_path):
    db = str(tmp_path / "ask_test.db")
    return CollisionEngine(db)


def make_blast_radius(mr_id, mr_iid, project, changed_syms, callers):
    return BlastRadius(
        mr_id=mr_id,
        mr_iid=mr_iid,
        mr_url=f"https://gitlab.com/{project}/-/merge_requests/{mr_iid}",
        mr_title=f"MR {mr_iid}",
        project_id=mr_id * 10,
        project_path=project,
        changed_symbols=changed_syms,
        downstream_callers=callers,
        affected_owners=["alice", "bob"],
        centrality_score=8.0,
    )


def make_symbol(name):
    return SymbolChange(
        name=name,
        old_signature=f"def {name}(x)",
        new_signature=f"def {name}(x, y)",
        file_path=f"src/{name}.py",
        language=Language.PYTHON,
        change_type=ChangeType.SIGNATURE_CHANGED,
    )


def make_caller(fn, project="group/consumer"):
    return CallerInfo(fn, f"src/{fn}.py", project, 999, "alice")


@pytest.fixture
def engine(tmp_path):
    eng = make_engine(tmp_path)
    br_a = make_blast_radius(1, 1, "group/core", [make_symbol("pay")], [make_caller("invoice")])
    br_b = make_blast_radius(2, 2, "group/notify", [], [make_caller("pay", "group/core")])
    br_c = make_blast_radius(3, 3, "group/safe", [make_symbol("internal")], [])
    eng.register_mr(br_a)
    eng.register_mr(br_b)
    eng.register_mr(br_c)
    for br in (br_a, br_b, br_c):
        eng.record_collisions(eng.find_collisions(br.mr_id))
    yield eng
    eng.close()


class TestAskIntents:
    def test_safe_to_merge(self, engine):
        result = ask_module.answer("Which MRs are safe to merge today?", engine)
        assert result["intent"] == "safe_to_merge"
        assert "answer" in result

    def test_what_breaks(self, engine):
        result = ask_module.answer("What does !1 break?", engine)
        assert result["intent"] == "what_breaks"
        assert "pay" in result["answer"].lower() or "collision" in result["answer"].lower()

    def test_what_breaks_unknown(self, engine):
        result = ask_module.answer("What does !999 break?", engine)
        assert "999" in result["answer"]

    def test_riskiest(self, engine):
        result = ask_module.answer("What's the riskiest collision?", engine)
        assert result["intent"] == "riskiest"
        assert "pay" in result["answer"] or "safe" in result["answer"].lower()

    def test_savings(self, engine):
        result = ask_module.answer("How much has MergeGuard saved us?", engine)
        assert result["intent"] == "savings"
        assert "MergeGuard" in result["answer"]

    def test_merge_plan(self, engine):
        result = ask_module.answer("What's the merge order?", engine)
        assert result["intent"] == "merge_plan"

    def test_overview_fallback(self, engine):
        result = ask_module.answer("Hello, give me a summary", engine)
        assert result["intent"] == "overview"

    def test_hotspots(self, engine):
        result = ask_module.answer("Which files are the hotspots?", engine)
        assert result["intent"] == "hotspots"

    def test_owner_load(self, engine):
        result = ask_module.answer("Who is most affected by collisions?", engine)
        assert result["intent"] == "owner_load"

    def test_dismissed_none(self, engine):
        result = ask_module.answer("Show me dismissed collisions", engine)
        assert result["intent"] == "dismissed"

    def test_who_to_coordinate(self, engine):
        result = ask_module.answer("Who do I need to talk to to merge !1?", engine)
        assert result["intent"] == "who_to_coordinate"
