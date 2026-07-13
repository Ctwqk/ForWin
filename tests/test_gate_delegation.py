from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from forwin.audit.gate_outcome import parse_gate_outcome
from forwin.generation.gate_delegation import (
    GateDelegationRequest,
    GateDelegationService,
    GateResolution,
    SPARK_GATE_MODEL,
    SparkGateDelegate,
)
from forwin.generation.pipeline_core.gate_delegation import GateDelegationStage
from forwin.models.audit import DecisionEvent
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.runtime.policy import RuntimePolicy
from forwin.state.updater import StateUpdater
from tests.postgres import postgres_test_url


class SpySparkDelegate:
    def __init__(self) -> None:
        self.calls: list[tuple[object, GateDelegationRequest]] = []

    def resolve(self, *, updater, request):
        self.calls.append((updater, request))
        return GateResolution(
            resolved=True,
            approved=True,
            decision="approve",
            delegate="spark",
            reason="approved",
        )


def test_human_gate_policy_never_calls_spark() -> None:
    spark = SpySparkDelegate()
    service = GateDelegationService(spark_delegate=spark)
    request = GateDelegationRequest(
        project_id="project-1",
        task_id="task-1",
        gate_kind="chapter_review_interval",
        scope="chapter",
        chapter_number=3,
    )

    outcome = service.resolve(
        request,
        policy=RuntimePolicy.for_profile("standard"),
        updater=SimpleNamespace(),
    )

    assert outcome.delegate == "human"
    assert outcome.resolved is False
    assert outcome.approved is False
    assert spark.calls == []


def test_spark_gate_policy_calls_exact_delegate() -> None:
    spark = SpySparkDelegate()
    service = GateDelegationService(spark_delegate=spark)
    policy = RuntimePolicy.for_profile("standard").with_user_settings(
        gate_delegate="spark"
    )
    updater = SimpleNamespace()
    request = GateDelegationRequest(
        project_id="project-1",
        task_id="task-1",
        gate_kind="chapter_review_interval",
        scope="chapter",
        chapter_number=3,
    )

    outcome = service.resolve(request, policy=policy, updater=updater)

    assert outcome.approved is True
    assert spark.calls == [(updater, request)]


class RecordingUpdater:
    def __init__(self) -> None:
        self.events = []
        self.traces: list[dict[str, object]] = []

    def save_decision_event(self, event):
        self.events.append(event)
        return SimpleNamespace(
            id=f"event-{len(self.events)}",
            causal_root_id=event.causal_root_id or f"event-{len(self.events)}",
        )

    def save_prompt_trace(self, **payload):
        self.traces.append(payload)
        return SimpleNamespace(id=f"trace-{len(self.traces)}")


class FakeSparkLLM:
    def __init__(
        self,
        content: str,
        *,
        actual_model: str = SPARK_GATE_MODEL,
        backend: str = "codex_bridge",
        error: Exception | None = None,
        prove_actual_model: bool = True,
    ) -> None:
        self.content = content
        self.actual_model = actual_model
        self.backend = backend
        self.error = error
        self.prove_actual_model = prove_actual_model
        self.last_call_result = None
        self.calls: list[dict[str, object]] = []
        self._attempts: list[dict[str, object]] = []

    def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        self._attempts = [
            {
                "attempt_group_id": "attempt-1",
                "model": self.actual_model,
                "_raw_request_payload": {
                    "messages": messages,
                    "authorization": "Bearer must-not-leak",
                },
                "_raw_response_text": self.content,
            }
        ]
        trace = {
            "backend": self.backend,
            "raw_events": [{"type": "turn.completed", "content": self.content}],
        }
        if self.prove_actual_model:
            trace["actual_model"] = self.actual_model
        self.last_call_result = SimpleNamespace(
            backend=self.backend,
            fallback_used=False,
            trace=trace,
        )
        if self.error is not None:
            raise self.error
        return self.content

    def drain_llm_attempt_events(self):
        attempts = list(self._attempts)
        self._attempts.clear()
        return attempts


def _approval_json() -> str:
    return json.dumps(
        {
            "decision": "approve",
            "reason": "warning is nonblocking",
            "risk_level": "medium",
            "findings": ["warning only"],
            "evidence": ["continuity warning"],
        }
    )


def _delegation_request() -> GateDelegationRequest:
    return GateDelegationRequest(
        project_id="project-1",
        task_id="task-1",
        gate_kind="chapter_review_interval",
        scope="chapter",
        chapter_number=3,
        input_snapshot={
            "draft": "complete chapter body",
            "api_key": "must-not-leak",
            "story_secret": "must-remain",
        },
    )


