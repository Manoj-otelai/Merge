"""Cross-MR blast-radius registry and semantic collision detection.

Maintains a SQLite-backed registry of open MR blast radii.
On each MR update, intersects the incoming blast radius against all others
to find where one MR's contract changes overlap another MR's dependencies.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time

from .merge_sequencer import MergeEdge, MergePlan, MRNode, compute_merge_plan
from .models import (
    BlastRadius,
    CallerInfo,
    Collision,
    CollisionMap,
    SymbolChange,
)
from .severity_scorer import score_collision_detailed, suggest_merge_order

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS blast_radii (
    mr_id           INTEGER PRIMARY KEY,
    mr_iid          INTEGER NOT NULL,
    mr_url          TEXT NOT NULL,
    mr_title        TEXT NOT NULL,
    project_id      INTEGER NOT NULL,
    project_path    TEXT NOT NULL,
    author          TEXT NOT NULL DEFAULT '',
    source_branch   TEXT NOT NULL DEFAULT '',
    changed_symbols TEXT NOT NULL DEFAULT '[]',   -- JSON
    downstream_callers TEXT NOT NULL DEFAULT '[]',-- JSON
    affected_owners TEXT NOT NULL DEFAULT '[]',   -- JSON
    centrality_score REAL NOT NULL DEFAULT 0.0,
    updated_at      REAL NOT NULL DEFAULT 0
);
"""

# Persistent history of every collision MergeGuard has surfaced (Phase 2.2).
_DDL_HISTORY = """
CREATE TABLE IF NOT EXISTS collision_events (
    event_key        TEXT PRIMARY KEY,   -- stable: symbol|min_id|max_id
    symbol           TEXT NOT NULL,
    mr_a_id          INTEGER NOT NULL,
    mr_b_id          INTEGER NOT NULL,
    mr_a_iid         INTEGER NOT NULL DEFAULT 0,
    mr_b_iid         INTEGER NOT NULL DEFAULT 0,
    project_a        TEXT NOT NULL DEFAULT '',
    project_b        TEXT NOT NULL DEFAULT '',
    severity         REAL NOT NULL DEFAULT 0,
    severity_label   TEXT NOT NULL DEFAULT 'low',
    caller_count     INTEGER NOT NULL DEFAULT 0,
    distinct_projects INTEGER NOT NULL DEFAULT 1,
    owners           TEXT NOT NULL DEFAULT '[]',  -- JSON
    files            TEXT NOT NULL DEFAULT '[]',  -- JSON
    first_seen       REAL NOT NULL DEFAULT 0,
    last_seen        REAL NOT NULL DEFAULT 0,
    resolved_at      REAL,                         -- NULL while open
    commented_at     REAL,                         -- last time we posted an MR comment
    dismissed_at     REAL                          -- NULL unless manually reviewed/dismissed
);
"""

# MR entries expire after 7 days of inactivity (stale open MR cleanup)
_TTL_SECONDS = 7 * 24 * 3600

# How long before we re-post a collision comment (hours)
_COMMENT_COOLDOWN_HOURS = 24


