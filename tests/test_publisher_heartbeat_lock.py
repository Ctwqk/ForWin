from __future__ import annotations

from tempfile import TemporaryDirectory
from unittest.mock import patch

from sqlalchemy.exc import OperationalError

from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.publishers.manager import PublisherManager


def test_extension_heartbeat_returns_retryable_payload_on_postgres_lock_timeout() -> None:
    with TemporaryDirectory() as tmp:
        engine = get_engine(postgres_test_url("publisher-lock"))
        init_db(engine)
        manager = PublisherManager(get_session_factory(engine), extension_api_key="secret")
        locked = OperationalError(
            "UPDATE publisher_extension_clients",
            {},
            Exception("canceling statement due to lock timeout"),
        )
        try:
            with patch.object(manager, "_ensure_extension_client", side_effect=locked):
                payload = manager.record_extension_heartbeat(
                    client_id="client-1",
                    extension_version="0.1.0",
                    browser_name="Chrome",
                    browser_version="123.0",
                    backend_base_url="http://127.0.0.1:8899",
                    platforms=[],
                )
        finally:
            engine.dispose()

    assert payload["ok"] is False
    assert payload["retryable"] is True
    assert "数据库忙" in payload["message"]
    assert payload["server_time"]


def test_browser_session_sync_returns_retryable_payload_on_postgres_lock_timeout() -> None:
    with TemporaryDirectory() as tmp:
        engine = get_engine(postgres_test_url("publisher-session-lock"))
        init_db(engine)
        manager = PublisherManager(get_session_factory(engine), extension_api_key="secret")
        locked = OperationalError(
            "UPDATE publisher_browser_session_entries",
            {},
            Exception("canceling statement due to lock timeout"),
        )
        try:
            with patch.object(manager, "_ensure_extension_client", side_effect=locked):
                payload = manager.record_browser_session(
                    client_id="client-1",
                    platform="qidian",
                    cookies=[{"name": "session", "value": "secret"}],
                )
        finally:
            engine.dispose()

    assert payload["ok"] is False
    assert payload["retryable"] is True
    assert "数据库忙" in payload["message"]
    assert payload["server_time"]
