"""Resolves the Jira ticket a PR claims to implement, and flattens its
description into plain text the reviewer LLM can actually read.

Ticket resolution is deterministic (a regex over the branch name / PR title),
not a guess — same principle as Phase 2's context: if the answer is
structurally determined, parse it rather than asking a model.

Every failure path here returns None rather than raising. A PR with no ticket,
an unreachable Jira, or a bad token must still get a code review — the Jira
half is additive, never a gate. See `main.py` for the fallback wiring.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

import httpx
from dotenv import load_dotenv

# See fetch_real_pr_diff.py for why both paths are loaded (Render's Secret
# Files feature mounts at /etc/secrets/, not the working directory).
load_dotenv()
load_dotenv("/etc/secrets/.env", override=False)

log = logging.getLogger("uvicorn.error")

# A Jira key is PROJECT-123. Anchored to a word boundary so `SCRUM-1` matches
# inside `SCRUM-1-add-validation` but a bare version string like `utf-8` doesn't.
TICKET_ID_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")

# Same case-insensitivity lesson as get_github_token: Linux (and therefore
# Render) treats env var names as case-sensitive, and the token may have been
# saved under a per-project name like `jira_pr-bot_api_token`. Hyphens are
# normalized to underscores so those still resolve.
JIRA_TOKEN_NAMES = ("JIRA_API_TOKEN", "JIRA_TOKEN", "JIRA_PR_BOT_API_TOKEN")
JIRA_EMAIL_NAMES = ("JIRA_EMAIL", "JIRA_USER_EMAIL")
JIRA_DOMAIN_NAMES = ("JIRA_DOMAIN", "JIRA_SITE")


@dataclass
class JiraTicket:
    key: str
    summary: str
    description: str  # ADF flattened to plain text; "" if the ticket has no body
    status: str | None = None
    issue_type: str | None = None
    url: str | None = None

    @property
    def has_detail(self) -> bool:
        """Whether this ticket says enough to grade a diff against.

        A title-only ticket can't support a confident "satisfies" verdict —
        there are no stated requirements to check completeness against. The
        prompt uses this to bias toward `partial` instead. See
        `review_prompt.py`.
        """
        return bool(self.description.strip())


def _env_lookup(names: tuple[str, ...]) -> str | None:
    for name, value in os.environ.items():
        if name.upper().replace("-", "_") in names and value.strip():
            return value.strip()
    return None


def get_jira_config() -> tuple[str, str, str] | None:
    """Returns (domain, email, token), or None if any part is missing.

    Missing Jira config is a normal state (the bot works without it), so this
    reports absence rather than raising.
    """
    domain = _env_lookup(JIRA_DOMAIN_NAMES)
    email = _env_lookup(JIRA_EMAIL_NAMES)
    token = _env_lookup(JIRA_TOKEN_NAMES)
    if not (domain and email and token):
        return None
    return domain, email, token


def extract_ticket_id(*candidates: str | None) -> str | None:
    """First Jira key found across the given strings, in order.

    Callers pass the branch name first, then the PR title — branch is the
    convention Jira's own "Create branch" button follows, but a key typed into
    the title is a common enough fallback to be worth checking.
    """
    for candidate in candidates:
        if not candidate:
            continue
        match = TICKET_ID_RE.search(candidate)
        if match:
            return match.group(1)
    return None


# ADF node types that should read as their own block of text. Anything not
# listed just has its children inlined.
_BLOCK_TYPES = {
    "paragraph", "heading", "codeBlock", "blockquote",
    "panel", "rule", "tableRow", "mediaSingle",
}


def _walk_adf(node: object, out: list[str]) -> None:
    if isinstance(node, list):
        for item in node:
            _walk_adf(item, out)
        return

    if not isinstance(node, dict):
        return

    node_type = node.get("type")

    if node_type == "text":
        out.append(str(node.get("text", "")))
        return
    if node_type == "hardBreak":
        out.append("\n")
        return
    # Mentions/emoji carry their display text in attrs, not a child text node.
    if node_type == "mention":
        out.append(str(node.get("attrs", {}).get("text", "")))
        return
    if node_type == "emoji":
        out.append(str(node.get("attrs", {}).get("shortName", "")))
        return
    if node_type == "inlineCard":
        out.append(str(node.get("attrs", {}).get("url", "")))
        return

    children = node.get("content") or []

    # Acceptance criteria are almost always a bullet/numbered list, so keeping
    # the list structure (rather than run-on text) materially changes how
    # readable the requirements are in the prompt.
    #
    # List items are collapsed to a single line each: their children are
    # paragraphs, whose block newlines would otherwise split every bullet into
    # a bare "-" followed by its text on the next line. Nested sub-lists
    # flatten into the parent bullet, which is a deliberate trade for
    # readability over exact structure.
    if node_type == "listItem":
        sub: list[str] = []
        _walk_adf(children, sub)
        item_text = re.sub(r"\s+", " ", "".join(sub)).strip()
        if item_text:
            out.append(f"\n- {item_text}")
        return

    if node_type in _BLOCK_TYPES:
        out.append("\n")
        _walk_adf(children, out)
        out.append("\n")
        return

    _walk_adf(children, out)


def flatten_adf(adf_doc: object) -> str:
    """Flattens an Atlassian Document Format description into plain text.

    Jira Cloud's v3 API returns descriptions as an ADF node tree, not a string.
    Tolerates a plain string (some fields/instances return one) and None.
    """
    if adf_doc is None:
        return ""
    if isinstance(adf_doc, str):
        return adf_doc.strip()

    out: list[str] = []
    _walk_adf(adf_doc, out)
    text = "".join(out)

    # Collapse the padding introduced by block/list handling.
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# In-memory cache of prior lookups, so a webhook retry for the same PR
# doesn't re-hit Jira for a ticket key it already resolved this process.
_ticket_cache: dict[str, JiraTicket | None] = {}


def fetch_jira_ticket(ticket_id: str) -> JiraTicket | None:
    """Fetches one ticket. Returns None if Jira isn't configured, the ticket
    doesn't exist, or the request fails — all of which mean "review the code
    without ticket context", never "fail the review".
    """
    cache_key = ticket_id.rstrip("0123456789")
    if cache_key in _ticket_cache:
        return _ticket_cache[cache_key]

    config = get_jira_config()
    if config is None:
        log.info("Jira not configured (need domain, email, token) — skipping ticket lookup for %s", ticket_id)
        return None

    domain, email, token = config
    base = f"https://{domain}.atlassian.net"
    url = f"{base}/rest/api/3/issue/{ticket_id}"

    try:
        response = httpx.get(
            url,
            auth=(email, token),
            headers={"Accept": "application/json"},
            params={"fields": "summary,description,status,issuetype"},
            timeout=15,
        )
        if response.status_code == 404:
            log.info("Jira ticket %s not found (404) — reviewing without ticket context", ticket_id)
            _ticket_cache[cache_key] = None
            return None
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPError:
        log.warning("Jira fetch failed for %s — reviewing without ticket context", ticket_id, exc_info=True)
        return None
    except ValueError:
        log.warning("Jira returned a non-JSON body for %s", ticket_id, exc_info=True)
        return None

    fields = data.get("fields") or {}
    ticket = JiraTicket(
        key=data.get("key", ticket_id),
        summary=(fields.get("summary") or "").strip(),
        description=flatten_adf(fields.get("description")),
        status=((fields.get("status") or {}).get("name")),
        issue_type=((fields.get("issuetype") or {}).get("name")),
        url=f"{base}/browse/{data.get('key', ticket_id)}",
    )
    _ticket_cache[cache_key] = ticket
    return ticket


def resolve_ticket_for_pr(branch_name: str | None, pr_title: str | None) -> JiraTicket | None:
    """Convenience wrapper: find a ticket key in the branch/title, then fetch it."""
    ticket_id = extract_ticket_id(branch_name, pr_title)
    if ticket_id is None:
        log.info("No Jira ticket key found in branch=%r title=%r", branch_name, pr_title)
        return None
    return fetch_jira_ticket(ticket_id)
