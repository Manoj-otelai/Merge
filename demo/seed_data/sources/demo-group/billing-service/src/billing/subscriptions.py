"""Billing service — subscription lifecycle."""
from payments.charge import charge_user


def process_subscription(account_id: int, monthly_price: float) -> bool:
    """Bill an account for its monthly subscription."""
    charged = charge_user(monthly_price)
    return charged
