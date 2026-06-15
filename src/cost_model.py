"""Cost & time-saved estimation for prevented collisions (Phase 2.1).

Every collision MergeGuard surfaces *before* merge is an incident that didn't
happen. This module turns each prevented collision into an estimate of engineer
hours and dollars saved, using fully transparent, tunable assumptions kept in a
single JSON config (`config/cost_model.json`, override via MERGEGUARD_COST_MODEL).

The estimate is deliberately conservative and explainable:

    expected_rework_hours = base
                          + per_caller × caller_count
                          + per_extra_team × (teams - 1)

    incident_cost = P(reaches prod | severity)
                  × ( downtime_minutes(severity) × downtime_cost_per_minute
                      + expected_rework_hours × engineer_hourly_cost )

    hours_saved   = P(reaches prod | severity) × expected_rework_hours

Catching it pre-merge saves the *expected* incident cost.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULTS = {
    "currency": "USD",
    "engineer_hourly_cost": 120,
    "downtime_cost_per_minute": 5000,
    "rework_hours_base": 4.0,
    "rework_hours_per_caller": 0.5,
    "coordination_hours_per_extra_team": 2.0,
    "expected_downtime_minutes_by_severity": {"critical": 90, "high": 45, "medium": 15, "low": 2},
    "probability_reaches_prod_by_severity": {"critical": 0.70, "high": 0.50, "medium": 0.25, "low": 0.05},
}

_DEFAULT_PATH = Path(__file__).parent.parent / "config" / "cost_model.json"


@dataclass
class Savings:
    dollars: float
    hours: float
    breakdown: dict

    def to_dict(self) -> dict:
        return {
            "dollars": round(self.dollars, 2),
            "hours": round(self.hours, 2),
            "breakdown": self.breakdown,
        }


class CostModel:
    def __init__(self, assumptions: dict | None = None) -> None:
        self.a = {**_DEFAULTS, **(assumptions or {})}

    @classmethod
    def load(cls, path: str | None = None) -> CostModel:
        p = Path(path or os.getenv("MERGEGUARD_COST_MODEL", str(_DEFAULT_PATH)))
        try:
            data = json.loads(p.read_text())
            data.pop("_comment", None)
            return cls(data)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Cost model load failed (%s); using defaults", exc)
            return cls()

    @property
    def currency(self) -> str:
        return self.a["currency"]

    def estimate(self, severity_label: str, caller_count: int, distinct_teams: int) -> Savings:
        """Estimate dollars + engineer-hours saved by preventing one collision."""
        sev = severity_label if severity_label in self.a["probability_reaches_prod_by_severity"] else "low"
        p_prod = float(self.a["probability_reaches_prod_by_severity"][sev])
        downtime_min = float(self.a["expected_downtime_minutes_by_severity"][sev])

        rework_hours = (
            float(self.a["rework_hours_base"])
            + float(self.a["rework_hours_per_caller"]) * max(caller_count, 0)
            + float(self.a["coordination_hours_per_extra_team"]) * max(distinct_teams - 1, 0)
        )

        downtime_cost = downtime_min * float(self.a["downtime_cost_per_minute"])
        rework_cost = rework_hours * float(self.a["engineer_hourly_cost"])

        dollars = p_prod * (downtime_cost + rework_cost)
        hours = p_prod * rework_hours

        return Savings(
            dollars=dollars,
            hours=hours,
            breakdown={
                "severity": sev,
                "p_reaches_prod": p_prod,
                "expected_downtime_minutes": downtime_min,
                "downtime_cost_at_full": round(downtime_cost, 2),
                "rework_hours_at_full": round(rework_hours, 2),
                "rework_cost_at_full": round(rework_cost, 2),
                "caller_count": caller_count,
                "distinct_teams": distinct_teams,
            },
        )
