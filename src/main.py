"""MergeGuard — FastAPI webhook server and UI host.

Receives GitLab merge_request webhook events, orchestrates symbol extraction,
Orbit blast-radius queries, collision detection, and MR comment posting.

Routes:
    POST /webhook/mr           — GitLab webhook endpoint
    GET  /api/collision-map    — JSON graph for UI
    GET  /api/collisions/{mr_id} — collisions for a specific MR
    GET  /health               — liveness check
    GET  /                     — serves the collision map SPA
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .collision_engine import CollisionEngine
from .gitlab_client import GitLabClient
from .models import BlastRadius
from .orbit_client import OrbitClient
from .symbol_extractor import extract_from_diff

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# ── Globals (initialized in lifespan) ───────────────────────────────────────

engine: CollisionEngine
gitlab: GitLabClient
orbit: OrbitClient

UI_DIR = Path(__file__).parent.parent / "ui"
DB_PATH = os.getenv("MERGEGUARD_DB", "mergeguard.db")
WEBHOOK_SECRET = os.getenv("GITLAB_WEBHOOK_SECRET", "")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global engine, gitlab, orbit
    engine = CollisionEngine(DB_PATH)
    gitlab = GitLabClient()
    orbit = OrbitClient()
    logger.info("MergeGuard started — db=%s orbit_local=%s", DB_PATH, orbit.use_local)
    yield
    engine.close()
    await gitlab.close()
    await orbit.close()


app = FastAPI(
    title="MergeGuard",
    description="Cross-MR Semantic Collision Sentinel powered by GitLab Orbit",
    version="1.0.0",
    lifespan=lifespan,
)

if UI_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(UI_DIR)), name="static")


# ── Webhook ──────────────────────────────────────────────────────────────────

class WebhookPayload(BaseModel):
    object_kind: str = ""
    object_attributes: dict = {}
    project: dict = {}
    user: dict = {}


@app.post("/webhook/mr")
async def handle_mr_webhook(request: Request) -> dict:
    """Receive GitLab merge_request webhook and process it."""
    if WEBHOOK_SECRET:
        token = request.headers.get("X-Gitlab-Token", "")
        if token != WEBHOOK_SECRET:
            raise HTTPException(status_code=401, detail="Invalid webhook token")

    payload = await request.json()

    if payload.get("object_kind") != "merge_request":
        return {"status": "ignored", "reason": "not a merge_request event"}

    attrs = payload.get("object_attributes", {})
    action = attrs.get("action", "")
    mr_id = attrs.get("id")
    mr_iid = attrs.get("iid")
    project_id = payload.get("project", {}).get("id")
    project_path = payload.get("project", {}).get("path_with_namespace", "")
    mr_url = attrs.get("url", "")
    mr_title = attrs.get("title", "")
    author = payload.get("user", {}).get("username", "")
    source_branch = attrs.get("source_branch", "")

    logger.info("MR event: %s !%s action=%s project=%s", mr_id, mr_iid, action, project_path)

    if action in ("merge", "close"):
        engine.close_mr(mr_id)
        return {"status": "closed", "mr_id": mr_id}

    if action not in ("open", "update", "reopen"):
        return {"status": "ignored", "reason": f"unhandled action: {action}"}

    # Step 1: Extract changed symbols from diff
    try:
        diff_text = await gitlab.get_mr_diff(project_id, mr_iid)
        changed_symbols = extract_from_diff(diff_text)
        logger.info("Extracted %d changed symbols from !%s", len(changed_symbols), mr_iid)
    except Exception as exc:
        logger.error("Diff extraction failed for !%s: %s", mr_iid, exc)
        changed_symbols = []

    # Step 2: Query Orbit for downstream blast radius
    all_callers = []
    all_owners = []
    max_centrality = 0.0

    for sym in changed_symbols:
        try:
            callers = await orbit.get_callers(sym.name, project_id)
            owners = await orbit.get_owners(sym.name)
            centrality = await orbit.get_centrality(sym.name)

            all_callers.extend(callers)
            all_owners.extend(owners)
            max_centrality = max(max_centrality, centrality)

            logger.info(
                "Orbit: symbol=%s callers=%d centrality=%.1f",
                sym.name, len(callers), centrality,
            )
        except Exception as exc:
            logger.warning("Orbit query failed for %s: %s", sym.name, exc)

    # Deduplicate
    seen_callers: set[tuple] = set()
    unique_callers = []
    for c in all_callers:
        key = (c.function_name, c.file_path, c.project_path)
        if key not in seen_callers:
            seen_callers.add(key)
            unique_callers.append(c)

    unique_owners = list(dict.fromkeys(all_owners))

    # Step 3: Register blast radius
    blast_radius = BlastRadius(
        mr_id=mr_id,
        mr_iid=mr_iid,
        mr_url=mr_url,
        mr_title=mr_title,
        project_id=project_id,
        project_path=project_path,
        author=author,
        source_branch=source_branch,
        changed_symbols=changed_symbols,
        downstream_callers=unique_callers,
        affected_owners=unique_owners,
        centrality_score=max_centrality,
    )
    engine.register_mr(blast_radius)

    # Step 4: Find collisions against all other open MRs
    collisions = engine.find_collisions(mr_id)
    logger.info("Found %d collision(s) for !%s", len(collisions), mr_iid)

    # Step 4b: Compute the global merge plan (topological order across all MRs)
    merge_plan_text = ""
    try:
        merge_plan_text = engine.get_merge_plan().summary_text()
    except Exception as exc:
        logger.warning("Merge plan computation failed: %s", exc)

    # Step 5: Post MR comments for each collision
    comments_posted = 0
    for collision in collisions:
        comment_body = collision.format_comment(mr_id, merge_plan_text)
        try:
            await gitlab.post_mr_comment(project_id, mr_iid, comment_body)
            comments_posted += 1

            # Also comment on the other MR if we can
            other_project_id = (
                collision.mr_b_id if mr_id == collision.mr_a_id else collision.mr_a_id
            )
            other_iid = (
                collision.mr_b_iid if mr_id == collision.mr_a_id else collision.mr_a_iid
            )
            try:
                await gitlab.post_mr_comment(
                    other_project_id, other_iid,
                    collision.format_comment(other_project_id, merge_plan_text),
                )
            except Exception as exc:
                logger.warning("Could not comment on other MR !%s: %s", other_iid, exc)

        except Exception as exc:
            logger.error("Failed to post comment on !%s: %s", mr_iid, exc)

    return {
        "status": "processed",
        "mr_id": mr_id,
        "mr_iid": mr_iid,
        "changed_symbols": len(changed_symbols),
        "downstream_callers": len(unique_callers),
        "collisions_found": len(collisions),
        "comments_posted": comments_posted,
    }


# ── API ───────────────────────────────────────────────────────────────────────

@app.get("/api/collision-map")
async def get_collision_map() -> dict:
    """Return the live collision graph for the UI."""
    cmap = engine.get_collision_map()
    return cmap.to_dict()


@app.get("/api/collisions/{mr_id}")
async def get_collisions(mr_id: int) -> dict:
    """Return collisions for a specific MR."""
    br = engine.get_blast_radius(mr_id)
    if not br:
        raise HTTPException(status_code=404, detail=f"MR {mr_id} not found in registry")

    collisions = engine.find_collisions(mr_id)
    return {
        "mr_id": mr_id,
        "mr_iid": br.mr_iid,
        "mr_title": br.mr_title,
        "mr_url": br.mr_url,
        "project": br.project_path,
        "author": br.author,
        "changed_symbols": len(br.changed_symbols),
        "downstream_callers": len(br.downstream_callers),
        "changed_symbol_names": [s.name for s in br.changed_symbols],
        "collisions": [
            {
                "other_mr_id": c.mr_b_id if mr_id == c.mr_a_id else c.mr_a_id,
                "other_mr_iid": c.mr_b_iid if mr_id == c.mr_a_id else c.mr_a_iid,
                "other_mr_title": c.mr_b_title if mr_id == c.mr_a_id else c.mr_a_title,
                "other_mr_url": c.mr_b_url if mr_id == c.mr_a_id else c.mr_a_url,
                "other_mr_project": c.mr_b_project if mr_id == c.mr_a_id else c.mr_a_project,
                "symbol": c.intersecting_symbol,
                "this_action": c.mr_a_changes if mr_id == c.mr_a_id else c.mr_b_depends_on,
                "other_action": c.mr_b_depends_on if mr_id == c.mr_a_id else c.mr_a_changes,
                "severity": c.severity,
                "severity_label": c.severity_label.value,
                "confidence": c.confidence,
                "explanation": c.explanation,
                "score_factors": c.score_factors,
                "suggested_order": c.suggested_order,
                "affected_owners": c.affected_owners,
                "affected_callers": [
                    {
                        "function_name": ca.function_name,
                        "file_path": ca.file_path,
                        "project_path": ca.project_path,
                        "owner": ca.owner,
                    }
                    for ca in c.affected_callers
                ],
            }
            for c in collisions
        ],
    }


@app.get("/api/merge-plan")
async def get_merge_plan() -> dict:
    """Return the global optimal merge order across all open MRs."""
    return engine.get_merge_plan().to_dict()


@app.get("/api/mrs")
async def list_mrs() -> dict:
    """List all open MRs in the registry."""
    all_mrs = engine.all_open_mrs()
    return {
        "count": len(all_mrs),
        "mrs": [
            {
                "mr_id": br.mr_id,
                "mr_iid": br.mr_iid,
                "mr_url": br.mr_url,
                "mr_title": br.mr_title,
                "project": br.project_path,
                "author": br.author,
                "changed_symbols": len(br.changed_symbols),
                "downstream_callers": len(br.downstream_callers),
            }
            for br in all_mrs
        ],
    }


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": "1.0.0"}


@app.get("/")
async def serve_ui() -> FileResponse:
    index = UI_DIR / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return HTMLResponse("<h1>MergeGuard</h1><p>UI not found — is the ui/ directory present?</p>")
