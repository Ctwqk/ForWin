from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import case, func, or_, select

from forwin.audit.events import DecisionEventType
from forwin.models.base import new_id
from forwin.models.publisher import (
    PublisherExtensionClient,
    PublisherUploadAttempt,
    PublisherUploadJob,
)

from .audit import PublisherAuditService
from .browser_sessions import as_utc, utc_now
from .connection_state import ExtensionConnectionService
from .platform_catalog import PlatformCatalog


EXECUTE_PHASES = ("claimed", "mutation_started", "receipt_observed")
RECONCILE_PHASES = ("claimed", "observation_started", "receipt_observed")
CLAIMABLE_TASK_KINDS = ("chapter_upload", "cover_upload", "audit_sync")


class PublisherProtocolError(ValueError):
    code = "publisher_protocol_error"
    status_code = 409

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.context = {
            str(key): value for key, value in context.items() if value is not None
        }

    def detail(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), **self.context}


class PublisherAttemptFenceError(PublisherProtocolError):
    code = "stale_attempt"


class PublisherLeaseExpiredError(PublisherProtocolError):
    code = "lease_expired"


class PublisherInvalidTransitionError(PublisherProtocolError):
    code = "invalid_transition"


class PublisherReceiptRequiredError(PublisherProtocolError):
    code = "receipt_required"


class PublisherAbsenceNotAuthoritativeError(PublisherProtocolError):
    code = "absence_not_authoritative"


class PublisherResourceNotFoundError(PublisherProtocolError):
    status_code = 404

    def __init__(self, resource: str, identifier: str) -> None:
        self.code = f"{resource}_not_found"
        super().__init__(
            f"publisher {resource} was not found", **{f"{resource}_id": identifier}
        )


