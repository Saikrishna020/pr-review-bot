"""Authenticate as a GitHub App, so reviews are posted by a distinct bot
identity (`your-app-name[bot]`) instead of a human's account.

Posting with a personal access token makes every review comment look like the
repo owner talking to themselves, which undermines the whole point of showing
an automated reviewer. A GitHub App gets its own author identity, its own
permissions, and its own rate limit.

Auth is two-legged, per GitHub's design:
  1. Sign a short-lived JWT with the App's private key (proves "I am this app").
  2. Exchange that JWT for an *installation* access token, which is what
     actually authorises calls against a repo the app is installed on.

Installation tokens last an hour, so they're cached and reused rather than
minted per API call — the context pipeline makes dozens of calls per review.

Every path here returns None when the app isn't configured, so the bot falls
back to the personal access token and keeps working. Nothing about App auth is
required to run this project.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import jwt
from dotenv import load_dotenv

# See fetch_real_pr_diff.py for why both paths are loaded — Render's Secret
# Files feature mounts at /etc/secrets/, which load_dotenv() doesn't check by
# default, and it's the natural place to put GITHUB_APP_PRIVATE_KEY (a
# multi-line PEM) since it needs no escaping there.
load_dotenv()
load_dotenv("/etc/secrets/.env", override=False)

log = logging.getLogger("uvicorn.error")

GITHUB_API = "https://api.github.com"

APP_ID_NAMES = ("GITHUB_APP_ID",)
INSTALLATION_ID_NAMES = ("GITHUB_APP_INSTALLATION_ID",)
PRIVATE_KEY_NAMES = ("GITHUB_APP_PRIVATE_KEY",)
PRIVATE_KEY_PATH_NAMES = ("GITHUB_APP_PRIVATE_KEY_PATH",)

# GitHub rejects app JWTs with an `exp` more than 10 minutes out. Nine minutes
# leaves room for clock skew between here and GitHub without being rejected.
_JWT_LIFETIME_SECONDS = 9 * 60
_JWT_BACKDATE_SECONDS = 60  # tolerate this machine's clock running slightly fast

# Refresh an installation token this long before it actually expires, so a
# review in flight never has one expire mid-run.
_TOKEN_REFRESH_MARGIN_SECONDS = 5 * 60


@dataclass
class _CachedToken:
    token: str
    expires_at: float  # unix seconds


_cached_token: _CachedToken | None = None
_token_lock = threading.Lock()


def _env_lookup(names: tuple[str, ...]) -> str | None:
    for name, value in os.environ.items():
        if name.upper().replace("-", "_") in names and value.strip():
            return value.strip()
    return None


def _load_private_key() -> str | None:
    """The App's PEM private key, from a file path or inline in the env.

    Both forms exist because they suit different places: a downloaded .pem
    file is natural locally, while hosts like Render only take single-line
    env values, so an inline key arrives with literal backslash-n escapes
    that have to be turned back into real newlines or the PEM won't parse.
    """
    key_path = _env_lookup(PRIVATE_KEY_PATH_NAMES)
    if key_path:
        path = Path(key_path).expanduser()
        if path.is_file():
            return path.read_text(encoding="utf-8")
        log.warning("GITHUB_APP_PRIVATE_KEY_PATH points at %s, which does not exist", path)
        return None

    inline = _env_lookup(PRIVATE_KEY_NAMES)
    if inline:
        return inline.replace("\\n", "\n")
    return None


def get_app_config() -> tuple[str, str, str] | None:
    """Returns (app_id, installation_id, private_key_pem), or None if the App
    isn't fully configured. Partial configuration is reported so a half-done
    setup doesn't silently fall back to the PAT and look like it worked.
    """
    app_id = _env_lookup(APP_ID_NAMES)
    installation_id = _env_lookup(INSTALLATION_ID_NAMES)
    private_key = _load_private_key()

    if app_id and installation_id and private_key:
        return app_id, installation_id, private_key

    if any((app_id, installation_id, private_key)):
        missing = [
            name for name, value in (
                ("GITHUB_APP_ID", app_id),
                ("GITHUB_APP_INSTALLATION_ID", installation_id),
                ("GITHUB_APP_PRIVATE_KEY(_PATH)", private_key),
            ) if not value
        ]
        log.warning(
            "GitHub App partially configured (missing: %s) — falling back to personal access token",
            ", ".join(missing),
        )
    return None


def build_app_jwt(app_id: str, private_key_pem: str) -> str:
    """Short-lived JWT proving control of the App's private key."""
    now = int(time.time())
    payload = {
        "iat": now - _JWT_BACKDATE_SECONDS,
        "exp": now + _JWT_LIFETIME_SECONDS,
        "iss": app_id,
    }
    return jwt.encode(payload, private_key_pem, algorithm="RS256")


