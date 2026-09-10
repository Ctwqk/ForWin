from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from forwin.api_schema.policy import RuntimePolicyUpdateRequest
from forwin.application.project_control.operations import (
    _checkpoint_action_gate_outcome,
)
from forwin.audit.gate_outcome import parse_gate_outcome
from forwin.canon_quality.signals import CanonAdmissionGateResult
from forwin.checker.hard_floor import HardFloorResult
from forwin.generation.pipeline_core.audit_control import (
    AuditControlStage,
    _band_checkpoint_gate_outcome,
)
from forwin.generation.pipeline_core.chapter_execution_support import (
    hard_floor_gate_outcome,
)
from forwin.canon.quality_preparation import _canon_quality_gate_outcome
from forwin.models.audit import DecisionEvent
from forwin.models.project import ChapterPlan
from forwin.observability.pipeline_trace import (
    PipelineAuditContext,
    PipelineTraceRecorder,
)
from forwin.planning.checkpoints import BandCheckpointIssueInfo
from forwin.planning.future_plan_audit import FuturePlanAuditIssue, FuturePlanAuditRun
from forwin.runtime.policy import RuntimePolicy
from forwin.state.updater import StateUpdater


class RecordingAuditStage(AuditControlStage):
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def _record_decision_event(self, **payload):
        self.events.append(payload)
        return SimpleNamespace(id=f"event-{len(self.events)}")


class FailingGenerationAuditStage(AuditControlStage):
    def _record_decision_event(self, **payload):
        super()._record_decision_event(**payload)
        raise RuntimeError("report sink unavailable")


@pytest.fixture
def generation_audit_session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    ChapterPlan.__table__.create(engine)
    DecisionEvent.__table__.create(engine)
    Session = sessionmaker(engine, expire_on_commit=False)
    with Session() as session:
        yield session


def _add_accepted_plans(session, chapter_numbers: list[int]) -> None:
    session.add_all(
        [
            ChapterPlan(
                id=f"plan-{chapter_number}",
                project_id="project-1",
                arc_plan_id="arc-1",
                chapter_number=chapter_number,
                status="accepted",
            )
            for chapter_number in chapter_numbers
        ]
    )


def test_future_plan_audit_event_carries_blocking_gate_outcome() -> None:
    stage = RecordingAuditStage()
    result = FuturePlanAuditRun(
        id="future-run-1",
        project_id="project-1",
        current_chapter=9,
        trigger_stage="post_acceptance",
        inspected_chapters=[10, 11],
        status="fail",
        issues=[
            FuturePlanAuditIssue(
                issue_type="countdown_inconsistency",
                target_chapter=10,
                description="countdown moved backwards",
                evidence_refs=["chapter:9", "plan:10"],
            )
        ],
        blocking_reasons=["countdown moved backwards"],
    )

    stage._record_future_plan_audit_events(
        updater=SimpleNamespace(),
        project_id="project-1",
        chapter_number=9,
        result=result,
    )

    outcome = parse_gate_outcome(stage.events[0]["payload"])
    assert outcome is not None
    assert outcome.gate_id == "future_plan_audit"
    assert outcome.candidate_id == "future-run-1"
    assert outcome.fired is True
    assert outcome.decision == "block"
    assert outcome.blocked is True
    assert outcome.issue_keys == ["countdown_inconsistency"]
    assert outcome.evidence_refs == ["chapter:9", "plan:10"]


def test_generation_audit_reports_each_six_accepted_rows_with_chapter_gaps(
    generation_audit_session,
) -> None:
    _add_accepted_plans(generation_audit_session, [1, 2, 4, 7, 8, 11])
    stage = RecordingAuditStage()

    stage._record_generation_audit_report_if_due(
        session=generation_audit_session,
        updater=SimpleNamespace(),
        project_id="project-1",
        chapter_number=11,
        future_plan_audit_result=None,
    )

    assert len(stage.events) == 1
    event = stage.events[0]
    assert event["event_family"] == "runtime_observation"
    assert event["related_object_type"] == "generation_audit_checkpoint"
    assert event["related_object_id"] == "project-1:accepted:6"
    assert event["payload"]["accepted_count"] == 6
    assert event["payload"]["cadence"] == 6
    assert event["payload"]["accepted_chapter_window"] == [1, 2, 4, 7, 8, 11]
    outcome = parse_gate_outcome(event["payload"])
    assert outcome is not None
    assert outcome.gate_id == "generation_audit"
    assert outcome.gate_version == "report-only-v1"
    assert outcome.evaluated is False
    assert outcome.fired is False
    assert outcome.decision == "pass"
    assert outcome.blocked is False
    assert outcome.candidate_id == "generation-audit:project-1:accepted:6"


