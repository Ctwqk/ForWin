from __future__ import annotations

from unittest.mock import patch

from sqlalchemy.exc import OperationalError

from forwin.publishers.manager import PublisherManager


class _DummySession:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


def test_extension_heartbeat_returns_retryable_payload_when_database_is_busy() -> None:
    manager = PublisherManager(lambda: _DummySession(), extension_api_key="secret")
    locked = OperationalError(
        "UPDATE publisher_extension_clients",
        {},
        Exception("deadlock detected"),
    )
    with patch.object(manager, "_ensure_extension_client", side_effect=locked):
        payload = manager.record_extension_heartbeat(
            client_id="client-1",
            extension_version="0.1.0",
            browser_name="Chrome",
            browser_version="123.0",
            backend_base_url="http://127.0.0.1:8899",
            platforms=[],
        )

    assert payload["ok"] is False
    assert payload["retryable"] is True
    assert "数据库忙" in payload["message"]
    assert payload["server_time"]


def test_browser_session_sync_returns_retryable_payload_when_database_is_busy() -> None:
    manager = PublisherManager(lambda: _DummySession(), extension_api_key="secret")
    locked = OperationalError(
        "UPDATE publisher_browser_session_entries",
        {},
        Exception("deadlock detected"),
    )
    with patch.object(manager, "_ensure_extension_client", side_effect=locked):
        payload = manager.record_browser_session(
            client_id="client-1",
            platform="qidian",
            cookies=[{"name": "session", "value": "secret"}],
        )

    assert payload["ok"] is False
    assert payload["retryable"] is True
    assert "数据库忙" in payload["message"]
    assert payload["server_time"]
