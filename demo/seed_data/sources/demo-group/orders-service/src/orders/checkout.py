"""Orders service — checkout flow."""
from payments.charge import charge_user


def handle_checkout(cart_total: float) -> str:
    """Charge the cart total and return an order status."""
    if charge_user(cart_total):
        return "confirmed"
    return "payment_failed"