@pytest.mark.parametrize("profile", ["standard", "pulp"])
def test_generation_audit_uses_the_same_fixed_cadence_for_every_profile(
    generation_audit_session,
    profile: str,
) -> None:
    _add_accepted_plans(generation_audit_session, [1, 3, 5, 7, 9, 11])
    stage = RecordingAuditStage()

    stage._record_generation_audit_report_if_due(
        session=generation_audit_session,
        updater=SimpleNamespace(),
        project_id="project-1",
        chapter_number=11,
        future_plan_audit_result=None,
    )

    assert RuntimePolicy.for_profile(profile).schema_version == 2
    assert len(stage.events) == 1
    assert stage.events[0]["payload"]["cadence"] == 6


def test_generation_audit_deduplicates_reports_at_the_same_accepted_count(
    generation_audit_session,
) -> None:
    _add_accepted_plans(generation_audit_session, [1, 2, 3, 4, 5, 6])
    stage = AuditControlStage()
    stage.trace_recorder = PipelineTraceRecorder(
        audit=PipelineAuditContext(), artifact_store=None, observability=None,
    )
    updater = StateUpdater(generation_audit_session)

    stage._record_generation_audit_report_if_due(
        session=generation_audit_session,
        updater=updater,
        project_id="project-1",
        chapter_number=6,
        future_plan_audit_result=None,
    )
    stage._record_generation_audit_report_if_due(
        session=generation_audit_session,
        updater=updater,
        project_id="project-1",
        chapter_number=6,
        future_plan_audit_result=None,
    )

    reports = generation_audit_session.query(DecisionEvent).all()
    assert len(reports) == 1
    assert reports[0].related_object_id == "project-1:accepted:6"


