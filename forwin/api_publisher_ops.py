from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from forwin.api_schemas import (
    CommentSyncJobResultRequest,
    ExtensionBrowserSessionResponse,
    ExtensionClaimCommentSyncJobRequest,
    ExtensionClaimCommentSyncJobResponse,
    ExtensionClaimUploadJobRequest,
    ExtensionClaimUploadJobResponse,
    ExtensionCommentsBatchRequest,
    ExtensionCommentsBatchResponse,
    ExtensionHeartbeatRequest,
    ExtensionHeartbeatResponse,
    ExtensionSessionSyncRequest,
    ExtensionSessionSyncResponse,
    PublisherCommentSyncJobRequest,
    PublisherCommentSyncJobResponse,
    PublisherBrowserSessionSummaryResponse,
    PublisherPlatformInfo,
    PublisherUploadJobCreateRequest,
    PublisherUploadJobResponse,
    TaskMutationResponse,
    UploadJobResultRequest,
)
from forwin.publishers.manager import (
    PublisherExtensionAuthError,
    PublisherExtensionAuthNotConfigured,
)


def _build_extension_package(extension_root: Path) -> bytes:
    if not extension_root.exists():
        raise HTTPException(404, "浏览器扩展目录不存在。")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        paths = sorted(path for path in extension_root.rglob("*") if path.is_file())
        paths.sort(
            key=lambda path: (
                path.name != "manifest.json",
                str(path.relative_to(extension_root)),
            )
        )
        for path in paths:
            archive.write(path, arcname=Path("forwin-publisher") / path.relative_to(extension_root))
    buffer.seek(0)
    return buffer.getvalue()


def _require_extension_auth(publisher_manager, x_forwin_extension_key: str | None) -> None:
    try:
        publisher_manager.verify_extension_api_key(x_forwin_extension_key)
    except PublisherExtensionAuthNotConfigured as exc:
        raise HTTPException(503, str(exc)) from exc
    except PublisherExtensionAuthError as exc:
        raise HTTPException(401, str(exc)) from exc


def download_publisher_extension_package(*, extension_root: Path) -> StreamingResponse:
    payload = _build_extension_package(extension_root)
    return StreamingResponse(
        io.BytesIO(payload),
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="forwin-publisher-extension.zip"',
        },
    )


def list_publisher_platforms(*, publisher_manager) -> list[PublisherPlatformInfo]:
    return [PublisherPlatformInfo(**item) for item in publisher_manager.list_platforms()]


