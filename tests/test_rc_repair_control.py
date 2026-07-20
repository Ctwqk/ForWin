from __future__ import annotations

from types import SimpleNamespace

import pytest

from forwin.audit.events import DecisionEventType
from forwin.generation.pipeline_core.audit_control import AuditControlStage
from forwin.models.audit import DecisionEvent
from forwin.protocol.context import ReviewContextPack
from forwin.protocol.review import (
    ContinuityIssue,
    RepairInstruction,
    RepairVerification,
    ReviewVerdict,
)
from forwin.protocol.writer import WriterOutput
from forwin.review.decision.types import Decision, DecisionInput, PlanLayerHealth
from forwin.review import llm_webnovel
from forwin.review.llm_webnovel import LLMWebNovelReviewer
from forwin.review.repair import service as repair_service
from forwin.runtime.policy import RuntimePolicy


def _review_context() -> ReviewContextPack:
    return ReviewContextPack(
        project_id="project-1",
        project_title="RC convergence",
        chapter_number=5,
        chapter_plan_title="Chapter 5",
        chapter_plan_one_line="Keep the repair controller deterministic.",
    )


def _writer_output() -> WriterOutput:
    return WriterOutput(
        project_id="project-1",
        chapter_number=5,
        title="Chapter 5",
        body="A concrete chapter body with anchored evidence.",
        end_of_chapter_summary="The chapter ends on a verified fact.",
    )


def _parse_review_payload(
    reviewer: LLMWebNovelReviewer,
    payload: dict[str, object],
    *,
    fallback_on_invalid: bool = False,
) -> ReviewVerdict:
    return reviewer._verdict_from_payload(
        payload=payload,
        context=_review_context(),
        writer_output=_writer_output(),
        fallback_on_invalid=fallback_on_invalid,
        allowed_evidence_ids={"draft:body_head"},
    )


def test_llm_fail_without_issues_is_rejected() -> None:
    reviewer = LLMWebNovelReviewer(enabled=False)

    with pytest.raises(ValueError, match="anchored error issue"):
        _parse_review_payload(
            reviewer,
            {"verdict": "fail", "issues": [], "review_summary": "failed"},
        )


def test_llm_fail_without_issues_falls_back_to_heuristic_when_configured() -> None:
    class _HeuristicReviewer:
        def _review_with_heuristics(self, _context, _writer_output) -> ReviewVerdict:
            return ReviewVerdict(
                verdict="warn",
                issues=[],
                review_summary="heuristic fallback",
            )

    reviewer = LLMWebNovelReviewer(
        heuristic_reviewer=_HeuristicReviewer(),
        enabled=False,
    )

    verdict = _parse_review_payload(
        reviewer,
        {"verdict": "fail", "issues": [], "review_summary": "failed"},
        fallback_on_invalid=True,
    )

    assert verdict.verdict == "warn"
    assert verdict.review_summary == "heuristic fallback"


def test_llm_fail_with_warning_only_issue_is_rejected() -> None:
    reviewer = LLMWebNovelReviewer(enabled=False)

    with pytest.raises(ValueError, match="anchored error issue"):
        _parse_review_payload(
            reviewer,
            {
                "verdict": "fail",
                "issues": [
                    {
                        "rule_name": "warning_only",
                        "severity": "warning",
                        "description": "This cannot justify a hard failure.",
                        "evidence_refs": ["draft:body_head"],
                    }
                ],
            },
        )


def test_llm_fail_with_anchored_error_issue_is_accepted() -> None:
    reviewer = LLMWebNovelReviewer(enabled=False)

    verdict = _parse_review_payload(
        reviewer,
        {
            "verdict": "fail",
            "issues": [
                {
                    "rule_name": "anchored_failure",
                    "severity": "error",
                    "description": "The cited text contains a real failure.",
                    "evidence_refs": ["draft:body_head"],
                }
            ],
        },
    )

    assert verdict.verdict == "fail"
    assert [issue.rule_name for issue in verdict.issues] == ["anchored_failure"]
    assert verdict.issues[0].evidence_refs == ["draft:body_head"]


