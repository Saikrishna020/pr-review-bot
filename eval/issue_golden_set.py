"""Hand-authored (diff, expected outcome) cases for general issue detection —
the "does the bot find real bugs without crying wolf" axis, separate from
Jira alignment (see jira_golden_set.py).

Same discipline as the Jira golden set: expected outcome decided BEFORE the
bot was run. Every case passes context=None and ticket=None, isolating pure
code-review judgement from Phase 2's context pipeline and Phase 3's ticket
alignment — those are covered by their own evals.

No dedicated "security" cases here on purpose: Phase 2's context is
import/caller-level, not a taint-tracking graph, so claiming reliable
vulnerability *detection* would be unfounded. scrum-secret below is a
pattern-level case (hardcoded credential) that a competent reviewer would
flag on sight, not a claim about scanning for injection classes generally.
"""

# --- Case 1: clean rename, no behavior change (false-positive check) ------

DIFF_CLEAN_RENAME = '''diff --git a/pricing.py b/pricing.py
index 1111111..2222222 100644
--- a/pricing.py
+++ b/pricing.py
@@ -10,9 +10,9 @@ def apply_discount(order_total: float, pct: float) -> float:
     return order_total - (order_total * pct / 100)


-def calc(items):
-    t = sum(i.price for i in items)
-    return t
+def calculate_subtotal(items):
+    subtotal = sum(item.price for item in items)
+    return subtotal
'''

# --- Case 2: off-by-one, medium severity -----------------------------------

DIFF_OFF_BY_ONE = '''diff --git a/report.py b/report.py
index 1111111..2222222 100644
--- a/report.py
+++ b/report.py
@@ -14,7 +14,10 @@ def summarize(entries: list[dict]) -> list[dict]:
     """Returns every entry except the running total row appended by the
     upstream exporter, which always lands at index 0."""
-    return entries
+    trimmed = []
+    for i in range(len(entries) - 1):
+        trimmed.append(entries[i])
+    return trimmed
'''

# --- Case 3: missing null check, blocking severity --------------------------

DIFF_NULL_CRASH = '''diff --git a/notify.py b/notify.py
index 1111111..2222222 100644
--- a/notify.py
+++ b/notify.py
@@ -22,7 +22,7 @@ def send_welcome_email(user: User) -> None:
     """user.email is optional at signup (verified later), so callers must
     handle the case where it hasn't been set yet."""
-    if user.email:
+    if user.email.lower().endswith("@company.com"):
         mailer.send(user.email, template="welcome")
'''

# --- Case 4: style-only nit, must not be flagged as blocking ---------------

DIFF_STYLE_NIT = '''diff --git a/config.py b/config.py
index 1111111..2222222 100644
--- a/config.py
+++ b/config.py
@@ -3,7 +3,7 @@
 DEFAULT_TIMEOUT = 30
 MAX_RETRIES = 3

-def get_timeout( ):
+def get_timeout():
     return DEFAULT_TIMEOUT
-
+
 def get_retries():
     return MAX_RETRIES
'''

# --- Case 5: complex-but-correct refactor (false-positive stress test) -----

DIFF_CORRECT_REFACTOR = '''diff --git a/orders.py b/orders.py
index 1111111..2222222 100644
--- a/orders.py
+++ b/orders.py
@@ -8,20 +8,20 @@ class OrderService:
     def cancel_order(self, order_id: int) -> Order:
-        order = self.db.get(order_id)
-        if order is None:
-            raise OrderNotFound(order_id)
-        if order.status == "shipped":
-            raise CannotCancel(order_id, "already shipped")
-        order.status = "cancelled"
-        self.db.save(order)
-        self.events.publish("order.cancelled", order_id)
+        order = self._require_order(order_id)
+        self._require_status_not(order, "shipped", "already shipped")
+        order.status = "cancelled"
+        self.db.save(order)
+        self.events.publish("order.cancelled", order_id)
         return order

     def refund_order(self, order_id: int) -> Order:
-        order = self.db.get(order_id)
-        if order is None:
-            raise OrderNotFound(order_id)
-        if order.status != "cancelled":
-            raise CannotRefund(order_id, "not cancelled")
-        order.status = "refunded"
-        self.db.save(order)
-        self.events.publish("order.refunded", order_id)
+        order = self._require_order(order_id)
+        self._require_status_is(order, "cancelled", "not cancelled")
+        order.status = "refunded"
+        self.db.save(order)
+        self.events.publish("order.refunded", order_id)
         return order
+
+    def _require_order(self, order_id: int) -> Order:
+        order = self.db.get(order_id)
+        if order is None:
+            raise OrderNotFound(order_id)
+        return order
+
+    def _require_status_not(self, order: Order, status: str, reason: str) -> None:
+        if order.status == status:
+            raise CannotCancel(order.id, reason)
+
+    def _require_status_is(self, order: Order, status: str, reason: str) -> None:
+        if order.status != status:
+            raise CannotRefund(order.id, reason)
'''