def _mint_installation_token(app_id: str, installation_id: str, private_key_pem: str) -> _CachedToken | None:
    try:
        app_jwt = build_app_jwt(app_id, private_key_pem)
    except Exception:
        log.warning("Could not sign GitHub App JWT (bad private key?) — falling back to PAT", exc_info=True)
        return None

    try:
        response = httpx.post(
            f"{GITHUB_API}/app/installations/{installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30,
        )
        response.raise_for_status()
    except httpx.HTTPError:
        log.warning("GitHub App token exchange failed — falling back to PAT", exc_info=True)
        return None

    data = response.json()
    token = data.get("token")
    if not token:
        log.warning("GitHub App token exchange returned no token — falling back to PAT")
        return None

    # Installation tokens are valid for an hour; trust our own clock rather
    # than parsing expires_at, so skew can't make us keep a dead token.
    return _CachedToken(token=token, expires_at=time.time() + 3600)


def get_installation_token() -> str | None:
    """A cached installation access token, or None if App auth isn't usable.

    Thread-safe: the context pipeline calls this from a worker pool, and
    minting a token per thread would waste calls and race the cache.
    """
    config = get_app_config()
    if config is None:
        return None

    app_id, installation_id, private_key = config

    with _token_lock:
        global _cached_token
        now = time.time()
        if _cached_token and _cached_token.expires_at - _TOKEN_REFRESH_MARGIN_SECONDS > now:
            return _cached_token.token

        minted = _mint_installation_token(app_id, installation_id, private_key)
        if minted is None:
            return None
        _cached_token = minted
        return minted.token


def reset_token_cache() -> None:
    """Drops the cached token. For tests, and for forcing a refresh."""
    global _cached_token
    with _token_lock:
        _cached_token = None


def discover_installation_id(owner: str, repo: str) -> str | None:
    """Looks up the installation id for a repo, so it doesn't have to be found
    by hand in the GitHub UI. Needs GITHUB_APP_ID and the private key set;
    the installation id itself is what this returns.
    """
    app_id = _env_lookup(APP_ID_NAMES)
    private_key = _load_private_key()
    if not (app_id and private_key):
        log.warning("Need GITHUB_APP_ID and a private key to discover the installation id")
        return None

    try:
        app_jwt = build_app_jwt(app_id, private_key)
        response = httpx.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/installation",
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30,
        )
        response.raise_for_status()
    except Exception:
        log.warning("Could not discover installation id for %s/%s", owner, repo, exc_info=True)
        return None

    return str(response.json().get("id"))


def main() -> None:
    """CLI helper: verify App auth and print the installation id for a repo.

    Usage: python github_app.py [owner/repo]
    """
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else None

    app_id = _env_lookup(APP_ID_NAMES)
    key = _load_private_key()
    print(f"GITHUB_APP_ID           : {app_id or 'MISSING'}")
    print(f"private key             : {'found' if key else 'MISSING'}")
    print(f"GITHUB_APP_INSTALLATION_ID: {_env_lookup(INSTALLATION_ID_NAMES) or 'not set'}")

    if target and app_id and key:
        owner, _, repo = target.partition("/")
        installation_id = discover_installation_id(owner, repo)
        print(f"\ninstallation id for {target}: {installation_id or 'NOT FOUND (is the app installed on it?)'}")
        if installation_id:
            print(f"\nAdd this to .env:\n  GITHUB_APP_INSTALLATION_ID={installation_id}")

    token = get_installation_token()
    print(f"\ninstallation token      : {'obtained OK' if token else 'unavailable (will fall back to PAT)'}")
    if token:
        who = httpx.get(
            f"{GITHUB_API}/app",
            headers={"Authorization": f"Bearer {build_app_jwt(app_id, key)}",
                     "Accept": "application/vnd.github+json"},
            timeout=30,
        )
        if who.status_code == 200:
            data = who.json()
            print(f"authenticated as app    : {data.get('name')} (slug: {data.get('slug')})")
            print(f"comments will appear as : {data.get('slug')}[bot]")


if __name__ == "__main__":
    main()