class PublisherAttemptService:
    def __init__(
        self,
        *,
        session_factory,
        platform_catalog: PlatformCatalog,
        connection_state: ExtensionConnectionService,
        audit: PublisherAuditService,
        job_serializer: Callable[[PublisherUploadJob], dict[str, Any]],
        default_lease_seconds: int = 90,
        authoritative_absence_platforms: Sequence[str] = (),
    ) -> None:
        self.session_factory = session_factory
        self.platform_catalog = platform_catalog
        self.connection_state = connection_state
        self.audit = audit
        self.job_serializer = job_serializer
        self.default_lease_seconds = max(10, int(default_lease_seconds or 90))
        self.authoritative_absence_platforms = frozenset(
            str(platform or "").strip()
            for platform in authoritative_absence_platforms
            if str(platform or "").strip()
        )

    def claim(
        self,
        *,
        client_id: str,
        connected_platforms: list[str],
        lease_seconds: int | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        worker_id = str(client_id or "").strip()
        if not worker_id:
            raise ValueError("publisher attempt worker_id must be non-empty")
        platforms = [
            platform
            for platform in dict.fromkeys(
                str(platform or "").strip() for platform in connected_platforms
            )
            if platform and self.platform_catalog.has(platform)
        ]
        if not platforms:
            return None
        claimed_at = now or utc_now()
        lease_duration = self._lease_seconds(lease_seconds)

        with self.session_factory() as session:
            client = self.connection_state.ensure_extension_client(session, worker_id)
            if client is None:
                raise ValueError("publisher attempt worker_id must be non-empty")
            # One worker may own only one claim; all later row locks use job -> attempt.
            session.execute(
                select(PublisherExtensionClient)
                .where(PublisherExtensionClient.client_id == worker_id)
                .with_for_update()
            ).scalar_one()
            self._expire_in_session(session, now=claimed_at, interrupted=False)
            active_ids = session.execute(
                select(PublisherUploadJob.id, PublisherUploadAttempt.id)
                .join(
                    PublisherUploadAttempt,
                    PublisherUploadAttempt.id == PublisherUploadJob.current_attempt_id,
                )
                .where(
                    PublisherUploadJob.deleted_at.is_(None),
                    PublisherUploadJob.task_kind.in_(CLAIMABLE_TASK_KINDS),
                    PublisherUploadAttempt.status == "running",
                    PublisherUploadAttempt.worker_id == worker_id,
                )
                .order_by(
                    PublisherUploadAttempt.claimed_at.asc(),
                    PublisherUploadAttempt.id.asc(),
                )
                .limit(1)
            ).one_or_none()
            if active_ids is not None:
                active_job_id, active_attempt_id = active_ids
                job = session.execute(
                    select(PublisherUploadJob)
                    .where(PublisherUploadJob.id == active_job_id)
                    .with_for_update()
                ).scalar_one_or_none()
                attempt = session.execute(
                    select(PublisherUploadAttempt)
                    .where(PublisherUploadAttempt.id == active_attempt_id)
                    .with_for_update()
                ).scalar_one_or_none()
                if (
                    job is not None
                    and attempt is not None
                    and job.deleted_at is None
                    and job.current_attempt_id == attempt.id
                    and attempt.worker_id == worker_id
                    and attempt.status == "running"
                    and self._lease_is_live(attempt, claimed_at)
                ):
                    session.commit()
                    if (
                        job.platform_id in platforms
                        and not job.abort_requested
                        and attempt.phase == "claimed"
                    ):
                        return self._serialize_claim(job, attempt)
                    return None

            claimable_platforms = self.connection_state.claimable_platforms(
                session,
                client_id=worker_id,
                platforms=platforms,
            )
            if not claimable_platforms:
                session.commit()
                return None

            job = session.execute(
                select(PublisherUploadJob)
                .where(
                    PublisherUploadJob.deleted_at.is_(None),
                    PublisherUploadJob.current_attempt_id == "",
                    PublisherUploadJob.idempotency_key != "",
                    PublisherUploadJob.body_sha256 != "",
                    PublisherUploadJob.platform_id.in_(claimable_platforms),
                    PublisherUploadJob.task_kind.in_(CLAIMABLE_TASK_KINDS),
                    or_(
                        (
                            (PublisherUploadJob.status == "pending")
                            & PublisherUploadJob.abort_requested.is_(False)
                            & (
                                PublisherUploadJob.available_at.is_(None)
                                | (PublisherUploadJob.available_at <= claimed_at)
                            )
                        ),
                        (
                            (PublisherUploadJob.status == "reconciling")
                            & (
                                PublisherUploadJob.reconcile_after.is_(None)
                                | (PublisherUploadJob.reconcile_after <= claimed_at)
                            )
                        ),
                    ),
                )
                .order_by(
                    case((PublisherUploadJob.status == "reconciling", 0), else_=1),
                    PublisherUploadJob.created_at.asc(),
                    PublisherUploadJob.id.asc(),
                )
                .with_for_update(skip_locked=True)
                .limit(1)
            ).scalar_one_or_none()
            if job is None:
                session.commit()
                return None

            attempt_number = (
                int(
                    session.scalar(
                        select(func.max(PublisherUploadAttempt.attempt_number)).where(
                            PublisherUploadAttempt.upload_job_id == job.id
                        )
                    )
                    or 0
                )
                + 1
            )
            lease_epoch = (
                int(
                    session.scalar(
                        select(func.max(PublisherUploadAttempt.lease_epoch)).where(
                            PublisherUploadAttempt.upload_job_id == job.id
                        )
                    )
                    or 0
                )
                + 1
            )
            attempt_kind = (
                "reconcile"
                if job.status == "reconciling" or job.task_kind == "audit_sync"
                else "execute"
            )
            attempt = PublisherUploadAttempt(
                id=new_id(),
                upload_job_id=job.id,
                attempt_number=attempt_number,
                attempt_kind=attempt_kind,
                worker_id=worker_id,
                lease_epoch=lease_epoch,
                status="running",
                phase="claimed",
                claimed_at=claimed_at,
                heartbeat_at=claimed_at,
                lease_expires_at=claimed_at + timedelta(seconds=lease_duration),
                content_sha256=str(job.body_sha256 or ""),
            )
            session.add(attempt)
            session.flush()

            job.status = "running"
            job.current_attempt_id = attempt.id
            job.extension_client_id = worker_id
            job.claimed_at = job.claimed_at or claimed_at
            job.started_at = job.started_at or claimed_at
            job.finished_at = None
            job.available_at = None
            job.reconcile_after = None
            job.error_message = ""
            job.result_message = (
                "发布任务已进入只读对账。"
                if attempt_kind == "reconcile"
                else "发布任务已被浏览器扩展领取。"
            )
            if job.canon_commit_id:
                payload = self._payload(job)
                payload.setdefault("publish_mode_frozen_at", claimed_at.isoformat())
                job.result_payload_json = json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                )
            self.audit.record_upload_job_event(
                session,
                job=job,
                event_type=DecisionEventType.UPLOAD_JOB_CLAIMED,
                summary="发布上传任务已创建围栏执行尝试。",
                actor_type="extension",
                extra_payload={
                    "attempt_id": attempt.id,
                    "attempt_number": attempt.attempt_number,
                    "attempt_kind": attempt.attempt_kind,
                    "lease_epoch": attempt.lease_epoch,
                    "worker_id": attempt.worker_id,
                },
            )
            session.commit()
            session.refresh(job)
            session.refresh(attempt)
            return self._serialize_claim(job, attempt)

    def heartbeat(
        self,
        *,
        job_id: str,
        attempt_id: str,
        worker_id: str,
        lease_epoch: int,
        lease_seconds: int | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        heartbeat_at = now or utc_now()
        with self.session_factory() as session:
            job, attempt = self.require_fence(
                session,
                job_id=job_id,
                attempt_id=attempt_id,
                worker_id=worker_id,
                lease_epoch=lease_epoch,
                now=heartbeat_at,
            )
            attempt.heartbeat_at = heartbeat_at
            attempt.lease_expires_at = heartbeat_at + timedelta(
                seconds=self._lease_seconds(lease_seconds)
            )
            session.commit()
            session.refresh(job)
            session.refresh(attempt)
            return self._serialize_claim(job, attempt)

    def transition(
        self,
        *,
        job_id: str,
        attempt_id: str,
        worker_id: str,
        lease_epoch: int,
        phase: str,
        current_url: str = "",
        lease_seconds: int | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        transitioned_at = now or utc_now()
        with self.session_factory() as session:
            job, attempt = self.require_fence(
                session,
                job_id=job_id,
                attempt_id=attempt_id,
                worker_id=worker_id,
                lease_epoch=lease_epoch,
                now=transitioned_at,
            )
            if job.abort_requested and str(phase or "").strip() == "mutation_started":
                raise PublisherInvalidTransitionError(
                    "publisher mutation cannot start after abort was requested"
                )
            self.advance_phase(attempt, phase)
            if current_url:
                job.current_url = str(current_url).strip()
            attempt.heartbeat_at = transitioned_at
            attempt.lease_expires_at = transitioned_at + timedelta(
                seconds=self._lease_seconds(lease_seconds)
            )
            self.audit.record_upload_job_event(
                session,
                job=job,
                event_type=DecisionEventType.UPLOAD_JOB_PROGRESS,
                summary=f"发布尝试阶段更新为 {attempt.phase}。",
                actor_type="extension",
                extra_payload={
                    "attempt_id": attempt.id,
                    "attempt_kind": attempt.attempt_kind,
                    "attempt_phase": attempt.phase,
                    "lease_epoch": attempt.lease_epoch,
                    "worker_id": attempt.worker_id,
                },
            )
            session.commit()
            session.refresh(job)
            session.refresh(attempt)
            return self._serialize_claim(job, attempt)

    def expire(self, *, now: datetime | None = None) -> list[str]:
        expired_at = now or utc_now()
        with self.session_factory() as session:
            job_ids = self._expire_in_session(
                session,
                now=expired_at,
                interrupted=False,
            )
            session.commit()
            return job_ids

    def recover_interrupted(self, *, now: datetime | None = None) -> list[str]:
        recovered_at = now or utc_now()
        with self.session_factory() as session:
            job_ids = self._expire_in_session(
                session,
                now=recovered_at,
                interrupted=True,
            )
            orphan_jobs = (
                session.execute(
                    select(PublisherUploadJob)
                    .where(
                        PublisherUploadJob.deleted_at.is_(None),
                        PublisherUploadJob.finished_at.is_(None),
                        PublisherUploadJob.status.in_(["running", "terminating"]),
                        PublisherUploadJob.current_attempt_id == "",
                    )
                    .with_for_update(skip_locked=True)
                )
                .scalars()
                .all()
            )
            for job in orphan_jobs:
                job.extension_client_id = ""
                if job.task_kind == "cover_generate":
                    if job.abort_requested or job.status == "terminating":
                        job.status = "cancelled"
                        job.finished_at = recovered_at
                        job.result_message = "封面生成任务已在重启恢复时取消。"
                    else:
                        job.status = "pending"
                        job.finished_at = None
                        job.result_message = "封面生成任务已在重启后恢复排队。"
                    job.available_at = None
                    job.reconcile_after = None
                else:
                    self._move_job_to_reconciling(job, now=recovered_at)
                job_ids.append(job.id)
            session.commit()
            return sorted(set(job_ids))

    def require_fence(
        self,
        session,
        *,
        job_id: str,
        attempt_id: str,
        worker_id: str,
        lease_epoch: int,
        now: datetime | None = None,
        require_live_lease: bool = True,
    ) -> tuple[PublisherUploadJob, PublisherUploadAttempt]:
        normalized_job_id = str(job_id or "").strip()
        normalized_attempt_id = str(attempt_id or "").strip()
        job = session.execute(
            select(PublisherUploadJob)
            .where(
                PublisherUploadJob.id == normalized_job_id,
                PublisherUploadJob.deleted_at.is_(None),
            )
            .with_for_update()
        ).scalar_one_or_none()
        if job is None:
            raise PublisherResourceNotFoundError("job", normalized_job_id)
        attempt = session.execute(
            select(PublisherUploadAttempt)
            .where(
                PublisherUploadAttempt.id == normalized_attempt_id,
                PublisherUploadAttempt.upload_job_id == normalized_job_id,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if attempt is None:
            raise PublisherResourceNotFoundError("attempt", normalized_attempt_id)
        if (
            job.current_attempt_id != attempt.id
            or attempt.worker_id != str(worker_id or "").strip()
            or attempt.lease_epoch != int(lease_epoch or 0)
            or attempt.status != "running"
        ):
            raise PublisherAttemptFenceError(
                "publisher attempt fence is no longer current",
                job_status=job.status,
                current_attempt_id=job.current_attempt_id,
            )
        if require_live_lease and not self._lease_is_live(attempt, now or utc_now()):
            raise PublisherLeaseExpiredError(
                "publisher attempt lease has expired",
                job_status=job.status,
                current_attempt_id=job.current_attempt_id,
            )
        return job, attempt

    def finish(
        self,
        session,
        *,
        job_id: str,
        attempt_id: str,
        worker_id: str,
        lease_epoch: int,
        status: str,
        now: datetime,
    ) -> tuple[PublisherUploadJob, PublisherUploadAttempt]:
        job, attempt = self.require_fence(
            session,
            job_id=job_id,
            attempt_id=attempt_id,
            worker_id=worker_id,
            lease_epoch=lease_epoch,
            now=now,
        )
        if status not in {"succeeded", "failed", "cancelled"}:
            raise ValueError("unsupported publisher attempt result status")

        uncertain = (
            self._attempt_is_uncertain(attempt) and job.task_kind != "audit_sync"
        )
        attempt.status = status
        attempt.finished_at = now
        attempt.heartbeat_at = now
        attempt.lease_expires_at = None
        job.current_attempt_id = ""
        job.extension_client_id = ""
        if status == "succeeded":
            job.status = "succeeded"
            job.finished_at = now
            job.available_at = None
            job.reconcile_after = None
            job.paused_at = None
            job.pause_reason = ""
        elif uncertain:
            self._move_job_to_reconciling(job, now=now)
        elif job.abort_requested:
            job.status = "cancelled"
            job.finished_at = now
            job.available_at = None
            job.reconcile_after = None
        else:
            job.status = "pending"
            job.finished_at = None
            job.available_at = now + self._retry_delay(attempt.attempt_number)
            job.reconcile_after = None
        return job, attempt

    def resolve_reconciliation(
        self,
        session,
        *,
        job_id: str,
        attempt_id: str,
        worker_id: str,
        lease_epoch: int,
        outcome: str,
        receipt_recorded: bool,
        pause_reason: str = "",
        now: datetime,
    ) -> tuple[PublisherUploadJob, PublisherUploadAttempt]:
        job, attempt = self.require_fence(
            session,
            job_id=job_id,
            attempt_id=attempt_id,
            worker_id=worker_id,
            lease_epoch=lease_epoch,
            now=now,
        )
        if attempt.attempt_kind != "reconcile":
            raise ValueError("publisher attempt is not a reconciliation attempt")
        if attempt.phase not in {"observation_started", "receipt_observed"}:
            raise PublisherInvalidTransitionError(
                "reconciliation requires observation_started phase acknowledgement"
            )
        normalized_outcome = str(outcome or "").strip()
        if normalized_outcome not in {
            "matched",
            "absent",
            "indeterminate",
            "risk_pause",
        }:
            raise ValueError("unsupported publisher reconciliation outcome")
        if normalized_outcome == "matched" and not receipt_recorded:
            raise PublisherReceiptRequiredError(
                "matched reconciliation requires a receipt"
            )
        if attempt.phase == "receipt_observed" and normalized_outcome != "matched":
            raise PublisherInvalidTransitionError(
                "receipt-confirmed reconciliation cannot report a negative outcome"
            )
        if (
            normalized_outcome == "absent"
            and job.platform_id not in self.authoritative_absence_platforms
        ):
            raise PublisherAbsenceNotAuthoritativeError(
                f"authoritative absence is not approved for {job.platform_id}"
            )

        attempt.finished_at = now
        attempt.heartbeat_at = now
        attempt.lease_expires_at = None
        job.current_attempt_id = ""
        job.extension_client_id = ""
        if normalized_outcome == "matched":
            attempt.status = "succeeded"
            attempt.phase = "receipt_observed"
            job.status = "succeeded"
            job.finished_at = now
            job.reconcile_after = None
            job.available_at = None
            job.paused_at = None
            job.pause_reason = ""
        elif normalized_outcome == "absent":
            attempt.status = "succeeded"
            attempt.phase = "observation_started"
            job.status = "cancelled" if job.abort_requested else "pending"
            job.finished_at = now if job.abort_requested else None
            job.reconcile_after = None
            job.available_at = None if job.abort_requested else now
        elif normalized_outcome == "risk_pause":
            attempt.status = "paused"
            attempt.phase = "observation_started"
            job.status = "paused"
            job.finished_at = None
            job.paused_at = now
            job.pause_reason = str(pause_reason or "publisher risk pause").strip()
            job.reconcile_after = None
            job.available_at = None
        else:
            attempt.status = "indeterminate"
            attempt.phase = "observation_started"
            job.status = "reconciling"
            job.finished_at = None
            job.reconcile_after = now + self._retry_delay(attempt.attempt_number)
            job.available_at = None
        return job, attempt

    def accept_late_receipt(
        self,
        session,
        *,
        job_id: str,
        attempt_id: str,
        worker_id: str,
        lease_epoch: int,
        now: datetime,
    ) -> tuple[PublisherUploadJob, PublisherUploadAttempt]:
        attempt = session.execute(
            select(PublisherUploadAttempt)
            .where(
                PublisherUploadAttempt.id == str(attempt_id or "").strip(),
                PublisherUploadAttempt.upload_job_id == str(job_id or "").strip(),
                PublisherUploadAttempt.worker_id == str(worker_id or "").strip(),
                PublisherUploadAttempt.lease_epoch == int(lease_epoch or 0),
            )
            .with_for_update()
        ).scalar_one_or_none()
        job = session.execute(
            select(PublisherUploadJob)
            .where(
                PublisherUploadJob.id == str(job_id or "").strip(),
                PublisherUploadJob.deleted_at.is_(None),
            )
            .with_for_update()
        ).scalar_one_or_none()
        if attempt is None or job is None:
            raise PublisherAttemptFenceError("stale publisher attempt fence")
        if job.current_attempt_id and job.current_attempt_id != attempt.id:
            current = session.get(PublisherUploadAttempt, job.current_attempt_id)
            if current is not None and current.status == "running":
                current.status = "superseded"
                current.finished_at = now
                current.lease_expires_at = None
        attempt.status = "succeeded"
        attempt.phase = "receipt_observed"
        attempt.finished_at = now
        attempt.heartbeat_at = now
        attempt.lease_expires_at = None
        job.status = "succeeded"
        job.current_attempt_id = ""
        job.extension_client_id = ""
        job.finished_at = now
        job.available_at = None
        job.reconcile_after = None
        job.paused_at = None
        job.pause_reason = ""
        return job, attempt

    @staticmethod
    def advance_phase(attempt: PublisherUploadAttempt, phase: str) -> None:
        normalized = str(phase or "").strip()
        phases = (
            RECONCILE_PHASES if attempt.attempt_kind == "reconcile" else EXECUTE_PHASES
        )
        if normalized not in phases:
            raise PublisherInvalidTransitionError(
                f"unsupported {attempt.attempt_kind} attempt phase: {normalized}"
            )
        current = str(attempt.phase or "claimed")
        if current not in phases or phases.index(normalized) < phases.index(current):
            raise PublisherInvalidTransitionError(
                "publisher attempt phase cannot move backwards"
            )
        attempt.phase = normalized

    def _expire_in_session(
        self,
        session,
        *,
        now: datetime,
        interrupted: bool,
    ) -> list[str]:
        predicate = PublisherUploadAttempt.status == "running"
        if not interrupted:
            predicate = predicate & (
                PublisherUploadAttempt.lease_expires_at.is_not(None)
                & (PublisherUploadAttempt.lease_expires_at <= now)
            )
        candidates = session.execute(
            select(
                PublisherUploadAttempt.id,
                PublisherUploadAttempt.upload_job_id,
            )
            .where(predicate)
            .order_by(PublisherUploadAttempt.created_at.asc())
        ).all()
        job_ids: list[str] = []
        for attempt_id, upload_job_id in candidates:
            job = session.execute(
                select(PublisherUploadJob)
                .where(PublisherUploadJob.id == upload_job_id)
                .with_for_update(skip_locked=True)
            ).scalar_one_or_none()
            if job is None:
                continue
            attempt = session.execute(
                select(PublisherUploadAttempt)
                .where(
                    PublisherUploadAttempt.id == attempt_id,
                    predicate,
                )
                .with_for_update(skip_locked=True)
            ).scalar_one_or_none()
            if attempt is None:
                continue
            attempt.status = "interrupted" if interrupted else "expired"
            attempt.finished_at = now
            attempt.lease_expires_at = None
            if (
                job.deleted_at is not None
                or job.current_attempt_id != attempt.id
                or job.status == "succeeded"
            ):
                continue
            job.current_attempt_id = ""
            job.extension_client_id = ""
            if interrupted or self._attempt_is_uncertain(attempt):
                self._move_job_to_reconciling(job, now=now)
            elif job.abort_requested:
                job.status = "cancelled"
                job.finished_at = now
                job.available_at = None
                job.reconcile_after = None
                job.result_message = "发布尝试在远端写入前终止。"
            else:
                job.status = "pending"
                job.finished_at = None
                job.available_at = now + self._retry_delay(attempt.attempt_number)
                job.reconcile_after = None
                job.result_message = "发布尝试租约已过期，任务等待重新领取。"
            self.audit.record_upload_job_event(
                session,
                job=job,
                event_type=DecisionEventType.UPLOAD_JOB_PROGRESS,
                summary="发布尝试租约已失效。",
                actor_type="system",
                extra_payload={
                    "attempt_id": attempt.id,
                    "attempt_kind": attempt.attempt_kind,
                    "attempt_phase": attempt.phase,
                    "lease_epoch": attempt.lease_epoch,
                    "interrupted": interrupted,
                    "worker_id": attempt.worker_id,
                },
            )
            job_ids.append(job.id)
        return sorted(set(job_ids))

    @staticmethod
    def _attempt_is_uncertain(attempt: PublisherUploadAttempt) -> bool:
        if attempt.attempt_kind == "reconcile":
            return True
        return str(attempt.phase or "claimed") in {
            "mutation_started",
            "receipt_observed",
        }

    @staticmethod
    def _move_job_to_reconciling(job: PublisherUploadJob, *, now: datetime) -> None:
        job.status = "reconciling"
        job.finished_at = None
        job.available_at = None
        job.reconcile_after = now
        job.result_message = "远端写入结果不确定，任务等待只读对账。"

    def _serialize_claim(
        self,
        job: PublisherUploadJob,
        attempt: PublisherUploadAttempt,
    ) -> dict[str, Any]:
        payload = self.serialize_state(job, attempt)
        payload["heartbeat_interval_seconds"] = max(
            5,
            self.default_lease_seconds // 3,
        )
        return payload

    def serialize_state(
        self,
        job: PublisherUploadJob,
        attempt: PublisherUploadAttempt,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        server_time = now or utc_now()
        payload = self.job_serializer(job)
        payload.update(
            {
                "attempt_id": attempt.id,
                "attempt_number": int(attempt.attempt_number or 0),
                "attempt_kind": attempt.attempt_kind,
                "attempt_status": attempt.status,
                "attempt_phase": attempt.phase,
                "lease_epoch": int(attempt.lease_epoch or 0),
                "lease_expires_at": self._datetime_text(attempt.lease_expires_at),
                "execution_mode": attempt.attempt_kind,
                "available_at": self._datetime_text(job.available_at),
                "reconcile_after": self._datetime_text(job.reconcile_after),
                "pause_reason": str(job.pause_reason or ""),
                "server_time": self._datetime_text(server_time),
            }
        )
        return payload

    @staticmethod
    def _payload(job: PublisherUploadJob) -> dict[str, Any]:
        try:
            payload = json.loads(job.result_payload_json or "{}")
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _lease_seconds(self, value: int | None) -> int:
        return max(10, int(value or self.default_lease_seconds))

    @staticmethod
    def _lease_is_live(
        attempt: PublisherUploadAttempt,
        now: datetime,
    ) -> bool:
        expires_at = as_utc(attempt.lease_expires_at)
        current = as_utc(now)
        return bool(
            expires_at is not None and current is not None and expires_at > current
        )

    @staticmethod
    def _retry_delay(attempt_number: int) -> timedelta:
        seconds = min(300, 2 ** min(max(1, int(attempt_number or 1)), 8))
        return timedelta(seconds=seconds)

    @staticmethod
    def _datetime_text(value: datetime | None) -> str:
        normalized = as_utc(value)
        return normalized.isoformat() if normalized is not None else ""


__all__ = [
    "PublisherAbsenceNotAuthoritativeError",
    "PublisherAttemptFenceError",
    "PublisherAttemptService",
    "PublisherInvalidTransitionError",
    "PublisherLeaseExpiredError",
    "PublisherProtocolError",
    "PublisherReceiptRequiredError",
    "PublisherResourceNotFoundError",
]
