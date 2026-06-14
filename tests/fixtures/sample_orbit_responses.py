"""Mock Orbit query responses for testing."""

CALLERS_OF_CHARGE_USER = [
    {
        "caller_name": "send_invoice",
        "caller_file": "src/notifications/invoice.py",
        "caller_project_id": 102,
        "caller_project_path": "demo-group/notification-service",
        "owner": "maria",
        "line_number": 8,
    },
    {
        "caller_name": "process_subscription",
        "caller_file": "src/billing/subscriptions.py",
        "caller_project_id": 103,
        "caller_project_path": "demo-group/billing-service",
        "owner": "sven",
        "line_number": 42,
    },
    {
        "caller_name": "handle_checkout",
        "caller_file": "src/orders/checkout.py",
        "caller_project_id": 104,
        "caller_project_path": "demo-group/orders-service",
        "owner": "maria",
        "line_number": 17,
    },
    {
        "caller_name": "refund_payment",
        "caller_file": "src/billing/refunds.py",
        "caller_project_id": 103,
        "caller_project_path": "demo-group/billing-service",
        "owner": "sven",
        "line_number": 65,
    },
]

CENTRALITY_CHARGE_USER = [{"caller_count": 12}]

OWNERS_CHARGE_USER = [
    {"username": "maria", "email": "maria@example.com"},
    {"username": "sven", "email": "sven@example.com"},
]

CALLERS_OF_AUTHENTICATE = [
    {
        "caller_name": "login_handler",
        "caller_file": "src/auth/login.py",
        "caller_project_id": 107,
        "caller_project_path": "demo-group/auth-service",
        "owner": "carol",
        "line_number": 23,
    },
    {
        "caller_name": "api_middleware",
        "caller_file": "src/api/middleware.py",
        "caller_project_id": 108,
        "caller_project_path": "demo-group/api-gateway",
        "owner": "dave",
        "line_number": 11,
    },
]

EMPTY_CALLERS: list = []
ZERO_CENTRALITY = [{"caller_count": 0}]
