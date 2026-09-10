from __future__ import annotations

import importlib.util
import json
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from forwin.audit.events import DecisionEventType
from forwin.models.audit import DecisionEvent
from forwin.models.base import Base
from forwin.models.genesis import PromptTrace
from forwin.models.project import Project
from forwin.state.updater import StateUpdater


def test_execution_trace_keeps_root_and_caller_transaction_without_pipeline():
    assert (
        importlib.util.find_spec("forwin.observability.pipeline_trace") is not None
    ), "Writer needs an independent transaction-preserving trace owner"
    from forwin.observability.pipeline_trace import (
        PipelineAuditContext,
        PipelineTraceRecorder,
    )

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    spans = []
    audit = PipelineAuditContext(task_id="task", root_event_id="")
    recorder = PipelineTraceRecorder(
        audit=audit,
        artifact_store=None,
        observability=SimpleNamespace(_record_span=spans.append),
    )
    try:
        with Session(engine) as session:
            session.add(Project(id="book", title="Book", premise="Fixture"))
            session.commit()
            updater = StateUpdater(session)
            started = recorder.record_event(
                updater=updater,
                project_id="book",
                chapter_number=2,
                event_family="runtime_observation",
                event_type=DecisionEventType.LLM_REQUEST_STARTED,
                summary="Writer started",
            )
            assert audit.root_event_id == started.id
            trace_id = recorder.save_prompt_trace(
                session=session,
                updater=updater,
                project_id="book",
                decision_event_id=started.id,
                prompt_trace={
                    "trace_scope": "writer",
                    "stage_key": "chapter_rewrite",
                    "input_snapshot": {"chapter_number": 2},
                    "attempts": [
                        {
                            "stage_key": "scene_generation",
                            "model": "actual-writer-model",
                            "attempt_no": 1,
                            "attempt_group_id": "group",
                            "duration_ms": 123,
                            "error_class": "TimeoutError",
                            "error_message": "timed out",
                            "error_category": "timeout",
                        }
                    ],
                },
            )
            trace = session.get(PromptTrace, trace_id)
            assert trace.decision_event_id == started.id
            assert json.loads(trace.attempts_json)[0]["model"] == "actual-writer-model"
            recorded = list(session.scalars(select(DecisionEvent)))
            assert len(recorded) == 2
            assert {event.causal_root_id for event in recorded} == {started.id}
            assert {event.task_id for event in recorded} == {"task"}
            trace_event = next(row for row in recorded if row.id != started.id)
            assert trace_event.parent_event_id == started.id
            assert trace_event.related_object_id == trace_id
            assert len(spans) == 1
            assert spans[0].context.chapter_number == 2
            assert spans[0].context.operation_id == "task"
            assert spans[0].duration_ms == 123
            session.rollback()
            assert list(session.scalars(select(PromptTrace))) == []
            assert list(session.scalars(select(DecisionEvent))) == []
            assert session.get(Project, "book") is not None
    finally:
        engine.dispose()


