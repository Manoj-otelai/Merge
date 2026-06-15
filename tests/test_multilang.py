"""Tests for multi-language symbol extraction (Phase 5.1)."""
from src.models import ChangeType, Language
from src.symbol_extractor import extract_from_diff


def diff(path: str, old_line: str, new_line: str) -> str:
    return (
        f"--- a/{path}\n+++ b/{path}\n"
        f"@@ -1,3 +1,3 @@\n"
        f"-{old_line}\n"
        f"+{new_line}\n"
        f"     pass\n"
    )


class TestGo:
    def test_signature_change(self):
        d = diff("svc/charge.go",
                 "func ChargeUser(amount float64) bool {",
                 "func ChargeUser(amount float64, currency string) bool {")
        changes = extract_from_diff(d)
        names = {c.name for c in changes}
        assert "ChargeUser" in names
        assert any(c.language == Language.GO for c in changes)

    def test_method_with_receiver(self):
        d = diff("svc/p.go",
                 "func (p *Payments) Charge(a int) error {",
                 "func (p *Payments) Charge(a int, cur string) error {")
        changes = extract_from_diff(d)
        assert "Charge" in {c.name for c in changes}


class TestRuby:
    def test_signature_change(self):
        d = diff("app/charge.rb",
                 "def charge_user(amount)",
                 "def charge_user(amount, currency)")
        changes = extract_from_diff(d)
        c = next(c for c in changes if c.name == "charge_user")
        assert c.language == Language.RUBY
        assert c.change_type == ChangeType.SIGNATURE_CHANGED


class TestJava:
    def test_signature_change(self):
        d = diff("Charge.java",
                 "public boolean chargeUser(double amount) {",
                 "public boolean chargeUser(double amount, String currency) {")
        changes = extract_from_diff(d)
        assert "chargeUser" in {c.name for c in changes}


class TestKotlin:
    def test_signature_change(self):
        d = diff("Charge.kt",
                 "fun chargeUser(amount: Double): Boolean {",
                 "fun chargeUser(amount: Double, currency: String): Boolean {")
        changes = extract_from_diff(d)
        assert "chargeUser" in {c.name for c in changes}


class TestRust:
    def test_signature_change(self):
        d = diff("charge.rs",
                 "pub fn charge_user(amount: f64) -> bool {",
                 "pub fn charge_user(amount: f64, currency: String) -> bool {")
        changes = extract_from_diff(d)
        c = next(c for c in changes if c.name == "charge_user")
        assert c.language == Language.RUST


class TestPhp:
    def test_signature_change(self):
        d = diff("Charge.php",
                 "public function chargeUser($amount) {",
                 "public function chargeUser($amount, $currency) {")
        changes = extract_from_diff(d)
        assert "chargeUser" in {c.name for c in changes}


class TestCSharp:
    def test_signature_change(self):
        d = diff("Charge.cs",
                 "public bool ChargeUser(double amount) {",
                 "public bool ChargeUser(double amount, string currency) {")
        changes = extract_from_diff(d)
        assert "ChargeUser" in {c.name for c in changes}


class TestNoFalsePositiveOnUnchanged:
    def test_body_only_change_not_flagged(self):
        d = (
            "--- a/x.go\n+++ b/x.go\n@@ -1,3 +1,3 @@\n"
            " func ChargeUser(amount float64) bool {\n"
            "-    return amount > 0\n"
            "+    return amount >= 0\n"
            " }\n"
        )
        changes = extract_from_diff(d)
        # Signature unchanged → no collision-causing change
        assert all(c.name != "ChargeUser" for c in changes)
