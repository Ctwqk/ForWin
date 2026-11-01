from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError

from forwin.models.publisher import (
    AUDIT_REQUEST_OPEN_STATUSES,
    PublisherAuditRequest,
    PublisherWorkBinding,
)
from forwin.publisher_runtime.bindings import PublisherBindingService
from forwin.publisher_runtime.browser_sessions import as_utc, isoformat, utc_now
from forwin.publisher_runtime.connection_state import ExtensionConnectionService
from forwin.publisher_runtime.platform_catalog import PlatformCatalog

logger = logging.getLogger(__name__)

# How long a claimed request is held for the extension that took it. The page read takes a
# minute or two; if nothing came back by then (browser closed, service worker killed, page
# not ready) the request can be claimed again, so an audit is never lost.
AUDIT_REQUEST_LEASE_SECONDS = 300


class AuditRequestError(ValueError):
    """A refused audit request call, with the HTTP status and a stable code for the extension."""

    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code

    def detail(self) -> dict[str, str]:
        return {"code": self.code, "message": str(self)}


@dataclass(frozen=True)
class _AuditJobFacts:
    """What PublisherBindingService reads from the upload job it was written for.

    update_from_audit_sync_result and the upserts under it take the job they were running for;
    an audit request has no job row, so this carries the same fields, from the request.
    """

    id: str
    project_id: str
    platform_id: str
    book_name: str
    task_kind: str = "audit_sync"
    chapter_number: int = 0
    chapter_title: str = ""
    body_text: str = ""
    publish: bool = False


def _naive_utc(value: datetime) -> datetime:
    """The naive UTC form the request table stores (and compares in SQL)."""
    normalized = as_utc(value)
    assert normalized is not None
    return normalized.replace(tzinfo=None)


def _text(value: Any) -> str:
    return str(value or "").strip()


