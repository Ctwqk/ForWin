from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError

from forwin.audit.events import DecisionEventType

from forwin.models.publisher import (
    AUDIT_REQUEST_OPEN_STATUSES,
    PublisherAuditRequest,
    PublisherWorkBinding,
)
from forwin.publisher_runtime.attempts import RISK_PAUSE_REASONS
from forwin.publisher_runtime.audit import PublisherAuditService
from forwin.publisher_runtime.bindings import PublisherBindingService
from forwin.publisher_runtime.browser_sessions import as_utc, isoformat, utc_now
from forwin.publisher_runtime.connection_state import ExtensionConnectionService
from forwin.publisher_runtime.platform_catalog import PlatformCatalog

logger = logging.getLogger(__name__)

# How long a claimed request is held for the extension that took it. The page read takes a
# minute or two; if nothing came back by then (browser closed, service worker killed, page
# not ready) the request can be claimed again, so an audit is never lost.
AUDIT_REQUEST_LEASE_SECONDS = 300

# How many times a request is handed out before it is given up. A failure report at this count,
# or a claim that finds a request already at it, moves the request to "failed" (a fresh request
# can then be asked for). Between attempts a request waits for its lease.
AUDIT_REQUEST_MAX_ATTEMPTS = 5

PAGE_NOT_BOOK_REASON = "audit page is not the book"


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


def page_references_book(current_url: str, remote_book_id: str) -> bool:
    """True when the page the audit was read from is the book's own page.

    The book id has to stand as a whole token in the URL path (fanqie .../book-info/7001,
    qidian .../CBID/9) or be the whole value of a query parameter (?book_id=7001). A login or
    dashboard page, a redirect that merely carries the book's address in a parameter, and a
    longer id that contains this one do not count.
    """
    book = _text(remote_book_id)
    if not book:
        return True
    url = _text(current_url)
    if not url:
        return False
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    token = re.compile(rf"(?<![0-9A-Za-z]){re.escape(book)}(?![0-9A-Za-z])")
    if token.search(unquote(parsed.path)):
        return True
    return any(
        book in values for values in parse_qs(parsed.query, keep_blank_values=False).values()
    )


