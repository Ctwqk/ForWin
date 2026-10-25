"""Residual chapter-text boundary violations become chapter_extraction conflicts and pause the chapter."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import event, select

from forwin.api_schema import ChapterReviewApproveRequest
from forwin.application.projects.reviews import approve_chapter_review
from forwin.audit.events import DecisionEventType
from forwin.book_state.projection import BookStateProjection
from forwin.book_state.protections import DeltaBoundaryViolation
from forwin.book_state.repository import BookStateRepository
from forwin.book_state.reviewer import BookStateReviewIssue, BookStateReviewVerdict
from forwin.candidate_drafts import CandidateDraftRepository, candidate_plan_revision
from forwin.canon.admission import CanonAdmissionService
from forwin.canon.eligibility import candidate_ineligibility_reason
from forwin.canon.preparation import BookStatePreparationOutcome, CanonPreparationService
from forwin.canon.types import CanonPreparationOutcome
from forwin.generation.pipeline_core.acceptance import AcceptanceStage
from forwin.generation.pipeline_core.chapter_conflicts import ChapterConflictPending
from forwin.generation.pipeline_core.project_chapters import ChapterExecutionStage
from forwin.models.audit import DecisionEvent
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.book_state import GraphDeltaRow
from forwin.models.draft import ChapterDraft, ChapterReview
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from forwin.models.world_conflict import WorldPlanConflictRow
from forwin.planning.beat_sheet.types import ProtectionSpec
from forwin.protocol.book_state import FactNode, WorldNode
from forwin.protocol.review import ContinuityIssue, ReviewVerdict
from forwin.protocol.writer import WriterOutput
from forwin.review.repair import service as repair_service
from forwin.review.repair.service import RepairService
from forwin.runtime.policy import RuntimePolicy
from forwin.state.repo import StateRepository
from forwin.state.updater import StateUpdater
from forwin.world_engine.chapter_conflicts import (
    CHAPTER_CONFLICT_OPTIONS,
    ChapterConflictRecorder,
    chapter_conflict_draft,
    violations_from_gate,
    violations_from_verdict,
)
from forwin.world_engine.conflict_delegate import WorldConflictDelegation
from forwin.world_engine.conflict_repository import WorldConflictRepository
from forwin.world_engine.conflict_resolution import ConflictResolutionService
from forwin.world_engine.conflict_types import ConflictDecision
from forwin.writer.prompt_core.world_repair import protection_repair_fix
from forwin.writer.prompt_core.world_view import UNNAMED_ACTOR_LABEL
from tests.postgres import postgres_test_url


PROJECT_ID = "chapter-conflict-project"
CHAPTER = 3
HERO = "hero"
SHADOW = "shadow"
SECRET = "secret-1"
HERO_NAME = "Hero-Name"
SHADOW_NAME = "Shadow-Name"
PROPOSITION = "PROPOSITION-OF-THE-SECRET"
PROTECTION = "protection-hero"
PLAN_ID = f"chapter-conflict-plan-{CHAPTER}"
HUMAN = RuntimePolicy.for_profile("standard").with_user_settings(world_conflict_delegate="human")
CODEX = RuntimePolicy.for_profile("standard").with_user_settings(world_conflict_delegate="codex")


@pytest.fixture
def factory():
    engine = get_engine(postgres_test_url("chapter-extraction-conflicts"))
    init_db(engine)
    factory = get_session_factory(engine)
    with factory.begin() as session:
        session.add(Project(
            id=PROJECT_ID, title="Test", premise="Test", genre="test",
            creation_status="writing", target_total_chapters=4,
        ))
        session.flush()
        session.add(ArcPlanVersion(
            id="chapter-conflict-arc", project_id=PROJECT_ID, arc_synopsis="Test",
            status="active", chapter_start=1, chapter_end=4,
        ))
        session.flush()
        session.add_all([
            ChapterPlan(
                id=f"chapter-conflict-plan-{number}", project_id=PROJECT_ID, arc_plan_id="chapter-conflict-arc",
                chapter_number=number, title=f"Chapter {number}", status="accepted" if number < CHAPTER else "planned",
            )
            for number in range(1, 5)
        ])
        repo = BookStateRepository(session)
        repo.create_world_node(
            WorldNode(id=HERO, project_id=PROJECT_ID, node_type="character", name=HERO_NAME),
            version_chapter=0, version_state={"alive_status": "alive"},
        )
        repo.create_world_node(
            WorldNode(id=SHADOW, project_id=PROJECT_ID, node_type="character", name=SHADOW_NAME,
                      profile={"visibility_level": "hidden"}),
            version_chapter=0, version_state={"alive_status": "alive"},
        )
        repo.create_fact_node(FactNode(
            id=SECRET, project_id=PROJECT_ID, proposition=PROPOSITION, related_node_refs=[HERO, SHADOW],
            sensitivity_level="secret",
        ))
    yield factory
    engine.dispose()


def _violation(**overrides) -> DeltaBoundaryViolation:
    return DeltaBoundaryViolation(**{
        "protection_node_id": PROTECTION, "milestone_ref": "", "kind": "must_be_alive", "ref": HERO,
        "observer_ref": "", "fact_ref": "", "field": "", "value": None, "until_chapter": 6,
        "source": "beat_sheet", "impact_level": "major", "evidence": "the hero falls",
        **overrides,
    })


def _boundary_issue(violation: DeltaBoundaryViolation) -> ContinuityIssue:
    return ContinuityIssue(
        rule_name="protection_violated", severity="error", description="d", reviewer="world_consistency",
        issue_type="world_consistency", blocking=True,
        original_result={"code": "protection_violated", "boundary": asdict(violation)},
    )


def _failing(*violations: DeltaBoundaryViolation) -> ReviewVerdict:
    return ReviewVerdict(verdict="fail", issues=[_boundary_issue(violation) for violation in violations])


PASSING = ReviewVerdict(verdict="pass", issues=[])


class FakeDelegate:
    def __init__(self, option: str = "rewrite_chapter", *, escalate: bool = False) -> None:
        self.option = option
        self.escalate = escalate
        self.calls: list[str] = []

    def decide(self, *, conflict, context_snapshot, policy, audit_writer):
        self.calls.append(conflict.id)
        assert context_snapshot["conflict"]["id"] == conflict.id
        return WorldConflictDelegation(
            option=self.option, rationale="Codex rationale.", trace_id="trace-1", escalate=self.escalate,
        )


def _recorder(session, *, policy=HUMAN, delegate=None) -> ChapterConflictRecorder:
    return ChapterConflictRecorder(
        session, policy=policy, delegate=delegate or FakeDelegate(), audit_writer_factory=lambda: None,
    )


def _rows(session) -> list[WorldPlanConflictRow]:
    return list(session.scalars(
        select(WorldPlanConflictRow).where(WorldPlanConflictRow.project_id == PROJECT_ID)
        .order_by(WorldPlanConflictRow.created_at, WorldPlanConflictRow.id)
        .execution_options(populate_existing=True)
    ))


def _events(session, event_type: str) -> list[DecisionEvent]:
    return list(session.scalars(select(DecisionEvent).where(
        DecisionEvent.project_id == PROJECT_ID, DecisionEvent.event_type == event_type,
    ).order_by(DecisionEvent.created_at, DecisionEvent.id)))


# drafts and conversions


def test_chapter_conflict_draft_fields() -> None:
    protection = _violation()
    milestone = _violation(protection_node_id="", milestone_ref=SHADOW, kind="milestone", ref=SHADOW,
                           field="state.alive_status", value="dead", impact_level="critical")

    draft = chapter_conflict_draft(
        protection, project_id=PROJECT_ID, chapter_number=CHAPTER, story_time=900, candidate_id="candidate-1",
    )
    milestone_draft = chapter_conflict_draft(milestone, project_id=PROJECT_ID, chapter_number=CHAPTER, story_time=900)

    assert (draft.source, draft.violated, draft.impact_level) == ("chapter_extraction", "protection", "major")
    assert (draft.detected_after_chapter, draft.blocks_from_chapter, draft.story_time) == (CHAPTER - 1, CHAPTER, 900)
    assert draft.origin_key == f"chapter_extraction:{CHAPTER}:{PROTECTION}"
    assert draft.involved_refs == [HERO]
    assert (draft.intent_ref, draft.actor_ref) == ("", "")
    assert draft.proposed_change == {
        "kind": "chapter_boundary", "chapter": CHAPTER, "boundary": asdict(protection), "candidate_id": "candidate-1",
        "detected_by": "draft_review", "names": {},
    }
    assert (milestone_draft.violated, milestone_draft.impact_level) == ("milestone", "critical")
    assert milestone_draft.origin_key == f"chapter_extraction:{CHAPTER}:milestone:{SHADOW}"
    # Without names every entity gets the neutral label, never its id.
    assert HERO not in draft.summary and UNNAMED_ACTOR_LABEL in draft.summary


def test_violations_come_from_the_review_boundary_and_the_gate_detail() -> None:
    protection = _violation()
    milestone = _violation(protection_node_id="", milestone_ref=SHADOW, kind="milestone", ref=SHADOW,
                           impact_level="critical")
    plain = ContinuityIssue(rule_name="movement_too_fast", severity="error", description="d", reviewer="r",
                            original_result={"code": "movement_too_fast"})
    verdict = ReviewVerdict(verdict="fail", issues=[plain, _boundary_issue(protection)])
    gate = BookStateReviewVerdict(project_id=PROJECT_ID, chapter_number=CHAPTER, verdict_id="v", accepted=False, issues=[
        BookStateReviewIssue(severity="error", code="movement_too_fast", target_ref="node:hero", message="m"),
        BookStateReviewIssue(severity="error", code="milestone_ref_ended", target_ref=f"node:{SHADOW}", message="m",
                             detail=asdict(milestone)),
    ])

    assert violations_from_verdict(verdict) == [protection]
    assert violations_from_gate(gate) == [milestone]


def test_chapter_conflict_summary_has_no_proposition(factory) -> None:
    keep_secret = _violation(protection_node_id="keep-secret", kind="must_not_know", ref=HERO, observer_ref=HERO,
                             fact_ref=f"fact:{SECRET}")
    keep_shadow = _violation(protection_node_id="keep-shadow", ref=SHADOW)
    with factory() as session:
        outcome = _recorder(session).record(
            project_id=PROJECT_ID, chapter_number=CHAPTER, candidate_id="candidate-1",
            violations=[keep_secret, keep_shadow],
        )
        session.commit()
        rows = {row.id: row for row in _rows(session)}
        detected = _events(session, DecisionEventType.WORLD_CONFLICT_DETECTED)

    # Rows inserted in one transaction share created_at: look them up in the outcome's violation order.
    secret_row, shadow_row = (rows[conflict_id] for conflict_id in outcome.conflict_ids)
    assert len(rows) == 2
    assert secret_row.origin_key == f"chapter_extraction:{CHAPTER}:keep-secret"
    for row in (secret_row, shadow_row):
        assert PROPOSITION not in row.summary
        assert SHADOW_NAME not in row.summary
    # The observer and the secret's visible subject are named; the concealed subject is left out.
    assert secret_row.summary.count(HERO_NAME) == 2
    assert UNNAMED_ACTOR_LABEL in shadow_row.summary
    assert sorted(event.summary for event in detected) == sorted([secret_row.summary, shadow_row.summary])
    # Operators read the violation itself in proposed_change, and the console its names.
    assert json.loads(secret_row.proposed_change_json)["boundary"] == asdict(keep_secret)
    names = json.loads(secret_row.proposed_change_json)["names"]
    assert names[HERO] == HERO_NAME and PROPOSITION not in json.dumps(names)
    assert json.loads(shadow_row.proposed_change_json)["names"] == {SHADOW: UNNAMED_ACTOR_LABEL}


# routing and idempotency


def test_residual_conflict_reuses_its_open_row(factory) -> None:
    violation = _violation()
    with factory() as session:
        first = _recorder(session).record(
            project_id=PROJECT_ID, chapter_number=CHAPTER, candidate_id="candidate-1", violations=[violation],
        )
        session.commit()
        second = _recorder(session).record(
            project_id=PROJECT_ID, chapter_number=CHAPTER, candidate_id="candidate-2", violations=[violation, violation],
        )
        session.commit()
        (row,) = _rows(session)
        detected = _events(session, DecisionEventType.WORLD_CONFLICT_DETECTED)

    assert first.conflict_ids == second.conflict_ids == (row.id,)
    assert first.blocking_conflict_id == second.blocking_conflict_id == row.id
    assert (row.status, row.required_decider, row.origin_key) == ("open", "human", f"chapter_extraction:{CHAPTER}:{PROTECTION}")
    assert json.loads(row.options_json) == list(CHAPTER_CONFLICT_OPTIONS) == ["rewrite_chapter", "amend_plan"]
    assert [event.related_object_id for event in detected] == [row.id]


@pytest.mark.parametrize(("impact_level", "policy", "decider"), [
    ("moderate", HUMAN, "human"),
    ("major", HUMAN, "human"),
    ("critical", HUMAN, "human"),
    ("critical", CODEX, "human"),
])
def test_recorder_keeps_residual_conflicts_from_auto(factory, impact_level, policy, decider) -> None:
    delegate = FakeDelegate()
    with factory() as session:
        outcome = _recorder(session, policy=policy, delegate=delegate).record(
            project_id=PROJECT_ID, chapter_number=CHAPTER, candidate_id="candidate-1",
            violations=[_violation(impact_level=impact_level)],
        )
        session.commit()
        (row,) = _rows(session)

    assert (row.status, row.required_decider) == ("open", decider)
    assert outcome.blocking_conflict_id == row.id
    assert outcome.codex_rewrite_ids == ()
    assert delegate.calls == []


def test_codex_amend_plan_hands_the_conflict_to_a_human(factory) -> None:
    delegate = FakeDelegate("amend_plan", escalate=True)
    with factory() as session:
        outcome = _recorder(session, policy=CODEX, delegate=delegate).record(
            project_id=PROJECT_ID, chapter_number=CHAPTER, candidate_id="candidate-1", violations=[_violation()],
        )
        session.commit()
        (row,) = _rows(session)

    assert delegate.calls == [row.id]
    assert (row.status, row.required_decider) == ("open", "human")
    assert outcome.blocking_conflict_id == row.id
    assert outcome.codex_rewrite_ids == ()


def test_open_row_no_longer_violated_resolves_as_auto_rewrite(factory) -> None:
    with factory() as session:
        recorded = _recorder(session).record(
            project_id=PROJECT_ID, chapter_number=CHAPTER, candidate_id="candidate-1", violations=[_violation()],
        )
        session.commit()
        outcome = _recorder(session).record(
            project_id=PROJECT_ID, chapter_number=CHAPTER, candidate_id="candidate-2", violations=[],
        )
        session.commit()
        (row,) = _rows(session)
        resolved = _events(session, DecisionEventType.WORLD_CONFLICT_RESOLVED)

    assert outcome.conflict_ids == () and outcome.blocking_conflict_id == ""
    assert row.id == recorded.blocking_conflict_id
    assert row.status == "resolved"
    decision = json.loads(row.decision_json)
    assert (decision["option"], decision["decided_by"]) == ("rewrite_chapter", "auto")
    assert json.loads(row.result_refs_json) == ["candidate:candidate-2"]
    assert [event.related_object_id for event in resolved] == [row.id]


def test_open_row_is_reused_only_by_the_check_that_found_it(factory) -> None:
    """The other check could never close a reused row, so the same boundary found by both checks gets two rows."""
    violation = _violation()
    with factory() as session:
        recorder = _recorder(session)
        draft = recorder.record(project_id=PROJECT_ID, chapter_number=CHAPTER, candidate_id="candidate-1",
                                violations=[violation])
        gate = recorder.record(project_id=PROJECT_ID, chapter_number=CHAPTER, candidate_id="candidate-1",
                               violations=[violation], detected_by="canon_gate")
        again = recorder.record(project_id=PROJECT_ID, chapter_number=CHAPTER, candidate_id="candidate-2",
                                violations=[violation], detected_by="canon_gate")
        session.commit()
        rows = {row.id: row for row in _rows(session)}

    assert draft.blocking_conflict_id != gate.blocking_conflict_id == again.blocking_conflict_id
    assert {conflict_id: json.loads(row.proposed_change_json)["detected_by"] for conflict_id, row in rows.items()} == {
        draft.blocking_conflict_id: "draft_review", gate.blocking_conflict_id: "canon_gate",
    }
    assert {row.origin_key for row in rows.values()} == {f"chapter_extraction:{CHAPTER}:{PROTECTION}"}


def test_no_longer_violated_resolution_uses_the_rows_own_source(factory) -> None:
    """The draft review never reports a gate-only boundary, so only the gate may close a gate row, and back."""
    gate_violation = _violation(protection_node_id="gate-only", impact_level="critical")
    draft_violation = _violation()
    with factory() as session:
        recorder = _recorder(session)
        gate = recorder.record(project_id=PROJECT_ID, chapter_number=CHAPTER, candidate_id="candidate-1",
                               violations=[gate_violation], detected_by="canon_gate")
        draft = recorder.record(project_id=PROJECT_ID, chapter_number=CHAPTER, candidate_id="candidate-1",
                                violations=[draft_violation])
        session.commit()
        recorder.record(project_id=PROJECT_ID, chapter_number=CHAPTER, candidate_id="candidate-2", violations=[])
        session.commit()
        after_draft = {row.id: row.status for row in _rows(session)}
        recorder.record(project_id=PROJECT_ID, chapter_number=CHAPTER, candidate_id="candidate-2", violations=[],
                        detected_by="canon_gate")
        session.commit()
        after_gate = {row.id: row.status for row in _rows(session)}
        gate_row = session.get(WorldPlanConflictRow, gate.blocking_conflict_id)

    assert after_draft == {gate.blocking_conflict_id: "open", draft.blocking_conflict_id: "resolved"}
    assert after_gate == {gate.blocking_conflict_id: "resolved", draft.blocking_conflict_id: "resolved"}
    assert json.loads(gate_row.proposed_change_json)["detected_by"] == "canon_gate"


# the chapter stage


def _writer_output(body: str = "The chapter body.") -> WriterOutput:
    return WriterOutput(
        project_id=PROJECT_ID, chapter_number=CHAPTER, title="Chapter three", body=body, char_count=len(body),
        end_of_chapter_summary="The chapter ends.",
    )


def _persist_candidate(session, output: WriterOutput, verdict: ReviewVerdict) -> str:
    plan = StateRepository(session).get_chapter_plan(PROJECT_ID, CHAPTER)
    version = len(list(session.scalars(select(ChapterDraft).where(ChapterDraft.chapter_plan_id == plan.id)))) + 1
    draft = ChapterDraft(chapter_plan_id=plan.id, version=version, body_text=output.body,
                         summary=output.end_of_chapter_summary, char_count=output.char_count)
    session.add(draft)
    session.flush()
    review = ChapterReview(draft_id=draft.id, verdict=verdict.verdict, issues_json="[]",
                           review_meta_json=json.dumps({"verdict": verdict.verdict}))
    session.add(review)
    session.flush()
    candidate = CandidateDraftRepository(session).create_reviewed_version(
        project_id=PROJECT_ID, chapter_plan=plan, draft=draft, review=review, writer_output=output,
        plan_revision=candidate_plan_revision(plan), policy_version=1,
    )
    StateUpdater(session).mark_chapter_status(PROJECT_ID, CHAPTER, "drafted")
    return candidate.id


class FakeRepair:
    """Persists each draft as the repair service does; the verdicts stand in for review and the extra pass."""

    def __init__(self, review: ReviewVerdict, *, after_extra_pass: ReviewVerdict | None = None) -> None:
        self.review = review
        self.after_extra_pass = after_extra_pass
        self.extra_passes: list[list[str]] = []
        self.reviewed: list[str] = []

    def review_candidate(self, **kwargs):
        self.reviewed.append(kwargs["writer_output"].body)
        _persist_candidate(kwargs["session"], kwargs["writer_output"], self.review)
        return kwargs["writer_output"], self.review, False

    def repair_world_conflict(self, **kwargs):
        self.extra_passes.append([row.id for row in kwargs["conflicts"]])
        # The repair loop counts its attempt on the chapter plan, as the real pass does.
        plan = kwargs["chapter_plan"]
        plan.repair_attempt_count = int(plan.repair_attempt_count or 0) + 1
        output = _writer_output("The repaired chapter body.")
        _persist_candidate(kwargs["session"], output, self.after_extra_pass)
        return output, self.after_extra_pass, False


def _run(factory, monkeypatch, *, repair, policy=HUMAN, delegate=None, canon_preparation=None, saved=None,
         execution=None):
    """Run chapter 3 through the stage; ``saved`` is the writer output the latest draft's artifact holds."""
    from forwin.generation.pipeline_core import project_chapters

    events: list[tuple[str, dict]] = []
    written: list[int] = []
    loaded: list[str] = []

    def load_saved(path):
        loaded.append(path)
        return saved or _writer_output()

    monkeypatch.setattr(project_chapters, "handle_chapter_review_gate", lambda *_args, **_kwargs: SimpleNamespace(
        pause_required=False, gate_approved=False,
    ))
    monkeypatch.setattr(project_chapters, "save_accepted_trope_usage_for_chapter", lambda *_args, **_kwargs: None)
    with factory() as session:
        repo, updater = StateRepository(session), StateUpdater(session)

        def write(**kwargs):
            written.append(kwargs["chapter_number"])
            return _writer_output()

        def commit(_plan):
            updater.mark_chapter_status(PROJECT_ID, CHAPTER, "accepted")
            session.commit()
            return SimpleNamespace(blocked=False, commit_id="commit-3")

        stage = SimpleNamespace(
            policy=SimpleNamespace(canon=SimpleNamespace(hard_floor=False)),
            _project_policy=lambda *_args: policy,
            _abort_requested=lambda: False,
            _pause_requested=lambda: False,
            _recover_post_canon_before_chapter=lambda **_kwargs: None,
            _ensure_chapter_plan=lambda **kwargs: repo.get_chapter_plan(kwargs["project_id"], kwargs["chapter_number"]),
            _strict_progression_block=lambda **_kwargs: ("", "", ""),
            _manual_boundary_checkpoint=lambda *_args, **_kwargs: None,
            _emit_progress=lambda event, **payload: events.append((event, deepcopy(payload))),
            retrieval_broker=SimpleNamespace(last_observability_summary={}, build_chapter_context=lambda *_args: SimpleNamespace()),
            _audit_current_plan_before_write=lambda **kwargs: kwargs["context"],
            _write_chapter_with_attention_fallback=write,
            repair=repair,
            repair_execution=execution or SimpleNamespace(),
            _load_writer_output_from_meta=load_saved,
            _review_issue_payloads=lambda verdict: [issue.model_dump(mode="json") for issue in verdict.issues],
            _review_canon_risk=lambda _verdict: "low",
            _chapter_conflict_recorder=lambda session, policy: _recorder(session, policy=policy, delegate=delegate),
            canon_preparation=canon_preparation or SimpleNamespace(
                prepare=lambda **kwargs: SimpleNamespace(blocked=False, plan=kwargs["chapter_number"]),
            ),
            canon_preparation_context=SimpleNamespace(),
            canon_admission=SimpleNamespace(commit_plan=commit),
            _make_state_helpers=lambda _session: (repo, updater, SimpleNamespace()),
            _run_phase3_pass=lambda **_kwargs: {"run_ids": {}},
            _run_post_canon_order_controls=lambda **_kwargs: {},
        )
        result = ChapterExecutionStage._run_project_chapters(
            stage, session=session, repo=repo, updater=updater, checker=SimpleNamespace(),
            project_id=PROJECT_ID, chapter_numbers=[CHAPTER], requested_chapters=1,
        )
    with factory() as session:
        status = session.get(ChapterPlan, f"chapter-conflict-plan-{CHAPTER}").status
        drafts = len(list(session.scalars(select(ChapterDraft).where(
            ChapterDraft.chapter_plan_id == f"chapter-conflict-plan-{CHAPTER}",
        ))))
    paused = [payload.get("payload") for _event, payload in events if payload.get("stage") == "paused_for_review"]
    return SimpleNamespace(result=result, written=written, status=status, drafts=drafts, paused=paused, loaded=loaded)


