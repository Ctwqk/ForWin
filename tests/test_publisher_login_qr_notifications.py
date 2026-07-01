from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from urllib.request import Request

import pytest

import forwin.publishers.manager as publisher_manager_module
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.publishers.manager import PublisherManager
from forwin.publisher_runtime.login_qr_notifications import DiscordLoginQrNotifier


class _FakeResponse:
    status = 204

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def getcode(self) -> int:
        return self.status


def _png_data_url(payload: bytes = b"png-bytes") -> str:
    encoded = base64.b64encode(payload).decode("ascii")
    return f"data:image/png;base64,{encoded}"


QIDIAN_LOGIN_COOKIES = [
    {
        "name": "AppAuthToken",
        "value": "token-secret",
        "domain": ".write.qq.com",
        "path": "/",
    },
    {
        "name": "pubtoken",
        "value": "pub-secret",
        "domain": ".write.qq.com",
        "path": "/",
    },
]


class _FakeLoginQrNotifier:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []
        self.success_calls: list[dict[str, str]] = []

    def notify(self, **kwargs) -> dict[str, object]:
        self.calls.append(kwargs)
        return {
            "ok": True,
            "dispatched": True,
            "message": "sent",
            "server_time": "2026-06-29T21:00:00+00:00",
        }

    def notify_login_success(self, **kwargs) -> dict[str, object]:
        self.success_calls.append(kwargs)
        return {
            "ok": True,
            "dispatched": True,
            "message": "success sent",
            "server_time": "2026-06-29T21:00:00+00:00",
        }


def test_login_qr_notification_skips_when_webhook_is_not_configured() -> None:
    notifier = DiscordLoginQrNotifier("")

    result = notifier.notify(
        client_id="client-1",
        platform="fanqie",
        current_url="https://fanqienovel.com/main/writer/?token=secret",
        image_data_url=_png_data_url(),
        source="canvas",
    )

    assert result["ok"] is True
    assert result["dispatched"] is False
    assert result["disabled"] is True


def test_publisher_manager_throttles_duplicate_login_qr_after_notifier_accepts_without_dispatch() -> None:
    manager = PublisherManager(lambda: None)
    notifier = _FakeLoginQrNotifier()

    def accept_without_dispatch(**kwargs) -> dict[str, object]:
        notifier.calls.append(kwargs)
        return {
            "ok": True,
            "dispatched": False,
            "message": "Discord login QR webhook is not configured.",
            "server_time": "2026-06-29T21:00:00+00:00",
        }

    notifier.notify = accept_without_dispatch  # type: ignore[method-assign]
    manager.login_qr_notifier = notifier

    first = manager.notify_login_qr(
        client_id="client-1",
        platform="fanqie",
        current_url="https://fanqienovel.com/main/writer/login?ticket=secret",
        image_data_url=_png_data_url(),
        source="frame:image",
    )
    second = manager.notify_login_qr(
        client_id="client-1",
        platform="fanqie",
        current_url="https://fanqienovel.com/main/writer/login?ticket=rotated",
        image_data_url=_png_data_url(),
        source="frame:image",
    )

    assert first["ok"] is True
    assert first["dispatched"] is False
    assert second["ok"] is True
    assert second["dispatched"] is False
    assert second["throttled"] is True
    assert len(notifier.calls) == 1