# --- Case 6: hardcoded secret, blocking severity ----------------------------

DIFF_HARDCODED_SECRET = '''diff --git a/payments.py b/payments.py
index 1111111..2222222 100644
--- a/payments.py
+++ b/payments.py
@@ -1,6 +1,9 @@
 import httpx

+STRIPE_API_KEY = "REPLACE_WITH_FAKE_STRIPE_KEY_FOR_TESTING_00000000"
+
 def charge(customer_id: str, amount_cents: int) -> dict:
     response = httpx.post(
         "https://api.stripe.com/v1/charges",
-        auth=(get_secret("stripe_api_key"), ""),
+        auth=(STRIPE_API_KEY, ""),
         data={"customer": customer_id, "amount": amount_cents},
     )
     return response.json()
'''


CASES = [
    {
        "id": "clean-rename",
        "diff": DIFF_CLEAN_RENAME,
        "expect_flag": False,
        "false_positive_stress_test": False,
        "rationale": (
            "Pure rename for readability, identical behavior. Baseline false-positive check — "
            "if the bot flags this, it's finding style preferences, not bugs."
        ),
    },
    {
        "id": "off-by-one",
        "diff": DIFF_OFF_BY_ONE,
        "expect_flag": True,
        "expect_severity_in": {"blocking", "warning"},
        "expect_keywords": ("last", "off by one", "off-by-one", "index", "excludes", "drops", "final entry"),
        "false_positive_stress_test": False,
        "rationale": (
            "The comment says drop index 0 (the totals row); the diff instead drops the LAST "
            "entry via range(len(entries) - 1) and keeps index 0. Real bug, medium-to-high severity."
        ),
    },
    {
        "id": "null-crash",
        "diff": DIFF_NULL_CRASH,
        "expect_flag": True,
        "expect_severity_in": {"blocking"},
        "expect_keywords": ("none", "null", "optional", "attributeerror", "crash", "not set"),
        "false_positive_stress_test": False,
        "rationale": (
            "Docstring explicitly says user.email is optional and callers must handle it unset; "
            "the diff calls .lower() on it directly. Crashes with AttributeError on any unverified "
            "signup. This is the case a reviewer must catch by reading the comment, not just the diff."
        ),
    },
    {
        "id": "style-nit",
        "diff": DIFF_STYLE_NIT,
        "expect_flag": True,
        "expect_severity_in": {"note"},
        "expect_keywords": None,
        "false_positive_stress_test": False,
        "rationale": (
            "Whitespace-only cleanup (extra space in a parameter list, a stray blank line). Worth "
            "a note at most; grading this 'blocking' or 'warning' is the failure this case targets."
        ),
    },
    {
        "id": "correct-refactor",
        "diff": DIFF_CORRECT_REFACTOR,
        "expect_flag": False,
        "false_positive_stress_test": True,
        "rationale": (
            "THE KEY CASE. Extracts two duplicated get-or-404 / status-check blocks into shared "
            "helpers; behavior is identical to before (same exceptions, same reasons, same order "
            "of operations) — it just looks larger and more different than it is. If the bot flags "
            "issues here, it's pattern-matching on diff size/shape rather than actually reasoning "
            "about equivalence."
        ),
    },
    {
        "id": "hardcoded-secret",
        "diff": DIFF_HARDCODED_SECRET,
        "expect_flag": True,
        "expect_severity_in": {"blocking"},
        "expect_keywords": ("hardcod", "secret", "credential", "api key", "key in", "committed"),
        "false_positive_stress_test": False,
        "rationale": (
            "Replaces a secret-store lookup (get_secret(...)) with a literal key committed to "
            "source. Pattern-level, not a claim about vulnerability scanning generally — any "
            "competent reviewer flags a literal key in a diff on sight."
        ),
    },
]
