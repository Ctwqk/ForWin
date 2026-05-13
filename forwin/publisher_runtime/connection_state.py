from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from forwin.models.publisher import (
    PublisherBrowserSession,
    PublisherBrowserSessionEntry,
    PublisherConnectionState,
    PublisherExtensionClient,
    PublisherExtensionPlatformState,
)

from .browser_sessions import (
    BrowserCookieCodec,
    as_utc,
    is_retryable_db_error,
    isoformat,
    pick_browser_sessions_by_platform,
    utc_now,
)
from .login_evidence import platform_login_evidence
from .platform_catalog import PlatformCatalog

logger = logging.getLogger(__name__)


def row_login_evidence(row: Any) -> bool:
    if row is None:
        return False
    payload = {
        "last_error": getattr(row, "last_error", ""),
    }
    try:
        status_payload = json.loads(str(getattr(row, "status_json", "") or "{}"))
    except json.JSONDecodeError:
        status_payload = {}
    if isinstance(status_payload, dict):
        payload.update(status_payload)
    return platform_login_evidence(str(getattr(row, "platform_id", "") or ""), payload)


def row_unverified_cookie_signal(row: Any) -> bool:
    if row is None:
        return False
    try:
        status_payload = json.loads(str(getattr(row, "status_json", "") or "{}"))
    except json.JSONDecodeError:
        return False
    if not isinstance(status_payload, dict):
        return False
    return bool(
        status_payload.get("page_evidence_required")
        and status_payload.get("cookie_signal")
        and not status_payload.get("page_authenticated")
    )


def ensure_extension_client(
    session,
    client_id: str,
) -> PublisherExtensionClient | None:
    normalized = str(client_id or "").strip()
    if not normalized:
        return None
    client = session.get(PublisherExtensionClient, normalized)
    if client is None:
        client = PublisherExtensionClient(client_id=normalized)
        session.add(client)
        try:
            session.flush([client])
        except IntegrityError:
            session.rollback()
            client = session.get(PublisherExtensionClient, normalized)
    return client


def upsert_extension_platform_state(
    session,
    *,
    client_id: str,
    platform_id: str,
    connected: bool,
    login_method: str,
    last_error: str,
    status_payload: dict[str, Any],
    last_heartbeat_at: datetime,
) -> None:
    state = session.get(
        PublisherExtensionPlatformState,
        {
            "client_id": client_id,
            "platform_id": platform_id,
        },
    )
    if state is None:
        state = PublisherExtensionPlatformState(
            client_id=client_id,
            platform_id=platform_id,
        )
        session.add(state)
    state.connected = connected
    state.login_method = str(login_method or "").strip()
    state.last_error = str(last_error or "").strip()
    state.status_json = json.dumps(status_payload or {}, ensure_ascii=False)
    state.last_heartbeat_at = last_heartbeat_at


