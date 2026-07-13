from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from forwin.candidate_drafts import (
    CandidateDraftRepository,
    candidate_plan_revision,
)
from forwin.canon.admission import CanonAdmissionService
from forwin.canon.preparation import CanonPreparationService
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.book_state import (
    GraphDeltaRow,
    MapSnapshotRow,
    WorldNodeRow,
    WorldSnapshotRow,
)
from forwin.models.canon import CanonCommitRecord
from forwin.models.draft import CandidateDraftRecord, ChapterDraft, ChapterReview
from forwin.models.entity import Entity, EntityAlias
from forwin.models.audit import DecisionEvent
from forwin.models.narrative_obligation import NarrativeObligationRow
from forwin.models.outbox import OutboxEvent
from forwin.models.project import ChapterPlan
from forwin.naming import (
    EntityAdmissionDecision,
    EntityAdmissionPlan,
    writer_output_admission_fingerprint,
)
import forwin.outbox.store as outbox_store
from forwin.protocol.book_state import ApprovedGraphDeltaSet, GraphDelta, NodePatch
from forwin.protocol.writer import WriterOutput
from forwin.runtime.policy import RuntimePolicy
from forwin.state.updater import StateUpdater
from tests.postgres import postgres_test_url


@dataclass(frozen=True)
class PreparedCanon:
    Session: sessionmaker[Session]
    plan: object
    project_id: str
    chapter_plan_id: str
    candidate_id: str
    obligation_id: str


@pytest.fixture
def prepared_canon() -> PreparedCanon:
    engine = get_engine(postgres_test_url("canon-atomic-transaction"))
    init_db(engine)
    SessionFactory = get_session_factory(engine)

    with SessionFactory.begin() as session:
        updater = StateUpdater(session)
        project = updater.create_project(
            title="Atomic Canon",
            premise="All accepted state commits together.",
            genre="thriller",
            runtime_policy=RuntimePolicy.for_profile("standard"),
        )
        arc = updater.create_arc_plan(project.id, "Arc one")
        chapter = updater.create_chapter_plan(
            project_id=project.id,
            arc_plan_id=arc.id,
            chapter_number=1,
            title="Chapter one",
            one_line="Commit one candidate",
            goals=["Prove atomicity"],
        )
        output = WriterOutput(
            project_id=project.id,
            chapter_number=1,
            title="Chapter one",
            body="Shen Linchuan enters the archive.",
            char_count=33,
            end_of_chapter_summary="A new character enters canon.",
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
            plan_revision=candidate_plan_revision(chapter),
            policy_version=1,
        )
        approved = ApprovedGraphDeltaSet(
            project_id=project.id,
            chapter_number=1,
            graph_deltas=[
                GraphDelta(
                    id=f"delta-{candidate.id}",
                    project_id=project.id,
                    chapter_number=1,
                    summary="Register Shen Linchuan",
                    node_patches=[
                        NodePatch(
                            node_id=f"character-{candidate.id}",
                            node_type="character",
                            op="create",
                            new_value={
                                "id": f"character-{candidate.id}",
                                "project_id": project.id,
                                "node_type": "character",
                                "name": "Shen Linchuan",
                                "description": "An archivist entering the story.",
                            },
                        )
                    ],
                )
            ],
            approved_by=["book_state_review"],
            review_verdict_id=review.id,
        )
        entity_plan = EntityAdmissionPlan(
            project_id=project.id,
            chapter_number=1,
            candidate_fingerprint=writer_output_admission_fingerprint(output),
            decisions=[
                EntityAdmissionDecision(
                    mention_name="Shen Linchuan",
                    action="register_character",
                    entity_id=f"character-{candidate.id}",
                    canonical_name="Shen Linchuan",
                    aliases=["Archivist Shen"],
                )
            ],
        )
        obligation = NarrativeObligationRow(
            project_id=project.id,
            origin_chapter_number=1,
            origin_draft_id=draft.id,
            origin_review_id=review.id,
            obligation_type="future_payoff",
            status="planned",
            summary="Explain why the archive was sealed.",
        )
        session.add(obligation)
        session.flush()
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
        prepared = PreparedCanon(
            Session=SessionFactory,
            plan=outcome.plan,
            project_id=project.id,
            chapter_plan_id=chapter.id,
            candidate_id=candidate.id,
            obligation_id=obligation.id,
        )

    yield prepared
    engine.dispose()


