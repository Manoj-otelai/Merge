"""Auto-fix MR generation (Phase 1.2).

When MR-A changes a contract that MR-B (or the default branch) depends on,
MergeGuard can auto-draft the *consumer-side* fix: it rewrites the affected
call sites to match the new signature, validates that the result still parses,
and (when enabled) opens a draft MR that @-mentions the owners Orbit found.

Design choices:
  * The rewriter is **deterministic and AST-validated**, not an opaque LLM
    guess — mechanical signature updates are exactly the kind of edit where a
    syntax-aware transform beats free-form generation, and every produced file
    is re-parsed so a patch can never be a non-compiling placeholder.
  * The pure generator takes a `source_provider` callable, so it is fully
    unit-testable offline. The GitLab side-effects (branch/commit/MR) live in
    `gitlab_client` and are only invoked behind the MERGEGUARD_AUTOFIX_ENABLED
    flag.

Supported transforms (Python):
  * RENAMED            → replace the callee name at each call site
  * SIGNATURE_CHANGED  → append the newly-added argument (with its default)
                         at each call site so the call is explicit & correct
  * REMOVED            → not auto-fixable; reported for manual handling
"""
from __future__ import annotations

import ast
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from .models import CallerInfo, ChangeType, SymbolChange

logger = logging.getLogger(__name__)

# (project_id, file_path) -> source text, or None if unavailable
SourceProvider = Callable[[int, str], str | None]


@dataclass
class FilePatch:
    file_path: str
    project_path: str
    project_id: int
    new_content: str
    edits: int

    def to_dict(self) -> dict:
        return {
            "file_path": self.file_path,
            "project_path": self.project_path,
            "project_id": self.project_id,
            "edits": self.edits,
        }


@dataclass
class AutoFixPlan:
    symbol: str
    change_type: str
    branch_name: str
    title: str
    description: str
    patches: list[FilePatch] = field(default_factory=list)
    owners: list[str] = field(default_factory=list)
    unfixable: list[str] = field(default_factory=list)

    @property
    def total_edits(self) -> int:
        return sum(p.edits for p in self.patches)

    @property
    def fixable(self) -> bool:
        return self.total_edits > 0

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "change_type": self.change_type,
            "branch_name": self.branch_name,
            "title": self.title,
            "description": self.description,
            "patches": [p.to_dict() for p in self.patches],
            "owners": self.owners,
            "unfixable": self.unfixable,
            "total_edits": self.total_edits,
            "fixable": self.fixable,
        }


# ── Signature parsing ────────────────────────────────────────────────────────

@dataclass
class _Param:
    name: str
    has_default: bool
    default_src: str  # source text of the default, e.g. "'USD'"


def _parse_signature_params(signature: str) -> list[_Param]:
    """Extract parameters from a signature string like
    'def charge_user(amount: float, currency: str = "USD") -> bool'.
    """
    if not signature:
        return []
    start = signature.find("(")
    if start == -1:
        return []
    depth = 0
    end = -1
    for i in range(start, len(signature)):
        if signature[i] == "(":
            depth += 1
        elif signature[i] == ")":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end == -1:
        return []
    inner = signature[start + 1 : end]
    stub = f"def _f({inner}): pass"
    try:
        tree = ast.parse(stub)
    except SyntaxError:
        return []
    fn = tree.body[0]
    assert isinstance(fn, ast.FunctionDef)
    args = fn.args
    params: list[_Param] = []
    pos = args.posonlyargs + args.args
    defaults = args.defaults
    offset = len(pos) - len(defaults)
    for idx, a in enumerate(pos):
        if a.arg in ("self", "cls"):
            continue
        d_idx = idx - offset
        if d_idx >= 0:
            params.append(_Param(a.arg, True, ast.unparse(defaults[d_idx])))
        else:
            params.append(_Param(a.arg, False, ""))
    for i, a in enumerate(args.kwonlyargs):
        dflt = args.kw_defaults[i]
        if dflt is not None:
            params.append(_Param(a.arg, True, ast.unparse(dflt)))
        else:
            params.append(_Param(a.arg, False, ""))
    return params