class CollisionEngine:
    def __init__(self, db_path: str = "mergeguard.db") -> None:
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(_DDL)
        self._conn.execute(_DDL_HISTORY)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after the initial schema without losing data."""
        existing = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(collision_events)").fetchall()
        }
        for col, defn in [
            ("commented_at", "REAL"),
            ("dismissed_at", "REAL"),
        ]:
            if col not in existing:
                self._conn.execute(f"ALTER TABLE collision_events ADD COLUMN {col} {defn}")

    # ── Registry management ──────────────────────────────────────────────────

    def register_mr(self, br: BlastRadius) -> None:
        """Upsert a MR's blast radius into the registry."""
        self._conn.execute(
            """
            INSERT INTO blast_radii (
                mr_id, mr_iid, mr_url, mr_title, project_id, project_path,
                author, source_branch, changed_symbols, downstream_callers,
                affected_owners, centrality_score, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(mr_id) DO UPDATE SET
                mr_iid=excluded.mr_iid,
                mr_url=excluded.mr_url,
                mr_title=excluded.mr_title,
                changed_symbols=excluded.changed_symbols,
                downstream_callers=excluded.downstream_callers,
                affected_owners=excluded.affected_owners,
                centrality_score=excluded.centrality_score,
                updated_at=excluded.updated_at
            """,
            (
                br.mr_id,
                br.mr_iid,
                br.mr_url,
                br.mr_title,
                br.project_id,
                br.project_path,
                br.author,
                br.source_branch,
                json.dumps([_sym_to_dict(s) for s in br.changed_symbols]),
                json.dumps([_caller_to_dict(c) for c in br.downstream_callers]),
                json.dumps(br.affected_owners),
                br.centrality_score,
                time.time(),
            ),
        )
        self._conn.commit()
        self._prune_stale()

    def close_mr(self, mr_id: int) -> None:
        """Remove a merged/closed MR from the registry."""
        self._conn.execute("DELETE FROM blast_radii WHERE mr_id = ?", (mr_id,))
        self._conn.commit()

    def get_blast_radius(self, mr_id: int) -> BlastRadius | None:
        row = self._conn.execute(
            "SELECT * FROM blast_radii WHERE mr_id = ?", (mr_id,)
        ).fetchone()
        return _row_to_blast_radius(row) if row else None

    def all_open_mrs(self) -> list[BlastRadius]:
        rows = self._conn.execute("SELECT * FROM blast_radii").fetchall()
        return [_row_to_blast_radius(r) for r in rows]

    # ── Collision detection ──────────────────────────────────────────────────

    def find_collisions(self, mr_id: int) -> list[Collision]:
        """Intersect this MR's blast radius against all other open MRs."""
        this_br = self.get_blast_radius(mr_id)
        if not this_br:
            return []

        collisions: list[Collision] = []
        this_changed = {s.name: s for s in this_br.changed_symbols}
        this_caller_names = this_br.caller_symbol_names()

        for other_br in self.all_open_mrs():
            if other_br.mr_id == mr_id:
                continue

            other_changed = {s.name: s for s in other_br.changed_symbols}
            other_caller_names = other_br.caller_symbol_names()

            # Case 1: this MR changes S, other MR calls S (with old signature)
            for sym_name, sym_change in this_changed.items():
                if sym_name in other_caller_names:
                    # The real affected callers are the Orbit-resolved downstream
                    # callers of S in the CHANGING MR's blast radius. Entries whose
                    # function_name == S are dependency markers, not real callers.
                    callers = [c for c in this_br.downstream_callers if c.function_name != sym_name]
                    scored = score_collision_detailed(sym_change, callers, this_br.centrality_score)
                    order = suggest_merge_order(
                        this_br.mr_id, this_br.mr_iid, this_br.project_path,
                        other_br.mr_id, other_br.mr_iid, other_br.project_path,
                        sym_name, mr_a_changes=True,
                    )
                    # Owners of the concretely-affected call sites (deduped, stable order)
                    owners = list(dict.fromkeys(c.owner for c in callers if c.owner))
                    collisions.append(Collision(
                        mr_a_id=this_br.mr_id,
                        mr_a_iid=this_br.mr_iid,
                        mr_a_url=this_br.mr_url,
                        mr_a_title=this_br.mr_title,
                        mr_a_project=this_br.project_path,
                        mr_b_id=other_br.mr_id,
                        mr_b_iid=other_br.mr_iid,
                        mr_b_url=other_br.mr_url,
                        mr_b_title=other_br.mr_title,
                        mr_b_project=other_br.project_path,
                        intersecting_symbol=sym_name,
                        mr_a_changes=sym_change.signature_summary(),
                        mr_b_depends_on=f"calls `{sym_name}` (with old signature)",
                        severity=scored.score,
                        severity_label=scored.label,
                        affected_callers=callers,
                        affected_owners=owners,
                        suggested_order=order,
                        confidence=scored.confidence,
                        explanation=scored.explanation,
                        score_factors=scored.factors,
                    ))

            # Case 2: other MR changes S, this MR calls S (has a dependency on old S)
            for sym_name, sym_change in other_changed.items():
                if sym_name in this_caller_names:
                    # Avoid duplicate — only add if not already covered by Case 1 above
                    already_added = any(
                        c.intersecting_symbol == sym_name
                        and c.mr_b_id == other_br.mr_id
                        for c in collisions
                    )
                    if already_added:
                        continue

                    # Real affected callers live in the CHANGING MR (other_br).
                    callers = [c for c in other_br.downstream_callers if c.function_name != sym_name]
                    scored = score_collision_detailed(sym_change, callers, other_br.centrality_score)
                    order = suggest_merge_order(
                        other_br.mr_id, other_br.mr_iid, other_br.project_path,
                        this_br.mr_id, this_br.mr_iid, this_br.project_path,
                        sym_name, mr_a_changes=True,
                    )
                    # Owners of the concretely-affected call sites (deduped, stable order)
                    owners = list(dict.fromkeys(c.owner for c in callers if c.owner))
                    collisions.append(Collision(
                        mr_a_id=this_br.mr_id,
                        mr_a_iid=this_br.mr_iid,
                        mr_a_url=this_br.mr_url,
                        mr_a_title=this_br.mr_title,
                        mr_a_project=this_br.project_path,
                        mr_b_id=other_br.mr_id,
                        mr_b_iid=other_br.mr_iid,
                        mr_b_url=other_br.mr_url,
                        mr_b_title=other_br.mr_title,
                        mr_b_project=other_br.project_path,
                        intersecting_symbol=sym_name,
                        mr_a_changes=f"calls `{sym_name}` (with old signature)",
                        mr_b_depends_on=sym_change.signature_summary(),
                        severity=scored.score,
                        severity_label=scored.label,
                        affected_callers=callers,
                        affected_owners=owners,
                        suggested_order=order,
                        confidence=scored.confidence,
                        explanation=scored.explanation,
                        score_factors=scored.factors,
                    ))

        # Deduplicate by (symbol, mr pair regardless of order)
        seen: set[tuple] = set()
        unique: list[Collision] = []
        for c in collisions:
            key = (c.intersecting_symbol, min(c.mr_a_id, c.mr_b_id), max(c.mr_a_id, c.mr_b_id))
            if key not in seen:
                seen.add(key)
                unique.append(c)

        return sorted(unique, key=lambda c: c.severity, reverse=True)

    def get_collision_map(self) -> CollisionMap:
        """Build serializable graph data for the UI visualization."""
        all_mrs = self.all_open_mrs()
        nodes = []
        for br in all_mrs:
            nodes.append({
                "id": br.mr_id,
                "iid": br.mr_iid,
                "title": br.mr_title,
                "url": br.mr_url,
                "project": br.project_path,
                "author": br.author,
                "symbol_count": len(br.changed_symbols),
                "caller_count": len(br.downstream_callers),
            })

        edges = []
        seen_pairs: set[tuple] = set()
        for br in all_mrs:
            for collision in self.find_collisions(br.mr_id):
                pair = (min(collision.mr_a_id, collision.mr_b_id), max(collision.mr_a_id, collision.mr_b_id))
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                edges.append({
                    "source": collision.mr_a_id,
                    "target": collision.mr_b_id,
                    "symbol": collision.intersecting_symbol,
                    "severity": collision.severity,
                    "severity_label": collision.severity_label.value,
                })

        return CollisionMap(
            nodes=nodes,
            edges=edges,
            total_collisions=len(edges),
            open_mrs=len(all_mrs),
        )

    def get_merge_plan(self) -> MergePlan:
        """Compute the global optimal merge order across all open MRs.

        For each collision we identify the *changer* (the MR whose changed
        symbols include the intersecting symbol) and the *caller* (the other
        MR, which depends on the current contract). The caller must merge
        first, so we add a 'merge-before' edge caller → changer. Mutually
        colliding MRs form a cycle, surfaced as a coordination group.
        """
        all_mrs = self.all_open_mrs()
        nodes = [
            MRNode(br.mr_id, br.mr_iid, br.project_path, br.mr_title, br.mr_url)
            for br in all_mrs
        ]
        changed_by = {br.mr_id: br.changed_symbol_names() for br in all_mrs}

        edges: list[MergeEdge] = []
        seen: set[tuple] = set()
        for br in all_mrs:
            for c in self.find_collisions(br.mr_id):
                sym = c.intersecting_symbol
                key = (min(c.mr_a_id, c.mr_b_id), max(c.mr_a_id, c.mr_b_id), sym)
                if key in seen:
                    continue
                seen.add(key)

                a_changes = sym in changed_by.get(c.mr_a_id, set())
                b_changes = sym in changed_by.get(c.mr_b_id, set())
                if a_changes and not b_changes:
                    changer, caller = c.mr_a_id, c.mr_b_id
                elif b_changes and not a_changes:
                    changer, caller = c.mr_b_id, c.mr_a_id
                elif a_changes and b_changes:
                    # Both change the same symbol → mutual conflict; add both
                    # directions so the SCC pass flags a coordination group.
                    edges.append(MergeEdge(c.mr_a_id, c.mr_b_id, sym, c.severity_label.value))
                    edges.append(MergeEdge(c.mr_b_id, c.mr_a_id, sym, c.severity_label.value))
                    continue
                else:
                    continue
                # caller merges before changer
                edges.append(MergeEdge(caller, changer, sym, c.severity_label.value))

        return compute_merge_plan(nodes, edges)

    # ── History & analytics (Phase 2.2) ──────────────────────────────────────

    @staticmethod
    def _event_key(c: Collision) -> str:
        lo, hi = sorted((c.mr_a_id, c.mr_b_id))
        return f"{c.intersecting_symbol}|{lo}|{hi}"

    def record_collisions(self, collisions: list[Collision]) -> int:
        """Persist detected collisions into history (idempotent per event_key).

        Re-detections bump last_seen and refresh severity; first_seen is kept.
        Reopens (resolved → seen again) clear resolved_at.
        """
        now = time.time()
        recorded = 0
        for c in collisions:
            key = self._event_key(c)
            files = sorted({ca.file_path for ca in c.affected_callers if ca.file_path})
            self._conn.execute(
                """
                INSERT INTO collision_events (
                    event_key, symbol, mr_a_id, mr_b_id, mr_a_iid, mr_b_iid,
                    project_a, project_b, severity, severity_label, caller_count,
                    distinct_projects, owners, files, first_seen, last_seen, resolved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(event_key) DO UPDATE SET
                    severity=excluded.severity,
                    severity_label=excluded.severity_label,
                    caller_count=excluded.caller_count,
                    distinct_projects=excluded.distinct_projects,
                    owners=excluded.owners,
                    files=excluded.files,
                    last_seen=excluded.last_seen,
                    resolved_at=NULL
                """,
                (
                    key, c.intersecting_symbol, c.mr_a_id, c.mr_b_id, c.mr_a_iid, c.mr_b_iid,
                    c.mr_a_project, c.mr_b_project, c.severity, c.severity_label.value,
                    len(c.affected_callers),
                    c.score_factors.get("distinct_projects", len({ca.project_path for ca in c.affected_callers}) or 1),
                    json.dumps(c.affected_owners), json.dumps(files), now, now,
                ),
            )
            recorded += 1
        self._conn.commit()
        return recorded

    def mark_resolved_for_mr(self, mr_id: int) -> int:
        """Mark all open events involving this MR as resolved (on merge/close)."""
        now = time.time()
        cur = self._conn.execute(
            """
            UPDATE collision_events SET resolved_at = ?
            WHERE resolved_at IS NULL AND (mr_a_id = ? OR mr_b_id = ?)
            """,
            (now, mr_id, mr_id),
        )
        self._conn.commit()
        return cur.rowcount

    # ── Notification deduplication ────────────────────────────────────────────

    def mark_commented(self, event_key: str) -> None:
        """Record that we just posted an MR comment for this collision."""
        self._conn.execute(
            "UPDATE collision_events SET commented_at = ? WHERE event_key = ?",
            (time.time(), event_key),
        )
        self._conn.commit()

    def already_commented(self, event_key: str, hours: float = _COMMENT_COOLDOWN_HOURS) -> bool:
        """Return True if we posted a comment for this collision within `hours`."""
        row = self._conn.execute(
            "SELECT commented_at FROM collision_events WHERE event_key = ?",
            (event_key,),
        ).fetchone()
        if not row or row["commented_at"] is None:
            return False
        return (time.time() - row["commented_at"]) < hours * 3600

    # ── Dismiss / acknowledge ─────────────────────────────────────────────────

    def dismiss_collision(self, event_key: str) -> bool:
        """Acknowledge a collision so it no longer blocks the merge gate."""
        cur = self._conn.execute(
            "UPDATE collision_events SET dismissed_at = ? WHERE event_key = ?",
            (time.time(), event_key),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def undismiss_collision(self, event_key: str) -> bool:
        """Reopen a dismissed collision."""
        cur = self._conn.execute(
            "UPDATE collision_events SET dismissed_at = NULL WHERE event_key = ?",
            (event_key,),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def is_dismissed(self, event_key: str) -> bool:
        row = self._conn.execute(
            "SELECT dismissed_at FROM collision_events WHERE event_key = ?",
            (event_key,),
        ).fetchone()
        return bool(row and row["dismissed_at"] is not None)

    # ── Timeline ──────────────────────────────────────────────────────────────

    def get_timeline(self, limit: int = 60) -> list[dict]:
        """Return the most recent collision events sorted newest-first."""
        rows = self._conn.execute(
            """
            SELECT * FROM collision_events
            ORDER BY last_seen DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        result = []
        for r in rows:
            result.append({
                "event_key": r["event_key"],
                "symbol": r["symbol"],
                "mr_a_iid": r["mr_a_iid"],
                "mr_b_iid": r["mr_b_iid"],
                "project_a": r["project_a"],
                "project_b": r["project_b"],
                "severity": r["severity"],
                "severity_label": r["severity_label"],
                "caller_count": r["caller_count"],
                "owners": json.loads(r["owners"] or "[]"),
                "first_seen": r["first_seen"],
                "last_seen": r["last_seen"],
                "resolved_at": r["resolved_at"],
                "dismissed_at": r["dismissed_at"],
                "commented_at": r["commented_at"],
                "status": (
                    "resolved" if r["resolved_at"]
                    else "dismissed" if r["dismissed_at"]
                    else "active"
                ),
            })
        return result

    def get_export(self) -> dict:
        """Return a full JSON-serialisable snapshot for export / audit."""
        return {
            "exported_at": time.time(),
            "open_mrs": [
                {
                    "mr_id": br.mr_id,
                    "mr_iid": br.mr_iid,
                    "mr_url": br.mr_url,
                    "mr_title": br.mr_title,
                    "project": br.project_path,
                    "author": br.author,
                    "source_branch": br.source_branch,
                    "changed_symbols": [s.name for s in br.changed_symbols],
                    "caller_count": len(br.downstream_callers),
                }
                for br in self.all_open_mrs()
            ],
            "timeline": self.get_timeline(limit=500),
        }

    def get_analytics(self, cost_model=None) -> dict:
        """Aggregate collision history into hotspots, owner load, and cost saved."""
        from .cost_model import CostModel

        cost_model = cost_model or CostModel.load()
        rows = self._conn.execute("SELECT * FROM collision_events").fetchall()

        total = len(rows)
        resolved = sum(1 for r in rows if r["resolved_at"] is not None)
        active = total - resolved

        by_sev: dict[str, int] = {}
        file_hot: dict[str, int] = {}
        project_hot: dict[str, int] = {}
        owner_load: dict[str, int] = {}
        resolution_times: list[float] = []
        dollars = 0.0
        hours = 0.0

        for r in rows:
            by_sev[r["severity_label"]] = by_sev.get(r["severity_label"], 0) + 1

            for f in json.loads(r["files"] or "[]"):
                file_hot[f] = file_hot.get(f, 0) + 1
            for p in (r["project_a"], r["project_b"]):
                if p:
                    project_hot[p] = project_hot.get(p, 0) + 1
            for o in json.loads(r["owners"] or "[]"):
                owner_load[o] = owner_load.get(o, 0) + 1

            if r["resolved_at"] is not None:
                resolution_times.append((r["resolved_at"] - r["first_seen"]) / 3600.0)

            savings = cost_model.estimate(
                r["severity_label"], r["caller_count"], r["distinct_projects"]
            )
            dollars += savings.dollars
            hours += savings.hours

        def top(d: dict, n: int = 5) -> list[dict]:
            return [
                {"name": k, "count": v}
                for k, v in sorted(d.items(), key=lambda kv: kv[1], reverse=True)[:n]
            ]

        avg_resolution_hours = (
            round(sum(resolution_times) / len(resolution_times), 2)
            if resolution_times else None
        )

        return {
            "totals": {
                "total_collisions": total,
                "active": active,
                "resolved": resolved,
                "by_severity": by_sev,
            },
            "cost_saved": {
                "currency": cost_model.currency,
                "dollars": round(dollars, 2),
                "engineer_hours": round(hours, 2),
            },
            "hotspot_files": top(file_hot),
            "hotspot_projects": top(project_hot),
            "owner_load": top(owner_load),
            "avg_resolution_hours": avg_resolution_hours,
        }

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _prune_stale(self) -> None:
        cutoff = time.time() - _TTL_SECONDS
        self._conn.execute("DELETE FROM blast_radii WHERE updated_at < ?", (cutoff,))
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


# ── Serialization helpers ────────────────────────────────────────────────────

def _sym_to_dict(s: SymbolChange) -> dict:
    return {
        "name": s.name,
        "old_signature": s.old_signature,
        "new_signature": s.new_signature,
        "file_path": s.file_path,
        "language": s.language.value,
        "change_type": s.change_type.value,
        "line_number": s.line_number,
    }


def _caller_to_dict(c: CallerInfo) -> dict:
    return {
        "function_name": c.function_name,
        "file_path": c.file_path,
        "project_path": c.project_path,
        "project_id": c.project_id,
        "owner": c.owner,
        "line_number": c.line_number,
    }


def _row_to_blast_radius(row: sqlite3.Row) -> BlastRadius:
    from .models import ChangeType, Language

    raw_syms = json.loads(row["changed_symbols"] or "[]")
    symbols = [
        SymbolChange(
            name=s["name"],
            old_signature=s["old_signature"],
            new_signature=s["new_signature"],
            file_path=s["file_path"],
            language=Language(s.get("language", "python")),
            change_type=ChangeType(s.get("change_type", "signature_changed")),
            line_number=s.get("line_number", 0),
        )
        for s in raw_syms
    ]

    raw_callers = json.loads(row["downstream_callers"] or "[]")
    callers = [
        CallerInfo(
            function_name=c["function_name"],
            file_path=c["file_path"],
            project_path=c["project_path"],
            project_id=c.get("project_id", 0),
            owner=c.get("owner", ""),
            line_number=c.get("line_number", 0),
        )
        for c in raw_callers
    ]

    return BlastRadius(
        mr_id=row["mr_id"],
        mr_iid=row["mr_iid"],
        mr_url=row["mr_url"],
        mr_title=row["mr_title"],
        project_id=row["project_id"],
        project_path=row["project_path"],
        author=row["author"],
        source_branch=row["source_branch"],
        changed_symbols=symbols,
        downstream_callers=callers,
        affected_owners=json.loads(row["affected_owners"] or "[]"),
        centrality_score=row["centrality_score"],
    )
