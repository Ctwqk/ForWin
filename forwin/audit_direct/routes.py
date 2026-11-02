"""The extension-facing endpoints of the direct audit channel: claim, result, failure.

All three sit under /api/publishers/extension/ and take the same X-Forwin-Extension-Key header as
every other extension route. The manual audit API (POST /api/publishers/audit-sync) stays in
the publisher application layer; it only creates the request.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import Header, HTTPException, Response

from forwin.api_schema import (
    ExtensionAuditRequestClaimResponse,
    ExtensionAuditRequestFailureRequest,
    ExtensionAuditRequestResultRequest,
    ExtensionAuditRequestResultResponse,
    ExtensionClaimAuditRequestRequest,
)
from forwin.application.publisher.operations import require_extension_auth

from .service import AuditRequestError

CLAIM_PATH = "/api/publishers/extension/audit-requests/claim"
RESULT_PATH = "/api/publishers/extension/audit-requests/{request_id}/result"
FAILURE_PATH = "/api/publishers/extension/audit-requests/{request_id}/failure"


def build_handlers(
    *,
    get_publisher_manager: Callable[[], Any],
) -> dict[str, Callable[..., Any]]:
    def claim_audit_request(
        req: ExtensionClaimAuditRequestRequest,
        x_forwin_extension_key: str | None = Header(default=None),
    ):
        manager = get_publisher_manager()
        require_extension_auth(manager, x_forwin_extension_key)
        claim = manager.claim_next_audit_request(
            client_id=req.client_id,
            connected_platforms=req.connected_platforms,
        )
        if claim is None:
            return Response(status_code=204)
        return ExtensionAuditRequestClaimResponse(**claim)

    def submit_audit_request_result(
        request_id: str,
        req: ExtensionAuditRequestResultRequest,
        x_forwin_extension_key: str | None = Header(default=None),
    ):
        manager = get_publisher_manager()
        require_extension_auth(manager, x_forwin_extension_key)
        try:
            finished = manager.submit_audit_request_result(
                request_id=request_id,
                client_id=req.client_id,
                work=req.work,
                chapters=req.chapters,
                cover=req.cover,
                milestones=req.milestones,
                current_url=req.current_url,
            )
        except AuditRequestError as exc:
            raise HTTPException(exc.status_code, exc.detail()) from exc
        return ExtensionAuditRequestResultResponse(
            request_id=finished["request_id"],
            status=finished["status"],
            work_binding_id=finished["work_binding_id"],
        )

    def report_audit_request_failure(
        request_id: str,
        req: ExtensionAuditRequestFailureRequest,
        x_forwin_extension_key: str | None = Header(default=None),
    ):
        manager = get_publisher_manager()
        require_extension_auth(manager, x_forwin_extension_key)
        try:
            reported = manager.fail_audit_request(
                request_id=request_id,
                client_id=req.client_id,
                reason=req.reason,
                risk_reason=req.risk_reason,
                current_url=req.current_url,
            )
        except AuditRequestError as exc:
            raise HTTPException(exc.status_code, exc.detail()) from exc
        return ExtensionAuditRequestResultResponse(
            request_id=reported["request_id"],
            status=reported["status"],
            work_binding_id=reported["work_binding_id"],
        )

    return {
        "claim_audit_request": claim_audit_request,
        "submit_audit_request_result": submit_audit_request_result,
        "report_audit_request_failure": report_audit_request_failure,
    }


__all__ = ["CLAIM_PATH", "FAILURE_PATH", "RESULT_PATH", "build_handlers"]
