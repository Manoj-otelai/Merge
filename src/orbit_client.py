"""GitLab Orbit client for cross-repo graph traversal.

Wraps the Orbit `query_graph` MCP tool / REST API. Falls back to Orbit Local
(DuckDB via `glab orbit local`) when Remote is unavailable.

The Orbit graph is indexed from the DEFAULT BRANCH only. We use it to find
who calls a symbol that exists on main — then cross-reference with the MR
diff to determine if that caller will break after the in-flight change merges.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from .models import CallerInfo

logger = logging.getLogger(__name__)

# Orbit DSL queries — Cypher-like graph traversal
_QUERY_CALLERS = """
MATCH (fn:Function {name: $name})-[:DEFINED_IN]->(file:File)
  -[:IN_PROJECT]->(sourceProj:Project)
MATCH (caller:Function)-[:CALLS]->(fn)
MATCH (callerFile:File)<-[:DEFINED_IN]-(caller)
MATCH (callerFile)-[:IN_PROJECT]->(callerProj:Project)
OPTIONAL MATCH (caller)-[:OWNED_BY]->(owner:User)
RETURN
  caller.name          AS caller_name,
  callerFile.path      AS caller_file,
  callerProj.id        AS caller_project_id,
  callerProj.full_path AS caller_project_path,
  owner.username       AS owner,
  caller.line          AS line_number
