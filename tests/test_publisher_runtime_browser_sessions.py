from __future__ import annotations

import json

from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.publisher import PublisherBrowserSessionEntry
from forwin.publisher_runtime.service import PublisherRuntimeService
from forwin.publishers.manager import PublisherManager


QIDIAN_COOKIES = [
    {
        "name": "AppAuthToken",
        "value": "token-secret-value",
        "domain": ".write.qq.com",
        "path": "/",
    },
    {
        "name": "pubtoken",
        "value": "pub-secret-value",
        "domain": ".write.qq.com",
        "path": "/",
    },
]


def _runtime(name: str, *, secret: str = "") -> tuple[object, PublisherRuntimeService]:
    engine = get_engine(postgres_test_url(name))
    init_db(engine)
    return engine, PublisherRuntimeService(
        session_factory=get_session_factory(engine),
        extension_api_key="secret",
        heartbeat_stale_seconds=90,
        preferred_client_id="",
        publisher_session_secret=secret,
        publisher_session_encryption_required=False,
    )


def test_browser_session_service_encrypts_and_redacts_cookie_values() -> None:
    engine, runtime = _runtime("publisher-runtime-browser-secret", secret="session-secret")
    try:
        payload = runtime.browser_sessions.record_browser_session(
            client_id="client-1",
            platform="qidian",
            cookies=QIDIAN_COOKIES,
        )
        assert payload["cookie_count"] == 2

        with runtime.session_factory() as session:
            entry = session.get(
                PublisherBrowserSessionEntry,
                {"client_id": "client-1", "platform_id": "qidian"},
            )
            assert entry is not None
            stored_raw = entry.cookies_json

        assert "token-secret-value" not in stored_raw
        assert json.loads(stored_raw)["encoding"] == "fernet-v1"
        summary = runtime.browser_sessions.get_browser_session_summary("qidian")
        assert summary is not None
        assert summary["cookies_redacted"] is True
        assert summary["cookie_names"] == ["AppAuthToken", "pubtoken"]
        assert "cookies" not in summary
        assert "token-secret-value" not in json.dumps(summary)
        restored = runtime.browser_sessions.get_browser_session("qidian")
        assert restored is not None
        assert restored["cookies"][0]["value"] == "token-secret-value"
    finally:
        engine.dispose()


def test_browser_session_sync_keeps_unverified_cookie_signal_logged_out() -> None:
    engine, runtime = _runtime("publisher-runtime-browser-unverified-cookie")
    try:
        payload = runtime.browser_sessions.record_browser_session(
            client_id="client-1",
            platform="qidian",
            cookies=QIDIAN_COOKIES,
            raw_state={
                "cookie_signal": True,
                "page_evidence_required": True,
                "page_inspected": False,
                "page_authenticated": False,
                "last_error": "",
            },
        )
        assert payload["cookie_count"] == 2

        items = {item["platform_id"]: item for item in runtime.connection_state.list_platforms()}
        assert items["qidian"]["connected"] is False
        assert items["qidian"]["extension_online"] is True
        summary = runtime.browser_sessions.get_browser_session_summary("qidian")
        assert summary is not None
        assert summary["connected"] is False
    finally:
        engine.dispose()


def test_browser_session_plaintext_read_does_not_upgrade_storage() -> None:
    engine, plaintext_runtime = _runtime("publisher-runtime-browser-plaintext")
    try:
        plaintext_runtime.browser_sessions.record_browser_session(
            client_id="client-1",
            platform="qidian",
            cookies=QIDIAN_COOKIES,
        )
        manager = PublisherManager(
            plaintext_runtime.session_factory,
            extension_api_key="secret",
            publisher_session_secret="session-secret",
        )

        try:
            manager.get_browser_session("qidian", upgrade_legacy=True)
        except TypeError:
            pass
        else:
            raise AssertionError("get_browser_session must reject upgrade_legacy")

        restored = manager.get_browser_session("qidian")
        assert restored is not None
        assert restored["cookies"][0]["value"] == "token-secret-value"

        with manager.session_factory() as session:
            entry = session.get(
                PublisherBrowserSessionEntry,
                {"client_id": "client-1", "platform_id": "qidian"},
            )
            assert entry is not None
            stored_raw = entry.cookies_json
        assert "token-secret-value" in stored_raw
        assert isinstance(json.loads(stored_raw), list)
    finally:
        engine.dispose()
