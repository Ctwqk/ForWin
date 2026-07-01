from __future__ import annotations

import json
import hashlib
import logging
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from forwin.models.publisher import (
    PublisherBrowserSessionEntry,
    PublisherCommentSyncJob,
    PublisherExtensionClient,
    PublisherCoverAsset,
    PublisherUploadJob,
    PublisherWorkBinding,
)
from forwin.publisher_runtime.audit import (
    comment_sync_event_type as _comment_sync_event_type,
    terminal_upload_event_type as _terminal_upload_event_type,
)
from forwin.publisher_runtime.auth import (
    PublisherExtensionAuthError,
    PublisherExtensionAuthNotConfigured,
)
from forwin.publisher_runtime.browser_sessions import (
    PublisherBrowserSessionDecodeError,
    as_utc as _as_utc,
    browser_session_sort_key,
    is_retryable_db_error as _is_retryable_db_error,
    isoformat as _isoformat,
    pick_browser_session_entry,
    pick_browser_sessions_by_platform,
    utc_now as _utc_now,
)
from forwin.publisher_runtime.connection_state import (
    ensure_extension_client,
    upsert_extension_platform_state,
)
from forwin.publisher_runtime.login_qr_notifications import DiscordLoginQrNotifier
from forwin.publisher_runtime.service import PublisherRuntimeService
from forwin.publisher_runtime.upload_jobs import CodexInterventionHandler
from forwin.publisher_runtime.platform_catalog import PlatformSpec

LOGIN_QR_NOTIFICATION_THROTTLE_SECONDS = 2 * 60

logger = logging.getLogger(__name__)


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _login_qr_throttle_url(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return raw[:500]
    if not parsed.scheme or not parsed.netloc:
        return raw[:500]
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))[:500]


