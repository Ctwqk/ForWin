from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from forwin.api_publisher_routes import build_handlers
from forwin.api_schemas import (
    ExtensionHeartbeatRequest,
    ExtensionLoginQrNotifyRequest,
    PublisherLoginQrOneShotRequest,
)


class _FakePublisherManager:
    def __init__(self) -> None:
        self.checked_keys: list[str | None] = []
        self.heartbeat_payloads: list[dict[str, object]] = []
        self.login_qr_payloads: list[dict[str, str]] = []
        self.one_shot_payloads: list[dict[str, object]] = []

    def verify_extension_api_key(self, value: str | None) -> None:
        self.checked_keys.append(value)
        if value != "secret":
            from forwin.publishers.manager import PublisherExtensionAuthError

            raise PublisherExtensionAuthError("bad key")

    def preferred_client_heartbeat(self, **_kwargs):
        return {"ok": True, "client_id": "client-1"}

    def get_browser_session(self, platform: str):
        return {
            "platform": platform,
            "client_id": "client-1",
            "cookie_count": 0,
            "cookies": [],
            "synced_at": "",
            "last_error": "",
        }

    def record_extension_heartbeat(self, **kwargs):
        self.heartbeat_payloads.append(kwargs)
        return {
            "ok": True,
            "message": "recorded",
            "server_time": "2026-06-28T12:00:00Z",
        }

    def notify_login_qr(self, **kwargs):
        self.login_qr_payloads.append(kwargs)
        return {
            "ok": True,
            "message": "sent",
            "server_time": "2026-06-28T12:00:00Z",
            "dispatched": True,
        }

    def start_login_qr_one_shot(self, **kwargs):
        self.one_shot_payloads.append(kwargs)
        return {
            "ok": True,
            "message": "one-shot ready",
            "server_time": "2026-06-28T12:00:00Z",
            "platform": kwargs["platform"],
            "expires_at": "2026-06-28T12:05:00Z",
            "allowed_until_ms": 1782648300000,
            "remaining_dispatches": kwargs["max_dispatches"],
            "login_qr_notifications_allowed": True,
        }


def test_extension_heartbeat_status_requires_extension_key() -> None:
    manager = _FakePublisherManager()
    handlers = build_handlers(
        get_publisher_manager=lambda: manager,
        extension_root=Path("browser_extension/forwin-publisher"),
    )

    with pytest.raises(HTTPException) as exc:
        handlers["publisher_extension_heartbeat_status"](x_forwin_extension_key=None)

    assert exc.value.status_code == 401
    assert manager.checked_keys == [None]
    assert handlers["publisher_extension_heartbeat_status"](
        x_forwin_extension_key="secret"
    ) == {"ok": True, "client_id": "client-1"}


def test_extension_browser_session_does_not_request_plaintext_upgrade() -> None:
    manager = _FakePublisherManager()
    handlers = build_handlers(
        get_publisher_manager=lambda: manager,
        extension_root=Path("browser_extension/forwin-publisher"),
    )

    response = handlers["publisher_extension_get_browser_session"](
        "qidian",
        x_forwin_extension_key="secret",
    )

    assert response is not None
    assert response.platform == "qidian"
    assert manager.checked_keys == ["secret"]


def test_extension_heartbeat_preserves_flat_platform_evidence_fields() -> None:
    manager = _FakePublisherManager()
    handlers = build_handlers(
        get_publisher_manager=lambda: manager,
        extension_root=Path("browser_extension/forwin-publisher"),
    )
    req = ExtensionHeartbeatRequest(
        client_id="client-1",
        extension_version="0.1.0",
        browser_name="Chrome",
        browser_version="123.0",
        backend_base_url="http://forwin-app-swarm:8899",
        platforms=[
            {
                "platform": "qidian",
                "connected": True,
                "login_method": "scan",
                "last_error": "",
                "cookie_signal": True,
                "page_authenticated": True,
                "page_login_visible": False,
                "current_url": "https://write.qq.com/portal/dashboard",
            }
        ],
    )

    response = handlers["publisher_extension_heartbeat"](
        req,
        x_forwin_extension_key="secret",
    )

    assert response.ok is True
    forwarded = manager.heartbeat_payloads[0]["platforms"][0]
    assert forwarded["cookie_signal"] is True
    assert forwarded["page_authenticated"] is True
    assert forwarded["current_url"] == "https://write.qq.com/portal/dashboard"


def test_extension_login_qr_notify_requires_extension_key_and_forwards_payload() -> None:
    manager = _FakePublisherManager()
    handlers = build_handlers(
        get_publisher_manager=lambda: manager,
        extension_root=Path("browser_extension/forwin-publisher"),
    )
    req = ExtensionLoginQrNotifyRequest(
        client_id="client-1",
        platform="fanqie",
        current_url="https://fanqienovel.com/main/writer/",
        image_data_url="data:image/png;base64,cXI=",
        source="canvas",
        captured_at="2026-06-28T12:00:00Z",
    )

    with pytest.raises(HTTPException) as exc:
        handlers["publisher_extension_login_qr_notify"](
            req,
            x_forwin_extension_key=None,
        )

    assert exc.value.status_code == 401
    response = handlers["publisher_extension_login_qr_notify"](
        req,
        x_forwin_extension_key="secret",
    )

    assert response.ok is True
    assert response.dispatched is True
    assert manager.checked_keys == [None, "secret"]
    assert manager.login_qr_payloads == [
        {
            "client_id": "client-1",
            "platform": "fanqie",
            "current_url": "https://fanqienovel.com/main/writer/",
            "image_data_url": "data:image/png;base64,cXI=",
            "source": "canvas",
            "captured_at": "2026-06-28T12:00:00Z",
        }
    ]


def test_publisher_login_qr_one_shot_start_forwards_operator_request() -> None:
    manager = _FakePublisherManager()
    handlers = build_handlers(
        get_publisher_manager=lambda: manager,
        extension_root=Path("browser_extension/forwin-publisher"),
    )
    req = PublisherLoginQrOneShotRequest(
        platform="qidian",
        webhook_url="https://discord.invalid/api/webhooks/one-shot",
        ttl_seconds=240,
        max_dispatches=1,
    )

    response = handlers["start_publisher_login_qr_one_shot"](req)

    assert response.ok is True
    assert response.platform == "qidian"
    assert response.login_qr_notifications_allowed is True
    assert manager.one_shot_payloads == [
        {
            "platform": "qidian",
            "webhook_url": "https://discord.invalid/api/webhooks/one-shot",
            "ttl_seconds": 240,
            "max_dispatches": 1,
        }
    ]
