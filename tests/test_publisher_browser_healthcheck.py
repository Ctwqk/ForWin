from __future__ import annotations

import json

import httpx

from forwin.publishers.healthcheck import (
    get_preferred_client_heartbeat,
    resolve_target_client_id,
)


class _FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload


def test_resolve_target_client_id_falls_back_to_profile_marker(tmp_path):
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    (profile_dir / ".forwin-extension-profile.json").write_text(
        json.dumps({"clientId": "marker-client"}, ensure_ascii=False),
        encoding="utf-8",
    )

    assert resolve_target_client_id("", profile_dir=profile_dir) == "marker-client"


def test_get_preferred_client_heartbeat_calls_backend_api(monkeypatch):
    calls = []

    def fake_get(url, *, params, timeout):  # noqa: ANN001
        calls.append((url, params, timeout))
        return _FakeResponse(
            {
                "ok": True,
                "client_id": "preferred-client",
                "backend_base_url": "http://forwin:8899",
                "last_heartbeat_at": "2026-04-27T12:00:00",
                "recent_platforms": ["fanqie"],
                "message": "preferred publisher client is heartbeating",
            }
        )

    monkeypatch.setattr(httpx, "get", fake_get)

    result = get_preferred_client_heartbeat(
        "http://backend:8899/",
        preferred_client_id="preferred-client",
        stale_seconds=90,
    )

    assert result.ok is True
    assert result.client_id == "preferred-client"
    assert result.backend_base_url == "http://forwin:8899"
    assert result.recent_platforms == ("fanqie",)
    assert calls == [
        (
            "http://backend:8899/api/publishers/extension/heartbeat-status",
            {
                "client_id": "preferred-client",
                "stale_seconds": 90,
                "allow_latest_recent_fallback": False,
            },
            10.0,
        )
    ]


def test_get_preferred_client_heartbeat_returns_api_failure(monkeypatch):
    def fake_get(url, *, params, timeout):  # noqa: ANN001
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(httpx, "get", fake_get)

    result = get_preferred_client_heartbeat(
        "http://backend:8899",
        preferred_client_id="preferred-client",
        stale_seconds=90,
    )

    assert result.ok is False
    assert result.client_id == "preferred-client"
    assert "publisher heartbeat API check failed" in result.message


def test_get_preferred_client_heartbeat_allows_latest_recent_fallback(monkeypatch):
    calls = []

    def fake_get(url, *, params, timeout):  # noqa: ANN001
        calls.append(params)
        return _FakeResponse(
            {
                "ok": False,
                "client_id": "",
                "message": "preferred publisher client id is empty",
                "latest_recent_client_id": "recent-client",
                "latest_recent_backend_base_url": "http://forwin:8899",
                "latest_recent_heartbeat_at": "2026-04-27T12:00:00",
            }
        )

    monkeypatch.setattr(httpx, "get", fake_get)

    result = get_preferred_client_heartbeat(
        "http://backend:8899",
        preferred_client_id="",
        stale_seconds=90,
        allow_latest_recent_fallback=True,
    )

    assert result.ok is False
    assert result.latest_recent_client_id == "recent-client"
    assert calls[0]["allow_latest_recent_fallback"] is True
