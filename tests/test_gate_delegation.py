from __future__ import annotations

import json
from types import SimpleNamespace

from forwin.generation.gate_delegation import (
    GateDelegationRequest,
    GateDelegationService,
    GateResolution,
    SPARK_GATE_MODEL,
    SparkGateDelegate,
)
from forwin.runtime.policy import RuntimePolicy


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