def create_publisher_upload_job(
    req: PublisherUploadJobCreateRequest,
    *,
    publisher_manager,
) -> PublisherUploadJobResponse:
    try:
        payload = publisher_manager.create_upload_job(
            project_id=str(req.project_id or "").strip(),
            platform=req.platform,
            book_name=req.book_name,
            chapter_title=req.chapter_title,
            body=req.body,
            upload_url=req.upload_url,
            publish=req.publish,
            create_if_missing=req.create_if_missing,
            book_meta=req.book_meta.model_dump() if req.book_meta else None,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return PublisherUploadJobResponse(**payload)


def get_publisher_upload_job(job_id: str, *, publisher_manager) -> PublisherUploadJobResponse:
    try:
        payload = publisher_manager.get_upload_job(job_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return PublisherUploadJobResponse(**payload)


def list_publisher_upload_jobs(
    *,
    publisher_manager,
    status: str = "",
    platform: str = "",
    limit: int = 30,
) -> list[PublisherUploadJobResponse]:
    payload = publisher_manager.list_upload_jobs(
        status=status,
        platform=platform,
        limit=limit,
    )
    return [PublisherUploadJobResponse(**item) for item in payload]


def terminate_publisher_upload_job(job_id: str, *, publisher_manager) -> TaskMutationResponse:
    try:
        payload = publisher_manager.terminate_upload_job(job_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return TaskMutationResponse(
        ok=True,
        task_kind="upload",
        task_id=job_id,
        status=str(payload.get("status", "")),
        message=str(payload.get("message", "")),
    )


def delete_publisher_upload_job(job_id: str, *, publisher_manager) -> TaskMutationResponse:
    try:
        publisher_manager.delete_upload_job(job_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return TaskMutationResponse(
        ok=True,
        task_kind="upload",
        task_id=job_id,
        status="deleted",
        message="任务已删除。",
    )


def publisher_extension_heartbeat(
    req: ExtensionHeartbeatRequest,
    *,
    publisher_manager,
    x_forwin_extension_key: str | None = None,
) -> ExtensionHeartbeatResponse:
    _require_extension_auth(publisher_manager, x_forwin_extension_key)
    payload = publisher_manager.record_extension_heartbeat(
        client_id=req.client_id,
        extension_version=req.extension_version,
        browser_name=req.browser_name,
        browser_version=req.browser_version,
        backend_base_url=req.backend_base_url,
        platforms=[
            {
                "platform": item.platform,
                "connected": item.connected,
                "login_method": item.login_method,
                "last_error": item.last_error,
                **item.raw_state,
            }
            for item in req.platforms
        ],
    )
    return ExtensionHeartbeatResponse(**payload)


def publisher_extension_session_sync(
    req: ExtensionSessionSyncRequest,
    *,
    publisher_manager,
    x_forwin_extension_key: str | None = None,
) -> ExtensionSessionSyncResponse:
    _require_extension_auth(publisher_manager, x_forwin_extension_key)
    payload = publisher_manager.record_browser_session(
        client_id=req.client_id,
        platform=req.platform,
        cookies=[item.model_dump() for item in req.cookies],
    )
    return ExtensionSessionSyncResponse(**payload)


def publisher_extension_get_browser_session(
    platform: str,
    *,
    publisher_manager,
    x_forwin_extension_key: str | None = None,
) -> ExtensionBrowserSessionResponse | None:
    _require_extension_auth(publisher_manager, x_forwin_extension_key)
    payload = publisher_manager.get_browser_session(platform, upgrade_legacy=True)
    if payload is None:
        return None
    return ExtensionBrowserSessionResponse(**payload)


def get_publisher_browser_session_summary(
    platform: str,
    *,
    publisher_manager,
) -> PublisherBrowserSessionSummaryResponse | None:
    payload = publisher_manager.get_browser_session_summary(platform)
    if payload is None:
        return None
    return PublisherBrowserSessionSummaryResponse(**payload)


def update_publisher_upload_job_result(
    job_id: str,
    req: UploadJobResultRequest,
    *,
    publisher_manager,
    x_forwin_extension_key: str | None = None,
) -> PublisherUploadJobResponse:
    _require_extension_auth(publisher_manager, x_forwin_extension_key)
    try:
        payload = publisher_manager.update_upload_job_result(
            job_id=job_id,
            client_id=req.client_id,
            status=req.status,
            message=req.message,
            current_url=req.current_url,
            error=req.error,
            result_payload=req.result_payload,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return PublisherUploadJobResponse(**payload)


def claim_publisher_upload_job(
    req: ExtensionClaimUploadJobRequest,
    *,
    publisher_manager,
    x_forwin_extension_key: str | None = None,
) -> ExtensionClaimUploadJobResponse:
    _require_extension_auth(publisher_manager, x_forwin_extension_key)
    payload = publisher_manager.claim_next_upload_job(
        client_id=req.client_id,
        connected_platforms=req.connected_platforms,
    )
    if payload is None:
        return ExtensionClaimUploadJobResponse(found=False, job=None)
    return ExtensionClaimUploadJobResponse(
        found=True,
        job=PublisherUploadJobResponse(**payload),
    )


def claim_publisher_comment_sync_job(
    req: ExtensionClaimCommentSyncJobRequest,
    *,
    publisher_manager,
    x_forwin_extension_key: str | None = None,
) -> ExtensionClaimCommentSyncJobResponse:
    _require_extension_auth(publisher_manager, x_forwin_extension_key)
    payload = publisher_manager.claim_next_comment_sync_job(
        client_id=req.client_id,
        connected_platforms=req.connected_platforms,
    )
    if payload is None:
        return ExtensionClaimCommentSyncJobResponse(found=False, job=None)
    return ExtensionClaimCommentSyncJobResponse(
        found=True,
        job=PublisherCommentSyncJobResponse(**payload),
    )


def create_publisher_comment_sync_job(
    req: PublisherCommentSyncJobRequest,
    *,
    publisher_manager,
) -> PublisherCommentSyncJobResponse:
    try:
        payload = publisher_manager.create_comment_sync_job(
            project_id=req.project_id,
            platform=req.platform,
            work_id=req.work_id,
            work_name=req.work_name,
            chapter_id=req.chapter_id,
            chapter_title=req.chapter_title,
            limit=req.limit,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return PublisherCommentSyncJobResponse(**payload)


def update_publisher_comment_sync_job_result(
    job_id: str,
    req: CommentSyncJobResultRequest,
    *,
    publisher_manager,
    x_forwin_extension_key: str | None = None,
) -> PublisherCommentSyncJobResponse:
    _require_extension_auth(publisher_manager, x_forwin_extension_key)
    try:
        payload = publisher_manager.update_comment_sync_job_result(
            job_id=job_id,
            client_id=req.client_id,
            status=req.status,
            message=req.message,
            error=req.error,
            result_payload=req.result_payload,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return PublisherCommentSyncJobResponse(**payload)


def ingest_publisher_comments_batch(
    req: ExtensionCommentsBatchRequest,
    *,
    publisher_manager,
    x_forwin_extension_key: str | None = None,
) -> ExtensionCommentsBatchResponse:
    _require_extension_auth(publisher_manager, x_forwin_extension_key)
    try:
        payload = publisher_manager.ingest_comments_batch(
            client_id=req.client_id,
            platform=req.platform,
            job_id=req.job_id,
            comments=[item.model_dump() for item in req.comments],
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return ExtensionCommentsBatchResponse(**payload)
