from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from forwin.writer.llm.adapter import LLMChatResult
from forwin.audit.gate_outcome import parse_gate_outcome
from forwin.generation.gate_delegation import (
    GateDelegationRequest,
    GateDelegationService,
    GateAuditWriter,
    GateResolution,
    LLMGateDelegate,
)
from forwin.generation.pipeline_core.gate_delegation import GateDelegationStage
from forwin.models.audit import DecisionEvent
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.genesis import PromptTrace
from forwin.runtime.policy import RuntimePolicy
from forwin.state.updater import StateUpdater
from tests.postgres import postgres_test_url


class SpyLLMDelegate:
    def __init__(self) -> None:
        self.calls: list[tuple[object, GateDelegationRequest]] = []

    def resolve(self, *, audit_writer, request):
        self.calls.append((audit_writer, request))
        return GateResolution(
            resolved=True,
            approved=True,
            decision="approve",
            delegate="llm",
            reason="approved",
        )


def test_human_gate_policy_never_calls_llm() -> None:
    llm = SpyLLMDelegate()
    service = GateDelegationService(llm_delegate=llm)
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
    )

    assert outcome.delegate == "human"
    assert outcome.resolved is False
    assert outcome.approved is False
    assert llm.calls == []


def test_llm_gate_policy_calls_exact_delegate() -> None:
    llm = SpyLLMDelegate()
    service = GateDelegationService(llm_delegate=llm)
    policy = RuntimePolicy.for_profile("standard").with_user_settings(
        gate_delegate="llm"
    )
    audit_writer = GateAuditWriter(
        save_decision_event=lambda _event: None,
        save_prompt_trace=lambda **_payload: None,
    )
    request = GateDelegationRequest(
        project_id="project-1",
        task_id="task-1",
        gate_kind="chapter_review_interval",
        scope="chapter",
        chapter_number=3,
    )

    outcome = service.resolve(
        request,
        policy=policy,
        audit_writer=audit_writer,
    )

    assert outcome.approved is True
    assert llm.calls == [(audit_writer, request)]
    assert not hasattr(audit_writer, "session")
    assert not hasattr(audit_writer, "mark_chapter_status")


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


def _recording_writer(updater: RecordingUpdater) -> GateAuditWriter:
    return GateAuditWriter(
        save_decision_event=updater.save_decision_event,
        save_prompt_trace=updater.save_prompt_trace,
    )


