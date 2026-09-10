from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ProductionPublishJob(BaseModel):
    job_id: str
    idempotency_key: str
    canon_commit_id: str
    candidate_id: str
    chapter_number: int
    platform: str


class ProductionBacklog(BaseModel):
    project_id: str
    capacity_available: int | None = None
    capacity_wait_reason: str = ""
    needs_plan: list[int] = Field(default_factory=list)
    planned_unwritten: list[int] = Field(default_factory=list)
    drafted_unreviewed: list[int] = Field(default_factory=list)
    needs_review: list[int] = Field(default_factory=list)
    reviewed_unpublished: list[int] = Field(default_factory=list)
    failed: list[int] = Field(default_factory=list)
    has_active_generation_task: bool = False
    has_active_upload_task: bool = False
    chapter_plan_count: int = 0
    has_existing_chapter_plans: bool = False
    scheduled_publish_jobs: list[ProductionPublishJob] = Field(default_factory=list)

    def publish_jobs_for(
        self,
        chapter_numbers: list[int],
        *,
        platforms: set[str],
    ) -> list[dict[str, Any]]:
        selected = {int(item) for item in chapter_numbers}
        return [
            item.model_dump(mode="json")
            for item in self.scheduled_publish_jobs
            if int(item.chapter_number) in selected and item.platform in platforms
        ]


__all__ = ["ProductionBacklog", "ProductionPublishJob"]
