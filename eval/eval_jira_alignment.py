"""Grades the Jira-alignment verdict against a hand-authored golden set.

Mirrors eval_context_retrieval.py's structure, with one important difference
in how it scores: verdict errors are NOT reported as a single accuracy
number, because the two directions of error are not equally bad.

  - A false "satisfies" tells a human the ticket is done when it isn't.
    That's the dangerous direction, and it is reported on its own line.
  - A false "does_not_satisfy" / over-cautious "partial" just costs someone
    a second look.

The prompt is deliberately biased against the first (see
review_prompt._ALIGNMENT_RULES), so this eval has to measure the two
separately or that bias is invisible.

Each case is one live LLM call, so a full run costs ~6 calls and a couple of
minutes.

Usage: python eval/eval_jira_alignment.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from jira_golden_set import CASES  # noqa: E402
from jira_ticket import fetch_jira_ticket  # noqa: E402
from review_real_pr import review_pr  # noqa: E402

# Phrases that indicate the bot told the reader the diff looks unrelated to
# the ticket, rather than merely incomplete.
_MISMATCH_HINTS = ("unrelated", "not related", "different", "wrong ticket", "no connection", "does not address")
_VAGUENESS_HINTS = ("underspecified", "no description", "no acceptance criteria", "vague", "not specified")


def run_case(case: dict) -> dict:
    ticket = fetch_jira_ticket(case["ticket_key"])
    if ticket is None:
        print(f"[{case['id']}] SKIP — could not fetch {case['ticket_key']} from Jira")
        return {"id": case["id"], "skipped": True}

    result = review_pr(case["diff"], context=None, ticket=ticket)
    verdict = result.jira_verdict
    acceptable = case.get("acceptable_verdicts", [case["expected_verdict"]])

    verdict_ok = verdict in acceptable
    false_satisfies = verdict == "satisfies" and "satisfies" not in acceptable
    # Called a complete implementation incomplete — the safe-but-noisy direction.
    false_negative = verdict != "satisfies" and acceptable == ["satisfies"]

    status = "PASS" if verdict_ok else ("FALSE-SATISFIES" if false_satisfies else "FAIL")
    print(f"[{status}] {case['id']} ({case['ticket_key']})")
    print(f"    expected {'|'.join(acceptable)}, got {verdict}")

    checks_ok = True

    if case.get("expect_mismatch_flagged"):
        text = f"{result.reasoning} {' '.join(result.missing_requirements)}".lower()
        flagged = any(hint in text for hint in _MISMATCH_HINTS)
        print(f"    mismatch flagged in reasoning: {'yes' if flagged else 'NO'}")
        checks_ok = checks_ok and flagged

    if case["ticket_key"] == "SCRUM-5":
        text = f"{result.reasoning} {' '.join(result.missing_requirements)}".lower()
        noted = any(hint in text for hint in _VAGUENESS_HINTS)
        print(f"    underspecification noted: {'yes' if noted else 'NO'}")
        checks_ok = checks_ok and noted

    if result.error:
        print(f"    error: {result.error}")

    print(f"    reasoning: {result.reasoning[:300]}")
    if result.missing_requirements:
        for requirement in result.missing_requirements[:4]:
            print(f"    missing: {requirement}")
    print()

    return {
        "id": case["id"],
        "skipped": False,
        "verdict_ok": verdict_ok,
        "checks_ok": checks_ok,
        "false_satisfies": false_satisfies,
        "false_negative": false_negative,
        "is_stress_test": case.get("false_satisfies_stress_test", False),
    }


def main() -> None:
    results = [run_case(case) for case in CASES]
    scored = [r for r in results if not r["skipped"]]
    if not scored:
        print("No cases ran.")
        return

    verdict_passes = sum(r["verdict_ok"] for r in scored)
    full_passes = sum(r["verdict_ok"] and r["checks_ok"] for r in scored)
    false_satisfies = [r for r in scored if r["false_satisfies"]]
    false_negatives = [r for r in scored if r["false_negative"]]

    print("=" * 72)
    print(f"Verdict correct:        {verdict_passes}/{len(scored)}")
    print(f"Verdict + extra checks: {full_passes}/{len(scored)}")
    print()
    print(f"False 'satisfies' (DANGEROUS):     {len(false_satisfies)}/{len(scored)}"
          + (f"  -> {', '.join(r['id'] for r in false_satisfies)}" if false_satisfies else ""))
    print(f"False 'not satisfied' (cautious):  {len(false_negatives)}/{len(scored)}"
          + (f"  -> {', '.join(r['id'] for r in false_negatives)}" if false_negatives else ""))

    stress = [r for r in scored if r["is_stress_test"]]
    if stress:
        held = sum(r["verdict_ok"] for r in stress)
        print(f"\nBuried-requirement stress test:    {held}/{len(stress)} held "
              "(diff meets every bullet but misses a requirement stated only in prose)")

    print("\nNot covered here: the truncation interaction (a diff large enough to trip "
          "Phase 2's caps must not yield a confident 'satisfies'). These cases all run "
          "with context=None; that check needs a real PR.")


if __name__ == "__main__":
    main()
