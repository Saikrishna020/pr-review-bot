# PR Review Bot — real-PR ticket report

5 real PRs, each linked to a real Jira ticket in the SCRUM sandbox, each seeded with one realistic dev mistake decided before the diff was written. Reviewed through the actual pipeline (real diff, real Phase 2 context, real Jira fetch, real posted GitHub comment).

**Every seeded condition (5/5) was caught or correctly recognized as clean.**

For the LLM's actual reasoning trace (prompt sent, raw structured output, token usage, latency) per case, open the **pr-review-bot** project in LangSmith and filter by the metadata field noted under each ticket below.

---

## SCRUM-10 — Add GET /debug/config to report which optional integrations are active

- Ticket: [SCRUM-10](https://saikrishnaaakash.atlassian.net/browse/SCRUM-10)
- PR: [#4 — SCRUM-10: Add GET /debug/config endpoint](https://github.com/Saikrishna020/pr-review-bot/pull/4)
- Branch: `SCRUM-10-debug-config`
- LangSmith: project **pr-review-bot**, filter `metadata.pr_number = 4` (or `metadata.ticket_key = "SCRUM-10"`)

**What was seeded (decided before the diff was written):** none — fully correct

**Bot's verdict:** ✅ Satisfies (expected `satisfies`, matched the pre-registered expectation)

**Code issues found:** none

**Bot's reasoning:** The diff adds GET /debug/config with the three required booleans (jira_configured, github_app_configured, langsmith_tracing_enabled) and target_owner/target_repo. It does not return secret values: get_jira_config() is only used as a None-check, GITHUB_APP_ID is only used via bool(), and LANGSMITH_TRACING/TARGET_* are not credentials. Existing debug endpoints are not modified; the change is additive and consistent with the /debug/rate-limit pattern.

**How well did it do:** Clean case, correctly recognized as clean. This is the false-positive check — the bot did not invent problems in a genuinely correct diff.

---

## SCRUM-11 — Cache fetch_jira_ticket results in-memory to avoid duplicate fetches on webhook retries

- Ticket: [SCRUM-11](https://saikrishnaaakash.atlassian.net/browse/SCRUM-11)
- PR: [#5 — SCRUM-11: Cache Jira ticket lookups in-memory](https://github.com/Saikrishna020/pr-review-bot/pull/5)
- Branch: `SCRUM-11-jira-cache`
- LangSmith: project **pr-review-bot**, filter `metadata.pr_number = 5` (or `metadata.ticket_key = "SCRUM-11"`)

**What was seeded (decided before the diff was written):** cache key strips trailing digits (project prefix only), so different ticket keys in the same project collide and can return each other's cached result

**Bot's verdict:** 🟡 Partial (expected `satisfies`, did NOT match the pre-registered expectation (see note below))

**Code issues found (1):**

- **[BLOCKING]** `jira_ticket.py:196` — The cache key is ticket_id.rstrip('0123456789'), which strips all trailing digits from the key. That means PROJ-1 and PROJ-2 both map to 'PROJ-' and share a single cache entry. The first cached result (or None from a 404) will be returned for every other ticket in the same project, violating the acceptance criterion that different ticket keys are cached independently and must never return each other's cached result. Use the full ticket_id as the cache key, not a project-prefix-only value.

**Requirements the bot said were not met:**

- Cache must be keyed by the full ticket key so different ticket keys are cached independently and never return each other's result.

**Bot's reasoning:** The diff adds an in-memory cache and correctly caches 404 'not found' results as None, but the cache key collapses all tickets sharing the same project prefix into one entry. This directly contradicts the explicit requirement that different ticket keys never share/return each other's cached result, and can cause the wrong ticket context or a false 'not found' to be served for sibling issues.

**How well did it do:** The seeded mistake was caught, with a verdict/severity that reflects its real impact.

---

## SCRUM-12 — /debug/rate-limit should report both core and search remaining as percentages

- Ticket: [SCRUM-12](https://saikrishnaaakash.atlassian.net/browse/SCRUM-12)
- PR: [#6 — SCRUM-12: Add core_remaining_pct to /debug/rate-limit](https://github.com/Saikrishna020/pr-review-bot/pull/6)
- Branch: `SCRUM-12-rate-limit-pct`
- LangSmith: project **pr-review-bot**, filter `metadata.pr_number = 6` (or `metadata.ticket_key = "SCRUM-12"`)

**What was seeded (decided before the diff was written):** implements core_remaining_pct only; omits search_remaining_pct, the one the ticket calls out as mattering most

**Bot's verdict:** 🟡 Partial (expected `partial`, matched the pre-registered expectation)

**Code issues found (2):**

- **[BLOCKING]** `main.py` — The response never includes search_remaining_pct, which is an explicit acceptance criterion and described as the most important field. The diff computes and returns only core_remaining_pct.
- **[WARNING]** `main.py:148` — The expression uses core['remaining'] after checking only core.get('limit'). If the rate-limit payload ever contains a truthy limit but a missing remaining key, this raises KeyError. Use .get for both fields, consistent with the defensive resource access above.

**Requirements the bot said were not met:**

- Add search_remaining_pct to the /debug/rate-limit response, computed as search['remaining'] / search['limit'] * 100 and rounded to 1 decimal.

**Bot's reasoning:** The diff implements core_remaining_pct correctly and preserves the core/search raw objects, satisfying part of the ticket. However, the search_remaining_pct field, which is explicitly required and called out as the most important, is completely missing. Therefore the change only partially satisfies the ticket.

**How well did it do:** The seeded mistake was caught, with a verdict/severity that reflects its real impact.

---

## SCRUM-13 — Log a startup warning when GITHUB_WEBHOOK_SECRET is unset

- Ticket: [SCRUM-13](https://saikrishnaaakash.atlassian.net/browse/SCRUM-13)
- PR: [#7 — SCRUM-13: Warn at startup when GITHUB_WEBHOOK_SECRET is unset](https://github.com/Saikrishna020/pr-review-bot/pull/7)
- Branch: `SCRUM-13-webhook-secret-warning`
- LangSmith: project **pr-review-bot**, filter `metadata.pr_number = 7` (or `metadata.ticket_key = "SCRUM-13"`)

**What was seeded (decided before the diff was written):** warning logic is correct, but a hardcoded fallback for WEBHOOK_SECRET was added alongside it, committed to a public repo — a real security issue outside the ticket's literal ask

**Bot's verdict:** 🟡 Partial (expected `satisfies`, did NOT match the pre-registered expectation (see note below))

**Code issues found (1):**

- **[BLOCKING]** `main.py:25` — Replacing a missing GITHUB_WEBHOOK_SECRET with a hardcoded placeholder makes WEBHOOK_SECRET truthy. The ticket says the current behavior is to skip signature verification when the secret is unset; that skip is presumably driven by WEBHOOK_SECRET being falsy. After this change, _verify_signature will no longer skip checks but will verify against the literal placeholder, so valid GitHub webhook deliveries will be rejected (or unsigned ones rejected) in the exact scenario this PR is meant to warn about. It also makes the existing startup info log report the secret as "set" because it checks WEBHOOK_SECRET, while the new warning says it is unset — contradictory and misleading. Add the warning without changing WEBHOOK_SECRET's value.

**Requirements the bot said were not met:**

- Preserve the existing runtime behavior of skipping webhook signature verification when GITHUB_WEBHOOK_SECRET is unset; the ticket only asks for a startup warning, not a change to verification.

**Bot's reasoning:** The diff does add a warning-level startup log when GITHUB_WEBHOOK_SECRET is unset, with a message that clearly states webhook signature checks are being skipped. However, it also introduces a placeholder default for WEBHOOK_SECRET, making the variable truthy when the env var is absent. This is inconsistent with the ticket description (which says the unset case currently skips verification) and will likely cause _verify_signature to attempt verification with a bogus secret instead of skipping it. The startup info log in the same lifespan will also now report the secret as set, contradicting the new warning. Therefore the ticket is only partially satisfied.

**How well did it do:** The seeded mistake was caught, with a verdict/severity that reflects its real impact.

---

## SCRUM-14 — Document LANGSMITH_API_KEY / LANGSMITH_PROJECT in the README Setup section

- Ticket: [SCRUM-14](https://saikrishnaaakash.atlassian.net/browse/SCRUM-14)
- PR: [#8 — SCRUM-14: Document LangSmith env vars in README](https://github.com/Saikrishna020/pr-review-bot/pull/8)
- Branch: `SCRUM-14-readme-langsmith-docs`
- LangSmith: project **pr-review-bot**, filter `metadata.pr_number = 8` (or `metadata.ticket_key = "SCRUM-14"`)

**What was seeded (decided before the diff was written):** diff is entirely unrelated to the ticket (wrong branch pushed) — changes a local demo script's print limit, not the README

**Bot's verdict:** ❌ Does not satisfy (expected `does_not_satisfy`, matched the pre-registered expectation)

**Code issues found:** none

**Requirements the bot said were not met:**

- README environment variables list does not include LANGSMITH_API_KEY, LANGSMITH_TRACING, or LANGSMITH_PROJECT
- No one-line explanations for the LangSmith variables were added
- No changes were made to the README Setup/environment variables section at all

**Bot's reasoning:** The diff only changes the diff-preview character limit in fetch_real_pr_diff.py from 3000 to 5000 characters. It is entirely unrelated to the ticket, which asks for documenting LANGSMITH_API_KEY, LANGSMITH_TRACING, and LANGSMITH_PROJECT in the README's environment variables section. None of the acceptance criteria are addressed, and no README file is touched. The ticket is therefore not satisfied.

⚠️ Phase 2 context was truncated for this review (lookup caps hit) — the verdict above was made from a knowingly incomplete view of the codebase.

**How well did it do:** The seeded mistake was caught, with a verdict/severity that reflects its real impact.

---

## Next step

Read each trace above for the model's actual token-by-token reasoning. If a trace reveals the structured output itself needs to change (e.g. a new field, a different severity taxonomy, a verdict category that's missing), the workflow is: raise a real Jira ticket describing the needed output-schema change, then implement it as its own PR — same discipline as everything else here, not an ad hoc prompt tweak.