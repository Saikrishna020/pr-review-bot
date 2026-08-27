"""Precision/recall of pr_context's GitHub-code-search-based caller/subclass
lookup against a hand-verified golden set, plus end-to-end checks of things
that only show up across a whole diff (like the changed-symbol cap).

Note on reproducibility: unlike a pinned local clone, this now searches
whatever GitHub's code search currently has indexed for the repo's default
branch. The golden set was verified with `grep` against the pinned sha noted
in each case, but a rerun later can drift if the target repo's code around
those call sites has since changed — that's an accepted trade-off of not
keeping a local checkout (see pr_context.py's module docstring).

Case types (see golden_set.json):
- "callers" (default): checks pr_context.find_references for one symbol
  against hand-verified confirmed/possible caller sets.
- "symbol_cap_truncation": checks pr_context.build_context end to end
  against a real PR diff known to exceed MAX_CHANGED_SYMBOLS, asserting the
  cap being hit is visible in the assembled context text.

A "callers" case can also set `"known_limitation": true` — this means the
expected values are the true ground truth, not what the tool can currently
reach (e.g. a real match that GitHub's code search ranks past the candidate
cap). These are scored and reported, but kept out of the main pass tally:
mixing a documented, accepted gap in with cases that should always pass
would make an expected 0.00 recall look like a regression instead of what
it is. If a future change (bigger cap, smarter query, import-graph-aware
ranking) fixes one, that shows up here as the score moving off 0.00.

Usage: python eval/eval_context_retrieval.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from code_graph import Symbol  # noqa: E402
from fetch_real_pr_diff import get_pr_diff, get_pr_head  # noqa: E402
from pr_context import MAX_CHANGED_SYMBOLS, build_context, find_references  # noqa: E402

GOLDEN_SET_PATH = Path(__file__).parent / "golden_set.json"

TRUNCATION_NOTE_RE = re.compile(r"this diff changed (\d+) functions/classes; only the first (\d+) were checked")


def _score(expected: list[tuple[str, int]], actual: list[tuple[str, int]]) -> tuple[float, float]:
    expected_set, actual_set = set(expected), set(actual)
    true_positives = expected_set & actual_set
    precision = len(true_positives) / len(actual_set) if actual_set else 1.0
    recall = len(true_positives) / len(expected_set) if expected_set else 1.0
    return precision, recall


def run_callers_case(case: dict) -> tuple[bool, bool]:
    """Returns (matches_golden_set_exactly, found_anything_at_all). The second
    value is what known_limitation cases are actually scored on — see main().
    """
    symbol = Symbol(
        name=case["symbol_name"], kind=case.get("symbol_kind", "function"),
        start_line=case.get("symbol_start_line", 0), end_line=case.get("symbol_end_line", 0),
    )

    confirmed, possible = find_references(case["owner"], case["repo"], symbol, case["symbol_file"])

    expected_confirmed = [(c["file"], c["line"]) for c in case["expected_confirmed_callers"]]
    expected_possible = [(c["file"], c["line"]) for c in case["expected_possible_callers"]]

    conf_precision, conf_recall = _score(expected_confirmed, confirmed)
    poss_precision, poss_recall = _score(expected_possible, possible)

    tag = " (known limitation — expected values are ground truth, not what the tool can reach)" if case.get("known_limitation") else ""
    print(f"[{case['id']}]{tag}")
    print(f"  confirmed — precision {conf_precision:.2f}, recall {conf_recall:.2f} "
          f"(expected {len(expected_confirmed)}, got {len(confirmed)})")
    print(f"  possible  — precision {poss_precision:.2f}, recall {poss_recall:.2f} "
          f"(expected {len(expected_possible)}, got {len(possible)})")

    missing = set(expected_confirmed) - set(confirmed)
    extra = set(confirmed) - set(expected_confirmed)
    if missing:
        print(f"  missing confirmed: {sorted(missing)}")
    if extra:
        print(f"  unexpected confirmed: {sorted(extra)}")

    matches_exactly = conf_precision == 1.0 and conf_recall == 1.0 and poss_precision == 1.0 and poss_recall == 1.0
    found_anything = bool(confirmed) or bool(possible)
    return matches_exactly, found_anything


def run_truncation_case(case: dict) -> bool:
    diff = get_pr_diff(case["owner"], case["repo"], case["pr_number"])
    head = get_pr_head(case["owner"], case["repo"], case["pr_number"])
    context = build_context(case["owner"], case["repo"], diff, head.sha)

    print(f"[{case['id']}]")

    # Two separate assertions, because Phase 3 depends on both: the note has
    # to be in the text (what the model reads) AND the structured flag has to
    # be set (what the posted comment and the "don't claim satisfies" rule
    # key off). One without the other is a silent half-failure.
    match = TRUNCATION_NOTE_RE.search(context.text)
    if match is None:
        print(f"  FAIL — no truncation note found in the assembled context "
              f"(diff may no longer touch enough symbols to hit the cap)")
        return False

    total_changed, checked = int(match.group(1)), int(match.group(2))
    min_expected = case.get("min_expected_changed_symbols", MAX_CHANGED_SYMBOLS + 1)

    print(f"  changed symbols detected: {total_changed}, checked: {checked} "
          f"(cap MAX_CHANGED_SYMBOLS={MAX_CHANGED_SYMBOLS}), context.truncated={context.truncated}")

    ok = total_changed >= min_expected and checked == MAX_CHANGED_SYMBOLS
    if not ok:
        print(f"  FAIL — expected at least {min_expected} changed symbols with "
              f"exactly {MAX_CHANGED_SYMBOLS} checked")
    if not context.truncated:
        print("  FAIL — truncation note present in text but RepoContext.truncated is False")
        ok = False
    return ok


def main() -> None:
    cases = json.loads(GOLDEN_SET_PATH.read_text(encoding="utf-8"))["cases"]

    scored: list[bool] = []
    known_limitation_reproduced: list[bool] = []

    for case in cases:
        case_type = case.get("type", "callers")

        if case_type == "symbol_cap_truncation":
            scored.append(run_truncation_case(case))
            continue

        matches_exactly, found_anything = run_callers_case(case)
        if case.get("known_limitation"):
            # "Reproduced" = the tool still finds nothing, matching what's
            # documented — that's the expected steady state, not a failure.
            # If it starts finding something, that's the gap narrowing and
            # worth a look, not a silent pass — flag it either way.
            reproduced = not found_anything
            if not reproduced:
                print("  NOTE: this case previously found nothing; it now found some "
                      "matches — the documented gap may have narrowed. Worth re-verifying "
                      "and updating the golden set rather than treating this as a failure.")
            known_limitation_reproduced.append(reproduced)
        else:
            scored.append(matches_exactly)

    print(f"\n{sum(scored)}/{len(scored)} scored cases matched the golden set exactly.")
    if known_limitation_reproduced:
        print(
            f"{sum(known_limitation_reproduced)}/{len(known_limitation_reproduced)} "
            f"known-limitation cases still reproduce their documented gap as expected "
            f"(excluded from the tally above by design — see module docstring)."
        )


if __name__ == "__main__":
    main()