def test_public_llm_review_repairs_empty_fail_into_warn(monkeypatch) -> None:
    stage_keys: list[str] = []

    def fake_call_chat_compat(*_args, **kwargs) -> str:
        stage_key = str(kwargs["stage_key"])
        stage_keys.append(stage_key)
        if stage_key == "chapter_review":
            return '{"verdict":"fail","issues":[],"review_summary":"invalid"}'
        assert stage_key == "chapter_review_json_repair"
        return '{"verdict":"warn","issues":[],"review_summary":"repaired"}'

    monkeypatch.setattr(llm_webnovel, "call_chat_compat", fake_call_chat_compat)

    verdict = LLMWebNovelReviewer(llm_client=object()).review(
        _review_context(),
        _writer_output(),
    )

    assert verdict is not None
    assert verdict.verdict == "warn"
    assert verdict.review_summary == "repaired"
    assert verdict.prompt_trace["output_summary"]["repair_attempts"] == 1
    assert stage_keys == ["chapter_review", "chapter_review_json_repair"]


def _rule_decision_input(review: ReviewVerdict | None = None) -> DecisionInput:
    return DecisionInput(
        project_id="project-1",
        chapter_number=5,
        review=review or _hard_failure_review(),
        signals=[],
        open_obligations=[],
        attempts_completed=0,
        prior_scope_history=[],
        budget=None,
        target_total_chapters=0,
        plan_layer_health=PlanLayerHealth(),
    )


def _manual_review_decision() -> Decision:
    return Decision(
        outcome="manual_review",
        reason="manual review required",
        rule_id="test_manual_review",
        missing_evidence=[],
        routed_from="test",
        sub_action={"scope": "operator"},
    )


class _AuditControlHarness(AuditControlStage):
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.recorded_parent_event_ids: list[str] = []

    def _record_decision_event(self, **kwargs) -> DecisionEvent:
        self.recorded_parent_event_ids.append(str(kwargs["parent_event_id"]))
        if self.fail:
            raise RuntimeError("audit sink unavailable")
        return DecisionEvent(
            id="rule-event-1",
            project_id=str(kwargs["project_id"]),
            chapter_number=int(kwargs["chapter_number"]),
            event_type=str(kwargs["event_type"]),
            parent_event_id=str(kwargs["parent_event_id"]),
        )


def test_rule_decision_audit_contract_returns_recorded_event() -> None:
    stage = _AuditControlHarness()

    event = stage._record_rule_decision_event(
        updater=object(),  # type: ignore[arg-type]
        decision=_manual_review_decision(),
        decision_input=_rule_decision_input(),
        parent_event_id="review-event-1",
    )

    assert isinstance(event, DecisionEvent)
    assert event.id == "rule-event-1"
    assert event.parent_event_id == "review-event-1"


def test_rule_decision_audit_contract_returns_none_on_recording_error() -> None:
    stage = _AuditControlHarness(fail=True)

    event = stage._record_rule_decision_event(
        updater=object(),  # type: ignore[arg-type]
        decision=_manual_review_decision(),
        decision_input=_rule_decision_input(),
        parent_event_id="review-event-1",
    )

    assert event is None


def _runtime_policy(*, max_rewrites: int) -> RuntimePolicy:
    payload = RuntimePolicy.for_profile("standard").model_dump(mode="python")
    payload["review"]["max_rewrites"] = max_rewrites
    return RuntimePolicy.model_validate(payload)


class _MemorySession:
    def __init__(self, timeline: list[str]) -> None:
        self.timeline = timeline

    def add(self, _row: object) -> None:
        self.timeline.append("session:add")

    def commit(self) -> None:
        self.timeline.append("session:commit")


class _MemoryRepo:
    def __init__(self, attempts: list[object]) -> None:
        self.attempts = attempts

    def list_chapter_rewrite_attempts(
        self,
        _project_id: str,
        _chapter_number: int,
    ) -> list[object]:
        return list(self.attempts)


class _RepairHarness:
    def __init__(self, *, max_rewrites: int) -> None:
        self.policy = _runtime_policy(max_rewrites=max_rewrites)
        self.rule_decisions: list[Decision] = []
        self.rule_events: list[DecisionEvent] = []
        self.events: list[DecisionEvent] = []
        self.timeline: list[str] = []

    def _pause_requested(self) -> bool:
        return False

    def _record_rule_decision_event(
        self,
        *,
        decision: Decision,
        parent_event_id: str,
        **_kwargs,
    ) -> DecisionEvent:
        self.rule_decisions.append(decision)
        self.timeline.append(f"rule:{decision.rule_id}:{decision.outcome}")
        event = DecisionEvent(
            id=f"rule-{len(self.rule_decisions)}",
            project_id="project-1",
            chapter_number=5,
            event_type=DecisionEventType.RULE_DECISION_EVALUATED,
            parent_event_id=parent_event_id,
        )
        self.rule_events.append(event)
        return event

    def _record_decision_event(
        self,
        *,
        event_type: str,
        parent_event_id: str,
        **_kwargs,
    ) -> DecisionEvent:
        self.timeline.append(f"event:{event_type}")
        event = DecisionEvent(
            id=f"event-{len(self.events) + 1}",
            project_id="project-1",
            chapter_number=5,
            event_type=event_type,
            parent_event_id=parent_event_id,
        )
        self.events.append(event)
        return event

    def _chapter_plan_snapshot(self, **_kwargs) -> dict[str, object]:
        return {}

    def _band_plan_snapshot(self, **_kwargs) -> dict[str, object]:
        return {}