def test_spark_delegate_proves_model_and_persists_complete_sanitized_trace() -> None:
    updater = RecordingUpdater()
    llm = FakeSparkLLM(_approval_json())

    outcome = SparkGateDelegate(llm_client=llm).resolve(
        updater=updater,
        request=_delegation_request(),
    )

    assert outcome.resolved is True
    assert outcome.approved is True
    assert outcome.actual_model == SPARK_GATE_MODEL
    assert llm.calls[0]["stage_key"] == "spark_pause_gate"
    trace = updater.traces[0]
    assert trace["trace_scope"] == "gate_delegation"
    assert "complete chapter body" in str(trace["input_snapshot_json"])
    assert "must-remain" in str(trace["input_snapshot_json"])
    assert "must-not-leak" not in str(trace["input_snapshot_json"])
    assert "must-not-leak" not in str(trace["attempts_json"])
    assert "_raw_request_payload" in str(trace["attempts_json"])
    assert "_raw_response_text" in str(trace["attempts_json"])
    assert "turn.completed" in str(trace["attempts_json"])
    assert {event.event_type for event in updater.events} == {
        "gate_delegation_requested",
        "prompt_trace_recorded",
        "gate_delegation_decided",
        "gate_delegation_approved",
    }
    events_by_type = {event.event_type: event for event in updater.events}
    requested = parse_gate_outcome(events_by_type["gate_delegation_requested"].payload)
    decided = parse_gate_outcome(events_by_type["gate_delegation_decided"].payload)
    approved = parse_gate_outcome(events_by_type["gate_delegation_approved"].payload)
    assert requested is not None
    assert requested.gate_id == "delegation"
    assert requested.responsibility_domain == "chapter_review_interval"
    assert requested.evaluated is False
    assert decided is not None
    assert decided.decision == "approve"
    assert decided.overridden_by == "spark"
    assert decided.trace_ids == ["trace-1"]
    assert approved == decided


def test_spark_delegate_uses_configured_codex_model() -> None:
    updater = RecordingUpdater()
    configured_model = "gpt-5.6-sol"
    llm = FakeSparkLLM(_approval_json(), actual_model=configured_model)

    outcome = SparkGateDelegate(
        llm_client=llm,
        requested_model=configured_model,
    ).resolve(
        updater=updater,
        request=_delegation_request(),
    )

    assert outcome.resolved is True
    assert outcome.approved is True
    assert outcome.requested_model == configured_model
    assert outcome.actual_model == configured_model
    assert llm.calls[0]["preferred_model"] == configured_model
    assert configured_model in str(updater.traces[0]["model_profile_json"])


def test_unproven_actual_model_fails_closed() -> None:
    updater = RecordingUpdater()
    llm = FakeSparkLLM(_approval_json(), prove_actual_model=False)

    outcome = SparkGateDelegate(llm_client=llm).resolve(
        updater=updater,
        request=_delegation_request(),
    )

    assert outcome.resolved is False
    assert outcome.approved is False
    assert outcome.failure_reason == "model_mismatch"
    assert outcome.actual_model == ""


def test_failed_route_persists_complete_bridge_trace() -> None:
    updater = RecordingUpdater()
    llm = FakeSparkLLM(
        "partial Spark response",
        error=RuntimeError("provider timeout"),
    )

    outcome = SparkGateDelegate(llm_client=llm).resolve(
        updater=updater,
        request=_delegation_request(),
    )

    assert outcome.approved is False
    assert outcome.failure_reason == "llm_call_failed"
    trace = updater.traces[0]
    assert "partial Spark response" in str(trace["attempts_json"])
    assert "provider timeout" in str(trace["output_summary_json"])


@pytest.mark.parametrize(
    ("content", "actual_model", "error", "expected_failure"),
    [
        ("not-json", SPARK_GATE_MODEL, None, "parse_or_schema"),
        ('{"decision":"approve"}', SPARK_GATE_MODEL, None, "parse_or_schema"),
        (_approval_json(), "gpt-5.6-sol", None, "model_mismatch"),
        (
            "partial response",
            SPARK_GATE_MODEL,
            TimeoutError("timed out"),
            "llm_call_failed",
        ),
    ],
)
def test_spark_failure_modes_reject_with_trace_and_failure_event(
    content: str,
    actual_model: str,
    error: Exception | None,
    expected_failure: str,
) -> None:
    updater = RecordingUpdater()
    llm = FakeSparkLLM(content, actual_model=actual_model, error=error)

    outcome = SparkGateDelegate(llm_client=llm).resolve(
        updater=updater,
        request=_delegation_request(),
    )

    assert outcome.resolved is False
    assert outcome.approved is False
    assert outcome.failure_reason == expected_failure
    assert len(updater.traces) == 1
    event_types = {event.event_type for event in updater.events}
    assert event_types == {
        "gate_delegation_requested",
        "prompt_trace_recorded",
        "gate_delegation_failed",
    }
    failure_event = next(
        event
        for event in updater.events
        if event.event_type == "gate_delegation_failed"
    )
    gate_outcome = parse_gate_outcome(failure_event.payload)
    assert gate_outcome is not None
    assert gate_outcome.decision == "error"
    assert gate_outcome.blocked is True
    assert expected_failure in gate_outcome.issue_keys
    assert gate_outcome.trace_ids == ["trace-1"]