def _authoritative_snapshot(prepared: PreparedCanon) -> dict[str, object]:
    with prepared.Session() as session:
        chapter = session.get(ChapterPlan, prepared.chapter_plan_id)
        obligation = session.get(NarrativeObligationRow, prepared.obligation_id)
        return {
            "graph_deltas": session.scalar(select(func.count(GraphDeltaRow.id))),
            "world_snapshots": session.scalar(select(func.count(WorldSnapshotRow.id))),
            "map_snapshots": session.scalar(select(func.count(MapSnapshotRow.id))),
            "world_nodes": session.scalar(select(func.count(WorldNodeRow.id))),
            "entities": session.scalar(select(func.count(Entity.id))),
            "aliases": session.scalar(select(func.count(EntityAlias.id))),
            "audit_events": session.scalar(select(func.count(DecisionEvent.id))),
            "outbox_events": session.scalar(select(func.count(OutboxEvent.id))),
            "canon_commits": session.scalar(select(func.count(CanonCommitRecord.id))),
            "chapter_status": chapter.status if chapter else "missing",
            "obligation_status": obligation.status if obligation else "missing",
        }


def _fail_at(expected_stage: str) -> Callable[[str], None]:
    def fail(stage: str) -> None:
        if stage == expected_stage:
            raise RuntimeError(f"injected failure after {stage}")

    return fail


@pytest.mark.parametrize(
    "failure_stage",
    ["book_state", "entity", "obligation", "chapter", "outbox"],
)
def test_canon_failure_rolls_back_every_authoritative_write(
    failure_stage: str,
    prepared_canon: PreparedCanon,
) -> None:
    before = _authoritative_snapshot(prepared_canon)

    outcome = CanonAdmissionService(session_factory=prepared_canon.Session).commit_plan(
        prepared_canon.plan,
        failure_injector=_fail_at(failure_stage),
    )

    assert outcome.blocked is True
    assert outcome.block_kind == "canon_write_failed"
    assert _authoritative_snapshot(prepared_canon) == before
    with prepared_canon.Session() as session:
        candidate = session.get(CandidateDraftRecord, prepared_canon.candidate_id)
        assert candidate is not None
        assert candidate.status == "failed"
        assert failure_stage in candidate.failure_reason


def test_atomic_commit_writes_all_authoritative_state_once(
    prepared_canon: PreparedCanon,
) -> None:
    outcome = CanonAdmissionService(session_factory=prepared_canon.Session).commit_plan(
        prepared_canon.plan
    )

    assert outcome.blocked is False
    assert outcome.commit_id
    assert outcome.idempotent is False
    assert outcome.compile_result is not None
    assert outcome.compile_result.committed is True
    snapshot = _authoritative_snapshot(prepared_canon)
    assert snapshot["graph_deltas"] == 1
    assert snapshot["world_snapshots"] == 1
    assert snapshot["map_snapshots"] == 1
    assert snapshot["world_nodes"] == 1
    assert snapshot["entities"] == 1
    assert snapshot["aliases"] == 1
    assert int(snapshot["audit_events"]) >= 3
    assert snapshot["outbox_events"] == 2
    assert snapshot["canon_commits"] == 1
    assert snapshot["chapter_status"] == "accepted"
    assert snapshot["obligation_status"] == "active"
    with prepared_canon.Session() as session:
        candidate = session.get(CandidateDraftRecord, prepared_canon.candidate_id)
        assert candidate is not None
        assert candidate.status == "accepted"
        assert candidate.canon_commit_id == outcome.commit_id


