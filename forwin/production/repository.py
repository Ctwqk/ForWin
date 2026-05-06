from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.models.project import ChapterPlan
from forwin.models.publisher import PublisherUploadJob
from forwin.models.task import GenerationTask
from forwin.state.query_helpers import load_latest_drafts_by_plan_id

from .backlog import ProductionBacklog, ProductionPublishChapter


def _normalized_project_ids(project_ids: list[str]) -> list[str]:
    return [str(project_id or "").strip() for project_id in project_ids if str(project_id or "").strip()]


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
            project_id: ProductionBacklog(project_id=project_id)
            for project_id in ids
        }
        if not ids:
            return backlogs

        plans = self.session.execute(
            select(ChapterPlan)
            .where(ChapterPlan.project_id.in_(ids))
            .order_by(ChapterPlan.project_id.asc(), ChapterPlan.chapter_number.asc())
        ).scalars().all()
        for plan in plans:
            backlog = backlogs.get(str(plan.project_id or ""))
            if backlog is None:
                continue
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

        accepted_plans = self.session.execute(
            select(ChapterPlan)
            .where(
                ChapterPlan.project_id.in_(ids),
                ChapterPlan.status == "accepted",
            )
            .order_by(ChapterPlan.project_id.asc(), ChapterPlan.chapter_number.asc())
        ).scalars().all()
        self._attach_reviewed_unpublished(backlogs, accepted_plans)
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
        return backlogs

    def _attach_reviewed_unpublished(
        self,
        backlogs: dict[str, ProductionBacklog],
        accepted_plans: list[ChapterPlan],
    ) -> None:
        if not accepted_plans:
            return
        plan_ids = [plan.id for plan in accepted_plans]
        draft_by_plan_id = load_latest_drafts_by_plan_id(self.session, plan_ids)
        accepted_titles_by_project: dict[str, set[str]] = defaultdict(set)
        for plan in accepted_plans:
            title = str(plan.title or "").strip()
            if title:
                accepted_titles_by_project[str(plan.project_id or "")].add(title)
        uploaded_or_queued_titles: dict[str, set[str]] = defaultdict(set)
        title_filters = {
            title
            for titles in accepted_titles_by_project.values()
            for title in titles
        }
        if title_filters:
            rows = self.session.execute(
                select(
                    PublisherUploadJob.project_id,
                    PublisherUploadJob.chapter_title,
                ).where(
                    PublisherUploadJob.deleted_at.is_(None),
                    PublisherUploadJob.project_id.in_(list(accepted_titles_by_project.keys())),
                    PublisherUploadJob.chapter_title.in_(title_filters),
                    PublisherUploadJob.status.in_(["pending", "running", "terminating", "succeeded"]),
                )
            ).all()
            for project_id, chapter_title in rows:
                normalized_project_id = str(project_id or "").strip()
                normalized_title = str(chapter_title or "").strip()
                if normalized_project_id and normalized_title:
                    uploaded_or_queued_titles[normalized_project_id].add(normalized_title)

        for plan in accepted_plans:
            project_id = str(plan.project_id or "").strip()
            backlog = backlogs.get(project_id)
            if backlog is None:
                continue
            title = str(plan.title or "").strip()
            if title and title in uploaded_or_queued_titles.get(project_id, set()):
                continue
            draft = draft_by_plan_id.get(plan.id)
            if draft is None:
                continue
            chapter_number = int(plan.chapter_number or 0)
            backlog.reviewed_unpublished.append(chapter_number)
            backlog.reviewed_unpublished_payloads.append(
                ProductionPublishChapter(
                    chapter_number=chapter_number,
                    chapter_title=title or f"第{chapter_number}章",
                    body=str(draft.body_text or ""),
                )
            )

    def _attach_active_generation_flags(
        self,
        backlogs: dict[str, ProductionBacklog],
        project_ids: list[str],
        *,
        terminal_statuses: set[str],
    ) -> None:
        rows = self.session.execute(
            select(GenerationTask.project_id)
            .where(
                GenerationTask.deleted_at.is_(None),
                GenerationTask.task_kind == "generation",
                GenerationTask.project_id.in_(project_ids),
                GenerationTask.status.notin_(tuple(terminal_statuses)),
            )
            .distinct()
        ).scalars().all()
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
        rows = self.session.execute(
            select(PublisherUploadJob.project_id)
            .where(
                PublisherUploadJob.deleted_at.is_(None),
                PublisherUploadJob.project_id.in_(project_ids),
                PublisherUploadJob.status.notin_(tuple(terminal_statuses)),
            )
            .distinct()
        ).scalars().all()
        for project_id in rows:
            backlog = backlogs.get(str(project_id or "").strip())
            if backlog is not None:
                backlog.has_active_upload_task = True
