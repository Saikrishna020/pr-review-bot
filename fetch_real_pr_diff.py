import os

import httpx
from dotenv import load_dotenv

load_dotenv()

GITHUB_API = "https://api.github.com"

SOURCE_OWNER = "pallets"
SOURCE_REPO = "click"
PR_STATE = "all"


def github_headers(accept: str = "application/vnd.github+json") -> dict[str, str]:
    headers = {
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
    }

    token = os.environ.get("GITHUB_TOKEN")
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


def post_pr_comment(owner: str, repo: str, pr_number: int, body: str) -> dict:
    """Post a single issue-style comment on a PR (PRs are issues in the GitHub API).

    Requires GITHUB_TOKEN to be set with `repo` (or fine-grained `pull_requests: write`) scope.
    """
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
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
