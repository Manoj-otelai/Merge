#!/usr/bin/env python3
"""MergeGuard CI/CD merge gate (Phase 4.2).

A pipeline-friendly gate that fails the job when an MR has an unresolved
semantic collision at or above a severity threshold. Use it as a required job
so a red MergeGuard status blocks merge.

Usage (in .gitlab-ci.yml):

    mergeguard-gate:
      stage: test
      image: python:3.11-slim
      script:
        - pip install httpx
        - python ci/mergeguard_gate.py
      variables:
        MERGEGUARD_URL: "https://mergeguard.your-host"
        MERGEGUARD_GATE_SEVERITY: "high"   # low|medium|high|critical
      rules:
        - if: '$CI_MERGE_REQUEST_IID'

It reads CI_MERGE_REQUEST_PROJECT_ID / CI_MERGE_REQUEST_IID (predefined in
GitLab MR pipelines) and queries MergeGuard's API. Exit code 1 = blocked.
"""
from __future__ import annotations

import os
import sys

import httpx

_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}


def main() -> int:
    base = os.getenv("MERGEGUARD_URL", "http://localhost:8080").rstrip("/")
    threshold = os.getenv("MERGEGUARD_GATE_SEVERITY", "high").lower()
    # GitLab provides the MR's *global* id is not predefined; we match by iid.
    mr_iid = os.getenv("CI_MERGE_REQUEST_IID", "")
    if not mr_iid:
        print("MergeGuard gate: not a merge-request pipeline; skipping.")
        return 0

    try:
        mrs = httpx.get(f"{base}/api/mrs", timeout=15).json().get("mrs", [])
    except Exception as exc:
        print(f"MergeGuard gate: could not reach MergeGuard ({exc}); skipping (fail-open).")
        return 0

    match = next((m for m in mrs if str(m.get("mr_iid")) == str(mr_iid)), None)
    if not match:
        print(f"MergeGuard gate: !{mr_iid} not yet analyzed; passing.")
        return 0

    try:
        data = httpx.get(f"{base}/api/collisions/{match['mr_id']}", timeout=15).json()
    except Exception as exc:
        print(f"MergeGuard gate: error fetching collisions ({exc}); skipping (fail-open).")
        return 0

    collisions = data.get("collisions", [])
    blocking = [c for c in collisions if _RANK.get(c.get("severity_label"), 0) >= _RANK.get(threshold, 3)]

    if blocking:
        print(f"❌ MergeGuard gate FAILED: {len(blocking)} collision(s) ≥ {threshold} for !{mr_iid}:")
        for c in blocking:
            print(f"   • {c['symbol']} ({c['severity_label']}) ↔ !{c['other_mr_iid']} — {c.get('explanation','')}")
        print("Resolve the collision(s) or coordinate the merge order before merging.")
        return 1

    print(f"✅ MergeGuard gate passed for !{mr_iid}: no collisions ≥ {threshold}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
