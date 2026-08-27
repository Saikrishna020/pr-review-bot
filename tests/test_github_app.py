"""Unit tests for GitHub App auth — no network.

A throwaway RSA key is generated per run, so these exercise real RS256
signing rather than mocking it away.
"""

import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

import github_app
from fetch_real_pr_diff import resolve_auth_token


@pytest.fixture
def rsa_keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private_pem, public_pem


@pytest.fixture(autouse=True)
def clear_app_env(monkeypatch):
    for name in (
        "GITHUB_APP_ID", "GITHUB_APP_INSTALLATION_ID",
        "GITHUB_APP_PRIVATE_KEY", "GITHUB_APP_PRIVATE_KEY_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    github_app.reset_token_cache()
    yield
    github_app.reset_token_cache()


def test_jwt_is_signed_and_carries_the_app_id(rsa_keypair):
    private_pem, public_pem = rsa_keypair
    token = github_app.build_app_jwt("123456", private_pem)

    decoded = jwt.decode(token, public_pem, algorithms=["RS256"], options={"verify_exp": True})
    assert decoded["iss"] == "123456"
    assert decoded["iat"] <= int(time.time())
    # GitHub rejects app JWTs expiring more than 10 minutes out.
    assert decoded["exp"] - int(time.time()) <= 600


def test_config_requires_all_three_parts(monkeypatch, rsa_keypair):
    private_pem, _ = rsa_keypair
    assert github_app.get_app_config() is None

    monkeypatch.setenv("GITHUB_APP_ID", "123456")
    assert github_app.get_app_config() is None  # still missing key + installation

    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", private_pem)
    assert github_app.get_app_config() is None  # still missing installation id

    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "789")
    config = github_app.get_app_config()
    assert config is not None
    assert config[0] == "123456"
    assert config[1] == "789"


def test_inline_private_key_unescapes_newlines(monkeypatch, rsa_keypair):
    # Hosts like Render only accept single-line env values, so the PEM arrives
    # with literal backslash-n. If those aren't restored the key won't parse.
    private_pem, public_pem = rsa_keypair
    monkeypatch.setenv("GITHUB_APP_ID", "1")
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "2")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", private_pem.replace("\n", "\\n"))

    config = github_app.get_app_config()
    assert config is not None
    # The recovered key must actually work for signing.
    decoded = jwt.decode(
        github_app.build_app_jwt("1", config[2]), public_pem, algorithms=["RS256"]
    )
    assert decoded["iss"] == "1"


def test_private_key_can_come_from_a_file(monkeypatch, tmp_path, rsa_keypair):
    private_pem, _ = rsa_keypair
    key_file = tmp_path / "app.pem"
    key_file.write_text(private_pem, encoding="utf-8")

    monkeypatch.setenv("GITHUB_APP_ID", "1")
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "2")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_PATH", str(key_file))

    config = github_app.get_app_config()
    assert config is not None
    assert "BEGIN PRIVATE KEY" in config[2]


def test_missing_key_file_does_not_raise(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_APP_ID", "1")
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "2")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_PATH", str(tmp_path / "nope.pem"))
    assert github_app.get_app_config() is None


def test_unconfigured_app_yields_no_installation_token():
    assert github_app.get_installation_token() is None


def test_auth_falls_back_to_pat_when_app_is_not_configured(monkeypatch):
    # The whole point of the fallback: App auth is optional, and the bot must
    # keep working on a plain PAT.
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake_for_test")
    token, identity = resolve_auth_token()
    assert token == "ghp_fake_for_test"
    assert identity == "personal-access-token"


def test_auth_reports_unauthenticated_when_nothing_is_set(monkeypatch):
    for name in list(__import__("os").environ):
        if name.upper() in ("GITHUB_TOKEN", "GH_TOKEN", "GITHUB_PAT"):
            monkeypatch.delenv(name, raising=False)
    token, identity = resolve_auth_token()
    assert token is None
    assert identity == "unauthenticated"


def test_prefer_app_false_forces_the_pat_even_when_app_is_configured(monkeypatch, rsa_keypair):
    # Regression test: a human-authored comment (post_pr_comment(as_bot=False))
    # must never end up posted under the App identity. It did, once — every
    # post_pr_comment call defaulted to preferring the App as soon as it was
    # configured, including ones meant to represent a person, which made a
    # human reply indistinguishable from the bot replying to itself.
    private_pem, _ = rsa_keypair
    monkeypatch.setenv("GITHUB_APP_ID", "1")
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "2")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", private_pem)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake_for_test")

    # App IS configured here (id/installation/key all set) — the point is
    # that prefer_app=False must still resolve to the PAT, not attempt the
    # App at all. If this fell through to the App instead, this fast offline
    # test would try a real network call and hang or fail on the fake ids.
    token, identity = resolve_auth_token(prefer_app=False)
    assert identity == "personal-access-token"
    assert token == "ghp_fake_for_test"
