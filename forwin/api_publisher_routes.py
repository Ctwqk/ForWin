from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from fastapi import Header

from forwin import api_publisher_ops
from forwin.api_schemas import (
    CommentSyncJobResultRequest,
    ExtensionClaimCommentSyncJobRequest,
    ExtensionClaimUploadJobRequest,
    ExtensionCommentsBatchRequest,
    ExtensionHeartbeatRequest,
    ExtensionLoginQrNotifyRequest,
    ExtensionSessionSyncRequest,
    PublisherBrowserSessionSummaryResponse,
    PublisherAuditSyncRequest,
    PublisherCommentSyncJobRequest,
    PublisherCoverGenerateRequest,
    PublisherCoverSelectRequest,
    PublisherCoverUploadRequest,
    PublisherPreflightRequest,
    PublisherUploadJobCreateRequest,
    UploadJobResultRequest,
)


def build_handlers(
    *,
    get_publisher_manager: Callable[[], Any],
    extension_root: Path,
) -> dict[str, Callable[..., Any]]:
    def download_publisher_extension_package():
        return api_publisher_ops.download_publisher_extension_package(
            extension_root=extension_root,
        )

    def download_publisher_firefox_extension_package():
        return api_publisher_ops.download_publisher_firefox_extension_package(
            extension_root=extension_root,
        )

    def list_publisher_platforms():
        return api_publisher_ops.list_publisher_platforms(
            publisher_manager=get_publisher_manager(),
        )

    def create_publisher_upload_job(req: PublisherUploadJobCreateRequest):
        return api_publisher_ops.create_publisher_upload_job(
            req,
            publisher_manager=get_publisher_manager(),
        )

    def get_publisher_upload_job(job_id: str):
        return api_publisher_ops.get_publisher_upload_job(
            job_id,
            publisher_manager=get_publisher_manager(),
        )

    def list_publisher_upload_jobs(status: str = "", platform: str = "", limit: int = 30):
        return api_publisher_ops.list_publisher_upload_jobs(
            publisher_manager=get_publisher_manager(),
            status=status,
            platform=platform,
            limit=limit,
        )

    def list_publisher_work_bindings(project_id: str = "", platform: str = ""):
        return api_publisher_ops.list_publisher_work_bindings(
            publisher_manager=get_publisher_manager(),
            project_id=project_id,
            platform=platform,
        )

    def list_publisher_chapter_bindings(
        project_id: str = "",
        platform: str = "",
        work_binding_id: str = "",
    ):
        return api_publisher_ops.list_publisher_chapter_bindings(
            publisher_manager=get_publisher_manager(),
            project_id=project_id,
            platform=platform,
            work_binding_id=work_binding_id,
        )

    def list_publisher_cover_assets(project_id: str = "", work_binding_id: str = ""):
        return api_publisher_ops.list_publisher_cover_assets(
            publisher_manager=get_publisher_manager(),
            project_id=project_id,
            work_binding_id=work_binding_id,
        )

    def generate_publisher_cover_candidates(req: PublisherCoverGenerateRequest):
        return api_publisher_ops.generate_publisher_cover_candidates(
            req,
            publisher_manager=get_publisher_manager(),
        )

    def select_publisher_cover_asset(req: PublisherCoverSelectRequest):
        return api_publisher_ops.select_publisher_cover_asset(
            req,
            publisher_manager=get_publisher_manager(),
        )

    def approve_publisher_cover_asset(req: PublisherCoverSelectRequest):
        return api_publisher_ops.approve_publisher_cover_asset(
            req,
            publisher_manager=get_publisher_manager(),
        )

    def reject_publisher_cover_asset(req: PublisherCoverSelectRequest):
        return api_publisher_ops.reject_publisher_cover_asset(
            req,
            publisher_manager=get_publisher_manager(),
        )

    def enqueue_publisher_cover_upload(req: PublisherCoverUploadRequest):
        return api_publisher_ops.enqueue_publisher_cover_upload(
            req,
            publisher_manager=get_publisher_manager(),
        )

    def enqueue_publisher_audit_sync(req: PublisherAuditSyncRequest):
        return api_publisher_ops.enqueue_publisher_audit_sync(
            req,
            publisher_manager=get_publisher_manager(),
        )

    def publisher_preflight(req: PublisherPreflightRequest):
        return api_publisher_ops.publisher_preflight(
            req,
            publisher_manager=get_publisher_manager(),
        )

    def terminate_publisher_upload_job(job_id: str):
        return api_publisher_ops.terminate_publisher_upload_job(
            job_id,
            publisher_manager=get_publisher_manager(),
        )

    def delete_publisher_upload_job(job_id: str):
        return api_publisher_ops.delete_publisher_upload_job(
            job_id,
            publisher_manager=get_publisher_manager(),
        )

    def publisher_extension_heartbeat(
        req: ExtensionHeartbeatRequest,
        x_forwin_extension_key: str | None = Header(default=None),
    ):
        return api_publisher_ops.publisher_extension_heartbeat(
            req,
            publisher_manager=get_publisher_manager(),
            x_forwin_extension_key=x_forwin_extension_key,
        )

    def publisher_extension_session_sync(
        req: ExtensionSessionSyncRequest,
        x_forwin_extension_key: str | None = Header(default=None),
    ):
        return api_publisher_ops.publisher_extension_session_sync(
            req,
            publisher_manager=get_publisher_manager(),
            x_forwin_extension_key=x_forwin_extension_key,
        )

    def publisher_extension_login_qr_notify(
        req: ExtensionLoginQrNotifyRequest,
        x_forwin_extension_key: str | None = Header(default=None),
    ):
        return api_publisher_ops.publisher_extension_login_qr_notify(
            req,
            publisher_manager=get_publisher_manager(),
            x_forwin_extension_key=x_forwin_extension_key,
        )

    def publisher_extension_get_browser_session(
        platform: str,
        x_forwin_extension_key: str | None = Header(default=None),
    ):
        return api_publisher_ops.publisher_extension_get_browser_session(
            platform,
            publisher_manager=get_publisher_manager(),
            x_forwin_extension_key=x_forwin_extension_key,
        )

    def get_publisher_browser_session_summary(platform: str):
        return api_publisher_ops.get_publisher_browser_session_summary(
            platform,
            publisher_manager=get_publisher_manager(),
        )

    def publisher_extension_heartbeat_status(
        client_id: str = "",
        stale_seconds: int = 90,
        allow_latest_recent_fallback: bool = False,
        x_forwin_extension_key: str | None = Header(default=None),
    ):
        publisher_manager = get_publisher_manager()
        api_publisher_ops._require_extension_auth(  # noqa: SLF001
            publisher_manager,
            x_forwin_extension_key,
        )
        return publisher_manager.preferred_client_heartbeat(
            preferred_client_id=client_id,
            stale_seconds=stale_seconds,
            allow_latest_recent_fallback=allow_latest_recent_fallback,
        )

    def update_publisher_upload_job_result(
        job_id: str,
        req: UploadJobResultRequest,
        x_forwin_extension_key: str | None = Header(default=None),
    ):
        return api_publisher_ops.update_publisher_upload_job_result(
            job_id,
            req,
            publisher_manager=get_publisher_manager(),
            x_forwin_extension_key=x_forwin_extension_key,
        )

    def claim_publisher_upload_job(
        req: ExtensionClaimUploadJobRequest,
        x_forwin_extension_key: str | None = Header(default=None),
    ):
        return api_publisher_ops.claim_publisher_upload_job(
            req,
            publisher_manager=get_publisher_manager(),
            x_forwin_extension_key=x_forwin_extension_key,
        )

    def claim_publisher_comment_sync_job(
        req: ExtensionClaimCommentSyncJobRequest,
        x_forwin_extension_key: str | None = Header(default=None),
    ):
        return api_publisher_ops.claim_publisher_comment_sync_job(
            req,
            publisher_manager=get_publisher_manager(),
            x_forwin_extension_key=x_forwin_extension_key,
        )

    def create_publisher_comment_sync_job(req: PublisherCommentSyncJobRequest):
        return api_publisher_ops.create_publisher_comment_sync_job(
            req,
            publisher_manager=get_publisher_manager(),
        )

    def update_publisher_comment_sync_job_result(
        job_id: str,
        req: CommentSyncJobResultRequest,
        x_forwin_extension_key: str | None = Header(default=None),
    ):
        return api_publisher_ops.update_publisher_comment_sync_job_result(
            job_id,
            req,
            publisher_manager=get_publisher_manager(),
            x_forwin_extension_key=x_forwin_extension_key,
        )

    def ingest_publisher_comments_batch(
        req: ExtensionCommentsBatchRequest,
        x_forwin_extension_key: str | None = Header(default=None),
    ):
        return api_publisher_ops.ingest_publisher_comments_batch(
            req,
            publisher_manager=get_publisher_manager(),
            x_forwin_extension_key=x_forwin_extension_key,
        )

    return {
        "download_publisher_extension_package": download_publisher_extension_package,
        "download_publisher_firefox_extension_package": download_publisher_firefox_extension_package,
        "list_publisher_platforms": list_publisher_platforms,
        "create_publisher_upload_job": create_publisher_upload_job,
        "get_publisher_upload_job": get_publisher_upload_job,
        "list_publisher_upload_jobs": list_publisher_upload_jobs,
        "list_publisher_work_bindings": list_publisher_work_bindings,
        "list_publisher_chapter_bindings": list_publisher_chapter_bindings,
        "list_publisher_cover_assets": list_publisher_cover_assets,
        "generate_publisher_cover_candidates": generate_publisher_cover_candidates,
        "select_publisher_cover_asset": select_publisher_cover_asset,
        "approve_publisher_cover_asset": approve_publisher_cover_asset,
        "reject_publisher_cover_asset": reject_publisher_cover_asset,
        "enqueue_publisher_cover_upload": enqueue_publisher_cover_upload,
        "enqueue_publisher_audit_sync": enqueue_publisher_audit_sync,
        "publisher_preflight": publisher_preflight,
        "terminate_publisher_upload_job": terminate_publisher_upload_job,
        "delete_publisher_upload_job": delete_publisher_upload_job,
        "publisher_extension_heartbeat": publisher_extension_heartbeat,
        "publisher_extension_login_qr_notify": publisher_extension_login_qr_notify,
        "publisher_extension_session_sync": publisher_extension_session_sync,
        "publisher_extension_get_browser_session": publisher_extension_get_browser_session,
        "get_publisher_browser_session_summary": get_publisher_browser_session_summary,
        "publisher_extension_heartbeat_status": publisher_extension_heartbeat_status,
        "update_publisher_upload_job_result": update_publisher_upload_job_result,
        "claim_publisher_upload_job": claim_publisher_upload_job,
        "claim_publisher_comment_sync_job": claim_publisher_comment_sync_job,
        "create_publisher_comment_sync_job": create_publisher_comment_sync_job,
        "update_publisher_comment_sync_job_result": update_publisher_comment_sync_job_result,
        "ingest_publisher_comments_batch": ingest_publisher_comments_batch,
    }
