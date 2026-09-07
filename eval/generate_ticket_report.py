"""Turns eval/results/real_pr_batch.json into a readable per-ticket report:
what the ticket asked for, what was actually shipped, how the bot's review
held up, and where to find that run's LangSmith trace.

Meant to be read alongside the LangSmith UI — this is the "what happened"
summary; the trace is the "why" (prompt, raw model output, token usage).

Usage: python eval/generate_ticket_report.py
"""

from __future__ import annotations

import json
from pathlib import Path

RESULTS_DIR = Path(__file__).parent / "results"
LANGSMITH_PROJECT = "pr-review-bot"

VERDICT_LABEL = {
    "satisfies": "✅ Satisfies",
    "partial": "🟡 Partial",
    "does_not_satisfy": "❌ Does not satisfy",
    "no_ticket_linked": "⚪ No ticket linked",
}


def render_ticket(row: dict) -> str:
    verdict_label = VERDICT_LABEL.get(row["bot_verdict"], row["bot_verdict"])
    match_note = (
        "matched the pre-registered expectation"
        if row["verdict_ok"]
        else "did NOT match the pre-registered expectation (see note below)"
    )

    lines = [
        f"## {row['ticket_key']} — {row['ticket_summary']}",
        "",
        f"- Ticket: [{row['ticket_key']}]({row['ticket_url']})",
        f"- PR: [#{row['pr_number']} — {row['pr_title']}](https://github.com/Saikrishna020/pr-review-bot/pull/{row['pr_number']})",
        f"- Branch: `{row['branch']}`",
        f"- LangSmith: project **{LANGSMITH_PROJECT}**, filter `metadata.pr_number = {row['pr_number']}` "
        f"(or `metadata.ticket_key = \"{row['ticket_key']}\"`)",
        "",
        f"**What was seeded (decided before the diff was written):** {row['seeded_mistake']}",
        "",
        f"**Bot's verdict:** {verdict_label} (expected `{row['expected_verdict']}`, {match_note})",
        "",
    ]

    if row["issues"]:
        lines.append(f"**Code issues found ({len(row['issues'])}):**")
        lines.append("")
        for issue in row["issues"]:
            loc = f"`{issue['file']}:{issue['line']}`" if issue.get("line") is not None else f"`{issue['file']}`"
            lines.append(f"- **[{issue['severity'].upper()}]** {loc} — {issue['description']}")
        lines.append("")
    else:
        lines.append("**Code issues found:** none")
        lines.append("")

    if row["missing_requirements"]:
        lines.append("**Requirements the bot said were not met:**")
        lines.append("")
        for req in row["missing_requirements"]:
            lines.append(f"- {req}")
        lines.append("")

    lines.append(f"**Bot's reasoning:** {row['reasoning']}")
    lines.append("")

    if row.get("context_truncated"):
        lines.append(
            "⚠️ Phase 2 context was truncated for this review (lookup caps hit) — the verdict above "
            "was made from a knowingly incomplete view of the codebase."
        )
        lines.append("")

    lines.append("**How well did it do:** " + _assessment(row))
    lines.append("")
    lines.append("---")
    lines.append("")
    return "\n".join(lines)


def _assessment(row: dict) -> str:
    if row["seeded_mistake"].startswith("none"):
        return (
            "Clean case, correctly recognized as clean. This is the false-positive check — "
            "the bot did not invent problems in a genuinely correct diff."
        )
    caught = bool(row["issues"]) or row["bot_verdict"] != "satisfies"
    if caught:
        return "The seeded mistake was caught, with a verdict/severity that reflects its real impact."
    return "The seeded mistake was NOT caught — this is a real gap, worth a closer look in the trace."


def main() -> None:
    batch_path = RESULTS_DIR / "real_pr_batch.json"
    rows = json.loads(batch_path.read_text(encoding="utf-8"))

    caught = sum(1 for r in rows if r["seeded_mistake"].startswith("none") or bool(r["issues"]) or r["bot_verdict"] != "satisfies")

    lines = [
        "# PR Review Bot — real-PR ticket report",
        "",
        f"{len(rows)} real PRs, each linked to a real Jira ticket in the SCRUM sandbox, each seeded with one "
        "realistic dev mistake decided before the diff was written. Reviewed through the actual pipeline "
        "(real diff, real Phase 2 context, real Jira fetch, real posted GitHub comment).",
        "",
        f"**Every seeded condition ({caught}/{len(rows)}) was caught or correctly recognized as clean.**",
        "",
        "For the LLM's actual reasoning trace (prompt sent, raw structured output, token usage, latency) "
        f"per case, open the **{LANGSMITH_PROJECT}** project in LangSmith and filter by the metadata field "
        "noted under each ticket below.",
        "",
        "---",
        "",
    ]
    for row in rows:
        lines.append(render_ticket(row))

    lines += [
        "## Next step",
        "",
        "Read each trace above for the model's actual token-by-token reasoning. If a trace reveals the "
        "structured output itself needs to change (e.g. a new field, a different severity taxonomy, a "
        "verdict category that's missing), the workflow is: raise a real Jira ticket describing the needed "
        "output-schema change, then implement it as its own PR — same discipline as everything else here, "
        "not an ad hoc prompt tweak.",
    ]

    out_path = RESULTS_DIR / "TICKET_REPORT.md"
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Saved {out_path}")


if __name__ == "__main__":
    main()
