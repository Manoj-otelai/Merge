"""Notification service — invoice and subscription-renewal emails.

These functions call payments-service's charge_user(). When MR !23 adds a
required `currency` parameter, these call sites are MergeGuard's auto-fix target.
"""
from payments.charge import charge_user


def send_invoice(user_id: int, amount: float) -> None:
    """Charge the user and email them an invoice."""
    ok = charge_user(amount)
    if ok:
        _email(user_id, f"Invoice for {amount}")


def send_subscription_renewal(user_id: int, amount: float) -> None:
    """Charge the user for a subscription renewal and notify them."""
    if charge_user(amount):
        _email(user_id, "Your subscription has been renewed")


def _email(user_id: int, body: str) -> None:
    print(f"-> email[{user_id}]: {body}")