class AuditRequestService:
    def __init__(
        self,
        *,
        session_factory,
        platform_catalog: PlatformCatalog,
        connection_state: ExtensionConnectionService,
        bindings: PublisherBindingService,
        lease_seconds: int = AUDIT_REQUEST_LEASE_SECONDS,
    ) -> None:
        self.session_factory = session_factory
        self.platform_catalog = platform_catalog
        self.connection_state = connection_state
        self.bindings = bindings
        self.lease_seconds = lease_seconds

    # -- creating -------------------------------------------------------------------------

    def enqueue(
        self,
        *,
        project_id: str = "",
        platform: str,
        work_binding_id: str = "",
        book_name: str = "",
    ) -> dict[str, Any]:
        """Ask for an audit of one bound book; an open request for it is returned instead."""
        with self.session_factory() as session:
            work = (
                session.get(PublisherWorkBinding, work_binding_id)
                if work_binding_id
                else None
            )
            if work is None and project_id:
                work = session.execute(
                    select(PublisherWorkBinding)
                    .where(
                        PublisherWorkBinding.project_id == project_id,
                        PublisherWorkBinding.platform_id == platform,
                    )
                    .limit(1)
                ).scalar_one_or_none()
            if work is None or not _text(work.remote_book_id):
                raise ValueError("审核同步需要已绑定的平台作品 ID。")
            requested_project_id = _text(project_id)
            requested_platform = _text(platform)
            requested_book_name = _text(book_name)
            if requested_project_id and requested_project_id != _text(work.project_id):
                raise ValueError("审核同步项目与作品绑定不一致。")
            if requested_platform != _text(work.platform_id):
                raise ValueError("审核同步平台与作品绑定不一致。")
            if requested_book_name and requested_book_name != _text(work.book_name):
                raise ValueError("审核同步书名与作品绑定不一致。")

            existing = self._open_request(session, work.id)
            if existing is not None:
                return self.serialize(existing, created=False)
            row = PublisherAuditRequest(
                project_id=work.project_id,
                platform_id=work.platform_id,
                work_binding_id=work.id,
                book_name=work.book_name,
                remote_book_id=work.remote_book_id,
                remote_url=work.remote_url,
                status="pending",
            )
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                # A concurrent call created the open request first.
                session.rollback()
                existing = self._open_request(session, work.id)
                if existing is None:
                    raise
                return self.serialize(existing, created=False)
            session.refresh(row)
            return self.serialize(row, created=True)

    @staticmethod
    def _open_request(session, work_binding_id: str) -> PublisherAuditRequest | None:
        return session.execute(
            select(PublisherAuditRequest)
            .where(
                PublisherAuditRequest.work_binding_id == work_binding_id,
                PublisherAuditRequest.status.in_(AUDIT_REQUEST_OPEN_STATUSES),
            )
            .limit(1)
        ).scalar_one_or_none()

    # -- claiming -------------------------------------------------------------------------

    def claim_next(
        self,
        *,
        client_id: str,
        connected_platforms: list[str],
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Hand the oldest claimable request to the extension, or None.

        Only requests on platforms the extension says it is logged in to (and may serve, as
        for comment sync) are considered. Claimable means pending, or claimed with a lease
        that has run out. The claim is one conditional UPDATE, so two extensions never get
        the same request.
        """
        platforms = [
            platform
            for platform in dict.fromkeys(connected_platforms)
            if self.platform_catalog.has(platform)
        ]
        if not platforms:
            return None
        stamp = _naive_utc(now or utc_now())
        lease_until = stamp + timedelta(seconds=self.lease_seconds)

        with self.session_factory() as session:
            claimable_platforms = self.connection_state.claimable_platforms(
                session,
                client_id=client_id,
                platforms=platforms,
            )
            if not claimable_platforms:
                return None

            claimable = or_(
                PublisherAuditRequest.status == "pending",
                and_(
                    PublisherAuditRequest.status == "claimed",
                    or_(
                        PublisherAuditRequest.lease_expires_at.is_(None),
                        PublisherAuditRequest.lease_expires_at <= stamp,
                    ),
                ),
            )
            while True:
                candidate = session.execute(
                    select(PublisherAuditRequest)
                    .where(
                        PublisherAuditRequest.platform_id.in_(claimable_platforms),
                        claimable,
                    )
                    .order_by(
                        PublisherAuditRequest.created_at.asc(),
                        PublisherAuditRequest.id.asc(),
                    )
                    .limit(1)
                ).scalar_one_or_none()
                if candidate is None:
                    return None
                claimed = session.execute(
                    update(PublisherAuditRequest)
                    .where(PublisherAuditRequest.id == candidate.id, claimable)
                    .values(
                        status="claimed",
                        extension_client_id=client_id,
                        claimed_at=stamp,
                        lease_expires_at=lease_until,
                        attempt_count=PublisherAuditRequest.attempt_count + 1,
                    )
                )
                if not claimed.rowcount:
                    session.rollback()
                    continue
                session.commit()
                session.refresh(candidate)
                return {
                    "request_id": candidate.id,
                    "platform": candidate.platform_id,
                    "remote_book_id": candidate.remote_book_id,
                    "upload_url": candidate.remote_url,
                    "remote_url": candidate.remote_url,
                }

    # -- result ---------------------------------------------------------------------------

    def submit_result(
        self,
        *,
        request_id: str,
        client_id: str,
        work: dict[str, Any],
        chapters: list[dict[str, Any]],
        cover: dict[str, Any],
        milestones: list[dict[str, Any]],
        current_url: str = "",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Apply the extension's observation and finish the request.

        Refused, with nothing written, unless the request exists, is still open, was claimed
        by this client and the observation is about the stored book. The request row is
        locked for the call, so a result and a re-claim cannot interleave. An observation that
        arrives after the lease ran out is still taken as long as no other client claimed the
        request meanwhile: it is a read, and the book is the same.
        """
        stamp = _naive_utc(now or utc_now())
        with self.session_factory() as session:
            row = session.execute(
                select(PublisherAuditRequest)
                .where(PublisherAuditRequest.id == request_id)
                .with_for_update()
            ).scalar_one_or_none()
            if row is None:
                raise AuditRequestError(
                    "audit_request_not_found", "审核请求不存在。", 404
                )
            if row.status == "succeeded":
                raise AuditRequestError(
                    "audit_request_finished", "审核请求已经完成。", 409
                )
            if row.status != "claimed" or row.extension_client_id != client_id:
                raise AuditRequestError(
                    "audit_request_not_owned", "审核请求不属于这个浏览器扩展。", 409
                )
            reported_book_id = _text(work.get("remote_book_id"))
            reported_binding_id = _text(work.get("work_binding_id"))
            if (reported_book_id and reported_book_id != row.remote_book_id) or (
                reported_binding_id and reported_binding_id != row.work_binding_id
            ):
                raise AuditRequestError(
                    "audit_result_identity_mismatch",
                    "审核结果对应的作品与请求不一致。",
                    422,
                )
            if session.get(PublisherWorkBinding, row.work_binding_id) is None:
                raise AuditRequestError(
                    "audit_work_binding_missing", "审核请求对应的作品绑定已不存在。", 409
                )

            binding = self.bindings.update_from_audit_sync_result(
                session,
                job=_AuditJobFacts(
                    id=row.id,
                    project_id=row.project_id,
                    platform_id=row.platform_id,
                    book_name=row.book_name,
                ),
                result_payload={
                    # The stored identity, not whatever the page reported.
                    "work": {
                        **work,
                        "work_binding_id": row.work_binding_id,
                        "remote_book_id": row.remote_book_id,
                    },
                    "chapters": chapters,
                    "cover": cover,
                    "milestones": milestones,
                },
                current_url=current_url,
            )
            if binding is None or binding.id != row.work_binding_id:
                # The bindings service matched another row; leave everything unwritten.
                raise AuditRequestError(
                    "audit_result_identity_mismatch",
                    "审核结果没有落到请求对应的作品绑定上。",
                    409,
                )
            row.status = "succeeded"
            row.finished_at = stamp
            row.lease_expires_at = None
            row.result_summary_json = json.dumps(
                {
                    "audit_state": binding.audit_state,
                    "chapter_count": len(chapters),
                    "milestone_count": len(milestones),
                    "current_url": current_url,
                },
                ensure_ascii=False,
            )
            session.commit()
            session.refresh(row)
            logger.info(
                "audit request %s finished for %s/%s: %s",
                row.id,
                row.platform_id,
                row.remote_book_id,
                binding.audit_state,
            )
            return self.serialize(row)

    # -- reading --------------------------------------------------------------------------

    def serialize(
        self, row: PublisherAuditRequest, *, created: bool | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "request_id": row.id,
            "project_id": row.project_id,
            "platform": row.platform_id,
            "work_binding_id": row.work_binding_id,
            "book_name": row.book_name,
            "remote_book_id": row.remote_book_id,
            "upload_url": row.remote_url,
            "remote_url": row.remote_url,
            "status": row.status,
            "extension_client_id": row.extension_client_id,
            "attempt_count": int(row.attempt_count or 0),
            "created_at": isoformat(row.created_at),
            "claimed_at": isoformat(row.claimed_at),
            "lease_expires_at": isoformat(row.lease_expires_at),
            "finished_at": isoformat(row.finished_at),
        }
        if created is not None:
            payload["created"] = created
        return payload
