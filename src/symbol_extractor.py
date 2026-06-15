"""Extract changed function/method/class signatures from MR unified diffs.

Critical design note: GitLab Orbit indexes only the default branch. Changed
symbols in feature branches are NOT in the Orbit graph. We must parse the MR
diff to identify what changed, then use Orbit to find who calls those symbols
on the default branch.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass

from .models import ChangeType, Language, SymbolChange


@dataclass
class FileDiff:
    file_path: str
    old_source: str
    new_source: str
    language: Language


def extract_from_diff(diff_text: str) -> list[SymbolChange]:
    """Parse a unified diff and return all changed symbol signatures."""
    file_diffs = _parse_unified_diff(diff_text)
    changes: list[SymbolChange] = []
    for fd in file_diffs:
        if fd.language == Language.PYTHON:
            changes.extend(_diff_python(fd))
        elif fd.language in (Language.JAVASCRIPT, Language.TYPESCRIPT):
            changes.extend(_diff_js(fd))
        elif fd.language in _REGEX_LANGS:
            changes.extend(_diff_regex(fd))
    return changes


def _parse_unified_diff(diff_text: str) -> list[FileDiff]:
    """Split a multi-file unified diff into per-file FileDiff objects.

    Uses a line-by-line state machine instead of a single regex so that blank
    context lines (which may appear as bare '\\n' in fixture diffs) don't
    terminate the hunk prematurely.
    """
    file_diffs: list[FileDiff] = []
    lines = diff_text.splitlines(keepends=True)
    i = 0

    while i < len(lines):
        line = lines[i]
        # Detect file header: --- a/<path>
        if line.startswith("--- "):
            raw_old = line[4:].rstrip("\n\r")
            old_path = raw_old[2:] if raw_old.startswith("a/") else raw_old

            if i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
                raw_new = lines[i + 1][4:].rstrip("\n\r")
                new_path = raw_new[2:] if raw_new.startswith("b/") else raw_new
                i += 2

                # Collect all hunk lines until the next file header
                hunk_lines: list[str] = []
                while i < len(lines):
                    ln = lines[i]
                    if ln.startswith("--- ") or ln.startswith("diff --git "):
                        break
                    hunk_lines.append(ln)
                    i += 1

                if old_path == "/dev/null":
                    old_path = new_path
                if new_path == "/dev/null":
                    new_path = old_path

                path = new_path
                language = _detect_language(path)
                if language == Language.UNKNOWN:
                    continue

                old_source, new_source = _reconstruct_sources("".join(hunk_lines))
                file_diffs.append(FileDiff(path, old_source, new_source, language))
                continue
        i += 1

    return file_diffs


def _reconstruct_sources(hunks: str) -> tuple[str, str]:
    """Reconstruct old and new file versions from hunk lines."""
    old_lines: list[str] = []
    new_lines: list[str] = []

    for line in hunks.splitlines():
        if line.startswith("@@"):
            continue
        if line.startswith("-"):
            old_lines.append(line[1:])
        elif line.startswith("+"):
            new_lines.append(line[1:])
        elif line.startswith(" "):
            # Standard context line: strip the leading space marker
            content = line[1:]
            old_lines.append(content)
            new_lines.append(content)
        else:
            # Blank line or non-standard prefix — treat as shared context
            old_lines.append(line)
            new_lines.append(line)

    return "\n".join(old_lines), "\n".join(new_lines)


def _detect_language(path: str) -> Language:
    if path.endswith(".py"):
        return Language.PYTHON
    if path.endswith((".js", ".jsx", ".mjs")):
        return Language.JAVASCRIPT
    if path.endswith((".ts", ".tsx")):
        return Language.TYPESCRIPT
    if path.endswith(".go"):
        return Language.GO
    if path.endswith(".rb"):
        return Language.RUBY
    if path.endswith(".java"):
        return Language.JAVA
    if path.endswith((".kt", ".kts")):
        return Language.KOTLIN
    if path.endswith(".rs"):
        return Language.RUST
    if path.endswith(".cs"):
        return Language.CSHARP
    if path.endswith(".php"):
        return Language.PHP
    return Language.UNKNOWN


# ── Python extraction ────────────────────────────────────────────────────────

def _parse_python_signatures(source: str) -> dict[str, tuple[str, int]]:
    """Return {function_name: (signature_string, line_no)} for all defs."""
    sigs: dict[str, tuple[str, int]] = {}
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return sigs

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            sig = _ast_func_signature(node)
            sigs[node.name] = (sig, node.lineno)
        elif isinstance(node, ast.ClassDef):
            # Track class names too — renames matter
            sigs[f"class:{node.name}"] = (f"class {node.name}", node.lineno)

    return sigs


def _ast_func_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Render a function node as a compact signature string."""
    args = node.args
    parts: list[str] = []

    # positional args
    defaults_offset = len(args.args) - len(args.defaults)
    for i, arg in enumerate(args.args):
        annotation = f": {ast.unparse(arg.annotation)}" if arg.annotation else ""
        default_idx = i - defaults_offset
        default = f" = {ast.unparse(args.defaults[default_idx])}" if default_idx >= 0 else ""
        parts.append(f"{arg.arg}{annotation}{default}")

    if args.vararg:
        parts.append(f"*{args.vararg.arg}")
    if args.kwonlyargs:
        if not args.vararg:
            parts.append("*")
        for i, arg in enumerate(args.kwonlyargs):
            annotation = f": {ast.unparse(arg.annotation)}" if arg.annotation else ""
            default = f" = {ast.unparse(args.kw_defaults[i])}" if args.kw_defaults[i] else ""
            parts.append(f"{arg.arg}{annotation}{default}")
    if args.kwarg:
        parts.append(f"**{args.kwarg.arg}")

    ret = f" -> {ast.unparse(node.returns)}" if node.returns else ""
    prefix = "async def " if isinstance(node, ast.AsyncFunctionDef) else "def "
    return f"{prefix}{node.name}({', '.join(parts)}){ret}"