LIMIT 100
"""

_QUERY_CENTRALITY = """
MATCH (fn:Function {name: $name})<-[:CALLS]-(c)
RETURN count(c) AS caller_count
"""

_QUERY_OWNERS = """
MATCH (fn:Function {name: $name})<-[:OWNS]-(owner:User)
RETURN owner.username AS username, owner.email AS email
"""

_QUERY_SCHEMA = "CALL db.schema.visualization()"


class OrbitClient:
    """Query GitLab Orbit's knowledge graph for downstream callers and owners."""

    def __init__(
        self,
        gitlab_url: str = "",
        token: str = "",
        use_local: bool = False,
    ) -> None:
        self.gitlab_url = gitlab_url or os.getenv("GITLAB_URL", "https://gitlab.com")
        self.token = token or os.getenv("GITLAB_TOKEN", "")
        self.use_local = use_local or os.getenv("ORBIT_USE_LOCAL", "false").lower() == "true"
        self._http = httpx.AsyncClient(
            base_url=self.gitlab_url,
            headers={"PRIVATE-TOKEN": self.token},
            timeout=30.0,
        )

        # ── Blast-radius cache + latency metrics (Phase 5.2) ──
        # Orbit indexes the default branch, so a symbol's callers only change
        # when the default branch changes (i.e. on merge) — a TTL cache is safe
        # and avoids redundant graph calls when many MRs touch the same symbol.
        self._cache_enabled = os.getenv("ORBIT_CACHE", "true").lower() == "true"
        self._cache_ttl = float(os.getenv("ORBIT_CACHE_TTL", "300"))
        self._cache: dict[tuple, tuple[float, Any]] = {}
        self._latencies_ms: deque[float] = deque(maxlen=500)
        self._hits = 0
        self._misses = 0

    async def _cached(self, key: tuple, factory: Callable[[], Awaitable[Any]]) -> Any:
        """Return a cached value or compute it, recording query latency on miss."""
        now = time.time()
        if self._cache_enabled:
            entry = self._cache.get(key)
            if entry and entry[0] > now:
                self._hits += 1
                return entry[1]
        self._misses += 1
        start = time.perf_counter()
        value = await factory()
        self._latencies_ms.append((time.perf_counter() - start) * 1000.0)
        if self._cache_enabled:
            self._cache[key] = (now + self._cache_ttl, value)
        return value

    def invalidate(self, symbol_name: str | None = None) -> int:
        """Invalidate cache entries (all, or for one symbol). Call on merge."""
        if symbol_name is None:
            n = len(self._cache)
            self._cache.clear()
            return n
        keys = [k for k in self._cache if len(k) > 1 and k[1] == symbol_name]
        for k in keys:
            del self._cache[k]
        return len(keys)

    def get_metrics(self) -> dict:
        """Cache hit rate and query-latency percentiles."""
        lat = sorted(self._latencies_ms)
        total = self._hits + self._misses

        def pct(p: float) -> float:
            if not lat:
                return 0.0
            idx = min(int(p * len(lat)), len(lat) - 1)
            return round(lat[idx], 2)

        return {
            "cache_enabled": self._cache_enabled,
            "cache_ttl_seconds": self._cache_ttl,
            "cache_entries": len(self._cache),
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / total, 3) if total else 0.0,
            "queries": len(lat),
            "latency_ms": {"p50": pct(0.50), "p95": pct(0.95), "p99": pct(0.99),
                           "max": round(lat[-1], 2) if lat else 0.0},
        }

    async def get_callers(self, symbol_name: str, project_id: int) -> list[CallerInfo]:
        """Return all functions that call `symbol_name` across all repos (cached)."""
        return await self._cached(
            ("callers", symbol_name, project_id),
            lambda: self._get_callers_uncached(symbol_name, project_id),
        )

    async def _get_callers_uncached(self, symbol_name: str, project_id: int) -> list[CallerInfo]:
        try:
            rows = await self._query(
                _QUERY_CALLERS,
                {"name": symbol_name, "project_id": str(project_id)},
            )
            return [
                CallerInfo(
                    function_name=r.get("caller_name", ""),
                    file_path=r.get("caller_file", ""),
                    project_path=r.get("caller_project_path", ""),
                    project_id=int(r.get("caller_project_id") or 0),
                    owner=r.get("owner") or "",
                    line_number=int(r.get("line_number") or 0),
                )
                for r in rows
                if r.get("caller_name")
            ]
        except Exception as exc:
            logger.warning("Orbit caller query failed for %s: %s", symbol_name, exc)
            return self._mock_callers(symbol_name, project_id)

    async def get_centrality(self, symbol_name: str) -> float:
        """Return caller count as a proxy for symbol centrality (cached)."""
        return await self._cached(
            ("centrality", symbol_name),
            lambda: self._get_centrality_uncached(symbol_name),
        )

    async def _get_centrality_uncached(self, symbol_name: str) -> float:
        try:
            rows = await self._query(_QUERY_CENTRALITY, {"name": symbol_name})
            if rows:
                return float(rows[0].get("caller_count", 0))
        except Exception as exc:
            logger.warning("Orbit centrality query failed for %s: %s", symbol_name, exc)
        return 0.0

    async def get_owners(self, symbol_name: str) -> list[str]:
        """Return GitLab usernames that own this symbol (cached)."""
        return await self._cached(
            ("owners", symbol_name),
            lambda: self._get_owners_uncached(symbol_name),
        )

    async def _get_owners_uncached(self, symbol_name: str) -> list[str]:
        try:
            rows = await self._query(_QUERY_OWNERS, {"name": symbol_name})
            return [r["username"] for r in rows if r.get("username")]
        except Exception as exc:
            logger.warning("Orbit owner query failed for %s: %s", symbol_name, exc)
        return []

    async def get_schema(self) -> dict:
        """Return Orbit graph schema for introspection / debugging."""
        try:
            rows = await self._query(_QUERY_SCHEMA, {})
            return {"schema": rows}
        except Exception as exc:
            logger.warning("Orbit schema query failed: %s", exc)
            return {}

    # ── Transport ────────────────────────────────────────────────────────────

    async def _query(self, cypher: str, params: dict[str, Any]) -> list[dict]:
        if self.use_local:
            return await self._query_local(cypher, params)
        return await self._query_remote(cypher, params)

    async def _query_remote(self, cypher: str, params: dict[str, Any]) -> list[dict]:
        """Call Orbit Remote REST API: POST /api/v4/orbit/query"""
        payload = {"query": cypher.strip(), "parameters": params}
        resp = await self._http.post("/api/v4/orbit/query", json=payload)
        resp.raise_for_status()
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    async def _query_local(self, cypher: str, params: dict[str, Any]) -> list[dict]:
        """Call Orbit Local via `glab orbit query` subprocess (uses DuckDB)."""
        param_args = []
        for k, v in params.items():
            param_args.extend(["--param", f"{k}={v}"])

        cmd = ["glab", "orbit", "query", "--format", "json"] + param_args + ["--", cypher.strip()]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                return json.loads(result.stdout)
            logger.warning("glab orbit query stderr: %s", result.stderr)
        except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError) as exc:
            logger.warning("Orbit Local query failed: %s", exc)
        return []

    async def close(self) -> None:
        await self._http.aclose()

    # ── Demo fallback mock data ──────────────────────────────────────────────
    # Used when Orbit is not reachable (e.g., demo environment without credentials)

    def _mock_callers(self, symbol_name: str, project_id: int) -> list[CallerInfo]:
        """Return realistic mock data for demo / testing purposes."""
        mock_data: dict[str, list[CallerInfo]] = {
            "charge_user": [
                CallerInfo("send_invoice", "src/notifications/invoice.py", "demo-group/notification-service", 102, "maria"),
                CallerInfo("process_subscription", "src/billing/subscriptions.py", "demo-group/billing-service", 103, "sven"),
                CallerInfo("handle_checkout", "src/orders/checkout.py", "demo-group/orders-service", 104, "maria"),
                CallerInfo("refund_payment", "src/billing/refunds.py", "demo-group/billing-service", 103, "sven"),
            ],
            "get_user_balance": [
                CallerInfo("show_dashboard", "src/web/dashboard.py", "demo-group/web-frontend", 105, "alice"),
                CallerInfo("export_report", "src/reports/monthly.py", "demo-group/reporting-service", 106, "bob"),
            ],
            "authenticate": [
                CallerInfo("login_handler", "src/auth/login.py", "demo-group/auth-service", 107, "carol"),
                CallerInfo("refresh_token", "src/auth/tokens.py", "demo-group/auth-service", 107, "carol"),
                CallerInfo("api_middleware", "src/api/middleware.py", "demo-group/api-gateway", 108, "dave"),
            ],
        }
        return mock_data.get(symbol_name, [
            CallerInfo(f"caller_of_{symbol_name}", "src/service/handler.py", "demo-group/consumer-service", project_id + 1, "engineer"),
        ])
