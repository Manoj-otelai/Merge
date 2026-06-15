"""Tests for the cost model and analytics (Phase 2)."""
from src.cost_model import CostModel
from src.collision_engine import CollisionEngine
from tests.test_collision_engine import make_blast_radius, make_caller, make_symbol


class TestCostModel:
    def test_defaults_load(self):
        cm = CostModel()
        assert cm.currency == "USD"

    def test_higher_severity_saves_more(self):
        cm = CostModel()
        high = cm.estimate("high", 8, 3)
        low = cm.estimate("low", 8, 3)
        assert high.dollars > low.dollars
        assert high.hours > low.hours

    def test_more_callers_more_hours(self):
        cm = CostModel()
        few = cm.estimate("high", 2, 1)
        many = cm.estimate("high", 20, 1)
        assert many.hours > few.hours

    def test_breakdown_is_transparent(self):
        cm = CostModel()
        s = cm.estimate("critical", 12, 3)
        assert s.breakdown["p_reaches_prod"] == 0.70
        assert s.breakdown["caller_count"] == 12
        assert "rework_hours_at_full" in s.breakdown

    def test_unknown_severity_falls_back_to_low(self):
        cm = CostModel()
        s = cm.estimate("bogus", 1, 1)
        assert s.breakdown["severity"] == "low"

    def test_custom_assumptions_override(self):
        cm = CostModel({"engineer_hourly_cost": 1000})
        base = CostModel({"engineer_hourly_cost": 1})
        assert cm.estimate("high", 5, 2).dollars > base.estimate("high", 5, 2).dollars


class TestAnalytics:
    def _seed(self, engine):
        mr_a = make_blast_radius(
            1, 1, "group/payments",
            [make_symbol("charge_user")],
            [make_caller("send_invoice", "group/notifications"),
             make_caller("process_sub", "group/billing")],
        )
        mr_b = make_blast_radius(2, 2, "group/notifications", [], [make_caller("charge_user", "group/payments")])
        engine.register_mr(mr_a)
        engine.register_mr(mr_b)
        engine.record_collisions(engine.find_collisions(1))

    def test_records_and_aggregates(self, tmp_path):
        engine = CollisionEngine(str(tmp_path / "a.db"))
        self._seed(engine)
        a = engine.get_analytics()
        assert a["totals"]["total_collisions"] >= 1
        assert a["cost_saved"]["dollars"] > 0
        assert a["cost_saved"]["engineer_hours"] > 0
        engine.close()

    def test_idempotent_recording(self, tmp_path):
        engine = CollisionEngine(str(tmp_path / "b.db"))
        self._seed(engine)
        before = engine.get_analytics()["totals"]["total_collisions"]
        # Re-record the same collisions — should not duplicate
        engine.record_collisions(engine.find_collisions(1))
        after = engine.get_analytics()["totals"]["total_collisions"]
        assert before == after
        engine.close()

    def test_resolution_marks_resolved(self, tmp_path):
        engine = CollisionEngine(str(tmp_path / "c.db"))
        self._seed(engine)
        engine.mark_resolved_for_mr(1)
        a = engine.get_analytics()
        assert a["totals"]["resolved"] >= 1
        assert a["totals"]["active"] == a["totals"]["total_collisions"] - a["totals"]["resolved"]
        engine.close()

    def test_owner_load_and_hotspots(self, tmp_path):
        engine = CollisionEngine(str(tmp_path / "d.db"))
        self._seed(engine)
        a = engine.get_analytics()
        assert isinstance(a["owner_load"], list)
        assert isinstance(a["hotspot_projects"], list)
        engine.close()
