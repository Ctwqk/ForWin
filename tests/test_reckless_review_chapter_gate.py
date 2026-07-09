from __future__ import annotations

import json
from types import SimpleNamespace

from sqlalchemy import select

from forwin.config import Config
from forwin.models.genesis import PromptTrace
from forwin.models.governance import DecisionEvent
from forwin.models.project import ChapterPlan
from forwin.orchestrator.loop import WritingOrchestrator
from forwin.protocol.review import ReviewVerdict
from forwin.protocol.writer import WriterOutput
from forwin.reckless_review import RECKLESS_REVIEW_MODEL


class FakeChapterGateLLM:
    def __init__(self, decision: str) -> None:
        self.decision = decision
        self.last_call_result = None
        self._attempts: list[dict[str, object]] = []
        self.calls: list[dict[str, object]] = []

    def chat(self, messages, **kwargs):
        content = json.dumps(
            {
                "decision": self.decision,
                "reason": (
                    "当前 draft 可接受。"
                    if self.decision == "approve"
                    else "当前 draft 不应进入 canon。"
                ),
                "risk_level": "medium" if self.decision == "approve" else "high",
                "findings": ["checkpoint mode delegated review"],
                "evidence": ["review verdict snapshot"],
            },
            ensure_ascii=False,
        )
        self.calls.append({"messages": messages, **kwargs})
        self._attempts = [
            {
                "attempt_group_id": "chapter-gate-attempt",
                "model": RECKLESS_REVIEW_MODEL,
                "http_status": 200,
                "attempt_no": 1,
                "duration_ms": 11,
                "_raw_request_payload": {"messages": messages},
                "_raw_response_text": content,
            }
        ]
        self.last_call_result = SimpleNamespace(
            backend="ordinary",
            fallback_used=False,
            trace={"backend": "ordinary", "model": RECKLESS_REVIEW_MODEL},
        )
        return content

    def drain_llm_attempt_events(self):
        attempts = list(self._attempts)
        self._attempts.clear()
        return attempts


def _arc_plan(_premise: str, _genre: str, num_chapters: int) -> dict:
    return {
        "arc_synopsis": "鲁莽章节 gate",
        "setting_summary": "无",
        "chapters": [
            {
                "chapter_number": chapter_number,
                "title": f"第{chapter_number}章",
                "one_line": "开场",
                "goals": ["推进主线"],
            }
            for chapter_number in range(1, num_chapters + 1)
        ],
        "characters": [],
        "locations": [],
        "factions": [],
        "relations": [],
        "plot_threads": [],
        "initial_time": {"label": "开始", "description": "开始"},
    }


def _write_chapter(context) -> WriterOutput:
    body = "正文" * 800
    return WriterOutput(
        chapter_number=context.chapter_number,
        title="第一章",
        body=body,
        char_count=len(body),
        end_of_chapter_summary="ok",
        state_changes=[],
        new_events=[],
        thread_beats=[],
        time_advance=None,
    )