def _added_param(old_sig: str, new_sig: str) -> _Param | None:
    """Return the first parameter present in new_sig but not old_sig."""
    old = {p.name for p in _parse_signature_params(old_sig)}
    for p in _parse_signature_params(new_sig):
        if p.name not in old:
            return p
    return None


# ── Call-site rewriting (Python) ─────────────────────────────────────────────

def _rewrite_python_add_arg(source: str, symbol: str, param: _Param) -> tuple[str, int]:
    """Append `param` (with its default) to each call of `symbol`.

    Returns (new_source, num_edits). The result is validated with ast.parse;
    if it does not parse, the original source is returned with 0 edits.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source, 0

    arg_text = f"{param.name}={param.default_src}" if param.default_src else param.name

    # Collect insertion points (byte offsets just before the call's closing ')')
    insertions: list[tuple[int, int, str]] = []  # (lineno, col_offset_end, text)
    lines = source.splitlines(keepends=True)
    line_starts = _line_start_offsets(lines)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not _call_targets(node, symbol):
            continue
        # Skip if the param is already passed explicitly
        if any(kw.arg == param.name for kw in node.keywords):
            continue
        # Insert just before the closing paren: use end position of last arg/kw,
        # falling back to the call's end.
        end_line = node.end_lineno
        end_col = node.end_col_offset
        if end_line is None or end_col is None:
            continue
        # node.end_col_offset points just after ')'. Insert before that ')'.
        abs_offset = line_starts[end_line - 1] + end_col - 1
        has_args = bool(node.args or node.keywords)
        text = f", {arg_text}" if has_args else arg_text
        insertions.append((abs_offset, 0, text))

    if not insertions:
        return source, 0

    # Apply from the end so offsets stay valid
    new_source = source
    for abs_offset, _pad, text in sorted(insertions, key=lambda x: x[0], reverse=True):
        new_source = new_source[:abs_offset] + text + new_source[abs_offset:]

    # Validate
    try:
        ast.parse(new_source)
    except SyntaxError:
        logger.warning("Auto-fix produced invalid Python for %s; skipping file", symbol)
        return source, 0

    return new_source, len(insertions)


def _rewrite_python_rename(source: str, old_name: str, new_name: str) -> tuple[str, int]:
    """Rename call references old_name → new_name, validated with ast.parse."""
    try:
        ast.parse(source)
    except SyntaxError:
        return source, 0

    pattern = re.compile(rf"\b{re.escape(old_name)}\b")
    # Only rewrite where followed (allowing spaces) by '(' to target calls.
    def repl(m: re.Match) -> str:
        tail = source[m.end():]
        if re.match(r"\s*\(", tail):
            return new_name
        return m.group(0)

    new_source, n = pattern.subn(repl, source)
    if n == 0:
        return source, 0
    # subn counts all matches incl. non-calls left unchanged; recount real edits
    edits = len(re.findall(rf"\b{re.escape(old_name)}\b\s*\(", source))
    try:
        ast.parse(new_source)
    except SyntaxError:
        return source, 0
    return new_source, edits


def _call_targets(node: ast.Call, symbol: str) -> bool:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == symbol
    if isinstance(func, ast.Attribute):
        return func.attr == symbol
    return False


def _line_start_offsets(lines: list[str]) -> list[int]:
    offsets = [0]
    total = 0
    for ln in lines:
        total += len(ln)
        offsets.append(total)
    return offsets


# ── Plan generation ──────────────────────────────────────────────────────────

def generate_fix(
    symbol_change: SymbolChange,
    callers: list[CallerInfo],
    source_provider: SourceProvider,
    other_mr_iid: int = 0,
) -> AutoFixPlan:
    """Build an AutoFixPlan that updates affected callers to the new contract."""
    sym = symbol_change.name
    branch = f"mergeguard/autofix-{sym}".replace("_", "-")
    owners = list(dict.fromkeys(c.owner for c in callers if c.owner))

    plan = AutoFixPlan(
        symbol=sym,
        change_type=symbol_change.change_type.value,
        branch_name=branch,
        title=f"Draft: MergeGuard auto-fix — update callers of `{sym}`",
        description="",
        owners=owners,
    )

    if symbol_change.change_type == ChangeType.REMOVED:
        plan.unfixable.append(
            f"`{sym}` is being removed — call sites need a manual replacement, "
            f"so no automatic patch was generated."
        )
        plan.description = _describe(plan, symbol_change, other_mr_iid)
        return plan

    added = None
    if symbol_change.change_type == ChangeType.SIGNATURE_CHANGED:
        added = _added_param(symbol_change.old_signature, symbol_change.new_signature)
        if added is None:
            plan.unfixable.append(
                f"Could not determine a safe mechanical edit for the signature "
                f"change of `{sym}` (no single added parameter)."
            )

    new_name = None
    if symbol_change.change_type == ChangeType.RENAMED:
        # new_signature for a rename holds the new name
        new_name = _name_from_signature(symbol_change.new_signature) or symbol_change.new_signature

    # Group caller files (dedup by project_id + file_path)
    seen_files: set[tuple] = set()
    for c in callers:
        if not c.file_path or c.file_path.endswith((".js", ".ts", ".jsx", ".tsx")):
            # Pure-Python rewriter for now; non-Python reported as unfixable.
            if c.file_path and not c.file_path.endswith(".py"):
                plan.unfixable.append(f"{c.project_path}/{c.file_path} (non-Python, manual)")
            continue
        if not c.file_path.endswith(".py"):
            continue
        key = (c.project_id, c.file_path)
        if key in seen_files:
            continue
        seen_files.add(key)

        source = source_provider(c.project_id, c.file_path)
        if not source:
            plan.unfixable.append(f"{c.project_path}/{c.file_path} (source unavailable)")
            continue

        if symbol_change.change_type == ChangeType.SIGNATURE_CHANGED and added:
            new_src, n = _rewrite_python_add_arg(source, sym, added)
        elif symbol_change.change_type == ChangeType.RENAMED and new_name:
            new_src, n = _rewrite_python_rename(source, sym, new_name)
        else:
            new_src, n = source, 0

        if n > 0:
            plan.patches.append(FilePatch(
                file_path=c.file_path,
                project_path=c.project_path,
                project_id=c.project_id,
                new_content=new_src,
                edits=n,
            ))
        else:
            plan.unfixable.append(f"{c.project_path}/{c.file_path} (no call site rewritten)")

    plan.description = _describe(plan, symbol_change, other_mr_iid)
    return plan


def _name_from_signature(signature: str) -> str | None:
    m = re.search(r"(?:def|class)\s+(\w+)", signature)
    if m:
        return m.group(1)
    m = re.match(r"\s*(\w+)\s*\(", signature)
    return m.group(1) if m else None


def _describe(plan: AutoFixPlan, sym: SymbolChange, other_mr_iid: int) -> str:
    mentions = " ".join(f"@{o}" for o in plan.owners) if plan.owners else ""
    lines = [
        f"**MergeGuard auto-fix** for the upcoming contract change of `{sym.name}`.",
        "",
        f"- Change: {sym.signature_summary()}",
    ]
    if other_mr_iid:
        lines.append(f"- Triggered by collision with !{other_mr_iid}")
    lines.append(f"- Files updated: **{len(plan.patches)}** ({plan.total_edits} call site(s))")
    for p in plan.patches:
        lines.append(f"  - `{p.project_path}/{p.file_path}` — {p.edits} edit(s)")
    if plan.unfixable:
        lines.append("")
        lines.append("**Needs manual attention:**")
        for u in plan.unfixable:
            lines.append(f"  - {u}")
    if mentions:
        lines += ["", f"cc {mentions}"]
    lines += ["", "_Patches are AST-validated — every modified file is re-parsed before commit._"]
    return "\n".join(lines)
