"""Unit tests for ticket-key extraction and ADF flattening — no network."""

from jira_ticket import JiraTicket, extract_ticket_id, flatten_adf


def test_extracts_key_from_branch_name():
    assert extract_ticket_id("SCRUM-12-add-validation") == "SCRUM-12"
    assert extract_ticket_id("feature/PROJ-7-fix-thing") == "PROJ-7"


def test_falls_back_to_pr_title_when_branch_has_no_key():
    assert extract_ticket_id("fix-the-thing", "SCRUM-3: fix the thing") == "SCRUM-3"


def test_branch_wins_over_title_when_both_have_keys():
    assert extract_ticket_id("SCRUM-1-work", "SCRUM-99: unrelated") == "SCRUM-1"


def test_returns_none_when_no_key_anywhere():
    assert extract_ticket_id("just-a-branch", "just a title") is None
    assert extract_ticket_id(None, None) is None


def test_does_not_match_lowercase_or_version_like_strings():
    # `utf-8` and `scrum-1` must not read as ticket keys, or every branch
    # mentioning an encoding would trigger a bogus Jira lookup.
    assert extract_ticket_id("switch-to-utf-8") is None
    assert extract_ticket_id("scrum-1-lowercase") is None


def test_flatten_adf_keeps_bullet_list_structure():
    # Acceptance criteria are nearly always bullets; collapsing them into
    # run-on text is the failure mode this guards against.
    adf = {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Intro."}]},
            {
                "type": "bulletList",
                "content": [
                    {"type": "listItem", "content": [
                        {"type": "paragraph", "content": [{"type": "text", "text": "First"}]}]},
                    {"type": "listItem", "content": [
                        {"type": "paragraph", "content": [{"type": "text", "text": "Second"}]}]},
                ],
            },
        ],
    }
    result = flatten_adf(adf)
    assert "- First" in result
    assert "- Second" in result
    assert "Intro." in result


def test_flatten_adf_joins_marked_text_runs():
    # Bold/italic split a sentence into several text nodes; they must rejoin
    # without losing spacing.
    adf = {"type": "doc", "content": [{"type": "paragraph", "content": [
        {"type": "text", "text": "Return "},
        {"type": "text", "text": "400", "marks": [{"type": "strong"}]},
        {"type": "text", "text": " on bad input."},
    ]}]}
    assert flatten_adf(adf) == "Return 400 on bad input."


def test_flatten_adf_tolerates_none_string_and_empty():
    assert flatten_adf(None) == ""
    assert flatten_adf("  plain  ") == "plain"
    assert flatten_adf({"type": "doc", "content": []}) == ""


def test_has_detail_distinguishes_title_only_tickets():
    # Drives the "don't answer satisfies on an underspecified ticket" rule.
    assert not JiraTicket(key="S-1", summary="It's slow", description="").has_detail
    assert not JiraTicket(key="S-1", summary="It's slow", description="   ").has_detail
    assert JiraTicket(key="S-1", summary="x", description="- do the thing").has_detail
