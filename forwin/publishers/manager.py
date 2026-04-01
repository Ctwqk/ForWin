from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from forwin.models.publisher import (
    PublisherBrowserSession,
    PublisherCommentSyncJob,
    PublisherConnectionState,
    PublisherExtensionClient,
    PublisherRawComment,
    PublisherUploadJob,
)

from .platforms import SUPPORTED_PLATFORMS, PlatformSpec


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
    return parsed.isoformat().replace("+00:00", "Z")


class PublisherManager:
    def __init__(
        self,
        session_factory: sessionmaker,
        extension_api_key: str = "",
        heartbeat_stale_seconds: int = 90,
    ) -> None:
        self.session_factory = session_factory
        self.extension_api_key = extension_api_key
        self.heartbeat_stale_seconds = heartbeat_stale_seconds

    def list_platforms(self) -> list[dict[str, Any]]:
        with self.session_factory() as session:
            states = {
                item.platform_id: item
                for item in session.execute(select(PublisherConnectionState)).scalars()
            }
            clients = {
                item.client_id: item
                for item in session.execute(select(PublisherExtensionClient)).scalars()
            }
            browser_sessions = {
                item.platform_id: item
                for item in session.execute(select(PublisherBrowserSession)).scalars()
            }

        items: list[dict[str, Any]] = []
        for spec in SUPPORTED_PLATFORMS.values():
            state = states.get(spec.platform_id)
            browser_session = browser_sessions.get(spec.platform_id)
            client = (
                clients.get(state.extension_client_id)
                if state and state.extension_client_id
                else None
            )
            session_client = (
                clients.get(browser_session.extension_client_id)
                if browser_session and browser_session.extension_client_id
                else None
            )
            last_heartbeat_at = None
            if client and client.last_heartbeat_at:
                last_heartbeat_at = client.last_heartbeat_at
            elif session_client and session_client.last_heartbeat_at:
                last_heartbeat_at = session_client.last_heartbeat_at
            elif state and state.last_heartbeat_at:
                last_heartbeat_at = state.last_heartbeat_at

            extension_online = self._is_recent(last_heartbeat_at)
            connected = False
            if state and state.connected:
                connected = True
            elif browser_session and self._is_browser_session_connected(
                spec.platform_id,
                browser_session.cookies_json,
                browser_session.last_error,
            ):
                connected = True

            last_error = state.last_error if state else ""
            if not last_error and browser_session:
                last_error = browser_session.last_error

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
                        client.client_id
                        if client
                        else (
                            session_client.client_id
                            if session_client
                            else (browser_session.extension_client_id if browser_session else "")
                        )
                    ),
                }
            )
        return items

    def create_upload_job(
        self,
        *,
        platform: str,
        book_name: str,
        chapter_title: str,
        body: str,
        upload_url: str | None,
        publish: bool,
    ) -> dict[str, Any]:
        spec = self._spec(platform)
        with self.session_factory() as session:
            job = PublisherUploadJob(
                platform_id=platform,
                status="pending",
                book_name=book_name,
                chapter_title=chapter_title,
                body_text=body,
                upload_url=upload_url or "",
                publish=publish,
                result_message=f"{spec.display_name} 上传任务已创建，等待浏览器扩展执行。",
            )
            session.add(job)
            session.commit()
            session.refresh(job)
            return self._serialize_upload_job(job)

    def claim_upload_job_for_server(
        self,
        *,
        job_id: str,
        client_id: str,
    ) -> dict[str, Any] | None:
        now = _utc_now()
        with self.session_factory() as session:
            job = session.get(PublisherUploadJob, job_id)
            if job is None or job.status != "pending":
                return None

            job.status = "running"
            job.extension_client_id = client_id
            job.claimed_at = job.claimed_at or now
            job.started_at = job.started_at or now
            job.result_message = "后端正在使用已同步会话执行上传。"
            job.error_message = ""

            session.commit()
            session.refresh(job)
            return self._serialize_upload_job(job)

    def claim_pending_upload_jobs_for_server(
        self,
        *,
        platform: str,
        client_id: str,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        self._spec(platform)
        now = _utc_now()
        claimed: list[dict[str, Any]] = []
        with self.session_factory() as session:
            jobs = session.execute(
                select(PublisherUploadJob)
                .where(
                    PublisherUploadJob.status == "pending",
                    PublisherUploadJob.platform_id == platform,
                )
                .order_by(PublisherUploadJob.created_at.asc())
                .limit(limit)
            ).scalars().all()

            for job in jobs:
                job.status = "running"
                job.extension_client_id = client_id
                job.claimed_at = job.claimed_at or now
                job.started_at = job.started_at or now
                job.result_message = "后端正在使用已同步会话执行上传。"
                job.error_message = ""

            session.commit()

            for job in jobs:
                session.refresh(job)
                claimed.append(self._serialize_upload_job(job))
        return claimed

    def get_upload_job(self, job_id: str) -> dict[str, Any]:
        with self.session_factory() as session:
            job = session.get(PublisherUploadJob, job_id)
            if job is None:
                raise ValueError("上传任务不存在。")
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
            stored = session.get(PublisherBrowserSession, platform)
            if stored is None:
                stored = PublisherBrowserSession(platform_id=platform)
                session.add(stored)
            stored.extension_client_id = client_id
            stored.cookie_count = len(normalized)
            stored.cookies_json = json.dumps(normalized, ensure_ascii=False)
            stored.synced_at = now
            stored.last_error = ""

            state = session.get(PublisherConnectionState, platform)
            if state is None:
                state = PublisherConnectionState(platform_id=platform)
                session.add(state)
            state.extension_client_id = client_id
            state.connected = self._is_browser_session_connected(
                platform,
                stored.cookies_json,
                "",
            )
            state.login_method = state.login_method or "scan"
            if state.connected:
                state.last_error = ""
            state.status_json = json.dumps(
                {
                    "platform": platform,
                    "connected": state.connected,
                    "login_method": state.login_method,
                    "last_error": state.last_error,
                    "cookie_names": self._cookie_names_from_json(stored.cookies_json),
                    "cookie_signal": state.connected,
                    "session_synced": True,
                },
                ensure_ascii=False,
            )
            state.last_heartbeat_at = now
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
        with self.session_factory() as session:
            stored = session.get(PublisherBrowserSession, platform)
            if stored is None:
                return
            stored.last_error = last_error.strip()
            if verified:
                stored.last_verified_at = _utc_now()
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
                    PublisherUploadJob.status == "pending",
                    PublisherUploadJob.platform_id.in_(platforms),
                )
                .order_by(PublisherUploadJob.created_at.asc())
                .limit(1)
            ).scalar_one_or_none()
            if job is None:
                return None

            job.status = "running"
            job.extension_client_id = client_id
            job.claimed_at = job.claimed_at or now
            job.started_at = job.started_at or now
            job.result_message = "上传任务已被浏览器扩展自动领取。"
            job.error_message = ""

            session.commit()
            session.refresh(job)
            return self._serialize_upload_job(job)

    def requeue_interrupted_upload_jobs(self) -> list[str]:
        now = _utc_now()
        recovered_platforms: set[str] = set()
        with self.session_factory() as session:
            jobs = session.execute(
                select(PublisherUploadJob).where(
                    PublisherUploadJob.status == "running",
                    PublisherUploadJob.finished_at.is_(None),
                )
            ).scalars().all()
            for job in jobs:
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
            client = session.get(PublisherExtensionClient, client_id)
            if client is None:
                client = PublisherExtensionClient(client_id=client_id)
                session.add(client)

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
        if status not in {"running", "succeeded", "failed"}:
            raise ValueError("不支持的上传任务状态。")

        now = _utc_now()
        with self.session_factory() as session:
            job = session.get(PublisherUploadJob, job_id)
            if job is None:
                raise ValueError("上传任务不存在。")

            if client_id:
                job.extension_client_id = client_id
            if status == "running":
                job.claimed_at = job.claimed_at or now
                job.started_at = job.started_at or now
                job.error_message = ""
            elif status in {"succeeded", "failed"}:
                job.started_at = job.started_at or now
                job.finished_at = now

            job.status = status
            job.current_url = current_url
            job.result_message = message
            job.error_message = error
            job.result_payload_json = json.dumps(result_payload or {}, ensure_ascii=False)

            if job.platform_id in SUPPORTED_PLATFORMS:
                state = session.get(PublisherConnectionState, job.platform_id)
                if state is None:
                    state = PublisherConnectionState(platform_id=job.platform_id)
                    session.add(state)
                if client_id:
                    state.extension_client_id = client_id
                state.last_heartbeat_at = now
                if status == "succeeded":
                    state.connected = True
                    state.last_error = ""
                elif status == "failed" and current_url and "login" in current_url:
                    state.connected = False
                    state.last_error = error or message

            session.commit()
            session.refresh(job)
            return self._serialize_upload_job(job)

    def create_comment_sync_job(
        self,
        *,
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
                "platform": job.platform_id,
                "status": job.status,
                "work_id": job.work_id,
                "work_name": job.work_name,
                "chapter_id": job.chapter_id,
                "chapter_title": job.chapter_title,
                "limit": job.limit,
                "created_at": _isoformat(job.created_at),
            }

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
            if job_id:
                job = session.get(PublisherCommentSyncJob, job_id)
                if job is not None:
                    job.extension_client_id = client_id
                    job.status = "running"
                    job.started_at = job.started_at or now

            for item in comments:
                remote_comment_id = str(item.get("remote_comment_id", "")).strip()
                if not remote_comment_id:
                    continue
                row = session.execute(
                    select(PublisherRawComment).where(
                        PublisherRawComment.platform_id == platform,
                        PublisherRawComment.remote_comment_id == remote_comment_id,
                    )
                ).scalar_one_or_none()
                if row is None:
                    row = PublisherRawComment(
                        platform_id=platform,
                        remote_comment_id=remote_comment_id,
                    )
                    session.add(row)
                    inserted += 1
                else:
                    updated += 1

                row.work_id = str(item.get("work_id", "")).strip()
                row.work_name = str(item.get("work_name", "")).strip()
                row.chapter_id = str(item.get("chapter_id", "")).strip()
                row.chapter_title = str(item.get("chapter_title", "")).strip()
                row.author_id = str(item.get("author_id", "")).strip()
                row.author_name = str(item.get("author_name", "")).strip()
                row.body_text = str(item.get("body", "")).strip()
                row.parent_remote_comment_id = str(item.get("parent_remote_comment_id", "")).strip()
                row.remote_created_at = str(item.get("created_at", "")).strip()
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

    def _serialize_upload_job(self, job: PublisherUploadJob) -> dict[str, Any]:
        spec = self._spec(job.platform_id)
        payload = json.loads(job.result_payload_json or "{}")
        return {
            "job_id": job.id,
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
            "created_at": _isoformat(job.created_at),
            "claimed_at": _isoformat(job.claimed_at),
            "started_at": _isoformat(job.started_at),
            "finished_at": _isoformat(job.finished_at),
        }

    def _is_recent(self, value: datetime | None) -> bool:
        parsed = _as_utc(value)
        if parsed is None:
            return False
        return parsed >= (_utc_now() - timedelta(seconds=self.heartbeat_stale_seconds))

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
