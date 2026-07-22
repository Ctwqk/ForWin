from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import Header

from forwin.api_schema import (
    ExtensionClaimUploadJobRequest,
    UploadAttemptHeartbeatRequest,
    UploadAttemptPauseRequest,
    UploadAttemptPhaseRequest,
    UploadAttemptReceiptRequest,
    UploadAttemptReconcileRequest,
    UploadAttemptResultRequest,
)
from forwin.application.publisher import PublisherApplicationService


def build_handlers(
    *,
    service: PublisherApplicationService,
) -> dict[str, Callable[..., Any]]:
    def claim_publisher_upload_job(
        req: ExtensionClaimUploadJobRequest,
        x_forwin_extension_key: str | None = Header(default=None),
    ):
        return service.claim_publisher_upload_job(
            req,
            extension_key=x_forwin_extension_key,
        )

    def heartbeat_publisher_upload_attempt(
        job_id: str,
        attempt_id: str,
        req: UploadAttemptHeartbeatRequest,
        x_forwin_extension_key: str | None = Header(default=None),
    ):
        return service.heartbeat_publisher_upload_attempt(
            job_id,
            attempt_id,
            req,
            extension_key=x_forwin_extension_key,
        )

    def transition_publisher_upload_attempt(
        job_id: str,
        attempt_id: str,
        req: UploadAttemptPhaseRequest,
        x_forwin_extension_key: str | None = Header(default=None),
    ):
        return service.transition_publisher_upload_attempt(
            job_id,
            attempt_id,
            req,
            extension_key=x_forwin_extension_key,
        )

    def finish_publisher_upload_attempt(
        job_id: str,
        attempt_id: str,
        req: UploadAttemptResultRequest,
        x_forwin_extension_key: str | None = Header(default=None),
    ):
        return service.finish_publisher_upload_attempt(
            job_id,
            attempt_id,
            req,
            extension_key=x_forwin_extension_key,
        )

    def pause_publisher_upload_attempt(
        job_id: str,
        attempt_id: str,
        req: UploadAttemptPauseRequest,
        x_forwin_extension_key: str | None = Header(default=None),
    ):
        return service.pause_publisher_upload_attempt(
            job_id,
            attempt_id,
            req,
            extension_key=x_forwin_extension_key,
        )

    def record_publisher_upload_receipt(
        job_id: str,
        attempt_id: str,
        req: UploadAttemptReceiptRequest,
        x_forwin_extension_key: str | None = Header(default=None),
    ):
        return service.record_publisher_upload_receipt(
            job_id,
            attempt_id,
            req,
            extension_key=x_forwin_extension_key,
        )

    def reconcile_publisher_upload_attempt(
        job_id: str,
        attempt_id: str,
        req: UploadAttemptReconcileRequest,
        x_forwin_extension_key: str | None = Header(default=None),
    ):
        return service.reconcile_publisher_upload_attempt(
            job_id,
            attempt_id,
            req,
            extension_key=x_forwin_extension_key,
        )

    return {
        "claim_publisher_upload_job": claim_publisher_upload_job,
        "heartbeat_publisher_upload_attempt": heartbeat_publisher_upload_attempt,
        "transition_publisher_upload_attempt": transition_publisher_upload_attempt,
        "finish_publisher_upload_attempt": finish_publisher_upload_attempt,
        "pause_publisher_upload_attempt": pause_publisher_upload_attempt,
        "record_publisher_upload_receipt": record_publisher_upload_receipt,
        "reconcile_publisher_upload_attempt": reconcile_publisher_upload_attempt,
    }


__all__ = ["build_handlers"]
