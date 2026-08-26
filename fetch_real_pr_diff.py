import os
from dataclasses import dataclass

import httpx
from dotenv import load_dotenv

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


def github_headers(accept: str = "application/vnd.github+json") -> dict[str, str]:
    headers = {
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
    }

    token = get_github_token()
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


def search_code(owner: str, repo: str, identifier: str) -> list[str]:
    """Finds files in `owner/repo` whose text mentions `identifier`, via GitHub's
    code search. Only indexes the default branch and is rate-limited (~10
    requests/minute authenticated) — on a rate limit or a rejected query, this
    degrades to "no candidates found" rather than raising, since caller/subclass
    context is a nice-to-have, not something a review should fail over.

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
        return []
    response.raise_for_status()
    return [item["path"] for item in response.json().get("items", [])]


def post_pr_comment(owner: str, repo: str, pr_number: int, body: str) -> dict:
    """Post a single issue-style comment on a PR (PRs are issues in the GitHub API).

    Requires GITHUB_TOKEN to be set with `repo` (or fine-grained `pull_requests: write`) scope.
    """
    if not get_github_token():
        raise RuntimeError("Set GITHUB_TOKEN in .env before posting comments.")

    url = f"{GITHUB_API}/repos/{owner}/{repo}/issues/{pr_number}/comments"

    response = httpx.post(
        url,
        headers=github_headers(),
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