def test_violation_fixed_in_repair_loop_stores_no_row(factory, monkeypatch) -> None:
    run = _run(factory, monkeypatch, repair=FakeRepair(PASSING))

    assert run.result.completed_chapters == [CHAPTER]
    assert run.result.pause_reason == ""
    assert run.status == "accepted"
    with factory() as session:
        assert _rows(session) == []
        assert _events(session, DecisionEventType.WORLD_CONFLICT_DETECTED) == []


def test_residual_violation_creates_one_conflict_and_pauses_mid_chapter(factory, monkeypatch) -> None:
    repair = FakeRepair(_failing(_violation()))

    first = _run(factory, monkeypatch, repair=repair)
    with factory() as session:
        (row,) = _rows(session)
    second = _run(factory, monkeypatch, repair=repair)

    for run in (first, second):
        assert run.result.pause_reason == "world_conflict"
        assert run.result.paused_chapters == [CHAPTER]
        assert run.result.completed_chapters == []
        assert run.paused == [{"reason": "world_conflict", "conflict_id": row.id}]
        # The draft stays as the chapter's latest candidate.
        assert (run.status, run.drafts) == ("needs_review", 1)
    # The second run stops before writing: the open conflict blocks the chapter.
    assert (first.written, second.written) == ([CHAPTER], [])
    with factory() as session:
        assert [stored.id for stored in _rows(session)] == [row.id]
        assert (row.status, row.required_decider, row.source) == ("open", "human", "chapter_extraction")
        assert len(_events(session, DecisionEventType.WORLD_CONFLICT_DETECTED)) == 1
        residual = json.loads(session.get(ChapterPlan, f"chapter-conflict-plan-{CHAPTER}").residual_review_issues_json)
        assert [issue["rule_name"] for issue in residual] == ["protection_violated"]


