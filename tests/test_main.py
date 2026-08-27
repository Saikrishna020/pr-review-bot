"""Unit tests for main.py's debug endpoints — GitHub's API is mocked, so
these run offline like the rest of tests/.
"""

import httpx
from fastapi.testclient import TestClient

import main

client = TestClient(main.app)


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._payload


def test_rate_limit_endpoint_returns_core_and_search(monkeypatch):
    fake_payload = {
        "resources": {
            "core": {"limit": 5000, "used": 12, "remaining": 4988, "reset": 1700000000},
            "search": {"limit": 30, "used": 5, "remaining": 25, "reset": 1700000060},
            "graphql": {"limit": 5000, "used": 0, "remaining": 5000, "reset": 1700000000},
        }
    }
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResponse(fake_payload))

    response = client.get("/debug/rate-limit")
    assert response.status_code == 200

    data = response.json()
    assert data["core"] == fake_payload["resources"]["core"]
    assert data["search"] == fake_payload["resources"]["search"]
    # Only the two quota blocks that matter for this bot — graphql isn't relevant here.
    assert "graphql" not in data


def test_rate_limit_endpoint_never_echoes_the_token(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_should_never_appear_in_response")
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResponse({"resources": {"core": {}, "search": {}}}))

    response = client.get("/debug/rate-limit")
    assert "ghp_should_never_appear_in_response" not in response.text


def test_rate_limit_endpoint_handles_missing_resource_blocks_gracefully(monkeypatch):
    # A malformed or unexpected GitHub response shouldn't 500 the endpoint.
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResponse({}))

    response = client.get("/debug/rate-limit")
    assert response.status_code == 200
    assert response.json()["core"] == {}
    assert response.json()["search"] == {}