def _diff_python(fd: FileDiff) -> list[SymbolChange]:
    old_sigs = _parse_python_signatures(fd.old_source)
    new_sigs = _parse_python_signatures(fd.new_source)
    return _compute_changes(old_sigs, new_sigs, fd.file_path, fd.language)


# ── Generic regex-based extraction (Go, Ruby, Java, Kotlin, Rust, C#, PHP) ───
# Each language maps to (function_patterns, type_pattern). Function patterns must
# capture (1) the symbol name and (2) the parameter list. Signatures are keyed by
# name, so a change is only flagged when a known symbol's params change or it is
# removed — which keeps these forgiving regexes from producing false positives.

_REGEX_LANG_PATTERNS: dict[Language, tuple[list[re.Pattern], re.Pattern | None]] = {
    Language.GO: (
        [re.compile(r"^func\s+(?:\([^)]*\)\s*)?(\w+)\s*\(([^)]*)\)", re.MULTILINE)],
        re.compile(r"^type\s+(\w+)\s+(?:struct|interface)", re.MULTILINE),
    ),
    Language.RUBY: (
        [re.compile(r"^\s*def\s+(?:self\.)?(\w+[!?=]?)\s*(?:\(([^)]*)\))?", re.MULTILINE)],
        re.compile(r"^\s*(?:class|module)\s+(\w+)", re.MULTILINE),
    ),
    Language.JAVA: (
        [re.compile(
            r"^\s*(?:@\w+\s+)*(?:public|private|protected|static|final|abstract|synchronized|native|\s)+"
            r"[\w<>\[\],.\s]+?\s+(\w+)\s*\(([^)]*)\)\s*(?:throws[\w,\s.]+)?\{",
            re.MULTILINE)],
        re.compile(r"^\s*(?:public|private|protected|abstract|final|\s)*(?:class|interface|enum|record)\s+(\w+)", re.MULTILINE),
    ),
    Language.KOTLIN: (
        [re.compile(r"^\s*(?:(?:public|private|protected|internal|open|override|suspend|inline|\s)+)?fun\s+(?:<[^>]+>\s*)?(\w+)\s*\(([^)]*)\)", re.MULTILINE)],
        re.compile(r"^\s*(?:data\s+|sealed\s+|abstract\s+|open\s+)?(?:class|interface|object)\s+(\w+)", re.MULTILINE),
    ),
    Language.RUST: (
        [re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+(\w+)\s*(?:<[^>]*>)?\s*\(([^)]*)\)", re.MULTILINE)],
        re.compile(r"^\s*(?:pub\s+)?(?:struct|enum|trait)\s+(\w+)", re.MULTILINE),
    ),
    Language.CSHARP: (
        [re.compile(
            r"^\s*(?:\[[^\]]+\]\s*)*(?:public|private|protected|internal|static|virtual|override|async|sealed|partial|\s)+"
            r"[\w<>\[\],.\s]+?\s+(\w+)\s*\(([^)]*)\)",
            re.MULTILINE)],
        re.compile(r"^\s*(?:public|private|protected|internal|abstract|sealed|partial|\s)*(?:class|interface|struct|record|enum)\s+(\w+)", re.MULTILINE),
    ),
    Language.PHP: (
        [re.compile(r"^\s*(?:(?:public|private|protected|static|abstract|final)\s+)*function\s+(\w+)\s*\(([^)]*)\)", re.MULTILINE)],
        re.compile(r"^\s*(?:abstract\s+|final\s+)?(?:class|interface|trait)\s+(\w+)", re.MULTILINE),
    ),
}

