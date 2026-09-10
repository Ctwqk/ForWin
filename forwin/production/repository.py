from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.generation.continue_workset import build_continue_generation_workset
from forwin.models.project import ChapterPlan
from forwin.models.publisher import PublisherUploadJob
from forwin.models.task import GenerationTask
from .backlog import ProductionBacklog, ProductionPublishJob


def _normalized_project_ids(project_ids: list[str]) -> list[str]:
    return [
        str(project_id or "").strip()
        for project_id in project_ids
        if str(project_id or "").strip()
    ]


class ProductionRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def load_backlogs(
        self,
        project_ids: list[str],
        *,
        generation_terminal_statuses: set[str],
        upload_terminal_statuses: set[str],
    ) -> dict[str, ProductionBacklog]:
        ids = _normalized_project_ids(project_ids)
        backlogs = {
            project_id: ProductionBacklog(project_id=project_id) for project_id in ids
        }
        if not ids:
            return backlogs

        plans = (
            self.session.execute(
                select(ChapterPlan)
                .where(ChapterPlan.project_id.in_(ids))
                .order_by(
                    ChapterPlan.project_id.asc(), ChapterPlan.chapter_number.asc()
                )
            )
            .scalars()
            .all()
        )
        plans_by_project: dict[str, list[ChapterPlan]] = defaultdict(list)
        for plan in plans:
            backlog = backlogs.get(str(plan.project_id or ""))
            if backlog is None:
                continue
            plans_by_project[str(plan.project_id or "")].append(plan)
            chapter_number = int(plan.chapter_number or 0)
            if chapter_number <= 0:
                continue
            backlog.chapter_plan_count += 1
            backlog.has_existing_chapter_plans = True
            status = str(plan.status or "").strip()
            if status == "planned":
                backlog.planned_unwritten.append(chapter_number)
            elif status == "failed":
                backlog.failed.append(chapter_number)
            elif status == "drafted":
                backlog.drafted_unreviewed.append(chapter_number)
            elif status == "needs_review":
                backlog.needs_review.append(chapter_number)
        self._attach_continue_worksets(backlogs, plans_by_project)

        self._attach_scheduled_publish_jobs(backlogs, ids)
        self._attach_active_generation_flags(
            backlogs,
            ids,
            terminal_statuses=generation_terminal_statuses,
        )
        self._attach_active_upload_flags(
            backlogs,
            ids,
            terminal_statuses=upload_terminal_statuses,
        )
        from .capacity import SerialCapacityService
        for project_id, backlog in backlogs.items():
            capacity = SerialCapacityService(self.session).snapshot(project_id, synchronize=False)
            backlog.capacity_available = capacity.available
            backlog.capacity_wait_reason = capacity.wait_reason
        return backlogs

    def _attach_continue_worksets(
        self,
        backlogs: dict[str, ProductionBacklog],
        plans_by_project: dict[str, list[ChapterPlan]],
    ) -> None:
        for project_id, backlog in backlogs.items():
            if not backlog.has_existing_chapter_plans:
                continue
            if backlog.needs_review or backlog.drafted_unreviewed:
                continue
            workset = build_continue_generation_workset(
                self.session,
                project_id,
                source="scheduler_continue",
                preloaded_plans=plans_by_project.get(project_id, []),
            )
            if not workset.chapter_numbers:
                backlog.planned_unwritten = []
                backlog.failed = []
                continue
            selected = set(workset.chapter_numbers)
            filtered_planned = [
                number for number in backlog.planned_unwritten if number in selected
            ]
            filtered_failed = [
                number for number in backlog.failed if number in selected
            ]
            if filtered_planned or filtered_failed:
                backlog.planned_unwritten = filtered_planned
                backlog.failed = filtered_failed
            else:
                backlog.planned_unwritten = list(workset.chapter_numbers)
                backlog.failed = []

    def _attach_scheduled_publish_jobs(
        self,
        backlogs: dict[str, ProductionBacklog],
        project_ids: list[str],
    ) -> None:
        if not project_ids:
            return
        jobs = (
            self.session.execute(
                select(PublisherUploadJob)
                .where(
                    PublisherUploadJob.deleted_at.is_(None),
                    PublisherUploadJob.project_id.in_(project_ids),
                    PublisherUploadJob.task_kind == "chapter_upload",
                    PublisherUploadJob.status == "scheduled",
                    PublisherUploadJob.canon_commit_id.is_not(None),
                    PublisherUploadJob.candidate_id != "",
                    PublisherUploadJob.chapter_number > 0,
                    PublisherUploadJob.idempotency_key != "",
                )
                .order_by(
                    PublisherUploadJob.project_id.asc(),
                    PublisherUploadJob.chapter_number.asc(),
                    PublisherUploadJob.platform_id.asc(),
                    PublisherUploadJob.id.asc(),
                )
            )
            .scalars()
            .all()
        )
        seen_chapters: dict[str, set[int]] = defaultdict(set)
        for job in jobs:
            project_id = str(job.project_id or "").strip()
            backlog = backlogs.get(project_id)
            if backlog is None:
                continue
            chapter_number = int(job.chapter_number or 0)
            if chapter_number not in seen_chapters[project_id]:
                backlog.reviewed_unpublished.append(chapter_number)
                seen_chapters[project_id].add(chapter_number)
            backlog.scheduled_publish_jobs.append(
                ProductionPublishJob(
                    job_id=job.id,
                    idempotency_key=job.idempotency_key,
                    canon_commit_id=str(job.canon_commit_id or ""),
                    candidate_id=job.candidate_id,
                    chapter_number=chapter_number,
                    platform=job.platform_id,
                )
            )

    def _attach_active_generation_flags(
        self,
        backlogs: dict[str, ProductionBacklog],
        project_ids: list[str],
        *,
        terminal_statuses: set[str],
    ) -> None:
        rows = (
            self.session.execute(
                select(GenerationTask.project_id)
                .where(
                    GenerationTask.deleted_at.is_(None),
                    GenerationTask.task_kind == "generation",
                    GenerationTask.project_id.in_(project_ids),
                    GenerationTask.status.notin_(tuple(terminal_statuses)),
                )
                .distinct()
            )
            .scalars()
            .all()
        )
        for project_id in rows:
            backlog = backlogs.get(str(project_id or "").strip())
            if backlog is not None:
                backlog.has_active_generation_task = True

    def _attach_active_upload_flags(
        self,
        backlogs: dict[str, ProductionBacklog],
        project_ids: list[str],
        *,
        terminal_statuses: set[str],
    ) -> None:
        rows = (
            self.session.execute(
                select(PublisherUploadJob.project_id)
                .where(
                    PublisherUploadJob.deleted_at.is_(None),
                    PublisherUploadJob.project_id.in_(project_ids),
                    PublisherUploadJob.status.notin_(tuple(terminal_statuses)),
                    PublisherUploadJob.status != "scheduled",
                )
                .distinct()
            )
            .scalars()
            .all()
        )
        for project_id in rows:
            backlog = backlogs.get(str(project_id or "").strip())
            if backlog is not None:
                backlog.has_active_upload_task = True