def test_publisher_manager_one_shot_login_qr_uses_temporary_webhook_once() -> None:
    manager = PublisherManager(lambda: None)
    notifier = _FakeLoginQrNotifier()
    manager.login_qr_notifier = notifier

    opened = manager.start_login_qr_one_shot(
        platform="fanqie",
        webhook_url="https://discord.invalid/api/webhooks/one-shot",
        ttl_seconds=300,
        max_dispatches=1,
    )

    assert opened["ok"] is True
    assert opened["platform"] == "fanqie"
    assert opened["remaining_dispatches"] == 1
    assert opened["login_qr_notifications_allowed"] is True

    first = manager.notify_login_qr(
        client_id="client-1",
        platform="fanqie",
        current_url="https://fanqienovel.com/main/writer/login",
        image_data_url=_png_data_url(b"first-qr"),
        source="frame:1:scripting:image",
    )
    second = manager.notify_login_qr(
        client_id="client-1",
        platform="fanqie",
        current_url="https://fanqienovel.com/main/writer/login",
        image_data_url=_png_data_url(b"second-qr"),
        source="frame:1:scripting:image",
    )

    assert first["ok"] is True
    assert first["dispatched"] is True
    assert first["one_shot"] is True
    assert second["ok"] is True
    assert second["dispatched"] is False
    assert second["disabled"] is True
    assert len(notifier.calls) == 1


def test_publisher_manager_one_shot_login_qr_rejects_screenshot_sources() -> None:
    manager = PublisherManager(lambda: None)
    notifier = _FakeLoginQrNotifier()
    manager.login_qr_notifier = notifier
    manager.start_login_qr_one_shot(
        platform="qidian",
        webhook_url="https://discord.invalid/api/webhooks/one-shot",
        ttl_seconds=300,
        max_dispatches=1,
    )

    result = manager.notify_login_qr(
        client_id="client-1",
        platform="qidian",
        current_url="https://write.qq.com/portal/login",
        image_data_url=_png_data_url(b"screenshot"),
        source="debugger-screenshot",
    )

    assert result["ok"] is True
    assert result["dispatched"] is False
    assert result["disabled"] is True
    assert result["message"] == "login QR screenshot capture is not allowed for one-shot delivery."
    assert notifier.calls == []


def test_publisher_manager_throttles_duplicate_login_qr_notifications() -> None:
    manager = PublisherManager(lambda: None)
    notifier = _FakeLoginQrNotifier()
    manager.login_qr_notifier = notifier

    first = manager.notify_login_qr(
        client_id="client-1",
        platform="qidian",
        current_url="https://write.qq.com/portal/login?ticket=secret",
        image_data_url=_png_data_url(),
        source="frame:image",
    )
    second = manager.notify_login_qr(
        client_id="client-1",
        platform="qidian",
        current_url="https://write.qq.com/portal/login?ticket=rotated",
        image_data_url=_png_data_url(),
        source="frame:image",
    )

    assert first["dispatched"] is True
    assert second["ok"] is True
    assert second["dispatched"] is False
    assert second["throttled"] is True
    assert len(notifier.calls) == 1


def test_publisher_manager_allows_changed_login_qr_inside_throttle_window() -> None:
    manager = PublisherManager(lambda: None)
    notifier = _FakeLoginQrNotifier()
    manager.login_qr_notifier = notifier

    first = manager.notify_login_qr(
        client_id="client-1",
        platform="fanqie",
        current_url="https://fanqienovel.com/main/writer/login",
        image_data_url=_png_data_url(b"first-qr"),
        source="image",
    )
    second = manager.notify_login_qr(
        client_id="client-1",
        platform="fanqie",
        current_url="https://fanqienovel.com/main/writer/login",
        image_data_url=_png_data_url(b"fresh-qr"),
        source="image",
    )

    assert first["dispatched"] is True
    assert second["ok"] is True
    assert second["dispatched"] is True
    assert "throttled" not in second
    assert len(notifier.calls) == 2


def test_publisher_manager_allows_fresh_login_qr_after_short_throttle_window(monkeypatch) -> None:
    base_time = datetime(2026, 6, 29, 21, 0, tzinfo=timezone.utc)
    times = iter([base_time, base_time + timedelta(seconds=121)])
    monkeypatch.setattr(publisher_manager_module, "_utc_now", lambda: next(times))

    manager = PublisherManager(lambda: None)
    notifier = _FakeLoginQrNotifier()
    manager.login_qr_notifier = notifier

    first = manager.notify_login_qr(
        client_id="client-1",
        platform="fanqie",
        current_url="https://fanqienovel.com/main/writer/login",
        image_data_url=_png_data_url(b"first-qr"),
        source="image",
    )
    second = manager.notify_login_qr(
        client_id="client-1",
        platform="fanqie",
        current_url="https://fanqienovel.com/main/writer/login",
        image_data_url=_png_data_url(b"fresh-qr"),
        source="image",
    )

    assert first["dispatched"] is True
    assert second["dispatched"] is True
    assert len(notifier.calls) == 2


