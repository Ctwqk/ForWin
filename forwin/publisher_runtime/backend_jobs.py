from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select

from forwin.models.publisher import PublisherUploadJob
from .browser_sessions import utc_now
from .covers import PublisherCoverService


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

    def run_pending_once(self, *, limit: int = 1) -> list[str]:
        handled: list[str] = []
        for _ in range(max(1, int(limit or 1))):
            job_id = self.claim_next_cover_generate_job()
            if not job_id:
                break
            handled.append(job_id)
            try:
                self.cover_service.generate_for_job(job_id)
            except Exception as exc:  # noqa: BLE001
                self.mark_failed(job_id, exc)
        return handled

    def claim_next_cover_generate_job(self) -> str:
        now = utc_now()
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
                return ""
            job.status = "running"
            job.started_at = job.started_at or now
            job.claimed_at = job.claimed_at or now
            job.extension_client_id = "backend"
            job.result_message = "封面生成任务已被后端接管。"
            session.commit()
            return job.id

    def mark_failed(self, job_id: str, exc: Exception) -> None:
        now = utc_now()
        with self.session_factory() as session:
            job = session.get(PublisherUploadJob, job_id)
            if job is None:
                return
            payload = _load_json_object(job.result_payload_json)
            payload["failure"] = f"{exc.__class__.__name__}: {exc}"
            job.status = "failed"
            job.finished_at = now
            job.error_message = str(exc)
            job.result_message = "封面生成失败。"
            job.result_payload_json = json.dumps(payload, ensure_ascii=False)
            session.commit()