@pytest.mark.parametrize("status", ["fail", "error"])
def test_blocking_checkpoint_never_reaches_spark(status: str) -> None:
    calls: list[dict[str, object]] = []

    def resolve_gate(**kwargs):
        calls.append(kwargs)
        return GateResolution(
            resolved=True,
            approved=True,
            decision="approve",
            delegate="spark",
        )

    stage = SimpleNamespace(
        _resolve_gate_delegation=resolve_gate,
        _audit_task_id="task-1",
        policy=RuntimePolicy.for_profile("standard").with_user_settings(
            gate_delegate="spark"
        ),
    )
    checkpoint = SimpleNamespace(
        id="checkpoint-1",
        project_id="project-1",
        arc_id="arc-1",
        band_id="band-1",
        chapter_start=1,
        chapter_end=10,
        trigger_source="auto_band_end",
        boundary_kind="band_end",
        boundary_chapter=10,
        status=status,
        summary="blocking checkpoint",
        reason="must not be delegated",
        issues_json="[]",
    )
    updater = SimpleNamespace(
        session=SimpleNamespace(
            add=lambda _row: None,
            flush=lambda: None,
        )
    )

    resolved = GateDelegationStage._resolve_checkpoint_gate(
        stage,
        updater=updater,
        checkpoint=checkpoint,
        gate_kind="band_checkpoint_pause",
        chapter_number=10,
    )

    assert resolved is False
    assert checkpoint.status == status
    assert calls == []


def test_nested_delegation_failure_rolls_back_savepoint() -> None:
    database_url = postgres_test_url("gate-delegation-savepoint")
    engine = get_engine(database_url)
    init_db(engine)
    Session = get_session_factory(engine)
    try:
        with Session.begin() as session:
            project = StateUpdater(session).create_project(
                title="Delegation savepoint",
                premise="Rollback delegated audit writes.",
                genre="test",
                runtime_policy=RuntimePolicy.for_profile("standard"),
            )
            project_id = project.id

        class FailingDelegation:
            @staticmethod
            def resolve(request, *, policy, updater):
                del policy
                updater.session.add(
                    DecisionEvent(
                        project_id=request.project_id,
                        event_type="injected_inside_savepoint",
                        summary="must roll back",
                    )
                )
                updater.session.flush()
                raise RuntimeError("savepoint failure")

        stage = SimpleNamespace(
            _audit_task_id="task-1",
            _audit_root_event_id="root-1",
            policy=RuntimePolicy.for_profile("standard").with_user_settings(
                gate_delegate="spark"
            ),
            gate_delegation=FailingDelegation(),
        )
        with Session.begin() as session:
            outcome = GateDelegationStage._resolve_gate_delegation(
                stage,
                updater=StateUpdater(session),
                project_id=project_id,
                gate_kind="chapter_review_interval",
                input_snapshot={"review": "eligible"},
                scope="chapter",
                chapter_number=1,
            )

        assert outcome.resolved is False
        assert outcome.approved is False
        assert outcome.failure_reason == "delegation_transaction_failed"
        with Session() as session:
            count = session.scalar(
                select(func.count(DecisionEvent.id)).where(
                    DecisionEvent.event_type == "injected_inside_savepoint"
                )
            )
        assert count == 0
    finally:
        engine.dispose()


def test_spark_delegate_module_has_no_authoritative_canon_write_surface() -> None:
    source = Path("forwin/generation/gate_delegation.py").read_text()

    for forbidden in (
        "forwin.canon",
        "CandidateDraftRepository",
        "CanonCommitRecord",
        "GraphDeltaRow",
        "EntityAdmissionCommitter",
        ".commit_plan(",
        ".mark_chapter_status(",
    ):
        assert forbidden not in source
