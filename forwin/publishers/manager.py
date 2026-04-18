from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from forwin.models.project import Project
from forwin.models.publisher import (
    PublisherBrowserSession,
    PublisherBrowserSessionEntry,
    PublisherCommentSyncJob,
    PublisherConnectionState,
    PublisherExtensionClient,
    PublisherExtensionPlatformState,
    PublisherRawComment,
    PublisherUploadJob,
)

from .platforms import SUPPORTED_PLATFORMS, PlatformSpec

_DISPLAY_TZ = ZoneInfo("America/Los_Angeles")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _isoformat(value: datetime | None) -> str:
    parsed = _as_utc(value)
    if parsed is None:
        return ""
    return parsed.astimezone(_DISPLAY_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class PublisherManager:
    def __init__(
        self,
        session_factory: sessionmaker,
        extension_api_key: str = "",
        heartbeat_stale_seconds: int = 90,
        preferred_client_id: str = "",
    ) -> None:
        self.session_factory = session_factory
        self.extension_api_key = extension_api_key
        self.heartbeat_stale_seconds = heartbeat_stale_seconds
        self.preferred_client_id = str(preferred_client_id or "").strip()

    @staticmethod
    def _browser_session_sort_key(row) -> tuple[datetime, datetime, datetime]:
        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        synced_at = _as_utc(getattr(row, "synced_at", None)) or epoch
        verified_at = _as_utc(getattr(row, "last_verified_at", None)) or epoch
        updated_at = _as_utc(getattr(row, "updated_at", None)) or epoch
        return (synced_at, verified_at, updated_at)

    def _pick_browser_session_entry(
        self,
        entries: list[PublisherBrowserSessionEntry],
    ) -> PublisherBrowserSessionEntry | None:
        if not entries:
            return None
        return max(entries, key=self._browser_session_sort_key)

    def _pick_browser_sessions_by_platform(
        self,
        entries: list[PublisherBrowserSessionEntry],
    ) -> dict[str, PublisherBrowserSessionEntry]:
        grouped: dict[str, list[PublisherBrowserSessionEntry]] = {}
        for entry in entries:
            grouped.setdefault(entry.platform_id, []).append(entry)
        selected: dict[str, PublisherBrowserSessionEntry] = {}
        for platform_id, rows in grouped.items():
            picked = self._pick_browser_session_entry(rows)
            if picked is not None:
                selected[platform_id] = picked
        return selected

    def list_platforms(self) -> list[dict[str, Any]]:
        platform_ids = list(SUPPORTED_PLATFORMS.keys())
        with self.session_factory() as session:
            state_rows = session.execute(
                select(
                    PublisherConnectionState.platform_id,
                    PublisherConnectionState.extension_client_id,
                    PublisherConnectionState.connected,
                    PublisherConnectionState.last_error,
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
            browser_sessions_by_platform = self._pick_browser_sessions_by_platform(
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
        for spec in SUPPORTED_PLATFORMS.values():
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
            last_heartbeat_at = None
            if preferred_client and preferred_client.last_heartbeat_at:
                last_heartbeat_at = preferred_client.last_heartbeat_at
            elif preferred_state and preferred_state.last_heartbeat_at:
                last_heartbeat_at = preferred_state.last_heartbeat_at
            elif client and client.last_heartbeat_at:
                last_heartbeat_at = client.last_heartbeat_at
            elif session_client and session_client.last_heartbeat_at:
                last_heartbeat_at = session_client.last_heartbeat_at
            elif state and state.last_heartbeat_at:
                last_heartbeat_at = state.last_heartbeat_at

            extension_online = self._is_recent(last_heartbeat_at)
            connected = False
            if preferred_state and preferred_state.connected and self._is_recent(preferred_state.last_heartbeat_at):
                connected = True
            elif state and state.connected and self._is_recent(state.last_heartbeat_at):
                connected = True
            elif browser_session and self._is_browser_session_connected(
                spec.platform_id,
                browser_session.cookies_json,
                browser_session.last_error,
            ):
                connected = True

            last_error = (
                preferred_state.last_error
                if preferred_state and self._is_recent(preferred_state.last_heartbeat_at)
                else (state.last_error if state and self._is_recent(state.last_heartbeat_at) else "")
            )
            if not last_error and browser_session:
                last_error = browser_session.last_error
            if not last_error and summary_browser_session:
                last_error = summary_browser_session.last_error

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
                    "last_heartbeat_at": _isoformat(last_heartbeat_at),
                    "last_error": last_error,
                    "extension_client_id": (
                        preferred_client.client_id
                        if preferred_client
                        else (
                            preferred_state.client_id
                            if preferred_state
                            else (
                                client.client_id
                                if client
                                else (
                                    session_client.client_id
                                    if session_client
                                    else (
                                        browser_session.client_id
                                        if browser_session
                                        else (
                                            summary_browser_session.extension_client_id
                                            if summary_browser_session
                                            else ""
                                        )
                                    )
                                )
                            )
                        )
                    ),
                }
            )
        return items

    def list_upload_jobs(
        self,
        *,
        status: str = "",
        platform: str = "",
        limit: int = 30,
        include_deleted: bool = False,
    ) -> list[dict[str, Any]]:
        normalized_status = str(status or "").strip()
        normalized_platform = str(platform or "").strip()
        normalized_limit = max(1, min(int(limit or 30), 100))
        with self.session_factory() as session:
            stmt = select(PublisherUploadJob).order_by(PublisherUploadJob.updated_at.desc())
            if not include_deleted:
                stmt = stmt.where(PublisherUploadJob.deleted_at.is_(None))
            if normalized_status:
                stmt = stmt.where(PublisherUploadJob.status == normalized_status)
            if normalized_platform:
                stmt = stmt.where(PublisherUploadJob.platform_id == normalized_platform)
            jobs = session.execute(stmt.limit(normalized_limit)).scalars().all()
            return [self._serialize_upload_job(job) for job in jobs]

    def create_upload_job(
        self,
        *,
        project_id: str = "",
        platform: str,
        book_name: str,
        chapter_title: str,
        body: str,
        upload_url: str | None,
        publish: bool,
        create_if_missing: bool = False,
        book_meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        spec = self._spec(platform)
        job_payload: dict[str, Any] = {}
        if create_if_missing:
            job_payload["create_if_missing"] = True
        normalized_book_meta = self._normalize_book_meta(book_meta)
        if normalized_book_meta:
            job_payload["book_meta"] = normalized_book_meta
        with self.session_factory() as session:
            resolved_project_id = self._resolve_project_id(
                session,
                explicit_project_id=project_id,
                work_name=book_name,
            )
            if resolved_project_id:
                job_payload["project_id"] = resolved_project_id
            job = PublisherUploadJob(
                project_id=resolved_project_id,
                platform_id=platform,
                status="pending",
                book_name=book_name,
                chapter_title=chapter_title,
                body_text=body,
                upload_url=upload_url or "",
                publish=publish,
                abort_requested=False,
                result_message=f"{spec.display_name} 上传任务已创建，等待浏览器扩展执行。",
                result_payload_json=json.dumps(job_payload, ensure_ascii=False),
            )
            session.add(job)
            session.commit()
            session.refresh(job)
            return self._serialize_upload_job(job)

    def get_upload_job(self, job_id: str) -> dict[str, Any]:
        with self.session_factory() as session:
            job = session.get(PublisherUploadJob, job_id)
            if job is None or job.deleted_at is not None:
                raise ValueError("上传任务不存在。")
            return self._serialize_upload_job(job)

    def terminate_upload_job(self, job_id: str) -> dict[str, Any]:
        now = _utc_now()
        with self.session_factory() as session:
            job = session.get(PublisherUploadJob, job_id)
            if job is None or job.deleted_at is not None:
                raise ValueError("上传任务不存在。")
            if job.status in {"succeeded", "failed", "cancelled"}:
                raise ValueError("终态上传任务不能再次终止。")
            if job.status == "pending":
                job.status = "cancelled"
                job.abort_requested = True
                job.finished_at = now
                job.result_message = "上传任务已在排队阶段取消。"
                job.error_message = ""
            else:
                job.status = "terminating"
                job.abort_requested = True
                job.result_message = "已请求浏览器扩展终止上传任务，等待执行端确认。"
            session.commit()
            session.refresh(job)
            return self._serialize_upload_job(job)

    def delete_upload_job(self, job_id: str) -> dict[str, Any]:
        now = _utc_now()
        with self.session_factory() as session:
            job = session.get(PublisherUploadJob, job_id)
            if job is None or job.deleted_at is not None:
                raise ValueError("上传任务不存在。")
            if job.status not in {"succeeded", "failed", "cancelled"}:
                raise ValueError("运行中的上传任务不能删除，请先终止。")
            job.deleted_at = now
            session.commit()
            session.refresh(job)
            return self._serialize_upload_job(job)

    def record_browser_session(
        self,
        *,
        client_id: str,
        platform: str,
        cookies: list[dict[str, Any]],
    ) -> dict[str, Any]:
        spec = self._spec(platform)
        now = _utc_now()
        normalized = [
            self._normalize_cookie(item)
            for item in cookies
            if isinstance(item, dict) and str(item.get("name", "")).strip()
        ]
        with self.session_factory() as session:
            self._ensure_extension_client(session, client_id)
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
            entry.cookies_json = json.dumps(normalized, ensure_ascii=False)
            entry.synced_at = now
            entry.last_error = ""

            stored = session.get(PublisherBrowserSession, platform)
            if stored is None:
                stored = PublisherBrowserSession(platform_id=platform)
                session.add(stored)
            stored.extension_client_id = client_id
            stored.cookie_count = len(normalized)
            stored.cookies_json = json.dumps(normalized, ensure_ascii=False)
            stored.synced_at = now
            stored.last_error = ""
            connected = self._is_browser_session_connected(
                platform,
                stored.cookies_json,
                "",
            )

            state = session.get(PublisherConnectionState, platform)
            if state is None:
                state = PublisherConnectionState(platform_id=platform)
                session.add(state)
            state.extension_client_id = client_id
            state.connected = connected
            state.login_method = state.login_method or "scan"
            if state.connected:
                state.last_error = ""
            state_payload = {
                "platform": platform,
                "connected": connected,
                "login_method": state.login_method,
                "last_error": state.last_error,
                "cookie_names": self._cookie_names_from_json(stored.cookies_json),
                "cookie_signal": connected,
                "session_synced": True,
            }
            state.status_json = json.dumps(state_payload, ensure_ascii=False)
            state.last_heartbeat_at = now
            self._upsert_extension_platform_state(
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
        return {
            "ok": True,
            "message": f"{spec.display_name} 浏览器会话已同步到后端。",
            "server_time": _isoformat(now),
            "cookie_count": len(normalized),
        }

    def get_browser_session(self, platform: str) -> dict[str, Any] | None:
        self._spec(platform)
        with self.session_factory() as session:
            entries = session.execute(
                select(PublisherBrowserSessionEntry).where(
                    PublisherBrowserSessionEntry.platform_id == platform
                )
            ).scalars().all()
            selected_entry = self._pick_browser_session_entry(entries)
            if selected_entry is not None:
                return {
                    "platform": selected_entry.platform_id,
                    "client_id": selected_entry.client_id,
                    "cookie_count": selected_entry.cookie_count,
                    "cookies": json.loads(selected_entry.cookies_json or "[]"),
                    "synced_at": _isoformat(selected_entry.synced_at),
                    "last_error": selected_entry.last_error,
                }

            stored = session.get(PublisherBrowserSession, platform)
            if stored is None:
                return None
            return {
                "platform": stored.platform_id,
                "client_id": stored.extension_client_id,
                "cookie_count": stored.cookie_count,
                "cookies": json.loads(stored.cookies_json or "[]"),
                "synced_at": _isoformat(stored.synced_at),
                "last_error": stored.last_error,
            }

    def has_browser_session(self, platform: str) -> bool:
        payload = self.get_browser_session(platform)
        if not payload or not payload.get("cookies"):
            return False
        return self._is_browser_session_connected(
            platform,
            json.dumps(payload["cookies"], ensure_ascii=False),
            str(payload.get("last_error", "")).strip(),
        )

    def mark_browser_session_result(
        self,
        *,
        platform: str,
        last_error: str = "",
        verified: bool = False,
    ) -> None:
        now = _utc_now()
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
            selected_entry = self._pick_browser_session_entry(entries)
            if stored is None and selected_entry is None:
                return
            if selected_entry is not None:
                selected_entry.last_error = last_error.strip()
                if verified:
                    selected_entry.last_verified_at = now
            session.commit()

    def claim_next_upload_job(
        self,
        *,
        client_id: str,
        connected_platforms: list[str],
    ) -> dict[str, Any] | None:
        platforms = [
            platform
            for platform in connected_platforms
            if platform in SUPPORTED_PLATFORMS
        ]
        if not platforms:
            return None

        now = _utc_now()
        with self.session_factory() as session:
            job = session.execute(
                select(PublisherUploadJob)
                .where(
                    PublisherUploadJob.status.in_(["running", "terminating"]),
                    PublisherUploadJob.finished_at.is_(None),
                    PublisherUploadJob.deleted_at.is_(None),
                    PublisherUploadJob.extension_client_id == client_id,
                    PublisherUploadJob.platform_id.in_(platforms),
                )
                .order_by(PublisherUploadJob.started_at.asc(), PublisherUploadJob.created_at.asc())
                .limit(1)
            ).scalar_one_or_none()
            if job is not None:
                return self._serialize_upload_job(job)

            claimable_platforms = self._claimable_platforms(
                session,
                client_id=client_id,
                platforms=platforms,
            )
            if not claimable_platforms:
                return None

            while True:
                job = session.execute(
                    select(PublisherUploadJob)
                    .where(
                        PublisherUploadJob.status == "pending",
                        PublisherUploadJob.abort_requested.is_(False),
                        PublisherUploadJob.deleted_at.is_(None),
                        PublisherUploadJob.platform_id.in_(claimable_platforms),
                    )
                    .order_by(PublisherUploadJob.created_at.asc())
                    .limit(1)
                ).scalar_one_or_none()
                if job is None:
                    return None

                claimed_at = job.claimed_at or now
                started_at = job.started_at or now
                claimed = session.execute(
                    update(PublisherUploadJob)
                    .where(
                        PublisherUploadJob.id == job.id,
                        PublisherUploadJob.status == "pending",
                        PublisherUploadJob.abort_requested.is_(False),
                        PublisherUploadJob.deleted_at.is_(None),
                    )
                    .values(
                        status="running",
                        extension_client_id=client_id,
                        claimed_at=claimed_at,
                        started_at=started_at,
                        abort_requested=False,
                        result_message="上传任务已被浏览器扩展自动领取。",
                        error_message="",
                    )
                )
                if not claimed.rowcount:
                    session.rollback()
                    continue

                session.commit()
                session.refresh(job)
                return self._serialize_upload_job(job)
            return None

    def claim_next_comment_sync_job(
        self,
        *,
        client_id: str,
        connected_platforms: list[str],
    ) -> dict[str, Any] | None:
        platforms = [
            platform
            for platform in connected_platforms
            if platform in SUPPORTED_PLATFORMS
        ]
        if not platforms:
            return None

        now = _utc_now()
        with self.session_factory() as session:
            job = session.execute(
                select(PublisherCommentSyncJob)
                .where(
                    PublisherCommentSyncJob.status == "running",
                    PublisherCommentSyncJob.finished_at.is_(None),
                    PublisherCommentSyncJob.extension_client_id == client_id,
                    PublisherCommentSyncJob.platform_id.in_(platforms),
                )
                .order_by(
                    PublisherCommentSyncJob.started_at.asc(),
                    PublisherCommentSyncJob.created_at.asc(),
                )
                .limit(1)
            ).scalar_one_or_none()
            if job is not None:
                return self._serialize_comment_sync_job(job)

            claimable_platforms = self._claimable_platforms(
                session,
                client_id=client_id,
                platforms=platforms,
            )
            if not claimable_platforms:
                return None

            while True:
                job = session.execute(
                    select(PublisherCommentSyncJob)
                    .where(
                        PublisherCommentSyncJob.status == "pending",
                        PublisherCommentSyncJob.platform_id.in_(claimable_platforms),
                    )
                    .order_by(PublisherCommentSyncJob.created_at.asc())
                    .limit(1)
                ).scalar_one_or_none()
                if job is None:
                    return None

                started_at = job.started_at or now
                claimed = session.execute(
                    update(PublisherCommentSyncJob)
                    .where(
                        PublisherCommentSyncJob.id == job.id,
                        PublisherCommentSyncJob.status == "pending",
                    )
                    .values(
                        status="running",
                        extension_client_id=client_id,
                        started_at=started_at,
                        error_message="",
                        result_summary_json=json.dumps(
                            {"phase": "claimed", "message": "评论同步任务已被浏览器扩展自动领取。"},
                            ensure_ascii=False,
                        ),
                    )
                )
                if not claimed.rowcount:
                    session.rollback()
                    continue

                session.commit()
                session.refresh(job)
                return self._serialize_comment_sync_job(job)
            return None

    def requeue_interrupted_upload_jobs(self) -> list[str]:
        now = _utc_now()
        recovered_platforms: set[str] = set()
        with self.session_factory() as session:
            jobs = session.execute(
                select(PublisherUploadJob).where(
                    PublisherUploadJob.status.in_(["running", "terminating"]),
                    PublisherUploadJob.finished_at.is_(None),
                    PublisherUploadJob.deleted_at.is_(None),
                )
            ).scalars().all()
            for job in jobs:
                if job.abort_requested:
                    job.status = "cancelled"
                    job.finished_at = now
                    job.result_message = "服务重启时检测到终止请求，任务已取消。"
                else:
                    job.status = "pending"
                    job.started_at = None
                    job.extension_client_id = ""
                    job.current_url = ""
                    job.error_message = ""
                    job.result_payload_json = json.dumps(
                        {"phase": "requeued-after-restart"},
                        ensure_ascii=False,
                    )
                    job.result_message = "服务重启后，上传任务已重新排队。"
                recovered_platforms.add(job.platform_id)

                state = session.get(PublisherConnectionState, job.platform_id)
                if state is not None:
                    state.last_heartbeat_at = now
            session.commit()
        return sorted(recovered_platforms)

    def record_extension_heartbeat(
        self,
        *,
        client_id: str,
        extension_version: str,
        browser_name: str,
        browser_version: str,
        backend_base_url: str,
        platforms: list[dict[str, Any]],
    ) -> dict[str, Any]:
        now = _utc_now()
        with self.session_factory() as session:
            client = self._ensure_extension_client(session, client_id)

            client.extension_version = extension_version
            client.browser_name = browser_name
            client.browser_version = browser_version
            client.backend_base_url = backend_base_url
            client.last_heartbeat_at = now

            for item in platforms:
                platform_id = str(item.get("platform", "")).strip()
                if not platform_id or platform_id not in SUPPORTED_PLATFORMS:
                    continue
                state = session.get(PublisherConnectionState, platform_id)
                if state is None:
                    state = PublisherConnectionState(platform_id=platform_id)
                    session.add(state)
                state.extension_client_id = client_id
                cookie_signal = bool(item.get("cookie_signal"))
                state.connected = bool(item.get("connected")) or cookie_signal
                state.login_method = str(item.get("login_method", "")).strip()
                state.last_error = str(item.get("last_error", "")).strip()
                state.status_json = json.dumps(item, ensure_ascii=False)
                state.last_heartbeat_at = now
                self._upsert_extension_platform_state(
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

        return {"ok": True, "message": "扩展心跳已记录。", "server_time": _isoformat(now)}

    def update_upload_job_result(
        self,
        *,
        job_id: str,
        client_id: str,
        status: str,
        message: str,
        current_url: str,
        error: str,
        result_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if status not in {"running", "succeeded", "failed", "cancelled"}:
            raise ValueError("不支持的上传任务状态。")

        now = _utc_now()
        with self.session_factory() as session:
            job = session.get(PublisherUploadJob, job_id)
            if job is None or job.deleted_at is not None:
                raise ValueError("上传任务不存在。")

            self._ensure_extension_client(session, client_id)
            if client_id:
                job.extension_client_id = client_id
            effective_status = status
            if job.abort_requested and status in {"succeeded", "failed", "cancelled"}:
                effective_status = "cancelled"
            elif job.abort_requested and status == "running":
                effective_status = "terminating"

            if effective_status == "running":
                job.claimed_at = job.claimed_at or now
                job.started_at = job.started_at or now
                job.error_message = ""
            elif effective_status in {"succeeded", "failed", "cancelled"}:
                job.started_at = job.started_at or now
                job.finished_at = now

            job.status = effective_status
            job.current_url = current_url
            job.result_message = (
                "上传任务已取消。"
                if effective_status == "cancelled"
                else message
            )
            job.error_message = "" if effective_status == "cancelled" else error
            merged_payload = {}
            try:
                merged_payload = json.loads(job.result_payload_json or "{}")
            except json.JSONDecodeError:
                merged_payload = {}
            if not isinstance(merged_payload, dict):
                merged_payload = {}
            if result_payload:
                merged_payload.update(result_payload)
            job.result_payload_json = json.dumps(merged_payload, ensure_ascii=False)

            if job.platform_id in SUPPORTED_PLATFORMS:
                state = session.get(PublisherConnectionState, job.platform_id)
                if state is None:
                    state = PublisherConnectionState(platform_id=job.platform_id)
                    session.add(state)
                if client_id:
                    state.extension_client_id = client_id
                state.last_heartbeat_at = now
                if effective_status == "succeeded":
                    state.connected = True
                    state.last_error = ""
                elif effective_status == "failed" and current_url and "login" in current_url:
                    state.connected = False
                    state.last_error = error or message
                self._upsert_extension_platform_state(
                    session,
                    client_id=client_id,
                    platform_id=job.platform_id,
                    connected=state.connected,
                    login_method=state.login_method,
                    last_error=state.last_error,
                    status_payload={
                        "platform": job.platform_id,
                        "connected": state.connected,
                        "login_method": state.login_method,
                        "last_error": state.last_error,
                        "source": "upload-job-result",
                    },
                    last_heartbeat_at=now,
                )

            session.commit()
            session.refresh(job)
            return self._serialize_upload_job(job)

    def create_comment_sync_job(
        self,
        *,
        project_id: str = "",
        platform: str,
        work_id: str,
        work_name: str,
        chapter_id: str,
        chapter_title: str,
        limit: int,
    ) -> dict[str, Any]:
        self._spec(platform)
        with self.session_factory() as session:
            job = PublisherCommentSyncJob(
                project_id=self._resolve_project_id(
                    session,
                    explicit_project_id=project_id,
                    work_name=work_name,
                ),
                platform_id=platform,
                status="pending",
                work_id=work_id,
                work_name=work_name,
                chapter_id=chapter_id,
                chapter_title=chapter_title,
                limit=limit,
            )
            session.add(job)
            session.commit()
            session.refresh(job)
            return {
                "job_id": job.id,
                "project_id": job.project_id,
                "platform": job.platform_id,
                "status": job.status,
                "work_id": job.work_id,
                "work_name": job.work_name,
                "chapter_id": job.chapter_id,
                "chapter_title": job.chapter_title,
                "limit": job.limit,
                "created_at": _isoformat(job.created_at),
            }

    def update_comment_sync_job_result(
        self,
        *,
        job_id: str,
        client_id: str,
        status: str,
        message: str,
        error: str,
        result_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if status not in {"running", "succeeded", "failed"}:
            raise ValueError("不支持的评论同步任务状态。")

        now = _utc_now()
        with self.session_factory() as session:
            job = session.get(PublisherCommentSyncJob, job_id)
            if job is None:
                raise ValueError("评论同步任务不存在。")

            self._ensure_extension_client(session, client_id)
            if client_id:
                job.extension_client_id = client_id
            if status == "running":
                job.started_at = job.started_at or now
                job.error_message = ""
            elif status in {"succeeded", "failed"}:
                job.started_at = job.started_at or now
                job.finished_at = now

            merged_payload: dict[str, Any]
            try:
                merged_payload = json.loads(job.result_summary_json or "{}")
            except json.JSONDecodeError:
                merged_payload = {}
            if not isinstance(merged_payload, dict):
                merged_payload = {}
            merged_payload.update({
                "message": str(message or "").strip(),
                "status": status,
            })
            if result_payload:
                merged_payload.update(result_payload)

            job.status = status
            job.error_message = str(error or "").strip()
            job.result_summary_json = json.dumps(merged_payload, ensure_ascii=False)

            session.commit()
            session.refresh(job)
            return self._serialize_comment_sync_job(job)

    def ingest_comments_batch(
        self,
        *,
        client_id: str,
        platform: str,
        comments: list[dict[str, Any]],
        job_id: str = "",
    ) -> dict[str, Any]:
        self._spec(platform)
        now = _utc_now()
        inserted = 0
        updated = 0

        with self.session_factory() as session:
            self._ensure_extension_client(session, client_id)
            resolved_job_project_id = ""
            if job_id:
                job = session.get(PublisherCommentSyncJob, job_id)
                if job is not None:
                    job.extension_client_id = client_id
                    job.status = "running"
                    job.started_at = job.started_at or now
                    resolved_job_project_id = str(job.project_id or "").strip()

            remote_ids = [
                str(item.get("remote_comment_id", "")).strip()
                for item in comments
                if str(item.get("remote_comment_id", "")).strip()
            ]
            existing_rows = (
                session.execute(
                    select(PublisherRawComment).where(
                        PublisherRawComment.platform_id == platform,
                        PublisherRawComment.remote_comment_id.in_(remote_ids),
                    )
                ).scalars().all()
                if remote_ids
                else []
            )
            row_map = {
                row.remote_comment_id: row
                for row in existing_rows
            }
            valid_project_ids = set()
            unique_project_ids_by_work_name: dict[str, str] = {}
            if not resolved_job_project_id:
                explicit_project_ids = {
                    str(item.get("project_id", "")).strip()
                    for item in comments
                    if str(item.get("project_id", "")).strip()
                }
                if explicit_project_ids:
                    valid_project_ids = set(
                        session.execute(
                            select(Project.id).where(Project.id.in_(explicit_project_ids))
                        ).scalars().all()
                    )
                work_names = {
                    str(item.get("work_name", "")).strip()
                    for item in comments
                    if str(item.get("work_name", "")).strip()
                }
                if work_names:
                    title_rows = session.execute(
                        select(Project.title, Project.id)
                        .where(Project.title.in_(work_names))
                        .order_by(Project.title.asc(), Project.id.asc())
                    ).all()
                    title_matches: dict[str, list[str]] = {}
                    for title, project_id in title_rows:
                        normalized_title = str(title or "").strip()
                        normalized_project_id = str(project_id or "").strip()
                        if not normalized_title or not normalized_project_id:
                            continue
                        title_matches.setdefault(normalized_title, []).append(normalized_project_id)
                    unique_project_ids_by_work_name = {
                        title: project_ids[0]
                        for title, project_ids in title_matches.items()
                        if len(project_ids) == 1
                    }

            for item in comments:
                remote_comment_id = str(item.get("remote_comment_id", "")).strip()
                if not remote_comment_id:
                    continue
                row = row_map.get(remote_comment_id)
                if row is None:
                    row = PublisherRawComment(
                        project_id=resolved_job_project_id,
                        platform_id=platform,
                        remote_comment_id=remote_comment_id,
                    )
                    session.add(row)
                    row_map[remote_comment_id] = row
                    inserted += 1
                else:
                    updated += 1

                explicit_project_id = str(item.get("project_id", "")).strip()
                work_name = str(item.get("work_name", "")).strip()
                if resolved_job_project_id:
                    row.project_id = resolved_job_project_id
                elif explicit_project_id and explicit_project_id in valid_project_ids:
                    row.project_id = explicit_project_id
                else:
                    row.project_id = unique_project_ids_by_work_name.get(work_name, "")
                row.work_id = str(item.get("work_id", "")).strip()
                row.work_name = work_name
                row.chapter_id = str(item.get("chapter_id", "")).strip()
                row.chapter_title = str(item.get("chapter_title", "")).strip()
                row.author_id = str(item.get("author_id", "")).strip()
                row.author_name = str(item.get("author_name", "")).strip()
                row.body_text = str(item.get("body", "")).strip()
                row.parent_remote_comment_id = str(item.get("parent_remote_comment_id", "")).strip()
                row.remote_created_at = str(item.get("created_at", "")).strip()
                row.like_count = max(0, _as_int(item.get("like_count", 0)))
                row.reply_count = max(0, _as_int(item.get("reply_count", 0)))
                row.raw_payload_json = json.dumps(item.get("raw_payload", item), ensure_ascii=False)
                row.synced_at = now

            if job_id:
                job = session.get(PublisherCommentSyncJob, job_id)
                if job is not None:
                    job.status = "succeeded"
                    job.finished_at = now
                    job.result_summary_json = json.dumps(
                        {"inserted": inserted, "updated": updated},
                        ensure_ascii=False,
                    )

            if platform in SUPPORTED_PLATFORMS:
                state = session.get(PublisherConnectionState, platform)
                if state is None:
                    state = PublisherConnectionState(platform_id=platform)
                    session.add(state)
                state.extension_client_id = client_id
                state.last_heartbeat_at = now
                self._upsert_extension_platform_state(
                    session,
                    client_id=client_id,
                    platform_id=platform,
                    connected=bool(state.connected),
                    login_method=state.login_method,
                    last_error=state.last_error,
                    status_payload={
                        "platform": platform,
                        "connected": bool(state.connected),
                        "login_method": state.login_method,
                        "last_error": state.last_error,
                        "source": "comment-batch-ingest",
                    },
                    last_heartbeat_at=now,
                )

            session.commit()

        return {
            "ok": True,
            "message": "评论批次已入库。",
            "inserted": inserted,
            "updated": updated,
            "synced_at": _isoformat(now),
        }

    def verify_extension_api_key(self, provided_key: str | None) -> None:
        if not self.extension_api_key:
            raise RuntimeError("后端尚未配置扩展 API Key。")
        if not provided_key or provided_key != self.extension_api_key:
            raise ValueError("扩展鉴权失败。")

    def backend_ready_payload(self) -> dict[str, Any]:
        return {"extension_api_key_configured": bool(self.extension_api_key)}

    @staticmethod
    def _normalize_cookie(cookie: dict[str, Any]) -> dict[str, Any]:
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

    @staticmethod
    def _normalize_book_meta(book_meta: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(book_meta, dict):
            return {}

        def normalized_tags(key: str) -> list[str]:
            values = book_meta.get(key, [])
            if not isinstance(values, list):
                return []
            return [str(item).strip() for item in values if str(item).strip()]

        normalized: dict[str, Any] = {}
        audience = str(book_meta.get("audience", "")).strip()
        if audience:
            normalized["audience"] = audience
        primary_category = str(book_meta.get("primary_category", "")).strip()
        if primary_category:
            normalized["primary_category"] = primary_category
        protagonist_names = normalized_tags("protagonist_names")[:2]
        if protagonist_names:
            normalized["protagonist_names"] = protagonist_names
        intro = str(book_meta.get("intro", "")).strip()
        if intro:
            normalized["intro"] = intro
        for key in ("theme_tags", "role_tags", "plot_tags"):
            tags = normalized_tags(key)[:2]
            if tags:
                normalized[key] = tags
        return normalized

    def _serialize_upload_job(self, job: PublisherUploadJob) -> dict[str, Any]:
        spec = self._spec(job.platform_id)
        payload = json.loads(job.result_payload_json or "{}")
        terminal = job.status in {"succeeded", "failed", "cancelled"}
        return {
            "job_id": job.id,
            "project_id": job.project_id,
            "platform": job.platform_id,
            "display_name": spec.display_name,
            "status": job.status,
            "book_name": job.book_name,
            "chapter_title": job.chapter_title,
            "body": job.body_text,
            "upload_url": job.upload_url or None,
            "publish": bool(job.publish),
            "extension_client_id": job.extension_client_id,
            "current_url": job.current_url,
            "message": job.result_message,
            "error": job.error_message,
            "result_payload": payload,
            "abort_requested": bool(job.abort_requested),
            "created_at": _isoformat(job.created_at),
            "updated_at": _isoformat(job.updated_at),
            "claimed_at": _isoformat(job.claimed_at),
            "started_at": _isoformat(job.started_at),
            "finished_at": _isoformat(job.finished_at),
            "terminable": bool(job.deleted_at is None and not terminal and not job.abort_requested),
            "deletable": bool(job.deleted_at is None and terminal),
        }

    def _serialize_comment_sync_job(self, job: PublisherCommentSyncJob) -> dict[str, Any]:
        payload = json.loads(job.result_summary_json or "{}")
        return {
            "job_id": job.id,
            "project_id": job.project_id,
            "platform": job.platform_id,
            "status": job.status,
            "work_id": job.work_id,
            "work_name": job.work_name,
            "chapter_id": job.chapter_id,
            "chapter_title": job.chapter_title,
            "limit": int(job.limit or 0),
            "extension_client_id": job.extension_client_id,
            "message": str(payload.get("message", "")).strip(),
            "error": job.error_message,
            "result_payload": payload,
            "created_at": _isoformat(job.created_at),
            "started_at": _isoformat(job.started_at),
            "finished_at": _isoformat(job.finished_at),
        }

    @staticmethod
    def _resolve_project_id(
        session,
        *,
        explicit_project_id: str = "",
        work_name: str = "",
    ) -> str:
        normalized_project_id = str(explicit_project_id or "").strip()
        if normalized_project_id:
            if session.get(Project, normalized_project_id) is not None:
                return normalized_project_id
        normalized_work_name = str(work_name or "").strip()
        if not normalized_work_name:
            return ""
        matches = session.execute(
            select(Project.id).where(Project.title == normalized_work_name).limit(2)
        ).scalars().all()
        if len(matches) == 1:
            return str(matches[0] or "").strip()
        return ""

    def _client_can_claim_platform(
        self,
        session,
        *,
        client_id: str,
        platform_id: str,
    ) -> bool:
        preferred_client_id = self.preferred_client_id
        if not preferred_client_id or client_id == preferred_client_id:
            return True
        return not self._preferred_client_can_claim_platform(
            session,
            platform_id=platform_id,
        )

    def _claimable_platforms(
        self,
        session,
        *,
        client_id: str,
        platforms: list[str],
    ) -> list[str]:
        if not platforms:
            return []
        preferred_client_id = self.preferred_client_id
        if not preferred_client_id or client_id == preferred_client_id:
            return list(platforms)
        return [
            platform_id
            for platform_id in platforms
            if not self._preferred_client_can_claim_platform(
                session,
                platform_id=platform_id,
            )
        ]

    def _preferred_client_can_claim_platform(self, session, *, platform_id: str) -> bool:
        preferred_client_id = self.preferred_client_id
        if not preferred_client_id:
            return False
        client = session.get(PublisherExtensionClient, preferred_client_id)
        if client is None or not self._is_recent(client.last_heartbeat_at):
            return False
        platform_state = session.get(
            PublisherExtensionPlatformState,
            {
                "client_id": preferred_client_id,
                "platform_id": platform_id,
            },
        )
        return bool(platform_state and platform_state.connected)

    @staticmethod
    def _upsert_extension_platform_state(
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

    def _is_recent(self, value: datetime | None) -> bool:
        parsed = _as_utc(value)
        if parsed is None:
            return False
        return parsed >= (_utc_now() - timedelta(seconds=self.heartbeat_stale_seconds))

    @staticmethod
    def _ensure_extension_client(
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

    def _is_browser_session_connected(
        self,
        platform: str,
        cookies_json: str,
        last_error: str,
    ) -> bool:
        if str(last_error or "").strip() == "login-required":
            return False
        cookie_names = set(self._cookie_names_from_json(cookies_json))
        if platform == "qidian":
            return "AppAuthToken" in cookie_names and bool(
                {"pubtoken", "ywopenid", "ywkey", "ywKey", "ywtab"} & cookie_names
            )
        if platform == "fanqie":
            has_session = bool({"sessionid", "sessionid_ss"} & cookie_names)
            has_writer_signal = bool(
                {"has_biz_token", "passport_auth_status", "passport_auth_status_ss", "sid_tt"} & cookie_names
            )
            return has_session and has_writer_signal
        return bool(cookie_names)

    @staticmethod
    def _cookie_names_from_json(cookies_json: str) -> list[str]:
        try:
            cookies = json.loads(cookies_json or "[]")
        except json.JSONDecodeError:
            return []
        names = [
            str(item.get("name", "")).strip()
            for item in cookies
            if isinstance(item, dict) and str(item.get("name", "")).strip()
        ]
        return sorted(dict.fromkeys(names))

    def _spec(self, platform: str) -> PlatformSpec:
        spec = SUPPORTED_PLATFORMS.get(platform)
        if spec is None:
            raise ValueError(f"不支持的平台: {platform}")
        return spec
