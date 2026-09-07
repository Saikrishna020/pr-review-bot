"""Runs the real webhook-equivalent pipeline against 5 real, live PRs opened
against Saikrishna020/pr-review-bot, each linked to a real Jira ticket in the
SCRUM sandbox (see create_seed_tickets.py), each seeded with one realistic
dev mistake decided BEFORE the diff was written (see TICKET_MISTAKES below).

This is the real-PR complement to the golden sets (issue_golden_set.py,
jira_golden_set.py): those isolate judgement with context=None; this exercises
the full path — real diff, real Phase 2 context, real Jira fetch, real
posted comment — the same code path main.py's /webhook uses.

Requires POST_COMMENTS=true to actually post; run with:
    POST_COMMENTS=true python eval/run_real_pr_batch.py

Usage: python eval/run_real_pr_batch.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from fetch_real_pr_diff import get_pr_diff, get_pr_head  # noqa: E402
from jira_ticket import resolve_ticket_for_pr  # noqa: E402
from pr_context import safe_build_context  # noqa: E402
from review_real_pr import format_review_comment, maybe_post_review, review_pr  # noqa: E402

OWNER, REPO = "Saikrishna020", "pr-review-bot"

# Ground truth, decided before the diff was written for each — same
# discipline as the golden sets, just against a real PR this time.
TICKET_MISTAKES = {
    "SCRUM-10": {"expected_verdict": "satisfies", "expected_mistake": "none — fully correct"},
    "SCRUM-11": {
        "expected_verdict": "satisfies",
        "expected_mistake": "cache key strips trailing digits (project prefix only), so different "
        "ticket keys in the same project collide and can return each other's cached result",
    },
    "SCRUM-12": {
        "expected_verdict": "partial",
        "expected_mistake": "implements core_remaining_pct only; omits search_remaining_pct, the "
        "one the ticket calls out as mattering most",
    },
    "SCRUM-13": {
        "expected_verdict": "satisfies",
        "expected_mistake": "warning logic is correct, but a hardcoded fallback for WEBHOOK_SECRET "
        "was added alongside it, committed to a public repo — a real security issue outside the "
        "ticket's literal ask",
    },
    "SCRUM-14": {
        "expected_verdict": "does_not_satisfy",
        "expected_mistake": "diff is entirely unrelated to the ticket (wrong branch pushed) — "
        "changes a local demo script's print limit, not the README",
    },
}


def run_pr(pr_number: int) -> dict:
    diff = get_pr_diff(OWNER, REPO, pr_number)
    head = get_pr_head(OWNER, REPO, pr_number)
    ticket = resolve_ticket_for_pr(head.ref, head.title)

    print(f"PR #{pr_number}: {head.title}")
    print(f"  branch: {head.ref}")
    print(f"  ticket: {ticket.key if ticket else 'NONE RESOLVED'}")

    context = safe_build_context(OWNER, REPO, diff, head.sha)

    trace_metadata = {
        "owner": OWNER,
        "repo": REPO,
        "pr_number": pr_number,
        "ticket_key": ticket.key if ticket else None,
        "entry_point": "real_pr_batch_eval",
    }
    result = review_pr(diff, context=context, ticket=ticket, trace_metadata=trace_metadata)

    ground_truth = TICKET_MISTAKES.get(ticket.key, {}) if ticket else {}
    expected_verdict = ground_truth.get("expected_verdict")
    verdict_ok = expected_verdict is None or result.jira_verdict == expected_verdict

    print(f"  ticket summary: {ticket.summary if ticket else '-'}")
    print(f"  seeded mistake: {ground_truth.get('expected_mistake', '-')}")
    print(f"  bot verdict:    {result.jira_verdict} (expected {expected_verdict}) — {'PASS' if verdict_ok else 'FAIL'}")
    print(f"  issues found:   {len(result.issues)}")
    for issue in result.issues:
        print(f"    - [{issue.severity}] {issue.file}:{issue.line} — {issue.description[:160]}")
    print(f"  reasoning: {result.reasoning[:300]}")
    if result.missing_requirements:
        for req in result.missing_requirements:
            print(f"    missing: {req}")
    print()

    maybe_post_review(OWNER, REPO, pr_number, result, ticket)
    print()

    return {
        "pr_number": pr_number,
        "pr_title": head.title,
        "branch": head.ref,
        "ticket_key": ticket.key if ticket else None,
        "ticket_summary": ticket.summary if ticket else None,
        "ticket_url": ticket.url if ticket else None,
        "seeded_mistake": ground_truth.get("expected_mistake"),
        "expected_verdict": expected_verdict,
        "bot_verdict": result.jira_verdict,
        "verdict_ok": verdict_ok,
        "issues": [issue.model_dump() for issue in result.issues],
        "missing_requirements": result.missing_requirements,
        "reasoning": result.reasoning,
        "context_truncated": result.context_truncated,
        "comment_posted": format_review_comment(result, ticket) is not None,
    }


def main() -> None:
    numbers_path = Path(__file__).parent / "results" / "real_pr_numbers.json"
    pr_numbers = [row["number"] for row in json.loads(numbers_path.read_text(encoding="utf-8"))]

    results = [run_pr(n) for n in pr_numbers]

    out_path = Path(__file__).parent / "results" / "real_pr_batch.json"
    out_path.write_text(json.dumps(results, indent=2), encoding="utf-8")

    md_lines = ["# Real-PR eval batch", "", "| PR | Ticket | Seeded mistake | Expected | Bot said | Match |", "|---|---|---|---|---|---|"]
    for r in results:
        md_lines.append(
            f"| [#{r['pr_number']}](https://github.com/{OWNER}/{REPO}/pull/{r['pr_number']}) "
            f"| {r['ticket_key']} | {r['seeded_mistake']} | {r['expected_verdict']} | {r['bot_verdict']} "
            f"| {'✅' if r['verdict_ok'] else '❌'} |"
        )
    (Path(__file__).parent / "results" / "real_pr_batch.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")

    passes = sum(r["verdict_ok"] for r in results)
    print("=" * 72)
    print(f"Real-PR batch: {passes}/{len(results)} verdicts matched expectations")
    print(f"Saved {out_path}")


if __name__ == "__main__":
    main()
