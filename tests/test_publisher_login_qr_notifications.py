from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from urllib.request import Request

import pytest

import forwin.publishers.manager as publisher_manager_module
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


class _FakeLoginQrNotifier:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def notify(self, **kwargs) -> dict[str, object]:
        self.calls.append(kwargs)
        return {
            "ok": True,
            "dispatched": True,
            "message": "sent",
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
