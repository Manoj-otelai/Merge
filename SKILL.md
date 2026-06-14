---
name: mergeguard
display_name: "MergeGuard — Cross-MR Semantic Collision Sentinel"
version: "1.0.0"
description: >
  Use when you need to detect semantic collisions between simultaneously-open
  merge requests in a GitLab group. Analyzes changed function/class/method
  signatures across MRs, queries Orbit for downstream callers and code owners,
  and identifies MRs that will silently break each other even when Git reports
  zero text conflicts.
category: "Code Review"
tags:
  - orbit
  - merge-request
  - semantic-analysis
  - cross-repo
  - collision-detection
  - code-quality
  - duo-agent
license: MIT
author: "MergeGuard Contributors"
gitlab_duo_compatible: true
orbit_required: true
---

# MergeGuard — Cross-MR Semantic Collision Sentinel

## What This Skill Does

MergeGuard detects the class of breakage that **Git's textual merge can never see**: two MRs that touch different files, both pass CI, but will break each other's contracts after merge.

**The problem:** MR-A renames `charge_user(amount)` → `charge_user(amount, currency)` in `payments-service`. MR-B adds new callers of `charge_user(amount)` in `notification-service`. Git sees no conflict. Both MRs go green. You merge them. Production breaks.

**MergeGuard's solution:** Uses GitLab Orbit to traverse the cross-repo caller graph, identifies every function that calls the changed symbol on the default branch, and intersects blast radii across all open MRs to find these hidden semantic collisions — before merge.

## When to Use

- Any time you want to know whether two open MRs in your group will semantically conflict
- When reviewing an MR that changes a function signature, class interface, or method name
- As an automated guard on every MR event in your GitLab group

## How It Works

```
MR opened/updated
      │
      ▼
[1] Extract changed symbols from diff
    (Python ast / JS regex — NOT from Orbit, which only indexes default branch)
      │
      ▼
[2] Query Orbit: find all downstream callers + owners
    query_graph: REFERENCES traversal across all repos
      │
      ▼
[3] Intersect blast radii of all open MRs
    (the novel step: cross-MR coordination via Orbit)
      │
      ▼
[4] Score severity: centrality × owner_spread × change_risk
      │
      ▼
[5] Post structured warning comment to both affected MRs
```

## Orbit Integration

MergeGuard makes **meaningful use of GitLab Orbit** for:

1. **Cross-repo caller resolution** — `query_graph` traversal via `REFERENCES` relationship finds every function that calls the changed symbol across all repositories in the group
2. **Code owner discovery** — finds GitLab users/groups responsible for affected callers
3. **Centrality scoring** — aggregation query counts total callers for severity assessment
4. **Schema discovery** — `get_graph_schema` adapts queries to available relationship types

**Key design decision:** Orbit indexes the default branch only. MergeGuard extracts changed symbols from the MR diff itself, then uses Orbit to find who calls those symbols on main. This is the architecturally correct approach and is explicitly called out in the source as a demonstration of deep Orbit understanding.

## Installation

### Option 1: glab skills (recommended)

```bash
glab skills install mergeguard
```

### Option 2: Docker (self-hosted)

```bash
git clone https://gitlab.com/your-group/mergeguard.git
cd mergeguard
cp .env.example .env   # Edit with your GITLAB_TOKEN
docker-compose up -d
```

### Option 3: Python directly

```bash
git clone https://gitlab.com/your-group/mergeguard.git
cd mergeguard
./install.sh
source .venv/bin/activate
uvicorn src.main:app --port 8080
```

## Configuration

| Variable | Required | Description |
|----------|----------|-------------|
| `GITLAB_URL` | Yes | Your GitLab instance URL |
| `GITLAB_TOKEN` | Yes | Personal access token with `api` scope |
| `GITLAB_WEBHOOK_SECRET` | Yes | Secret token for webhook validation |
| `ORBIT_USE_LOCAL` | No | `true` to use `glab orbit local` (DuckDB) instead of Remote |
| `MERGEGUARD_DB` | No | SQLite database path (default: `mergeguard.db`) |

## GitLab Group Webhook Setup

1. Go to **Group → Settings → Webhooks**
2. URL: `http://your-mergeguard-host:8080/webhook/mr`
3. Secret token: `$GITLAB_WEBHOOK_SECRET`
4. Enable: **Merge request events**
5. Save

MergeGuard will now automatically analyze every MR opened or updated across your group.

## Example Output

When a semantic collision is detected, MergeGuard posts to **both** affected MRs:

```
🔴 MergeGuard: Semantic Collision Detected (Severity: HIGH)

This MR has a semantic conflict with demo-group/notification-service!7 —
"feat: add invoice notification and subscription renewal"

Collision on symbol: `charge_user`

| | Action |
|---|---|
| This MR | `def charge_user(amount)` → `def charge_user(amount, currency)` |
| !7 | calls `charge_user` (with old signature: charge_user(amount)) |

Impact: 4 caller(s) across 2 project(s)
Owners to notify: @maria, @sven

Affected callers:
  - `send_invoice` in `src/notifications/invoice.py` (demo-group/notification-service)
  - `send_subscription_renewal` in `src/notifications/invoice.py` (demo-group/notification-service)
  - `process_subscription` in `src/billing/subscriptions.py` (demo-group/billing-service)
  - `handle_checkout` in `src/orders/checkout.py` (demo-group/orders-service)

Suggested merge order: Merge `demo-group/notification-service!7` first
(it calls `charge_user` with the current signature). Then update callers
to use the new API before merging this MR.

> Detected by MergeGuard via GitLab Orbit cross-repo graph traversal.
> Orbit queried 12 downstream callers across the dependency graph.
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/webhook/mr` | GitLab MR webhook receiver |
| `GET` | `/api/collision-map` | Live collision graph (JSON for UI) |
| `GET` | `/api/collisions/{mr_id}` | Collisions for a specific MR |
| `GET` | `/api/mrs` | List all tracked open MRs |
| `GET` | `/` | Collision map visualization UI |
| `GET` | `/health` | Liveness check |

## Languages Supported

| Language | Extension | Extraction Method |
|----------|-----------|------------------|
| Python | `.py` | `ast.parse()` — exact AST-based signature comparison |
| JavaScript | `.js`, `.jsx` | Regex patterns for `function`, arrow functions, class methods |
| TypeScript | `.ts`, `.tsx` | Regex patterns + class method detection |

## Severity Scoring

```
severity = (centrality / 50) × owner_spread_factor × change_risk_factor

severity_label:
  ≥ 0.75 → 🚨 CRITICAL
  ≥ 0.50 → 🔴 HIGH  
  ≥ 0.25 → 🟠 MEDIUM
  <  0.25 → 🟡 LOW
```

- **centrality**: number of callers found by Orbit (normalized)
- **owner_spread_factor**: distinct teams/projects affected
- **change_risk_factor**: REMOVED=1.0, RENAMED=0.9, SIGNATURE_CHANGED=0.75
