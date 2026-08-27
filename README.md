# PR Review Bot

An automated code review bot that watches GitHub pull requests, builds deterministic repo context for the diff (imports, callers, class hierarchy — parsed, not embedded), pulls in the linked Jira ticket, and answers two questions in one pass: *are there real problems in this code*, and *does this diff actually do what the ticket asked for*. Findings are posted back as a PR comment — no manual steps once wired up.

## How it works

```
PR opened/updated on target repo
  → GitHub sends a webhook to POST /webhook
  → server acknowledges immediately
  → in the background:
      fetch diff
      → concurrently:
          · fetch + parse just the files this diff touches (nothing stored)
            + GitHub code search for what calls/subclasses the changed symbols
          · resolve the Jira key from the branch name and fetch that ticket
      → one DeepSeek call (diff + code context + ticket)
      → post comment: code findings, then a Jira alignment verdict
```

There's also a manual trigger, `POST /review`, for testing a specific PR on demand without needing a live webhook.

### Repository context

Rather than embeddings/RAG, context is built with static analysis, so it's deterministic and free at query time — and rather than a local clone of the repo, everything is fetched on demand per review, so nothing is stored or kept in sync between requests:

- `code_graph.py` parses one Python file's text at a time with tree-sitter into definitions, call sites, and imports. Python-only for now; a language dispatch would be the extension point if that ever needs to change.
- For each PR diff, `pr_context.py` fetches just the changed files (GitHub Contents API) and maps changed lines to the symbols they belong to. For each changed function/class, it asks GitHub's code search for files that might reference it, then fetches and re-parses only those candidates to confirm they're real references (not a comment or a same-named unrelated thing) — a single text search alone isn't trustworthy enough to hand an LLM as fact.
- Confirmed matches (the caller's file actually imports the changed file, verified by parsing it) are separated from possible matches (name matches, import not confirmed) — both the prompt and the eval treat that distinction as real signal, not noise.
- All the independent GitHub API calls for one review run through a shared thread pool (`pr_context.MAX_WORKERS`), not sequentially — a diff touching several files can still mean dozens of API calls, and doing them one at a time is the difference between ~30s and ~5 minutes.
- Known trade-off of not keeping a local clone: GitHub code search only indexes the default branch, isn't guaranteed fully fresh, and is rate-limited (~10 requests/min authenticated) — so caller lookups are capped to the first few changed symbols per review (`MAX_CHANGED_SYMBOLS`) and the first few search hits per symbol (`MAX_SEARCH_CANDIDATES`). Search queries are scoped with `extension:py` so a common identifier's real code hits aren't crowded out of that small cap by markdown docs. Hitting either cap is not silent: it's logged, and a note is added to the assembled context itself (e.g. "only checked the first 8 of 46 search matches") so the reviewing LLM knows the absence of a caller means "not checked," not "doesn't exist."
- `eval/eval_context_retrieval.py` checks caller lookups against a hand-verified golden set (`eval/golden_set.json`, 8 cases as of writing — a name collision, a single-file caller, a mix of confirmed+possible, two high-volume multi-file callers, a true negative, and two cases that deliberately hit the tool's own caps: one PR that trips `MAX_CHANGED_SYMBOLS` — checking the truncation is visible in the assembled context, not just logged — and one symbol (`Option`) whose 3 real subclasses rank past `MAX_SEARCH_CANDIDATES` in code search, a documented "known limitation" case scored separately so an accepted gap doesn't read as a regression, and so a future fix shows up as the score moving off 0.00) and reports precision/recall — run it with `python eval/eval_context_retrieval.py`. Since it now depends on live search results rather than a pinned local clone, results can drift over time if the target repo changes near the verified call sites; each case records the sha (or PR number, for the diff-level case) it was verified against.
- `tests/` has fast, offline unit tests for the parsing logic itself (definitions, bases, calls, import forms, diff hunk parsing) — no network calls, run with `pytest tests/`. One of them is a regression test for a real bug the eval caught during development: `resolve_python_import_paths` built GitHub API paths with plain `str(Path(...))`, which is backslash-joined on Windows and silently broke every absolute-import resolution.

### Jira alignment

The ticket is a third input to the *same* review call, not a separate check bolted on afterwards. That's deliberate: sharing one call means the reviewer can cross-reference, e.g. notice that a caller visible in the code context also needed the fix the ticket described — something two independent passes structurally cannot see.

- **Ticket resolution is deterministic.** `jira_ticket.extract_ticket_id` regexes a `PROJ-123` key out of the branch name, falling back to the PR title. Same principle as the code context: if the answer is structurally determined, parse it rather than asking a model.
- **Descriptions are flattened from ADF.** Jira Cloud returns descriptions as an Atlassian Document Format node tree, not text. `flatten_adf` walks it, preserving bullet structure specifically — acceptance criteria are nearly always bullet lists, and collapsing them into run-on prose measurably degrades the most important part of the prompt.
- **Jira is never a gate.** No key in the branch, ticket 404s, bad token, Jira unreachable, no Jira configured at all — every one of those paths returns `None` and the review proceeds code-only, with `jira_verdict` set to `no_ticket_linked` in code rather than asked of the model (nothing to judge means nothing to hallucinate).
- **The prompt is asymmetrically skeptical.** A false "satisfies" tells a human the ticket is done when it isn't; a false "partial" just costs a second look. So the prompt requires *every* stated requirement to be clearly met, tells the model to read the whole description rather than only the bulleted criteria, and — when the code context was truncated — explicitly forbids a confident "satisfies" on the basis of code it wasn't shown.
- **Underspecified tickets map to `partial`.** A title-only ticket gives nothing to verify completeness against, so "satisfies" would be unfalsifiable rather than earned. The verdict vocabulary has no dedicated "needs clarification" value, so `review_prompt._vagueness_clause` fires when `JiraTicket.has_detail` is false and requires the reasoning to name the ambiguity. Known gap: `has_detail` only detects an *empty* body, so a short-but-useless description ("It's slow.") slips past the deterministic clause and relies on the model's own judgement.
- `eval/eval_jira_alignment.py` grades verdicts against `eval/jira_golden_set.py` — hand-authored `(diff, ticket, expected verdict)` cases whose expected answers were written down before any diff existed or the bot ran. Crucially it **does not report a single accuracy number**: false-"satisfies" is reported on its own line, separately from over-cautious errors, because the prompt is deliberately biased against the first and blending them would hide whether that bias works.

#### Current results (6/6, 0 false-"satisfies")

| Case | Ticket shape | Diff | Expected | Got |
|---|---|---|---|---|
| 1 | Clear, 4 acceptance criteria | Fully implements it | `satisfies` | ✅ `satisfies` |
| 2 | Clear requirement | Unrelated (emoji tweak) | `does_not_satisfy` | ✅ `does_not_satisfy` |
| 3 | Three sub-points | Implements 2 of 3 | `partial` | ✅ `partial` |
| 4 | **Edge case buried in prose** | Meets every bullet, misses the prose requirement | `partial`/`does_not_satisfy` | ✅ `partial` |
| 5 | Title-only, no description | Plausible perf change | `partial` + names the ambiguity | ✅ `partial` |
| 6 | Wrong ticket linked | README wording change | `does_not_satisfy` + flags mismatch | ✅ `does_not_satisfy` |

Case 4 is the one that matters most. Its acceptance-criteria bullets cover only 422/403 handling; the requirement to distinguish "code search hit its 1000-result cap" from "the symbol doesn't exist" appears only in the description's prose. A reviewer that skims the bullet list marks it done. The bot caught it and listed it as a missing requirement.

**Truncation case (verified on a real PR).** The golden-set cases above all run with `context=None`, to isolate ticket judgement from the context pipeline. The remaining question — does a diff large enough to trip Phase 2's caps avoid a confident `satisfies`? — was checked against a real PR ([#1](https://github.com/Saikrishna020/pr-review-bot/pull/1), linked to SCRUM-7, 102 changed symbols against a cap of 5). Result: `context_truncated=True` and the verdict hedged to `partial` rather than `satisfies`, as intended.

That run also turned up two real bugs in this codebase, both since fixed:

- **A data race.** `code_graph` held one module-level tree-sitter `Parser` shared across `pr_context`'s 16-worker pool. tree-sitter parsers aren't safe to call `.parse()` on concurrently. Parsers are now thread-local, with a regression test that runs 64 concurrent parses.
- **Silent degradation.** A rate-limited code search or a failed file fetch produced context that was quietly incomplete but presented as complete — so an empty callers list read as "nothing calls this" rather than "we couldn't look". `search_code` now returns a `degraded` flag, failed file fetches are tracked, and both feed `RepoContext.truncated` and add an explicit note to the context text.

## Project layout

| File | Purpose |
|---|---|
| `fetch_real_pr_diff.py` | GitHub API calls: list PRs, fetch a diff/file/PR metadata, code search, post a comment |
| `code_graph.py` | Tree-sitter parsing of one Python file's text: definitions, call sites, imports |
| `pr_context.py` | Fetches what a diff touches on demand and assembles it into reviewer context |
| `queries/python.scm` | Tree-sitter query used by `code_graph.py` |
| `jira_ticket.py` | Ticket-key extraction, Jira fetch, ADF → plain text flattening |
| `review_prompt.py` | Assembles the combined (or code-only) reviewer prompt |
| `review_result.py` | Pydantic models for the structured review output + tolerant parsing |
| `review_real_pr.py` | Runs the review call, formats the PR comment, decides whether to post |
| `main.py` | FastAPI app exposing `/review` (manual) and `/webhook` (real GitHub events) |
| `review_diff_local.py` | Minimal standalone demo against a hardcoded fake diff |
| `eval/` | Golden-set evals: caller/subclass precision-recall, and Jira verdict accuracy |
| `tests/` | Offline unit tests for parsing, prompt assembly, and result validation (no network) |

## Setup

```bash
python -m venv pr-bot
pr-bot\Scripts\activate       # Windows
pip install -r requirements.txt
cp .env.example .env          # then fill in the values below
```

Environment variables (see `.env.example`):

- `DEEPSEEK_API_KEY` — DeepSeek API key
- `GITHUB_TOKEN` — GitHub personal access token with `repo` scope (read diffs, post comments)
- `TARGET_OWNER` / `TARGET_REPO` — the repo the bot reviews
- `POST_COMMENTS` — must be `true` before the bot posts anything; defaults to a dry run
- `GITHUB_WEBHOOK_SECRET` — shared secret used to verify incoming webhook requests

## Running locally

```bash
uvicorn main:app --reload
```

Test the manual endpoint:

```bash
curl -X POST http://127.0.0.1:8000/review \
  -H "Content-Type: application/json" \
  -d '{"owner":"OWNER","repo":"REPO","pr_number":1,"post":false}'
```

## Safety guardrails

- Posting is off by default (`POST_COMMENTS=false`) — every run is a dry run until explicitly enabled.
- `pallets/click` is hard-blocked as a comment target in code, since it's used as a free source of realistic diffs for local testing and should never actually receive a posted comment.
- Incoming webhook requests are verified against `GITHUB_WEBHOOK_SECRET` via HMAC-SHA256 when the secret is configured.

## Deployment

Deploys as a standard ASGI app, e.g. on Render:

- Build command: `pip install -r requirements.txt`
- Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
- Set all `.env` values as environment variables in the host's dashboard (`.env` itself is not committed) — for `GITHUB_APP_PRIVATE_KEY`, paste the `.pem`'s actual multi-line contents directly (most hosts' env editors handle real newlines fine); `GITHUB_APP_PRIVATE_KEY_PATH` only makes sense for a local checkout
- **Python version matters**: `.python-version` pins this repo to 3.11, and it's load-bearing, not cosmetic — `tree_sitter_languages` only ships prebuilt wheels up to roughly 3.12, with no source distribution to fall back to, so a host that defaults to a newer Python (e.g. Render currently defaults to 3.14) fails the build with `No matching distribution found for tree_sitter_languages`. Most hosts read `.python-version` automatically; if yours doesn't, set `PYTHON_VERSION=3.11.9` (or whatever `.python-version` says) directly.

After deploying, register a webhook on the target repo pointed at `https://<your-host>/webhook`, subscribed to `pull_request` events, using the same value as `GITHUB_WEBHOOK_SECRET`.
