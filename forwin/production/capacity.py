"""Single owner for serial capacity across enqueue, chapter execution and Canon.

Lock order is Project then GenerationTask. Reservations survive lease expiry: a
reclaimed task adopts its position under the new epoch, never an old worker.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select

from forwin.application.errors import GenerationTaskLeaseLost
from forwin.models.canon import CanonCommitRecord, CanonPublicationProtection
from forwin.models.capacity import (
    ChapterCapacityReservation,
    SerialCapacityConfigRevision,
)
from forwin.models.draft import CandidateDraftRecord
from forwin.models.project import ChapterPlan, Project
from forwin.models.task import GenerationTask
from forwin.production.policy import serial_buffer_limit


class CapacityWait(RuntimeError):
    """Normal, automatically resumable generation backpressure."""

    def __init__(self, reason: str, *, chapter_number: int = 0):
        self.reason = reason
        self.chapter_number = chapter_number
        super().__init__(reason)


@dataclass(frozen=True)
class CapacitySnapshot:
    total: int
    limit: int
    accepted: int
    published: int
    reserved: int
    available: int
    config_version: int
    wait_reason: str = ""


def contiguous_tail(numbers) -> int:
    tail = 0
    for number in sorted({int(n) for n in numbers if int(n) > 0}):
        if number != tail + 1:
            break
        tail = number
    return tail


def task_production_mode(task: GenerationTask) -> str:
    try:
        payload = json.loads(task.execution_payload_json or "{}")
    except (ValueError, TypeError):
        return "daily_serial"
    mode = payload.get("long_run_mode", "daily_serial")
    if mode in {"factory_batch", "soak_test"} and payload.get("isolated") is True:
        return mode
    return "daily_serial"


class SerialCapacityService:
    def __init__(self, session):
        self.session = session

    def _project(self, project_id):
        project = self.session.scalar(
            select(Project)
            .where(Project.id == project_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if project is None:
            raise CapacityWait("capacity_project_missing")
        return project

    def _configuration(self, project, *, synchronize=True):
        try:
            automation = json.loads(project.automation_json or "{}")
        except (ValueError, TypeError):
            automation = {}
        platform = str(automation.get("primary_publish_platform") or "").strip()
        total = max(0, int(project.target_total_chapters or 0))
        latest = self.session.scalar(
            select(SerialCapacityConfigRevision)
            .where(SerialCapacityConfigRevision.project_id == project.id)
            .order_by(SerialCapacityConfigRevision.version.desc())
            .limit(1)
        )
        if latest is None or (latest.total_chapters, latest.primary_platform) != (
            total,
            platform,
        ):
            latest = SerialCapacityConfigRevision(
                project_id=project.id,
                version=(latest.version + 1 if latest else 1),
                total_chapters=total,
                primary_platform=platform,
            )
            if synchronize:
                self.session.add(latest)
                self.session.flush()
        return latest

    def snapshot(
        self, project_id: str, *, synchronize: bool = True
    ) -> CapacitySnapshot:
        project = (
            self._project(project_id)
            if synchronize
            else self.session.get(Project, project_id)
        )
        if project is None:
            raise CapacityWait("capacity_project_missing")
        config = self._configuration(project, synchronize=synchronize)
        accepted_numbers = set(
            self.session.scalars(
                select(ChapterPlan.chapter_number)
                .join(
                    CanonCommitRecord,
                    CanonCommitRecord.id == ChapterPlan.active_commit_id,
                )
                .where(
                    ChapterPlan.project_id == project_id,
                    ChapterPlan.status == "accepted",
                    CanonCommitRecord.status == "committed",
                    CanonCommitRecord.chapter_plan_id == ChapterPlan.id,
                )
            )
        )
        accepted = contiguous_tail(accepted_numbers)
        # The receipt owner persists public facts independently of deletable jobs.
        published = (
            contiguous_tail(
                self.session.scalars(
                    select(CanonPublicationProtection.chapter_number)
                    .join(
                        CanonCommitRecord,
                        CanonCommitRecord.id
                        == CanonPublicationProtection.canon_commit_id,
                    )
                    .join(
                        ChapterPlan,
                        ChapterPlan.id == CanonPublicationProtection.chapter_plan_id,
                    )
                    .join(
                        CandidateDraftRecord,
                        CandidateDraftRecord.id == CanonCommitRecord.candidate_id,
                    )
                    .where(
                        CanonPublicationProtection.project_id == project_id,
                        CanonPublicationProtection.platform_id
                        == config.primary_platform,
                        CanonPublicationProtection.state == "published",
                        CanonPublicationProtection.content_sha256 != "",
                        CanonPublicationProtection.content_sha256
                        == CandidateDraftRecord.body_hash,
                        ChapterPlan.active_commit_id == CanonCommitRecord.id,
                        CanonCommitRecord.chapter_plan_id == ChapterPlan.id,
                        CanonCommitRecord.status == "committed",
                    )
                )
            )
            if config.primary_platform
            else 0
        )
        reserved = 0
        for reservation in self.session.scalars(
            select(ChapterCapacityReservation).where(
                ChapterCapacityReservation.project_id == project_id
            )
        ).all():
            task_query = select(GenerationTask).where(
                GenerationTask.id == reservation.task_id
            )
            if synchronize:
                task_query = task_query.with_for_update().execution_options(
                    populate_existing=True
                )
            task = self.session.scalar(task_query)
            if (
                reservation.chapter_number in accepted_numbers
                or task is None
                or task.deleted_at
                or task.status
                in {"failed", "partial_failed", "cancelled", "paused", "completed"}
            ):
                if synchronize:
                    self.session.delete(reservation)
            else:
                reserved += 1
        if synchronize:
            self.session.flush()
        limit = serial_buffer_limit(config.total_chapters)
        available = max(
            0,
            min(
                config.total_chapters - accepted - reserved,
                limit - accepted - reserved + published,
            ),
        )
        reason = ""
        if not config.primary_platform:
            reason, available = "primary_publish_platform_required", 0
        elif config.total_chapters <= 0:
            reason, available = "target_total_chapters_required", 0
        elif not available:
            reason = "serial_buffer_full"
        return CapacitySnapshot(
            config.total_chapters,
            limit,
            accepted,
            published,
            reserved,
            available,
            config.version,
            reason,
        )

    def _task(self, task_id, worker_id=None, lease_epoch=None):
        task = self.session.scalar(
            select(GenerationTask)
            .where(GenerationTask.id == task_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        expires = task.lease_expires_at if task else None
        if expires is not None and expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        if task is None or (
            worker_id is not None
            and (
                task.lease_owner != worker_id
                or task.lease_epoch != lease_epoch
                or task.status != "running"
                or (expires is not None and expires <= datetime.now(UTC))
            )
        ):
            raise GenerationTaskLeaseLost(f"generation task lease lost: {task_id}")
        return task

    def reserve(self, project_id, chapter_number, *, task_id, worker_id, lease_epoch):
        self._project(project_id)
        task = self._task(task_id, worker_id, lease_epoch)
        if task.project_id != project_id:
            raise GenerationTaskLeaseLost("reservation project does not match task")
        snap = self.snapshot(project_id)
        if chapter_number <= snap.accepted:
            return
        old = self.session.get(ChapterCapacityReservation, (project_id, chapter_number))
        if old is not None and old.task_id != task_id:
            previous = self._task(old.task_id)
            if previous.status != "needs_review":
                raise CapacityWait(
                    "chapter_reserved_by_another_task", chapter_number=chapter_number
                )
        offline = task_production_mode(task) != "daily_serial"
        if not offline and (
            snap.wait_reason
            in {"primary_publish_platform_required", "target_total_chapters_required"}
            or snap.accepted + snap.reserved + (0 if old else 1) - snap.published
            > snap.limit
            or chapter_number > snap.total
            or chapter_number > snap.published + snap.limit
        ):
            raise CapacityWait(
                snap.wait_reason or "serial_buffer_full", chapter_number=chapter_number
            )
        chapter = self.session.scalar(
            select(ChapterPlan).where(
                ChapterPlan.project_id == project_id,
                ChapterPlan.chapter_number == chapter_number,
            )
        )
        if (
            old
            and old.chapter_plan_id
            and chapter
            and old.chapter_plan_id != chapter.id
        ):
            raise CapacityWait(
                "capacity_chapter_identity_changed", chapter_number=chapter_number
            )
        if old is None:
            old = ChapterCapacityReservation(
                project_id=project_id,
                chapter_number=chapter_number,
                task_id=task_id,
                lease_epoch=lease_epoch,
                config_version=snap.config_version,
                chapter_plan_id=chapter.id if chapter else None,
            )
            self.session.add(old)
        else:
            old.task_id = task_id
            old.lease_epoch = lease_epoch
            old.config_version = snap.config_version
            old.chapter_plan_id = chapter.id if chapter else old.chapter_plan_id
        self.session.flush()

    def release(self, project_id, chapter_number, *, task_id, worker_id, lease_epoch):
        self._project(project_id)
        self._task(task_id, worker_id, lease_epoch)
        row = self.session.get(ChapterCapacityReservation, (project_id, chapter_number))
        if row and row.task_id == task_id and row.lease_epoch == lease_epoch:
            self.session.delete(row)
            self.session.flush()

    def candidate_provenance(
        self, project_id, chapter_number, *, parent_candidate_id=""
    ):
        from forwin.models.draft import CandidateDraftRecord

        row = self.session.get(ChapterCapacityReservation, (project_id, chapter_number))
        if row is not None:
            return {"generation_task_id": row.task_id}
        if parent_candidate_id:
            parent = self.session.get(CandidateDraftRecord, parent_candidate_id)
            if parent is not None:
                return {
                    "generation_task_id": json.loads(parent.metadata_json or "{}").get(
                        "generation_task_id", ""
                    )
                }
        return {}

    def commit_mode(self, project_id, chapter_number, *, candidate_id=""):
        row = self.session.get(ChapterCapacityReservation, (project_id, chapter_number))
        task = self.session.get(GenerationTask, row.task_id) if row else None
        if candidate_id and row:
            from forwin.models.draft import CandidateDraftRecord

            candidate = self.session.get(CandidateDraftRecord, candidate_id)
            provenance = (
                json.loads(candidate.metadata_json or "{}") if candidate else {}
            )
            if provenance.get("generation_task_id") != row.task_id:
                return "daily_serial"
        if task:
            return task_production_mode(task)
        current_mode = self.session.scalar(
            select(CanonCommitRecord.production_mode)
            .join(ChapterPlan, ChapterPlan.active_commit_id == CanonCommitRecord.id)
            .where(
                ChapterPlan.project_id == project_id,
                ChapterPlan.chapter_number == chapter_number,
            )
        )
        return current_mode or "daily_serial"

    def validate_commit(self, project_id, chapter_number, *, candidate_id=""):
        snap = self.snapshot(project_id)
        row = self.session.get(ChapterCapacityReservation, (project_id, chapter_number))
        if row:
            task = self._task(row.task_id)
            if task.lease_epoch != row.lease_epoch:
                raise CapacityWait(
                    "capacity_reservation_epoch_changed", chapter_number=chapter_number
                )
            mode = self.commit_mode(
                project_id, chapter_number, candidate_id=candidate_id
            )
            if task_production_mode(task) != "daily_serial" and mode == "daily_serial":
                raise CapacityWait(
                    "capacity_candidate_task_mismatch", chapter_number=chapter_number
                )
            if mode != "daily_serial":
                return
        # A revision replaces an existing slot; new admission consumes capacity.
        accepted = self.session.scalar(
            select(ChapterPlan.id).where(
                ChapterPlan.project_id == project_id,
                ChapterPlan.chapter_number == chapter_number,
                ChapterPlan.status == "accepted",
                ChapterPlan.active_commit_id.is_not(None),
            )
        )
        if accepted:
            return
        extra = 0 if row else 1
        if snap.wait_reason in {
            "primary_publish_platform_required",
            "target_total_chapters_required",
        } or (
            snap.accepted + snap.reserved + extra - snap.published > snap.limit
            or chapter_number > snap.total
            or chapter_number > snap.published + snap.limit
        ):
            raise CapacityWait(
                snap.wait_reason or "serial_buffer_full", chapter_number=chapter_number
            )

    def consume_commit(self, project_id, chapter_number):
        row = self.session.get(ChapterCapacityReservation, (project_id, chapter_number))
        if row:
            self.session.delete(row)
            self.session.flush()
