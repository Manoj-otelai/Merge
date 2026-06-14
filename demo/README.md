# MergeGuard Demo Setup

This directory contains scripts and seed data to run the 3-minute demo scenario.

## Demo Scenario

Two MRs in two repositories, both green CI, zero Git text conflicts — but they will
break each other after merge:

| | MR-A | MR-B |
|---|---|---|
| **Repository** | `payments-service` | `notification-service` |
| **Title** | Add currency support to `charge_user` | Add invoice & subscription notifications |
| **What it does** | Changes `charge_user(amount)` → `charge_user(amount, currency)` | Adds new callers using `charge_user(amount)` (old signature) |
| **Git sees** | No conflicts | No conflicts |
| **MergeGuard sees** | **COLLISION** on `charge_user` |

**Expected MergeGuard output:** HIGH-severity collision warning on both MRs, listing
4 downstream callers found by Orbit and the suggested merge order.

## Prerequisites

- GitLab group with Orbit Remote enabled
- `glab` CLI authenticated
- MergeGuard deployed and running (see root README)
- Two projects created in your GitLab group

## Running the Demo

### 1. Set environment variables

```bash
export GITLAB_GROUP="your-group"
export GITLAB_URL="https://gitlab.com"
export MERGEGUARD_URL="http://localhost:8080"
```

### 2. Run setup script

```bash
chmod +x demo/setup_demo.sh
./demo/setup_demo.sh
```

This script:
1. Creates `payments-service` and `notification-service` repos in `$GITLAB_GROUP`
2. Seeds them with the base code (MR target state)
3. Creates MR-A (charge_user signature change) in payments-service
4. Creates MR-B (new callers of old signature) in notification-service
5. Waits 2 minutes for Orbit indexing
6. Triggers MergeGuard webhooks for both MRs

### 3. Observe the collision

Open both MRs and look for MergeGuard's comment. Then open the collision map UI
at `http://localhost:8080` to see the D3 visualization.

### 4. Demo talking points

**0:00–0:30** — "Two MRs. Both pass CI. Zero Git conflicts. But watch what happens..."

**0:30–1:45** — Open MR-A. Show MergeGuard's comment:
- Points to MR-B as the collision partner
- Names `charge_user` as the intersecting symbol  
- Shows 4 downstream callers found by Orbit across 2 repos
- Suggests merge order

**1:45–2:30** — Show the collision map UI:
- MR-A and MR-B as nodes connected by a red HIGH-severity edge
- Click the edge → see the shared symbol and callers
- "Orbit doesn't just tell you what one change breaks. It tells you what your changes break in each other — before you merge."

**2:30–3:00** — "One-line install: `glab skills install mergeguard`. Works across your entire group."

## Verifying Without Setup Script

You can trigger MergeGuard directly with sample data:

```bash
# Simulate MR-A webhook
curl -X POST http://localhost:8080/webhook/mr \
  -H "Content-Type: application/json" \
  -H "X-Gitlab-Token: ${MERGEGUARD_WEBHOOK_SECRET}" \
  -d @demo/seed_data/webhook_mr_a.json

# Simulate MR-B webhook
curl -X POST http://localhost:8080/webhook/mr \
  -H "Content-Type: application/json" \
  -H "X-Gitlab-Token: ${MERGEGUARD_WEBHOOK_SECRET}" \
  -d @demo/seed_data/webhook_mr_b.json

# Check collision map
curl http://localhost:8080/api/collision-map | python -m json.tool
```
