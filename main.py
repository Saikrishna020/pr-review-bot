import hashlib
import hmac
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

import httpx
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from pydantic import BaseModel

from fetch_real_pr_diff import get_github_token, get_pr_diff, get_pr_head, github_headers, resolve_auth_token
from jira_ticket import JiraTicket, resolve_ticket_for_pr
from pr_context import RepoContext, safe_build_context
from review_real_pr import maybe_post_review, review_pr

# See fetch_real_pr_diff.py for why both paths are loaded (Render's Secret
# Files feature mounts at /etc/secrets/, not the working directory).
load_dotenv()
load_dotenv("/etc/secrets/.env", override=False)

log = logging.getLogger("uvicorn.error")

WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET")

# GitHub events that mean "there's new/changed code to review" for a PR.
REVIEWABLE_ACTIONS = {"opened", "synchronize", "reopened"}


def _safe_resolve_ticket(branch_ref: str | None, pr_title: str | None) -> JiraTicket | None:
    """Ticket lookup that can never fail a review. `resolve_ticket_for_pr`
    already returns None for the expected misses (no key, 404, bad auth); this
    catches anything genuinely unexpected on top of that.
    """
    try:
        return resolve_ticket_for_pr(branch_ref, pr_title)
    except Exception:
        log.warning("Jira ticket resolution failed for branch=%r; reviewing without it", branch_ref, exc_info=True)
        return None


