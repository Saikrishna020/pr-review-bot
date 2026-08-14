# PR Review Bot

An automated code review bot that watches GitHub pull requests, sends the diff to an LLM (DeepSeek) for review, and posts the findings back as a PR comment — no manual steps once wired up.

## How it works

```
PR opened/updated on target repo
  → GitHub sends a webhook to POST /webhook
  → server acknowledges immediately
  → in the background: fetch diff → DeepSeek review → post comment on the PR
```

There's also a manual trigger, `POST /review`, for testing a specific PR on demand without needing a live webhook.

## Project layout

| File | Purpose |
|---|---|
| `fetch_real_pr_diff.py` | GitHub API calls: list PRs, fetch a diff, post a comment |
| `review_real_pr.py` | Sends a diff to DeepSeek, validates/formats the response, decides whether to post |
| `main.py` | FastAPI app exposing `/review` (manual) and `/webhook` (real GitHub events) |
| `review_diff_local.py` | Minimal standalone demo against a hardcoded fake diff |

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
- Set all `.env` values as environment variables in the host's dashboard (`.env` itself is not committed)

After deploying, register a webhook on the target repo pointed at `https://<your-host>/webhook`, subscribed to `pull_request` events, using the same value as `GITHUB_WEBHOOK_SECRET`.
