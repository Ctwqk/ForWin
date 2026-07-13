from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from forwin.candidate_drafts import CandidateDraftRepository
from forwin.audit.events import DecisionEventType
from forwin.audit.gate_outcome import parse_gate_outcome
from forwin.canon.plan import (
    CanonAuditEvent,
    CanonCommitPlan,
    CanonOutboxEvent,
)
from forwin.canon.preparation import (
    BookStatePreparationOutcome,
    CanonPreparationContext,
    CanonPreparationService,
)
from forwin.canon.types import CanonQualityGateOutcome
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.book_state import GraphDeltaRow
from forwin.models.draft import ChapterDraft, ChapterReview
from forwin.models.entity import Entity, EntityAlias
from forwin.models.outbox import OutboxEvent
from forwin.naming import EntityAdmissionPlan
from forwin.naming.entity_registrar import writer_output_admission_fingerprint
from forwin.protocol.review import ReviewVerdict
from forwin.protocol.book_state import ApprovedGraphDeltaSet, GraphDelta
from forwin.protocol.writer import WriterOutput
from forwin.runtime.policy import RuntimePolicy
from forwin.state.updater import StateUpdater
from tests.postgres import postgres_test_url


def _approved_changes() -> ApprovedGraphDeltaSet:
    return ApprovedGraphDeltaSet(
        project_id="project-plan",
        chapter_number=7,
        graph_deltas=[
            GraphDelta(
                id="delta-plan-7",
                project_id="project-plan",
                chapter_number=7,
                summary="第七章状态变化",
            )
        ],
        approved_by=["book_state_review"],
        review_verdict_id="review-plan-7",
    )


def _entity_plan() -> EntityAdmissionPlan:
    return EntityAdmissionPlan(
        project_id="project-plan",
        chapter_number=7,
        candidate_fingerprint="candidate-body-a",
    )


def _build_plan(*, body_hash: str = "body-a", plan_revision: str = "plan-a"):
    return CanonCommitPlan.build(
        project_id="project-plan",
        chapter_number=7,
        candidate_id="candidate-7",
        candidate_body_hash=body_hash,
        plan_revision=plan_revision,
        policy_version=3,
        expected_previous_accepted_chapter=6,
        expected_book_state_chapter=6,
        approved_book_state_changes=_approved_changes(),
        entity_admission_plan=_entity_plan(),
        acceptance_mode="normal",
        repair_attempt_count=1,
        residual_review_issues=[],
        canon_risk_level="low",
        audit_events=(
            CanonAuditEvent(
                event_type="CANON_COMMIT",
                event_family="business_event",
                summary="canon commit",
            ),
        ),
        outbox_events=(
            CanonOutboxEvent(
                event_type="canon.post_commit.requested",
                payload={"project_id": "project-plan", "chapter_number": 7},
            ),
        ),
    )


def test_canon_commit_plan_key_is_deterministic_and_candidate_specific() -> None:
    first = _build_plan()
    identical = _build_plan()
    changed_body = _build_plan(body_hash="body-b")
    changed_plan = _build_plan(plan_revision="plan-b")

    assert first.idempotency_key == identical.idempotency_key
    assert first.idempotency_key != changed_body.idempotency_key
    assert first.idempotency_key != changed_plan.idempotency_key
    assert first.outbox_events[0].event_id == (
        f"{first.idempotency_key}:canon.post_commit.requested"
    )


def test_canon_commit_plan_is_frozen_and_rejects_cross_project_payloads() -> None:
    plan = _build_plan()

    with pytest.raises(ValidationError):
        plan.chapter_number = 8

    with pytest.raises(ValueError, match="BookState project mismatch"):
        CanonCommitPlan.build(
            **{
                **plan.model_dump(mode="python", exclude={"idempotency_key"}),
                "approved_book_state_changes": plan.approved_book_state_changes.model_copy(
                    update={"project_id": "other-project"}
                ),
            }
        )