def _login_qr_image_fingerprint(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class PublisherManager:
    def __init__(
        self,
        session_factory: sessionmaker,
        extension_api_key: str = "",
        heartbeat_stale_seconds: int = 90,
        preferred_client_id: str = "",
        strict_preferred_client: bool = False,
        publisher_session_secret: str = "",
        publisher_session_encryption_required: bool = False,
        publisher_login_discord_webhook_url: str = "",
        codex_intervention_handler: CodexInterventionHandler | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.extension_api_key = str(extension_api_key or "").strip()
        self.heartbeat_stale_seconds = heartbeat_stale_seconds
        self.preferred_client_id = str(preferred_client_id or "").strip()
        self.strict_preferred_client = bool(strict_preferred_client)
        self.publisher_session_secret = str(publisher_session_secret or "").strip()
        self.publisher_session_encryption_required = bool(
            publisher_session_encryption_required
        )
        self.login_qr_notifier = DiscordLoginQrNotifier(
            publisher_login_discord_webhook_url
        )
        self._login_qr_notification_throttle: dict[tuple[str, str], tuple[datetime, str]] = {}
        self._login_qr_one_shots: dict[str, dict[str, Any]] = {}
        self._login_qr_one_shot_exhausted_until: dict[str, datetime] = {}
        self.runtime = PublisherRuntimeService(
            session_factory=session_factory,
            extension_api_key=self.extension_api_key,
            heartbeat_stale_seconds=self.heartbeat_stale_seconds,
            preferred_client_id=self.preferred_client_id,
            publisher_session_secret=self.publisher_session_secret,
            publisher_session_encryption_required=self.publisher_session_encryption_required,
            strict_preferred_client=self.strict_preferred_client,
            codex_intervention_handler=codex_intervention_handler,
        )
        self._plaintext_cookie_storage_warned = (
            self.runtime.browser_cookie_codec._plaintext_cookie_storage_warned
        )
        self._install_compat_hooks()

    def _install_compat_hooks(self) -> None:
        self.runtime.connection_state.ensure_extension_client = (
            lambda session, client_id: self._ensure_extension_client(session, client_id)
        )

    def _sync_runtime_config(self) -> None:
        self.runtime.auth.extension_api_key = str(self.extension_api_key or "").strip()
        self.runtime.connection_state.heartbeat_stale_seconds = self.heartbeat_stale_seconds
        self.runtime.connection_state.preferred_client_id = str(
            self.preferred_client_id or ""
        ).strip()
        self.runtime.connection_state.strict_preferred_client = bool(
            self.strict_preferred_client
        )

    def list_platforms(self) -> list[dict[str, Any]]:
        self._sync_runtime_config()
        return self.runtime.connection_state.list_platforms()

    def start_login_qr_one_shot(
        self,
        *,
        platform: str,
        webhook_url: str,
        ttl_seconds: int = 300,
        max_dispatches: int = 1,
    ) -> dict[str, Any]:
        platform_id = str(platform or "").strip()
        webhook = str(webhook_url or "").strip()
        if not platform_id:
            raise ValueError("platform is required for login QR one-shot.")
        if not webhook:
            raise ValueError("webhook_url is required for login QR one-shot.")
        if not webhook.startswith("https://"):
            raise ValueError("login QR one-shot webhook_url must be an https URL.")
        ttl = max(30, min(_as_int(ttl_seconds, 300), 15 * 60))
        dispatches = max(1, min(_as_int(max_dispatches, 1), 3))
        now = _utc_now()
        expires_at = now + timedelta(seconds=ttl)
        self._login_qr_one_shot_exhausted_until.pop(platform_id, None)
        self._login_qr_one_shots[platform_id] = {
            "platform": platform_id,
            "webhook_url": webhook,
            "expires_at": expires_at,
            "remaining_dispatches": dispatches,
        }
        return {
            "ok": True,
            "message": "login QR one-shot enabled",
            "server_time": _isoformat(now),
            "platform": platform_id,
            "expires_at": _isoformat(expires_at),
            "allowed_until_ms": int(expires_at.timestamp() * 1000),
            "remaining_dispatches": dispatches,
            "login_qr_notifications_allowed": True,
        }

    def _active_login_qr_one_shot(self, platform_id: str, now: datetime) -> dict[str, Any] | None:
        one_shot = self._login_qr_one_shots.get(platform_id)
        if not one_shot:
            return None
        expires_at = one_shot.get("expires_at")
        if _as_int(one_shot.get("remaining_dispatches"), 0) <= 0:
            self._login_qr_one_shots.pop(platform_id, None)
            if isinstance(expires_at, datetime) and now < expires_at:
                self._login_qr_one_shot_exhausted_until[platform_id] = expires_at
            return None
        if not isinstance(expires_at, datetime) or now >= expires_at:
            self._login_qr_one_shots.pop(platform_id, None)
            self._login_qr_one_shot_exhausted_until.pop(platform_id, None)
            return None
        return one_shot

    def _login_qr_one_shot_exhausted(self, platform_id: str, now: datetime) -> bool:
        exhausted_until = self._login_qr_one_shot_exhausted_until.get(platform_id)
        if not isinstance(exhausted_until, datetime):
            return False
        if now >= exhausted_until:
            self._login_qr_one_shot_exhausted_until.pop(platform_id, None)
            return False
        return True

    def _notify_with_one_shot_webhook(
        self,
        one_shot: dict[str, Any],
        *,
        client_id: str,
        platform: str,
        current_url: str,
        image_data_url: str,
        source: str = "",
        captured_at: str = "",
    ) -> dict[str, Any]:
        old_webhook_url = getattr(self.login_qr_notifier, "webhook_url", "")
        if hasattr(self.login_qr_notifier, "webhook_url"):
            self.login_qr_notifier.webhook_url = str(one_shot.get("webhook_url") or "")
        try:
            return self.login_qr_notifier.notify(
                client_id=client_id,
                platform=platform,
                current_url=current_url,
                image_data_url=image_data_url,
                source=source,
                captured_at=captured_at,
            )
        finally:
            if hasattr(self.login_qr_notifier, "webhook_url"):
                self.login_qr_notifier.webhook_url = old_webhook_url

    def notify_login_qr(
        self,
        *,
        client_id: str,
        platform: str,
        current_url: str,
        image_data_url: str,
        source: str = "",
        captured_at: str = "",
    ) -> dict[str, Any]:
        now = _utc_now()
        platform_id = str(platform or "").strip()
        one_shot = self._active_login_qr_one_shot(platform_id, now)
        if one_shot is None and self._login_qr_one_shot_exhausted(platform_id, now):
            return {
                "ok": True,
                "dispatched": False,
                "disabled": True,
                "one_shot": True,
                "message": "login QR one-shot has already dispatched.",
                "server_time": _isoformat(now),
            }
        if one_shot is not None and "screenshot" in str(source or "").lower():
            return {
                "ok": True,
                "dispatched": False,
                "disabled": True,
                "one_shot": True,
                "message": "login QR screenshot capture is not allowed for one-shot delivery.",
                "server_time": _isoformat(now),
            }
        throttle_url = _login_qr_throttle_url(current_url)
        throttle_key = (platform_id, throttle_url)
        image_fingerprint = _login_qr_image_fingerprint(image_data_url)
        last_notification = self._login_qr_notification_throttle.get(throttle_key)
        if last_notification is not None:
            last_notified_at, last_image_fingerprint = last_notification
            recently_notified = now - last_notified_at < timedelta(
                seconds=LOGIN_QR_NOTIFICATION_THROTTLE_SECONDS
            )
            recently_notified = (
                recently_notified and image_fingerprint == last_image_fingerprint
            )
        else:
            recently_notified = False
        if recently_notified:
            return {
                "ok": True,
                "dispatched": False,
                "throttled": True,
                "message": "login QR notification throttled",
                "server_time": _isoformat(now),
            }

        if one_shot is not None:
            result = self._notify_with_one_shot_webhook(
                one_shot,
                client_id=client_id,
                platform=platform,
                current_url=current_url,
                image_data_url=image_data_url,
                source=source,
                captured_at=captured_at,
            )
            result["one_shot"] = True
            if result.get("ok") and result.get("dispatched"):
                remaining = _as_int(one_shot.get("remaining_dispatches"), 0) - 1
                one_shot["remaining_dispatches"] = remaining
                if remaining <= 0:
                    self._login_qr_one_shots.pop(platform_id, None)
                    expires_at = one_shot.get("expires_at")
                    if isinstance(expires_at, datetime) and now < expires_at:
                        self._login_qr_one_shot_exhausted_until[platform_id] = expires_at
            return result

        result = self.login_qr_notifier.notify(
            client_id=client_id,
            platform=platform,
            current_url=current_url,
            image_data_url=image_data_url,
            source=source,
            captured_at=captured_at,
        )
        if result.get("ok") and throttle_key[0] and throttle_key[1]:
            self._login_qr_notification_throttle[throttle_key] = (now, image_fingerprint)
        return result

    def _notify_login_success_platforms(
        self,
        *,
        client_id: str,
        platforms: list[str],
    ) -> list[str]:
        # Success confirmations are intentionally separate from QR delivery:
        # heartbeats must not capture or resend QR images, but a disconnected ->
        # connected transition should tell the operator their scan worked.
        dispatched: list[str] = []
        for platform in dict.fromkeys(str(item or "").strip() for item in platforms):
            if not platform:
                continue
            try:
                result = self.login_qr_notifier.notify_login_success(
                    client_id=client_id,
                    platform=platform,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Publisher login success Discord notification failed for %s: %s",
                    platform,
                    exc,
                )
                continue
            if result.get("ok") and result.get("dispatched"):
                dispatched.append(platform)
        return dispatched

    def list_upload_jobs(
        self,
        *,
        status: str = "",
        platform: str = "",
        limit: int = 30,
        include_deleted: bool = False,
    ) -> list[dict[str, Any]]:
        return self.runtime.upload_jobs.list_upload_jobs(
            status=status,
            platform=platform,
            limit=limit,
            include_deleted=include_deleted,
        )

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
        cover_generation_enabled: bool = True,
        cover_confirmation_required: bool = False,
        cover_candidate_count: int = 4,
        cover_style_hint: str = "",
        auto_cover_upload_enabled: bool = True,
        publisher_compliance_required: bool = False,
    ) -> dict[str, Any]:
        return self.runtime.upload_jobs.create_upload_job(
            project_id=project_id,
            platform=platform,
            book_name=book_name,
            chapter_title=chapter_title,
            body=body,
            upload_url=upload_url,
            publish=publish,
            create_if_missing=create_if_missing,
            book_meta=book_meta,
            cover_generation_enabled=cover_generation_enabled,
            cover_confirmation_required=cover_confirmation_required,
            cover_candidate_count=cover_candidate_count,
            cover_style_hint=cover_style_hint,
            auto_cover_upload_enabled=auto_cover_upload_enabled,
            publisher_compliance_required=publisher_compliance_required,
        )

    def list_work_bindings(
        self,
        *,
        project_id: str = "",
        platform: str = "",
    ) -> list[dict[str, Any]]:
        return self.runtime.bindings.list_work_bindings(
            project_id=project_id,
            platform_id=platform,
        )

    def list_chapter_bindings(
        self,
        *,
        project_id: str = "",
        platform: str = "",
        work_binding_id: str = "",
    ) -> list[dict[str, Any]]:
        return self.runtime.bindings.list_chapter_bindings(
            project_id=project_id,
            platform_id=platform,
            work_binding_id=work_binding_id,
        )

    def list_cover_assets(
        self,
        *,
        project_id: str = "",
        work_binding_id: str = "",
    ) -> list[dict[str, Any]]:
        return self.runtime.cover_service.list_cover_assets(
            project_id=project_id,
            work_binding_id=work_binding_id,
        )

    def generate_cover_candidates(
        self,
        *,
        project_id: str = "",
        platform: str,
        book_name: str,
        book_meta: dict[str, Any] | None = None,
        cover_candidate_count: int = 4,
        cover_style_hint: str = "",
        cover_confirmation_required: bool = False,
    ) -> dict[str, Any]:
        return self.runtime.cover_service.generate_cover_candidates(
            project_id=project_id,
            platform_id=platform,
            book_name=book_name,
            book_meta=book_meta,
            candidate_count=cover_candidate_count,
            cover_style_hint=cover_style_hint,
            cover_confirmation_required=cover_confirmation_required,
        )

    def select_cover_asset(self, cover_asset_id: str) -> dict[str, Any]:
        return self.runtime.cover_service.set_cover_selection(
            cover_asset_id,
            selection_state="selected",
            status="selected",
        )

    def approve_cover_asset(self, cover_asset_id: str) -> dict[str, Any]:
        return self.runtime.cover_service.set_cover_selection(
            cover_asset_id,
            selection_state="approved",
            status="approved",
        )

    def reject_cover_asset(self, cover_asset_id: str) -> dict[str, Any]:
        return self.runtime.cover_service.set_cover_selection(
            cover_asset_id,
            selection_state="rejected",
            status="rejected",
        )

    def enqueue_cover_upload(self, cover_asset_id: str) -> dict[str, Any]:
        with self.session_factory() as session:
            cover = session.get(PublisherCoverAsset, cover_asset_id)
            if cover is None:
                raise ValueError("封面不存在。")
            work = (
                session.get(PublisherWorkBinding, cover.work_binding_id)
                if cover.work_binding_id
                else None
            )
            if work is None and cover.project_id:
                work = session.execute(
                    select(PublisherWorkBinding)
                    .where(PublisherWorkBinding.project_id == cover.project_id)
                    .limit(1)
                ).scalar_one_or_none()
            if work is None:
                raise ValueError("封面尚未绑定平台作品，不能上传。")
            payload = {
                "project_id": work.project_id,
                "work_binding_id": work.id,
                "platform": work.platform_id,
                "book_name": work.book_name,
                "remote_book_id": work.remote_book_id,
                "remote_url": work.remote_url,
                "cover_asset_id": cover.id,
                "file_path": cover.file_path,
            }
            job = PublisherUploadJob(
                project_id=work.project_id,
                platform_id=work.platform_id,
                task_kind="cover_upload",
                status="pending",
                book_name=work.book_name,
                chapter_title="",
                body_text="",
                upload_url=work.remote_url,
                publish=False,
                result_message="封面上传任务已创建，等待浏览器扩展执行。",
                result_payload_json=json.dumps(payload, ensure_ascii=False),
            )
            session.add(job)
            session.commit()
            session.refresh(job)
            return self.runtime.upload_jobs.serialize_upload_job(job)

    def enqueue_audit_sync(
        self,
        *,
        project_id: str = "",
        platform: str,
        work_binding_id: str = "",
        book_name: str = "",
    ) -> dict[str, Any]:
        with self.session_factory() as session:
            work = session.get(PublisherWorkBinding, work_binding_id) if work_binding_id else None
            if work is None and project_id:
                work = session.execute(
                    select(PublisherWorkBinding)
                    .where(
                        PublisherWorkBinding.project_id == project_id,
                        PublisherWorkBinding.platform_id == platform,
                    )
                    .limit(1)
                ).scalar_one_or_none()
            payload = {
                "project_id": project_id or (work.project_id if work is not None else ""),
                "work_binding_id": work.id if work is not None else "",
                "remote_book_id": work.remote_book_id if work is not None else "",
                "remote_url": work.remote_url if work is not None else "",
            }
            job = PublisherUploadJob(
                project_id=payload["project_id"],
                platform_id=platform or (work.platform_id if work is not None else ""),
                task_kind="audit_sync",
                status="pending",
                book_name=book_name or (work.book_name if work is not None else ""),
                chapter_title="",
                body_text="",
                upload_url=payload["remote_url"],
                publish=False,
                result_message="审核同步任务已创建，等待浏览器扩展执行。",
                result_payload_json=json.dumps(payload, ensure_ascii=False),
            )
            session.add(job)
            session.commit()
            session.refresh(job)
            return self.runtime.upload_jobs.serialize_upload_job(job)

    def get_preflight(
        self,
        *,
        platform: str,
        book_name: str,
        chapter_title: str = "",
        body: str = "",
        create_if_missing: bool = False,
        book_meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_book_meta = self.runtime.upload_jobs.normalize_book_meta(book_meta)
        return self.runtime.preflight.check_upload_readiness(
            platform_id=platform,
            book_name=book_name,
            chapter_title=chapter_title,
            body=body,
            create_if_missing=create_if_missing,
            book_meta=normalized_book_meta,
        )

    def create_upload_jobs_batch(
        self,
        *,
        project_id: str = "",
        platform: str,
        book_name: str,
        jobs: list[dict[str, Any]],
        upload_url: str | None,
        publish: bool,
        create_if_missing: bool = False,
        book_meta: dict[str, Any] | None = None,
        cover_generation_enabled: bool = True,
        cover_confirmation_required: bool = False,
        cover_candidate_count: int = 4,
        cover_style_hint: str = "",
        auto_cover_upload_enabled: bool = True,
        publisher_compliance_required: bool = False,
    ) -> int:
        return self.runtime.upload_jobs.create_upload_jobs_batch(
            project_id=project_id,
            platform=platform,
            book_name=book_name,
            jobs=jobs,
            upload_url=upload_url,
            publish=publish,
            create_if_missing=create_if_missing,
            book_meta=book_meta,
            cover_generation_enabled=cover_generation_enabled,
            cover_confirmation_required=cover_confirmation_required,
            cover_candidate_count=cover_candidate_count,
            cover_style_hint=cover_style_hint,
            auto_cover_upload_enabled=auto_cover_upload_enabled,
            publisher_compliance_required=publisher_compliance_required,
        )

    def get_upload_job(self, job_id: str) -> dict[str, Any]:
        return self.runtime.upload_jobs.get_upload_job(job_id)

    def terminate_upload_job(self, job_id: str) -> dict[str, Any]:
        return self.runtime.upload_jobs.terminate_upload_job(job_id)

    def delete_upload_job(self, job_id: str) -> dict[str, Any]:
        return self.runtime.upload_jobs.delete_upload_job(job_id)

    def record_browser_session(
        self,
        *,
        client_id: str,
        platform: str,
        cookies: list[dict[str, Any]],
        raw_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        result = self.runtime.browser_sessions.record_browser_session(
            client_id=client_id,
            platform=platform,
            cookies=cookies,
            raw_state=raw_state,
        )
        result["login_success_notifications"] = self._notify_login_success_platforms(
            client_id=client_id,
            platforms=list(result.get("login_success_platforms") or []),
        )
        return result

    def get_browser_session(self, platform: str) -> dict[str, Any] | None:
        return self.runtime.browser_sessions.get_browser_session(platform)

    def get_browser_session_summary(self, platform: str) -> dict[str, Any] | None:
        return self.runtime.browser_sessions.get_browser_session_summary(platform)

    def has_browser_session(self, platform: str) -> bool:
        return self.runtime.browser_sessions.has_browser_session(platform)

    def mark_browser_session_result(
        self,
        *,
        platform: str,
        last_error: str = "",
        verified: bool = False,
    ) -> None:
        self.runtime.browser_sessions.mark_browser_session_result(
            platform=platform,
            last_error=last_error,
            verified=verified,
        )

    def claim_next_upload_job(
        self,
        *,
        client_id: str,
        connected_platforms: list[str],
    ) -> dict[str, Any] | None:
        return self.runtime.upload_jobs.claim_next_upload_job(
            client_id=client_id,
            connected_platforms=connected_platforms,
        )

    def claim_next_comment_sync_job(
        self,
        *,
        client_id: str,
        connected_platforms: list[str],
    ) -> dict[str, Any] | None:
        return self.runtime.comment_sync.claim_next_comment_sync_job(
            client_id=client_id,
            connected_platforms=connected_platforms,
        )

    def requeue_interrupted_upload_jobs(self) -> list[str]:
        return self.runtime.upload_jobs.requeue_interrupted_upload_jobs()

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
        self._sync_runtime_config()
        result = self.runtime.connection_state.heartbeat(
            client_id=client_id,
            extension_version=extension_version,
            browser_name=browser_name,
            browser_version=browser_version,
            backend_base_url=backend_base_url,
            platforms=platforms,
        )
        result["login_success_notifications"] = self._notify_login_success_platforms(
            client_id=client_id,
            platforms=list(result.get("login_success_platforms") or []),
        )
        return result

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
        return self.runtime.upload_jobs.update_upload_job_result(
            job_id=job_id,
            client_id=client_id,
            status=status,
            message=message,
            current_url=current_url,
            error=error,
            result_payload=result_payload,
        )

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
        return self.runtime.comment_sync.create_comment_sync_job(
            project_id=project_id,
            platform=platform,
            work_id=work_id,
            work_name=work_name,
            chapter_id=chapter_id,
            chapter_title=chapter_title,
            limit=limit,
        )

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
        return self.runtime.comment_sync.update_comment_sync_job_result(
            job_id=job_id,
            client_id=client_id,
            status=status,
            message=message,
            error=error,
            result_payload=result_payload,
        )

    def ingest_comments_batch(
        self,
        *,
        client_id: str,
        platform: str,
        comments: list[dict[str, Any]],
        job_id: str = "",
    ) -> dict[str, Any]:
        return self.runtime.comment_sync.ingest_comments_batch(
            client_id=client_id,
            platform=platform,
            comments=comments,
            job_id=job_id,
        )

    def verify_extension_api_key(self, provided_key: str | None) -> None:
        self._sync_runtime_config()
        self.runtime.auth.verify_extension_api_key(provided_key)

    def backend_ready_payload(self) -> dict[str, Any]:
        self._sync_runtime_config()
        return self.runtime.auth.backend_ready_payload()

    def preferred_client_heartbeat(
        self,
        *,
        preferred_client_id: str = "",
        stale_seconds: int = 90,
        allow_latest_recent_fallback: bool = False,
    ) -> dict[str, Any]:
        self._sync_runtime_config()
        return self.runtime.connection_state.preferred_client_heartbeat(
            preferred_client_id=preferred_client_id,
            stale_seconds=stale_seconds,
            allow_latest_recent_fallback=allow_latest_recent_fallback,
        )

    def _record_project_event(self, *args, **kwargs):
        return self.runtime.audit.record_project_event(*args, **kwargs)

    def _record_upload_job_event(self, *args, **kwargs):
        return self.runtime.audit.record_upload_job_event(*args, **kwargs)

    def _record_comment_sync_event(self, *args, **kwargs):
        return self.runtime.audit.record_comment_sync_event(*args, **kwargs)

    @staticmethod
    def _browser_session_sort_key(row) -> tuple[datetime, datetime, datetime]:
        return browser_session_sort_key(row)

    def _pick_browser_session_entry(
        self,
        entries: list[PublisherBrowserSessionEntry],
    ) -> PublisherBrowserSessionEntry | None:
        return pick_browser_session_entry(entries)

    def _pick_browser_sessions_by_platform(
        self,
        entries: list[PublisherBrowserSessionEntry],
    ) -> dict[str, PublisherBrowserSessionEntry]:
        return pick_browser_sessions_by_platform(entries)

    def _normalize_cookie(self, cookie: dict[str, Any]) -> dict[str, Any]:
        return self.runtime.browser_cookie_codec.normalize_cookie(cookie)

    def _normalize_book_meta(self, book_meta: dict[str, Any] | None) -> dict[str, Any]:
        return self.runtime.upload_jobs.normalize_book_meta(book_meta)

    def _serialize_upload_job(self, job: PublisherUploadJob) -> dict[str, Any]:
        return self.runtime.upload_jobs.serialize_upload_job(job)

    def _serialize_comment_sync_job(self, job: PublisherCommentSyncJob) -> dict[str, Any]:
        return self.runtime.comment_sync.serialize_comment_sync_job(job)

    def _new_upload_job(self, *args, **kwargs) -> PublisherUploadJob:
        return self.runtime.upload_jobs.new_upload_job(*args, **kwargs)

    def _resolve_project_id(self, *args, **kwargs) -> str:
        return self.runtime.upload_jobs.resolve_project_id(*args, **kwargs)

    def _claimable_platforms(self, *args, **kwargs) -> list[str]:
        return self.runtime.connection_state.claimable_platforms(*args, **kwargs)

    def _preferred_client_can_claim_platform(self, *args, **kwargs) -> bool:
        return self.runtime.connection_state.preferred_client_can_claim_platform(
            *args,
            **kwargs,
        )

    @staticmethod
    def _upsert_extension_platform_state(*args, **kwargs) -> None:
        upsert_extension_platform_state(*args, **kwargs)

    def _is_recent(self, value: datetime | None) -> bool:
        return self.runtime.connection_state.is_recent(value)

    def _ensure_extension_client(
        self,
        session,
        client_id: str,
    ) -> PublisherExtensionClient | None:
        return ensure_extension_client(session, client_id)

    def _is_browser_session_connected(
        self,
        platform: str,
        cookies_json: str,
        last_error: str,
    ) -> bool:
        return self.runtime.browser_cookie_codec.is_browser_session_connected(
            platform,
            cookies_json,
            last_error,
        )

    def _encode_cookies_for_storage(self, cookies: list[dict[str, Any]]) -> str:
        return self.runtime.browser_cookie_codec.encode(cookies)

    def _decode_cookies_from_storage(self, raw: str) -> list[dict[str, Any]]:
        return self.runtime.browser_cookie_codec.decode(raw)

    def _decode_cookies_from_storage_metadata(self, raw: str) -> dict[str, Any]:
        return self.runtime.browser_cookie_codec.decode_metadata(raw)

    def _decode_cookies_from_storage_with_metadata(
        self,
        raw: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        return self.runtime.browser_cookie_codec.decode_with_metadata(raw)

    def _normalize_cookie_list(self, cookies: list[Any]) -> list[dict[str, Any]]:
        return self.runtime.browser_cookie_codec.normalize_cookie_list(cookies)

    def _cookie_names_from_json(self, cookies_json: str) -> list[str]:
        return self.runtime.browser_cookie_codec.cookie_names_from_json(cookies_json)

    def _spec(self, platform: str) -> PlatformSpec:
        return self.runtime.platform_catalog.get(platform)
