import json
import os

from dotenv import load_dotenv
from openai import OpenAI

from fetch_real_pr_diff import (
    SOURCE_OWNER,
    SOURCE_REPO,
    get_pr_diff,
    list_closed_prs,
    post_pr_comment,
)

load_dotenv()

ALLOWED_SEVERITIES = {"high", "medium", "low"}

# Where the bot should actually post comments. Defaults to the demo source
# (pallets/click) ONLY for reading diffs; posting there is blocked below no
# matter what, so pointing this at your own repo is required to go live.
TARGET_OWNER = os.environ.get("TARGET_OWNER", SOURCE_OWNER)
TARGET_REPO = os.environ.get("TARGET_REPO", SOURCE_REPO)

# Off by default so running this script never surprises you with a live
# comment. Set POST_COMMENTS=true in .env once you're ready to go end-to-end.
POST_COMMENTS = os.environ.get("POST_COMMENTS", "false").strip().lower() == "true"

# pallets/click is a real external OSS project used only as a free source of
# realistic diffs for local testing. Never post to it, regardless of config.
BLOCKED_TARGETS = {("pallets", "click")}


def get_deepseek_client() -> OpenAI:
    api_key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("Deepseek_api")
    if not api_key:
        raise RuntimeError("Set DEEPSEEK_API_KEY in .env before running this script.")

    return OpenAI(
        api_key=api_key,
        base_url="https://api.deepseek.com",
    )


def validate_review_json(raw_response: str) -> dict:
    try:
        parsed = json.loads(raw_response)
    except json.JSONDecodeError:
        return {
            "issues": [],
            "error": "DeepSeek did not return valid JSON.",
            "raw": raw_response,
        }

    if not isinstance(parsed, dict):
        return {
            "issues": [],
            "error": "Review response must be a JSON object.",
            "raw": raw_response,
        }

    issues = parsed.get("issues")
    if not isinstance(issues, list):
        return {
            "issues": [],
            "error": "Review response must contain an 'issues' list.",
            "raw": raw_response,
        }

    valid_issues = []
    invalid_issues = []

    for issue in issues:
        if not isinstance(issue, dict):
            invalid_issues.append(issue)
            continue

        file_path = issue.get("file")
        line = issue.get("line")
        severity = issue.get("severity")
        comment = issue.get("comment")

        if not isinstance(file_path, str) or not file_path.strip():
            invalid_issues.append(issue)
            continue

        if not isinstance(line, int) or line < 1:
            invalid_issues.append(issue)
            continue

        if not isinstance(severity, str) or severity.lower() not in ALLOWED_SEVERITIES:
            invalid_issues.append(issue)
            continue

        if not isinstance(comment, str) or not comment.strip():
            invalid_issues.append(issue)
            continue

        valid_issues.append(
            {
                "file": file_path,
                "line": line,
                "severity": severity.lower(),
                "comment": comment,
            }
        )

    result = {"issues": valid_issues}
    if invalid_issues:
        result["warning"] = f"Skipped {len(invalid_issues)} invalid issue(s)."

    return result


def review_diff(diff: str) -> dict:
    client = get_deepseek_client()

    response = client.chat.completions.create(
        model="deepseek-v4-flash",
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": (
                    "You are an experienced code reviewer. "
                    "Find real bugs, security issues, missing edge cases, or bad logic in PR diffs. "
                    "Prefer one high-signal comment over many low-value comments. "
                    "\n\n"
                    "You only see the diff, not the full repo, its issue tracker, its test suite, or "
                    "any linked discussion. Do not make factual claims about how a library, framework, "
                    "parser, or runtime behaves unless that behavior is directly visible in the diff "
                    "itself (e.g. shown in a code comment, docstring, or test assertion that is part of "
                    "the diff). This applies even if the behavior seems well-known to you — your training "
                    "data may be stale or wrong for the exact version in this PR. "
                    "If a potential issue depends on external behavior you cannot verify from the diff "
                    "alone, either omit it, or phrase it as a question / suggestion to verify rather than "
                    "an assertion (e.g. 'Consider confirming that X still does Y in the current version, "
                    "and linking the source' instead of 'X does Y'). Never state as fact something you "
                    "are inferring rather than reading. "
                    "Return ONLY valid JSON with this shape: "
                    '{"issues":[{"file":"path/to/file.py","line":123,"severity":"high|medium|low","comment":"feedback"}]}. '
                    'If there are no issues, return {"issues":[]}.'
                ),
            },
            {
                "role": "user",
                "content": f"Review this pull request diff and return JSON:\n\n{diff}",
            },
        ],
    )

    raw_response = response.choices[0].message.content
    return validate_review_json(raw_response)


SEVERITY_EMOJI = {"high": "🔴", "medium": "🟡", "low": "🟢"}


def format_review_comment(result: dict) -> str:
    issues = result.get("issues", [])

    if not issues:
        return "**PR Review Bot**\n\nNo issues found in this diff."

    lines = [f"**PR Review Bot** found {len(issues)} issue(s):", ""]
    for issue in issues:
        emoji = SEVERITY_EMOJI.get(issue["severity"], "⚪")
        lines.append(
            f"- {emoji} **{issue['severity'].upper()}** `{issue['file']}:{issue['line']}` — {issue['comment']}"
        )

    if result.get("warning"):
        lines.append("")
        lines.append(f"_{result['warning']}_")

    return "\n".join(lines)


def maybe_post_review(owner: str, repo: str, pr_number: int, result: dict) -> None:
    if (owner, repo) in BLOCKED_TARGETS:
        print(f"Skipping comment post: {owner}/{repo} is a blocked demo target.")
        return

    if not POST_COMMENTS:
        print("POST_COMMENTS is not enabled — skipping comment post (dry run).")
        return

    body = format_review_comment(result)
    posted = post_pr_comment(owner, repo, pr_number, body)
    print(f"Posted comment: {posted.get('html_url', posted.get('url', '(no url returned)'))}")


def main() -> None:
    prs = list_closed_prs(TARGET_OWNER, TARGET_REPO, limit=5)
    if not prs:
        print(f"No pull requests found in {TARGET_OWNER}/{TARGET_REPO}.")
        print("Create a small PR first, or set TARGET_OWNER/TARGET_REPO in .env to a repo with existing PRs.")
        return

    chosen_pr = prs[0]

    print(f"Reviewing real PR #{chosen_pr['number']}: {chosen_pr['title']}")
    print(chosen_pr["html_url"])
    print()

    diff = get_pr_diff(TARGET_OWNER, TARGET_REPO, chosen_pr["number"])
    print(f"Diff length: {len(diff)} characters")
    print("Sending real diff to DeepSeek...")
    print()

    result = review_diff(diff)
    if result.get("error"):
        print(f"Review failed safely: {result['error']}")
        print()

    if result.get("warning"):
        print(result["warning"])
        print()

    print(json.dumps(result, indent=2))
    print()

    maybe_post_review(TARGET_OWNER, TARGET_REPO, chosen_pr["number"], result)


if __name__ == "__main__":
    main()
