from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from forwin.api_schema import (
    CommentSyncJobResultRequest,
    ExtensionClaimCommentSyncJobRequest,
    ExtensionClaimUploadJobRequest,
    ExtensionCommentsBatchRequest,
    ExtensionHeartbeatRequest,
    ExtensionLoginQrNotifyRequest,
    ExtensionSessionSyncRequest,
    PublisherAuditSyncRequest,
    PublisherCommentSyncJobRequest,
    PublisherCoverGenerateRequest,
    PublisherCoverSelectRequest,
    PublisherCoverUploadRequest,
    PublisherLoginQrOneShotRequest,
    PublisherPreflightRequest,
    PublisherUploadJobCreateRequest,
    UploadAttemptHeartbeatRequest,
    UploadAttemptPhaseRequest,
    UploadAttemptReceiptRequest,
    UploadAttemptReconcileRequest,
    UploadAttemptResultRequest,
)

from . import operations


class PublisherApplicationService:
    """Application boundary for publisher jobs and extension traffic."""

    def __init__(
        self,
        *,
        get_publisher_manager: Callable[[], Any],
        extension_root: Path,
    ) -> None:
        self.get_publisher_manager = get_publisher_manager
        self.extension_root = extension_root

    def download_publisher_extension_package(self):
        return operations.download_publisher_extension_package(
            extension_root=self.extension_root,
        )

    def download_publisher_firefox_extension_package(self):
        return operations.download_publisher_firefox_extension_package(
            extension_root=self.extension_root,
        )

    def list_publisher_platforms(self):
        return operations.list_publisher_platforms(
            publisher_manager=self.get_publisher_manager(),
        )

    def create_publisher_upload_job(self, req: PublisherUploadJobCreateRequest):
        return operations.create_publisher_upload_job(
            req,
            publisher_manager=self.get_publisher_manager(),
        )

    def get_publisher_upload_job(self, job_id: str):
        return operations.get_publisher_upload_job(
            job_id,
            publisher_manager=self.get_publisher_manager(),
        )

    def list_publisher_upload_jobs(
        self,
        status: str = "",
        platform: str = "",
        limit: int = 30,
    ):
        return operations.list_publisher_upload_jobs(
            publisher_manager=self.get_publisher_manager(),
            status=status,
            platform=platform,
            limit=limit,
        )

    def list_publisher_work_bindings(
        self,
        project_id: str = "",
        platform: str = "",
    ):
        return operations.list_publisher_work_bindings(
            publisher_manager=self.get_publisher_manager(),
            project_id=project_id,
            platform=platform,
        )

    def list_publisher_chapter_bindings(
        self,
        project_id: str = "",
        platform: str = "",
        work_binding_id: str = "",
    ):
        return operations.list_publisher_chapter_bindings(
            publisher_manager=self.get_publisher_manager(),
            project_id=project_id,
            platform=platform,
            work_binding_id=work_binding_id,
        )

    def list_publisher_cover_assets(
        self,
        project_id: str = "",
        work_binding_id: str = "",
    ):
        return operations.list_publisher_cover_assets(
            publisher_manager=self.get_publisher_manager(),
            project_id=project_id,
            work_binding_id=work_binding_id,
        )

    def generate_publisher_cover_candidates(self, req: PublisherCoverGenerateRequest):
        return operations.generate_publisher_cover_candidates(
            req,
            publisher_manager=self.get_publisher_manager(),
        )

    def select_publisher_cover_asset(self, req: PublisherCoverSelectRequest):
        return operations.select_publisher_cover_asset(
            req,
            publisher_manager=self.get_publisher_manager(),
        )

    def approve_publisher_cover_asset(self, req: PublisherCoverSelectRequest):
        return operations.approve_publisher_cover_asset(
            req,
            publisher_manager=self.get_publisher_manager(),
        )

    def reject_publisher_cover_asset(self, req: PublisherCoverSelectRequest):
        return operations.reject_publisher_cover_asset(
            req,
            publisher_manager=self.get_publisher_manager(),
        )

    def enqueue_publisher_cover_upload(self, req: PublisherCoverUploadRequest):
        return operations.enqueue_publisher_cover_upload(
            req,
            publisher_manager=self.get_publisher_manager(),
        )

    def enqueue_publisher_audit_sync(self, req: PublisherAuditSyncRequest):
        return operations.enqueue_publisher_audit_sync(
            req,
            publisher_manager=self.get_publisher_manager(),
        )

    def publisher_preflight(self, req: PublisherPreflightRequest):
        return operations.publisher_preflight(
            req,
            publisher_manager=self.get_publisher_manager(),
        )

    def start_publisher_login_qr_one_shot(
        self,
        req: PublisherLoginQrOneShotRequest,
    ):
        return operations.start_publisher_login_qr_one_shot(
            req,
            publisher_manager=self.get_publisher_manager(),
        )

    def terminate_publisher_upload_job(self, job_id: str):
        return operations.terminate_publisher_upload_job(
            job_id,
            publisher_manager=self.get_publisher_manager(),
        )

    def delete_publisher_upload_job(self, job_id: str):
        return operations.delete_publisher_upload_job(
            job_id,
            publisher_manager=self.get_publisher_manager(),
        )

    def publisher_extension_heartbeat(
        self,
        req: ExtensionHeartbeatRequest,
        *,
        extension_key: str | None,
    ):
        return operations.publisher_extension_heartbeat(
            req,
            publisher_manager=self.get_publisher_manager(),
            x_forwin_extension_key=extension_key,
        )

    def publisher_extension_session_sync(
        self,
        req: ExtensionSessionSyncRequest,
        *,
        extension_key: str | None,
    ):
        return operations.publisher_extension_session_sync(
            req,
            publisher_manager=self.get_publisher_manager(),
            x_forwin_extension_key=extension_key,
        )

    def publisher_extension_login_qr_notify(
        self,
        req: ExtensionLoginQrNotifyRequest,
        *,
        extension_key: str | None,
    ):
        return operations.publisher_extension_login_qr_notify(
            req,
            publisher_manager=self.get_publisher_manager(),
            x_forwin_extension_key=extension_key,
        )

    def publisher_extension_get_browser_session(
        self,
        platform: str,
        *,
        extension_key: str | None,
    ):
        return operations.publisher_extension_get_browser_session(
            platform,
            publisher_manager=self.get_publisher_manager(),
            x_forwin_extension_key=extension_key,
        )

    def get_publisher_browser_session_summary(self, platform: str):
        return operations.get_publisher_browser_session_summary(
            platform,
            publisher_manager=self.get_publisher_manager(),
        )

    def publisher_extension_heartbeat_status(
        self,
        *,
        client_id: str,
        stale_seconds: int,
        allow_latest_recent_fallback: bool,
        extension_key: str | None,
    ):
        return operations.publisher_extension_heartbeat_status(
            publisher_manager=self.get_publisher_manager(),
            client_id=client_id,
            stale_seconds=stale_seconds,
            allow_latest_recent_fallback=allow_latest_recent_fallback,
            x_forwin_extension_key=extension_key,
        )

    def finish_publisher_upload_attempt(
        self,
        job_id: str,
        attempt_id: str,
        req: UploadAttemptResultRequest,
        *,
        extension_key: str | None,
    ):
        return operations.finish_publisher_upload_attempt(
            job_id,
            attempt_id,
            req,
            publisher_manager=self.get_publisher_manager(),
            x_forwin_extension_key=extension_key,
        )

    def heartbeat_publisher_upload_attempt(
        self,
        job_id: str,
        attempt_id: str,
        req: UploadAttemptHeartbeatRequest,
        *,
        extension_key: str | None,
    ):
        return operations.heartbeat_publisher_upload_attempt(
            job_id,
            attempt_id,
            req,
            publisher_manager=self.get_publisher_manager(),
            x_forwin_extension_key=extension_key,
        )

    def transition_publisher_upload_attempt(
        self,
        job_id: str,
        attempt_id: str,
        req: UploadAttemptPhaseRequest,
        *,
        extension_key: str | None,
    ):
        return operations.transition_publisher_upload_attempt(
            job_id,
            attempt_id,
            req,
            publisher_manager=self.get_publisher_manager(),
            x_forwin_extension_key=extension_key,
        )

    def reconcile_publisher_upload_attempt(
        self,
        job_id: str,
        attempt_id: str,
        req: UploadAttemptReconcileRequest,
        *,
        extension_key: str | None,
    ):
        return operations.reconcile_publisher_upload_attempt(
            job_id,
            attempt_id,
            req,
            publisher_manager=self.get_publisher_manager(),
            x_forwin_extension_key=extension_key,
        )

    def record_publisher_upload_receipt(
        self,
        job_id: str,
        attempt_id: str,
        req: UploadAttemptReceiptRequest,
        *,
        extension_key: str | None,
    ):
        return operations.record_publisher_upload_receipt(
            job_id,
            attempt_id,
            req,
            publisher_manager=self.get_publisher_manager(),
            x_forwin_extension_key=extension_key,
        )

    def claim_publisher_upload_job(
        self,
        req: ExtensionClaimUploadJobRequest,
        *,
        extension_key: str | None,
    ):
        return operations.claim_publisher_upload_job(
            req,
            publisher_manager=self.get_publisher_manager(),
            x_forwin_extension_key=extension_key,
        )

    def claim_publisher_comment_sync_job(
        self,
        req: ExtensionClaimCommentSyncJobRequest,
        *,
        extension_key: str | None,
    ):
        return operations.claim_publisher_comment_sync_job(
            req,
            publisher_manager=self.get_publisher_manager(),
            x_forwin_extension_key=extension_key,
        )

    def create_publisher_comment_sync_job(self, req: PublisherCommentSyncJobRequest):
        return operations.create_publisher_comment_sync_job(
            req,
            publisher_manager=self.get_publisher_manager(),
        )

    def update_publisher_comment_sync_job_result(
        self,
        job_id: str,
        req: CommentSyncJobResultRequest,
        *,
        extension_key: str | None,
    ):
        return operations.update_publisher_comment_sync_job_result(
            job_id,
            req,
            publisher_manager=self.get_publisher_manager(),
            x_forwin_extension_key=extension_key,
        )

    def ingest_publisher_comments_batch(
        self,
        req: ExtensionCommentsBatchRequest,
        *,
        extension_key: str | None,
    ):
        return operations.ingest_publisher_comments_batch(
            req,
            publisher_manager=self.get_publisher_manager(),
            x_forwin_extension_key=extension_key,
        )


__all__ = ["PublisherApplicationService"]
