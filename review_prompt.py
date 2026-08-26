"""Builds the reviewer prompt: one call that does code review and Jira
alignment together, rather than two independent passes.

The point of combining them is cross-referencing — Phase 2's caller/import
context is what lets the reviewer notice that a ticket's intent isn't fully
covered (e.g. the diff fixes a function but a caller shown in the context
needed the same fix). Two separate calls couldn't see that.

Two variants: combined (ticket resolved) and code-only (no ticket). In the
code-only variant the Jira section is omitted entirely and `jira_verdict` is
set in code afterwards, never asked of the model — there's nothing for it to
judge, so asking invites a fabricated answer.
"""

from __future__ import annotations

from jira_ticket import JiraTicket
from pr_context import RepoContext

# Carried over from Phase 1 — this instruction exists because the reviewer
# kept asserting confident, wrong claims about library behavior it couldn't
# see. Keep it when editing the rest of the prompt.
_GROUNDING_RULES = (
    "You are an experienced code reviewer. Find real bugs, security issues, missing edge "
    "cases, or bad logic. Prefer one high-signal finding over many low-value ones, and do "
    "not raise style-only nitpicks as blocking issues.\n\n"
    "You see the diff, plus (usually) some statically-derived context about the surrounding "
    "code — not the full repo, its issue tracker, its test suite, or any linked discussion. "
    "Do not make factual claims about how a library, framework, parser, or runtime behaves "
    "unless that behavior is directly visible in what you were given. This applies even if "
    "the behavior seems well-known to you — your training data may be stale or wrong for the "
    "exact version in this PR. If a potential issue depends on external behavior you cannot "
    "verify, either omit it or phrase it as a question to verify rather than an assertion. "
    "Never state as fact something you are inferring rather than reading."
)

_SCHEMA_RULES = (
    "Return ONLY valid JSON with this exact shape:\n"
    "{\n"
    '  "issues": [\n'
    '    {"severity": "blocking|warning|note", "file": "path/to/file.py", "line": 123, '
    '"description": "what is wrong and why"}\n'
    "  ],\n"
    '  "jira_verdict": "satisfies|partial|does_not_satisfy",\n'
    '  "missing_requirements": ["requirement the diff does not cover"],\n'
    '  "reasoning": "brief explanation of the verdict"\n'
    "}\n"
    'Use an empty list for "issues" if you find none. "line" may be null if a finding '
    "isn't tied to a specific line."
)

_CODE_ONLY_SCHEMA_RULES = (
    "Return ONLY valid JSON with this exact shape:\n"
    "{\n"
    '  "issues": [\n'
    '    {"severity": "blocking|warning|note", "file": "path/to/file.py", "line": 123, '
    '"description": "what is wrong and why"}\n'
    "  ],\n"
    '  "reasoning": "brief summary of the review"\n'
    "}\n"
    'Use an empty list for "issues" if you find none. "line" may be null if a finding '
    "isn't tied to a specific line."
)

# Deliberately asymmetric. A false "satisfies" is the dangerous direction —
# it tells a human the ticket is done when it isn't, and that's exactly the
# failure the eval's false-satisfies rate is built to catch. A false
# "partial" just costs someone a second look.
_ALIGNMENT_RULES = (
    "2. JIRA ALIGNMENT: Compare the diff against the ticket's requirements.\n"
    "   - Be skeptical by default. Only answer \"satisfies\" if EVERY stated requirement is "
    "clearly and completely addressed by the diff. If you are unsure whether a requirement "
    "is met, it is not met.\n"
    "   - Read the whole ticket description, not just any bulleted acceptance criteria. A "
    "requirement or edge case mentioned only in prose still counts, and missing one is a "
    "common failure.\n"
    "   - Use the related-code context: if it shows something the ticket implies but the diff "
    "doesn't handle (for example a caller that needed the same fix), record that as a missing "
    "requirement, not merely a code-quality note.\n"
    "   - If the diff appears unrelated to the ticket entirely, answer \"does_not_satisfy\" and "
    "say so explicitly in your reasoning — that usually means the wrong ticket was linked.\n"
    "   - List every unmet requirement in \"missing_requirements\"."
)


def _truncation_clause(context: RepoContext | None) -> str:
    if context is None or not context.truncated:
        return ""
    return (
        "\n   - IMPORTANT: the related-code context below is incomplete (it hit its lookup "
        "caps, as noted in that section). You may not have been shown every caller or usage. "
        "Do not answer \"satisfies\" with full confidence on the basis of code you were not "
        "shown — prefer \"partial\" and say what you could not verify."
    )


def _vagueness_clause(ticket: JiraTicket) -> str:
    """Policy for the underspecified-ticket case.

    A one-line ticket with no body gives nothing to verify completeness
    against, so "satisfies" would be unfalsifiable rather than earned. The
    verdict vocabulary has no dedicated "needs clarification" value, so this
    maps to `partial` with the ambiguity recorded explicitly.
    """
    if ticket.has_detail:
        return ""
    return (
        "\n   - NOTE: this ticket has a title but no description or acceptance criteria. You "
        "therefore cannot verify that the diff covers everything intended. Do not answer "
        "\"satisfies\". Answer \"partial\" if the diff is plausibly related to the title, or "
        "\"does_not_satisfy\" if it clearly is not, and state in your reasoning that the "
        "ticket is underspecified."
    )


def build_system_prompt(ticket: JiraTicket | None) -> str:
    if ticket is None:
        return f"{_GROUNDING_RULES}\n\n{_CODE_ONLY_SCHEMA_RULES}"
    return f"{_GROUNDING_RULES}\n\n{_SCHEMA_RULES}"


def build_user_prompt(
    diff: str,
    context: RepoContext | None = None,
    ticket: JiraTicket | None = None,
) -> str:
    parts: list[str] = []

    if ticket is not None:
        ticket_block = [f"## Ticket ({ticket.key})", f"Summary: {ticket.summary}"]
        if ticket.issue_type:
            ticket_block.append(f"Type: {ticket.issue_type}")
        if ticket.has_detail:
            ticket_block.append(f"\nRequirements / description:\n{ticket.description}")
        else:
            ticket_block.append("\n(This ticket has no description or acceptance criteria.)")
        parts.append("\n".join(ticket_block))

    parts.append(f"## Diff\n{diff}")

    if context:
        parts.append(f"## Related code (same file, imports, callers of changed symbols)\n{context.text}")

    instructions = [
        "---",
        "1. CODE REVIEW: Identify real issues in the diff. Use the related-code context to "
        "check whether this change could break callers, violate assumptions elsewhere in the "
        "codebase, or introduce inconsistency with surrounding code.",
    ]

    if ticket is not None:
        instructions.append(_ALIGNMENT_RULES + _truncation_clause(context) + _vagueness_clause(ticket))

    parts.append("\n\n".join(instructions))
    return "\n\n".join(parts)