def test_canon_commit_plan_round_trips_without_losing_typed_payloads() -> None:
    plan = _build_plan()

    restored = CanonCommitPlan.model_validate_json(plan.model_dump_json())

    assert restored == plan
    assert restored.approved_book_state_changes.graph_deltas[0].id == "delta-plan-7"
    assert restored.entity_admission_plan.candidate_fingerprint == "candidate-body-a"


def test_prepare_from_approved_persists_only_precanon_candidate_state() -> None:
    engine = get_engine(postgres_test_url("canon-plan-preparation"))
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        updater = StateUpdater(session)
        project = updater.create_project(
            title="Canon plan preparation",
            premise="准备阶段不写 Canon",
            genre="悬疑",
            runtime_policy=RuntimePolicy.for_profile("standard"),
        )
        arc = updater.create_arc_plan(project.id, "第一弧")
        chapter = updater.create_chapter_plan(
            project_id=project.id,
            arc_plan_id=arc.id,
            chapter_number=1,
            title="第一章",
            one_line="准备 Canon plan",
            goals=["不产生权威写"],
        )
        output = WriterOutput(
            project_id=project.id,
            chapter_number=1,
            title="第一章",
            body="准备阶段正文",
            char_count=6,
            end_of_chapter_summary="准备完成",
        )
        draft = ChapterDraft(
            chapter_plan_id=chapter.id,
            version=1,
            body_text=output.body,
            summary=output.end_of_chapter_summary,
            char_count=output.char_count,
        )
        session.add(draft)
        session.flush()
        review = ChapterReview(
            draft_id=draft.id,
            verdict="pass",
            issues_json="[]",
            review_meta_json='{"verdict":"pass"}',
        )
        session.add(review)
        session.flush()
        candidate = CandidateDraftRepository(session).create_reviewed_version(
            project_id=project.id,
            chapter_plan=chapter,
            draft=draft,
            review=review,
            writer_output=output,
            plan_revision="arc-v1:chapter-1",
            policy_version=1,
        )
        approved = ApprovedGraphDeltaSet(
            project_id=project.id,
            chapter_number=1,
            graph_deltas=[
                GraphDelta(
                    id="prepared-delta-1",
                    project_id=project.id,
                    chapter_number=1,
                    summary="prepared only",
                )
            ],
            approved_by=["book_state_review"],
        )
        entity_plan = EntityAdmissionPlan(
            project_id=project.id,
            chapter_number=1,
            candidate_fingerprint=writer_output_admission_fingerprint(output),
        )

        outcome = CanonPreparationService().prepare_from_approved(
            session=session,
            candidate_id=candidate.id,
            approved_book_state_changes=approved,
            entity_admission_plan=entity_plan,
            acceptance_mode="normal",
            repair_attempt_count=0,
            residual_review_issues=[],
            canon_risk_level="low",
        )

        assert outcome.plan is not None
        assert outcome.plan.candidate_id == candidate.id
        assert outcome.plan.expected_previous_accepted_chapter == 0
        assert outcome.plan.expected_book_state_chapter == 0
        assert candidate.status == "ready_for_canon"
        assert candidate.idempotency_key == outcome.plan.idempotency_key
        assert session.scalar(select(func.count(GraphDeltaRow.id))) == 0
        assert session.scalar(select(func.count(Entity.id))) == 0
        assert session.scalar(select(func.count(EntityAlias.id))) == 0
        assert session.scalar(select(func.count(OutboxEvent.id))) == 0
        assert chapter.status == "planned"


