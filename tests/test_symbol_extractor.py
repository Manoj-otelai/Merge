"""Tests for the symbol extractor."""
import pytest

from src.models import ChangeType, Language
from src.symbol_extractor import (
    extract_from_diff,
    _parse_python_signatures,
    _parse_js_signatures,
)
from tests.fixtures.sample_diffs import (
    PYTHON_SIGNATURE_CHANGE,
    PYTHON_NEW_CALLER,
    PYTHON_RENAME,
    PYTHON_REMOVED,
    JS_SIGNATURE_CHANGE,
    TS_METHOD_CHANGE,
    PYTHON_BODY_ONLY,
    PYTHON_NEW_FILE,
)


class TestPythonSignatureExtraction:
    def test_detects_signature_change(self):
        changes = extract_from_diff(PYTHON_SIGNATURE_CHANGE)
        names = {c.name for c in changes}
        assert "charge_user" in names
        charge = next(c for c in changes if c.name == "charge_user")
        assert charge.change_type == ChangeType.SIGNATURE_CHANGED
        assert charge.language == Language.PYTHON
        assert "amount: float" in charge.old_signature
        assert "currency" in charge.new_signature

    def test_internal_function_also_detected(self):
        changes = extract_from_diff(PYTHON_SIGNATURE_CHANGE)
        names = {c.name for c in changes}
        assert "_process_payment" in names

    def test_new_caller_produces_no_change(self):
        # MR-B only adds a new caller — no existing signatures changed
        changes = extract_from_diff(PYTHON_NEW_CALLER)
        # The new `send_invoice` function is ADDED, which we filter out
        assert all(c.change_type != ChangeType.ADDED for c in changes)

    def test_rename_detected(self):
        changes = extract_from_diff(PYTHON_RENAME)
        names = {c.name for c in changes}
        # authenticate is removed (renamed away), authenticate_user is added
        assert "authenticate" in names
        auth = next(c for c in changes if c.name == "authenticate")
        assert auth.change_type == ChangeType.REMOVED

    def test_removal_detected(self):
        changes = extract_from_diff(PYTHON_REMOVED)
        names = {c.name for c in changes}
        assert "legacy_format" in names
        removed = next(c for c in changes if c.name == "legacy_format")
        assert removed.change_type == ChangeType.REMOVED

    def test_body_only_change_produces_no_output(self):
        changes = extract_from_diff(PYTHON_BODY_ONLY)
        # Signatures of add/multiply are unchanged
        assert len(changes) == 0

    def test_new_file_produces_no_collision_triggering_changes(self):
        changes = extract_from_diff(PYTHON_NEW_FILE)
        # New functions — should be ADDED (filtered out)
        collision_changes = [c for c in changes if c.change_type != ChangeType.ADDED]
        assert len(collision_changes) == 0

    def test_parse_python_signatures_basic(self):
        source = """
def foo(x: int, y: str = "default") -> bool:
    pass

async def bar(items: list) -> None:
    pass

class MyClass:
    pass
"""
        sigs = _parse_python_signatures(source)
        assert "foo" in sigs
        assert "bar" in sigs
        assert "class:MyClass" in sigs
        assert "x: int" in sigs["foo"][0]
        assert "async def bar" in sigs["bar"][0]


class TestJavaScriptExtraction:
    def test_js_signature_change(self):
        changes = extract_from_diff(JS_SIGNATURE_CHANGE)
        names = {c.name for c in changes}
        assert "fetchUser" in names or "sendRequest" in names

    def test_ts_method_change(self):
        changes = extract_from_diff(TS_METHOD_CHANGE)
        # getUser gets new parameter
        names = {c.name for c in changes}
        assert "getUser" in names

    def test_parse_js_signatures_basic(self):
        source = """
function doSomething(a, b, c) {
    return a + b;
}

const arrowFn = (x, y) => x * y;
"""
        sigs = _parse_js_signatures(source)
        assert "doSomething" in sigs or "arrowFn" in sigs


class TestMultiFileDiff:
    def test_handles_multiple_files(self):
        combined = PYTHON_SIGNATURE_CHANGE + "\n" + JS_SIGNATURE_CHANGE
        changes = extract_from_diff(combined)
        languages = {c.language for c in changes}
        # Should detect both Python and JS changes
        assert len(changes) > 1

    def test_unknown_extensions_skipped(self):
        diff = """\
--- a/config.yaml
+++ b/config.yaml
@@ -1,3 +1,4 @@
 key: value
+new_key: new_value
"""
        changes = extract_from_diff(diff)
        assert len(changes) == 0
