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
    PublisherCoverSelectRequest,
    PublisherCoverUploadRequest,
    PublisherLoginQrOneShotRequest,
    PublisherPreflightRequest,
    PublisherUploadJobCreateRequest,
    PublisherUploadResumeRequest,
    UploadAttemptHeartbeatRequest,
    UploadAttemptPauseRequest,
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

    def _run(self, operation, *args, **kwargs):
        return operation(
            *args,
            publisher_manager=self.get_publisher_manager(),
            **kwargs,
        )

    def download_publisher_extension_package(self):
        return operations.download_publisher_extension_package(
            extension_root=self.extension_root,
        )

    def download_publisher_firefox_extension_package(self):
        return operations.download_publisher_firefox_extension_package(
            extension_root=self.extension_root,
        )

    def list_publisher_platforms(self):
        return self._run(operations.list_publisher_platforms)

    def create_publisher_upload_job(self, req: PublisherUploadJobCreateRequest):
        return self._run(operations.create_publisher_upload_job, req)

    def get_publisher_upload_job(self, job_id: str):
        return self._run(operations.get_publisher_upload_job, job_id)

    def list_publisher_upload_jobs(
        self,
        status: str = "",
        platform: str = "",
        limit: int = 30,
    ):
        return self._run(
            operations.list_publisher_upload_jobs,
            status=status,
            platform=platform,
            limit=limit,
        )

    def list_publisher_work_bindings(
        self,
        project_id: str = "",
        platform: str = "",
    ):
        return self._run(
            operations.list_publisher_work_bindings,
            project_id=project_id,
            platform=platform,
        )

    def list_publisher_chapter_bindings(
        self,
        project_id: str = "",
        platform: str = "",
        work_binding_id: str = "",
    ):
        return self._run(
            operations.list_publisher_chapter_bindings,
            project_id=project_id,
            platform=platform,
            work_binding_id=work_binding_id,
        )

    def list_publisher_cover_assets(
        self,
        project_id: str = "",
        work_binding_id: str = "",
    ):
        return self._run(
            operations.list_publisher_cover_assets,
            project_id=project_id,
            work_binding_id=work_binding_id,
        )

    def select_publisher_cover_asset(self, req: PublisherCoverSelectRequest):
        return self._run(operations.select_publisher_cover_asset, req)

    def approve_publisher_cover_asset(self, req: PublisherCoverSelectRequest):
        return self._run(operations.approve_publisher_cover_asset, req)

    def reject_publisher_cover_asset(self, req: PublisherCoverSelectRequest):
        return self._run(operations.reject_publisher_cover_asset, req)

    def enqueue_publisher_cover_upload(self, req: PublisherCoverUploadRequest):
        return self._run(operations.enqueue_publisher_cover_upload, req)

    def enqueue_publisher_audit_sync(self, req: PublisherAuditSyncRequest):
        return self._run(operations.enqueue_publisher_audit_sync, req)

    def publisher_preflight(self, req: PublisherPreflightRequest):
        return self._run(operations.publisher_preflight, req)

    def start_publisher_login_qr_one_shot(
        self,
        req: PublisherLoginQrOneShotRequest,
    ):
        return self._run(operations.start_publisher_login_qr_one_shot, req)

    def terminate_publisher_upload_job(self, job_id: str):
        return self._run(operations.terminate_publisher_upload_job, job_id)

    def delete_publisher_upload_job(self, job_id: str):
        return self._run(operations.delete_publisher_upload_job, job_id)

    def resume_publisher_upload_job(
        self,
        job_id: str,
        req: PublisherUploadResumeRequest,
        *,
        operator_principal,
    ):
        return self._run(
            operations.resume_publisher_upload_job,
            job_id,
            req,
            operator_principal=operator_principal,
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

    def pause_publisher_upload_attempt(
        self,
        job_id: str,
        attempt_id: str,
        req: UploadAttemptPauseRequest,
        *,
        extension_key: str | None,
    ):
        return operations.pause_publisher_upload_attempt(
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
