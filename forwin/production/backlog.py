from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ProductionPublishChapter(BaseModel):
    chapter_number: int
    chapter_title: str
    body: str


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
    reviewed_unpublished_payloads: list[ProductionPublishChapter] = Field(default_factory=list)

    def publish_jobs_for(self, chapter_numbers: list[int]) -> list[dict[str, Any]]:
        selected = {int(item) for item in chapter_numbers}
        return [
            {
                "chapter_title": item.chapter_title,
                "body": item.body,
            }
            for item in self.reviewed_unpublished_payloads
            if int(item.chapter_number) in selected
        ]