class FakeLLM:
    def __init__(
        self, content: str, *, model: str = "deepseek-chat",
        profile_id: str = "env-deepseek", error: Exception | None = None,
    ) -> None:
        self.content = content
        self.model = model
        self.profile_id = profile_id
        self.error = error
        self.calls: list[dict[str, object]] = []
        self._attempts: list[dict[str, object]] = []

    def chat_with_meta(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        self._attempts = [{
            "attempt_group_id": "attempt-1",
            "profile_id": self.profile_id,
            "model": self.model,
            "_raw_request_payload": {
                "messages": messages,
                "authorization": "Bearer must-not-leak",
            },
            "_raw_response_text": self.content,
        }]
        if self.error is not None:
            raise self.error
        return LLMChatResult(self.content, self.profile_id, self.model)

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


def test_llm_delegate_records_served_model_and_complete_sanitized_trace() -> None:
    updater = RecordingUpdater()
    llm = FakeLLM(_approval_json())

    outcome = LLMGateDelegate(llm_client=llm).resolve(
        audit_writer=_recording_writer(updater),
        request=_delegation_request(),
    )

    assert outcome.resolved is True
    assert outcome.approved is True
    assert outcome.model == "deepseek-chat"
    assert llm.calls[0]["stage_key"] == "pause_gate"
    trace = updater.traces[0]
    assert trace["trace_scope"] == "gate_delegation"
    assert "complete chapter body" in str(trace["input_snapshot_json"])
    assert "must-remain" in str(trace["input_snapshot_json"])
    assert "must-not-leak" not in str(trace["input_snapshot_json"])
    assert "must-not-leak" not in str(trace["attempts_json"])
    assert "_raw_request_payload" in str(trace["attempts_json"])
    assert "_raw_response_text" in str(trace["attempts_json"])
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
    assert decided.overridden_by == "llm"
    assert decided.trace_ids == ["trace-1"]
    assert approved == decided


@pytest.mark.parametrize("profile_id, model", [("env-deepseek", "deepseek-chat"), ("env-kimi", "kimi-k2.5")])
def test_llm_delegate_accepts_served_chain_model(profile_id: str, model: str) -> None:
    updater = RecordingUpdater()
    llm = FakeLLM(_approval_json(), model=model, profile_id=profile_id)

    outcome = LLMGateDelegate(llm_client=llm).resolve(
        audit_writer=_recording_writer(updater), request=_delegation_request(),
    )

    assert outcome.resolved is True
    assert outcome.approved is True
    assert (outcome.profile_id, outcome.model) == (profile_id, model)
    assert "preferred_model" not in llm.calls[0]
    assert "permission_profile" not in llm.calls[0]
    assert json.loads(updater.traces[0]["model_profile_json"])["model"] == model


def test_failed_route_persists_complete_attempt_trace() -> None:
    updater = RecordingUpdater()
    llm = FakeLLM(
        "partial LLM response",
        error=RuntimeError("provider timeout"),
    )

    outcome = LLMGateDelegate(llm_client=llm).resolve(
        audit_writer=_recording_writer(updater),
        request=_delegation_request(),
    )

    assert outcome.approved is False
    assert outcome.failure_reason == "llm_call_failed"
    trace = updater.traces[0]
    assert "partial LLM response" in str(trace["attempts_json"])
    assert "provider timeout" in str(trace["output_summary_json"])


@pytest.mark.parametrize(
    ("content", "model", "error", "expected_failure"),
    [
        ("not-json", "deepseek-chat", None, "parse_or_schema"),
        ('{"decision":"approve"}', "deepseek-chat", None, "parse_or_schema"),
        (
            '{"decision":"approve","reason":"looks fine"}',
            "deepseek-chat",
            None,
            "parse_or_schema",
        ),
        (
            "partial response",
            "deepseek-chat",
            TimeoutError("timed out"),
            "llm_call_failed",
        ),
    ],
)
def test_llm_failure_modes_reject_with_trace_and_failure_event(
    content: str,
    model: str,
    error: Exception | None,
    expected_failure: str,
) -> None:
    updater = RecordingUpdater()
    llm = FakeLLM(content, model=model, error=error)

    outcome = LLMGateDelegate(llm_client=llm).resolve(
        audit_writer=_recording_writer(updater),
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


@pytest.mark.parametrize("status", ["pending", "fail", "error", "overridden"])
def test_noneligible_checkpoint_never_reaches_llm(status: str) -> None:
    calls: list[dict[str, object]] = []

    def resolve_gate(**kwargs):
        calls.append(kwargs)
        return GateResolution(
            resolved=True,
            approved=True,
            decision="approve",
            delegate="llm",
        )

    stage = SimpleNamespace(
        _resolve_gate_delegation=resolve_gate,
        _audit_task_id="task-1",
        policy=RuntimePolicy.for_profile("standard").with_user_settings(
            gate_delegate="llm"
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


def test_delegation_audit_failure_rolls_back_dedicated_transaction() -> None:
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
            def resolve(request, *, policy, audit_writer):
                del policy
                audit_writer.save_decision_event(
                    SimpleNamespace(
                        project_id=request.project_id,
                        event_type="injected_inside_savepoint",
                        summary="must roll back",
                        id="",
                        task_id="",
                        band_id="",
                        chapter_number=0,
                        scope="project",
                        event_family="runtime_observation",
                        actor_type="system",
                        actor_id="",
                        reason="",
                        payload={},
                        related_object_type="",
                        related_object_id="",
                        parent_event_id="",
                        causal_root_id="",
                    )
                )
                raise RuntimeError("savepoint failure")

        stage = SimpleNamespace(
            _audit_task_id="task-1",
            _audit_root_event_id="root-1",
            policy=RuntimePolicy.for_profile("standard").with_user_settings(
                gate_delegate="llm"
            ),
            gate_delegation=FailingDelegation(),
            _SessionFactory=Session,
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


def test_successful_llm_audit_survives_outer_transaction_rollback() -> None:
    database_url = postgres_test_url("gate-delegation-durable-audit")
    engine = get_engine(database_url)
    init_db(engine)
    Session = get_session_factory(engine)
    try:
        with Session.begin() as session:
            project = StateUpdater(session).create_project(
                title="Durable delegation audit",
                premise="LLM evidence must survive chapter rollback.",
                genre="test",
                runtime_policy=RuntimePolicy.for_profile("standard"),
            )
            project_id = project.id
        stage = SimpleNamespace(
            _audit_task_id="task-1",
            _audit_root_event_id="root-1",
            policy=RuntimePolicy.for_profile("standard").with_user_settings(
                gate_delegate="llm"
            ),
            gate_delegation=GateDelegationService(
                llm_delegate=LLMGateDelegate(
                    llm_client=FakeLLM(_approval_json())
                )
            ),
            _SessionFactory=Session,
        )
        session = Session()
        try:
            outcome = GateDelegationStage._resolve_gate_delegation(
                stage,
                updater=StateUpdater(session),
                project_id=project_id,
                gate_kind="chapter_review_interval",
                input_snapshot={"review": "eligible"},
                scope="chapter",
                chapter_number=1,
            )
            assert outcome.approved is True
            session.rollback()
        finally:
            session.close()

        with Session() as session:
            assert session.scalar(
                select(func.count(PromptTrace.id)).where(
                    PromptTrace.project_id == project_id,
                    PromptTrace.trace_scope == "gate_delegation",
                )
            ) == 1
            event_types = set(
                session.scalars(
                    select(DecisionEvent.event_type).where(
                        DecisionEvent.project_id == project_id
                    )
                )
            )
        assert event_types == {
            "gate_delegation_requested",
            "prompt_trace_recorded",
            "gate_delegation_decided",
            "gate_delegation_approved",
        }
    finally:
        engine.dispose()


def test_llm_delegate_module_has_no_authoritative_canon_write_surface() -> None:
    source = Path("forwin/generation/gate_delegation.py").read_text()

    for forbidden in (
        "forwin.canon",
        "CandidateDraftRepository",
        "CanonCommitRecord",
        "GraphDeltaRow",
        "EntityAdmissionCommitter",
        ".commit_plan(",
        ".mark_chapter_status(",
        "StateUpdater",
        ".session",
    ):
        assert forbidden not in source