@pytest.mark.parametrize("fixed", [False, True], ids=["still_residual", "fixed_by_extra_pass"])
def test_codex_rewrite_gets_one_more_pass_then_human(factory, monkeypatch, fixed) -> None:
    violation = _violation(impact_level="moderate")
    repair = FakeRepair(_failing(violation), after_extra_pass=PASSING if fixed else _failing(violation))
    delegate = FakeDelegate("rewrite_chapter")

    run = _run(factory, monkeypatch, repair=repair, policy=CODEX, delegate=delegate)

    with factory() as session:
        rows = _rows(session)
        repaired = CandidateDraftRepository(session).latest_for_chapter(project_id=PROJECT_ID, chapter_number=CHAPTER)
        pending = WorldConflictRepository(session).pending_chapter_resumes(PROJECT_ID, chapter_number=CHAPTER)
    codex_row = rows[0]
    assert delegate.calls == [codex_row.id]
    assert repair.extra_passes == [[codex_row.id]]
    decision = json.loads(codex_row.decision_json)
    assert (codex_row.status, decision["option"], decision["decided_by"]) == ("resolved", "rewrite_chapter", "codex")
    # The extra pass ran on the repaired candidate, so the decision is no pending resume.
    assert json.loads(codex_row.result_refs_json) == ["trace-1", f"candidate:{repaired.id}"]
    assert pending == []
    if fixed:
        assert len(rows) == 1
        assert run.result.completed_chapters == [CHAPTER]
        assert run.status == "accepted"
        return
    # Codex decides once per boundary: the still-residual violation goes straight to a human.
    human_row = rows[1]
    assert len(rows) == 2
    assert human_row.origin_key == codex_row.origin_key
    assert (human_row.status, human_row.required_decider) == ("open", "human")
    assert run.result.pause_reason == "world_conflict"
    assert run.paused == [{"reason": "world_conflict", "conflict_id": human_row.id}]
    assert (run.status, run.drafts) == ("needs_review", 2)


