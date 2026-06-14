"""Sample unified diffs for testing symbol extraction."""

# MR-A: payments-service changes charge_user signature
PYTHON_SIGNATURE_CHANGE = """\
--- a/src/payments/charge.py
+++ b/src/payments/charge.py
@@ -1,15 +1,17 @@
 from decimal import Decimal
-from typing import Optional
+from typing import Optional
+from .currencies import Currency

-def charge_user(amount: float) -> bool:
+def charge_user(amount: float, currency: str = "USD") -> bool:
     \"\"\"Charge the current user's account.\"\"\"
-    return _process_payment(amount)
+    return _process_payment(amount, currency)

 def get_user_balance(user_id: int) -> Decimal:
     balance = _fetch_balance(user_id)
     return balance

-def _process_payment(amount: float) -> bool:
+def _process_payment(amount: float, currency: str) -> bool:
     # internal
     pass
"""

# MR-B: notification-service adds a caller of old charge_user signature
PYTHON_NEW_CALLER = """\
--- a/src/notifications/invoice.py
+++ b/src/notifications/invoice.py
@@ -1,10 +1,17 @@
 from ..payments.charge import charge_user

+def send_invoice(user_id: int, amount: float) -> None:
+    \"\"\"Send invoice and charge the user.\"\"\"
+    success = charge_user(amount)
+    if success:
+        _send_email(user_id, amount)
+
 def notify_user(user_id: int, message: str) -> None:
     pass
"""

# Python: function renamed
PYTHON_RENAME = """\
--- a/src/auth/session.py
+++ b/src/auth/session.py
@@ -1,8 +1,8 @@
-def authenticate(username: str, password: str) -> bool:
+def authenticate_user(username: str, password: str, mfa_code: str = "") -> bool:
     \"\"\"Authenticate a user session.\"\"\"
     return _verify_credentials(username, password)

 def logout(session_id: str) -> None:
     pass
"""

# Python: function removed
PYTHON_REMOVED = """\
--- a/src/utils/helpers.py
+++ b/src/utils/helpers.py
@@ -1,10 +1,5 @@
 def format_date(ts: int) -> str:
     return str(ts)

-def legacy_format(ts: int) -> str:
-    \"\"\"Deprecated helper.\"\"\"
-    return format_date(ts)
-
 def parse_timestamp(s: str) -> int:
     return int(s)
"""

# JavaScript: function signature change
JS_SIGNATURE_CHANGE = """\
--- a/src/api/client.js
+++ b/src/api/client.js
@@ -1,10 +1,11 @@
-async function fetchUser(userId) {
+async function fetchUser(userId, options = {}) {
   const response = await fetch(`/api/users/${userId}`);
   return response.json();
 }

-const sendRequest = (url, data) => {
+const sendRequest = (url, data, headers = {}) => {
   return fetch(url, { method: 'POST', body: JSON.stringify(data) });
 };
"""

# TypeScript: class method change
TS_METHOD_CHANGE = """\
--- a/src/services/UserService.ts
+++ b/src/services/UserService.ts
@@ -1,12 +1,13 @@
 class UserService {
-  async getUser(id: number): Promise<User> {
+  async getUser(id: number, includeProfile: boolean = false): Promise<User> {
     return this.db.findById(id);
   }

   async createUser(data: UserData): Promise<User> {
     return this.db.create(data);
   }
 }
"""

# No-op diff (only body changes, not signatures)
PYTHON_BODY_ONLY = """\
--- a/src/utils/math.py
+++ b/src/utils/math.py
@@ -1,6 +1,6 @@
 def add(a: int, b: int) -> int:
-    return a + b
+    result = a + b
+    return result

 def multiply(a: int, b: int) -> int:
     return a * b
"""

# New file (all additions — should not produce collision-triggering changes)
PYTHON_NEW_FILE = """\
--- /dev/null
+++ b/src/new_module.py
@@ -0,0 +1,5 @@
+def brand_new_function(x: int) -> int:
+    return x * 2
+
+def another_new_func() -> None:
+    pass
"""
