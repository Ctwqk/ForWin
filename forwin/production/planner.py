from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from .backlog import ProductionBacklog
from .policy import ProductionPolicy


class ProductionPlan(BaseModel):
    project_id: str
    date: str
    plan_chapters: list[int] = Field(default_factory=list)
    write_chapters: list[int] = Field(default_factory=list)
    review_chapters: list[int] = Field(default_factory=list)
    publish_chapters: list[int] = Field(default_factory=list)
    blocked_reason: str = ""
    notes: list[str] = Field(default_factory=list)
    generation_mode: str = ""
    requested_chapters: int = 0
    publish_jobs: list[dict[str, Any]] = Field(default_factory=list)


class ProductionPlanner:
    def plan(
        self,
        *,
        policy: ProductionPolicy,
        backlog: ProductionBacklog,
        now: datetime,
    ) -> ProductionPlan:
        plan = ProductionPlan(
            project_id=backlog.project_id,
            date=now.date().isoformat(),
        )
        if not policy.enabled:
            return plan.model_copy(update={"blocked_reason": "disabled"})
        if backlog.needs_review and policy.stop_when_review_pending:
            return plan.model_copy(update={"blocked_reason": "waiting_review"})
        if backlog.has_active_generation_task:
            return plan.model_copy(update={"blocked_reason": "active_generation_task"})

        if policy.quota.plan > 0:
            plan.plan_chapters.extend(backlog.needs_plan[: policy.quota.plan])
        if policy.quota.review > 0:
            review_candidates = [*backlog.needs_review, *backlog.drafted_unreviewed]
            plan.review_chapters.extend(review_candidates[: policy.quota.review])

        write_quota = max(0, int(policy.quota.write or 0))
        has_existing_chapter_plans = backlog.has_existing_chapter_plans or backlog.chapter_plan_count > 0
        if write_quota > 0 and not has_existing_chapter_plans:
            plan.write_chapters.extend(range(1, write_quota + 1))
            plan.generation_mode = "initial"
            plan.requested_chapters = write_quota
        elif write_quota > 0:
            write_candidates = [*backlog.planned_unwritten, *backlog.failed]
            plan.write_chapters.extend(write_candidates[:write_quota])
            if plan.write_chapters:
                plan.generation_mode = "continue"
                plan.requested_chapters = max(
                    int(backlog.chapter_plan_count or 0),
                    len(write_candidates),
                    len(plan.write_chapters),
                    1,
                )

        publish_quota = max(0, int(policy.quota.publish or 0))
        if (
            publish_quota > 0
            and policy.auto_publish
            and policy.publish_bindings
            and backlog.reviewed_unpublished
        ):
            if backlog.has_active_upload_task and policy.max_active_upload_tasks <= 1:
                plan.notes.append("active_upload_task")
            else:
                plan.publish_chapters.extend(backlog.reviewed_unpublished[:publish_quota])
                plan.publish_jobs.extend(backlog.publish_jobs_for(plan.publish_chapters))

        return plan
