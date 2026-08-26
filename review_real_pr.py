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
from jira_ticket import JiraTicket, resolve_ticket_for_pr
from pr_context import RepoContext, safe_build_context
from review_prompt import build_system_prompt, build_user_prompt
from review_result import ReviewResult, parse_review_result

load_dotenv()

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


def review_pr(
    diff: str,
    context: RepoContext | None = None,
    ticket: JiraTicket | None = None,
) -> ReviewResult:
    """One LLM call producing both the code review and the Jira verdict.

    When no ticket resolved, the Jira half is dropped from the prompt entirely
    and the verdict is set here rather than asked of the model.
    """
    client = get_deepseek_client()

    response = client.chat.completions.create(
        model="deepseek-v4-flash",
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": build_system_prompt(ticket)},
            {"role": "user", "content": build_user_prompt(diff, context=context, ticket=ticket)},
        ],
    )

    result = parse_review_result(response.choices[0].message.content)

    if ticket is None:
        # Nothing to align against — don't let a stray model-invented verdict stand.
        result.jira_verdict = "no_ticket_linked"
        result.missing_requirements = []

    # The model can't know what context it wasn't shown, so this is set from
    # the pipeline rather than trusted from the response.
    result.context_truncated = bool(context and context.truncated)

    return result


SEVERITY_EMOJI = {"blocking": "🔴", "warning": "🟡", "note": "🟢"}

VERDICT_DISPLAY = {
    "satisfies": ("✅", "Satisfies the ticket"),
    "partial": ("🟡", "Partially satisfies the ticket"),
    "does_not_satisfy": ("❌", "Does not satisfy the ticket"),
    "no_ticket_linked": ("⚪", "No Jira ticket linked"),
}


def format_review_comment(result: ReviewResult, ticket: JiraTicket | None = None) -> str:
    lines = ["**PR Review Bot**"]

    # Caveat goes at the top, not buried — it qualifies everything below it.
    if result.context_truncated:
        lines += [
            "",
            "> ⚠️ _Codebase context was truncated (lookup caps reached), so some callers or "
            "usages may not have been checked. Findings and the ticket verdict below are "
            "based on a partial view._",
        ]

    lines.append("")
    if result.issues:
        lines.append(f"**Code review** — {len(result.issues)} finding(s):")
        lines.append("")
        for issue in result.issues:
            emoji = SEVERITY_EMOJI.get(issue.severity, "⚪")
            location = f"`{issue.file}:{issue.line}`" if issue.line is not None else f"`{issue.file}`"
            lines.append(f"- {emoji} **{issue.severity.upper()}** {location} — {issue.description}")
    else:
        lines.append("**Code review** — no issues found in this diff.")

    emoji, label = VERDICT_DISPLAY.get(result.jira_verdict, ("⚪", result.jira_verdict))
    lines += ["", "---", ""]
    if ticket is not None:
        ticket_ref = f"[{ticket.key}]({ticket.url})" if ticket.url else ticket.key
        lines.append(f"**Jira alignment** ({ticket_ref} — {ticket.summary})")
    else:
        lines.append("**Jira alignment**")
    lines += ["", f"{emoji} **{label}**"]

    if result.missing_requirements:
        lines += ["", "Not addressed:"]
        lines += [f"- {requirement}" for requirement in result.missing_requirements]

    if result.reasoning:
        lines += ["", f"_{result.reasoning}_"]

    if result.error:
        lines += ["", f"_⚠️ {result.error}_"]

    return "\n".join(lines)


def maybe_post_review(
    owner: str,
    repo: str,
    pr_number: int,
    result: ReviewResult,
    ticket: JiraTicket | None = None,
) -> None:
    if (owner, repo) in BLOCKED_TARGETS:
        print(f"Skipping comment post: {owner}/{repo} is a blocked demo target.")
        return

    if not POST_COMMENTS:
        print("POST_COMMENTS is not enabled — skipping comment post (dry run).")
        return

    body = format_review_comment(result, ticket)
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

    branch_ref = chosen_pr.get("head", {}).get("ref")
    ticket = resolve_ticket_for_pr(branch_ref, chosen_pr.get("title"))
    print(f"Jira ticket: {ticket.key} — {ticket.summary}" if ticket else "Jira ticket: none linked")

    print("Fetching repo context (imports/callers/subclasses for what this diff touches)...")
    context = safe_build_context(TARGET_OWNER, TARGET_REPO, diff, chosen_pr["head"]["sha"])
    if context:
        truncated_note = " (truncated)" if context.truncated else ""
        print(f"Context: {len(context.text)} characters{truncated_note}")
    else:
        print("Context: none")
    print("Sending diff + context + ticket to DeepSeek...")
    print()

    result = review_pr(diff, context=context, ticket=ticket)
    if result.error:
        print(f"Review degraded safely: {result.error}")
        print()

    print(json.dumps(result.model_dump(), indent=2))
    print()

    maybe_post_review(TARGET_OWNER, TARGET_REPO, chosen_pr["number"], result, ticket)


if __name__ == "__main__":
    main()
