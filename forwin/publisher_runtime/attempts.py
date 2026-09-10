from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import case, func, or_, select

from forwin.audit.events import DecisionEventType
from forwin.models.base import new_id
from forwin.models.canon import CanonPublicationProtection
from forwin.models.publisher import (
    PublisherExtensionClient,
    PublisherOperatorAction,
    PublisherUploadAttempt,
    PublisherUploadJob,
    PublisherUploadReceipt,
)

from .audit import PublisherAuditService
from .browser_sessions import as_utc, utc_now
from .connection_state import ExtensionConnectionService
from .platform_catalog import PlatformCatalog
from .protection import (
    PublicationConflictError,
    PublicationPrefixWait,
    lock_job_chapter,
    lock_publisher_projects,
    release_absent_publication,
    require_active_job,
    reserve_publication,
)

EXECUTE_PHASES = ("claimed", "mutation_started", "receipt_observed")
RECONCILE_PHASES = ("claimed", "observation_started", "receipt_observed")
CLAIMABLE_TASK_KINDS = ("chapter_upload", "cover_upload", "audit_sync")
RISK_PAUSE_REASONS = ("captcha", "mfa", "account_risk")


class _RetryClaim:
    pass


_RETRY_CLAIM = _RetryClaim()


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

        # Expiry is its own short transaction; its books cannot invert the
        # subsequent selected job's Project -> Chapter lock order.
        with self.session_factory.begin() as expiry_session:
            self._expire_in_session(expiry_session, now=claimed_at, interrupted=False)
        # Each discarded pre-action payload commits before selecting another
        # book, so this scan never holds project locks in conflicting order.
        # Bound one request; later polls keep draining any larger stale backlog.
        for _ in range(100):
            result = self._claim_once(
                worker_id=worker_id,
                platforms=platforms,
                claimed_at=claimed_at,
                lease_duration=lease_duration,
            )
            if result is not _RETRY_CLAIM:
                return result
        return None

    def _claim_once(
        self,
        *,
        worker_id: str,
        platforms: list[str],
        claimed_at: datetime,
        lease_duration: int,
    ) -> dict[str, Any] | None | _RetryClaim:
        with self.session_factory() as session:
            client = self.connection_state.ensure_extension_client(session, worker_id)
            if client is None:
                raise ValueError("publisher attempt worker_id must be non-empty")
            # One worker may own only one claim; project/chapter locks precede jobs.
            session.execute(
                select(PublisherExtensionClient)
                .where(PublisherExtensionClient.client_id == worker_id)
                .with_for_update()
            ).scalar_one()
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
                lock_job_chapter(session, active_job_id)
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

            eligible = (
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
                .limit(1)
            )
            selected_id = session.scalar(
                eligible.with_only_columns(PublisherUploadJob.id)
            )
            if selected_id is None:
                session.commit()
                return None
            lock_job_chapter(session, selected_id)
            job = session.scalar(
                eligible.where(PublisherUploadJob.id == selected_id).with_for_update()
            )
            if job is None:
                session.commit()
                return None

            if job.status != "reconciling":
                try:
                    require_active_job(session, job)
                except ValueError as exc:
                    self._retire_stale_pending_job(
                        session,
                        job=job,
                        reason=str(exc),
                        now=claimed_at,
                    )
                    session.commit()
                    return _RETRY_CLAIM
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

    def _retire_stale_pending_job(
        self, session, *, job, reason: str, now: datetime
    ) -> None:
        protection = session.scalar(
            select(CanonPublicationProtection.state).where(
                CanonPublicationProtection.upload_job_id == job.id,
            )
        )
        uncertain = protection in {"reserved", "published"}
        if protection is None:
            uncertain = bool(
                session.scalar(
                    select(PublisherUploadAttempt.id)
                    .where(
                        PublisherUploadAttempt.upload_job_id == job.id,
                        or_(
                            PublisherUploadAttempt.attempt_kind == "reconcile",
                            PublisherUploadAttempt.phase.in_(
                                ["mutation_started", "receipt_observed"]
                            ),
                        ),
                    )
                    .limit(1)
                )
                or session.scalar(
                    select(PublisherUploadReceipt.id)
                    .where(
                        PublisherUploadReceipt.upload_job_id == job.id,
                    )
                    .limit(1)
                )
            )
        if uncertain:
            self._move_job_to_reconciling(job, now=now)
        else:
            job.status = "cancelled"
            job.abort_requested = True
            job.finished_at = now
            job.available_at = None
            job.result_message = "旧 Canon 发布载荷已在外部动作前作废。"
        job.error_message = reason
        self.audit.record_upload_job_event(
            session,
            job=job,
            event_type=(
                DecisionEventType.UPLOAD_JOB_PROGRESS
                if uncertain
                else DecisionEventType.UPLOAD_JOB_CANCELLED
            ),
            summary=(
                "旧发布身份保留保护，转入只读对账。"
                if uncertain
                else "旧 Canon 发布载荷已在外部动作前作废。"
            ),
            actor_type="system",
            extra_payload={"reason": reason},
        )

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
            if str(phase or "").strip() == "mutation_started":
                if attempt.attempt_kind != "execute":
                    raise PublisherInvalidTransitionError(
                        f"unsupported {attempt.attempt_kind} attempt phase: mutation_started"
                    )
                try:
                    reserve_publication(session, job)
                except PublicationPrefixWait as exc:
                    if attempt.phase != "claimed":
                        raise  # Existing external mutations retain their recovery owner.
                    attempt.status = "cancelled"
                    attempt.finished_at = transitioned_at
                    attempt.lease_expires_at = None
                    job.status = "pending"
                    job.current_attempt_id = ""
                    job.extension_client_id = ""
                    job.available_at = transitioned_at + timedelta(seconds=30)
                    job.reconcile_after = None
                    job.error_message = ""
                    job.result_message = (
                        "等待本平台前序章节确认公开；当前发布名额已释放。"
                    )
                    evidence = {
                        "reason": exc.reason,
                        "attempt_id": attempt.id,
                        "retry_at": job.available_at.isoformat(),
                    }
                    attempt.result_json = json.dumps(evidence, sort_keys=True)
                    payload = self._payload(job)
                    payload["publication_wait"] = evidence
                    job.result_payload_json = json.dumps(
                        payload, ensure_ascii=False, sort_keys=True
                    )
                    self.audit.record_upload_job_event(
                        session,
                        job=job,
                        event_type=DecisionEventType.UPLOAD_JOB_PROGRESS,
                        summary=job.result_message,
                        actor_type="system",
                        extra_payload=evidence,
                    )
                    session.commit()
                    raise
                except PublicationConflictError as exc:
                    # No external action was authorized. Persist the conflict so
                    # lease expiry cannot continually return this job to the front.
                    attempt.status = "paused"
                    attempt.finished_at = transitioned_at
                    attempt.lease_expires_at = None
                    attempt.error_code = exc.reason
                    attempt.error_message = str(exc)
                    job.status = "paused"
                    job.current_attempt_id = ""
                    job.extension_client_id = ""
                    job.paused_at = transitioned_at
                    job.pause_reason = exc.reason
                    job.available_at = None
                    job.reconcile_after = None
                    job.error_message = str(exc)
                    job.result_message = "同一章节已有受保护发布身份；当前任务已暂停。"
                    evidence = {
                        "conflicting_job_id": exc.conflicting_job_id,
                        "attempt_id": attempt.id,
                        "reason": exc.reason,
                    }
                    payload = self._payload(job)
                    payload["publication_conflict"] = evidence
                    job.result_payload_json = json.dumps(
                        payload, ensure_ascii=False, sort_keys=True
                    )
                    self.audit.record_upload_job_event(
                        session,
                        job=job,
                        event_type=DecisionEventType.UPLOAD_JOB_PAUSED,
                        summary=job.result_message,
                        actor_type="system",
                        extra_payload=evidence,
                    )
                    session.commit()
                    raise
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

    def pause(
        self,
        *,
        job_id: str,
        attempt_id: str,
        worker_id: str,
        lease_epoch: int,
        risk_reason: str,
        current_url: str = "",
        evidence: dict[str, Any] | None = None,
        client_observed_at: str = "",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        paused_at = now or utc_now()
        normalized_reason = self._risk_pause_reason(risk_reason)
        normalized_job_id = str(job_id or "").strip()
        normalized_attempt_id = str(attempt_id or "").strip()
        normalized_worker_id = str(worker_id or "").strip()
        with self.session_factory() as session:
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
            identity_matches = (
                attempt.worker_id == normalized_worker_id
                and attempt.lease_epoch == int(lease_epoch or 0)
            )
            payload = self._payload(job)
            prior_pause = payload.get("risk_pause")
            archived_pause = payload.get("risk_after_abort")
            if not isinstance(archived_pause, dict):
                archived_pause = payload.get("risk_termination")
            if (
                identity_matches
                and attempt.status == "paused"
                and job.status == "paused"
                and job.current_attempt_id == ""
                and job.pause_reason == normalized_reason
                and isinstance(prior_pause, dict)
                and prior_pause.get("pause_token") == attempt.id
            ):
                state = self.serialize_state(job, attempt, now=paused_at)
                state["pause_disposition"] = "idempotent"
                session.commit()
                return state
            if (
                identity_matches
                and job.abort_requested
                and job.current_attempt_id == ""
                and attempt.status in {"paused", "cancelled", "indeterminate"}
                and isinstance(archived_pause, dict)
                and archived_pause.get("pause_token") == attempt.id
                and archived_pause.get("risk_reason") == normalized_reason
            ):
                state = self.serialize_state(job, attempt, now=paused_at)
                state["pause_disposition"] = "idempotent"
                session.commit()
                return state
            if (
                not identity_matches
                or job.current_attempt_id != attempt.id
                or attempt.status != "running"
            ):
                raise PublisherAttemptFenceError(
                    "publisher attempt fence is no longer current",
                    job_status=job.status,
                    current_attempt_id=job.current_attempt_id,
                )
            if not self._lease_is_live(attempt, paused_at):
                raise PublisherLeaseExpiredError(
                    "publisher attempt lease has expired",
                    job_status=job.status,
                    current_attempt_id=job.current_attempt_id,
                )
            allowed_phases = (
                RECONCILE_PHASES
                if attempt.attempt_kind == "reconcile"
                else EXECUTE_PHASES
            )
            if attempt.phase not in allowed_phases:
                raise PublisherInvalidTransitionError(
                    "publisher risk pause requires a recognized attempt phase"
                )

            pause_payload = {
                "pause_token": attempt.id,
                "risk_reason": normalized_reason,
                "attempt_id": attempt.id,
                "attempt_kind": attempt.attempt_kind,
                "attempt_phase": attempt.phase,
                "lease_epoch": attempt.lease_epoch,
                "worker_id": attempt.worker_id,
                "current_url": str(current_url or "").strip(),
                "client_observed_at": str(client_observed_at or "").strip(),
                "paused_at": self._datetime_text(paused_at),
                "evidence": evidence if isinstance(evidence, dict) else {},
            }
            if job.abort_requested:
                self._converge_risk_after_abort(
                    session,
                    job=job,
                    attempt=attempt,
                    pause_payload=pause_payload,
                    now=paused_at,
                )
                session.commit()
                session.refresh(job)
                session.refresh(attempt)
                state = self.serialize_state(job, attempt, now=paused_at)
                state["pause_disposition"] = "abort_converged"
                return state
            attempt.status = "paused"
            attempt.finished_at = paused_at
            attempt.heartbeat_at = paused_at
            attempt.lease_expires_at = None
            attempt.error_code = normalized_reason
            attempt.error_message = str(
                pause_payload["evidence"].get("matched_text")
                or pause_payload["evidence"].get("message")
                or normalized_reason
            )
            attempt.result_json = json.dumps(
                pause_payload,
                ensure_ascii=False,
                sort_keys=True,
            )
            job.status = "paused"
            job.current_attempt_id = ""
            job.extension_client_id = ""
            job.finished_at = None
            job.available_at = None
            job.reconcile_after = None
            job.paused_at = paused_at
            job.pause_reason = normalized_reason
            if pause_payload["current_url"]:
                job.current_url = pause_payload["current_url"]
            job.result_message = "平台风险状态已暂停，等待操作员手工处理。"
            job.error_message = ""
            payload["risk_pause"] = pause_payload
            payload.pop("risk_resume", None)
            job.result_payload_json = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
            )
            self.audit.record_upload_job_event(
                session,
                job=job,
                event_type=DecisionEventType.UPLOAD_JOB_PAUSED,
                summary=f"发布任务因 {normalized_reason} 风险信号暂停。",
                actor_type="extension",
                extra_payload=pause_payload,
            )
            session.commit()
            session.refresh(job)
            session.refresh(attempt)
            state = self.serialize_state(job, attempt, now=paused_at)
            state["pause_disposition"] = "applied"
            return state

    def resume(
        self,
        *,
        job_id: str,
        expected_pause_reason: str,
        expected_pause_token: str,
        operator_reason: str,
        operator_actor_id: str,
        operator_auth_method: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        resumed_at = now or utc_now()
        normalized_job_id = str(job_id or "").strip()
        normalized_pause_reason = self._risk_pause_reason(expected_pause_reason)
        normalized_pause_token = str(expected_pause_token or "").strip()
        normalized_operator_reason = str(operator_reason or "").strip()
        normalized_actor_id = str(operator_actor_id or "").strip()
        normalized_auth_method = str(operator_auth_method or "").strip()
        if not normalized_pause_token:
            raise ValueError("publisher resume requires an expected pause token")
        if len(normalized_operator_reason) < 3:
            raise ValueError("publisher resume requires an operator reason")
        if not normalized_actor_id or not normalized_auth_method:
            raise ValueError("publisher resume requires an authenticated operator")
        if normalized_auth_method not in {"basic", "trusted_proxy"}:
            raise ValueError("publisher resume received an unsupported auth method")

        with self.session_factory() as session:
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
            prior_action = session.execute(
                select(PublisherOperatorAction).where(
                    PublisherOperatorAction.upload_job_id == normalized_job_id,
                    PublisherOperatorAction.action == "resume",
                    PublisherOperatorAction.pause_token == normalized_pause_token,
                )
            ).scalar_one_or_none()
            if job.status != "paused":
                if (
                    prior_action is not None
                    and prior_action.actor_id == normalized_actor_id
                    and prior_action.auth_method == normalized_auth_method
                    and prior_action.reason == normalized_operator_reason
                ):
                    session.commit()
                    return {
                        "disposition": "idempotent",
                        "job": self.job_serializer(job),
                        "transition": self._operator_action_transition(prior_action),
                    }
                raise PublisherInvalidTransitionError(
                    "publisher job is not paused",
                    job_status=job.status,
                )
            if job.abort_requested:
                raise PublisherInvalidTransitionError(
                    "publisher job cannot resume after abort was requested"
                )
            payload = self._payload(job)
            pause_payload = payload.get("risk_pause")
            current_pause_token = (
                str(pause_payload.get("pause_token") or "").strip()
                if isinstance(pause_payload, dict)
                else ""
            )
            if job.pause_reason != normalized_pause_reason:
                raise PublisherInvalidTransitionError(
                    "publisher pause reason does not match the expected pause reason",
                    expected_pause_reason=normalized_pause_reason,
                    actual_pause_reason=job.pause_reason,
                )
            if current_pause_token != normalized_pause_token:
                raise PublisherInvalidTransitionError(
                    "publisher pause token is stale",
                    expected_pause_token=normalized_pause_token,
                    actual_pause_token=current_pause_token,
                )
            attempt = session.execute(
                select(PublisherUploadAttempt)
                .where(
                    PublisherUploadAttempt.id == current_pause_token,
                    PublisherUploadAttempt.upload_job_id == job.id,
                    PublisherUploadAttempt.status == "paused",
                )
                .with_for_update()
            ).scalar_one_or_none()
            if attempt is None or job.current_attempt_id:
                raise PublisherAttemptFenceError(
                    "publisher paused attempt fence is no longer current"
                )

            old_state = self._job_state_snapshot(job)
            old_state.update(
                {
                    "attempt_kind": attempt.attempt_kind,
                    "attempt_phase": attempt.phase,
                }
            )
            needs_reconciliation = (
                attempt.attempt_kind == "reconcile"
                or self._attempt_is_uncertain(attempt)
            )
            new_status = "reconciling" if needs_reconciliation else "pending"
            job.status = new_status
            job.paused_at = None
            job.pause_reason = ""
            job.finished_at = None
            job.error_message = ""
            job.available_at = resumed_at if new_status == "pending" else None
            job.reconcile_after = resumed_at if new_status == "reconciling" else None
            job.result_message = (
                "风险暂停已由操作员解除，任务等待只读对账。"
                if needs_reconciliation
                else "风险暂停已由操作员解除，任务等待重新领取。"
            )
            transition = {
                "pause_token": normalized_pause_token,
                "pause_reason": normalized_pause_reason,
                "actor": normalized_actor_id,
                "auth_method": normalized_auth_method,
                "operator_reason": normalized_operator_reason,
                "transitioned_at": self._datetime_text(resumed_at),
                "attempt_id": attempt.id,
                "attempt_kind": attempt.attempt_kind,
                "attempt_phase": attempt.phase,
                "old_state": "paused",
                "new_state": new_status,
            }
            payload.pop("risk_pause", None)
            payload["risk_resume"] = transition
            job.result_payload_json = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
            )
            new_state = self._job_state_snapshot(job)
            action = PublisherOperatorAction(
                id=new_id(),
                upload_job_id=job.id,
                action="resume",
                pause_token=normalized_pause_token,
                actor_id=normalized_actor_id,
                auth_method=normalized_auth_method,
                reason=normalized_operator_reason,
                old_state_json=json.dumps(
                    old_state, ensure_ascii=False, sort_keys=True
                ),
                new_state_json=json.dumps(
                    new_state, ensure_ascii=False, sort_keys=True
                ),
                occurred_at=resumed_at,
            )
            session.add(action)
            self.audit.record_upload_job_event(
                session,
                job=job,
                event_type=DecisionEventType.UPLOAD_JOB_RESUMED,
                summary="发布任务风险暂停已由操作员解除。",
                actor_type="manual_ui",
                actor_id=normalized_actor_id,
                reason=normalized_operator_reason,
                event_family="audit_action",
                extra_payload={
                    **transition,
                    "old_state_snapshot": old_state,
                    "new_state_snapshot": new_state,
                },
            )
            session.commit()
            session.refresh(job)
            return {
                "disposition": "applied",
                "job": self.job_serializer(job),
                "transition": transition,
            }

    def recover_interrupted(self, *, now: datetime | None = None) -> list[str]:
        recovered_at = now or utc_now()
        with self.session_factory() as session:
            recovery_ids = list(
                session.scalars(
                    select(PublisherUploadJob.id).where(
                        PublisherUploadJob.status.in_(["running", "terminating"])
                    )
                )
            )
            lock_publisher_projects(session, recovery_ids)
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
                        PublisherUploadJob.task_kind != "cover_generate",
                    )
                    .with_for_update(skip_locked=True)
                )
                .scalars()
                .all()
            )
            for job in orphan_jobs:
                job.extension_client_id = ""
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
        lock_job_chapter(session, normalized_job_id)
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
        risk_reason: str = "",
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
        normalized_risk_reason = (
            self._risk_pause_reason(risk_reason)
            if normalized_outcome == "risk_pause"
            else ""
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
            release_absent_publication(session, job)
            attempt.status = "succeeded"
            attempt.phase = "observation_started"
            job.status = "cancelled" if job.abort_requested else "pending"
            job.finished_at = now if job.abort_requested else None
            job.reconcile_after = None
            job.available_at = None if job.abort_requested else now
        elif normalized_outcome == "risk_pause" and job.abort_requested:
            attempt.status = "indeterminate"
            attempt.phase = "observation_started"
            job.status = "reconciling"
            job.finished_at = None
            job.paused_at = None
            job.pause_reason = ""
            job.reconcile_after = now + self._retry_delay(attempt.attempt_number)
            job.available_at = None
            payload = self._payload(job)
            payload.pop("risk_pause", None)
            payload["risk_after_abort"] = {
                "pause_token": attempt.id,
                "risk_reason": normalized_risk_reason,
                "attempt_id": attempt.id,
                "attempt_kind": attempt.attempt_kind,
                "attempt_phase": attempt.phase,
                "lease_epoch": attempt.lease_epoch,
                "worker_id": attempt.worker_id,
                "paused_at": self._datetime_text(now),
                "next_status": job.status,
            }
            job.result_payload_json = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
            )
        elif normalized_outcome == "risk_pause":
            attempt.status = "paused"
            attempt.phase = "observation_started"
            job.status = "paused"
            job.finished_at = None
            job.paused_at = now
            job.pause_reason = normalized_risk_reason
            job.reconcile_after = None
            job.available_at = None
            payload = self._payload(job)
            payload["risk_pause"] = {
                "pause_token": attempt.id,
                "risk_reason": normalized_risk_reason,
                "attempt_id": attempt.id,
                "attempt_kind": attempt.attempt_kind,
                "attempt_phase": attempt.phase,
                "lease_epoch": attempt.lease_epoch,
                "worker_id": attempt.worker_id,
                "paused_at": self._datetime_text(now),
            }
            payload.pop("risk_resume", None)
            job.result_payload_json = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
            )
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
        lock_job_chapter(session, str(job_id or "").strip())
        job = session.execute(
            select(PublisherUploadJob)
            .where(
                PublisherUploadJob.id == str(job_id or "").strip(),
                PublisherUploadJob.deleted_at.is_(None),
            )
            .with_for_update()
        ).scalar_one_or_none()
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
        lock_publisher_projects(session, [job_id for _, job_id in candidates])
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
                "pause_token": self.pause_token(job),
                "paused_at": self._datetime_text(job.paused_at),
                "server_time": self._datetime_text(server_time),
            }
        )
        return payload

    def _converge_risk_after_abort(
        self,
        session,
        *,
        job: PublisherUploadJob,
        attempt: PublisherUploadAttempt,
        pause_payload: dict[str, Any],
        now: datetime,
    ) -> None:
        needs_reconciliation = (
            attempt.attempt_kind == "reconcile" or self._attempt_is_uncertain(attempt)
        )
        attempt.status = "indeterminate" if needs_reconciliation else "cancelled"
        attempt.finished_at = now
        attempt.heartbeat_at = now
        attempt.lease_expires_at = None
        attempt.error_code = str(pause_payload.get("risk_reason") or "")
        attempt.error_message = str(
            pause_payload.get("evidence", {}).get("matched_text")
            or pause_payload.get("evidence", {}).get("message")
            or pause_payload.get("risk_reason")
            or ""
        )
        archived_pause = {
            **pause_payload,
            "next_status": "reconciling" if needs_reconciliation else "cancelled",
        }
        attempt.result_json = json.dumps(
            archived_pause,
            ensure_ascii=False,
            sort_keys=True,
        )
        job.current_attempt_id = ""
        job.extension_client_id = ""
        job.status = str(archived_pause["next_status"])
        job.finished_at = None if needs_reconciliation else now
        job.available_at = None
        job.reconcile_after = now if needs_reconciliation else None
        job.paused_at = None
        job.pause_reason = ""
        if pause_payload.get("current_url"):
            job.current_url = str(pause_payload["current_url"])
        job.result_message = (
            "终止后检测到平台风险，保持只读对账。"
            if needs_reconciliation
            else "终止后检测到平台风险，任务已在远端写入前取消。"
        )
        payload = self._payload(job)
        payload.pop("risk_pause", None)
        payload["risk_after_abort"] = archived_pause
        job.result_payload_json = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
        )
        self.audit.record_upload_job_event(
            session,
            job=job,
            event_type=(
                DecisionEventType.UPLOAD_JOB_PROGRESS
                if needs_reconciliation
                else DecisionEventType.UPLOAD_JOB_CANCELLED
            ),
            summary="发布任务终止后收到平台风险报告。",
            actor_type="extension",
            extra_payload=archived_pause,
        )

    @staticmethod
    def _payload(job: PublisherUploadJob) -> dict[str, Any]:
        try:
            payload = json.loads(job.result_payload_json or "{}")
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    @classmethod
    def pause_token(cls, job: PublisherUploadJob) -> str:
        payload = cls._payload(job)
        risk_pause = payload.get("risk_pause")
        return (
            str(risk_pause.get("pause_token") or "").strip()
            if isinstance(risk_pause, dict)
            else ""
        )

    @staticmethod
    def _risk_pause_reason(value: str) -> str:
        normalized = str(value or "").strip()
        if normalized not in RISK_PAUSE_REASONS:
            raise PublisherInvalidTransitionError(
                "unsupported publisher risk pause reason"
            )
        return normalized

    @classmethod
    def _job_state_snapshot(cls, job: PublisherUploadJob) -> dict[str, Any]:
        return {
            "status": str(job.status or ""),
            "current_attempt_id": str(job.current_attempt_id or ""),
            "extension_client_id": str(job.extension_client_id or ""),
            "available_at": cls._datetime_text(job.available_at),
            "reconcile_after": cls._datetime_text(job.reconcile_after),
            "paused_at": cls._datetime_text(job.paused_at),
            "pause_reason": str(job.pause_reason or ""),
            "pause_token": cls.pause_token(job),
            "abort_requested": bool(job.abort_requested),
        }

    @staticmethod
    def _json_object(raw: str) -> dict[str, Any]:
        try:
            payload = json.loads(raw or "{}")
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    @classmethod
    def _operator_action_transition(
        cls,
        action: PublisherOperatorAction,
    ) -> dict[str, Any]:
        old_state = cls._json_object(action.old_state_json)
        new_state = cls._json_object(action.new_state_json)
        return {
            "pause_token": action.pause_token,
            "pause_reason": str(old_state.get("pause_reason") or ""),
            "actor": action.actor_id,
            "auth_method": action.auth_method,
            "operator_reason": action.reason,
            "transitioned_at": cls._datetime_text(action.occurred_at),
            "attempt_id": action.pause_token,
            "attempt_kind": str(old_state.get("attempt_kind") or ""),
            "attempt_phase": str(old_state.get("attempt_phase") or ""),
            "old_state": str(old_state.get("status") or "paused"),
            "new_state": str(new_state.get("status") or ""),
        }

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
    "RISK_PAUSE_REASONS",
]
