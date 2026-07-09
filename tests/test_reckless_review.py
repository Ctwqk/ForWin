from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.genesis import PromptTrace
from forwin.models.governance import DecisionEvent
from forwin.models.project import Project
from forwin.reckless_review import (
    RECKLESS_REVIEW_MODEL,
    RecklessReviewAgent,
    RecklessReviewRequest,
)
from forwin.state.updater import StateUpdater


class FakeReviewLLM:
    def __init__(
        self,
        content: str,
        *,
        actual_model: str = RECKLESS_REVIEW_MODEL,
        backend: str = "ordinary",
        error: Exception | None = None,
    ) -> None:
        self.content = content
        self.actual_model = actual_model
        self.backend = backend
        self.error = error
        self.last_call_result = None
        self.calls: list[dict[str, object]] = []
        self._attempts: list[dict[str, object]] = []

    def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        self._attempts = [
            {
                "attempt_group_id": "attempt-group-1",
                "model": self.actual_model,
                "http_status": 200 if self.error is None else 503,
                "attempt_no": 1,
                "duration_ms": 37,
                "error_class": "" if self.error is None else self.error.__class__.__name__,
                "error_message": "" if self.error is None else str(self.error),
                "final_failure": self.error is not None,
                "_raw_request_payload": {
                    "messages": messages,
                    "authorization": "Bearer must-not-leak",
                    "max_tokens": kwargs.get("max_tokens"),
                },
                "_raw_response_text": self.content,
            }
        ]
        if self.error is not None:
            raise self.error
        trace = {
            "backend": self.backend,
            "model": self.actual_model,
            "raw_events": [{"type": "turn.completed", "content": self.content}],
        }
        self.last_call_result = SimpleNamespace(
            content=self.content,
            backend=self.backend,
            fallback_used=False,
            trace=trace,
        )
        return self.content

    def drain_llm_attempt_events(self):
        attempts = list(self._attempts)
        self._attempts.clear()
        return attempts


@pytest.fixture()
def review_session():
    engine = get_engine(postgres_test_url("reckless-review"))
    init_db(engine)
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        project = Project(
            id="project-reckless-review",
            title="鲁莽审核测试",
            premise="测试",
            genre="悬疑",
        )
        session.add(project)
        session.commit()
        yield session, project
    engine.dispose()


def _request(project_id: str) -> RecklessReviewRequest:
    return RecklessReviewRequest(
        project_id=project_id,
        task_id="task-reckless-1",
        causal_root_id="root-reckless-1",
        gate_kind="chapter_review",
        scope="chapter",
        chapter_number=4,
        related_object_type="chapter_review",
        related_object_id="review-4",
        input_snapshot={
            "chapter_number": 4,
            "draft": "这是必须完整保留的章节正文。",
            "issues": [{"code": "continuity_warn", "severity": "warning"}],
            "api_key": "must-not-leak",
            "story_secret": "故事秘密必须保留",
        },
    )


def _approval_json() -> str:
    return json.dumps(
        {
            "decision": "approve",
            "reason": "警告不影响 canon 连续性，可以放行。",
            "risk_level": "medium",
            "findings": ["仅有非阻断警告"],
            "evidence": ["continuity_warn severity=warning"],
        },
        ensure_ascii=False,
    )


def test_spark_approval_persists_complete_trace(review_session) -> None:
    session, project = review_session
    llm = FakeReviewLLM(_approval_json())
    agent = RecklessReviewAgent(llm_client=llm)

    outcome = agent.review_and_record(
        updater=StateUpdater(session),
        request=_request(project.id),
    )
    session.commit()

    assert outcome.completed is True
    assert outcome.approved is True
    assert outcome.actual_model == RECKLESS_REVIEW_MODEL
    trace = session.get(PromptTrace, outcome.trace_id)
    assert trace is not None
    assert trace.trace_scope == "reckless_review"
    assert "这是必须完整保留的章节正文" in trace.input_snapshot_json
    assert "故事秘密必须保留" in trace.input_snapshot_json
    assert "must-not-leak" not in trace.input_snapshot_json
    assert "must-not-leak" not in trace.attempts_json
    assert "continuity_warn" in trace.prompt_layers_json
    assert "_raw_request_payload" in trace.attempts_json
    assert "_raw_response_text" in trace.attempts_json
    assert "警告不影响 canon 连续性" in trace.attempts_json
    assert "turn.completed" in trace.attempts_json
    assert "警告不影响 canon 连续性" in trace.output_summary_json
    assert trace.backend == "ordinary"
    assert trace.permission_profile == "prompt_only_readonly"
    event_types = {
        row.event_type
        for row in session.query(DecisionEvent)
        .filter(DecisionEvent.project_id == project.id)
    }
    assert event_types == {
        "reckless_review_requested",
        "prompt_trace_recorded",
        "reckless_review_decided",
    }


def test_non_spark_success_is_logged_but_cannot_approve(review_session) -> None:
    session, project = review_session
    agent = RecklessReviewAgent(
        llm_client=FakeReviewLLM(_approval_json(), actual_model="kimi-k2.5")
    )

    outcome = agent.review_and_record(
        updater=StateUpdater(session),
        request=_request(project.id),
    )
    session.commit()

    assert outcome.completed is False
    assert outcome.approved is False
    assert outcome.failure_reason == "model_mismatch"
    assert outcome.actual_model == "kimi-k2.5"
    trace = session.get(PromptTrace, outcome.trace_id)
    assert trace is not None
    assert "kimi-k2.5" in trace.model_profile_json
    assert "model_mismatch" in trace.output_summary_json


@pytest.mark.parametrize(
    ("llm", "failure_reason"),
    [
        (FakeReviewLLM("not-json"), "parse_or_schema"),
        (
            FakeReviewLLM(
                "",
                error=RuntimeError("Spark unavailable"),
            ),
            "llm_call_failed",
        ),
    ],
)
def test_invalid_output_and_call_failure_are_fully_logged(
    review_session,
    llm: FakeReviewLLM,
    failure_reason: str,
) -> None:
    session, project = review_session
    agent = RecklessReviewAgent(llm_client=llm)

    outcome = agent.review_and_record(
        updater=StateUpdater(session),
        request=_request(project.id),
    )
    session.commit()

    assert outcome.completed is False
    assert outcome.approved is False
    assert outcome.failure_reason == failure_reason
    trace = session.get(PromptTrace, outcome.trace_id)
    assert trace is not None
    assert failure_reason in trace.output_summary_json
    failed_events = (
        session.query(DecisionEvent)
        .filter(
            DecisionEvent.project_id == project.id,
            DecisionEvent.event_type == "reckless_review_failed",
        )
        .all()
    )
    assert len(failed_events) == 1


def test_spark_reject_is_a_completed_machine_decision(review_session) -> None:
    session, project = review_session
    llm = FakeReviewLLM(
        json.dumps(
            {
                "decision": "reject",
                "reason": "存在无法接受的 canon 冲突。",
                "risk_level": "high",
                "findings": ["事实冲突"],
                "evidence": ["人物已死亡却再次出现"],
            },
            ensure_ascii=False,
        )
    )

    outcome = RecklessReviewAgent(llm_client=llm).review_and_record(
        updater=StateUpdater(session),
        request=_request(project.id),
    )

    assert outcome.completed is True
    assert outcome.approved is False
    assert outcome.decision == "reject"
    assert outcome.failure_reason == ""