def test_publisher_manager_allows_same_login_qr_after_short_throttle_window(monkeypatch) -> None:
    base_time = datetime(2026, 6, 29, 21, 0, tzinfo=timezone.utc)
    times = iter([base_time, base_time + timedelta(seconds=121)])
    monkeypatch.setattr(publisher_manager_module, "_utc_now", lambda: next(times))

    manager = PublisherManager(lambda: None)
    notifier = _FakeLoginQrNotifier()
    manager.login_qr_notifier = notifier
    same_image = _png_data_url(b"same-qr")

    first = manager.notify_login_qr(
        client_id="client-1",
        platform="qidian",
        current_url="https://write.qq.com/portal/login",
        image_data_url=same_image,
        source="frame:image",
    )
    second = manager.notify_login_qr(
        client_id="client-1",
        platform="qidian",
        current_url="https://write.qq.com/portal/login",
        image_data_url=same_image,
        source="frame:image",
    )

    assert first["dispatched"] is True
    assert second["ok"] is True
    assert second["dispatched"] is True
    assert len(notifier.calls) == 2


def test_login_qr_notification_posts_multipart_payload() -> None:
    calls: list[Request] = []

    def fake_urlopen(request: Request, *, timeout: float):
        calls.append(request)
        assert timeout == 8.0
        return _FakeResponse()

    notifier = DiscordLoginQrNotifier(
        "https://discord.invalid/api/webhooks/test",
        urlopen_impl=fake_urlopen,
    )

    result = notifier.notify(
        client_id="client-1",
        platform="qidian",
        current_url="https://write.qq.com/login?ticket=secret#frag",
        image_data_url=_png_data_url(b"qr-image"),
        source="canvas",
        captured_at="2026-06-28T12:00:00Z",
    )

    assert result["ok"] is True
    assert result["dispatched"] is True
    assert len(calls) == 1
    request = calls[0]
    assert request.full_url == "https://discord.invalid/api/webhooks/test"
    assert request.get_method() == "POST"
    assert request.headers["Content-type"].startswith("multipart/form-data; boundary=")
    body = request.data or b""
    assert b'Content-Disposition: form-data; name="payload_json"' in body
    assert b'Content-Disposition: form-data; name="files[0]"; filename="qidian-login-qr.png"' in body
    assert b"qr-image" in body
    assert b"ticket=secret" not in body
    payload_start = body.index(b"\r\n\r\n") + 4
    payload_end = body.index(b"\r\n--", payload_start)
    payload = json.loads(body[payload_start:payload_end].decode("utf-8"))
    assert "ForWin publisher login requires scan" in payload["content"]
    assert "https://write.qq.com/login" in payload["content"]


def test_login_success_notification_posts_json_payload() -> None:
    calls: list[Request] = []

    def fake_urlopen(request: Request, *, timeout: float):
        calls.append(request)
        assert timeout == 8.0
        return _FakeResponse()

    notifier = DiscordLoginQrNotifier(
        "https://discord.invalid/api/webhooks/test",
        urlopen_impl=fake_urlopen,
    )

    result = notifier.notify_login_success(
        client_id="client-secret-123456",
        platform="fanqie",
        detected_at="2026-06-30T01:55:00Z",
    )

    assert result["ok"] is True
    assert result["dispatched"] is True
    assert len(calls) == 1
    request = calls[0]
    assert request.full_url == "https://discord.invalid/api/webhooks/test"
    assert request.get_method() == "POST"
    assert request.headers["Content-type"] == "application/json"
    assert request.data is not None
    payload = json.loads(request.data.decode("utf-8"))
    assert "ForWin publisher login confirmed" in payload["content"]
    assert "Platform: fanqie" in payload["content"]
    assert "client-secret" not in payload["content"]
    assert "123456" in payload["content"]
    assert "2026-06-30T01:55:00Z" in payload["content"]


