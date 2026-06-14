# 🛡️ MergeGuard — Cross-MR Semantic Collision Sentinel

> **GitLab Transcend Hackathon 2026 submission** · Showcase Track · MIT License

MergeGuard is a **GitLab Duo Agent Platform flow** that detects **semantic collisions across simultaneously-open merge requests** — the class of breakage where two MRs touch different files, both pass CI, both have zero Git conflicts, but will break each other after merge.

It uses **GitLab Orbit** as a cross-change coordination layer: traversing the knowledge graph to find who calls a changed symbol across all repositories, then intersecting the blast radii of all open MRs to identify hidden semantic conflicts before they reach production.

## The Problem

```
MR-A (payments-service): charge_user(amount) → charge_user(amount, currency)
MR-B (notification-service): adds caller: charge_user(100.0)   ← old signature

Git sees: no conflicts ✓
CI: both green ✓
After merge: 💥 TypeError in production
```

An empirical study of 143 projects / 36,122 merge commits found that **19.32% of merges resulted in conflicts**, with **75.23% requiring developers to reason about program logic** to resolve — and code involved in semantic conflicts is **26× more likely to have a bug**. Each hour of downtime from such breaks costs $300K–$5M+ in large enterprises.

MergeGuard catches these at the cheapest possible moment: **before merge**.

## Architecture

```
GitLab Group Webhook
        │
        ▼
┌─────────────────────────────────────────────────┐
│  GitLab Duo Agent Platform Flow                  │
│                                                  │
│  [1] extract_symbols ──► MR diff → SymbolChange  │
│                                                  │
│  [2] query_orbit ──────► Orbit query_graph       │◄── GitLab Orbit
│                           REFERENCES traversal   │    (default branch
│                           cross-repo callers     │     knowledge graph)
│                                                  │
│  [3] detect_collisions ► blast radius registry  │
│                           intersection engine    │
│                                                  │
│  [4] score_severity ───► centrality × spread    │
│                                                  │
│  [5] post_comment ─────► MR warning comment     │──► GitLab MR
└─────────────────────────────────────────────────┘

Collision Map UI (D3.js): http://localhost:8080
```

**Key design decision:** Orbit indexes the default branch only. MergeGuard extracts changed symbols from MR diffs directly, then uses Orbit to find who calls those symbols on `main`. This correctly identifies callers that will break after merge.

## Quick Start

### Option 1: One-command install

```bash
git clone https://gitlab.com/your-group/mergeguard.git && cd mergeguard
./install.sh
# Edit .env with your GITLAB_TOKEN and GITLAB_WEBHOOK_SECRET
source .venv/bin/activate
uvicorn src.main:app --port 8080
```

### Option 2: Docker

```bash
git clone https://gitlab.com/your-group/mergeguard.git && cd mergeguard
cp .env.example .env  # Edit credentials
docker-compose up -d
```

### Option 3: GitLab AI Catalog

```bash
glab skills install mergeguard
```

### Configure Group Webhook

1. **Group → Settings → Webhooks**
2. URL: `http://your-host:8080/webhook/mr`
3. Secret: `$GITLAB_WEBHOOK_SECRET`
4. Enable: **Merge request events** ✓

## How It Works

### Step 1: Symbol Extraction

When an MR is opened or updated, MergeGuard fetches its diff and extracts all changed function/class/method signatures:

```python
# MR-A diff shows:
# -    def charge_user(self, amount: float) -> bool:
# +    def charge_user(self, amount: float, currency: str = "USD") -> bool:

# MergeGuard extracts:
SymbolChange(
    name="charge_user",
    old_signature="def charge_user(amount: float) -> bool",
    new_signature="def charge_user(amount: float, currency: str = 'USD') -> bool",
    change_type=ChangeType.SIGNATURE_CHANGED,
)
```

Supports: Python (AST-based), JavaScript/TypeScript (regex-based).

### Step 2: Orbit Blast Radius Query

For each changed symbol, MergeGuard calls `query_graph` to find all downstream callers across every repo in the group:

```json
{
  "query_type": "traversal",
  "node": {
    "id": "sym", "entity": "Definition",
    "filters": {"name": {"operator": "eq", "value": "charge_user"}}
  },
  "relationships": [{
    "type": "REFERENCES", "direction": "incoming",
    "node": {"id": "caller", "entity": "Definition",
             "columns": ["name", "file_path"]},
    "relationships": [{
      "type": "IN_PROJECT", "direction": "outgoing",
      "node": {"id": "proj", "entity": "Project",
               "columns": ["full_path"]}
    }]
  }],
  "limit": 100
}
```

