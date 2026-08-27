"""Unit tests for prompt assembly — the two variants and the conditional
skepticism clauses. No network, no model call.
"""

from jira_ticket import JiraTicket
from pr_context import RepoContext
from review_prompt import build_system_prompt, build_user_prompt

DETAILED = JiraTicket(
    key="SCRUM-1",
    summary="Reject non-positive pr_number",
    description="- Return 400 when pr_number <= 0\n- Leave valid values unaffected",
    issue_type="Bug",
)

VAGUE = JiraTicket(key="SCRUM-5", summary="Make the bot faster", description="")


def test_code_only_variant_omits_jira_entirely():
    user = build_user_prompt("diff text", context=None, ticket=None)
    assert "JIRA ALIGNMENT" not in user
    assert "## Ticket" not in user
    assert "CODE REVIEW" in user

    system = build_system_prompt(None)
    assert "jira_verdict" not in system  # nothing to ask the model for


def test_combined_variant_includes_ticket_and_alignment_task():
    user = build_user_prompt("diff text", context=None, ticket=DETAILED)
    assert "## Ticket (SCRUM-1)" in user
    assert "Reject non-positive pr_number" in user
    assert "Return 400 when pr_number <= 0" in user
    assert "JIRA ALIGNMENT" in user

    system = build_system_prompt(DETAILED)
    assert "jira_verdict" in system


def test_truncated_context_adds_the_do_not_claim_satisfies_warning():
    truncated = RepoContext(text="some context", truncated=True)
    user = build_user_prompt("diff", context=truncated, ticket=DETAILED)
    assert "incomplete" in user
    assert 'Do not answer "satisfies" with full confidence' in user


def test_untruncated_context_omits_that_warning():
    complete = RepoContext(text="some context", truncated=False)
    user = build_user_prompt("diff", context=complete, ticket=DETAILED)
    assert 'Do not answer "satisfies" with full confidence' not in user


def test_vague_ticket_forbids_satisfies():
    user = build_user_prompt("diff", context=None, ticket=VAGUE)
    assert "no description or acceptance criteria" in user
    assert 'Do not answer "satisfies"' in user
    assert "underspecified" in user


def test_detailed_ticket_does_not_get_the_vagueness_clause():
    user = build_user_prompt("diff", context=None, ticket=DETAILED)
    assert "underspecified" not in user


def test_context_text_is_included_when_present():
    context = RepoContext(text="CALLER: foo.py:12", truncated=False)
    user = build_user_prompt("diff", context=context, ticket=None)
    assert "CALLER: foo.py:12" in user
    assert "Related code" in user


def test_empty_context_section_is_skipped():
    user = build_user_prompt("diff", context=RepoContext(text="", truncated=False), ticket=None)
    assert "Related code" not in user
