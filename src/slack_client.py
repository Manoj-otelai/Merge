"""Slack notifications for high-severity collisions (Phase 4.1).

Posts a real-time alert to a Slack channel via chat.postMessage when a collision
at or above a configurable severity threshold is detected. Live use requires:

    SLACK_BOT_TOKEN   xoxb-… bot token with chat:write
    SLACK_CHANNEL     channel id or #name to post to
    SLACK_MIN_SEVERITY   one of low|medium|high|critical (default: high)

When the token/channel are absent the notifier is a no-op, so it never breaks
the offline demo.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import httpx

from .models import Collision

logger = logging.getLogger(__name__)

_SEV_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}
_SEV_EMOJI = {"low": "🟡", "medium": "🟠", "high": "🔴", "critical": "🚨"}


@dataclass
class SlackResult:
    sent: int
    skipped: int
    errors: int


class SlackNotifier:
    def __init__(
        self,
        token: str = "",
        channel: str = "",
        min_severity: str = "",
    ) -> None:
        self.token = token or os.getenv("SLACK_BOT_TOKEN", "")
        self.channel = channel or os.getenv("SLACK_CHANNEL", "")
        self.min_severity = (min_severity or os.getenv("SLACK_MIN_SEVERITY", "high")).lower()
        self._http = httpx.AsyncClient(
            base_url="https://slack.com/api",
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=15.0,
        )

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.channel)

    def _meets_threshold(self, severity_label: str) -> bool:
        return _SEV_RANK.get(severity_label, 0) >= _SEV_RANK.get(self.min_severity, 3)

    def build_blocks(self, c: Collision, map_url: str = "") -> list[dict]:
        """Build Slack Block Kit blocks for a collision alert."""
        emoji = _SEV_EMOJI.get(c.severity_label.value, "⚠️")
        owners = " ".join(f"@{o}" for o in c.affected_owners) if c.affected_owners else "_unknown_"
        conf = round(c.confidence * 100)
        fields = [
            {"type": "mrkdwn", "text": f"*Symbol:*\n`{c.intersecting_symbol}`"},
            {"type": "mrkdwn", "text": f"*Severity:*\n{c.severity_label.value.upper()} ({conf}% conf)"},
            {"type": "mrkdwn", "text": f"*{c.mr_a_project}!{c.mr_a_iid}*\n{c.mr_a_changes}"},
            {"type": "mrkdwn", "text": f"*{c.mr_b_project}!{c.mr_b_iid}*\n{c.mr_b_depends_on}"},
            {"type": "mrkdwn", "text": f"*Owners:*\n{owners}"},
            {"type": "mrkdwn", "text": f"*Callers at risk:*\n{len(c.affected_callers)}"},
        ]
        blocks: list[dict] = [
            {"type": "header", "text": {"type": "plain_text", "text": f"{emoji} MergeGuard: semantic collision"}},
            {"type": "section", "fields": fields},
        ]
        if c.explanation:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"_{c.explanation}_"}})
        if c.suggested_order:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"🧭 *Merge order:* {c.suggested_order}"}})
        actions = []
        if c.mr_a_url:
            actions.append({"type": "button", "text": {"type": "plain_text", "text": f"Open !{c.mr_a_iid}"}, "url": c.mr_a_url})
        if c.mr_b_url:
            actions.append({"type": "button", "text": {"type": "plain_text", "text": f"Open !{c.mr_b_iid}"}, "url": c.mr_b_url})
        if map_url:
            actions.append({"type": "button", "text": {"type": "plain_text", "text": "Collision map"}, "url": map_url})
        if actions:
            blocks.append({"type": "actions", "elements": actions})
        return blocks

    async def notify_collisions(self, collisions: list[Collision], map_url: str = "") -> SlackResult:
        """Post alerts for all collisions meeting the severity threshold."""
        sent = skipped = errors = 0
        if not self.enabled:
            return SlackResult(0, len(collisions), 0)
        for c in collisions:
            if not self._meets_threshold(c.severity_label.value):
                skipped += 1
                continue
            try:
                emoji = _SEV_EMOJI.get(c.severity_label.value, "⚠️")
                resp = await self._http.post("/chat.postMessage", json={
                    "channel": self.channel,
                    "text": f"{emoji} MergeGuard collision on {c.intersecting_symbol} "
                            f"({c.mr_a_project}!{c.mr_a_iid} ↔ {c.mr_b_project}!{c.mr_b_iid})",
                    "blocks": self.build_blocks(c, map_url),
                })
                data = resp.json()
                if data.get("ok"):
                    sent += 1
                else:
                    errors += 1
                    logger.warning("Slack post failed: %s", data.get("error"))
            except Exception as exc:
                errors += 1
                logger.warning("Slack notify error: %s", exc)
        return SlackResult(sent, skipped, errors)

    async def close(self) -> None:
        await self._http.aclose()
