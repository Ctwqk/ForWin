from __future__ import annotations

from datetime import datetime, timedelta, timezone

from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.publisher import PublisherExtensionClient, PublisherExtensionPlatformState
from forwin.publisher_runtime.service import PublisherRuntimeService


def _runtime(
    name: str,
    *,
    preferred_client_id: str = "",
    strict_preferred_client: bool = False,
) -> tuple[object, PublisherRuntimeService]:
    engine = get_engine(postgres_test_url(name))
    init_db(engine)
    return engine, PublisherRuntimeService(
        session_factory=get_session_factory(engine),
        extension_api_key="secret",
        heartbeat_stale_seconds=90,
        preferred_client_id=preferred_client_id,
        strict_preferred_client=strict_preferred_client,
        publisher_session_secret="",
        publisher_session_encryption_required=False,
    )


def test_connection_state_lists_platforms_and_does_not_trust_connected_without_cookie_signal() -> None:
    engine, runtime = _runtime("publisher-runtime-connection")
    try:
        runtime.connection_state.heartbeat(
            client_id="client-1",
            extension_version="0.1.0",
            browser_name="Chrome",
            browser_version="123.0",
            backend_base_url="http://127.0.0.1:8899",
            platforms=[
                {
                    "platform": "qidian",
                    "connected": True,
                    "cookie_signal": False,
                    "login_method": "scan",
                    "last_error": "",
                }
            ],
        )
        items = {item["platform_id"]: item for item in runtime.connection_state.list_platforms()}

        assert items["qidian"]["connected"] is False
        assert items["qidian"]["extension_online"] is True
        assert items["qidian"]["extension_client_id"] == "client-1"
        assert items["qidian"]["supported_login_methods"] == ["scan"]
    finally:
        engine.dispose()


def test_connection_state_login_page_evidence_overrides_cookie_signal() -> None:
    engine, runtime = _runtime("publisher-runtime-connection-login-page")
    try:
        runtime.connection_state.heartbeat(
            client_id="client-1",
            extension_version="0.1.0",
            browser_name="Chrome",
            browser_version="123.0",
            backend_base_url="http://127.0.0.1:8899",
            platforms=[
                {
                    "platform": "qidian",
                    "connected": False,
                    "cookie_signal": True,
                    "page_login_visible": True,
                    "current_url": "https://write.qq.com/portal/login",
                    "login_method": "scan",
                    "last_error": "login-required",
                }
            ],
        )
        items = {item["platform_id"]: item for item in runtime.connection_state.list_platforms()}

        assert items["qidian"]["connected"] is False
        assert items["qidian"]["extension_online"] is True
        assert items["qidian"]["last_error"] == "login-required"
    finally:
        engine.dispose()


def test_connection_state_unverified_cookie_signal_does_not_display_logged_in() -> None:
    engine, runtime = _runtime("publisher-runtime-connection-cookie-unverified")
    try:
        runtime.connection_state.heartbeat(
            client_id="client-1",
            extension_version="0.1.0",
            browser_name="Chrome",
            browser_version="123.0",
            backend_base_url="http://127.0.0.1:8899",
            platforms=[
                {
                    "platform": "qidian",
                    "connected": False,
                    "cookie_signal": True,
                    "page_evidence_required": True,
                    "page_inspected": False,
                    "page_authenticated": False,
                    "login_method": "scan",
                    "last_error": "",
                }
            ],
        )
        items = {item["platform_id"]: item for item in runtime.connection_state.list_platforms()}

        assert items["qidian"]["connected"] is False
        assert items["qidian"]["extension_online"] is True
        assert items["qidian"]["last_error"] == ""
    finally:
        engine.dispose()


def test_connection_state_uses_latest_connected_client_when_preferred_is_stale() -> None:
    engine, runtime = _runtime(
        "publisher-runtime-connection-preferred",
        preferred_client_id="linux-client",
    )
    try:
        runtime.connection_state.heartbeat(
            client_id="linux-client",
            extension_version="0.1.0",
            browser_name="Chrome",
            browser_version="123.0",
            backend_base_url="http://10.0.0.150:8899",
            platforms=[{"platform": "fanqie", "connected": True, "cookie_signal": True}],
        )
        runtime.connection_state.heartbeat(
            client_id="laptop-client",
            extension_version="0.1.0",
            browser_name="Chrome",
            browser_version="123.0",
            backend_base_url="http://10.0.0.35:8899",
            platforms=[{"platform": "fanqie", "connected": True, "cookie_signal": True}],
        )
        with runtime.session_factory() as session:
            stale_at = datetime.now(timezone.utc) - timedelta(minutes=10)
            client = session.get(PublisherExtensionClient, "linux-client")
            state = session.get(
                PublisherExtensionPlatformState,
                {"client_id": "linux-client", "platform_id": "fanqie"},
            )
            assert client is not None
            assert state is not None
            client.last_heartbeat_at = stale_at
            state.last_heartbeat_at = stale_at
            session.commit()

        items = {item["platform_id"]: item for item in runtime.connection_state.list_platforms()}
        assert items["fanqie"]["connected"] is True
        assert items["fanqie"]["extension_online"] is True
        assert items["fanqie"]["extension_client_id"] == "laptop-client"
        assert items["fanqie"]["preferred_client_state"]["client_id"] == "linux-client"
        assert items["fanqie"]["preferred_client_state"]["recent"] is False
        assert items["fanqie"]["latest_client_state"]["client_id"] == "laptop-client"
        assert items["fanqie"]["latest_client_state"]["connected"] is True
        assert items["fanqie"]["fallback_available"] is True
        assert items["fanqie"]["fallback_client_id"] == "laptop-client"
    finally:
        engine.dispose()


