import hashlib
import hmac
import os

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from pydantic import BaseModel

from fetch_real_pr_diff import get_pr_diff
from review_real_pr import maybe_post_review, review_diff

load_dotenv()

WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET")

# GitHub events that mean "there's new/changed code to review" for a PR.
REVIEWABLE_ACTIONS = {"opened", "synchronize", "reopened"}

app = FastAPI(title="PR Review Bot")


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
    """Temporary: confirm the deployed process actually sees GITHUB_TOKEN and that
    it authenticates, without ever exposing the full token value. Remove once the
    Render env var mystery is solved."""
    import httpx

    from fetch_real_pr_diff import github_headers

    raw = os.environ.get("GITHUB_TOKEN")
    r = httpx.get("https://api.github.com/rate_limit", headers=github_headers(), timeout=15)
    core = r.json().get("resources", {}).get("core", {})
    return {
        "token_env_var_set": bool(raw),
        "token_length": len(raw) if raw else 0,
        "token_prefix": raw[:7] if raw else None,
        "rate_limit_seen_by_app": core,
    }


@app.post("/review")
def review(request: ReviewRequest) -> dict:
    """Manual trigger: review one PR on demand and return the JSON result.

    Intended for curl/Postman testing, not for GitHub to call directly.
    Set post=true to also post the review as a PR comment (still subject
    to the POST_COMMENTS env flag and the blocked-target guard).
    """
    diff = get_pr_diff(request.owner, request.repo, request.pr_number)
    result = review_diff(diff)

    if request.post:
        maybe_post_review(request.owner, request.repo, request.pr_number, result)

    return result


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


def _run_review_and_post(owner: str, repo: str, pr_number: int) -> None:
    diff = get_pr_diff(owner, repo, pr_number)
    result = review_diff(diff)
    maybe_post_review(owner, repo, pr_number, result)


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
    pr_number = payload["pull_request"]["number"]

    # Respond to GitHub immediately; the diff fetch + LLM review can take
    # longer than GitHub's webhook timeout, so do the real work after.
    background_tasks.add_task(_run_review_and_post, owner, repo, pr_number)

    return {"status": "accepted", "owner": owner, "repo": repo, "pr_number": pr_number}