def test_boundary_first_seen_after_the_extra_pass_goes_to_a_human(factory, monkeypatch) -> None:
    first = _violation(impact_level="moderate")
    second = _violation(protection_node_id="protection-two", impact_level="moderate")
    delegate = FakeDelegate("rewrite_chapter")
    repair = FakeRepair(_failing(first), after_extra_pass=_failing(second))

    run = _run(factory, monkeypatch, repair=repair, policy=CODEX, delegate=delegate)

    with factory() as session:
        rows = {row.origin_key.rsplit(":", 1)[-1]: row for row in _rows(session)}
    # Codex decides once per chapter pass: the repaired draft's new boundary goes straight to a human.
    assert delegate.calls == [rows[PROTECTION].id]
    assert repair.extra_passes == [[rows[PROTECTION].id]]
    assert (rows["protection-two"].status, rows["protection-two"].required_decider) == ("open", "human")
    assert run.result.pause_reason == "world_conflict"
    assert run.paused == [{"reason": "world_conflict", "conflict_id": rows["protection-two"].id}]
    assert run.status == "needs_review"


class SequencedPreparation:
    """Canon preparation that refuses an ineligible review, as the real one does; otherwise its review gate blocks
    with each verdict in turn, the last one from then on."""

    def __init__(self, *gates: BookStateReviewVerdict) -> None:
        self.gates = list(gates)
        self.calls = 0

    def prepare(self, **kwargs):
        self.calls += 1
        if reason := candidate_ineligibility_reason(kwargs["verdict"]):
            return CanonPreparationOutcome(blocked_path=reason, block_kind="candidate_ineligible")
        gate = self.gates[min(self.calls - 1, len(self.gates) - 1)]
        return CanonPreparationOutcome(
            blocked_path="book-state-review-gate-blocked", block_kind="book_state", book_state_review=gate,
        )


