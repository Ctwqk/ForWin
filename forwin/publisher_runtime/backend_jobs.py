from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select, text

from forwin.models.base import new_id
from forwin.models.publisher import PublisherUploadJob

from .browser_sessions import utc_now
from .covers import PublisherCoverService


PUBLISHER_BACKEND_WORKER_LOCK_ID = 0x466F7257696E0001


@dataclass(frozen=True, slots=True)
class PublisherBackendJobClaim:
    job_id: str
    owner_token: str


def _load_json_object(raw: str | None) -> dict[str, Any]:
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


class PublisherBackendJobRunner:
    def __init__(
        self,
        *,
        session_factory,
        cover_service: PublisherCoverService,
    ) -> None:
        self.session_factory = session_factory
        self.cover_service = cover_service

    @contextmanager
    def singleton_worker_lock(self) -> Iterator[None]:
        engine = self.session_factory.kw.get("bind")
        if engine is None:
            raise RuntimeError("publisher backend worker has no database bind")
        with engine.connect() as connection:
            acquired = bool(
                connection.execute(
                    text("SELECT pg_try_advisory_lock(:lock_id)"),
                    {"lock_id": PUBLISHER_BACKEND_WORKER_LOCK_ID},
                ).scalar_one()
            )
            if not acquired:
                raise RuntimeError(
                    "another publisher backend worker is already active"
                )
            connection.commit()
            try:
                yield
            finally:
                connection.execute(
                    text("SELECT pg_advisory_unlock(:lock_id)"),
                    {"lock_id": PUBLISHER_BACKEND_WORKER_LOCK_ID},
                )

    def run_pending_once(self, *, limit: int = 1) -> list[str]:
        handled: list[str] = []
        for _ in range(max(1, int(limit or 1))):
            claim = self.claim_next_cover_generate_job()
            if claim is None:
                break
            handled.append(claim.job_id)
            try:
                self.cover_service.generate_for_job(
                    claim.job_id,
                    owner_token=claim.owner_token,
                )
            except Exception as exc:  # noqa: BLE001
                self.mark_failed(
                    claim.job_id,
                    exc,
                    owner_token=claim.owner_token,
                )
        return handled

    def claim_next_cover_generate_job(
        self,
    ) -> PublisherBackendJobClaim | None:
        now = utc_now()
        owner_token = f"backend:{new_id()}"
        with self.session_factory() as session:
            job = session.execute(
                select(PublisherUploadJob)
                .where(
                    PublisherUploadJob.task_kind == "cover_generate",
                    PublisherUploadJob.status == "pending",
                    PublisherUploadJob.deleted_at.is_(None),
                )
                .order_by(PublisherUploadJob.created_at.asc())
                .with_for_update(skip_locked=True)
                .limit(1)
            ).scalar_one_or_none()
            if job is None:
                return None
            job.status = "running"
            job.started_at = job.started_at or now
            job.claimed_at = job.claimed_at or now
            job.extension_client_id = owner_token
            job.result_message = "封面生成任务已被后端接管。"
            session.commit()
            return PublisherBackendJobClaim(
                job_id=job.id,
                owner_token=owner_token,
            )

    def recover_interrupted_cover_jobs(
        self,
        *,
        now: datetime | None = None,
    ) -> list[str]:
        recovered_at = now or utc_now()
        with self.session_factory() as session:
            jobs = (
                session.execute(
                    select(PublisherUploadJob)
                    .where(
                        PublisherUploadJob.task_kind == "cover_generate",
                        PublisherUploadJob.status.in_(
                            ["running", "terminating"]
                        ),
                        PublisherUploadJob.current_attempt_id == "",
                        PublisherUploadJob.deleted_at.is_(None),
                    )
                    .order_by(PublisherUploadJob.created_at.asc())
                    .with_for_update()
                )
                .scalars()
                .all()
            )
            for job in jobs:
                job.extension_client_id = ""
                job.available_at = None
                job.reconcile_after = None
                if job.abort_requested or job.status == "terminating":
                    job.status = "cancelled"
                    job.finished_at = recovered_at
                    job.result_message = "封面生成任务已在重启恢复时取消。"
                else:
                    job.status = "pending"
                    job.finished_at = None
                    job.result_message = "封面生成任务已在重启后恢复排队。"
            session.commit()
            recovered_ids = sorted(job.id for job in jobs)
        self.cover_service.cleanup_orphaned_files()
        return recovered_ids

    def mark_failed(
        self,
        job_id: str,
        exc: Exception,
        *,
        owner_token: str,
    ) -> None:
        now = utc_now()
        with self.session_factory() as session:
            job = session.execute(
                select(PublisherUploadJob)
                .where(
                    PublisherUploadJob.id == job_id,
                    PublisherUploadJob.task_kind == "cover_generate",
                    PublisherUploadJob.deleted_at.is_(None),
                )
                .with_for_update()
            ).scalar_one_or_none()
            if job is None:
                return
            payload = _load_json_object(job.result_payload_json)
            if job.extension_client_id != owner_token:
                return
            if job.abort_requested or job.status == "terminating":
                payload["failure_after_abort"] = (
                    f"{exc.__class__.__name__}: {exc}"
                )
                job.status = "cancelled"
                job.finished_at = now
                job.result_message = "封面生成任务已取消。"
                job.result_payload_json = json.dumps(
                    payload,
                    ensure_ascii=False,
                )
                session.commit()
                return
            if job.status != "running":
                return
            payload["failure"] = f"{exc.__class__.__name__}: {exc}"
            job.status = "failed"
            job.finished_at = now
            job.error_message = str(exc)
            job.result_message = "封面生成失败。"
            job.result_payload_json = json.dumps(payload, ensure_ascii=False)
            session.commit()
