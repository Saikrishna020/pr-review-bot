"""Structured output for a combined code-quality + Jira-alignment review.

The LLM is asked to return exactly this shape; `parse_review_result` validates
it and degrades to an empty-but-valid result rather than raising, so a
malformed model response can never take down a review.
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

Severity = Literal["blocking", "warning", "note"]
JiraVerdict = Literal["satisfies", "partial", "does_not_satisfy", "no_ticket_linked"]


class Issue(BaseModel):
    severity: Severity
    file: str
    line: int | None = None
    description: str


class ReviewResult(BaseModel):
    issues: list[Issue] = Field(default_factory=list)
    jira_verdict: JiraVerdict = "no_ticket_linked"
    missing_requirements: list[str] = Field(default_factory=list)
    reasoning: str = ""

    # True when Phase 2's context hit its symbol/candidate caps, i.e. the
    # reviewer was working from a knowingly incomplete picture of the
    # codebase. Set from the RepoContext, not asked of the model — the model
    # can't know what it wasn't shown. Surfaced in the posted comment.
    context_truncated: bool = False

    # Not in the original design, but Phase 1 deliberately never let a bad
    # model response raise; keeping that property means recording the failure
    # somewhere. None on a clean parse.
    error: str | None = None


def parse_review_result(raw_response: str | None) -> ReviewResult:
    """Validates the model's JSON into a ReviewResult, never raising.

    A model that returns malformed JSON, or valid JSON in the wrong shape,
    yields an empty result carrying `error` — which the comment formatter
    renders as "review failed safely" rather than silently posting nothing.
    """
    if not raw_response or not raw_response.strip():
        return ReviewResult(error="The model returned an empty response.")

    try:
        parsed = json.loads(raw_response)
    except json.JSONDecodeError:
        return ReviewResult(error="The model did not return valid JSON.")

    if not isinstance(parsed, dict):
        return ReviewResult(error="Review response must be a JSON object.")

    # Drop individually-malformed issues instead of failing the whole review —
    # one bad entry shouldn't cost us the other findings.
    raw_issues = parsed.get("issues")
    if isinstance(raw_issues, list):
        valid_issues = []
        skipped = 0
        for candidate in raw_issues:
            try:
                valid_issues.append(Issue.model_validate(candidate))
            except ValidationError:
                skipped += 1
        parsed["issues"] = [issue.model_dump() for issue in valid_issues]
    else:
        parsed["issues"] = []
        skipped = 0

    try:
        result = ReviewResult.model_validate(parsed)
    except ValidationError as exc:
        # Shape is wrong beyond the issues list (e.g. an invented verdict
        # value); keep whatever issues survived rather than discarding them.
        return ReviewResult(
            issues=[Issue.model_validate(i) for i in parsed["issues"]],
            error=f"Review response failed validation: {exc.error_count()} field error(s).",
        )

    if skipped:
        result.error = f"Skipped {skipped} malformed issue entr{'y' if skipped == 1 else 'ies'}."
    return result
