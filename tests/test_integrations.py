"""Tests for Phase 4 integrations: Slack, Ask MergeGuard."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src import ask as ask_module
from src.collision_engine import CollisionEngine
from src.slack_client import SlackNotifier
from tests.test_collision_engine import make_blast_radius, make_caller, make_symbol


def seeded_engine(tmp_path):
    engine = CollisionEngine(str(tmp_path / "i.db"))
    mr_a = make_blast_radius(
        1, 23, "group/payments",
        [make_symbol("charge_user")],
        [make_caller("send_invoice", "group/notifications"),
         make_caller("process_sub", "group/billing")],
    )
    mr_b = make_blast_radius(2, 7, "group/notifications", [], [make_caller("charge_user", "group/payments")])
    engine.register_mr(mr_a)
    engine.register_mr(mr_b)
    engine.record_collisions(engine.find_collisions(1))
    return engine


# ── Slack ────────────────────────────────────────────────────────────────────

class TestSlack:
    def test_disabled_without_token(self):
        n = SlackNotifier(token="", channel="")
        assert not n.enabled

    def test_enabled_with_token_and_channel(self):
        n = SlackNotifier(token="xoxb-test", channel="#eng")
        assert n.enabled

    def test_threshold_filtering(self):
        n = SlackNotifier(token="x", channel="#c", min_severity="critical")
        assert n._meets_threshold("critical")
        assert not n._meets_threshold("high")
        n2 = SlackNotifier(token="x", channel="#c", min_severity="high")
        assert n2._meets_threshold("high")
        assert not n2._meets_threshold("medium")

    @pytest.mark.asyncio
    async def test_noop_when_disabled(self, tmp_path):
        engine = seeded_engine(tmp_path)
        collisions = engine.find_collisions(1)
        n = SlackNotifier(token="", channel="")
        result = await n.notify_collisions(collisions)
        assert result.sent == 0
        engine.close()

    @pytest.mark.asyncio
    async def test_posts_when_enabled(self, tmp_path):
        engine = seeded_engine(tmp_path)
        collisions = engine.find_collisions(1)
        n = SlackNotifier(token="xoxb", channel="#eng", min_severity="low")
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        with patch.object(n._http, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_resp
            result = await n.notify_collisions(collisions, map_url="http://x")
        assert result.sent >= 1
        assert mock_post.called
        engine.close()

    def test_build_blocks_shape(self, tmp_path):
        engine = seeded_engine(tmp_path)
        c = engine.find_collisions(1)[0]
        n = SlackNotifier(token="x", channel="#c")
        blocks = n.build_blocks(c, map_url="http://map")
        assert blocks[0]["type"] == "header"
        assert any(b["type"] == "section" for b in blocks)
        engine.close()


# ── Ask MergeGuard ───────────────────────────────────────────────────────────

class TestAsk:
    def test_what_breaks(self, tmp_path):
        engine = seeded_engine(tmp_path)
        res = ask_module.answer("what does !23 break?", engine)
        assert res["intent"] == "what_breaks"
        assert "charge_user" in res["answer"]
        engine.close()

    def test_safe_to_merge(self, tmp_path):
        engine = seeded_engine(tmp_path)
        res = ask_module.answer("which of my open MRs are safe to merge today?", engine)
        assert res["intent"] == "safe_to_merge"
        engine.close()

    def test_who_to_coordinate(self, tmp_path):
        engine = seeded_engine(tmp_path)
        res = ask_module.answer("who do I need to coordinate with to merge !23?", engine)
        assert res["intent"] == "who_to_coordinate"
        engine.close()

    def test_riskiest(self, tmp_path):
        engine = seeded_engine(tmp_path)
        res = ask_module.answer("what's the riskiest collision?", engine)
        assert res["intent"] == "riskiest"
        assert "charge_user" in res["answer"]
        engine.close()

    def test_savings(self, tmp_path):
        engine = seeded_engine(tmp_path)
        res = ask_module.answer("how much has mergeguard saved us?", engine)
        assert res["intent"] == "savings"
        assert res["data"]["cost_saved"]["dollars"] > 0
        engine.close()

    def test_merge_plan(self, tmp_path):
        engine = seeded_engine(tmp_path)
        res = ask_module.answer("what's the merge order?", engine)
        assert res["intent"] == "merge_plan"
        engine.close()

    def test_unknown_mr(self, tmp_path):
        engine = seeded_engine(tmp_path)
        res = ask_module.answer("what does !999 break?", engine)
        assert "don't have" in res["answer"].lower()
        engine.close()

    def test_overview_fallback(self, tmp_path):
        engine = seeded_engine(tmp_path)
        res = ask_module.answer("hello there", engine)
        assert res["intent"] == "overview"
        engine.close()
