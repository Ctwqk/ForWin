from __future__ import annotations

import importlib
import importlib.util
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from forwin.audit.events import DecisionEventType
from forwin.models.audit import DecisionEvent
from forwin.observability.pipeline_trace import (
    PipelineAuditContext,
    PipelineTraceRecorder,
)
from forwin.observability.service import ObservabilityService
from forwin.observability.spans import current_span
from forwin.state.updater import StateUpdater
from tests.test_candidate_review_owner import database as _shared_database

database = _shared_database


def _owners(session, callback=None, pause=None):
    assert (
        importlib.util.find_spec("forwin.observability.pipeline_progress") is not None
    ), "A0 needs transaction-bound progress owner"
    assert importlib.util.find_spec("forwin.review.repair.control") is not None, (
        "Repair needs finite pause/stage control"
    )
    progress_module = importlib.import_module("forwin.observability.pipeline_progress")
    control_module = importlib.import_module("forwin.review.repair.control")
    records = []
    obs = ObservabilityService(
        performance_span_sink=SimpleNamespace(record_span=records.append)
    )
    trace = PipelineTraceRecorder(
        audit=PipelineAuditContext(task_id="task"),
        artifact_store=None,
        observability=obs,
    )
    progress = progress_module.PipelineProgressRecorder(
        trace_recorder=trace, observability=obs, progress_callback=callback
    )
    progress.bind(project_id="book", updater=StateUpdater(session))
    return (
        progress,
        control_module.RepairControl(progress=progress, should_pause=pause),
        records,
    )


@pytest.mark.parametrize("terminal_stage", ["paused", "failed"])
def test_repair_stages_keep_same_transaction_and_close_span_on_clear(
    database, terminal_stage
):
    session, _ = database
    notifications = []
    progress, control, spans = _owners(
        session, lambda event, payload: notifications.append((event, payload))
    )
    try:
        control.notify("repairing_chapter", project_id="book", chapter_number=1)
        assert current_span() is not None
        control.notify("repair_review", project_id="book", chapter_number=1)
        progress.emit(
            "stage_changed", stage=terminal_stage, project_id="book", current_chapter=1
        )
        events = list(session.scalars(select(DecisionEvent)))
        assert [row.event_type for row in events] == [
            DecisionEventType.STAGE_ENTERED,
            DecisionEventType.STAGE_EXITED,
            DecisionEventType.STAGE_DURATION_SUMMARY,
            DecisionEventType.STAGE_ENTERED,
            DecisionEventType.STAGE_EXITED,
            DecisionEventType.STAGE_DURATION_SUMMARY,
            DecisionEventType.STAGE_ENTERED,
        ]
        assert len({row.causal_root_id for row in events}) == 1
        assert {row.chapter_number for row in events} == {1}
        assert {row.task_id for row in events} == {"task"}
        progress.clear()
        assert current_span() is None
        assert [span.span_name for span in spans] == [
            "stage.repairing_chapter",
            "stage.repair_review",
            f"stage.{terminal_stage}",
        ]
        assert notifications[0] == (
            "stage_changed",
            {"stage": "repairing_chapter", "project_id": "book", "current_chapter": 1},
        )
        session.rollback()
        assert list(session.scalars(select(DecisionEvent))) == []
    finally:
        progress.clear()


def test_progress_callback_error_keeps_stage_event_and_clear_closes_span(database):
    session, _ = database

    def fail(*_args):
        raise RuntimeError("UI disconnected")

    progress, control, spans = _owners(session, fail)
    try:
        control.notify("repairing_chapter", project_id="book", chapter_number=1)
        assert len(list(session.scalars(select(DecisionEvent)))) == 1
    finally:
        progress.clear()
    assert current_span() is None
    assert [span.span_name for span in spans] == ["stage.repairing_chapter"]


@pytest.mark.parametrize(
    "result,want",
    [(True, True), (False, False), (RuntimeError("predicate unavailable"), False)],
)
def test_repair_pause_stays_separate_from_progress_and_tolerates_predicate_error(
    database, result, want
):
    session, _ = database

    def pause():
        if isinstance(result, Exception):
            raise result
        return result

    progress, control, spans = _owners(session, pause=pause)
    assert control.paused() is want
    assert list(session.scalars(select(DecisionEvent))) == []
    assert spans == []
    progress.clear()