def test_writer_failure_trace_uses_actual_attempts_and_same_transaction():
    from dataclasses import replace

    from tests.test_writer_execution_owner import _harness, _output

    output = _output()
    harness = _harness([TimeoutError("writer timed out")], [output], root_event_id="")
    harness.owner.skill_prompt_layer_builder = SimpleNamespace(build=lambda _skills: [])
    attempts = [
        {
            "stage_key": "scene_generation",
            "model": "actual-attempt-model",
            "attempt_no": 1,
            "attempt_group_id": "writer-group",
            "duration_ms": 123,
            "error_class": "TimeoutError",
            "error_message": "timed out",
            "error_category": "timeout",
        }
    ]
    drain_calls = []

    def drain():
        drain_calls.append(True)
        result = list(attempts)
        attempts.clear()
        return result

    harness.writer.llm_client.drain_llm_attempt_events = drain
    diagnostics = []
    harness.store.save_observability_diagnostic = lambda **kwargs: (
        diagnostics.append(kwargs) or {"path": "diagnostic.json"}
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            session.add(
                Project(id="project-writer-owner", title="Book", premise="Fixture")
            )
            session.commit()
            request = replace(harness.request, updater=StateUpdater(session))
            result = harness.owner.execute(request)
            assert result.unwrap() is output
            assert (
                len(drain_calls) == 2
            )  # Preserve the existing destructive-drain contract.
            traces = list(session.scalars(select(PromptTrace)))
            assert (
                len(traces) == 1
            )  # Success trace remains the review owner's responsibility.
            trace = traces[0]
            assert trace.stage_key == "chapter_draft"
            assert json.loads(trace.model_profile_json)["model"] == "active-model"
            assert json.loads(trace.attempts_json)[0]["model"] == "actual-attempt-model"
            events = list(session.scalars(select(DecisionEvent)))
            assert len({event.causal_root_id for event in events}) == 1
            primary_failed = next(
                row for row in events if row.id == trace.decision_event_id
            )
            assert primary_failed.event_type == DecisionEventType.LLM_REQUEST_FAILED
            assert [row.event_type for row in events[:2]] == [
                DecisionEventType.LLM_REQUEST_STARTED,
                DecisionEventType.LLM_REQUEST_FAILED,
            ]
            preview_events = [
                row
                for row in events
                if json.loads(row.payload_json).get("stage")
                == "chapter_preview_fallback"
            ]
            assert [row.event_type for row in preview_events] == [
                DecisionEventType.WRITER_PREVIEW_FALLBACK_STARTED,
                DecisionEventType.WRITER_PREVIEW_FALLBACK_SUCCEEDED,
            ]
            assert {row.parent_event_id for row in preview_events} == {
                primary_failed.id
            }
            assert len(diagnostics) == 1
            assert diagnostics[0]["trace_id"] == trace.id
            assert diagnostics[0]["source_event_id"] == primary_failed.id
            session.rollback()
            assert list(session.scalars(select(PromptTrace))) == []
            assert list(session.scalars(select(DecisionEvent))) == []
    finally:
        engine.dispose()


def test_rule_decision_trace_keeps_parent_and_best_effort_failure():
    from forwin.observability.pipeline_trace import (
        PipelineAuditContext,
        PipelineTraceRecorder,
    )
    from forwin.protocol.review import ReviewVerdict
    from forwin.review.decision.types import Decision, DecisionInput, PlanLayerHealth
    from tests.test_writer_execution_owner import _EventUpdater

    recorder = PipelineTraceRecorder(
        audit=PipelineAuditContext(root_event_id="root"),
        artifact_store=None,
        observability=None,
    )
    assert hasattr(recorder, "record_rule_decision"), (
        "Rule decisions need one concrete transaction-bound recorder"
    )
    updater = _EventUpdater()
    decision = Decision(
        rule_id="rule",
        outcome="local_repair",
        reason="Evidence requires repair",
        missing_evidence=[],
        routed_from="test",
        sub_action={},
    )
    row = recorder.record_rule_decision(
        updater=updater,
        decision=decision,
        decision_input=DecisionInput(
            project_id="book",
            chapter_number=2,
            review=ReviewVerdict(verdict="fail"),
            signals=[],
            open_obligations=[],
            attempts_completed=0,
            prior_scope_history=[],
            budget=None,
            target_total_chapters=10,
            plan_layer_health=PlanLayerHealth(),
        ),
        related_object_type="chapter_review",
        related_object_id="review",
        parent_event_id="parent",
    )
    assert row.info.event_type == DecisionEventType.RULE_DECISION_EVALUATED
    assert row.info.parent_event_id == "parent"
    assert row.info.causal_root_id == "root"
    assert row.info.reason == "Evidence requires repair"

    def fail(_info):
        raise RuntimeError("audit unavailable")

    assert (
        recorder.record_rule_decision(
            updater=SimpleNamespace(save_decision_event=fail),
            decision=decision,
            decision_input=DecisionInput(
                project_id="book",
                chapter_number=2,
                review=ReviewVerdict(verdict="fail"),
                signals=[],
                open_obligations=[],
                attempts_completed=0,
                prior_scope_history=[],
                budget=None,
                target_total_chapters=10,
                plan_layer_health=PlanLayerHealth(),
            ),
        )
        is None
    )
