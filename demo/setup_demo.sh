#!/usr/bin/env bash
# MergeGuard demo setup — creates two colliding MRs in a GitLab group
set -euo pipefail

GITLAB_GROUP="${GITLAB_GROUP:?Set GITLAB_GROUP to your GitLab group path}"
GITLAB_URL="${GITLAB_URL:-https://gitlab.com}"
MERGEGUARD_URL="${MERGEGUARD_URL:-http://localhost:8080}"
WEBHOOK_SECRET="${MERGEGUARD_WEBHOOK_SECRET:-demo-secret}"

PAYMENTS_REPO="$GITLAB_GROUP/payments-service"
NOTIF_REPO="$GITLAB_GROUP/notification-service"
WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

info() { echo "▶ $*"; }
ok()   { echo "✓ $*"; }

# ── Create repos ─────────────────────────────────────────────────────────────
info "Creating demo repositories in $GITLAB_GROUP..."

glab repo create "$PAYMENTS_REPO" --private --no-clone 2>/dev/null || true
glab repo create "$NOTIF_REPO" --private --no-clone 2>/dev/null || true
ok "Repositories ready"

# ── Seed payments-service (base: old charge_user signature) ──────────────────
info "Seeding payments-service..."
git -C "$WORKDIR" clone "$GITLAB_URL/$PAYMENTS_REPO.git" payments-service
pushd "$WORKDIR/payments-service"

mkdir -p src/payments
cat > src/__init__.py << 'EOF'
EOF

cat > src/payments/__init__.py << 'EOF'
EOF

cat > src/payments/charge.py << 'EOF'
"""Payment processing service."""
from decimal import Decimal
from typing import Optional


class PaymentService:
    """Handles user payment processing."""

    def charge_user(self, amount: float) -> bool:
        """
        Charge the user's default payment method.

        Args:
            amount: Amount to charge in dollars

        Returns:
            True if charge succeeded, False otherwise
        """
        return self._process_payment(amount)

    def get_user_balance(self, user_id: int) -> Decimal:
        """Fetch the user's current account balance."""
        return Decimal("0.00")

    def _process_payment(self, amount: float) -> bool:
        """Internal payment processor."""
        return True
EOF

git add .
git commit -m "Initial payments service with charge_user"
git push origin main
ok "payments-service seeded"

# ── Create MR-A branch: currency parameter addition ─────────────────────────
info "Creating MR-A: charge_user signature change..."
git checkout -b feature/add-currency-support

cat > src/payments/charge.py << 'EOF'
"""Payment processing service."""
from decimal import Decimal
from typing import Optional


class PaymentService:
    """Handles user payment processing."""

    def charge_user(self, amount: float, currency: str = "USD") -> bool:
        """
        Charge the user's default payment method in the specified currency.

        Args:
            amount: Amount to charge in dollars
            currency: ISO 4217 currency code (default: USD)

        Returns:
            True if charge succeeded, False otherwise
        """
        return self._process_payment(amount, currency)

    def get_user_balance(self, user_id: int) -> Decimal:
        """Fetch the user's current account balance."""
        return Decimal("0.00")

    def _process_payment(self, amount: float, currency: str) -> bool:
        """Internal payment processor."""
        return True
EOF

git add .
git commit -m "feat: add currency parameter to charge_user"
git push origin feature/add-currency-support

MR_A_IID=$(glab mr create \
  --title "feat: add currency support to charge_user" \
  --description "Adds ISO 4217 currency parameter to charge_user. Required for multi-currency support." \
  --source-branch feature/add-currency-support \
  --target-branch main \
  --yes \
  --print-url | grep -oE '[0-9]+$')
ok "MR-A created: $PAYMENTS_REPO!$MR_A_IID"
popd

# ── Seed notification-service ────────────────────────────────────────────────
info "Seeding notification-service..."
git -C "$WORKDIR" clone "$GITLAB_URL/$NOTIF_REPO.git" notification-service
pushd "$WORKDIR/notification-service"

mkdir -p src/notifications
cat > src/__init__.py << 'EOF'
EOF

cat > src/notifications/__init__.py << 'EOF'
EOF

cat > src/notifications/invoice.py << 'EOF'
"""Invoice notification service."""


class InvoiceNotifier:
    """Sends invoice notifications."""

    def notify_user(self, user_id: int, message: str) -> None:
        """Send a notification to the user."""
        self._send_email(user_id, message)

    def _send_email(self, user_id: int, body: str) -> None:
        """Internal email sender."""
        pass
EOF

git add .
git commit -m "Initial notification service"
git push origin main
ok "notification-service seeded"

# ── Create MR-B branch: new callers of old charge_user signature ─────────────
info "Creating MR-B: new callers using old charge_user signature..."
git checkout -b feature/invoice-notifications

cat > src/notifications/invoice.py << 'EOF'
"""Invoice notification service."""
import sys
sys.path.insert(0, "../payments-service")

try:
    from src.payments.charge import PaymentService
except ImportError:
    # In a real multi-repo setup, this would be a cross-repo import
    class PaymentService:
        def charge_user(self, amount):
            return True


