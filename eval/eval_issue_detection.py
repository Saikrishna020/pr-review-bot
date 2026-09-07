"""Grades general issue detection against a hand-authored golden set — the
"does it find real bugs and leave clean/correct code alone" axis.

Mirrors eval_jira_alignment.py's structure. Scored on two things that are
NOT equally bad, same reasoning as the Jira eval:

  - Missing a real bug just costs someone the value the bot was supposed to
    provide (recall failure).
  - Flagging clean or correctly-refactored code erodes trust in every future
    review, including the ones that catch something real (false-positive
    failure). Reported on its own line.

Each case is one live LLM call, so a full run costs ~6 calls and under a
minute.

Usage: python eval/eval_issue_detection.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from issue_golden_set import CASES  # noqa: E402
from review_real_pr import review_pr  # noqa: E402


def run_case(case: dict) -> dict:
    result = review_pr(
        case["diff"],
        context=None,
        ticket=None,
        trace_metadata={"eval": "issue_detection", "case_id": case["id"]},
    )
    flagged = bool(result.issues)
    expect_flag = case["expect_flag"]

    false_positive = flagged and not expect_flag
    missed = expect_flag and not flagged

    severity_ok = True
    keyword_ok = True
    if expect_flag and flagged:
        allowed_severities = case.get("expect_severity_in")
        if allowed_severities:
            severity_ok = any(issue.severity in allowed_severities for issue in result.issues)

        keywords = case.get("expect_keywords")
        if keywords:
            text = " ".join(issue.description.lower() for issue in result.issues)
            keyword_ok = any(kw in text for kw in keywords)

    passed = (flagged == expect_flag) and severity_ok and keyword_ok

    status = "PASS" if passed else ("FALSE-POSITIVE" if false_positive else "FAIL")
    print(f"[{status}] {case['id']}")
    print(f"    expected flag={expect_flag}, got flag={flagged} ({len(result.issues)} issue(s))")
    if flagged and expect_flag:
        print(f"    severity match: {'yes' if severity_ok else 'NO'}, keyword match: {'yes' if keyword_ok else 'NO'}")
    for issue in result.issues:
        print(f"    - [{issue.severity}] {issue.file}:{issue.line} — {issue.description[:160]}")
    if result.error:
        print(f"    error: {result.error}")
    print()

    return {
        "id": case["id"],
        "passed": passed,
        "false_positive": false_positive,
        "missed": missed,
        "is_stress_test": case.get("false_positive_stress_test", False),
    }


def main() -> None:
    results = [run_case(case) for case in CASES]

    total_passes = sum(r["passed"] for r in results)
    false_positives = [r for r in results if r["false_positive"]]
    missed = [r for r in results if r["missed"]]
    stress = [r for r in results if r["is_stress_test"]]

    print("=" * 72)
    print(f"Overall:                        {total_passes}/{len(results)}")
    print()
    print(f"False positives (clean/correct code flagged): {len(false_positives)}/{len(results)}"
          + (f"  -> {', '.join(r['id'] for r in false_positives)}" if false_positives else ""))
    print(f"Missed real issues:                            {len(missed)}/{len(results)}"
          + (f"  -> {', '.join(r['id'] for r in missed)}" if missed else ""))

    if stress:
        held = sum(r["passed"] for r in stress)
        print(f"\nFalse-positive stress test (correct-but-large refactor): {held}/{len(stress)} held")

    print("\nNot covered here: whether findings survive when Phase 2 context is present "
          "(same-file/import/caller data mixed into the prompt) — these cases all run with "
          "context=None to isolate pure code-review judgement.")


if __name__ == "__main__":
    main()