def _run_reckless_gate(
    decision: str,
    *,
    operation_mode: str = "checkpoint",
    review_verdict: str = "pass",
    review_interval_chapters: int = 0,
    review_fail_max_rewrites: int = 3,
    num_chapters: int = 1,
) -> dict[str, object]:
    database_url = postgres_test_url(
        f"reckless-chapter-{operation_mode}-{review_verdict}-{decision}"
    )
    orchestrator = WritingOrchestrator(
        Config(
            database_url=database_url,
            minimax_api_key="",
            minimax_model="fake-model",
            chapter_review_form_mode="off",
            operation_mode=operation_mode,
            review_delegation_mode="reckless",
            review_interval_chapters=review_interval_chapters,
            review_fail_max_rewrites=review_fail_max_rewrites,
            auto_band_checkpoint=False,
            manual_checkpoints_enabled=False,
            future_constraints_enabled=False,
            generation_audit_interval_chapters=0,
            generation_audit_pause_enabled=False,
            phase4_use_llm=False,
        )
    )
    original_llm = orchestrator.llm_client
    fake_llm = FakeChapterGateLLM(decision)
    orchestrator.llm_client = fake_llm
    orchestrator.arc_director.plan_arc = _arc_plan
    orchestrator.writer.write_chapter = _write_chapter
    if review_verdict != "pass":
        orchestrator.review_hub.review = lambda **_kwargs: ReviewVerdict(
            verdict=review_verdict,
            issues=[],
            recommended_action="continue" if review_verdict == "warn" else "manual_review",
        )
    try:
        result = orchestrator.run("p", "g", num_chapters)
        with orchestrator._SessionFactory() as session:
            plans = list(
                session.execute(
                    select(ChapterPlan).order_by(ChapterPlan.chapter_number)
                ).scalars()
            )
            traces = list(
                session.execute(
                    select(PromptTrace).where(PromptTrace.trace_scope == "reckless_review")
                ).scalars()
            )
            events = list(
                session.execute(
                    select(DecisionEvent).where(
                        DecisionEvent.event_type.in_(
                            [
                                "reckless_review_requested",
                                "reckless_review_decided",
                                "reckless_gate_overridden",
                            ]
                        )
                    )
                ).scalars()
            )
            snapshot = {
                "result": result,
                "plan_statuses": [plan.status for plan in plans],
                "acceptance_modes": [plan.acceptance_mode for plan in plans],
                "trace_count": len(traces),
                "event_types": {event.event_type for event in events},
                "llm_calls": len(fake_llm.calls),
            }
    finally:
        original_llm.close()
        orchestrator.engine.dispose()
    return snapshot


def test_reckless_checkpoint_approval_continues_to_canon() -> None:
    snapshot = _run_reckless_gate("approve")

    result = snapshot["result"]
    assert result.status == "completed"
    assert result.completed_chapters == [1]
    assert snapshot["plan_statuses"] == ["accepted"]
    assert snapshot["acceptance_modes"] == ["reckless_approved"]
    assert snapshot["trace_count"] == 1
    assert snapshot["llm_calls"] == 1
    assert snapshot["event_types"] == {
        "reckless_review_requested",
        "reckless_review_decided",
        "reckless_gate_overridden",
    }


def test_reckless_checkpoint_rejection_preserves_review_pause() -> None:
    snapshot = _run_reckless_gate("reject")

    result = snapshot["result"]
    assert result.status == "needs_review"
    assert result.paused_chapters == [1]
    assert snapshot["plan_statuses"] == ["needs_review"]
    assert snapshot["acceptance_modes"] == [""]
    assert snapshot["trace_count"] == 1
    assert snapshot["llm_calls"] == 1
    assert snapshot["event_types"] == {
        "reckless_review_requested",
        "reckless_review_decided",
    }


def test_reckless_copilot_warn_approval_continues_to_canon() -> None:
    snapshot = _run_reckless_gate(
        "approve",
        operation_mode="copilot",
        review_verdict="warn",
    )

    assert snapshot["result"].status == "completed"
    assert snapshot["plan_statuses"] == ["accepted"]
    assert snapshot["acceptance_modes"] == ["reckless_approved"]
    assert snapshot["trace_count"] == 1


def test_reckless_review_interval_approval_continues_batch() -> None:
    snapshot = _run_reckless_gate(
        "approve",
        operation_mode="blackbox",
        review_interval_chapters=1,
        num_chapters=2,
    )

    assert snapshot["result"].status == "completed"
    assert snapshot["result"].completed_chapters == [1, 2]
    assert snapshot["plan_statuses"] == ["accepted", "accepted"]
    assert snapshot["acceptance_modes"] == ["reckless_approved", "normal"]
    assert snapshot["trace_count"] == 1


def test_reckless_mode_does_not_duplicate_existing_force_accept() -> None:
    snapshot = _run_reckless_gate(
        "approve",
        operation_mode="blackbox",
        review_verdict="fail",
        review_fail_max_rewrites=0,
    )

    assert snapshot["result"].status == "completed"
    assert snapshot["plan_statuses"] == ["accepted"]
    assert snapshot["acceptance_modes"] == ["force_accept_after_repair"]
    assert snapshot["trace_count"] == 0
    assert snapshot["llm_calls"] == 0