def _gate(*violations: DeltaBoundaryViolation) -> BookStateReviewVerdict:
    return BookStateReviewVerdict(project_id=PROJECT_ID, chapter_number=CHAPTER, verdict_id="v", accepted=False, issues=[
        BookStateReviewIssue(severity="error", code="protection_violated", message="m",
                             target_ref=f"review_constraint:{violation.protection_node_id}", detail=asdict(violation))
        for violation in violations
    ])


@pytest.mark.parametrize("blocks", ["pass_then_critical", "critical_beside_codex"])
def test_gate_violation_stays_open_after_the_extra_pass(factory, monkeypatch, blocks) -> None:
    critical = _violation(impact_level="critical")
    moderate = _violation(protection_node_id="protection-two", impact_level="moderate")
    gates = [_gate(moderate), _gate(critical, moderate)] if blocks == "pass_then_critical" else [_gate(critical, moderate)]
    preparation = SequencedPreparation(*gates)
    delegate = FakeDelegate("rewrite_chapter")
    repair = FakeRepair(PASSING, after_extra_pass=PASSING)

    run = _run(factory, monkeypatch, repair=repair, policy=CODEX, delegate=delegate, canon_preparation=preparation)

    with factory() as session:
        rows = _rows(session)
        pending = WorldConflictRepository(session).pending_chapter_resumes(PROJECT_ID, chapter_number=CHAPTER)
    decided_by = [json.loads(row.decision_json or "{}").get("decided_by") for row in rows]
    (critical_row,) = [row for row in rows if row.origin_key.endswith(f":{PROTECTION}")]
    (codex_row,) = [row for row in rows if row.required_decider == "codex"]
    # A residual gate conflict is never auto: the critical boundary the chapter still breaks stays open.
    assert "auto" not in decided_by
    assert (critical_row.status, critical_row.required_decider) == ("open", "human")
    assert json.loads(critical_row.proposed_change_json)["detected_by"] == "canon_gate"
    assert run.result.pause_reason == "world_conflict"
    assert run.paused == [{"reason": "world_conflict", "conflict_id": critical_row.id}]
    assert run.status == "needs_review"
    assert delegate.calls == [codex_row.id]
    if blocks == "pass_then_critical":
        # The pass ran on the first block; the gate, not the repaired draft's verdict, judged the result.
        assert preparation.calls == 2
        assert repair.extra_passes == [[codex_row.id]]
        assert pending == []
        (repeat,) = [row for row in rows if row.origin_key == codex_row.origin_key and row.id != codex_row.id]
        assert (repeat.status, repeat.required_decider) == ("open", "human")
    else:
        # A human conflict already blocks the chapter: no pass, and Codex's decision waits as a pending resume.
        assert preparation.calls == 1
        assert repair.extra_passes == []
        assert [row.id for row in pending] == [codex_row.id]
        assert len(rows) == 2