def test_prepare_uses_pretransaction_collaborators_without_compiling() -> None:
    engine = get_engine(postgres_test_url("canon-plan-collaborators"))
    init_db(engine)
    Session = get_session_factory(engine)
    calls: list[str] = []
    recorded_events: list[dict[str, object]] = []

    def record_decision_event(**payload):
        recorded_events.append(payload)
        return SimpleNamespace(id=f"event-{len(recorded_events)}")

    with Session.begin() as session:
        updater = StateUpdater(session)
        project = updater.create_project(
            title="Canon prepare collaborators",
            premise="质量与 extraction 都在事务外",
            genre="悬疑",
            runtime_policy=RuntimePolicy.for_profile("standard"),
        )
        arc = updater.create_arc_plan(project.id, "第一弧")
        chapter = updater.create_chapter_plan(
            project_id=project.id,
            arc_plan_id=arc.id,
            chapter_number=1,
            title="第一章",
            one_line="准备协作",
            goals=["不 compile"],
        )
        output_without_plan = WriterOutput(
            project_id=project.id,
            chapter_number=1,
            title="第一章",
            body="协作者准备正文",
            char_count=7,
            end_of_chapter_summary="准备完成",
        )
        entity_plan = EntityAdmissionPlan(
            project_id=project.id,
            chapter_number=1,
            candidate_fingerprint=writer_output_admission_fingerprint(
                output_without_plan
            ),
        )
        output = output_without_plan.model_copy(
            update={
                "generation_meta": {
                    "entity_admission_plan": entity_plan.model_dump(mode="json")
                }
            }
        )
        draft = ChapterDraft(
            chapter_plan_id=chapter.id,
            version=1,
            body_text=output.body,
            summary=output.end_of_chapter_summary,
            char_count=output.char_count,
        )
        session.add(draft)
        session.flush()
        review = ChapterReview(
            draft_id=draft.id,
            verdict="pass",
            issues_json="[]",
            review_meta_json='{"verdict":"pass"}',
        )
        session.add(review)
        session.flush()
        candidate = CandidateDraftRepository(session).create_reviewed_version(
            project_id=project.id,
            chapter_plan=chapter,
            draft=draft,
            review=review,
            writer_output=output,
            plan_revision="arc-v1:chapter-1",
            policy_version=1,
        )
        approved = ApprovedGraphDeltaSet(
            project_id=project.id,
            chapter_number=1,
            graph_deltas=[],
            approved_by=["book_state_review"],
        )

        def quality_evaluator(**_kwargs):
            calls.append("quality")
            return CanonQualityGateOutcome()

        class PreparedBookState:
            def prepare(self, **_kwargs):
                calls.append("book_state")
                return BookStatePreparationOutcome(
                    approved_changes=approved,
                )

        outcome = CanonPreparationService(
            quality_evaluator=quality_evaluator,
            book_state_preparer=PreparedBookState(),
        ).prepare(
            context=CanonPreparationContext(
                policy=RuntimePolicy.for_profile("standard"),
                llm_client=object(),  # type: ignore[arg-type]
                artifact_store=object(),  # type: ignore[arg-type]
                _record_decision_event=record_decision_event,  # type: ignore[arg-type]
                _record_rule_decision_event=lambda **_kwargs: None,  # type: ignore[arg-type]
            ),
            session=session,
            repo=None,
            updater=updater,
            candidate_id=candidate.id,
            project_id=project.id,
            chapter_number=1,
            writer_output=output,
            verdict=ReviewVerdict(verdict="pass"),
            acceptance_mode="normal",
            repair_attempt_count=0,
            residual_review_issues=[],
            canon_risk_level="low",
        )

        assert calls == ["quality", "book_state"]
        assert outcome.plan is not None
        assert session.scalar(select(func.count(GraphDeltaRow.id))) == 0
        assert (
            recorded_events[0]["event_type"] == DecisionEventType.CANON_COMMIT_STARTED
        )
        gate_outcome = parse_gate_outcome(recorded_events[0]["payload"])
        assert gate_outcome is not None
        assert gate_outcome.gate_id == "canon_quality"
        assert gate_outcome.candidate_id == candidate.id
        assert gate_outcome.policy_version == 1
        assert gate_outcome.evaluated is False
