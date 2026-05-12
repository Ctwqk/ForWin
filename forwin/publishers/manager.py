from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import sessionmaker

from forwin.models.publisher import (
    PublisherBrowserSessionEntry,
    PublisherCommentSyncJob,
    PublisherExtensionClient,
    PublisherUploadJob,
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
    BrowserCookieCodec,
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
from forwin.publisher_runtime.service import PublisherRuntimeService
from forwin.publisher_runtime.upload_jobs import CodexInterventionHandler
from forwin.publisher_runtime.platform_catalog import PlatformSpec


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
        publisher_session_secret: str = "",
        publisher_session_encryption_required: bool = False,
        codex_intervention_handler: CodexInterventionHandler | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.extension_api_key = str(extension_api_key or "").strip()
        self.heartbeat_stale_seconds = heartbeat_stale_seconds
        self.preferred_client_id = str(preferred_client_id or "").strip()
        self.publisher_session_secret = str(publisher_session_secret or "").strip()
        self.publisher_session_encryption_required = bool(
            publisher_session_encryption_required
        )
        self.runtime = PublisherRuntimeService(
            session_factory=session_factory,
            extension_api_key=self.extension_api_key,
            heartbeat_stale_seconds=self.heartbeat_stale_seconds,
            preferred_client_id=self.preferred_client_id,
            publisher_session_secret=self.publisher_session_secret,
            publisher_session_encryption_required=self.publisher_session_encryption_required,
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

    def list_platforms(self) -> list[dict[str, Any]]:
        self._sync_runtime_config()
        return self.runtime.connection_state.list_platforms()

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
        return self.runtime.browser_sessions.record_browser_session(
            client_id=client_id,
            platform=platform,
            cookies=cookies,
            raw_state=raw_state,
        )

    def get_browser_session(
        self,
        platform: str,
        *,
        upgrade_legacy: bool = False,
    ) -> dict[str, Any] | None:
        return self.runtime.browser_sessions.get_browser_session(
            platform,
            upgrade_legacy=upgrade_legacy,
        )

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
        return self.runtime.connection_state.heartbeat(
            client_id=client_id,
            extension_version=extension_version,
            browser_name=browser_name,
            browser_version=browser_version,
            backend_base_url=backend_base_url,
            platforms=platforms,
        )

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

    @staticmethod
    def _legacy_cookie_names_from_json(cookies_json: str) -> list[str]:
        return BrowserCookieCodec.legacy_cookie_names_from_json(cookies_json)

    def _spec(self, platform: str) -> PlatformSpec:
        return self.runtime.platform_catalog.get(platform)