def test_login_qr_notification_rejects_non_image_data_url() -> None:
    notifier = DiscordLoginQrNotifier("https://discord.invalid/api/webhooks/test")

    with pytest.raises(ValueError, match="image data URL"):
        notifier.notify(
            client_id="client-1",
            platform="fanqie",
            current_url="https://fanqienovel.com/main/writer/",
            image_data_url="data:text/plain;base64,Zm9v",
            source="test",
        )


def test_publisher_manager_notifies_login_success_once_from_heartbeat() -> None:
    engine = get_engine(postgres_test_url("publisher-login-success-heartbeat"))
    init_db(engine)
    manager = PublisherManager(get_session_factory(engine))
    notifier = _FakeLoginQrNotifier()
    manager.login_qr_notifier = notifier
    try:
        first = manager.record_extension_heartbeat(
            client_id="client-secret-123456",
            extension_version="0.1.0",
            browser_name="Chrome",
            browser_version="149.0",
            backend_base_url="http://forwin-app:8899",
            platforms=[
                {
                    "platform": "fanqie",
                    "connected": False,
                    "cookie_signal": True,
                    "page_evidence_required": True,
                    "page_authenticated": False,
                    "page_login_visible": True,
                    "login_method": "scan",
                    "last_error": "login-required",
                }
            ],
        )
        second = manager.record_extension_heartbeat(
            client_id="client-secret-123456",
            extension_version="0.1.0",
            browser_name="Chrome",
            browser_version="149.0",
            backend_base_url="http://forwin-app:8899",
            platforms=[
                {
                    "platform": "fanqie",
                    "connected": True,
                    "cookie_signal": True,
                    "page_evidence_required": True,
                    "page_authenticated": True,
                    "page_login_visible": False,
                    "login_method": "scan",
                    "last_error": "",
                }
            ],
        )
        third = manager.record_extension_heartbeat(
            client_id="client-secret-123456",
            extension_version="0.1.0",
            browser_name="Chrome",
            browser_version="149.0",
            backend_base_url="http://forwin-app:8899",
            platforms=[
                {
                    "platform": "fanqie",
                    "connected": True,
                    "cookie_signal": True,
                    "page_evidence_required": True,
                    "page_authenticated": True,
                    "page_login_visible": False,
                    "login_method": "scan",
                    "last_error": "",
                }
            ],
        )

        assert first["login_success_notifications"] == []
        assert second["login_success_notifications"] == ["fanqie"]
        assert third["login_success_notifications"] == []
        assert notifier.success_calls == [
            {
                "client_id": "client-secret-123456",
                "platform": "fanqie",
            }
        ]
    finally:
        engine.dispose()


def test_publisher_manager_notifies_login_success_from_browser_session_sync() -> None:
    engine = get_engine(postgres_test_url("publisher-login-success-browser-session"))
    init_db(engine)
    manager = PublisherManager(get_session_factory(engine))
    notifier = _FakeLoginQrNotifier()
    manager.login_qr_notifier = notifier
    try:
        payload = manager.record_browser_session(
            client_id="client-secret-654321",
            platform="qidian",
            cookies=QIDIAN_LOGIN_COOKIES,
            raw_state={
                "cookie_signal": True,
                "page_evidence_required": True,
                "page_authenticated": True,
                "page_login_visible": False,
                "current_url": "https://write.qq.com/portal/dashboard",
            },
        )

        assert payload["login_success_notifications"] == ["qidian"]
        assert notifier.success_calls == [
            {
                "client_id": "client-secret-654321",
                "platform": "qidian",
            }
        ]
    finally:
        engine.dispose()
