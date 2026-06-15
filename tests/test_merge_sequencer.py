"""Tests for the global merge sequencing engine (Phase 1.1)."""
from src.merge_sequencer import (
    MergeEdge,
    MRNode,
    compute_merge_plan,
)


def nodes(*specs) -> list[MRNode]:
    """specs: (mr_id, iid, project)."""
    return [MRNode(i, iid, proj, f"MR {iid}") for (i, iid, proj) in specs]


class TestMergePlan:
    def test_empty(self):
        plan = compute_merge_plan([], [])
        assert plan.steps == []
        assert plan.cycles == []
        assert plan.independent == []

    def test_all_independent(self):
        mrs = nodes((1, 1, "g/a"), (2, 2, "g/b"))
        plan = compute_merge_plan(mrs, [])
        assert plan.steps == []
        assert {m["mr_id"] for m in plan.independent} == {1, 2}

    def test_simple_linear_order(self):
        # caller (2) must merge before changer (1)
        mrs = nodes((1, 10, "g/payments"), (2, 7, "g/notif"))
        edges = [MergeEdge(before_id=2, after_id=1, symbol="charge_user")]
        plan = compute_merge_plan(mrs, edges)
        order = [s.mr_id for s in plan.steps]
        assert order == [2, 1]
        assert plan.cycles == []
        assert plan.steps[0].unblocks == [1]
        assert plan.steps[1].waits_on == [2]

    def test_fan_out_two_callers_before_changer(self):
        # !7 and !14 both call charge_user changed by !23 → both before !23
        mrs = nodes((23, 23, "g/payments"), (7, 7, "g/notif"), (14, 14, "g/billing"))
        edges = [
            MergeEdge(7, 23, "charge_user"),
            MergeEdge(14, 23, "charge_user"),
        ]
        plan = compute_merge_plan(mrs, edges)
        order = [s.mr_iid for s in plan.steps]
        # changer (!23) must be last; callers ordered by iid first
        assert order[-1] == 23
        assert set(order[:2]) == {7, 14}
        assert order[:2] == [7, 14]  # deterministic tie-break by iid

    def test_chain(self):
        # 1 before 2 before 3
        mrs = nodes((1, 1, "g/a"), (2, 2, "g/b"), (3, 3, "g/c"))
        edges = [MergeEdge(1, 2, "x"), MergeEdge(2, 3, "y")]
        plan = compute_merge_plan(mrs, edges)
        assert [s.mr_id for s in plan.steps] == [1, 2, 3]

    def test_cycle_detected_as_coordination_group(self):
        # 1 and 2 mutually collide → cycle
        mrs = nodes((1, 1, "g/a"), (2, 2, "g/b"))
        edges = [MergeEdge(1, 2, "foo"), MergeEdge(2, 1, "bar")]
        plan = compute_merge_plan(mrs, edges)
        assert len(plan.cycles) == 1
        group = plan.cycles[0]
        assert set(group.mr_ids) == {1, 2}
        assert "foo" in group.symbols and "bar" in group.symbols
        # cyclic nodes are not emitted as linear steps
        assert plan.steps == []

    def test_cycle_plus_independent_chain(self):
        mrs = nodes((1, 1, "g/a"), (2, 2, "g/b"), (3, 3, "g/c"), (4, 4, "g/d"))
        edges = [
            MergeEdge(1, 2, "foo"), MergeEdge(2, 1, "bar"),  # cycle 1<->2
            MergeEdge(3, 4, "baz"),                           # chain 3->4
        ]
        plan = compute_merge_plan(mrs, edges)
        assert len(plan.cycles) == 1
        assert [s.mr_id for s in plan.steps] == [3, 4]

    def test_self_loop_ignored(self):
        mrs = nodes((1, 1, "g/a"))
        edges = [MergeEdge(1, 1, "x")]
        plan = compute_merge_plan(mrs, edges)
        assert plan.cycles == []
        assert {m["mr_id"] for m in plan.independent} == {1}

    def test_summary_text(self):
        mrs = nodes((1, 10, "g/payments"), (2, 7, "g/notif"))
        edges = [MergeEdge(2, 1, "charge_user")]
        plan = compute_merge_plan(mrs, edges)
        text = plan.summary_text()
        assert "!7" in text and "!10" in text
