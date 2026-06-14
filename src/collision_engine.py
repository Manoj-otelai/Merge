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
from pathlib import Path
from typing import Optional

from .models import (
    BlastRadius,
    CallerInfo,
    Collision,
    CollisionMap,
    Severity,
    SymbolChange,
)
from .severity_scorer import score_collision, suggest_merge_order

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

# MR entries expire after 7 days of inactivity (stale open MR cleanup)
_TTL_SECONDS = 7 * 24 * 3600


class CollisionEngine:
    def __init__(self, db_path: str = "mergeguard.db") -> None:
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(_DDL)
        self._conn.commit()

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

    def get_blast_radius(self, mr_id: int) -> Optional[BlastRadius]:
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
                    score, label = score_collision(sym_change, callers, this_br.centrality_score)
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
                        severity=score,
                        severity_label=label,
                        affected_callers=callers,
                        affected_owners=owners,
                        suggested_order=order,
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
                    score, label = score_collision(sym_change, callers, other_br.centrality_score)
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
                        severity=score,
                        severity_label=label,
                        affected_callers=callers,
                        affected_owners=owners,
                        suggested_order=order,
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
