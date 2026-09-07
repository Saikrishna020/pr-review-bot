"""Runs both golden sets (Jira alignment + issue detection) for real and
saves the results — not just prints them — to eval/results/, so a run's
numbers are a durable artifact instead of scrollback.

Writes:
  eval/results/golden_set_results.json  — full structured results, one row
                                           per case, plus aggregates
  eval/results/golden_set_results.md    — the same, as a readable table for
                                           the README / portfolio writeup

Usage: python eval/save_golden_set_results.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from eval_issue_detection import run_case as run_issue_case  # noqa: E402
from eval_jira_alignment import run_case as run_jira_case  # noqa: E402
from issue_golden_set import CASES as ISSUE_CASES  # noqa: E402
from jira_golden_set import CASES as JIRA_CASES  # noqa: E402

RESULTS_DIR = Path(__file__).parent / "results"


def run_all() -> dict:
    print("=" * 72)
    print("JIRA ALIGNMENT")
    print("=" * 72)
    jira_results = [run_jira_case(case) for case in JIRA_CASES]

    print("=" * 72)
    print("ISSUE DETECTION")
    print("=" * 72)
    issue_results = [run_issue_case(case) for case in ISSUE_CASES]

    jira_scored = [r for r in jira_results if not r["skipped"]]
    jira_pass = sum(r["verdict_ok"] for r in jira_scored)
    issue_pass = sum(r["passed"] for r in issue_results)

    return {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "jira_alignment": {
            "cases": jira_results,
            "passed": jira_pass,
            "total": len(jira_scored),
            "false_satisfies": [r["id"] for r in jira_scored if r["false_satisfies"]],
        },
        "issue_detection": {
            "cases": issue_results,
            "passed": issue_pass,
            "total": len(issue_results),
            "false_positives": [r["id"] for r in issue_results if r["false_positive"]],
            "missed": [r["id"] for r in issue_results if r["missed"]],
        },
        "overall": {
            "passed": jira_pass + issue_pass,
            "total": len(jira_scored) + len(issue_results),
        },
    }


def write_markdown(data: dict, path: Path) -> None:
    lines = [
        "# Golden-set eval results",
        "",
        f"Run at: {data['run_at']}",
        "",
        f"**Overall: {data['overall']['passed']}/{data['overall']['total']}**",
        "",
        "## Jira alignment",
        "",
        f"{data['jira_alignment']['passed']}/{data['jira_alignment']['total']} verdicts correct.",
    ]
    if data["jira_alignment"]["false_satisfies"]:
        lines.append(f"False 'satisfies' (dangerous direction): {', '.join(data['jira_alignment']['false_satisfies'])}")
    lines += ["", "| Case | Ticket | Verdict OK | Extra checks OK |", "|---|---|---|---|"]
    for r in data["jira_alignment"]["cases"]:
        if r["skipped"]:
            lines.append(f"| {r['id']} | — | SKIPPED | — |")
            continue
        lines.append(f"| {r['id']} | {'✅' if r['verdict_ok'] else '❌'} | {'✅' if r['checks_ok'] else '❌'} |")

    lines += [
        "",
        "## Issue detection",
        "",
        f"{data['issue_detection']['passed']}/{data['issue_detection']['total']} cases passed.",
    ]
    if data["issue_detection"]["false_positives"]:
        lines.append(f"False positives: {', '.join(data['issue_detection']['false_positives'])}")
    if data["issue_detection"]["missed"]:
        lines.append(f"Missed real issues: {', '.join(data['issue_detection']['missed'])}")
    lines += ["", "| Case | Passed |", "|---|---|"]
    for r in data["issue_detection"]["cases"]:
        lines.append(f"| {r['id']} | {'✅' if r['passed'] else '❌'} |")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    RESULTS_DIR.mkdir(exist_ok=True)
    data = run_all()

    json_path = RESULTS_DIR / "golden_set_results.json"
    json_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    md_path = RESULTS_DIR / "golden_set_results.md"
    write_markdown(data, md_path)

    print("=" * 72)
    print(f"Saved {json_path}")
    print(f"Saved {md_path}")
    print(f"Overall: {data['overall']['passed']}/{data['overall']['total']}")


if __name__ == "__main__":
    main()
