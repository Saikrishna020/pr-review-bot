"""Unit tests for parsing the model's JSON into a ReviewResult.

The through-line: a bad model response must degrade, never raise — that
property was deliberate in Phase 1 and has to survive the Phase 3 rewrite.
"""

import json

from review_result import ReviewResult, parse_review_result


def _payload(**overrides) -> str:
    base = {
        "issues": [
            {"severity": "blocking", "file": "a.py", "line": 12, "description": "boom"}
        ],
        "jira_verdict": "partial",
        "missing_requirements": ["second criterion"],
        "reasoning": "only one of two criteria met",
    }
    base.update(overrides)
    return json.dumps(base)


def test_parses_a_well_formed_response():
    result = parse_review_result(_payload())
    assert result.error is None
    assert result.jira_verdict == "partial"
    assert result.missing_requirements == ["second criterion"]
    assert len(result.issues) == 1
    assert result.issues[0].severity == "blocking"
    assert result.issues[0].line == 12


def test_malformed_json_degrades_instead_of_raising():
    result = parse_review_result("not json at all{{{")
    assert result.error is not None
    assert result.issues == []
    assert result.jira_verdict == "no_ticket_linked"


def test_empty_response_degrades():
    assert parse_review_result("").error is not None
    assert parse_review_result(None).error is not None


def test_one_bad_issue_does_not_discard_the_good_ones():
    raw = json.dumps({
        "issues": [
            {"severity": "blocking", "file": "a.py", "line": 1, "description": "real"},
            {"severity": "catastrophic", "file": "b.py", "description": "invalid severity"},
            {"file": "c.py", "description": "missing severity"},
        ],
        "jira_verdict": "satisfies",
        "missing_requirements": [],
        "reasoning": "ok",
    })
    result = parse_review_result(raw)
    assert len(result.issues) == 1
    assert result.issues[0].description == "real"
    assert "Skipped 2" in (result.error or "")


def test_invented_verdict_value_is_rejected_but_issues_survive():
    raw = json.dumps({
        "issues": [{"severity": "note", "file": "a.py", "line": 3, "description": "x"}],
        "jira_verdict": "probably_fine",  # not in the allowed literal
        "missing_requirements": [],
        "reasoning": "",
    })
    result = parse_review_result(raw)
    assert result.error is not None
    assert len(result.issues) == 1
    assert result.jira_verdict == "no_ticket_linked"  # falls back to the safe default


def test_line_may_be_null():
    raw = json.dumps({
        "issues": [{"severity": "note", "file": "a.py", "line": None, "description": "file-level"}],
        "jira_verdict": "satisfies",
        "missing_requirements": [],
        "reasoning": "",
    })
    result = parse_review_result(raw)
    assert result.error is None
    assert result.issues[0].line is None


def test_context_truncated_defaults_false_and_is_not_taken_from_model():
    # The model can't know what it wasn't shown, so even if it claims a value
    # the pipeline overwrites it; parsing just needs a safe default here.
    result = parse_review_result(_payload())
    assert result.context_truncated is False


def test_json_that_is_not_an_object_degrades():
    assert parse_review_result("[1, 2, 3]").error is not None


def test_missing_fields_fall_back_to_defaults():
    result = parse_review_result(json.dumps({"issues": []}))
    assert result.error is None
    assert result.jira_verdict == "no_ticket_linked"
    assert result.missing_requirements == []
    assert isinstance(result, ReviewResult)
