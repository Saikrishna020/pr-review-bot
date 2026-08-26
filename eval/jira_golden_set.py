"""Hand-authored (diff, ticket, expected verdict) cases for Jira alignment.

Ground truth was decided BEFORE any diff was written or the bot was run —
same discipline as the Phase 2 context golden set. Each diff is written
specifically to produce its expected verdict against a real ticket in the
SCRUM sandbox project.

Diffs are hand-written rather than pulled from real PRs on purpose: it keeps
the eval runnable without maintaining six live branches, and it isolates the
Jira-alignment judgement from Phase 2's context pipeline (every case here
passes context=None). The truncation interaction — a real diff large enough
to trip Phase 2's caps, which must not yield a confident "satisfies" — is
therefore NOT covered here; it needs a real PR and is tracked as the next
gap to close.

`acceptable_verdicts` allows more than one answer where the ticket genuinely
supports it (e.g. a diff that misses a requirement could defensibly be
"partial" or "does_not_satisfy"). `satisfies` is never in that list for a
case where the diff is incomplete — that's the whole point.
"""

# --- Case 1: clear ticket, diff fully implements it -----------------------

DIFF_VALIDATES_PR_NUMBER = '''diff --git a/main.py b/main.py
index 1111111..2222222 100644
--- a/main.py
+++ b/main.py
@@ -124,6 +124,12 @@ def review(request: ReviewRequest) -> dict:
     Intended for curl/Postman testing, not for GitHub to call directly.
     Set post=true to also post the review as a PR comment (still subject
     to the POST_COMMENTS env flag and the blocked-target guard).
     """
+    if request.pr_number <= 0:
+        raise HTTPException(
+            status_code=400,
+            detail="pr_number must be a positive integer.",
+        )
+
     try:
         diff = get_pr_diff(request.owner, request.repo, request.pr_number)
         head = get_pr_head(request.owner, request.repo, request.pr_number)
'''

# --- Case 2 / 6: unrelated diffs (wrong ticket linked) --------------------

DIFF_UNRELATED_EMOJI_TWEAK = '''diff --git a/review_real_pr.py b/review_real_pr.py
index 1111111..2222222 100644
--- a/review_real_pr.py
+++ b/review_real_pr.py
@@ -96,7 +96,7 @@ def review_pr(
-SEVERITY_EMOJI = {"blocking": "\\U0001F534", "warning": "\\U0001F7E1", "note": "\\U0001F7E2"}
+SEVERITY_EMOJI = {"blocking": "\\u26D4", "warning": "\\u26A0", "note": "\\u2139"}
'''

DIFF_UNRELATED_README_WORDING = '''diff --git a/README.md b/README.md
index 1111111..2222222 100644
--- a/README.md
+++ b/README.md
@@ -1,6 +1,6 @@
 # PR Review Bot

-An automated code review bot that watches GitHub pull requests, builds deterministic repo context for the diff
+An automated code review bot that monitors GitHub pull requests, assembles deterministic repository context for the diff
 (imports, callers, class hierarchy - parsed, not embedded), sends both to an LLM (DeepSeek) for review, and posts
 the findings back as a PR comment - no manual steps once wired up.
'''

# --- Case 3: three sub-points, diff implements two of them ----------------

DIFF_WEBHOOK_TWO_OF_THREE = '''diff --git a/main.py b/main.py
index 1111111..2222222 100644
--- a/main.py
+++ b/main.py
@@ -188,6 +188,7 @@ async def webhook(request: Request, background_tasks: BackgroundTasks) -> dict:
     raw_body = await request.body()
     _verify_signature(raw_body, request.headers.get("X-Hub-Signature-256"))

+    log.info("Webhook delivery %s received", request.headers.get("X-GitHub-Delivery"))
     event = request.headers.get("X-GitHub-Event")
     if event != "pull_request":
         return {"status": "ignored", "reason": f"unhandled event type: {event}"}
@@ -200,6 +201,11 @@ async def webhook(request: Request, background_tasks: BackgroundTasks) -> dict:
     owner = payload["repository"]["owner"]["login"]
     repo = payload["repository"]["name"]
     pull_request = payload["pull_request"]
+    if "number" not in pull_request:
+        raise HTTPException(
+            status_code=400,
+            detail="pull_request payload is missing 'number'.",
+        )
     pr_number = pull_request["number"]
'''

# --- Case 4: false-"satisfies" stress test --------------------------------
# Implements both bulleted acceptance criteria exactly, and nothing about the
# 1000-result-cap requirement buried in the ticket's prose.

DIFF_SEARCH_HANDLES_ERRORS_ONLY = '''diff --git a/fetch_real_pr_diff.py b/fetch_real_pr_diff.py
index 1111111..2222222 100644
--- a/fetch_real_pr_diff.py
+++ b/fetch_real_pr_diff.py
@@ -118,8 +118,12 @@ def search_code(owner: str, repo: str, identifier: str) -> list[str]:
     url = f"{GITHUB_API}/search/code"
     params = {"q": f"{identifier} repo:{owner}/{repo} extension:py"}

-    response = httpx.get(url, headers=github_headers(), params=params, timeout=30)
-    if response.status_code in (403, 422):
-        return []
+    try:
+        response = httpx.get(url, headers=github_headers(), params=params, timeout=30)
+    except httpx.HTTPError:
+        return []
+
+    if response.status_code in (403, 422):
+        return []
+
     response.raise_for_status()
     return [item["path"] for item in response.json().get("items", [])]
'''

