"""Seed the MergeGuard collision registry with a deterministic demo scenario.

Populates the SQLite DB directly so the UI and API can be demoed without a live
GitLab or Orbit connection. Run this, then start the server pointed at the same
DB to see a fully populated collision map.

    python -m demo.seed_local                 # seeds mergeguard.db
    MERGEGUARD_DB=demo.db python -m demo.seed_local

Scenario: three open MRs across a payments platform.
  !23 payments-service     changes  charge_user(amount) -> charge_user(amount, currency)
  !7  notification-service calls    charge_user(amount)            ← collides with !23
  !14 billing-service      changes  authenticate(user, pwd) -> authenticate(user, pwd, mfa)
                           and calls charge_user(amount)            ← collides with !23 too
"""
from __future__ import annotations

import os
import sys

# Allow running as a script from the repo root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.collision_engine import CollisionEngine
from src.models import (
    BlastRadius,
    CallerInfo,
    ChangeType,
    Language,
    SymbolChange,
)

GITLAB = "https://gitlab.com"
GROUP = "demo-group"


def mr_url(project: str, iid: int) -> str:
    return f"{GITLAB}/{GROUP}/{project}/-/merge_requests/{iid}"


def seed(db_path: str) -> None:
    engine = CollisionEngine(db_path)

    # Clear any previous demo state for determinism
    for mr_id in (1001, 1002, 1003):
        engine.close_mr(mr_id)

    # ── MR !23 — payments-service: charge_user gets a currency parameter ──────
    payments = BlastRadius(
        mr_id=1001,
        mr_iid=23,
        mr_url=mr_url("payments-service", 23),
        mr_title="feat: add currency support to charge_user",
        project_id=101,
        project_path=f"{GROUP}/payments-service",
        author="dmitri",
        source_branch="feature/add-currency-support",
        changed_symbols=[
            SymbolChange(
                name="charge_user",
                old_signature="def charge_user(amount: float) -> bool",
                new_signature="def charge_user(amount: float, currency: str = 'USD') -> bool",
                file_path="src/payments/charge.py",
                language=Language.PYTHON,
                change_type=ChangeType.SIGNATURE_CHANGED,
            ),
        ],
        downstream_callers=[
            CallerInfo("send_invoice", "src/notifications/invoice.py", f"{GROUP}/notification-service", 102, "maria"),
            CallerInfo("send_subscription_renewal", "src/notifications/invoice.py", f"{GROUP}/notification-service", 102, "maria"),
            CallerInfo("process_subscription", "src/billing/subscriptions.py", f"{GROUP}/billing-service", 103, "sven"),
            CallerInfo("handle_checkout", "src/orders/checkout.py", f"{GROUP}/orders-service", 104, "priya"),
        ],
        affected_owners=["maria", "sven", "priya"],
        centrality_score=12.0,
    )

    # ── MR !7 — notification-service: adds callers of the OLD charge_user ─────
    notifications = BlastRadius(
        mr_id=1002,
        mr_iid=7,
        mr_url=mr_url("notification-service", 7),
        mr_title="feat: add invoice notification and subscription renewal",
        project_id=102,
        project_path=f"{GROUP}/notification-service",
        author="maria",
        source_branch="feature/invoice-notifications",
        changed_symbols=[],  # no signature changes — only new callers
        downstream_callers=[
            # This MR's code calls charge_user with the current (old) signature
            CallerInfo("charge_user", "src/payments/charge.py", f"{GROUP}/payments-service", 101, "dmitri"),
        ],
        affected_owners=["maria"],
        centrality_score=0.0,
    )

    # ── MR !14 — billing-service: changes authenticate AND calls charge_user ──
    billing = BlastRadius(
        mr_id=1003,
        mr_iid=14,
        mr_url=mr_url("billing-service", 14),
        mr_title="feat: require MFA on authenticate; wire up renewal billing",
        project_id=103,
        project_path=f"{GROUP}/billing-service",
        author="sven",
        source_branch="feature/mfa-and-billing",
        changed_symbols=[
            SymbolChange(
                name="authenticate",
                old_signature="def authenticate(username: str, password: str) -> bool",
                new_signature="def authenticate(username: str, password: str, mfa_code: str = '') -> bool",
                file_path="src/auth/session.py",
                language=Language.PYTHON,
                change_type=ChangeType.SIGNATURE_CHANGED,
            ),
        ],
        downstream_callers=[
            # billing calls charge_user too → collides with !23
            CallerInfo("charge_user", "src/payments/charge.py", f"{GROUP}/payments-service", 101, "dmitri"),
            # callers of authenticate (relevant if another MR depended on it)
            CallerInfo("login_handler", "src/auth/login.py", f"{GROUP}/auth-service", 107, "carol"),
            CallerInfo("api_middleware", "src/api/middleware.py", f"{GROUP}/api-gateway", 108, "dave"),
        ],
        affected_owners=["dmitri", "carol", "dave"],
        centrality_score=9.0,
    )

    for br in (payments, notifications, billing):
        engine.register_mr(br)

    cmap = engine.get_collision_map()
    print(f"✓ Seeded {db_path}")
    print(f"  Open MRs:   {cmap.open_mrs}")
    print(f"  Collisions: {cmap.total_collisions}")
    for edge in cmap.edges:
        print(f"    ⚡ {edge['symbol']:<14} {edge['severity_label']:<8} "
              f"(MR {edge['source']} ↔ MR {edge['target']})")
    engine.close()


if __name__ == "__main__":
    db = os.getenv("MERGEGUARD_DB", "mergeguard.db")
    seed(db)