class ExtensionConnectionService:
    def __init__(
        self,
        *,
        session_factory,
        platform_catalog: PlatformCatalog,
        codec: BrowserCookieCodec,
        heartbeat_stale_seconds: int = 90,
        preferred_client_id: str = "",
        strict_preferred_client: bool = False,
    ) -> None:
        self.session_factory = session_factory
        self.platform_catalog = platform_catalog
        self.codec = codec
        self.heartbeat_stale_seconds = heartbeat_stale_seconds
        self.preferred_client_id = str(preferred_client_id or "").strip()
        self.strict_preferred_client = bool(strict_preferred_client)
        self.ensure_extension_client = ensure_extension_client
        self.upsert_extension_platform_state = upsert_extension_platform_state

    def is_recent(self, value: datetime | None) -> bool:
        parsed = as_utc(value)
        if parsed is None:
            return False
        return parsed >= (utc_now() - timedelta(seconds=self.heartbeat_stale_seconds))

    def is_browser_session_connected(
        self,
        platform: str,
        cookies_json: str,
        last_error: str,
    ) -> bool:
        return self.codec.is_browser_session_connected(platform, cookies_json, last_error)

    def list_platforms(self) -> list[dict[str, Any]]:
        platform_ids = self.platform_catalog.list_ids()
        with self.session_factory() as session:
            state_rows = session.execute(
                select(
                    PublisherConnectionState.platform_id,
                    PublisherConnectionState.extension_client_id,
                    PublisherConnectionState.connected,
                    PublisherConnectionState.last_error,
                    PublisherConnectionState.status_json,
                    PublisherConnectionState.last_heartbeat_at,
                ).where(PublisherConnectionState.platform_id.in_(platform_ids))
            ).all()
            browser_session_rows = session.execute(
                select(
                    PublisherBrowserSession.platform_id,
                    PublisherBrowserSession.extension_client_id,
                    PublisherBrowserSession.cookies_json,
                    PublisherBrowserSession.last_error,
                ).where(PublisherBrowserSession.platform_id.in_(platform_ids))
            ).all()
            browser_session_entry_rows = session.execute(
                select(PublisherBrowserSessionEntry).where(
                    PublisherBrowserSessionEntry.platform_id.in_(platform_ids)
                )
            ).scalars().all()
            browser_sessions_by_platform = pick_browser_sessions_by_platform(
                browser_session_entry_rows
            )
            client_ids = {
                client_id
                for row in state_rows
                for client_id in [row.extension_client_id]
                if client_id
            } | {
                client_id
                for row in browser_session_rows
                for client_id in [row.extension_client_id]
                if client_id
            } | {
                client_id
                for row in browser_session_entry_rows
                for client_id in [row.client_id]
                if client_id
            }
            if self.preferred_client_id:
                client_ids.add(self.preferred_client_id)
            client_rows = (
                session.execute(
                    select(
                        PublisherExtensionClient.client_id,
                        PublisherExtensionClient.last_heartbeat_at,
                    ).where(PublisherExtensionClient.client_id.in_(client_ids))
                ).all()
                if client_ids
                else []
            )

            states = {row.platform_id: row for row in state_rows}
            clients = {row.client_id: row for row in client_rows}
            browser_sessions = {row.platform_id: row for row in browser_session_rows}
            preferred_states = (
                {
                    row.platform_id: row
                    for row in session.execute(
                        select(
                            PublisherExtensionPlatformState.platform_id,
                            PublisherExtensionPlatformState.client_id,
                            PublisherExtensionPlatformState.connected,
                            PublisherExtensionPlatformState.last_error,
                            PublisherExtensionPlatformState.status_json,
                            PublisherExtensionPlatformState.last_heartbeat_at,
                        ).where(
                            PublisherExtensionPlatformState.client_id == self.preferred_client_id,
                            PublisherExtensionPlatformState.platform_id.in_(platform_ids),
                        )
                    ).all()
                }
                if self.preferred_client_id
                else {}
            )

        items: list[dict[str, Any]] = []
        for spec in self.platform_catalog.values():
            state = states.get(spec.platform_id)
            browser_session = browser_sessions_by_platform.get(spec.platform_id)
            summary_browser_session = browser_sessions.get(spec.platform_id)
            preferred_state = preferred_states.get(spec.platform_id)
            client = (
                clients.get(state.extension_client_id)
                if state and state.extension_client_id
                else None
            )
            preferred_client = (
                clients.get(preferred_state.client_id)
                if preferred_state and preferred_state.client_id
                else None
            )
            session_client = (
                clients.get(browser_session.client_id)
                if browser_session and browser_session.client_id
                else None
            )
            preferred_client_recent = bool(
                preferred_client and self.is_recent(preferred_client.last_heartbeat_at)
            )
            preferred_state_recent = bool(
                preferred_state and self.is_recent(preferred_state.last_heartbeat_at)
            )
            client_recent = bool(client and self.is_recent(client.last_heartbeat_at))
            session_client_recent = bool(
                session_client and self.is_recent(session_client.last_heartbeat_at)
            )
            state_recent = bool(state and self.is_recent(state.last_heartbeat_at))
            login_evidence_recent = (
                bool(preferred_state and preferred_state_recent and row_login_evidence(preferred_state))
                or bool(state and state_recent and row_login_evidence(state))
            )
            unverified_cookie_recent = (
                bool(
                    preferred_state
                    and preferred_state_recent
                    and row_unverified_cookie_signal(preferred_state)
                )
                or bool(state and state_recent and row_unverified_cookie_signal(state))
            )
            extension_heartbeat_at = None
            if preferred_client_recent and preferred_client.last_heartbeat_at:
                extension_heartbeat_at = preferred_client.last_heartbeat_at
            elif client_recent and client.last_heartbeat_at:
                extension_heartbeat_at = client.last_heartbeat_at
            elif session_client_recent and session_client.last_heartbeat_at:
                extension_heartbeat_at = session_client.last_heartbeat_at
            elif preferred_client and preferred_client.last_heartbeat_at:
                extension_heartbeat_at = preferred_client.last_heartbeat_at
            elif client and client.last_heartbeat_at:
                extension_heartbeat_at = client.last_heartbeat_at
            elif session_client and session_client.last_heartbeat_at:
                extension_heartbeat_at = session_client.last_heartbeat_at

            last_heartbeat_at = extension_heartbeat_at
            if last_heartbeat_at is None:
                if preferred_state_recent and preferred_state.last_heartbeat_at:
                    last_heartbeat_at = preferred_state.last_heartbeat_at
                elif state_recent and state.last_heartbeat_at:
                    last_heartbeat_at = state.last_heartbeat_at
                elif preferred_state and preferred_state.last_heartbeat_at:
                    last_heartbeat_at = preferred_state.last_heartbeat_at
                elif state and state.last_heartbeat_at:
                    last_heartbeat_at = state.last_heartbeat_at

            extension_online = self.is_recent(extension_heartbeat_at or last_heartbeat_at)
            preferred_connected = bool(
                preferred_state
                and preferred_state.connected
                and self.is_recent(preferred_state.last_heartbeat_at)
            )
            global_connected = bool(
                state and state.connected and self.is_recent(state.last_heartbeat_at)
            )
            browser_connected = bool(
                browser_session
                and self.is_browser_session_connected(
                    spec.platform_id,
                    browser_session.cookies_json,
                    browser_session.last_error,
                )
            )
            connected = False
            if login_evidence_recent or unverified_cookie_recent:
                connected = False
            elif self.preferred_client_id:
                connected = preferred_connected
            elif (
                preferred_connected
            ):
                connected = True
            elif global_connected:
                connected = True
            elif browser_connected:
                connected = True

            last_error = (
                preferred_state.last_error
                if preferred_state and self.is_recent(preferred_state.last_heartbeat_at)
                else (
                    state.last_error
                    if state and self.is_recent(state.last_heartbeat_at)
                    else ""
                )
            )
            if not last_error and browser_session:
                last_error = browser_session.last_error
            if not last_error and summary_browser_session:
                last_error = summary_browser_session.last_error

            preferred_client_id = (
                preferred_state.client_id
                if preferred_state and preferred_state.client_id
                else self.preferred_client_id
            )
            latest_client_id = (
                state.extension_client_id if state and state.extension_client_id else ""
            )
            session_client_id = (
                browser_session.client_id if browser_session and browser_session.client_id else ""
            )
            fallback_client_id = ""
            if self.preferred_client_id and not connected:
                if (
                    latest_client_id
                    and latest_client_id != self.preferred_client_id
                    and global_connected
                ):
                    fallback_client_id = latest_client_id
                elif (
                    session_client_id
                    and session_client_id != self.preferred_client_id
                    and browser_connected
                ):
                    fallback_client_id = session_client_id
            selected_extension_client_id = (
                preferred_client_id
                if self.preferred_client_id
                else (
                    latest_client_id
                    or session_client_id
                    or (
                        summary_browser_session.extension_client_id
                        if summary_browser_session
                        else ""
                    )
                )
            )

            items.append(
                {
                    "platform_id": spec.platform_id,
                    "display_name": spec.display_name,
                    "login_url": spec.login_url,
                    "dashboard_url": spec.dashboard_url,
                    "publish_url": spec.publish_url,
                    "supported_login_methods": list(spec.supported_login_methods),
                    "supported_actions": list(spec.supported_actions),
                    "connected": connected,
                    "extension_online": extension_online,
                    "last_heartbeat_at": isoformat(last_heartbeat_at),
                    "last_error": last_error,
                    "extension_client_id": selected_extension_client_id,
                    "preferred_client_state": {
                        "client_id": preferred_client_id,
                        "connected": preferred_connected,
                        "recent": preferred_state_recent and preferred_client_recent,
                        "last_heartbeat_at": isoformat(
                            preferred_state.last_heartbeat_at
                            if preferred_state is not None
                            else (
                                preferred_client.last_heartbeat_at
                                if preferred_client is not None
                                else None
                            )
                        ),
                        "last_error": preferred_state.last_error if preferred_state else "",
                    },
                    "latest_client_state": {
                        "client_id": latest_client_id,
                        "connected": global_connected,
                        "recent": state_recent and client_recent,
                        "last_heartbeat_at": isoformat(
                            state.last_heartbeat_at if state is not None else None
                        ),
                        "last_error": state.last_error if state else "",
                    },
                    "global_platform_state": {
                        "client_id": latest_client_id,
                        "connected": global_connected,
                        "recent": state_recent,
                        "last_heartbeat_at": isoformat(
                            state.last_heartbeat_at if state is not None else None
                        ),
                        "last_error": state.last_error if state else "",
                    },
                    "browser_session_state": {
                        "client_id": session_client_id,
                        "connected": browser_connected,
                        "recent": session_client_recent,
                        "last_error": browser_session.last_error if browser_session else "",
                    },
                    "fallback_available": bool(fallback_client_id),
                    "fallback_client_id": fallback_client_id,
                }
            )
        return items

    def heartbeat(
        self,
        *,
        client_id: str,
        extension_version: str,
        browser_name: str,
        browser_version: str,
        backend_base_url: str,
        platforms: list[dict[str, Any]],
    ) -> dict[str, Any]:
        now = utc_now()
        try:
            with self.session_factory() as session:
                client = self.ensure_extension_client(session, client_id)

                client.extension_version = extension_version
                client.browser_name = browser_name
                client.browser_version = browser_version
                client.backend_base_url = backend_base_url
                client.last_heartbeat_at = now

                for item in platforms:
                    platform_id = str(item.get("platform", "")).strip()
                    if not platform_id or not self.platform_catalog.has(platform_id):
                        continue
                    state = session.get(PublisherConnectionState, platform_id)
                    if state is None:
                        state = PublisherConnectionState(platform_id=platform_id)
                        session.add(state)
                    state.extension_client_id = client_id
                    cookie_signal = bool(item.get("cookie_signal"))
                    login_evidence = platform_login_evidence(platform_id, item)
                    state.connected = bool(
                        (cookie_signal or bool(item.get("page_authenticated")))
                        and not login_evidence
                    )
                    state.login_method = str(item.get("login_method", "")).strip()
                    state.last_error = (
                        "login-required"
                        if login_evidence
                        else str(item.get("last_error", "")).strip()
                    )
                    state.status_json = json.dumps(item, ensure_ascii=False)
                    state.last_heartbeat_at = now
                    self.upsert_extension_platform_state(
                        session,
                        client_id=client_id,
                        platform_id=platform_id,
                        connected=state.connected,
                        login_method=state.login_method,
                        last_error=state.last_error,
                        status_payload=item,
                        last_heartbeat_at=now,
                    )

                session.commit()
        except OperationalError as exc:
            if not is_retryable_db_error(exc):
                raise
            logger.warning(
                "Publisher extension heartbeat skipped because database is busy: %s",
                exc,
            )
            return {
                "ok": False,
                "message": "扩展心跳暂未写入：数据库忙，请稍后重试。",
                "server_time": isoformat(now),
                "retryable": True,
            }

        return {"ok": True, "message": "扩展心跳已记录。", "server_time": isoformat(now)}

    def preferred_client_heartbeat(
        self,
        *,
        preferred_client_id: str = "",
        stale_seconds: int = 90,
        allow_latest_recent_fallback: bool = False,
    ) -> dict[str, Any]:
        resolved_client_id = str(preferred_client_id or "").strip()
        cutoff = utc_now() - timedelta(seconds=max(int(stale_seconds or 90), 1))
        with self.session_factory() as session:
            latest_recent = session.execute(
                select(PublisherExtensionClient)
                .where(PublisherExtensionClient.last_heartbeat_at >= cutoff)
                .order_by(PublisherExtensionClient.last_heartbeat_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            latest_recent_client_id = (
                str(latest_recent.client_id or "").strip()
                if latest_recent is not None
                else ""
            )
            client_id = resolved_client_id
            if not client_id and allow_latest_recent_fallback:
                client_id = latest_recent_client_id
            client = session.get(PublisherExtensionClient, client_id) if client_id else None
            recent_platforms = []
            if client_id:
                recent_platforms = list(
                    session.execute(
                        select(PublisherExtensionPlatformState.platform_id)
                        .where(
                            PublisherExtensionPlatformState.client_id == client_id,
                            PublisherExtensionPlatformState.last_heartbeat_at >= cutoff,
                        )
                        .order_by(PublisherExtensionPlatformState.platform_id.asc())
                    ).scalars()
                )

        latest_payload = {
            "latest_recent_client_id": latest_recent_client_id,
            "latest_recent_backend_base_url": (
                str(latest_recent.backend_base_url or "").strip()
                if latest_recent is not None
                else ""
            ),
            "latest_recent_heartbeat_at": isoformat(
                latest_recent.last_heartbeat_at if latest_recent is not None else None
            ),
        }
        using_latest_recent = bool(
            allow_latest_recent_fallback
            and client_id
            and latest_recent_client_id
            and client_id == latest_recent_client_id
            and not resolved_client_id
        )
        if not client_id:
            return {
                "ok": False,
                "client_id": "",
                "backend_base_url": "",
                "last_heartbeat_at": "",
                "recent_platforms": [],
                "message": "preferred publisher client id is empty and no recent publisher client heartbeat was found",
                **latest_payload,
            }
        if client is None:
            return {
                "ok": False,
                "client_id": client_id,
                "backend_base_url": "",
                "last_heartbeat_at": "",
                "recent_platforms": [],
                "message": "preferred publisher client heartbeat was not found",
                **latest_payload,
            }
        heartbeat = as_utc(client.last_heartbeat_at)
        if heartbeat is None:
            return {
                "ok": False,
                "client_id": client_id,
                "backend_base_url": str(client.backend_base_url or ""),
                "last_heartbeat_at": "",
                "recent_platforms": [],
                "message": "preferred publisher client heartbeat timestamp is missing or invalid",
                **latest_payload,
            }
        if heartbeat < cutoff or not recent_platforms:
            return {
                "ok": False,
                "client_id": client_id,
                "backend_base_url": str(client.backend_base_url or ""),
                "last_heartbeat_at": isoformat(heartbeat),
                "recent_platforms": recent_platforms,
                "message": (
                    "latest publisher client heartbeat is stale"
                    if using_latest_recent
                    else "preferred publisher client heartbeat is stale"
                ),
                **latest_payload,
            }
        return {
            "ok": True,
            "client_id": client_id,
            "backend_base_url": str(client.backend_base_url or ""),
            "last_heartbeat_at": isoformat(heartbeat),
            "recent_platforms": recent_platforms,
            "message": (
                "latest publisher client heartbeat is recent"
                if using_latest_recent
                else "preferred publisher client heartbeat is recent"
            ),
            **latest_payload,
        }

    def claimable_platforms(
        self,
        session,
        *,
        client_id: str,
        platforms: list[str],
    ) -> list[str]:
        if not platforms:
            return []
        preferred_client_id = self.preferred_client_id
        if self.strict_preferred_client and preferred_client_id:
            return list(platforms) if client_id == preferred_client_id else []
        if not preferred_client_id or client_id == preferred_client_id:
            return list(platforms)
        return [
            platform_id
            for platform_id in platforms
            if not self.preferred_client_can_claim_platform(
                session,
                platform_id=platform_id,
            )
        ]

    def preferred_client_can_claim_platform(self, session, *, platform_id: str) -> bool:
        preferred_client_id = self.preferred_client_id
        if not preferred_client_id:
            return False
        client = session.get(PublisherExtensionClient, preferred_client_id)
        if client is None or not self.is_recent(client.last_heartbeat_at):
            return False
        platform_state = session.get(
            PublisherExtensionPlatformState,
            {
                "client_id": preferred_client_id,
                "platform_id": platform_id,
            },
        )
        return bool(platform_state and platform_state.connected)