### Step 3: Cross-MR Intersection (the novel step)

MergeGuard maintains a SQLite registry of all open MR blast radii. For each new MR, it intersects:

```
COLLISION when: MR-A changes symbol S
            AND MR-B's code calls symbol S
```

This is the capability that Orbit enables but no single-MR tool exposes: **using the knowledge graph as a cross-change coordination layer**.

### Step 4: Severity Scoring

```
severity = (caller_count / 50) × (distinct_teams / 5) × change_risk

change_risk:
  REMOVED          → 1.0
  RENAMED          → 0.9
  SIGNATURE_CHANGED → 0.75
```

### Step 5: MR Comment

MergeGuard posts to both affected MRs with:
- Severity badge and score
- Link to the colliding MR
- The exact intersecting symbol
- Up to 5 downstream callers (with project paths and owners)
- Suggested merge order

## Collision Map UI

Open `http://localhost:8080` to see the live D3.js collision graph:

- **Nodes**: open MRs (sized by downstream caller count)
- **Edges**: semantic collisions (colored by severity: 🔴 critical, 🟠 high, 🟡 medium)
- **Click node**: show collision details for that MR
- **Click edge**: drill into the shared symbol and affected callers
- Auto-refreshes every 30 seconds

## Project Structure

```
├── agent/
│   ├── agent.yml          # Duo Agent Platform agent definition
│   ├── flow.yml           # 5-step orchestration flow
│   └── skills/            # Individual skill definitions
├── src/
│   ├── main.py            # FastAPI webhook server
│   ├── models.py          # Data models (SymbolChange, BlastRadius, Collision)
│   ├── symbol_extractor.py # MR diff → changed symbols
│   ├── orbit_client.py    # Orbit query_graph interface
│   ├── collision_engine.py # Blast-radius registry + intersection
│   ├── severity_scorer.py  # Collision severity scoring
│   └── gitlab_client.py   # GitLab API (diffs, comments)
├── ui/
│   ├── index.html         # Collision map SPA
│   ├── collision_map.js   # D3.js force graph
│   └── styles.css
├── tests/
│   ├── test_symbol_extractor.py
│   ├── test_collision_engine.py
│   └── test_orbit_client.py
├── demo/
│   ├── setup_demo.sh      # Creates two colliding MRs in your GitLab group
│   └── seed_data/         # Patches, expected outputs
├── SKILL.md               # AI Catalog entry
└── install.sh             # One-command installer
```

## Configuration

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GITLAB_URL` | Yes | `https://gitlab.com` | GitLab instance URL |
| `GITLAB_TOKEN` | Yes | — | PAT with `api` scope |
| `GITLAB_WEBHOOK_SECRET` | Yes | — | Webhook validation token |
| `ORBIT_USE_LOCAL` | No | `false` | Use `glab orbit local` (DuckDB) |
| `MERGEGUARD_DB` | No | `mergeguard.db` | SQLite database path |

## Running Tests

```bash
pip install -r requirements.txt
pytest tests/ -v
```

## Languages Supported

| Language | Extraction | Notes |
|----------|-----------|-------|
| Python | `ast.parse()` | Exact AST-based signature comparison |
| JavaScript | Regex | `function`, arrow functions, class methods |
| TypeScript | Regex | All JS patterns + typed signatures |

## Judging Criteria Alignment

| Criterion (25% each) | MergeGuard's approach |
|---------------------|----------------------|
| **Technological Implementation** | Real multi-hop Orbit traversal; cross-MR intersection requires solving the default-branch constraint explicitly |
| **Design & Usability** | Output lands as MR comment in existing review workflow; one-line install |
| **Potential Impact** | Attacks universal pre-merge failure mode; 26× bug risk reduction; quantified downtime cost |
| **Quality of Idea** | Reveals new Orbit capability: cross-change coordination layer, not just per-change lookup |

## License

MIT — see [LICENSE](LICENSE)

## DCO Sign-off

All commits include Developer Certificate of Origin sign-off as required by the hackathon.

---

*Built for the [GitLab Transcend Hackathon 2026](https://contributors.gitlab.com/transcend-hackathon) — "Intelligent orchestration, now with context."*
