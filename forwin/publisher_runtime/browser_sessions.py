from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from forwin.models.publisher import (
    PublisherBrowserSession,
    PublisherBrowserSessionEntry,
    PublisherConnectionState,
)
from .login_evidence import payload_value, platform_login_evidence
from forwin.secret_store import (
    SecretStoreError,
    decrypt_json_with_secret,
    encrypt_json_with_secret,
)

from .platform_catalog import PlatformCatalog

logger = logging.getLogger(__name__)
_DISPLAY_TZ = ZoneInfo("America/Los_Angeles")
_SESSION_COOKIE_ENCODING = "fernet-v1"


class PublisherBrowserSessionDecodeError(ValueError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def isoformat(value: datetime | None) -> str:
    parsed = as_utc(value)
    if parsed is None:
        return ""
    return parsed.astimezone(_DISPLAY_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")


def status_payload_unverified_cookie_signal(payload: dict[str, Any] | None) -> bool:
    if not isinstance(payload, dict):
        return False
    return bool(
        payload_value(payload, "page_evidence_required")
        and payload_value(payload, "cookie_signal")
        and not payload_value(payload, "page_authenticated")
    )


def is_retryable_db_error(exc: OperationalError) -> bool:
    orig = getattr(exc, "orig", None)
    sqlstate = str(
        getattr(orig, "sqlstate", "") or getattr(orig, "pgcode", "") or ""
    ).strip()
    if sqlstate in {"40001", "40P01", "55P03", "57014", "08000", "08003", "08006", "08001"}:
        return True
    message = str(exc).lower()
    return (
        "database is locked" in message
        or "database table is locked" in message
        or "deadlock detected" in message
        or "could not serialize access" in message
        or "lock timeout" in message
        or "connection refused" in message
        or "connection not open" in message
        or "server closed the connection" in message
        or "terminating connection" in message
    )


def browser_session_sort_key(row) -> tuple[datetime, datetime, datetime]:
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    synced_at = as_utc(getattr(row, "synced_at", None)) or epoch
    verified_at = as_utc(getattr(row, "last_verified_at", None)) or epoch
    updated_at = as_utc(getattr(row, "updated_at", None)) or epoch
    return (synced_at, verified_at, updated_at)


def pick_browser_session_entry(
    entries: list[PublisherBrowserSessionEntry],
) -> PublisherBrowserSessionEntry | None:
    if not entries:
        return None
    return max(entries, key=browser_session_sort_key)


def pick_browser_sessions_by_platform(
    entries: list[PublisherBrowserSessionEntry],
) -> dict[str, PublisherBrowserSessionEntry]:
    grouped: dict[str, list[PublisherBrowserSessionEntry]] = {}
    for entry in entries:
        grouped.setdefault(entry.platform_id, []).append(entry)
    selected: dict[str, PublisherBrowserSessionEntry] = {}
    for platform_id, rows in grouped.items():
        picked = pick_browser_session_entry(rows)
        if picked is not None:
            selected[platform_id] = picked
    return selected


class BrowserCookieCodec:
    def __init__(
        self,
        *,
        publisher_session_secret: str = "",
        publisher_session_encryption_required: bool = False,
    ) -> None:
        self.publisher_session_secret = str(publisher_session_secret or "").strip()
        self.publisher_session_encryption_required = bool(
            publisher_session_encryption_required
        )
        self._plaintext_cookie_storage_warned = False
        if (
            self.publisher_session_encryption_required
            and not self.publisher_session_secret
        ):
            raise ValueError(
                "Publisher session encryption is required but no session secret is configured"
            )
        if not self.publisher_session_secret:
            logger.warning(
                "Publisher session secret is not configured; browser cookies will be stored as plaintext."
            )

    @staticmethod
    def normalize_cookie(cookie: dict[str, Any]) -> dict[str, Any]:
        same_site = str(cookie.get("sameSite", "Lax") or "Lax").strip().lower()
        if same_site in {"no_restriction", "none"}:
            same_site = "None"
        elif same_site == "strict":
            same_site = "Strict"
        else:
            same_site = "Lax"
        expiration = cookie.get("expirationDate", cookie.get("expires", -1))
        try:
            expires = float(expiration)
        except (TypeError, ValueError):
            expires = -1
        return {
            "name": str(cookie.get("name", "")).strip(),
            "value": str(cookie.get("value", "")),
            "domain": str(cookie.get("domain", "")).strip(),
            "path": str(cookie.get("path", "/") or "/"),
            "secure": bool(cookie.get("secure")),
            "httpOnly": bool(cookie.get("httpOnly")),
            "sameSite": same_site,
            "expires": expires,
        }

    def encode(self, cookies: list[dict[str, Any]]) -> str:
        if self.publisher_session_secret:
            ciphertext = encrypt_json_with_secret(self.publisher_session_secret, cookies)
            return json.dumps(
                {"encoding": _SESSION_COOKIE_ENCODING, "ciphertext": ciphertext},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        if not self._plaintext_cookie_storage_warned:
            logger.warning(
                "Publisher session secret is not configured; storing browser cookies as plaintext."
            )
            self._plaintext_cookie_storage_warned = True
        return json.dumps(cookies, ensure_ascii=False)

    def decode(self, raw: str) -> list[dict[str, Any]]:
        cookies, _metadata = self.decode_with_metadata(raw)
        return cookies

    def decode_metadata(self, raw: str) -> dict[str, Any]:
        _cookies, metadata = self.decode_with_metadata(raw)
        return metadata

    def decode_with_metadata(
        self,
        raw: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        metadata: dict[str, Any] = {
            "encrypted": False,
            "error": "",
        }
        text = str(raw or "").strip()
        if not text:
            return [], metadata
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            metadata["error"] = "publisher_session_malformed"
            logger.warning("Stored publisher browser session is malformed JSON.")
            return [], metadata
        if isinstance(payload, list):
            return self.normalize_cookie_list(payload), metadata
        if not isinstance(payload, dict):
            metadata["error"] = "publisher_session_malformed"
            logger.warning("Stored publisher browser session has an invalid payload shape.")
            return [], metadata
        if payload.get("encoding") != _SESSION_COOKIE_ENCODING:
            metadata["error"] = "publisher_session_unknown_encoding"
            logger.warning(
                "Stored publisher browser session has unknown encoding: %s",
                payload.get("encoding"),
            )
            return [], metadata
        metadata["encrypted"] = True
        if not self.publisher_session_secret:
            metadata["error"] = "publisher_session_secret_missing"
            logger.warning(
                "Stored publisher browser session is encrypted but no session secret is configured."
            )
            return [], metadata
        try:
            decrypted = decrypt_json_with_secret(
                self.publisher_session_secret,
                str(payload.get("ciphertext") or ""),
            )
        except SecretStoreError:
            metadata["error"] = "publisher_session_decrypt_failed"
            logger.warning(
                "Stored publisher browser session could not be decrypted.",
                exc_info=True,
            )
            return [], metadata
        if not isinstance(decrypted, list):
            metadata["error"] = "publisher_session_malformed"
            logger.warning("Decrypted publisher browser session is not a cookie list.")
            return [], metadata
        return self.normalize_cookie_list(decrypted), metadata

    def normalize_cookie_list(self, cookies: list[Any]) -> list[dict[str, Any]]:
        return [
            self.normalize_cookie(item)
            for item in cookies
            if isinstance(item, dict) and str(item.get("name", "")).strip()
        ]

    def cookie_names_from_json(self, cookies_json: str) -> list[str]:
        cookies = self.decode(cookies_json)
        names = [
            str(item.get("name", "")).strip()
            for item in cookies
            if isinstance(item, dict) and str(item.get("name", "")).strip()
        ]
        return sorted(dict.fromkeys(names))

    def is_browser_session_connected(
        self,
        platform: str,
        cookies_json: str,
        last_error: str,
    ) -> bool:
        if str(last_error or "").strip() == "login-required":
            return False
        cookie_names = set(self.cookie_names_from_json(cookies_json))
        if platform == "qidian":
            return "AppAuthToken" in cookie_names and bool(
                {"pubtoken", "ywopenid", "ywkey", "ywKey", "ywtab"} & cookie_names
            )
        if platform == "fanqie":
            has_session = bool({"sessionid", "sessionid_ss"} & cookie_names)
            has_writer_signal = bool(
                {"has_biz_token", "passport_auth_status", "passport_auth_status_ss", "sid_tt"}
                & cookie_names
            )
            return has_session and has_writer_signal
        return bool(cookie_names)


class BrowserSessionService:
    def __init__(
        self,
        *,
        session_factory,
        platform_catalog: PlatformCatalog,
        codec: BrowserCookieCodec,
        connection_state,
    ) -> None:
        self.session_factory = session_factory
        self.platform_catalog = platform_catalog
        self.codec = codec
        self.connection_state = connection_state

    def record_browser_session(
        self,
        *,
        client_id: str,
        platform: str,
        cookies: list[dict[str, Any]],
        raw_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        spec = self.platform_catalog.get(platform)
        now = utc_now()
        normalized = [
            self.codec.normalize_cookie(item)
            for item in cookies
            if isinstance(item, dict) and str(item.get("name", "")).strip()
        ]
        encoded_cookies = self.codec.encode(normalized)
        login_success_platforms: list[str] = []
        try:
            with self.session_factory() as session:
                self.connection_state.ensure_extension_client(session, client_id)
                cookie_connected = self.codec.is_browser_session_connected(
                    platform,
                    encoded_cookies,
                    "",
                )
                evidence_payload = dict(raw_state or {})
                cookie_signal = bool(evidence_payload.get("cookie_signal", cookie_connected))
                page_authenticated = bool(evidence_payload.get("page_authenticated"))
                login_evidence = platform_login_evidence(platform, evidence_payload)
                unverified_cookie_signal = bool(
                    evidence_payload.get("page_evidence_required")
                    and cookie_signal
                    and not page_authenticated
                )
                saved_connected_cookie_state = bool(
                    payload_value(evidence_payload, "connected")
                    and payload_value(evidence_payload, "saved_connected")
                    and cookie_connected
                    and cookie_signal
                    and not login_evidence
                    and not unverified_cookie_signal
                )
                state = session.get(PublisherConnectionState, platform)
                was_connected = bool(state.connected) if state is not None else False
                existing_payload: dict[str, Any] = {}
                if state is not None:
                    try:
                        parsed = json.loads(str(state.status_json or "{}"))
                    except json.JSONDecodeError:
                        parsed = {}
                    if isinstance(parsed, dict):
                        existing_payload = parsed
                existing_login_evidence = platform_login_evidence(
                    platform,
                    existing_payload,
                )
                fanqie_requires_authenticated_page = bool(
                    platform == "fanqie"
                    and normalized
                    and not page_authenticated
                    and not saved_connected_cookie_state
                )
                reject_session_sync = bool(
                    not page_authenticated
                    and not saved_connected_cookie_state
                    and (
                        login_evidence
                        or existing_login_evidence
                        or fanqie_requires_authenticated_page
                    )
                )
                if reject_session_sync:
                    if state is None:
                        state = PublisherConnectionState(platform_id=platform)
                        session.add(state)
                    login_method = state.login_method or "scan"
                    state_payload = {
                        "platform": platform,
                        "connected": False,
                        "login_method": login_method,
                        "last_error": "login-required",
                        "cookie_names": self.codec.cookie_names_from_json(encoded_cookies),
                        "session_sync_skipped": True,
                        **existing_payload,
                        **evidence_payload,
                        "cookie_signal": cookie_signal,
                    }
                    if existing_login_evidence and not login_evidence:
                        for key in (
                            "current_url",
                            "page_authenticated",
                            "page_login_visible",
                        ):
                            if key in existing_payload:
                                state_payload[key] = existing_payload[key]
                    state_payload["connected"] = False
                    state_payload["last_error"] = "login-required"
                    state.extension_client_id = client_id
                    state.connected = False
                    state.login_method = login_method
                    state.last_error = "login-required"
                    state.status_json = json.dumps(state_payload, ensure_ascii=False)
                    state.last_heartbeat_at = now
                    self.connection_state.upsert_extension_platform_state(
                        session,
                        client_id=client_id,
                        platform_id=platform,
                        connected=False,
                        login_method=login_method,
                        last_error="login-required",
                        status_payload=state_payload,
                        last_heartbeat_at=now,
                    )
                    session.commit()
                    return {
                        "ok": True,
                        "skipped": True,
                        "message": f"{spec.display_name} 浏览器会话未写入：当前页面仍需要登录。",
                        "server_time": isoformat(now),
                        "cookie_count": len(normalized),
                        "login_success_platforms": [],
                    }
                entry = session.get(
                    PublisherBrowserSessionEntry,
                    {"client_id": client_id, "platform_id": platform},
                )
                if entry is None:
                    entry = PublisherBrowserSessionEntry(
                        client_id=client_id,
                        platform_id=platform,
                    )
                    session.add(entry)
                entry.cookie_count = len(normalized)
                entry.cookies_json = encoded_cookies
                entry.synced_at = now
                entry.last_error = ""

                stored = session.get(PublisherBrowserSession, platform)
                if stored is None:
                    stored = PublisherBrowserSession(platform_id=platform)
                    session.add(stored)
                stored.extension_client_id = client_id
                stored.cookie_count = len(normalized)
                stored.cookies_json = encoded_cookies
                stored.synced_at = now
                stored.last_error = ""
                connected = bool(
                    (cookie_signal or page_authenticated or saved_connected_cookie_state)
                    and not login_evidence
                    and not unverified_cookie_signal
                )

                preserve_authenticated_heartbeat = bool(
                    unverified_cookie_signal
                    and not login_evidence
                    and state is not None
                    and state.connected
                    and existing_payload.get("page_authenticated")
                    and not platform_login_evidence(platform, existing_payload)
                )
                if state is None:
                    state = PublisherConnectionState(platform_id=platform)
                    session.add(state)
                if not preserve_authenticated_heartbeat:
                    state.extension_client_id = client_id
                    state.connected = connected
                    if state.connected and not was_connected:
                        login_success_platforms.append(platform)
                    state.login_method = state.login_method or "scan"
                    if state.connected:
                        state.last_error = ""
                    state_payload = {
                        "platform": platform,
                        "connected": connected,
                        "login_method": state.login_method,
                        "last_error": state.last_error,
                        "cookie_names": self.codec.cookie_names_from_json(stored.cookies_json),
                        "session_synced": True,
                        **evidence_payload,
                        "cookie_signal": cookie_signal,
                    }
                    state.status_json = json.dumps(state_payload, ensure_ascii=False)
                    state.last_heartbeat_at = now
                    self.connection_state.upsert_extension_platform_state(
                        session,
                        client_id=client_id,
                        platform_id=platform,
                        connected=connected,
                        login_method=state.login_method,
                        last_error="",
                        status_payload=state_payload,
                        last_heartbeat_at=now,
                    )
                session.commit()
        except OperationalError as exc:
            if not is_retryable_db_error(exc):
                raise
            logger.warning(
                "Publisher browser session sync skipped because database is busy: %s",
                exc,
            )
            return {
                "ok": False,
                "message": f"{spec.display_name} 浏览器会话暂未写入：数据库忙，请稍后重试。",
                "server_time": isoformat(now),
                "cookie_count": len(normalized),
                "retryable": True,
            }
        return {
            "ok": True,
            "message": f"{spec.display_name} 浏览器会话已同步到后端。",
            "server_time": isoformat(now),
            "cookie_count": len(normalized),
            "login_success_platforms": login_success_platforms,
        }

    def get_browser_session(self, platform: str) -> dict[str, Any] | None:
        self.platform_catalog.get(platform)
        with self.session_factory() as session:
            entries = session.execute(
                select(PublisherBrowserSessionEntry).where(
                    PublisherBrowserSessionEntry.platform_id == platform
                )
            ).scalars().all()
            selected_entry = pick_browser_session_entry(entries)
            if selected_entry is not None:
                cookies, metadata = self.codec.decode_with_metadata(
                    selected_entry.cookies_json
                )
                return {
                    "platform": selected_entry.platform_id,
                    "client_id": selected_entry.client_id,
                    "cookie_count": selected_entry.cookie_count,
                    "cookies": cookies,
                    "synced_at": isoformat(selected_entry.synced_at),
                    "last_error": selected_entry.last_error or metadata["error"],
                }

            stored = session.get(PublisherBrowserSession, platform)
            if stored is None:
                return None
            cookies, metadata = self.codec.decode_with_metadata(stored.cookies_json)
            return {
                "platform": stored.platform_id,
                "client_id": stored.extension_client_id,
                "cookie_count": stored.cookie_count,
                "cookies": cookies,
                "synced_at": isoformat(stored.synced_at),
                "last_error": stored.last_error or metadata["error"],
            }

    def get_browser_session_summary(self, platform: str) -> dict[str, Any] | None:
        self.platform_catalog.get(platform)
        with self.session_factory() as session:
            entries = session.execute(
                select(PublisherBrowserSessionEntry).where(
                    PublisherBrowserSessionEntry.platform_id == platform
                )
            ).scalars().all()
            selected_entry = pick_browser_session_entry(entries)
            row = selected_entry or session.get(PublisherBrowserSession, platform)
            if row is None:
                return None
            raw = str(row.cookies_json or "")
            metadata = self.codec.decode_metadata(raw)
            cookie_names = self.codec.cookie_names_from_json(raw)
            last_error = str(getattr(row, "last_error", "") or "") or metadata["error"]
            state = session.get(PublisherConnectionState, platform)
            status_payload: dict[str, Any] = {}
            if state is not None:
                try:
                    parsed = json.loads(str(state.status_json or "{}"))
                except json.JSONDecodeError:
                    parsed = {}
                if isinstance(parsed, dict):
                    status_payload = parsed
                    status_payload.setdefault("last_error", state.last_error or "")
            connected = self.codec.is_browser_session_connected(
                platform,
                raw,
                last_error,
            )
            if (
                connected
                and (
                    status_payload_unverified_cookie_signal(status_payload)
                    or platform_login_evidence(platform, status_payload)
                )
            ):
                connected = False
            return {
                "platform": platform,
                "client_id": str(
                    getattr(row, "client_id", "")
                    or getattr(row, "extension_client_id", "")
                    or ""
                ),
                "cookie_count": int(getattr(row, "cookie_count", 0) or 0),
                "cookie_names": cookie_names,
                "cookies_redacted": True,
                "synced_at": isoformat(getattr(row, "synced_at", None)),
                "last_error": last_error,
                "connected": connected,
            }

    def has_browser_session(self, platform: str) -> bool:
        payload = self.get_browser_session(platform)
        if not payload or not payload.get("cookies"):
            return False
        return self.codec.is_browser_session_connected(
            platform,
            self.codec.encode(payload["cookies"]),
            str(payload.get("last_error", "")).strip(),
        )

    def mark_browser_session_result(
        self,
        *,
        platform: str,
        last_error: str = "",
        verified: bool = False,
    ) -> None:
        now = utc_now()
        with self.session_factory() as session:
            stored = session.get(PublisherBrowserSession, platform)
            if stored is not None:
                stored.last_error = last_error.strip()
                if verified:
                    stored.last_verified_at = now
            entries = session.execute(
                select(PublisherBrowserSessionEntry).where(
                    PublisherBrowserSessionEntry.platform_id == platform
                )
            ).scalars().all()
            selected_entry = pick_browser_session_entry(entries)
            if stored is None and selected_entry is None:
                return
            if selected_entry is not None:
                selected_entry.last_error = last_error.strip()
                if verified:
                    selected_entry.last_verified_at = now
            session.commit()
