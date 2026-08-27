import os
from dataclasses import dataclass

import httpx
from dotenv import load_dotenv

from github_app import get_installation_token

load_dotenv()

GITHUB_API = "https://api.github.com"

SOURCE_OWNER = "pallets"
SOURCE_REPO = "click"
PR_STATE = "all"


# Env var names that may hold the GitHub token. Linux (and therefore Render) treats
# env var names as case-sensitive, unlike Windows, so a token saved as `Github_token`
# is invisible to a plain os.environ["GITHUB_TOKEN"] lookup there.
GITHUB_TOKEN_NAMES = ("GITHUB_TOKEN", "GH_TOKEN", "GITHUB_PAT")


def get_github_token() -> str | None:
    """Resolve the GitHub token from env, tolerating any capitalization."""
    for name, value in os.environ.items():
        if name.upper() in GITHUB_TOKEN_NAMES and value.strip():
            return value.strip()
    return None


def resolve_auth_token(prefer_app: bool = True) -> tuple[str | None, str]:
    """The token to authenticate with, and which identity it represents.

    By default, prefers a GitHub App installation token when the App is
    configured, so comments are authored by `app-slug[bot]` rather than by
    whoever owns the personal access token. Falls back to the PAT, then to
    unauthenticated (which still works for public repos, at 60 requests/hour).

    Pass `prefer_app=False` to force the PAT even when the App is available —
    for anything meant to represent a human rather than the reviewer itself
    (see `post_pr_comment`'s `as_bot` parameter). Using the App identity there
    would make a human's reply indistinguishable from the bot talking to
    itself, which defeats the entire point of a distinct bot identity.
    """
    if prefer_app:
        installation_token = get_installation_token()
        if installation_token:
            return installation_token, "github-app"

    pat = get_github_token()
    if pat:
        return pat, "personal-access-token"

    return None, "unauthenticated"


def github_headers(accept: str = "application/vnd.github+json", prefer_app: bool = True) -> dict[str, str]:
    headers = {
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
    }

    token, _identity = resolve_auth_token(prefer_app=prefer_app)
    if token:
        headers["Authorization"] = f"Bearer {token}"

    return headers


def list_closed_prs(owner: str, repo: str, limit: int = 5) -> list[dict]:
    url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls"
    params = {
        "state": PR_STATE,
        "sort": "updated",
        "direction": "desc",
        "per_page": limit,
    }

    response = httpx.get(
        url,
        headers=github_headers(),
        params=params,
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def get_pr_diff(owner: str, repo: str, pr_number: int) -> str:
    url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}"

    response = httpx.get(
        url,
        headers=github_headers(accept="application/vnd.github.v3.diff"),
        timeout=30,
    )
    response.raise_for_status()
    return response.text


@dataclass
class PRHead:
    """The bits of a PR needed to review it: which commit to read files at,
    and where a Jira ticket key might be written (branch name or title).
    """

    sha: str
    ref: str | None  # head branch name, e.g. "SCRUM-1-add-validation"
    title: str | None


def get_pr_head(owner: str, repo: str, pr_number: int) -> PRHead:
    url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}"

    response = httpx.get(url, headers=github_headers(), timeout=30)
    response.raise_for_status()
    data = response.json()
    return PRHead(
        sha=data["head"]["sha"],
        ref=data.get("head", {}).get("ref"),
        title=data.get("title"),
    )


def get_file_content(owner: str, repo: str, path: str, ref: str | None = None) -> str | None:
    """Fetches one file's raw text content at `ref` (a sha or branch name), or the
    default branch if `ref` is None. Returns None if the file doesn't exist at
    that ref (e.g. an import that resolves to a guessed path that isn't real).
    """
    url = f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}"
    params = {"ref": ref} if ref else {}

    response = httpx.get(
        url,
        headers=github_headers(accept="application/vnd.github.v3.raw"),
        params=params,
        timeout=30,
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.text


def search_code(owner: str, repo: str, identifier: str) -> tuple[list[str], bool]:
    """Finds files in `owner/repo` whose text mentions `identifier`, via GitHub's
    code search. Returns (paths, degraded).

    Only indexes the default branch and is rate-limited (~10 requests/minute
    authenticated) — on a rate limit or a rejected query, this degrades to "no
    candidates found" rather than raising, since caller/subclass context is a
    nice-to-have, not something a review should fail over.

    `degraded` is True when the empty list means "the search failed" rather
    than "there are genuinely no matches". Callers must not report an empty
    degraded result as a confirmed absence — that's indistinguishable from
    "no callers exist" to a reader, which is exactly the wrong thing to tell
    a reviewer.

    Scoped to `extension:py` since parsing is Python-only right now — without
    it, a common identifier gets crowded out by docs/README hits before any
    real code result shows up within the small per-symbol result cap
    (`pr_context.MAX_SEARCH_CANDIDATES`). Generalize this qualifier if that
    ever changes.
    """
    url = f"{GITHUB_API}/search/code"
    params = {"q": f"{identifier} repo:{owner}/{repo} extension:py"}

    response = httpx.get(url, headers=github_headers(), params=params, timeout=30)
    if response.status_code in (403, 422):
        return [], True
    response.raise_for_status()
    return [item["path"] for item in response.json().get("items", [])], False


def post_pr_comment(owner: str, repo: str, pr_number: int, body: str, as_bot: bool = True) -> dict:
    """Post a single issue-style comment on a PR (PRs are issues in the GitHub API).

    `as_bot=True` (the default) is for the review itself — it uses the GitHub
    App identity when one is configured, so the comment is authored by
    `<app-slug>[bot]` rather than a person. Pass `as_bot=False` for anything
    meant to represent a human replying to that review (e.g. a "developer
    response" comment); this forces the personal access token even when the
    App is available, so a human reply is never mistaken for the bot
    commenting on its own review.
    """
    token, _identity = resolve_auth_token(prefer_app=as_bot)
    if token is None:
        raise RuntimeError(
            "No usable GitHub credentials — set GITHUB_TOKEN"
            + (", or configure the GitHub App," if as_bot else "")
            + " before posting comments."
        )

    url = f"{GITHUB_API}/repos/{owner}/{repo}/issues/{pr_number}/comments"

    response = httpx.post(
        url,
        headers=github_headers(prefer_app=as_bot),
        json={"body": body},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def main() -> None:
    print(f"Fetching recent {PR_STATE} PRs from {SOURCE_OWNER}/{SOURCE_REPO}...")
    prs = list_closed_prs(SOURCE_OWNER, SOURCE_REPO)

    if not prs:
        print("No pull requests found in this repo yet.")
        print("Create a small PR first, or point SOURCE_OWNER/SOURCE_REPO at a repo with existing PRs.")
        return

    for index, pr in enumerate(prs):
        print(f"[{index}] #{pr['number']}: {pr['title']} ({pr['state']})")

    chosen_pr = prs[0]
    print()
    print(f"Fetching diff for PR #{chosen_pr['number']}: {chosen_pr['title']}")
    print(chosen_pr["html_url"])
    print()

    diff = get_pr_diff(SOURCE_OWNER, SOURCE_REPO, chosen_pr["number"])

    print(f"Diff length: {len(diff)} characters")
    print()
    print(diff[:3000])

    if len(diff) > 3000:
        print()
        print("[diff preview truncated]")


if __name__ == "__main__":
    main()
