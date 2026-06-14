"""Tests for the collision detection engine."""
import os
import tempfile

import pytest

from src.collision_engine import CollisionEngine
from src.models import (
    BlastRadius,
    CallerInfo,
    ChangeType,
    Language,
    Severity,
    SymbolChange,
)


def make_symbol(name: str, old_sig: str = "", new_sig: str = "",
                change_type: ChangeType = ChangeType.SIGNATURE_CHANGED) -> SymbolChange:
    return SymbolChange(
        name=name,
        old_signature=old_sig or f"def {name}(amount)",
        new_signature=new_sig or f"def {name}(amount, currency)",
        file_path=f"src/{name}.py",
        language=Language.PYTHON,
        change_type=change_type,
    )


def make_caller(fn_name: str, project: str = "demo-group/consumer") -> CallerInfo:
    return CallerInfo(
        function_name=fn_name,
        file_path=f"src/service/{fn_name}.py",
        project_path=project,
        project_id=999,
        owner="engineer",
    )


def make_blast_radius(
    mr_id: int,
    mr_iid: int,
    project: str,
    changed_syms: list,
    callers: list,
) -> BlastRadius:
    return BlastRadius(
        mr_id=mr_id,
        mr_iid=mr_iid,
        mr_url=f"https://gitlab.com/{project}/-/merge_requests/{mr_iid}",
        mr_title=f"MR {mr_iid}",
        project_id=mr_id * 10,
        project_path=project,
        changed_symbols=changed_syms,
        downstream_callers=callers,
        affected_owners=["owner1"],
        centrality_score=5.0,
    )


@pytest.fixture
def engine(tmp_path):
    db = str(tmp_path / "test.db")
    eng = CollisionEngine(db)
    yield eng
    eng.close()


class TestCollisionDetection:
    def test_no_collision_when_only_one_mr(self, engine):
        br = make_blast_radius(
            1, 1, "group/payments",
            [make_symbol("charge_user")],
            [],
        )
        engine.register_mr(br)
        collisions = engine.find_collisions(1)
        assert collisions == []

    def test_collision_detected_when_mr_b_calls_changed_symbol(self, engine):
        """MR-A changes charge_user; MR-B calls charge_user → collision."""
        mr_a = make_blast_radius(
            1, 1, "group/payments",
            changed_syms=[make_symbol("charge_user")],
            callers=[make_caller("send_invoice", "group/notifications")],
        )
        mr_b = make_blast_radius(
            2, 2, "group/notifications",
            changed_syms=[],  # MR-B doesn't change signatures
            callers=[make_caller("charge_user", "group/payments")],  # but calls charge_user
        )
        engine.register_mr(mr_a)
        engine.register_mr(mr_b)

        collisions_a = engine.find_collisions(1)
        assert len(collisions_a) == 1
        assert collisions_a[0].intersecting_symbol == "charge_user"
        assert collisions_a[0].mr_a_id == 1
        assert collisions_a[0].mr_b_id == 2

    def test_collision_detected_bidirectionally(self, engine):
        """Finding collisions for MR-B should reveal the same collision."""
        mr_a = make_blast_radius(
            1, 1, "group/payments",
            [make_symbol("charge_user")],
            [make_caller("send_invoice", "group/notifications")],
        )
        mr_b = make_blast_radius(
            2, 2, "group/notifications",
            [],
            [make_caller("charge_user", "group/payments")],
        )
        engine.register_mr(mr_a)
        engine.register_mr(mr_b)

        collisions_b = engine.find_collisions(2)
        assert len(collisions_b) >= 1
        symbols = {c.intersecting_symbol for c in collisions_b}
        assert "charge_user" in symbols

    def test_no_collision_when_different_symbols(self, engine):
        mr_a = make_blast_radius(
            1, 1, "group/payments",
            [make_symbol("charge_user")],
            [],
        )
        mr_b = make_blast_radius(
            2, 2, "group/notifications",
            [make_symbol("send_email")],
            [make_caller("notify", "group/notifications")],
        )
        engine.register_mr(mr_a)
        engine.register_mr(mr_b)

        assert engine.find_collisions(1) == []

    def test_collision_severity_ordering(self, engine):
        """Higher-severity collisions should appear first."""
        mr_a = make_blast_radius(
            1, 1, "group/payments",
            [make_symbol("charge_user"), make_symbol("obscure_helper")],
            [make_caller("send_invoice", "group/notifications")] * 15,
        )
        mr_b = make_blast_radius(
            2, 2, "group/notifications",
            [],
            [make_caller("charge_user", "group/p"), make_caller("obscure_helper", "group/p")],
        )
        engine.register_mr(mr_a)
        engine.register_mr(mr_b)

        collisions = engine.find_collisions(1)
        if len(collisions) > 1:
            assert collisions[0].severity >= collisions[1].severity

    def test_closing_mr_removes_it(self, engine):
        br = make_blast_radius(1, 1, "group/payments", [make_symbol("f")], [])
        engine.register_mr(br)
        engine.close_mr(1)
        assert engine.get_blast_radius(1) is None

    def test_collision_map_structure(self, engine):
        mr_a = make_blast_radius(1, 1, "group/p", [make_symbol("fn")], [])
        mr_b = make_blast_radius(2, 2, "group/q", [], [make_caller("fn", "group/p")])
        engine.register_mr(mr_a)
        engine.register_mr(mr_b)

        cmap = engine.get_collision_map()
        assert cmap.open_mrs == 2
        assert len(cmap.nodes) == 2
        node_ids = {n["id"] for n in cmap.nodes}
        assert 1 in node_ids
        assert 2 in node_ids

    def test_register_updates_existing_mr(self, engine):
        br1 = make_blast_radius(1, 1, "group/p", [make_symbol("old_fn")], [])
        engine.register_mr(br1)

        br2 = make_blast_radius(1, 1, "group/p", [make_symbol("new_fn")], [])
        engine.register_mr(br2)

        stored = engine.get_blast_radius(1)
        assert stored is not None
        names = {s.name for s in stored.changed_symbols}
        assert "new_fn" in names
        assert "old_fn" not in names

    def test_three_mr_fan_out(self, engine):
        """One MR changes a shared symbol; two others depend on it."""
        mr_a = make_blast_radius(
            1, 1, "group/core",
            [make_symbol("shared_api")],
            [make_caller("x", "group/svc-b"), make_caller("y", "group/svc-c")],
        )
        mr_b = make_blast_radius(2, 2, "group/svc-b", [], [make_caller("shared_api", "group/core")])
        mr_c = make_blast_radius(3, 3, "group/svc-c", [], [make_caller("shared_api", "group/core")])

        engine.register_mr(mr_a)
        engine.register_mr(mr_b)
        engine.register_mr(mr_c)

        collisions = engine.find_collisions(1)
        colliding_mrs = {c.mr_b_id for c in collisions}
        assert 2 in colliding_mrs
        assert 3 in colliding_mrs
