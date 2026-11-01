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
    # Why publish-hub's release gate holds this job now ("" when it does not); a held job does
    # not take a slot of the day's publish quota (R38).
    hold_reason: str = ""


class ProductionBacklog(BaseModel):
    project_id: str
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
        """The jobs to release for the selected chapters; held jobs are left out."""
        selected = {int(item) for item in chapter_numbers}
        return [
            item.model_dump(mode="json")
            for item in self.scheduled_publish_jobs
            if int(item.chapter_number) in selected
            and item.platform in platforms
            and not item.hold_reason
        ]

    def held_publish_jobs(self, *, platforms: set[str]) -> list[dict[str, Any]]:
        """The lowest held job of each platform: the release point runs the gate on it alone.

        Running it creates a missing book and records why the binding's chapters wait; one job
        per binding is enough for both.
        """
        lowest: dict[str, ProductionPublishJob] = {}
        for item in self.scheduled_publish_jobs:
            if not item.hold_reason or item.platform not in platforms:
                continue
            current = lowest.get(item.platform)
            if current is None or int(item.chapter_number) < int(current.chapter_number):
                lowest[item.platform] = item
        return [
            lowest[platform].model_dump(mode="json") for platform in sorted(lowest)
        ]


__all__ = ["ProductionBacklog", "ProductionPublishJob"]