def test_generation_audit_failure_rolls_back_only_the_report(
    generation_audit_session,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _add_accepted_plans(generation_audit_session, [1, 2, 3, 4, 5, 6])
    stage = FailingGenerationAuditStage()
    stage._audit_task_id = ""
    stage._audit_root_event_id = ""

    stage._record_generation_audit_report_if_due(
        session=generation_audit_session,
        updater=StateUpdater(generation_audit_session),
        project_id="project-1",
        chapter_number=6,
        future_plan_audit_result=None,
    )

    assert generation_audit_session.query(ChapterPlan).count() == 6
    assert generation_audit_session.query(DecisionEvent).count() == 0
    assert "generation audit report failed" in caplog.text


def test_generation_audit_count_failure_does_not_block_chapter_progress(
    generation_audit_session,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _add_accepted_plans(generation_audit_session, [1, 2, 3, 4, 5, 6])
    stage = RecordingAuditStage()
    original_query = generation_audit_session.query

    def fail_accepted_count(*entities, **kwargs):
        if entities == (ChapterPlan,):
            raise RuntimeError("accepted count unavailable")
        return original_query(*entities, **kwargs)

    monkeypatch.setattr(generation_audit_session, "query", fail_accepted_count)
    stage._record_generation_audit_report_if_due(
        session=generation_audit_session,
        updater=SimpleNamespace(),
        project_id="project-1",
        chapter_number=6,
        future_plan_audit_result=None,
    )
    monkeypatch.setattr(generation_audit_session, "query", original_query)

    assert generation_audit_session.query(ChapterPlan).count() == 6
    assert generation_audit_session.query(DecisionEvent).count() == 0
    assert "generation audit report failed" in caplog.text


def test_removed_generation_audit_policy_request_fields_are_rejected() -> None:
    request = {
        "expected_version": 1,
        "quality_profile": "standard",
        "min_chapter_chars": 2500,
        "target_chapter_chars": 2800,
        "max_chapter_chars": 3200,
        "review_interval_chapters": 0,
        "manual_checkpoints": True,
        "band_checkpoint_action": "pause_on_warn",
        "gate_delegate": "human",
        "reason": "update policy",
        "generation_audit_" + "interval": 6,
        "generation_audit_" + "pauses": False,
    }

    with pytest.raises(ValidationError, match="extra_forbidden"):
        RuntimePolicyUpdateRequest.model_validate(request)


def test_band_checkpoint_gate_outcome_preserves_issue_identity() -> None:
    outcome = _band_checkpoint_gate_outcome(
        project_id="project-1",
        checkpoint_id="checkpoint-1",
        band_id="band-3",
        chapter_number=12,
        status="fail",
        issues=[
            BandCheckpointIssueInfo(
                code="next_band_compatibility",
                severity="error",
                issue_group="fact_conflict",
                detail="plan:13",
            )
        ],
    )

    assert outcome.gate_id == "band_checkpoint"
    assert outcome.candidate_id == "checkpoint-1"
    assert outcome.fired is True
    assert outcome.decision == "block"
    assert outcome.blocked is True
    assert outcome.issue_keys == ["next_band_compatibility"]
    assert outcome.issue_groups == ["fact_conflict"]
    assert outcome.evidence_refs == ["plan:13"]


def test_hard_floor_gate_outcome_uses_reviewed_candidate_identity() -> None:
    warning = hard_floor_gate_outcome(
        HardFloorResult(
            passed=True,
            warning_reasons=["ending_hook"],
            checks={"ending_hook": False},
        ),
        candidate_id="candidate-4-v2",
        chapter_number=4,
        policy_version=2,
    )
    blocked = hard_floor_gate_outcome(
        HardFloorResult(
            passed=False,
            fail_reasons=["chapter_length"],
            checks={"chapter_length": False},
        ),
        candidate_id="candidate-4-v3",
        chapter_number=4,
        policy_version=2,
    )

    assert warning.candidate_id == "candidate-4-v2"
    assert warning.fired is True
    assert warning.decision == "warn"
    assert warning.blocked is False
    assert blocked.candidate_id == "candidate-4-v3"
    assert blocked.decision == "block"
    assert blocked.blocked is True
    assert blocked.issue_keys == ["chapter_length"]


def test_canon_quality_gate_outcome_tracks_candidate_and_signal_groups() -> None:
    outcome = _canon_quality_gate_outcome(
        CanonAdmissionGateResult(
            project_id="project-1",
            chapter_number=7,
            draft_id="draft-7",
            review_id="review-7",
            commit_allowed=False,
            verdict="fail",
            admission_mode="blocked",
            blocking_reasons=["countdown_non_monotonic"],
            deterministic_issue_refs=["signal:countdown:7"],
        ),
        candidate_id="candidate-7-v2",
        policy_version=4,
        signal_types=["countdown_non_monotonic"],
        evidence_refs=["signal:countdown:7"],
    )

    assert outcome.gate_id == "canon_quality"
    assert outcome.candidate_id == "candidate-7-v2"
    assert outcome.policy_version == 4
    assert outcome.fired is True
    assert outcome.decision == "block"
    assert outcome.blocked is True
    assert outcome.issue_keys == ["countdown_non_monotonic"]
    assert outcome.issue_groups == ["fact_conflict"]
    assert outcome.evidence_refs == ["signal:countdown:7"]


def test_manual_checkpoint_opportunity_and_override_share_identity() -> None:
    checkpoint = SimpleNamespace(
        id="manual-checkpoint-1",
        trigger_source="manual_boundary",
        boundary_kind="chapter_start",
        boundary_chapter=8,
        band_id="band-2",
        issues_json="[]",
    )
    created = _checkpoint_action_gate_outcome(
        checkpoint,
        policy_version=3,
        evaluated=False,
        fired=False,
        decision="pause",
    )
    overridden = _checkpoint_action_gate_outcome(
        checkpoint,
        policy_version=3,
        evaluated=True,
        fired=True,
        decision="approve",
        overridden_by="manual",
    )

    assert created.gate_id == "manual_checkpoint"
    assert created.scope == "chapter"
    assert created.candidate_id == "manual-checkpoint-1"
    assert created.evaluated is False
    assert overridden.candidate_id == created.candidate_id
    assert overridden.decision == "approve"
    assert overridden.overridden_by == "manual"
