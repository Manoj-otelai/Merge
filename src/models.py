from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Language(str, Enum):
    PYTHON = "python"
    JAVASCRIPT = "javascript"
    TYPESCRIPT = "typescript"
    UNKNOWN = "unknown"


class ChangeType(str, Enum):
    RENAMED = "renamed"
    SIGNATURE_CHANGED = "signature_changed"
    REMOVED = "removed"
    ADDED = "added"


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class SymbolChange:
    name: str
    old_signature: str
    new_signature: str
    file_path: str
    language: Language
    change_type: ChangeType
    line_number: int = 0

    def __hash__(self) -> int:
        return hash((self.name, self.file_path, self.change_type))

    def signature_summary(self) -> str:
        if self.change_type == ChangeType.SIGNATURE_CHANGED:
            return f"`{self.old_signature}` → `{self.new_signature}`"
        elif self.change_type == ChangeType.RENAMED:
            return f"renamed `{self.old_signature}` → `{self.new_signature}`"
        elif self.change_type == ChangeType.REMOVED:
            return f"removed `{self.old_signature}`"
        return f"added `{self.new_signature}`"


@dataclass
class CallerInfo:
    function_name: str
    file_path: str
    project_path: str
    project_id: int
    owner: str = ""
    line_number: int = 0

    def __hash__(self) -> int:
        return hash((self.function_name, self.file_path, self.project_path))


@dataclass
class BlastRadius:
    mr_id: int
    mr_iid: int
    mr_url: str
    mr_title: str
    project_id: int
    project_path: str
    changed_symbols: list[SymbolChange] = field(default_factory=list)
    downstream_callers: list[CallerInfo] = field(default_factory=list)
    affected_owners: list[str] = field(default_factory=list)
    centrality_score: float = 0.0
    author: str = ""
    source_branch: str = ""

    def caller_symbol_names(self) -> set[str]:
        """Symbol names that exist in this MR's downstream blast radius."""
        return {c.function_name for c in self.downstream_callers}

    def changed_symbol_names(self) -> set[str]:
        """Symbol names changed by this MR."""
        return {s.name for s in self.changed_symbols}


@dataclass
class Collision:
    mr_a_id: int
    mr_a_iid: int
    mr_a_url: str
    mr_a_title: str
    mr_a_project: str
    mr_b_id: int
    mr_b_iid: int
    mr_b_url: str
    mr_b_title: str
    mr_b_project: str
    intersecting_symbol: str
    mr_a_changes: str
    mr_b_depends_on: str
    severity: float
    severity_label: Severity
    affected_callers: list[CallerInfo] = field(default_factory=list)
    affected_owners: list[str] = field(default_factory=list)
    suggested_order: str = ""
    confidence: float = 1.0
    explanation: str = ""
    score_factors: dict = field(default_factory=dict)

    def format_comment(self, perspective_mr_id: int, merge_plan_text: str = "") -> str:
        """Format a MR comment from the perspective of one of the two MRs."""
        if perspective_mr_id == self.mr_a_id:
            other_iid = self.mr_b_iid
            other_url = self.mr_b_url
            other_title = self.mr_b_title
            other_project = self.mr_b_project
            this_action = self.mr_a_changes
            other_action = self.mr_b_depends_on
        else:
            other_iid = self.mr_a_iid
            other_url = self.mr_a_url
            other_title = self.mr_a_title
            other_project = self.mr_a_project
            this_action = self.mr_b_depends_on
            other_action = self.mr_a_changes

        severity_emoji = {"low": "🟡", "medium": "🟠", "high": "🔴", "critical": "🚨"}
        emoji = severity_emoji.get(self.severity_label.value, "⚠️")

        owners_str = ", ".join(f"@{o}" for o in self.affected_owners) if self.affected_owners else "unknown"
        callers_str = f"{len(self.affected_callers)} caller(s)" if self.affected_callers else "unknown callers"

        caller_details = ""
        if self.affected_callers:
            lines = []
            for c in self.affected_callers[:5]:
                lines.append(f"  - `{c.function_name}` in `{c.file_path}` ({c.project_path})")
            caller_details = "\n**Affected callers:**\n" + "\n".join(lines)
            if len(self.affected_callers) > 5:
                caller_details += f"\n  - … and {len(self.affected_callers) - 5} more"

        confidence_pct = round(self.confidence * 100)
        why_section = ""
        if self.explanation:
            why_section = f"\n<details><summary><b>Why this is dangerous</b></summary>\n\n{self.explanation}\n\n</details>\n"

        plan_section = ""
        if merge_plan_text:
            plan_section = (
                f"\n<details><summary><b>Global merge plan</b> (all open MRs)</summary>\n\n"
                f"```\n{merge_plan_text}\n```\n\n</details>\n"
            )

        return f"""{emoji} **MergeGuard: Semantic Collision Detected** (Severity: **{self.severity_label.value.upper()}** · {confidence_pct}% confidence)

This MR has a **semantic conflict** with [{other_project}!{other_iid}]({other_url}) — *"{other_title}"*

**Collision on symbol:** `{self.intersecting_symbol}`

| | Action |
|---|---|
| This MR | {this_action} |
| !{other_iid} | {other_action} |

**Impact:** {callers_str} across {len(set(c.project_path for c in self.affected_callers))} project(s)
**Owners to notify:** {owners_str}
{caller_details}
{why_section}{plan_section}
**Suggested merge order:** {self.suggested_order}

> _Detected by [MergeGuard](https://gitlab.com/ai-catalog/mergeguard) via GitLab Orbit cross-repo graph traversal._
> _Orbit queried {len(self.affected_callers)} downstream callers across the dependency graph._"""


@dataclass
class CollisionMap:
    """Serializable collision map for the UI."""
    nodes: list[dict]
    edges: list[dict]
    total_collisions: int
    open_mrs: int

    def to_dict(self) -> dict:
        return {
            "nodes": self.nodes,
            "edges": self.edges,
            "total_collisions": self.total_collisions,
            "open_mrs": self.open_mrs,
        }