_REGEX_LANGS = frozenset(_REGEX_LANG_PATTERNS)


def _parse_regex_signatures(source: str, language: Language) -> dict[str, tuple[str, int]]:
    func_patterns, class_pattern = _REGEX_LANG_PATTERNS[language]
    sigs: dict[str, tuple[str, int]] = {}

    for pattern in func_patterns:
        for match in pattern.finditer(source):
            name = match.group(1)
            params_group = match.group(2) if match.lastindex and match.lastindex >= 2 else ""
            params = re.sub(r"\s+", " ", (params_group or "").strip())
            line_no = source[: match.start()].count("\n") + 1
            if name not in sigs:
                sigs[name] = (f"{name}({params})", line_no)

    if class_pattern:
        for match in class_pattern.finditer(source):
            name = match.group(1)
            line_no = source[: match.start()].count("\n") + 1
            sigs[f"class:{name}"] = (f"type {name}", line_no)

    return sigs


def _diff_regex(fd: FileDiff) -> list[SymbolChange]:
    old_sigs = _parse_regex_signatures(fd.old_source, fd.language)
    new_sigs = _parse_regex_signatures(fd.new_source, fd.language)
    return _compute_changes(old_sigs, new_sigs, fd.file_path, fd.language)


# ── JavaScript / TypeScript extraction ──────────────────────────────────────

_JS_FUNC_PATTERNS = [
    # function name(params)
    re.compile(r"^(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\(([^)]*)\)", re.MULTILINE),
    # const name = (params) =>  /  const name = async (params) =>
    re.compile(r"^(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\(([^)]*)\)\s*=>", re.MULTILINE),
    # class method:  name(params) {
    re.compile(r"^\s+(?:async\s+)?(\w+)\s*\(([^)]*)\)\s*\{", re.MULTILINE),
    # TypeScript: public/private/protected methods
    re.compile(r"^\s+(?:public|private|protected|static|async|\s)+\s+(\w+)\s*\(([^)]*)\)", re.MULTILINE),
]

_CLASS_PATTERN = re.compile(r"^(?:export\s+)?(?:abstract\s+)?class\s+(\w+)", re.MULTILINE)


def _parse_js_signatures(source: str) -> dict[str, tuple[str, int]]:
    sigs: dict[str, tuple[str, int]] = {}

    for pattern in _JS_FUNC_PATTERNS:
        for match in pattern.finditer(source):
            name = match.group(1)
            params = re.sub(r"\s+", " ", match.group(2).strip())
            line_no = source[: match.start()].count("\n") + 1
            sig = f"{name}({params})"
            if name not in sigs:
                sigs[name] = (sig, line_no)

    for match in _CLASS_PATTERN.finditer(source):
        name = match.group(1)
        line_no = source[: match.start()].count("\n") + 1
        sigs[f"class:{name}"] = (f"class {name}", line_no)

    return sigs


def _diff_js(fd: FileDiff) -> list[SymbolChange]:
    old_sigs = _parse_js_signatures(fd.old_source)
    new_sigs = _parse_js_signatures(fd.new_source)
    return _compute_changes(old_sigs, new_sigs, fd.file_path, fd.language)


# ── Shared diff logic ────────────────────────────────────────────────────────

def _compute_changes(
    old_sigs: dict[str, tuple[str, int]],
    new_sigs: dict[str, tuple[str, int]],
    file_path: str,
    language: Language,
) -> list[SymbolChange]:
    changes: list[SymbolChange] = []

    all_names = set(old_sigs) | set(new_sigs)
    for name in all_names:
        old = old_sigs.get(name)
        new = new_sigs.get(name)

        if old is None and new is not None:
            changes.append(SymbolChange(
                name=name,
                old_signature="",
                new_signature=new[0],
                file_path=file_path,
                language=language,
                change_type=ChangeType.ADDED,
                line_number=new[1],
            ))
        elif old is not None and new is None:
            changes.append(SymbolChange(
                name=name,
                old_signature=old[0],
                new_signature="",
                file_path=file_path,
                language=language,
                change_type=ChangeType.REMOVED,
                line_number=old[1],
            ))
        elif old is not None and new is not None and old[0] != new[0]:
            changes.append(SymbolChange(
                name=name,
                old_signature=old[0],
                new_signature=new[0],
                file_path=file_path,
                language=language,
                change_type=ChangeType.SIGNATURE_CHANGED,
                line_number=new[1],
            ))

    # Filter out ADDED — newly added symbols can't cause collision (nothing calls them yet)
    return [c for c in changes if c.change_type != ChangeType.ADDED]
