"""Tests for the Orbit client (using mock responses)."""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.orbit_client import OrbitClient
from src.models import CallerInfo
from tests.fixtures.sample_orbit_responses import (
    CALLERS_OF_CHARGE_USER,
    CENTRALITY_CHARGE_USER,
    OWNERS_CHARGE_USER,
    EMPTY_CALLERS,
)


@pytest.fixture
def orbit():
    return OrbitClient(gitlab_url="https://gitlab.example.com", token="test-token")


class TestOrbitClientMock:
    def test_mock_callers_returned_on_orbit_failure(self, orbit):
        """When Orbit Remote fails, mock data is returned for known symbols."""
        callers = orbit._mock_callers("charge_user", 101)
        assert len(callers) > 0
        assert all(isinstance(c, CallerInfo) for c in callers)
        assert any(c.function_name == "send_invoice" for c in callers)

    def test_mock_callers_fallback_for_unknown_symbol(self, orbit):
        callers = orbit._mock_callers("completely_unknown_fn", 101)
        assert len(callers) >= 1

    @pytest.mark.asyncio
    async def test_get_callers_returns_caller_info_objects(self, orbit):
        """Callers from Orbit rows are correctly mapped to CallerInfo."""
        with patch.object(orbit, "_query", new_callable=AsyncMock) as mock_query:
            mock_query.return_value = CALLERS_OF_CHARGE_USER
            callers = await orbit.get_callers("charge_user", 101)

        assert len(callers) == 4
        names = {c.function_name for c in callers}
        assert "send_invoice" in names
        assert "process_subscription" in names

    @pytest.mark.asyncio
    async def test_get_centrality_returns_float(self, orbit):
        with patch.object(orbit, "_query", new_callable=AsyncMock) as mock_query:
            mock_query.return_value = CENTRALITY_CHARGE_USER
            result = await orbit.get_centrality("charge_user")

        assert result == 12.0

    @pytest.mark.asyncio
    async def test_get_centrality_returns_zero_on_empty(self, orbit):
        with patch.object(orbit, "_query", new_callable=AsyncMock) as mock_query:
            mock_query.return_value = []
            result = await orbit.get_centrality("nonexistent")

        assert result == 0.0

    @pytest.mark.asyncio
    async def test_get_owners_returns_usernames(self, orbit):
        with patch.object(orbit, "_query", new_callable=AsyncMock) as mock_query:
            mock_query.return_value = OWNERS_CHARGE_USER
            owners = await orbit.get_owners("charge_user")

        assert "maria" in owners
        assert "sven" in owners

    @pytest.mark.asyncio
    async def test_get_callers_falls_back_on_exception(self, orbit):
        with patch.object(orbit, "_query", new_callable=AsyncMock) as mock_query:
            mock_query.side_effect = Exception("Orbit unavailable")
            callers = await orbit.get_callers("charge_user", 101)

        # Should fall back to mock data
        assert len(callers) > 0

    @pytest.mark.asyncio
    async def test_query_remote_uses_correct_endpoint(self, orbit):
        """Verify the remote query hits /api/v4/orbit/query."""
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"data": []}

        with patch.object(orbit._http, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response
            await orbit._query_remote("MATCH (n) RETURN n", {})

        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert "/api/v4/orbit/query" in call_args[0][0]