class InvoiceNotifier:
    """Sends invoice notifications and processes payments."""

    def notify_user(self, user_id: int, message: str) -> None:
        """Send a notification to the user."""
        self._send_email(user_id, message)

    def send_invoice(self, user_id: int, amount: float) -> None:
        """Send invoice email and charge the user."""
        payment_svc = PaymentService()
        # NOTE: uses the current charge_user(amount) signature
        success = payment_svc.charge_user(amount)
        if success:
            self._send_email(user_id, f"Invoice for ${amount:.2f} — payment successful")
        else:
            self._send_email(user_id, f"Invoice for ${amount:.2f} — payment FAILED")

    def send_subscription_renewal(self, user_id: int, plan_amount: float) -> None:
        """Charge user for monthly subscription renewal."""
        svc = PaymentService()
        charged = svc.charge_user(plan_amount)
        if charged:
            self._log_renewal(user_id, plan_amount)

    def _send_email(self, user_id: int, body: str) -> None:
        """Internal email sender."""
        pass

    def _log_renewal(self, user_id: int, amount: float) -> None:
        pass
EOF

git add .
git commit -m "feat: add invoice notification and subscription renewal"
git push origin feature/invoice-notifications

MR_B_IID=$(glab mr create \
  --title "feat: add invoice notification and subscription renewal" \
  --description "Adds send_invoice and send_subscription_renewal methods that call PaymentService.charge_user." \
  --source-branch feature/invoice-notifications \
  --target-branch main \
  --yes \
  --print-url | grep -oE '[0-9]+$')
ok "MR-B created: $NOTIF_REPO!$MR_B_IID"
popd

# ── Wait for Orbit indexing ───────────────────────────────────────────────────
info "Waiting 120 seconds for Orbit to index the default branches..."
sleep 120
ok "Orbit indexing window passed"

# ── Trigger MergeGuard ───────────────────────────────────────────────────────
PAYMENTS_PROJECT_ID=$(glab api "groups/$GITLAB_GROUP/projects" --method GET \
  | python3 -c "import sys,json; ps=json.load(sys.stdin); print(next(p['id'] for p in ps if 'payments-service' in p['path']))" 2>/dev/null || echo "0")

NOTIF_PROJECT_ID=$(glab api "groups/$GITLAB_GROUP/projects" --method GET \
  | python3 -c "import sys,json; ps=json.load(sys.stdin); print(next(p['id'] for p in ps if 'notification-service' in p['path']))" 2>/dev/null || echo "0")

if [[ "$PAYMENTS_PROJECT_ID" -gt 0 && "$NOTIF_PROJECT_ID" -gt 0 ]]; then
  info "Triggering MergeGuard for MR-A ($PAYMENTS_PROJECT_ID!$MR_A_IID)..."
  curl -s -X POST "$MERGEGUARD_URL/webhook/mr" \
    -H "Content-Type: application/json" \
    -H "X-Gitlab-Token: $WEBHOOK_SECRET" \
    -d "{
      \"object_kind\": \"merge_request\",
      \"object_attributes\": {
        \"id\": 1001,
        \"iid\": $MR_A_IID,
        \"action\": \"open\",
        \"url\": \"$GITLAB_URL/$PAYMENTS_REPO/-/merge_requests/$MR_A_IID\",
        \"title\": \"feat: add currency support to charge_user\",
        \"source_branch\": \"feature/add-currency-support\"
      },
      \"project\": {
        \"id\": $PAYMENTS_PROJECT_ID,
        \"path_with_namespace\": \"$PAYMENTS_REPO\"
      },
      \"user\": {\"username\": \"demo-user\"}
    }"

  sleep 5

  info "Triggering MergeGuard for MR-B ($NOTIF_PROJECT_ID!$MR_B_IID)..."
  curl -s -X POST "$MERGEGUARD_URL/webhook/mr" \
    -H "Content-Type: application/json" \
    -H "X-Gitlab-Token: $WEBHOOK_SECRET" \
    -d "{
      \"object_kind\": \"merge_request\",
      \"object_attributes\": {
        \"id\": 1002,
        \"iid\": $MR_B_IID,
        \"action\": \"open\",
        \"url\": \"$GITLAB_URL/$NOTIF_REPO/-/merge_requests/$MR_B_IID\",
        \"title\": \"feat: add invoice notification and subscription renewal\",
        \"source_branch\": \"feature/invoice-notifications\"
      },
      \"project\": {
        \"id\": $NOTIF_PROJECT_ID,
        \"path_with_namespace\": \"$NOTIF_REPO\"
      },
      \"user\": {\"username\": \"demo-user\"}
    }"
fi

echo ""
echo "═══════════════════════════════════════════════════"
echo "  Demo setup complete!"
echo "  MR-A: $GITLAB_URL/$PAYMENTS_REPO/-/merge_requests/$MR_A_IID"
echo "  MR-B: $GITLAB_URL/$NOTIF_REPO/-/merge_requests/$MR_B_IID"
echo "  Collision Map: $MERGEGUARD_URL"
echo "═══════════════════════════════════════════════════"