def test_gate_pass_records_the_repaired_drafts_own_violations(factory, monkeypatch) -> None:
    """Canon preparation would refuse the failing repaired draft; its new boundary pauses the chapter instead."""
    gate_only = _violation(impact_level="moderate")
    new_in_draft = _violation(protection_node_id="protection-three", impact_level="moderate")
    preparation = SequencedPreparation(_gate(gate_only))
    delegate = FakeDelegate("rewrite_chapter")
    repair = FakeRepair(PASSING, after_extra_pass=_failing(new_in_draft))

    run = _run(factory, monkeypatch, repair=repair, policy=CODEX, delegate=delegate, canon_preparation=preparation)

    with factory() as session:
        rows = {row.origin_key.rsplit(":", 1)[-1]: row for row in _rows(session)}
        repaired = CandidateDraftRepository(session).latest_for_chapter(project_id=PROJECT_ID, chapter_number=CHAPTER)
    gate_row, draft_row = rows[PROTECTION], rows["protection-three"]
    assert set(rows) == {PROTECTION, "protection-three"}
    # The repaired draft's boundary is a draft-review conflict for a human: this recording follows the pass.
    assert (draft_row.status, draft_row.required_decider) == ("open", "human")
    assert json.loads(draft_row.proposed_change_json)["detected_by"] == "draft_review"
    assert run.result.pause_reason == "world_conflict"
    assert run.paused == [{"reason": "world_conflict", "conflict_id": draft_row.id}]
    assert (run.status, preparation.calls, delegate.calls) == ("needs_review", 1, [gate_row.id])
    # The gate row keeps Codex's decision and the pass's candidate; the draft verdict never closes it.
    decision = json.loads(gate_row.decision_json)
    assert (gate_row.status, decision["decided_by"], decision["option"]) == ("resolved", "codex", "rewrite_chapter")
    assert json.loads(gate_row.result_refs_json) == ["trace-1", f"candidate:{repaired.id}"]
    assert json.loads(gate_row.proposed_change_json)["detected_by"] == "canon_gate"


@pytest.mark.parametrize("after_extra_pass", ["passes", "fails_without_boundary"])
def test_gate_pass_without_draft_boundary_prepares_again(factory, monkeypatch, after_extra_pass) -> None:
    """Without a boundary in the repaired draft's review, the gate judges again, or the generic pause stands."""
    plain = ContinuityIssue(rule_name="movement_too_fast", severity="error", description="d", reviewer="r")
    verdict = PASSING if after_extra_pass == "passes" else ReviewVerdict(verdict="fail", issues=[plain])
    preparation = SequencedPreparation(_gate(_violation(impact_level="moderate")))
    repair = FakeRepair(PASSING, after_extra_pass=verdict)

    run = _run(factory, monkeypatch, repair=repair, policy=CODEX, delegate=FakeDelegate("rewrite_chapter"),
               canon_preparation=preparation)

    with factory() as session:
        rows = _rows(session)
    assert preparation.calls == 2
    assert all(json.loads(row.proposed_change_json)["detected_by"] == "canon_gate" for row in rows)
    assert run.status == "needs_review"
    if after_extra_pass == "passes":
        # The gate still blocks the repaired draft: that recording follows the pass, so a human decides.
        assert run.result.pause_reason == "world_conflict"
        assert [(row.status, row.required_decider) for row in rows] == [("resolved", "codex"), ("open", "human")]
    else:
        # Canon preparation refuses the failing draft before its gate runs, and nothing is recorded from it.
        assert run.result.pause_reason == ""
        assert [(row.status, row.required_decider) for row in rows] == [("resolved", "codex")]


def test_canon_preparation_outcome_carries_the_gate_verdict(factory) -> None:
    gate = BookStateReviewVerdict(project_id=PROJECT_ID, chapter_number=CHAPTER, verdict_id="v", accepted=False)
    preparer = SimpleNamespace(prepare=lambda **_kwargs: BookStatePreparationOutcome(
        blocked_path="book-state-review-gate-blocked", review_verdict=gate,
    ))
    with factory() as session:
        outcome = CanonPreparationService(book_state_preparer=preparer)._prepare_book_state(
            context=None, session=session, candidate_id="missing", project_id=PROJECT_ID, chapter_number=CHAPTER,
            writer_output=None, verdict=None, entity_admission_plan=None,
        )

    assert (outcome.block_kind, outcome.book_state_review) == ("book_state", gate)


