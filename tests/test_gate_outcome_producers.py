from __future__ import annotations

from types import SimpleNamespace

from forwin.application.project_control.operations import (
    _checkpoint_action_gate_outcome,
)
from forwin.audit.gate_outcome import parse_gate_outcome
from forwin.generation.pipeline_core.audit_control import (
    AuditControlStage,
    _band_checkpoint_gate_outcome,
)
from forwin.generation.pipeline_core.chapter_execution_support import (
    hard_floor_gate_outcome,
)
from forwin.generation.pipeline_core.quality_gates import _canon_quality_gate_outcome
from forwin.canon_quality.signals import CanonAdmissionGateResult
from forwin.checker.hard_floor import HardFloorResult
from forwin.planning.checkpoints import BandCheckpointIssueInfo
from forwin.planning.future_plan_audit import FuturePlanAuditIssue, FuturePlanAuditRun


class RecordingAuditStage(AuditControlStage):
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def _record_decision_event(self, **payload):
        self.events.append(payload)
        return SimpleNamespace(id=f"event-{len(self.events)}")


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


def test_generation_audit_interval_event_distinguishes_pause_from_block() -> None:
    stage = RecordingAuditStage()
    stage._generation_audit_checkpoint_payload = lambda **_kwargs: {
        "checkpoint_chapter": 10,
        "checkpoint_interval": 5,
        "pause": {"enabled": True, "will_pause": True},
    }
    policy = SimpleNamespace(
        pause=SimpleNamespace(
            generation_audit_interval=5,
            generation_audit_pauses=True,
        )
    )

    will_pause = stage._record_generation_audit_checkpoint_if_due(
        session=SimpleNamespace(),
        updater=SimpleNamespace(),
        project_id="project-1",
        chapter_number=10,
        requested_chapters=20,
        last_requested_chapter=20,
        completed_chapters=list(range(1, 10)),
        failed_chapters=[],
        paused_chapters=[],
        future_plan_audit_result=None,
        policy=policy,
    )

    outcome = parse_gate_outcome(stage.events[0]["payload"])
    assert will_pause is True
    assert outcome is not None
    assert outcome.gate_id == "generation_audit"
    assert outcome.scope == "chapter"
    assert outcome.fired is True
    assert outcome.decision == "pause"
    assert outcome.blocked is False


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
