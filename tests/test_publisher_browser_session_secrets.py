from __future__ import annotations

import json

from forwin.api_publisher_ops import (
    get_publisher_browser_session_summary,
    publisher_extension_get_browser_session,
)
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.publisher import PublisherBrowserSessionEntry
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


def _manager(name: str, **kwargs) -> tuple[object, PublisherManager]:
    engine = get_engine(postgres_test_url(name))
    init_db(engine)
    return engine, PublisherManager(
        get_session_factory(engine),
        extension_api_key="extension-secret",
        **kwargs,
    )


def test_session_secret_encrypts_new_cookie_storage() -> None:
    engine, manager = _manager(
        "publisher-secret-encrypts",
        publisher_session_secret="session-secret",
    )
    try:
        manager.record_browser_session(
            client_id="client-1",
            platform="qidian",
            cookies=QIDIAN_COOKIES,
        )

        with manager.session_factory() as session:
            entry = session.get(
                PublisherBrowserSessionEntry,
                {"client_id": "client-1", "platform_id": "qidian"},
            )
            assert entry is not None
            stored_raw = entry.cookies_json

        assert "token-secret-value" not in stored_raw
        assert "pub-secret-value" not in stored_raw
        assert json.loads(stored_raw)["encoding"] == "fernet-v1"

        restored = manager.get_browser_session("qidian")
        assert restored is not None
        assert [item["value"] for item in restored["cookies"]] == [
            "token-secret-value",
            "pub-secret-value",
        ]

        summary = get_publisher_browser_session_summary(
            "qidian",
            publisher_manager=manager,
        )
        assert summary is not None
        payload = summary.model_dump()
        assert payload["cookies_redacted"] is True
        assert payload["cookie_count"] == 2
        assert payload["cookie_names"] == ["AppAuthToken", "pubtoken"]
        assert "cookies" not in payload
        assert "token-secret-value" not in json.dumps(payload)
    finally:
        engine.dispose()


def test_plaintext_session_reads_without_upgrade_in_extension_context() -> None:
    engine, plaintext_manager = _manager("publisher-secret-plaintext")
    try:
        plaintext_manager.record_browser_session(
            client_id="client-1",
            platform="qidian",
            cookies=QIDIAN_COOKIES,
        )
        secret_manager = PublisherManager(
            plaintext_manager.session_factory,
            extension_api_key="extension-secret",
            publisher_session_secret="session-secret",
        )

        restored = publisher_extension_get_browser_session(
            "qidian",
            publisher_manager=secret_manager,
            x_forwin_extension_key="extension-secret",
        )
        assert restored is not None
        assert restored.cookies[0].value == "token-secret-value"

        with secret_manager.session_factory() as session:
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


def test_encrypted_session_without_matching_secret_does_not_restore_or_connect() -> None:
    engine, manager = _manager(
        "publisher-secret-wrong",
        publisher_session_secret="session-secret",
    )
    try:
        manager.record_browser_session(
            client_id="client-1",
            platform="qidian",
            cookies=QIDIAN_COOKIES,
        )

        wrong_secret = PublisherManager(
            manager.session_factory,
            extension_api_key="extension-secret",
            publisher_session_secret="wrong-secret",
        )
        restored = wrong_secret.get_browser_session("qidian")
        assert restored is not None
        assert restored["cookies"] == []
        assert wrong_secret.has_browser_session("qidian") is False
        summary = wrong_secret.get_browser_session_summary("qidian")
        assert summary is not None
        assert summary["cookie_names"] == []
        assert summary["connected"] is False

        missing_secret = PublisherManager(
            manager.session_factory,
            extension_api_key="extension-secret",
        )
        restored_missing = missing_secret.get_browser_session("qidian")
        assert restored_missing is not None
        assert restored_missing["cookies"] == []
        assert missing_secret.has_browser_session("qidian") is False
    finally:
        engine.dispose()


def test_extension_route_returns_full_cookies_only_after_key_check() -> None:
    engine, manager = _manager(
        "publisher-secret-extension-full",
        publisher_session_secret="session-secret",
    )
    try:
        manager.record_browser_session(
            client_id="client-1",
            platform="qidian",
            cookies=QIDIAN_COOKIES,
        )

        restored = publisher_extension_get_browser_session(
            "qidian",
            publisher_manager=manager,
            x_forwin_extension_key="extension-secret",
        )

        assert restored is not None
        assert restored.cookies[0].name == "AppAuthToken"
        assert restored.cookies[0].value == "token-secret-value"
    finally:
        engine.dispose()
