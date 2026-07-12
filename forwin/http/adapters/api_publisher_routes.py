from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import Header

from forwin.api_schema import (
    CommentSyncJobResultRequest,
    ExtensionClaimCommentSyncJobRequest,
    ExtensionClaimUploadJobRequest,
    ExtensionCommentsBatchRequest,
    ExtensionHeartbeatRequest,
    ExtensionLoginQrNotifyRequest,
    ExtensionSessionSyncRequest,
    UploadJobResultRequest,
)
from forwin.application.publisher import PublisherApplicationService


def build_handlers(
    *,
    service: PublisherApplicationService,
) -> dict[str, Callable[..., Any]]:
    def publisher_extension_heartbeat(
        req: ExtensionHeartbeatRequest,
        x_forwin_extension_key: str | None = Header(default=None),
    ):
        return service.publisher_extension_heartbeat(
            req,
            extension_key=x_forwin_extension_key,
        )

    def publisher_extension_session_sync(
        req: ExtensionSessionSyncRequest,
        x_forwin_extension_key: str | None = Header(default=None),
    ):
        return service.publisher_extension_session_sync(
            req,
            extension_key=x_forwin_extension_key,
        )

    def publisher_extension_login_qr_notify(
        req: ExtensionLoginQrNotifyRequest,
        x_forwin_extension_key: str | None = Header(default=None),
    ):
        return service.publisher_extension_login_qr_notify(
            req,
            extension_key=x_forwin_extension_key,
        )

    def publisher_extension_get_browser_session(
        platform: str,
        x_forwin_extension_key: str | None = Header(default=None),
    ):
        return service.publisher_extension_get_browser_session(
            platform,
            extension_key=x_forwin_extension_key,
        )

    def publisher_extension_heartbeat_status(
        client_id: str = "",
        stale_seconds: int = 90,
        allow_latest_recent_fallback: bool = False,
        x_forwin_extension_key: str | None = Header(default=None),
    ):
        return service.publisher_extension_heartbeat_status(
            client_id=client_id,
            stale_seconds=stale_seconds,
            allow_latest_recent_fallback=allow_latest_recent_fallback,
            extension_key=x_forwin_extension_key,
        )

    def update_publisher_upload_job_result(
        job_id: str,
        req: UploadJobResultRequest,
        x_forwin_extension_key: str | None = Header(default=None),
    ):
        return service.update_publisher_upload_job_result(
            job_id,
            req,
            extension_key=x_forwin_extension_key,
        )

    def claim_publisher_upload_job(
        req: ExtensionClaimUploadJobRequest,
        x_forwin_extension_key: str | None = Header(default=None),
    ):
        return service.claim_publisher_upload_job(
            req,
            extension_key=x_forwin_extension_key,
        )

    def claim_publisher_comment_sync_job(
        req: ExtensionClaimCommentSyncJobRequest,
        x_forwin_extension_key: str | None = Header(default=None),
    ):
        return service.claim_publisher_comment_sync_job(
            req,
            extension_key=x_forwin_extension_key,
        )

    def update_publisher_comment_sync_job_result(
        job_id: str,
        req: CommentSyncJobResultRequest,
        x_forwin_extension_key: str | None = Header(default=None),
    ):
        return service.update_publisher_comment_sync_job_result(
            job_id,
            req,
            extension_key=x_forwin_extension_key,
        )

    def ingest_publisher_comments_batch(
        req: ExtensionCommentsBatchRequest,
        x_forwin_extension_key: str | None = Header(default=None),
    ):
        return service.ingest_publisher_comments_batch(
            req,
            extension_key=x_forwin_extension_key,
        )

    return {
        "download_publisher_extension_package": service.download_publisher_extension_package,
        "download_publisher_firefox_extension_package": service.download_publisher_firefox_extension_package,
        "list_publisher_platforms": service.list_publisher_platforms,
        "create_publisher_upload_job": service.create_publisher_upload_job,
        "get_publisher_upload_job": service.get_publisher_upload_job,
        "list_publisher_upload_jobs": service.list_publisher_upload_jobs,
        "list_publisher_work_bindings": service.list_publisher_work_bindings,
        "list_publisher_chapter_bindings": service.list_publisher_chapter_bindings,
        "list_publisher_cover_assets": service.list_publisher_cover_assets,
        "generate_publisher_cover_candidates": service.generate_publisher_cover_candidates,
        "select_publisher_cover_asset": service.select_publisher_cover_asset,
        "approve_publisher_cover_asset": service.approve_publisher_cover_asset,
        "reject_publisher_cover_asset": service.reject_publisher_cover_asset,
        "enqueue_publisher_cover_upload": service.enqueue_publisher_cover_upload,
        "enqueue_publisher_audit_sync": service.enqueue_publisher_audit_sync,
        "publisher_preflight": service.publisher_preflight,
        "start_publisher_login_qr_one_shot": service.start_publisher_login_qr_one_shot,
        "terminate_publisher_upload_job": service.terminate_publisher_upload_job,
        "delete_publisher_upload_job": service.delete_publisher_upload_job,
        "publisher_extension_heartbeat": publisher_extension_heartbeat,
        "publisher_extension_login_qr_notify": publisher_extension_login_qr_notify,
        "publisher_extension_session_sync": publisher_extension_session_sync,
        "publisher_extension_get_browser_session": publisher_extension_get_browser_session,
        "get_publisher_browser_session_summary": service.get_publisher_browser_session_summary,
        "publisher_extension_heartbeat_status": publisher_extension_heartbeat_status,
        "update_publisher_upload_job_result": update_publisher_upload_job_result,
        "claim_publisher_upload_job": claim_publisher_upload_job,
        "claim_publisher_comment_sync_job": claim_publisher_comment_sync_job,
        "create_publisher_comment_sync_job": service.create_publisher_comment_sync_job,
        "update_publisher_comment_sync_job_result": update_publisher_comment_sync_job_result,
        "ingest_publisher_comments_batch": ingest_publisher_comments_batch,
    }


__all__ = ["build_handlers"]