# --- Case 5: vague ticket, plausible diff ---------------------------------

DIFF_RAISE_MAX_WORKERS = '''diff --git a/pr_context.py b/pr_context.py
index 1111111..2222222 100644
--- a/pr_context.py
+++ b/pr_context.py
@@ -39,7 +39,7 @@
 DEFAULT_BUDGET_CHARS = 12_000
 MAX_CHANGED_SYMBOLS = 5    # how many changed functions/classes we search callers for
 MAX_SEARCH_CANDIDATES = 8  # how many search hits we fetch+confirm per symbol
-MAX_WORKERS = 16           # concurrent GitHub API calls
+MAX_WORKERS = 32           # concurrent GitHub API calls
'''


CASES = [
    {
        "id": "scrum1-fully-implemented",
        "ticket_key": "SCRUM-1",
        "diff": DIFF_VALIDATES_PR_NUMBER,
        "expected_verdict": "satisfies",
        "acceptable_verdicts": ["satisfies"],
        "rationale": (
            "All four acceptance criteria are met by the diff: 400 on non-positive, a body "
            "naming the constraint, the guard sits before any GitHub call, and positive "
            "values are untouched. The baseline 'can it recognise a genuinely complete "
            "implementation' case — if this drifts to partial, the skepticism bias is too strong."
        ),
    },
    {
        "id": "scrum2-unrelated-diff",
        "ticket_key": "SCRUM-2",
        "diff": DIFF_UNRELATED_EMOJI_TWEAK,
        "expected_verdict": "does_not_satisfy",
        "acceptable_verdicts": ["does_not_satisfy"],
        "rationale": (
            "Ticket asks for a new /debug/rate-limit endpoint; the diff only swaps severity "
            "emoji. Nothing in the diff touches routing, GitHub quota, or JSON output."
        ),
    },
    {
        "id": "scrum3-two-of-three",
        "ticket_key": "SCRUM-3",
        "diff": DIFF_WEBHOOK_TWO_OF_THREE,
        "expected_verdict": "partial",
        "acceptable_verdicts": ["partial"],
        "rationale": (
            "Implements AC 1 (400 on missing pull_request.number) and AC 2 (log "
            "X-GitHub-Delivery), but leaves the unhandled-action path returning 200 "
            "{'status': 'ignored'} — AC 3 untouched. Two of three is the textbook 'partial'; "
            "grading this 'satisfies' would be a false satisfies, grading it "
            "'does_not_satisfy' understates real progress."
        ),
    },
    {
        "id": "scrum4-buried-edge-case",
        "ticket_key": "SCRUM-4",
        "diff": DIFF_SEARCH_HANDLES_ERRORS_ONLY,
        "expected_verdict": "partial",
        # Either non-satisfies answer is defensible; "satisfies" is the failure.
        "acceptable_verdicts": ["partial", "does_not_satisfy"],
        "false_satisfies_stress_test": True,
        "rationale": (
            "THE KEY CASE. The diff implements both bulleted acceptance criteria exactly "
            "(422 -> [], 403 -> []) and nothing else. The requirement that callers be able "
            "to distinguish 'search hit its 1000-result cap' from 'target does not exist' "
            "appears only in the ticket's prose, not the bullets. A reviewer that reads only "
            "the AC list marks this 'satisfies' and tells a human the ticket is done when a "
            "real requirement is unimplemented — the exact failure the prompt's 'read the "
            "whole description, not just the bullets' instruction targets."
        ),
    },
    {
        "id": "scrum5-vague-ticket",
        "ticket_key": "SCRUM-5",
        "diff": DIFF_RAISE_MAX_WORKERS,
        "expected_verdict": "partial",
        "acceptable_verdicts": ["partial"],
        "rationale": (
            "Title-only ticket ('Make the bot faster', no description). The diff raises "
            "MAX_WORKERS, which is plausibly a speed change, so it isn't unrelated — but "
            "with no acceptance criteria there is nothing against which completeness could "
            "be verified, so 'satisfies' would be unfalsifiable rather than earned. Policy "
            "decision recorded in review_prompt._vagueness_clause: map to 'partial' and say "
            "the ticket is underspecified. Note the paired check in the runner that the "
            "reasoning actually mentions the underspecification."
        ),
    },
    {
        "id": "scrum6-wrong-ticket-linked",
        "ticket_key": "SCRUM-6",
        "diff": DIFF_UNRELATED_README_WORDING,
        "expected_verdict": "does_not_satisfy",
        "acceptable_verdicts": ["does_not_satisfy"],
        "expect_mismatch_flagged": True,
        "rationale": (
            "Ticket asks for retry-with-backoff on 5xx; the diff is a README wording change. "
            "Beyond the verdict, the bot should say plainly that the diff looks unrelated to "
            "the ticket — that phrasing is what tells a human 'you linked the wrong ticket' "
            "rather than 'your implementation is incomplete'."
        ),
    },
]
