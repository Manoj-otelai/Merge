"""GitLab REST API client for MR diffs, comments, and CI status."""
from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)


class GitLabClient:
    def __init__(self, gitlab_url: str = "", token: str = "") -> None:
        self.gitlab_url = gitlab_url or os.getenv("GITLAB_URL", "https://gitlab.com")
        self.token = token or os.getenv("GITLAB_TOKEN", "")
        self._http = httpx.AsyncClient(
            base_url=self.gitlab_url,
            headers={"PRIVATE-TOKEN": self.token},
            timeout=30.0,
        )

    async def get_mr_diff(self, project_id: int, mr_iid: int) -> str:
        """Return the unified diff text for a merge request."""
        resp = await self._http.get(
            f"/api/v4/projects/{project_id}/merge_requests/{mr_iid}/diffs",
            params={"unidiff": "true", "per_page": 100},
        )
        resp.raise_for_status()
        diffs = resp.json()
        # GitLab returns a list of file diffs; concatenate into a single unified diff
        parts: list[str] = []
        for d in diffs:
            if d.get("diff"):
                old_path = d.get("old_path", d.get("new_path", ""))
                new_path = d.get("new_path", old_path)
                parts.append(f"--- a/{old_path}\n+++ b/{new_path}\n{d['diff']}")
        return "\n".join(parts)

    async def get_mr_info(self, project_id: int, mr_iid: int) -> dict:
        """Return basic MR metadata."""
        resp = await self._http.get(
            f"/api/v4/projects/{project_id}/merge_requests/{mr_iid}"
        )
        resp.raise_for_status()
        return resp.json()

    async def post_mr_comment(
        self, project_id: int, mr_iid: int, body: str
    ) -> dict:
        """Post a note (comment) on a merge request."""
        resp = await self._http.post(
            f"/api/v4/projects/{project_id}/merge_requests/{mr_iid}/notes",
            json={"body": body},
        )
        resp.raise_for_status()
        return resp.json()

    async def get_project(self, project_id: int) -> dict:
        resp = await self._http.get(f"/api/v4/projects/{project_id}")
        resp.raise_for_status()
        return resp.json()

    async def get_pipeline_status(self, project_id: int, mr_iid: int) -> str | None:
        """Return the latest pipeline status for an MR."""
        resp = await self._http.get(
            f"/api/v4/projects/{project_id}/merge_requests/{mr_iid}/pipelines",
            params={"per_page": 1},
        )
        if resp.status_code != 200:
            return None
        pipelines = resp.json()
        if pipelines:
            return pipelines[0].get("status")
        return None

    async def has_coverage_job(self, project_id: int, mr_iid: int) -> bool:
        """Check if the MR's pipeline includes a coverage job."""
        status = await self.get_pipeline_status(project_id, mr_iid)
        if status not in ("success", "passed"):
            return False
        resp = await self._http.get(
            f"/api/v4/projects/{project_id}/merge_requests/{mr_iid}/pipelines",
            params={"per_page": 1},
        )
        if resp.status_code != 200:
            return False
        pipelines = resp.json()
        if not pipelines:
            return False
        pipeline_id = pipelines[0]["id"]
        jobs_resp = await self._http.get(
            f"/api/v4/projects/{project_id}/pipelines/{pipeline_id}/jobs"
        )
        if jobs_resp.status_code != 200:
            return False
        jobs = jobs_resp.json()
        return any("coverage" in (j.get("name", "").lower()) for j in jobs)

    async def set_commit_status(
        self,
        project_id: int,
        sha: str,
        state: str,
        name: str = "mergeguard",
        description: str = "",
        target_url: str = "",
    ) -> dict:
        """Set an external commit status (CI/CD merge gate, Phase 4.2).

        `state` is one of: pending, running, success, failed, canceled.
        Combined with 'Pipelines must succeed' / required status checks, a
        `failed` status blocks merge until the collision is resolved.
        """
        payload: dict = {"state": state, "name": name}
        if description:
            payload["description"] = description[:140]
        if target_url:
            payload["target_url"] = target_url
        resp = await self._http.post(
            f"/api/v4/projects/{project_id}/statuses/{sha}",
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()

    async def get_default_branch(self, project_id: int) -> str:
        """Return the project's default branch (e.g. 'main')."""
        proj = await self.get_project(project_id)
        return proj.get("default_branch", "main")

    async def get_file_content(
        self, project_id: int, file_path: str, ref: str = "main"
    ) -> str | None:
        """Fetch raw file content at a ref. Returns None on 404."""
        from urllib.parse import quote

        encoded = quote(file_path, safe="")
        resp = await self._http.get(
            f"/api/v4/projects/{project_id}/repository/files/{encoded}/raw",
            params={"ref": ref},
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.text

    async def create_branch(
        self, project_id: int, branch: str, ref: str
    ) -> dict:
        """Create a new branch from `ref`."""
        resp = await self._http.post(
            f"/api/v4/projects/{project_id}/repository/branches",
            params={"branch": branch, "ref": ref},
        )
        resp.raise_for_status()
        return resp.json()

    async def commit_files(
        self,
        project_id: int,
        branch: str,
        message: str,
        files: list[dict],
    ) -> dict:
        """Commit multiple file updates in one commit via the commits API.

        `files`: [{"path": str, "content": str}] — all treated as updates.
        """
        actions = [
            {"action": "update", "file_path": f["path"], "content": f["content"]}
            for f in files
        ]
        resp = await self._http.post(
            f"/api/v4/projects/{project_id}/repository/commits",
            json={"branch": branch, "commit_message": message, "actions": actions},
        )
        resp.raise_for_status()
        return resp.json()

    async def create_mr(
        self,
        project_id: int,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
    ) -> dict:
        """Create a new merge request (used for auto-drafted fix MRs)."""
        resp = await self._http.post(
            f"/api/v4/projects/{project_id}/merge_requests",
            json={
                "source_branch": source_branch,
                "target_branch": target_branch,
                "title": title,
                "description": description,
                "remove_source_branch": True,
            },
        )
        resp.raise_for_status()
        return resp.json()

    async def close(self) -> None:
        await self._http.aclose()