def _repair_attempt(*, phase: str, scope: str = "chapter_plan") -> object:
    return SimpleNamespace(
        repair_phase=phase,
        repair_scope=scope,
        forced_accept_applied=False,
    )


def _hard_failure_review() -> ReviewVerdict:
    return ReviewVerdict(
        verdict="fail",
        issues=[
            ContinuityIssue(
                rule_name="hard_continuity_failure",
                severity="error",
                description="A hard continuity issue remains.",
                reviewer="test",
                issue_type="continuity",
                target_scope="chapter_plan",
                evidence_refs=["draft:body_head"],
            )
        ],
        repair_instruction=RepairInstruction(
            repair_scope="chapter_plan",
            failure_type="continuity",
            must_fix=["A hard continuity issue remains."],
            evidence_refs=["draft:body_head"],
        ),
    )


def _soft_failure_review() -> ReviewVerdict:
    return ReviewVerdict(
        verdict="fail",
        issues=[
            ContinuityIssue(
                rule_name="soft_stall_failure",
                severity="error",
                description="A soft pacing issue remains.",
                reviewer="test",
                issue_type="stall",
                target_scope="chapter_plan",
                evidence_refs=["draft:body_head"],
            )
        ],
        repair_verification=RepairVerification(
            fixed_all_must_fix=True,
            preserved_all_must_preserve=True,
        ),
    )


def _run_repair_loop(
    harness: _RepairHarness,
    *,
    attempts: list[object],
    review: ReviewVerdict,
    repair_phase: str = "review_repair",
) -> tuple[object, ReviewVerdict, bool]:
    output = object()
    return repair_service._run_repair_loop_for_phase(
        harness,  # type: ignore[arg-type]
        session=_MemorySession(harness.timeline),  # type: ignore[arg-type]
        repo=_MemoryRepo(attempts),  # type: ignore[arg-type]
        updater=object(),  # type: ignore[arg-type]
        checker=object(),  # type: ignore[arg-type]
        project_id="project-1",
        chapter_plan=SimpleNamespace(chapter_number=5, repair_attempt_count=0),
        current_context=object(),
        current_output=output,  # type: ignore[arg-type]
        current_draft=SimpleNamespace(id="draft-1"),
        current_review=review,
        current_review_row=SimpleNamespace(id="review-1", review_meta_json="{}"),
        current_review_trace_id="trace-1",
        current_review_event=SimpleNamespace(id="review-event-1"),
        repair_phase=repair_phase,
    )


def test_active_phase_budget_routes_directly_to_recorded_final_residual(
    monkeypatch,
) -> None:
    harness = _RepairHarness(max_rewrites=1)
    attempts = [_repair_attempt(phase="review_repair")]

    def unexpected_repair_decision(_decision_input):
        raise AssertionError("repair policy must not run after phase budget exhaustion")

    monkeypatch.setattr(
        repair_service,
        "decide_repair_v2",
        unexpected_repair_decision,
    )

    _output, review, forced_accept = _run_repair_loop(
        harness,
        attempts=attempts,
        review=_hard_failure_review(),
    )

    assert forced_accept is False
    assert review.final_residual_decision is not None
    assert review.final_residual_decision.decision == "manual_review_required"
    assert [decision.rule_id for decision in harness.rule_decisions] == [
        "final_residual_policy"
    ]
    assert not any(
        event.event_type == DecisionEventType.REPAIR_STARTED
        for event in harness.events
    )
    assert harness.rule_events[0].parent_event_id == "review-event-1"
    assert harness.timeline.index("rule:final_residual_policy:manual_review") < (
        harness.timeline.index("session:add")
    )