def _gather_review_inputs(
    owner: str, repo: str, diff: str, head_sha: str, branch_ref: str | None, pr_title: str | None
) -> tuple[RepoContext | None, JiraTicket | None]:
    """Fetches the Jira ticket while the (much slower) codebase context builds.

    Threads rather than asyncio, matching Phase 2's existing concurrency
    pattern — the whole GitHub/Jira layer is sync httpx.
    """
    with ThreadPoolExecutor(max_workers=2) as pool:
        context_future = pool.submit(safe_build_context, owner, repo, diff, head_sha)
        ticket_future = pool.submit(_safe_resolve_ticket, branch_ref, pr_title)
        return context_future.result(), ticket_future.result()

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Surface config problems in the logs at boot, rather than as a 500 on first use.

    An unauthenticated GitHub client still "works" until it hits the 60/hour shared-IP
    limit, so a missing token has to be stated explicitly to be noticed.
    """
    _token, identity = resolve_auth_token()
    log.info(
        "PR Review Bot starting | commit=%s github_auth=%s deepseek_key=%s "
        "target=%s/%s post_comments=%s webhook_secret=%s",
        os.environ.get("RENDER_GIT_COMMIT", "unknown")[:7],
        {
            "github-app": "GitHub App (comments authored by the app)",
            "personal-access-token": "PAT (comments authored by the token's owner)",
            "unauthenticated": "MISSING (GitHub calls will be rate-limited)",
        }[identity],
        "found" if os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("Deepseek_api") else "MISSING",
        os.environ.get("TARGET_OWNER", "unset"),
        os.environ.get("TARGET_REPO", "unset"),
        os.environ.get("POST_COMMENTS", "false"),
        "set" if WEBHOOK_SECRET else "unset (signature checks skipped)",
    )
    yield


app = FastAPI(title="PR Review Bot", lifespan=lifespan)


def _github_error_detail(exc: httpx.HTTPStatusError) -> str:
    """Turn a raw GitHub HTTP error into something actionable in the response body."""
    status = exc.response.status_code
    if status == 403 and "rate limit" in exc.response.text.lower():
        if not get_github_token():
            return (
                "GitHub rate limit hit and no token was found in the environment. "
                "Set GITHUB_TOKEN and make sure the service actually restarted afterwards."
            )
        return "GitHub rate limit hit even though a token was found; check the token's validity."
    if status in (401, 403):
        return f"GitHub rejected the credentials ({status}). Check GITHUB_TOKEN's value and scopes."
    if status == 404:
        return "PR or repo not found (or the token can't see it)."
    return f"GitHub returned {status}."


class ReviewRequest(BaseModel):
    owner: str
    repo: str
    pr_number: int
    post: bool = False


@app.get("/")
def health() -> dict:
    return {"status": "ok"}


@app.get("/debug/github-auth")
def debug_github_auth() -> dict:
    """Confirm the running process can actually see and use a GitHub token, without
    ever exposing the token value. `commit` identifies which build is live, so a
    stale deploy is obvious rather than being mistaken for a config problem."""
    token = get_github_token()
    response = httpx.get(
        "https://api.github.com/rate_limit", headers=github_headers(), timeout=15
    )
    return {
        "commit": os.environ.get("RENDER_GIT_COMMIT", "unknown")[:7],
        "token_found": bool(token),
        "token_length": len(token) if token else 0,
        "authenticated": response.json().get("resources", {}).get("core", {}).get("limit") != 60,
        "rate_limit": response.json().get("resources", {}).get("core", {}),
    }


@app.get("/debug/rate-limit")
def debug_rate_limit() -> dict:
    """Both quota blocks that actually matter day to day, not just `core`.

    `/debug/github-auth` only ever surfaced `core`, which is a poor proxy for
    whether a review is about to degrade — Phase 2's context building can fire
    dozens of code-search calls per review, and `search` has a far tighter
    budget (~10/min authenticated) than `core` (~5000/hr). This exists so that
    can be checked before a review silently loses caller context, not
    diagnosed after the fact from a "possible" list that's suspiciously empty.
    """
    response = httpx.get("https://api.github.com/rate_limit", headers=github_headers(), timeout=15)
    response.raise_for_status()
    resources = response.json().get("resources", {})
    core = resources.get("core", {})
    search = resources.get("search", {})
    core_remaining_pct = round(core["remaining"] / core["limit"] * 100, 1) if core.get("limit") else None
    return {
        "commit": os.environ.get("RENDER_GIT_COMMIT", "unknown")[:7],
        "core": core,
        "search": search,
        "core_remaining_pct": core_remaining_pct,
    }


@app.post("/review")
def review(request: ReviewRequest) -> dict:
    """Manual trigger: review one PR on demand and return the JSON result.

    Intended for curl/Postman testing, not for GitHub to call directly.
    Set post=true to also post the review as a PR comment (still subject
    to the POST_COMMENTS env flag and the blocked-target guard).
    """
    try:
        diff = get_pr_diff(request.owner, request.repo, request.pr_number)
        head = get_pr_head(request.owner, request.repo, request.pr_number)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail=_github_error_detail(exc)) from exc

    context, ticket = _gather_review_inputs(
        request.owner, request.repo, diff, head.sha, head.ref, head.title
    )
    result = review_pr(diff, context=context, ticket=ticket)

    if request.post:
        maybe_post_review(request.owner, request.repo, request.pr_number, result, ticket)

    return result.model_dump()


def _verify_signature(raw_body: bytes, signature_header: str | None) -> None:
    if not WEBHOOK_SECRET:
        # No secret configured (e.g. early local testing) — skip verification.
        return

    if not signature_header or not signature_header.startswith("sha256="):
        raise HTTPException(status_code=401, detail="Missing or malformed signature.")

    expected = hmac.new(WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    provided = signature_header.removeprefix("sha256=")

    if not hmac.compare_digest(expected, provided):
        raise HTTPException(status_code=401, detail="Invalid webhook signature.")


def _run_review_and_post(
    owner: str, repo: str, pr_number: int, head_sha: str,
    branch_ref: str | None = None, pr_title: str | None = None,
) -> None:
    """Runs after the webhook response has already been sent, so an exception here
    would otherwise vanish silently while GitHub still shows a green delivery."""
    target = f"{owner}/{repo}#{pr_number}"
    try:
        log.info("Reviewing %s (branch=%s)", target, branch_ref)
        diff = get_pr_diff(owner, repo, pr_number)
        context, ticket = _gather_review_inputs(owner, repo, diff, head_sha, branch_ref, pr_title)
        result = review_pr(diff, context=context, ticket=ticket)
        log.info(
            "Review of %s found %d issue(s), jira_verdict=%s (ticket=%s, context_truncated=%s)",
            target, len(result.issues), result.jira_verdict,
            ticket.key if ticket else "none", result.context_truncated,
        )
        maybe_post_review(owner, repo, pr_number, result, ticket)
    except httpx.HTTPStatusError as exc:
        log.error("Review of %s failed: %s", target, _github_error_detail(exc))
    except Exception:
        log.exception("Review of %s failed unexpectedly", target)


@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks) -> dict:
    """Real GitHub webhook target. Configure this URL (via ngrok or your
    deployed host) as the repo's webhook, subscribed to pull_request events.
    """
    raw_body = await request.body()
    _verify_signature(raw_body, request.headers.get("X-Hub-Signature-256"))

    event = request.headers.get("X-GitHub-Event")
    if event != "pull_request":
        return {"status": "ignored", "reason": f"unhandled event type: {event}"}

    payload = await request.json()
    action = payload.get("action")
    if action not in REVIEWABLE_ACTIONS:
        return {"status": "ignored", "reason": f"unhandled action: {action}"}

    owner = payload["repository"]["owner"]["login"]
    repo = payload["repository"]["name"]
    pull_request = payload["pull_request"]
    pr_number = pull_request["number"]
    head_sha = pull_request["head"]["sha"]
    # The webhook payload already carries the branch name and title, so the
    # Jira key costs no extra API call here.
    branch_ref = pull_request.get("head", {}).get("ref")
    pr_title = pull_request.get("title")

    # Respond to GitHub immediately; the diff fetch + LLM review can take
    # longer than GitHub's webhook timeout, so do the real work after.
    background_tasks.add_task(
        _run_review_and_post, owner, repo, pr_number, head_sha, branch_ref, pr_title
    )

    return {"status": "accepted", "owner": owner, "repo": repo, "pr_number": pr_number}
