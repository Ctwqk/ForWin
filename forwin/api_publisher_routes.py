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
    ExtensionSessionSyncRequest,
    PublisherBrowserSessionSummaryResponse,
    PublisherCommentSyncJobRequest,
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
        "terminate_publisher_upload_job": terminate_publisher_upload_job,
        "delete_publisher_upload_job": delete_publisher_upload_job,
        "publisher_extension_heartbeat": publisher_extension_heartbeat,
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
