"""Durable, replay-safe generation handoff on the existing outbox."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from forwin.generation.auto_continue import GenerationAutoContinueController
from forwin.generation.pipeline_core.result import RunResult
from forwin.generation.task_payload import payload_from_json
from forwin.generation.task_repository import TERMINAL_GENERATION_STATUSES
from forwin.models.audit import DecisionEvent
from forwin.models.outbox import OutboxEvent
from forwin.models.task import GenerationTask
from forwin.outbox.store import enqueue_outbox_event

GENERATION_CONTINUATION_REQUESTED = "generation.continuation.requested.v1"


def continuation_event_id(parent_task_id: str) -> str:
    return f"generation-continuation:{parent_task_id}:v1"


def continuation_decision_id(parent_task_id: str) -> str:
    return f"generation-continuation-decision:{parent_task_id}:v1"


class GenerationCompletionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    project_id: str
    failure_reason: str = ""
    requested_chapters: int = 0
    completed_chapters: list[int] = Field(default_factory=list)
    failed_chapters: list[int] = Field(default_factory=list)
    paused_chapters: list[int] = Field(default_factory=list)
    frozen_artifacts: list[str] = Field(default_factory=list)
    system_block_chapters: list[int] = Field(default_factory=list)
    cancelled: bool = False
    paused: bool = False
    capacity_wait_reason: str = ""
    capacity_wait_chapter: int = 0

    @property
    def status(self) -> str:
        if self.failure_reason:
            return "failed"
        return RunResult(**self.model_dump(exclude={"failure_reason"})).status

    @classmethod
    def from_result(cls, result):
        return cls.model_validate(
            {
                name: getattr(result, name)
                for name in cls.model_fields
                if hasattr(result, name)
            }
        )


class GenerationContinuationEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1] = 1
    parent_task_id: str = Field(min_length=1)
    result: GenerationCompletionResult


def enqueue_continuation(session, task, result: GenerationCompletionResult):
    payload = GenerationContinuationEvent(parent_task_id=task.id, result=result)
    enqueue_outbox_event(
        session,
        event_id=continuation_event_id(task.id),
        aggregate_type="generation_task",
        aggregate_id=task.id,
        event_type=GENERATION_CONTINUATION_REQUESTED,
        payload=payload.model_dump(mode="json"),
    )


def continuation_pending(session, task: GenerationTask) -> bool:
    """Read-side capability; mutations recheck this under Project → Task locks."""
    if (
        task.status not in TERMINAL_GENERATION_STATUSES
        or task.deleted_at
        or task.pause_requested
        or task.cancel_requested
    ):
        return False
    if session.get(DecisionEvent, continuation_decision_id(task.id)) is not None:
        return False
    if session.scalar(
        select(GenerationTask.id).where(
            GenerationTask.continuation_parent_task_id == task.id
        )
    ):
        return False
    row = session.scalar(
        select(OutboxEvent).where(
            OutboxEvent.event_id == continuation_event_id(task.id)
        )
    )
    return bool(row and payload_from_json(task.execution_payload_json).auto_continue)


def consume_continuation(session, event: GenerationContinuationEvent, application):
    return GenerationAutoContinueController(session).consume(event, application)


def build_generation_continuation_handlers(*, session_factory, infrastructure):
    def handle(claim):
        event = GenerationContinuationEvent.model_validate(dict(claim.payload))
        if (
            claim.event_type != GENERATION_CONTINUATION_REQUESTED
            or claim.event_id != continuation_event_id(event.parent_task_id)
            or claim.aggregate_type != "generation_task"
            or claim.aggregate_id != event.parent_task_id
        ):
            raise ValueError("invalid generation continuation envelope")
        from forwin.application.generation import GenerationApplicationService

        application = GenerationApplicationService(
            session_factory=session_factory, infrastructure=infrastructure
        )
        with session_factory.begin() as session:
            consume_continuation(session, event, application)

    return {GENERATION_CONTINUATION_REQUESTED: handle}