class AuditRequestService:
    def __init__(
        self,
        *,
        session_factory,
        platform_catalog: PlatformCatalog,
        connection_state: ExtensionConnectionService,
        bindings: PublisherBindingService,
        audit: PublisherAuditService | None = None,
        lease_seconds: int = AUDIT_REQUEST_LEASE_SECONDS,
        max_attempts: int = AUDIT_REQUEST_MAX_ATTEMPTS,
    ) -> None:
        self.session_factory = session_factory
        self.platform_catalog = platform_catalog
        self.connection_state = connection_state
        self.bindings = bindings
        self.audit = audit
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts

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
        that has run out. A request already handed out `max_attempts` times without an answer
        is failed by the claim instead of being served again. The claim is one conditional
        UPDATE, so two extensions never get the same request.
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
                if int(candidate.attempt_count or 0) >= self.max_attempts:
                    # Handed out as often as allowed and never answered: give up on it.
                    gave_up = session.execute(
                        update(PublisherAuditRequest)
                        .where(PublisherAuditRequest.id == candidate.id, claimable)
                        .values(
                            status="failed",
                            finished_at=stamp,
                            lease_expires_at=None,
                            error_message=_text(candidate.error_message)
                            or f"no answer after {self.max_attempts} attempts",
                        )
                    )
                    if gave_up.rowcount:
                        session.commit()
                    else:
                        session.rollback()
                    continue
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

        Three further refusals end the attempt instead: a work binding that is gone or was
        corrected to another book since the request (the request fails; the binding is not
        touched), and a page that is not the book's own (a logged-out redirect, the dashboard:
        recorded as a failure, retried after the lease or failed at the attempt cap, and the
        binding's address is not overwritten). The bindings are updated from the live work
        binding, so a correction made after the request is kept.
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
            self._require_open_and_owned(row, client_id)
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
            live = session.get(PublisherWorkBinding, row.work_binding_id)
            if live is None:
                self._record_failure(
                    session, row, "work binding no longer exists", stamp, final=True
                )
                session.commit()
                raise AuditRequestError(
                    "audit_work_binding_missing", "审核请求对应的作品绑定已不存在。", 409
                )
            if _text(live.remote_book_id) != row.remote_book_id:
                # The binding was corrected after the request was made: this observation is
                # about the book it used to point to and must not overwrite the correction.
                self._record_failure(
                    session,
                    row,
                    "work binding changed after the request was made",
                    stamp,
                    final=True,
                )
                session.commit()
                raise AuditRequestError(
                    "audit_request_stale", "作品绑定在请求之后已被更正，请重新发起审核。", 409
                )
            if not page_references_book(current_url, row.remote_book_id):
                # A logged-out redirect or the dashboard: not an audit of this book, and it
                # must not overwrite the binding's address either.
                self._record_failure(session, row, PAGE_NOT_BOOK_REASON, stamp)
                session.commit()
                raise AuditRequestError(
                    "audit_page_not_the_book", "读取审核状态的页面不是这本书的页面。", 422
                )

            binding = self.bindings.update_from_audit_sync_result(
                session,
                job=_AuditJobFacts(
                    id=row.id,
                    # the live binding, so a correction made after the request stays
                    project_id=live.project_id,
                    platform_id=live.platform_id,
                    book_name=live.book_name,
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

    # -- failure --------------------------------------------------------------------------

    def report_failure(
        self,
        *,
        request_id: str,
        client_id: str,
        reason: str,
        risk_reason: str = "",
        current_url: str = "",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """The extension could not read the book's page.

        Accepted only from the client that holds a claimed request, with the same refusals as a
        result (nothing written). The reason is kept. Below the attempt cap the request stays
        claimed and comes back when its lease runs out; at the cap it becomes "failed". A risk
        page (`risk_reason`: captcha, mfa or account_risk) fails it at once and records the
        pause the way an upload job's risk pause was: the reason on the row and an
        upload_job_paused event carrying the pause details.
        """
        stamp = _naive_utc(now or utc_now())
        normalized_risk = _text(risk_reason)
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
            self._require_open_and_owned(row, client_id)
            if normalized_risk and normalized_risk not in RISK_PAUSE_REASONS:
                raise AuditRequestError(
                    "audit_risk_reason_invalid", "不支持的平台风险原因。", 422
                )
            self._record_failure(
                session,
                row,
                _text(reason) or "audit page could not be read",
                stamp,
                risk_reason=normalized_risk,
                current_url=_text(current_url),
            )
            session.commit()
            session.refresh(row)
            return self.serialize(row)

    @staticmethod
    def _require_open_and_owned(row: PublisherAuditRequest, client_id: str) -> None:
        if row.status in ("succeeded", "failed"):
            raise AuditRequestError("audit_request_finished", "审核请求已经完成。", 409)
        if row.status != "claimed" or row.extension_client_id != client_id:
            raise AuditRequestError(
                "audit_request_not_owned", "审核请求不属于这个浏览器扩展。", 409
            )

    def _record_failure(
        self,
        session,
        row: PublisherAuditRequest,
        reason: str,
        stamp: datetime,
        *,
        final: bool = False,
        risk_reason: str = "",
        current_url: str = "",
    ) -> None:
        """Keep the reason; fail the request when it is final, risky or out of attempts."""
        row.error_message = reason
        if not (final or risk_reason or int(row.attempt_count or 0) >= self.max_attempts):
            return  # stays claimed; the lease brings it back
        row.status = "failed"
        row.finished_at = stamp
        row.lease_expires_at = None
        if risk_reason:
            row.risk_reason = risk_reason
            self._record_risk_pause(session, row, risk_reason, current_url, stamp)

    def _record_risk_pause(
        self,
        session,
        row: PublisherAuditRequest,
        risk_reason: str,
        current_url: str,
        stamp: datetime,
    ) -> None:
        if self.audit is None:
            return
        pause_payload = {
            "platform_id": row.platform_id,
            "request_id": row.id,
            "task_kind": "audit_sync",
            "status": row.status,
            "work_binding_id": row.work_binding_id,
            "book_name": row.book_name,
            "extension_client_id": row.extension_client_id,
            "risk_reason": risk_reason,
            "current_url": current_url,
            "paused_at": as_utc(stamp).isoformat(),
        }
        try:
            # A savepoint, so an event that cannot be written (for instance a binding whose
            # project row is gone) never takes the recorded pause down with it.
            with session.begin_nested():
                self.audit.record_project_event(
                    session,
                    project_id=row.project_id,
                    event_type=DecisionEventType.UPLOAD_JOB_PAUSED,
                    summary=f"审核同步因 {risk_reason} 风险信号暂停。",
                    payload=pause_payload,
                    related_object_type="publisher_audit_request",
                    related_object_id=row.id,
                    actor_type="extension",
                )
        except Exception:
            logger.warning(
                "audit request %s: risk pause event not recorded", row.id, exc_info=True
            )

    # -- reading --------------------------------------------------------------------------

    def latest_for_work_binding(self, work_binding_id: str) -> dict[str, Any] | None:
        """The most recent audit request of a work binding, whatever its state (or None)."""
        with self.session_factory() as session:
            row = session.execute(
                select(PublisherAuditRequest)
                .where(PublisherAuditRequest.work_binding_id == work_binding_id)
                .order_by(
                    PublisherAuditRequest.created_at.desc(),
                    PublisherAuditRequest.id.desc(),
                )
                .limit(1)
            ).scalar_one_or_none()
            return self.serialize(row) if row is not None else None

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
            "reason": row.error_message or "",
            "risk_reason": row.risk_reason or "",
            "created_at": isoformat(row.created_at),
            "claimed_at": isoformat(row.claimed_at),
            "lease_expires_at": isoformat(row.lease_expires_at),
            "finished_at": isoformat(row.finished_at),
        }
        if created is not None:
            payload["created"] = created
        return payload