def test_canon_prep_violation_creates_conflict(factory, monkeypatch) -> None:
    violation = _violation(impact_level="critical")
    gate = BookStateReviewVerdict(project_id=PROJECT_ID, chapter_number=CHAPTER, verdict_id="v", accepted=False, issues=[
        BookStateReviewIssue(severity="error", code="protection_violated", target_ref=f"review_constraint:{PROTECTION}",
                             message="m", detail=asdict(violation)),
    ])
    preparation = SimpleNamespace(prepare=lambda **_kwargs: CanonPreparationOutcome(
        blocked_path="book-state-review-gate-blocked", block_kind="book_state", book_state_review=gate,
    ))

    run = _run(factory, monkeypatch, repair=FakeRepair(PASSING), canon_preparation=preparation)

    with factory() as session:
        (row,) = _rows(session)
    assert (row.status, row.required_decider, row.impact_level) == ("open", "human", "critical")
    assert row.origin_key == f"chapter_extraction:{CHAPTER}:{PROTECTION}"
    assert run.result.pause_reason == "world_conflict"
    assert run.paused == [{"reason": "world_conflict", "conflict_id": row.id}]
    assert (run.status, run.drafts) == ("needs_review", 1)


# resolution and resume


def _open_conflict(factory, monkeypatch, violation: DeltaBoundaryViolation) -> str:
    """Run the chapter once: its residual violation pauses it mid-way with an open human conflict."""
    run = _run(factory, monkeypatch, repair=FakeRepair(_failing(violation)))
    assert (run.status, run.result.pause_reason) == ("needs_review", "world_conflict")
    with factory() as session:
        (row,) = _rows(session)
    return row.id


def _resolve(factory, conflict_id: str, option: str):
    with factory() as session:
        result = ConflictResolutionService(session, admission=CanonAdmissionService(), policy=HUMAN).resolve(
            project_id=PROJECT_ID, conflict_id=conflict_id, decision=ConflictDecision(
                option=option, decided_by="human", rationale="rationale", decided_at=datetime.now(UTC),
            ),
        )
        session.commit()
    return result


class SavedDraftRepair(RepairService):
    """The real world-conflict repair; a fresh review would mean the chapter was written again."""

    def review_candidate(self, **kwargs):
        raise AssertionError("a rewrite_chapter resume repairs the saved draft")


def _decision_events(**_kwargs):
    return SimpleNamespace(id="event-1")


def test_rewrite_chapter_resumes_repair_from_saved_draft(factory, monkeypatch) -> None:
    conflict_id = _open_conflict(factory, monkeypatch, _violation())
    with factory() as session:
        draft = session.scalar(select(ChapterDraft).where(ChapterDraft.chapter_plan_id == PLAN_ID))
        draft_id, artifact = draft.id, draft.llm_raw_response
    result = _resolve(factory, conflict_id, "rewrite_chapter")
    with factory() as session:
        row = session.get(WorldPlanConflictRow, conflict_id)
        plan_status = session.get(ChapterPlan, PLAN_ID).status
        pending = WorldConflictRepository(session).pending_chapter_resumes(PROJECT_ID, chapter_number=CHAPTER)
        plan_constraints = session.scalars(select(GraphDeltaRow.id).where(
            GraphDeltaRow.project_id == PROJECT_ID, GraphDeltaRow.source_type == "plan_constraint",
        )).all()
    # No canon is written; the plan goes back to planned and the decision waits to resume the chapter.
    assert result.graph_delta_ids == () and plan_constraints == []
    assert (row.status, json.loads(row.decision_json)["option"], json.loads(row.result_refs_json)) == (
        "resolved", "rewrite_chapter", [],
    )
    assert plan_status == "planned" and [item.id for item in pending] == [conflict_id]

    loops: list[dict] = []

    def repair_loop(_execution, **kwargs):
        loops.append({**kwargs, "current_draft_id": kwargs["current_draft"].id})
        output = _writer_output("The repaired chapter body.")
        _persist_candidate(kwargs["session"], output, PASSING)
        return output, PASSING, False

    monkeypatch.setattr(repair_service, "_run_repair_loop_for_phase", repair_loop)
    execution = SimpleNamespace(_record_decision_event=_decision_events, _review_event_payload=lambda _review: {})
    run = _run(factory, monkeypatch, repair=SavedDraftRepair(), execution=execution)

    # No new draft is written: the saved one goes to repair, in its own phase, with the boundary as must_fix.
    assert run.written == [] and run.loaded == [artifact]
    (loop,) = loops
    assert loop["repair_phase"] == "world_conflict_repair"
    assert loop["current_draft_id"] == draft_id
    review = loop["current_review"]
    assert review.verdict == "fail" and review.repair_instruction.repair_scope == "draft"
    assert review.repair_instruction.must_fix == [protection_repair_fix("must_be_alive", {"name": HERO_NAME})]
    assert HERO_NAME in review.repair_instruction.must_fix[0]
    assert [issue.original_result["conflict_id"] for issue in review.issues] == [conflict_id]
    assert PROPOSITION not in review.model_dump_json()
    assert (run.status, run.result.completed_chapters) == ("accepted", [CHAPTER])
    with factory() as session:
        latest = CandidateDraftRepository(session).latest_for_chapter(project_id=PROJECT_ID, chapter_number=CHAPTER)
        row = session.get(WorldPlanConflictRow, conflict_id)
        assert json.loads(row.result_refs_json) == [f"candidate:{latest.id}"]
        assert WorldConflictRepository(session).pending_chapter_resumes(PROJECT_ID, chapter_number=CHAPTER) == []
        # The synthetic review was saved on the saved draft for the repair to start from.
        saved_reviews = session.scalars(select(ChapterReview).where(ChapterReview.draft_id == draft_id)).all()
        assert len(saved_reviews) == 2