def test_same_idempotency_key_returns_prior_commit(
    prepared_canon: PreparedCanon,
) -> None:
    service = CanonAdmissionService(session_factory=prepared_canon.Session)

    first = service.commit_plan(prepared_canon.plan)
    after_first = _authoritative_snapshot(prepared_canon)
    second = service.commit_plan(prepared_canon.plan)

    assert second.blocked is False
    assert second.commit_id == first.commit_id
    assert second.idempotent is True
    assert _authoritative_snapshot(prepared_canon) == after_first


def test_empty_graph_delta_cannot_commit_second_candidate_for_accepted_chapter(
    prepared_canon: PreparedCanon,
) -> None:
    empty_changes = ApprovedGraphDeltaSet(
        project_id=prepared_canon.project_id,
        chapter_number=1,
        graph_deltas=[],
        approved_by=["book_state_review"],
    )
    first_plan = prepared_canon.plan.model_copy(
        update={
            "approved_book_state_changes": empty_changes,
            "entity_admission_plan": EntityAdmissionPlan(
                project_id=prepared_canon.project_id,
                chapter_number=1,
                candidate_fingerprint=(
                    prepared_canon.plan.entity_admission_plan.candidate_fingerprint
                ),
            ),
        }
    )
    with prepared_canon.Session.begin() as session:
        first_candidate = session.get(
            CandidateDraftRecord,
            prepared_canon.candidate_id,
        )
        assert first_candidate is not None
        first_candidate.canon_commit_plan_json = first_plan.model_dump_json()

    first = CanonAdmissionService(
        session_factory=prepared_canon.Session
    ).commit_plan(first_plan)
    assert first.blocked is False

    with prepared_canon.Session.begin() as session:
        chapter = session.get(ChapterPlan, prepared_canon.chapter_plan_id)
        assert chapter is not None and chapter.status == "accepted"
        output = WriterOutput(
            project_id=prepared_canon.project_id,
            chapter_number=1,
            title="Chapter one alternate",
            body="An alternate draft reaches the same accepted chapter.",
            char_count=53,
            end_of_chapter_summary="This candidate must remain non-Canon.",
        )
        draft = ChapterDraft(
            chapter_plan_id=chapter.id,
            version=2,
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
        second_candidate = CandidateDraftRepository(session).create_reviewed_version(
            project_id=prepared_canon.project_id,
            chapter_plan=chapter,
            draft=draft,
            review=review,
            writer_output=output,
            plan_revision=candidate_plan_revision(chapter),
            policy_version=1,
        )
        prepared = CanonPreparationService().prepare_from_approved(
            session=session,
            candidate_id=second_candidate.id,
            approved_book_state_changes=empty_changes,
            entity_admission_plan=EntityAdmissionPlan(
                project_id=prepared_canon.project_id,
                chapter_number=1,
                candidate_fingerprint=writer_output_admission_fingerprint(output),
            ),
            acceptance_mode="normal",
            repair_attempt_count=0,
            residual_review_issues=[],
            canon_risk_level="low",
        )
        assert prepared.plan is not None
        second_plan = prepared.plan

    before_second = _authoritative_snapshot(prepared_canon)
    second = CanonAdmissionService(
        session_factory=prepared_canon.Session
    ).commit_plan(second_plan)

    assert second.blocked is True
    assert second.stale is True
    assert second.block_kind == "stale_canon_plan"
    assert "already accepted" in second.failure_reason
    assert _authoritative_snapshot(prepared_canon) == before_second


def test_stale_plan_rolls_back_and_returns_candidate_to_ready(
    prepared_canon: PreparedCanon,
) -> None:
    with prepared_canon.Session.begin() as session:
        chapter = session.get(ChapterPlan, prepared_canon.chapter_plan_id)
        assert chapter is not None
        chapter.title = "Changed after review"
        session.add(chapter)
    before = _authoritative_snapshot(prepared_canon)

    outcome = CanonAdmissionService(session_factory=prepared_canon.Session).commit_plan(
        prepared_canon.plan
    )

    assert outcome.blocked is True
    assert outcome.block_kind == "stale_canon_plan"
    assert outcome.stale is True
    assert _authoritative_snapshot(prepared_canon) == before
    with prepared_canon.Session() as session:
        candidate = session.get(CandidateDraftRecord, prepared_canon.candidate_id)
        assert candidate is not None
        assert candidate.status == "ready_for_canon"


@pytest.mark.parametrize("failure_kind", ["row", "serialization", "flush"])
def test_outbox_internal_failure_rolls_back_every_authoritative_write(
    failure_kind: str,
    prepared_canon: PreparedCanon,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _authoritative_snapshot(prepared_canon)
    message = f"injected outbox {failure_kind} failure"

    if failure_kind == "row":

        def fail_row_construction(**_kwargs):
            raise RuntimeError(message)

        monkeypatch.setattr(outbox_store, "OutboxEvent", fail_row_construction)
    elif failure_kind == "serialization":

        def fail_serialization(*_args, **_kwargs):
            raise TypeError(message)

        monkeypatch.setattr(
            outbox_store,
            "json",
            SimpleNamespace(dumps=fail_serialization),
        )
    else:
        session_type = prepared_canon.Session.class_
        original_flush = session_type.flush

        def fail_outbox_flush(session, *args, **kwargs):
            if any(isinstance(item, OutboxEvent) for item in session.new):
                raise RuntimeError(message)
            return original_flush(session, *args, **kwargs)

        monkeypatch.setattr(session_type, "flush", fail_outbox_flush)

    outcome = CanonAdmissionService(session_factory=prepared_canon.Session).commit_plan(
        prepared_canon.plan
    )

    assert outcome.blocked is True
    assert outcome.block_kind == "canon_write_failed"
    assert message in outcome.failure_reason
    assert _authoritative_snapshot(prepared_canon) == before
    with prepared_canon.Session() as session:
        candidate = session.get(CandidateDraftRecord, prepared_canon.candidate_id)
        assert candidate is not None
        assert candidate.status == "failed"
        assert message in candidate.failure_reason


def test_entity_admission_fingerprint_change_after_preparation_is_stale(
    prepared_canon: PreparedCanon,
) -> None:
    with prepared_canon.Session.begin() as session:
        candidate = session.get(CandidateDraftRecord, prepared_canon.candidate_id)
        assert candidate is not None
        metadata = json.loads(candidate.metadata_json)
        assert metadata["writer_output_admission_fingerprint"]
        metadata["writer_output_admission_fingerprint"] = "changed-after-prepare"
        candidate.metadata_json = json.dumps(metadata, sort_keys=True)
        session.add(candidate)
    before = _authoritative_snapshot(prepared_canon)

    outcome = CanonAdmissionService(session_factory=prepared_canon.Session).commit_plan(
        prepared_canon.plan
    )

    assert outcome.blocked is True
    assert outcome.stale is True
    assert outcome.block_kind == "stale_canon_plan"
    assert "entity admission candidate fingerprint changed" in outcome.failure_reason
    assert _authoritative_snapshot(prepared_canon) == before


def test_alias_conflict_created_after_entity_plan_preparation_rolls_back(
    prepared_canon: PreparedCanon,
) -> None:
    with prepared_canon.Session.begin() as session:
        owner = Entity(
            id="existing-alias-owner",
            project_id=prepared_canon.project_id,
            kind="character",
            name="Existing Character",
            aliases_json='["Archivist Shen"]',
        )
        session.add(owner)
        session.flush()
        session.add(
            EntityAlias(
                id="existing-conflicting-alias",
                entity_id=owner.id,
                project_id=prepared_canon.project_id,
                alias="Archivist Shen",
            )
        )
    before = _authoritative_snapshot(prepared_canon)

    outcome = CanonAdmissionService(session_factory=prepared_canon.Session).commit_plan(
        prepared_canon.plan
    )

    assert outcome.blocked is True
    assert outcome.block_kind == "stale_canon_plan"
    assert outcome.stale is True
    assert "belongs to another entity" in outcome.failure_reason
    assert _authoritative_snapshot(prepared_canon) == before
    with prepared_canon.Session() as session:
        candidate = session.get(CandidateDraftRecord, prepared_canon.candidate_id)
        assert candidate is not None
        assert candidate.status == "ready_for_canon"
