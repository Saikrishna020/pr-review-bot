"""One-off provisioning script: creates 5 real Jira tickets in the SCRUM
sandbox project for the real-PR eval batch (see run_real_pr_batch.py).

Each ticket's `expected_mistake` field records, BEFORE any code is written,
which realistic dev mistake the paired PR will intentionally contain — same
discipline as the hand-authored golden sets, just against a real ticket
instead of a synthetic one.

Not meant to be re-run casually: it creates real tickets with real IDs.
Run once, then hardcode the returned keys into run_real_pr_batch.py.

Usage: python eval/create_seed_tickets.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))

from jira_ticket import get_jira_config  # noqa: E402


def adf(*paragraphs_and_lists: object) -> dict:
    """Builds a minimal ADF doc from a mix of plain strings (paragraphs) and
    lists of strings (bullet lists)."""
    content = []
    for item in paragraphs_and_lists:
        if isinstance(item, str):
            content.append({"type": "paragraph", "content": [{"type": "text", "text": item}]})
        else:
            content.append({
                "type": "bulletList",
                "content": [
                    {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": line}]}]}
                    for line in item
                ],
            })
    return {"type": "doc", "version": 1, "content": content}


TICKETS = [
    {
        "id": "seed-1-debug-config",
        "summary": "Add GET /debug/config to report which optional integrations are active",
        "expected_mistake": "none — fully correct implementation",
        "description": adf(
            "We keep learning about missing integrations (Jira, GitHub App, LangSmith) only "
            "when something silently degrades. Add a read-only GET /debug/config endpoint, "
            "following the existing /debug/rate-limit pattern.",
            "Acceptance criteria:",
            [
                "Response includes jira_configured, github_app_configured, and "
                "langsmith_tracing_enabled as booleans",
                "Response includes target_owner and target_repo",
                "No secret values (tokens, keys, emails) appear anywhere in the response",
                "Existing /debug/rate-limit and /debug/github-auth endpoints are unaffected",
            ],
        ),
    },
    {
        "id": "seed-2-jira-cache",
        "summary": "Cache fetch_jira_ticket results in-memory to avoid duplicate fetches on webhook retries",
        "expected_mistake": "cache key collides across different ticket keys sharing a project prefix",
        "description": adf(
            "GitHub retries webhook deliveries on timeout, which can cause the same PR to be "
            "reviewed twice in quick succession, hitting Jira for the same ticket key each time. "
            "Add a simple in-memory cache to fetch_jira_ticket so a repeated lookup for the same "
            "ticket key within the process lifetime reuses the previous result instead of calling "
            "Jira again.",
            "Acceptance criteria:",
            [
                "A second call to fetch_jira_ticket with the same ticket key does not make a new "
                "HTTP request to Jira",
                "Different ticket keys are cached independently and must never return each "
                "other's cached result",
                "A ticket key that previously returned None (not found) is not retried on every "
                "call either",
            ],
        ),
    },
    {
        "id": "seed-3-rate-limit-pct",
        "summary": "/debug/rate-limit should report both core and search remaining as percentages",
        "expected_mistake": "implements core_remaining_pct only, omits search_remaining_pct",
        "description": adf(
            "The /debug/rate-limit endpoint currently returns raw core and search rate limit "
            "objects, but reading raw remaining/limit numbers to gauge risk is slow to eyeball. "
            "Add a computed percentage field for each resource.",
            "Acceptance criteria:",
            [
                "Response includes core_remaining_pct (core.remaining / core.limit * 100, "
                "rounded to 1 decimal)",
                "Response includes search_remaining_pct the same way — this one matters most "
                "since search's budget is far tighter and burns out first",
                "Existing core/search raw objects remain in the response unchanged",
            ],
        ),
    },
    {
        "id": "seed-4-webhook-secret-warning",
        "summary": "Log a startup warning when GITHUB_WEBHOOK_SECRET is unset",
        "expected_mistake": "adds a hardcoded fallback secret that silently defeats the warning and the security control",
        "description": adf(
            "Right now if GITHUB_WEBHOOK_SECRET is left unset, the app silently skips signature "
            "verification on /webhook — visible only by reading _verify_signature's code. Add a "
            "warning-level log line at startup (in the lifespan handler, alongside the existing "
            "startup info log) when the secret is missing.",
            "Acceptance criteria:",
            [
                "A warning-level log line appears at startup when GITHUB_WEBHOOK_SECRET is unset",
                "No warning is logged when the secret is set",
                "The warning message makes clear that webhook signature checks are being skipped",
            ],
        ),
    },
    {
        "id": "seed-5-readme-langsmith-docs",
        "summary": "Document LANGSMITH_API_KEY / LANGSMITH_PROJECT in the README Setup section",
        "expected_mistake": "diff is unrelated to the ticket (wrong branch pushed)",
        "description": adf(
            "We just added optional LangSmith tracing (see .env.example). Update the README's "
            "environment variables section to mention LANGSMITH_API_KEY, LANGSMITH_TRACING, and "
            "LANGSMITH_PROJECT as optional tracing config, consistent with how the Jira block is "
            "documented as optional.",
            "Acceptance criteria:",
            [
                "README's environment variables list includes the three LangSmith vars with a "
                "one-line explanation each",
                "No other README sections change",
            ],
        ),
    },
]


def create_ticket(domain: str, email: str, token: str, summary: str, description: dict) -> dict:
    base = f"https://{domain}.atlassian.net"
    response = httpx.post(
        f"{base}/rest/api/3/issue",
        auth=(email, token),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        json={
            "fields": {
                "project": {"key": "SCRUM"},
                "summary": summary,
                "description": description,
                "issuetype": {"name": "Task"},
            }
        },
        timeout=15,
    )
    response.raise_for_status()
    data = response.json()
    return {"key": data["key"], "url": f"{base}/browse/{data['key']}"}


def main() -> None:
    config = get_jira_config()
    if config is None:
        print("Jira not configured (need JIRA_DOMAIN/JIRA_EMAIL/JIRA_API_TOKEN in .env).")
        return
    domain, email, token = config

    created = []
    for ticket in TICKETS:
        result = create_ticket(domain, email, token, ticket["summary"], ticket["description"])
        print(f"{result['key']}: {ticket['summary']}")
        print(f"    {result['url']}")
        print(f"    expected mistake: {ticket['expected_mistake']}")
        created.append({**result, "id": ticket["id"], "summary": ticket["summary"], "expected_mistake": ticket["expected_mistake"]})

    out_path = Path(__file__).parent / "results" / "seed_tickets.json"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(created, indent=2), encoding="utf-8")
    print(f"\nSaved {out_path}")


if __name__ == "__main__":
    main()