def test_amend_plan_releases_protection_and_rereviews_same_text(factory, monkeypatch) -> None:
    with factory() as session:
        CanonAdmissionService().commit_plan_constraints(
            session=session, project_id=PROJECT_ID, constraint_set_id="sheet-1", releases=[],
            creates=[ProtectionSpec(ref=HERO, kind="must_be_alive", until_chapter=6)],
        )
        session.commit()
        runtime = BookStateProjection(session).load_runtime_as_of(PROJECT_ID, as_of_chapter=0)
        (protection_id,) = [node.id for node in runtime.narrative.nodes_by_id.values()
                            if node.node_type == "review_constraint"]
    conflict_id = _open_conflict(factory, monkeypatch, _violation(protection_node_id=protection_id))

    result = _resolve(factory, conflict_id, "amend_plan")

    constraint_set_id = f"conflict:{conflict_id}"
    with factory() as session:
        again = CanonAdmissionService().commit_plan_constraints(
            session=session, project_id=PROJECT_ID, constraint_set_id=constraint_set_id, creates=[],
            releases=[protection_id],
        )
        releases = session.scalars(select(GraphDeltaRow).where(
            GraphDeltaRow.project_id == PROJECT_ID, GraphDeltaRow.source_type == "plan_constraint",
            GraphDeltaRow.source_id == constraint_set_id,
        )).all()
        runtime = BookStateProjection(session).load_runtime_as_of(PROJECT_ID, as_of_chapter=CHAPTER - 1)
        row = session.get(WorldPlanConflictRow, conflict_id)
        plan_status = session.get(ChapterPlan, PLAN_ID).status
    # One plan_constraint delta releases the protection, and committing the same set again changes nothing.
    assert [delta.id for delta in releases] == list(result.graph_delta_ids)
    assert again.idempotent and again.graph_delta_ids == result.graph_delta_ids
    assert runtime.narrative.nodes_by_id[protection_id].payload["status"] == "released"
    assert (row.status, json.loads(row.decision_json)["option"]) == ("resolved", "amend_plan")
    assert json.loads(row.result_refs_json) == list(result.graph_delta_ids)
    assert plan_status == "planned"

    repair = FakeRepair(PASSING)
    run = _run(factory, monkeypatch, repair=repair, saved=_writer_output("The saved chapter body."))

    # The same text is reviewed again without a writer call, and now the chapter passes.
    assert run.written == [] and repair.reviewed == ["The saved chapter body."]
    assert (run.status, run.result.completed_chapters, run.result.pause_reason) == ("accepted", [CHAPTER], "")
    with factory() as session:
        latest = CandidateDraftRepository(session).latest_for_chapter(project_id=PROJECT_ID, chapter_number=CHAPTER)
        row = session.get(WorldPlanConflictRow, conflict_id)
        assert json.loads(row.result_refs_json) == [*result.graph_delta_ids, f"candidate:{latest.id}"]


def test_accept_review_refused_while_chapter_conflict_open(factory, monkeypatch) -> None:
    conflict_id = _open_conflict(factory, monkeypatch, _violation())
    stage = SimpleNamespace(
        _SessionFactory=factory,
        _make_state_helpers=lambda session: (StateRepository(session), StateUpdater(session), None),
    )

    with pytest.raises(ChapterConflictPending, match=conflict_id) as refused:
        AcceptanceStage.accept_review(stage, PROJECT_ID, CHAPTER)
    assert isinstance(refused.value, ValueError) and refused.value.conflict_id == conflict_id

    # Over HTTP the refusal is a 409, not a bad request.
    with pytest.raises(HTTPException) as http:
        approve_chapter_review(
            PROJECT_ID, CHAPTER, ChapterReviewApproveRequest(reason="operator review"),
            config=object(), pipeline=SimpleNamespace(
                accept_review=lambda *args, **kwargs: AcceptanceStage.accept_review(stage, *args, **kwargs),
            ),
            get_session=factory, display_datetime=str, active_generation_task_error_cls=RuntimeError,
            require_reason=lambda reason, action: reason, project_has_active_generation_task=lambda *_a, **_k: False,
            generation_task_conflict_message=str, log_decision_event=lambda *_a, **_k: None,
            create_continue_generation_task=lambda **_kwargs: None, update_task=lambda *_a, **_k: None,
        )
    assert http.value.status_code == 409 and conflict_id in http.value.detail
    with factory() as session:
        assert session.get(ChapterPlan, PLAN_ID).status == "needs_review"
        assert session.get(WorldPlanConflictRow, conflict_id).status == "open"


class _LockOrder:
    """Per connection, whether a transaction that wrote the chapter plans went on to lock or write a conflict row."""

    def __init__(self, engine) -> None:
        self.dirty: set[int] = set()
        self.inversions: list[str] = []
        self.engine = engine
        event.listen(engine, "before_cursor_execute", self.statement)
        event.listen(engine, "commit", self.end)
        event.listen(engine, "rollback", self.end)

    def statement(self, conn, _cursor, statement, *_args) -> None:
        text = " ".join(statement.split()).upper()
        if text.startswith("UPDATE CHAPTER_PLANS"):
            self.dirty.add(id(conn.connection.dbapi_connection))
        elif id(conn.connection.dbapi_connection) in self.dirty and "WORLD_PLAN_CONFLICTS" in text and (
            text.startswith("UPDATE WORLD_PLAN_CONFLICTS") or "FOR UPDATE" in text
        ):
            self.inversions.append(text[:120])

    def end(self, conn) -> None:
        self.dirty.discard(id(conn.connection.dbapi_connection))

    def remove(self) -> None:
        event.remove(self.engine, "before_cursor_execute", self.statement)
        event.remove(self.engine, "commit", self.end)
        event.remove(self.engine, "rollback", self.end)


@pytest.mark.parametrize("path", ["codex_pass", "resume"])
def test_conflict_rows_are_locked_only_after_the_chapter_plan_write_commits(factory, monkeypatch, path) -> None:
    """Lock order: a conflict row comes before the chapter plan, so each write of the plan commits first."""
    violation = _violation(impact_level="moderate")
    if path == "resume":
        conflict_id = _open_conflict(factory, monkeypatch, violation)
        _resolve(factory, conflict_id, "rewrite_chapter")
    order = _LockOrder(factory.kw["bind"])
    try:
        if path == "codex_pass":
            repair = FakeRepair(_failing(violation), after_extra_pass=_failing(violation))
            _run(factory, monkeypatch, repair=repair, policy=CODEX, delegate=FakeDelegate("rewrite_chapter"))
            assert len(repair.extra_passes) == 1
        else:
            repair = FakeRepair(_failing(violation), after_extra_pass=_failing(violation))
            run = _run(factory, monkeypatch, repair=repair)
            assert run.written == [] and len(repair.extra_passes) == 1
    finally:
        order.remove()
    assert order.inversions == []
