from __future__ import annotations

import base64
import json
import mimetypes
import uuid
from datetime import UTC, datetime
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen


MAX_LOGIN_QR_IMAGE_BYTES = 4 * 1024 * 1024
ALLOWED_LOGIN_QR_MIME_TYPES = {
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
}


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_url(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw[:500]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))[:500]


def _redacted_client_id(value: str) -> str:
    client_id = str(value or "").strip()
    if not client_id:
        return "unknown"
    if len(client_id) <= 6:
        return client_id
    return f"...{client_id[-6:]}"


def _parse_image_data_url(value: str) -> tuple[str, bytes]:
    text = str(value or "").strip()
    if not text.startswith("data:") or "," not in text:
        raise ValueError("login QR image_data_url must be a base64 image data URL")
    header, encoded = text.split(",", 1)
    if ";base64" not in header.lower():
        raise ValueError("login QR image_data_url must be a base64 image data URL")
    mime_type = header[len("data:") :].split(";", 1)[0].strip().lower()
    if mime_type not in ALLOWED_LOGIN_QR_MIME_TYPES:
        raise ValueError("login QR image_data_url must be an allowed image data URL")
    try:
        image_bytes = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("login QR image_data_url contains invalid base64") from exc
    if not image_bytes:
        raise ValueError("login QR image_data_url is empty")
    if len(image_bytes) > MAX_LOGIN_QR_IMAGE_BYTES:
        raise ValueError("login QR image is too large")
    return mime_type, image_bytes


def _extension_for_mime(mime_type: str) -> str:
    if mime_type == "image/jpeg":
        return ".jpg"
    return mimetypes.guess_extension(mime_type) or ".png"


def _multipart_body(
    *,
    payload: dict[str, Any],
    file_field_name: str,
    filename: str,
    mime_type: str,
    file_bytes: bytes,
) -> tuple[str, bytes]:
    boundary = f"forwin-{uuid.uuid4().hex}"
    chunks: list[bytes] = []

    def add(value: str) -> None:
        chunks.append(value.encode("utf-8"))

    add(f"--{boundary}\r\n")
    add('Content-Disposition: form-data; name="payload_json"\r\n')
    add("Content-Type: application/json; charset=utf-8\r\n\r\n")
    add(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    add("\r\n")
    add(f"--{boundary}\r\n")
    add(
        f'Content-Disposition: form-data; name="{file_field_name}"; '
        f'filename="{filename}"\r\n'
    )
    add(f"Content-Type: {mime_type}\r\n\r\n")
    chunks.append(file_bytes)
    add("\r\n")
    add(f"--{boundary}--\r\n")
    return boundary, b"".join(chunks)


class DiscordLoginQrNotifier:
    def __init__(
        self,
        webhook_url: str = "",
        *,
        timeout_seconds: float = 8.0,
        urlopen_impl: Callable[..., Any] = urlopen,
    ) -> None:
        self.webhook_url = str(webhook_url or "").strip()
        self.timeout_seconds = float(timeout_seconds)
        self.urlopen_impl = urlopen_impl

    def notify(
        self,
        *,
        client_id: str,
        platform: str,
        current_url: str,
        image_data_url: str,
        source: str = "",
        captured_at: str = "",
    ) -> dict[str, Any]:
        now = utc_now_iso()
        if not self.webhook_url:
            return {
                "ok": True,
                "message": "Discord login QR webhook is not configured.",
                "server_time": now,
                "dispatched": False,
                "disabled": True,
            }
        platform_id = str(platform or "").strip() or "unknown"
        mime_type, image_bytes = _parse_image_data_url(image_data_url)
        safe_url = _safe_url(current_url)
        timestamp = str(captured_at or "").strip() or now
        filename = f"{platform_id}-login-qr{_extension_for_mime(mime_type)}"
        payload = {
            "content": "\n".join(
                line
                for line in (
                    "ForWin publisher login requires scan.",
                    f"Platform: {platform_id}",
                    f"Client: {str(client_id or '').strip() or 'unknown'}",
                    f"URL: {safe_url}" if safe_url else "",
                    f"Source: {str(source or '').strip()}" if source else "",
                    f"Captured: {timestamp}",
                )
                if line
            ),
        }
        boundary, body = _multipart_body(
            payload=payload,
            file_field_name="files[0]",
            filename=filename,
            mime_type=mime_type,
            file_bytes=image_bytes,
        )
        request = Request(
            self.webhook_url,
            data=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "User-Agent": "ForWin-Publisher/1.0",
            },
            method="POST",
        )
        with self.urlopen_impl(request, timeout=self.timeout_seconds) as response:
            status = int(getattr(response, "status", 0) or response.getcode() or 0)
        if status and status >= 400:
            raise RuntimeError(f"Discord login QR webhook failed with HTTP {status}")
        return {
            "ok": True,
            "message": "Discord login QR notification sent.",
            "server_time": now,
            "dispatched": True,
        }

    def notify_login_success(
        self,
        *,
        client_id: str,
        platform: str,
        detected_at: str = "",
    ) -> dict[str, Any]:
        now = utc_now_iso()
        if not self.webhook_url:
            return {
                "ok": True,
                "message": "Discord login success webhook is not configured.",
                "server_time": now,
                "dispatched": False,
            }
        platform_id = str(platform or "").strip() or "unknown"
        timestamp = str(detected_at or "").strip() or now
        payload = {
            "content": "\n".join(
                [
                    "ForWin publisher login confirmed.",
                    f"Platform: {platform_id}",
                    f"Client: {_redacted_client_id(client_id)}",
                    f"Detected: {timestamp}",
                ]
            ),
        }
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = Request(
            self.webhook_url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "ForWin-Publisher/1.0",
            },
            method="POST",
        )
        with self.urlopen_impl(request, timeout=self.timeout_seconds) as response:
            status = int(getattr(response, "status", 0) or response.getcode() or 0)
        if status and status >= 400:
            raise RuntimeError(
                f"Discord login success webhook failed with HTTP {status}"
            )
        return {
            "ok": True,
            "message": "Discord login success notification sent.",
            "server_time": now,
            "dispatched": True,
        }
