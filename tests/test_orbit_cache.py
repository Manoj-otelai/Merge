"""Tests for Orbit query caching + latency metrics (Phase 5.2)."""
from unittest.mock import AsyncMock, patch

import pytest

from src.orbit_client import OrbitClient
from tests.fixtures.sample_orbit_responses import CALLERS_OF_CHARGE_USER, CENTRALITY_CHARGE_USER


@pytest.fixture
def orbit():
    return OrbitClient(gitlab_url="https://gitlab.example.com", token="t")


class TestCache:
    @pytest.mark.asyncio
    async def test_second_call_is_cached(self, orbit):
        with patch.object(orbit, "_query", new_callable=AsyncMock) as mq:
            mq.return_value = CALLERS_OF_CHARGE_USER
            await orbit.get_callers("charge_user", 101)
            await orbit.get_callers("charge_user", 101)
        # Underlying query invoked only once despite two calls
        assert mq.call_count == 1
        m = orbit.get_metrics()
        assert m["hits"] == 1
        assert m["misses"] == 1
        assert m["hit_rate"] == 0.5

    @pytest.mark.asyncio
    async def test_different_args_not_cached_together(self, orbit):
        with patch.object(orbit, "_query", new_callable=AsyncMock) as mq:
            mq.return_value = CALLERS_OF_CHARGE_USER
            await orbit.get_callers("charge_user", 101)
            await orbit.get_callers("charge_user", 202)
        assert mq.call_count == 2

    @pytest.mark.asyncio
    async def test_invalidate_all(self, orbit):
        with patch.object(orbit, "_query", new_callable=AsyncMock) as mq:
            mq.return_value = CALLERS_OF_CHARGE_USER
            await orbit.get_callers("charge_user", 101)
            n = orbit.invalidate()
            assert n >= 1
            await orbit.get_callers("charge_user", 101)
        assert mq.call_count == 2

    @pytest.mark.asyncio
    async def test_invalidate_by_symbol(self, orbit):
        with patch.object(orbit, "_query", new_callable=AsyncMock) as mq:
            mq.return_value = CENTRALITY_CHARGE_USER
            await orbit.get_centrality("charge_user")
            await orbit.get_centrality("other_fn")
            removed = orbit.invalidate("charge_user")
        assert removed == 1

    @pytest.mark.asyncio
    async def test_cache_can_be_disabled(self):
        o = OrbitClient(gitlab_url="x", token="t")
        o._cache_enabled = False
        with patch.object(o, "_query", new_callable=AsyncMock) as mq:
            mq.return_value = CENTRALITY_CHARGE_USER
            await o.get_centrality("charge_user")
            await o.get_centrality("charge_user")
        assert mq.call_count == 2

    @pytest.mark.asyncio
    async def test_metrics_have_latency_percentiles(self, orbit):
        with patch.object(orbit, "_query", new_callable=AsyncMock) as mq:
            mq.return_value = CENTRALITY_CHARGE_USER
            await orbit.get_centrality("a")
            await orbit.get_centrality("b")
        m = orbit.get_metrics()
        assert "p95" in m["latency_ms"]
        assert m["queries"] == 2
