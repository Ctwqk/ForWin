from __future__ import annotations

import json
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.generation.task_payload import (
    GenerationTaskExecutionPayload,
    payload_to_json,
)
from forwin.models.task import GenerationTask


TERMINAL_GENERATION_STATUSES = frozenset(
    {
        "completed",
        "partial_failed",
        "failed",
        "needs_review",
        "cancelled",
        "paused",
    }
)


def new_task_id() -> str:
    return uuid.uuid4().hex[:12]


class GenerationTaskRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def has_active(self, project_id: str) -> bool:
        return (
            self.session.execute(
                select(GenerationTask.id)
                .where(
                    GenerationTask.project_id == project_id,
                    GenerationTask.deleted_at.is_(None),
                    GenerationTask.task_kind == "generation",
                    GenerationTask.status.notin_(TERMINAL_GENERATION_STATUSES),
                )
                .limit(1)
            ).first()
            is not None
        )

    def get_for_update(self, task_id: str) -> GenerationTask | None:
        return self.session.execute(
            select(GenerationTask)
            .where(GenerationTask.id == task_id)
            .with_for_update()
        ).scalar_one_or_none()

    def create(
        self,
        *,
        task_id: str,
        project_id: str,
        title: str,
        subtitle: str,
        message: str,
        requested_chapters: int,
        max_chapters: int,
        run_until_chapter: int,
        payload: GenerationTaskExecutionPayload,
    ) -> GenerationTask:
        task = GenerationTask(
            id=task_id,
            task_kind="generation",
            status="queued",
            title=title,
            subtitle=subtitle,
            project_id=project_id,
            message=message or f"开始生成 {requested_chapters} 章。",
            current_stage="queued",
            requested_chapters=requested_chapters,
            max_chapters=max_chapters,
            run_until_chapter=run_until_chapter,
            execution_payload_json=payload_to_json(payload),
        )
        self.session.add(task)
        self.session.flush()
        return task

    def update(self, task_id: str, changes: dict[str, object]) -> GenerationTask | None:
        task = self.session.get(GenerationTask, task_id)
        if task is None:
            return None
        scalar_fields = {
            "status": "status",
            "current_stage": "current_stage",
            "project_id": "project_id",
            "message": "message",
            "error": "error_message",
            "current_chapter": "current_chapter",
        }
        list_fields = {
            "completed_chapters": "completed_chapters_json",
            "failed_chapters": "failed_chapters_json",
            "paused_chapters": "paused_chapters_json",
            "frozen_artifacts": "frozen_artifacts_json",
        }
        for key, attribute in scalar_fields.items():
            if key in changes:
                value = changes[key]
                setattr(
                    task,
                    attribute,
                    str(value or "") if key == "error" else value,
                )
        for key, attribute in list_fields.items():
            if key in changes:
                setattr(
                    task,
                    attribute,
                    json.dumps(changes[key] or [], ensure_ascii=False),
                )
        self.session.add(task)
        self.session.flush()
        return task


__all__ = [
    "GenerationTaskRepository",
    "TERMINAL_GENERATION_STATUSES",
    "new_task_id",
]