def test_zero_phase_budget_starts_no_rewrite(monkeypatch) -> None:
    harness = _RepairHarness(max_rewrites=0)

    def unexpected_repair_decision(_decision_input):
        raise AssertionError("repair policy must not run with a zero phase budget")

    monkeypatch.setattr(
        repair_service,
        "decide_repair_v2",
        unexpected_repair_decision,
    )

    _output, review, forced_accept = _run_repair_loop(
        harness,
        attempts=[],
        review=_hard_failure_review(),
    )

    assert forced_accept is False
    assert review.repair_exhausted is True
    assert [decision.rule_id for decision in harness.rule_decisions] == [
        "final_residual_policy"
    ]
    assert not any(
        event.event_type == DecisionEventType.REPAIR_STARTED
        for event in harness.events
    )


def test_attempts_from_another_phase_do_not_exhaust_active_phase(
    monkeypatch,
) -> None:
    class _RewriteStarted(Exception):
        pass

    harness = _RepairHarness(max_rewrites=1)
    attempts = [_repair_attempt(phase="canon_repair")]
    monkeypatch.setattr(
        repair_service,
        "decide_repair_v2",
        lambda _decision_input: Decision(
            outcome="chapter_patch",
            reason="chapter repair is executable",
            rule_id="test_chapter_patch",
            missing_evidence=[],
            routed_from="test",
            sub_action={"scope": "chapter_plan"},
        ),
    )

    def rewrite_started(*_args, **_kwargs):
        raise _RewriteStarted

    monkeypatch.setattr(repair_service, "_apply_repair_patch", rewrite_started)

    with pytest.raises(_RewriteStarted):
        _run_repair_loop(
            harness,
            attempts=attempts,
            review=_hard_failure_review(),
        )

    assert any(
        event.event_type == DecisionEventType.REPAIR_STARTED
        for event in harness.events
    )


@pytest.mark.parametrize(
    ("proposed_outcome", "proposed_scope"),
    [("arc_patch", "arc_plan"), ("book_patch", "book_plan")],
)
def test_unsupported_patch_is_recorded_as_manual_review_without_rewrite(
    monkeypatch,
    proposed_outcome: str,
    proposed_scope: str,
) -> None:
    harness = _RepairHarness(max_rewrites=3)
    monkeypatch.setattr(
        repair_service,
        "decide_repair_v2",
        lambda _decision_input: Decision(
            outcome=proposed_outcome,  # type: ignore[arg-type]
            reason="scope escalation",
            rule_id="test_scope_escalation",
            missing_evidence=[],
            routed_from="test",
            sub_action={"scope": proposed_scope},
        ),
    )

    _output, review, forced_accept = _run_repair_loop(
        harness,
        attempts=[],
        review=_hard_failure_review(),
    )

    assert forced_accept is False
    assert review.final_residual_decision is not None
    assert [decision.outcome for decision in harness.rule_decisions] == [
        "manual_review",
        "manual_review",
    ]
    normalized = harness.rule_decisions[0]
    assert normalized.rule_id == "repair_scope_not_executable"
    assert normalized.sub_action["proposed_outcome"] == proposed_outcome
    assert normalized.sub_action["proposed_scope"] == proposed_scope
    assert harness.rule_decisions[1].rule_id == "final_residual_policy"
    assert [event.parent_event_id for event in harness.rule_events] == [
        "review-event-1",
        "rule-1",
    ]
    assert not any(
        event.event_type == DecisionEventType.REPAIR_STARTED
        for event in harness.events
    )


def test_force_accept_final_residual_decision_is_recorded_before_application(
    monkeypatch,
) -> None:
    harness = _RepairHarness(max_rewrites=3)
    monkeypatch.setattr(
        repair_service,
        "decide_repair_v2",
        lambda _decision_input: Decision(
            outcome="manual_review",
            reason="no executable repair remains",
            rule_id="test_manual_review",
            missing_evidence=[],
            routed_from="test",
            sub_action={"scope": "operator"},
        ),
    )

    _output, review, forced_accept = _run_repair_loop(
        harness,
        attempts=[],
        review=_soft_failure_review(),
    )

    assert forced_accept is True
    assert review.verdict == "warn"
    assert review.forced_accept_applied is True
    assert [decision.rule_id for decision in harness.rule_decisions] == [
        "test_manual_review",
        "final_residual_policy",
    ]
    final_rule_index = harness.timeline.index("rule:final_residual_policy:accept")
    assert final_rule_index < harness.timeline.index("session:add")
    assert [event.parent_event_id for event in harness.rule_events] == [
        "review-event-1",
        "rule-1",
    ]
    forced_accept_event = next(
        event
        for event in harness.events
        if event.event_type == DecisionEventType.FORCED_ACCEPT_APPLIED
    )
    assert forced_accept_event.parent_event_id == "rule-2"
