"""Tests for the auto-fix MR generator (Phase 1.2)."""
import ast

from src.autofix import generate_fix
from src.models import CallerInfo, ChangeType, Language, SymbolChange


def sym(name="charge_user", old="def charge_user(amount: float) -> bool",
        new="def charge_user(amount: float, currency: str = 'USD') -> bool",
        change_type=ChangeType.SIGNATURE_CHANGED) -> SymbolChange:
    return SymbolChange(
        name=name, old_signature=old, new_signature=new,
        file_path="src/payments/charge.py", language=Language.PYTHON,
        change_type=change_type,
    )


def caller(fn, path, project="demo-group/notif", pid=102) -> CallerInfo:
    return CallerInfo(fn, path, project, pid, owner="maria")


INVOICE_SRC = (
    "from payments.charge import charge_user\n"
    "\n"
    "def send_invoice(uid, amount):\n"
    "    ok = charge_user(amount)\n"
    "    return ok\n"
)


class TestAddArgument:
    def test_appends_new_argument_at_call_site(self):
        provider = lambda pid, path: INVOICE_SRC
        plan = generate_fix(sym(), [caller("send_invoice", "src/notifications/invoice.py")], provider)
        assert plan.fixable
        assert plan.total_edits == 1
        patched = plan.patches[0].new_content
        assert "charge_user(amount, currency='USD')" in patched

    def test_patch_is_valid_python(self):
        provider = lambda pid, path: INVOICE_SRC
        plan = generate_fix(sym(), [caller("send_invoice", "src/notifications/invoice.py")], provider)
        ast.parse(plan.patches[0].new_content)  # must not raise

    def test_does_not_double_add_if_already_present(self):
        already = "from x import charge_user\nok = charge_user(amount, currency='EUR')\n"
        provider = lambda pid, path: already
        plan = generate_fix(sym(), [caller("send_invoice", "a.py")], provider)
        assert plan.total_edits == 0

    def test_source_unavailable_marked_unfixable(self):
        provider = lambda pid, path: None
        plan = generate_fix(sym(), [caller("send_invoice", "a.py")], provider)
        assert not plan.fixable
        assert any("unavailable" in u for u in plan.unfixable)

    def test_dedups_same_file(self):
        provider = lambda pid, path: INVOICE_SRC
        callers = [
            caller("send_invoice", "src/notifications/invoice.py"),
            caller("send_subscription_renewal", "src/notifications/invoice.py"),
        ]
        plan = generate_fix(sym(), callers, provider)
        # Only one patch for the shared file
        assert len(plan.patches) == 1


class TestRename:
    def test_renames_call_references(self):
        src = "from x import charge_user\nresult = charge_user(amount)\n"
        provider = lambda pid, path: src
        s = sym(new="def bill_user(amount: float) -> bool", change_type=ChangeType.RENAMED)
        plan = generate_fix(s, [caller("send_invoice", "a.py")], provider)
        assert plan.fixable
        assert "bill_user(amount)" in plan.patches[0].new_content

    def test_rename_patch_is_valid_python(self):
        src = "from x import charge_user\nresult = charge_user(amount)\n"
        provider = lambda pid, path: src
        s = sym(new="def bill_user(amount) -> bool", change_type=ChangeType.RENAMED)
        plan = generate_fix(s, [caller("send_invoice", "a.py")], provider)
        ast.parse(plan.patches[0].new_content)


class TestRemoved:
    def test_removed_is_not_auto_fixable(self):
        provider = lambda pid, path: INVOICE_SRC
        s = sym(new="", change_type=ChangeType.REMOVED)
        plan = generate_fix(s, [caller("send_invoice", "a.py")], provider)
        assert not plan.fixable
        assert plan.unfixable


class TestMetadata:
    def test_branch_and_owners(self):
        provider = lambda pid, path: INVOICE_SRC
        plan = generate_fix(sym(), [caller("send_invoice", "src/notifications/invoice.py")], provider)
        assert plan.branch_name == "mergeguard/autofix-charge-user"
        assert "maria" in plan.owners
        assert "charge_user" in plan.title
        assert "@maria" in plan.description