def test_connection_state_uses_latest_connected_client_when_preferred_is_login_required() -> None:
    engine, runtime = _runtime(
        "publisher-runtime-connection-preferred-login-required",
        preferred_client_id="linux-client",
    )
    try:
        runtime.connection_state.heartbeat(
            client_id="linux-client",
            extension_version="0.1.0",
            browser_name="Chrome",
            browser_version="123.0",
            backend_base_url="http://10.0.0.150:8899",
            platforms=[
                {
                    "platform": "qidian",
                    "connected": False,
                    "cookie_signal": False,
                    "last_error": "login-required",
                }
            ],
        )
        runtime.connection_state.heartbeat(
            client_id="laptop-client",
            extension_version="0.1.0",
            browser_name="Chrome",
            browser_version="123.0",
            backend_base_url="http://10.0.0.35:8899",
            platforms=[
                {
                    "platform": "qidian",
                    "connected": True,
                    "cookie_signal": True,
                    "page_authenticated": True,
                    "last_error": "",
                }
            ],
        )

        items = {item["platform_id"]: item for item in runtime.connection_state.list_platforms()}
        assert items["qidian"]["connected"] is True
        assert items["qidian"]["extension_client_id"] == "laptop-client"
        assert items["qidian"]["last_error"] == ""
        assert items["qidian"]["preferred_client_state"]["client_id"] == "linux-client"
        assert items["qidian"]["preferred_client_state"]["connected"] is False
        assert items["qidian"]["preferred_client_state"]["recent"] is True
        assert items["qidian"]["latest_client_state"]["client_id"] == "laptop-client"
        assert items["qidian"]["latest_client_state"]["connected"] is True
        assert items["qidian"]["fallback_available"] is True
        assert items["qidian"]["fallback_client_id"] == "laptop-client"
    finally:
        engine.dispose()


def test_connection_state_strict_preferred_client_blocks_fallback_claims() -> None:
    engine, runtime = _runtime(
        "publisher-runtime-connection-strict-preferred",
        preferred_client_id="linux-client",
        strict_preferred_client=True,
    )
    try:
        runtime.connection_state.heartbeat(
            client_id="linux-client",
            extension_version="0.1.0",
            browser_name="Chrome",
            browser_version="123.0",
            backend_base_url="http://10.0.0.150:8899",
            platforms=[{"platform": "fanqie", "connected": False, "cookie_signal": False}],
        )
        runtime.connection_state.heartbeat(
            client_id="laptop-client",
            extension_version="0.1.0",
            browser_name="Chrome",
            browser_version="123.0",
            backend_base_url="http://10.0.0.35:8899",
            platforms=[{"platform": "fanqie", "connected": True, "cookie_signal": True}],
        )

        with runtime.session_factory() as session:
            assert runtime.connection_state.claimable_platforms(
                session,
                client_id="laptop-client",
                platforms=["fanqie"],
            ) == []
            assert runtime.connection_state.claimable_platforms(
                session,
                client_id="linux-client",
                platforms=["fanqie"],
            ) == ["fanqie"]
    finally:
        engine.dispose()


def test_connection_state_strict_preferred_client_blocks_fallback_claims() -> None:
    engine, runtime = _runtime(
        "publisher-runtime-connection-strict-preferred",
        preferred_client_id="linux-client",
        strict_preferred_client=True,
    )
    try:
        runtime.connection_state.heartbeat(
            client_id="linux-client",
            extension_version="0.1.0",
            browser_name="Chrome",
            browser_version="123.0",
            backend_base_url="http://10.0.0.150:8899",
            platforms=[{"platform": "fanqie", "connected": False, "cookie_signal": False}],
        )
        runtime.connection_state.heartbeat(
            client_id="laptop-client",
            extension_version="0.1.0",
            browser_name="Chrome",
            browser_version="123.0",
            backend_base_url="http://10.0.0.35:8899",
            platforms=[{"platform": "fanqie", "connected": True, "cookie_signal": True}],
        )

        with runtime.session_factory() as session:
            assert runtime.connection_state.claimable_platforms(
                session,
                client_id="laptop-client",
                platforms=["fanqie"],
            ) == []
            assert runtime.connection_state.claimable_platforms(
                session,
                client_id="linux-client",
                platforms=["fanqie"],
            ) == ["fanqie"]
    finally:
        engine.dispose()
