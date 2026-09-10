from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

import forwin.outbox.store as outbox_store
from forwin.api_schema import ChapterReviewApproveRequest, ChapterReviewRetryRequest
from forwin.audit.events import DecisionEventType
from forwin.book_state.projection import BookStateProjection
from forwin.book_state.repository import BookStateRepository
from forwin.candidate_drafts import (
    CandidateDraftRepository,
    candidate_plan_revision,
)
from forwin.canon.admission import CanonAdmissionService
from forwin.canon.outbox_events import (
    CANON_PHASE3_REQUESTED,
    CANON_PROJECTION_REQUESTED,
    CANON_PUBLISHER_REQUESTED,
    canon_event_id,
)
from forwin.canon.plan import CanonCommitPlan
from forwin.canon.preparation import (
    BookStatePreparationOutcome,
    CanonPreparationService,
)
from forwin.config import InfrastructureConfig
from forwin.maintenance.events import ORDER_CONTROLS_KEY, POST_CANON_STEP_NAMES
from forwin.models.audit import DecisionEvent
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
from forwin.models.knowledge import KnowledgeEditProposalRow
from forwin.models.maintenance import PostCanonMaintenanceRun
from forwin.models.narrative_obligation import NarrativeObligationRow
from forwin.models.outbox import OutboxEvent
from forwin.models.project import ChapterPlan, Project
from forwin.models.subworld import SubWorldRosterItem
from forwin.naming import (
    EntityAdmissionDecision,
    EntityAdmissionPlan,
    writer_output_admission_fingerprint,
)
from forwin.protocol.book_state import (
    ApprovedGraphDeltaSet,
    CognitionPatch,
    GraphDelta,
    NodePatch,
    WorldNode,
)
from forwin.protocol.writer import WriterOutput
from forwin.runtime.container import RuntimeContainer
from forwin.runtime.policy import RuntimePolicy
from forwin.state.updater import StateUpdater
from tests.http_runtime_harness import HttpRuntimeHarness
from tests.postgres import postgres_test_url


@dataclass(frozen=True)
class PreparedCanon:
    Session: sessionmaker[Session]
    plan: CanonCommitPlan
    project_id: str
    chapter_plan_id: str
    candidate_id: str
    obligation_id: str
    roster_item_id: str


@dataclass(frozen=True)
class HistoricalRewriteScenario:
    Session: sessionmaker[Session]
    project_id: str
    chapter_two_id: str
    old_commit_id: str
    successor_candidate_id: str
    successor_draft_id: str
    rewritten_plan: CanonCommitPlan


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
            automation_json=json.dumps(
                {
                    "primary_publish_platform": "qidian",
                    "publish_bindings": [
                        {
                            "platform": "qidian",
                            "book_name": "Prepared Publisher Snapshot",
                            "upload_url": "https://write.example/prepared",
                        }
                    ]
                }
            ),
        )
        arc = updater.create_arc_plan(project.id, "Arc one")
        subworld = updater.create_subworld(
            project_id=project.id,
            origin_arc_id=arc.id,
            parent_subworld_id=None,
            name="Archive district",
            purpose="Introduce the archivist",
            scope="arc_local",
        )
        roster_item = updater.create_roster_item(
            project_id=project.id,
            subworld_id=subworld.id,
            entity_id=None,
            display_name="Shen Linchuan",
            role_hint="Archivist",
            is_core=True,
            status="activated_named",
            activation_chapter=1,
            metadata={
                "pending_entity_admission": True,
                "entry_target_chapter": 1,
            },
        )
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
            roster_item_id=roster_item.id,
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


def _rebuild_plan(
    plan: CanonCommitPlan,
    *,
    approved_book_state_changes: ApprovedGraphDeltaSet,
    entity_admission_plan: EntityAdmissionPlan,
) -> CanonCommitPlan:
    return CanonCommitPlan.build(
        project_id=plan.project_id,
        chapter_number=plan.chapter_number,
        candidate_id=plan.candidate_id,
        candidate_body_hash=plan.candidate_body_hash,
        plan_revision=plan.plan_revision,
        policy_version=plan.policy_version,
        expected_previous_accepted_chapter=plan.expected_previous_accepted_chapter,
        expected_book_state_chapter=plan.expected_book_state_chapter,
        approved_book_state_changes=approved_book_state_changes,
        entity_admission_plan=entity_admission_plan,
        acceptance_mode=plan.acceptance_mode,
        repair_attempt_count=plan.repair_attempt_count,
        residual_review_issues=plan.residual_review_issues,
        canon_risk_level=plan.canon_risk_level,
        chapter_title=plan.chapter_title,
        publisher_bindings=plan.publisher_bindings,
        audit_events=plan.audit_events,
        schema_version=plan.schema_version,
    )


def _prepare_chapter_candidate(
    session: Session,
    *,
    project_id: str,
    chapter: ChapterPlan,
    version: int,
    body: str,
    summary: str,
    delta_id: str,
    delta_summary: str,
    node_id: str,
    graph_deltas: list[GraphDelta] | None = None,
) -> CanonCommitPlan:
    output = WriterOutput(
        project_id=project_id,
        chapter_number=chapter.chapter_number,
        title=chapter.title,
        body=body,
        char_count=len(body),
        end_of_chapter_summary=summary,
    )
    draft = ChapterDraft(
        chapter_plan_id=chapter.id,
        version=version,
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
        project_id=project_id,
        chapter_plan=chapter,
        draft=draft,
        review=review,
        writer_output=output,
        plan_revision=candidate_plan_revision(chapter),
        policy_version=1,
    )
    approved = ApprovedGraphDeltaSet(
        project_id=project_id,
        chapter_number=chapter.chapter_number,
        graph_deltas=graph_deltas if graph_deltas is not None else [
            GraphDelta(
                id=delta_id,
                project_id=project_id,
                chapter_number=chapter.chapter_number,
                summary=delta_summary,
                node_patches=[
                    NodePatch(
                        node_id=node_id,
                        node_type="event",
                        op="create",
                        new_value={
                            "id": node_id,
                            "project_id": project_id,
                            "node_type": "event",
                            "name": delta_summary,
                            "description": delta_summary,
                            "state": {"event": delta_summary},
                        },
                    )
                ],
            )
        ],
        approved_by=["book_state_review"],
        review_verdict_id=review.id,
    )
    prepared = CanonPreparationService().prepare_from_approved(
        session=session,
        candidate_id=candidate.id,
        approved_book_state_changes=approved,
        entity_admission_plan=EntityAdmissionPlan(
            project_id=project_id,
            chapter_number=chapter.chapter_number,
            candidate_fingerprint=writer_output_admission_fingerprint(output),
        ),
        acceptance_mode="normal",
        repair_attempt_count=0,
        residual_review_issues=[],
        canon_risk_level="low",
    )
    assert prepared.plan is not None
    return prepared.plan


def _complete_post_canon_barrier(
    session: Session,
    *,
    commit_id: str,
    project_id: str,
    chapter_number: int,
    candidate_id: str,
) -> None:
    for step_name in POST_CANON_STEP_NAMES:
        result = {}
        if step_name == POST_CANON_STEP_NAMES[-1]:
            result = {
                ORDER_CONTROLS_KEY: {
                    "status": "succeeded",
                    "result": {"blocking_reasons": []},
                }
            }
        session.add(
            PostCanonMaintenanceRun(
                id=f"post-canon-{commit_id}-{step_name}",
                canon_commit_id=commit_id,
                project_id=project_id,
                chapter_number=chapter_number,
                candidate_id=candidate_id,
                step_name=step_name,
                idempotency_key=f"post-canon-key-{commit_id}-{step_name}",
                status="succeeded",
                result_json=json.dumps(result, sort_keys=True),
            )
        )


def _rewrite_marker(
    session: Session, project_id: str, chapter_number: int
) -> dict[str, object] | None:
    event = session.scalar(
        select(DecisionEvent)
        .where(
            DecisionEvent.project_id == project_id,
            DecisionEvent.chapter_number == chapter_number,
            DecisionEvent.event_family == "audit_action",
            DecisionEvent.event_type == DecisionEventType.RETRY_ATTEMPT,
            DecisionEvent.actor_type == "api",
        )
        .order_by(DecisionEvent.created_at.desc(), DecisionEvent.id.desc())
    )
    if event is None:
        return None
    payload = json.loads(event.payload_json or "{}")
    return payload if "previous_commit_id" in payload else None


@pytest.fixture
def historical_rewrite_scenario(
    request: pytest.FixtureRequest,
) -> HistoricalRewriteScenario:
    ordered_successor = getattr(request, "param", None) == "manifest_order"
    missing_metadata = getattr(request, "param", None) == "missing_old_metadata"
    engine = get_engine(postgres_test_url("historical-canon-rewrite"))
    init_db(engine)
    SessionFactory = get_session_factory(engine)
    with SessionFactory.begin() as session:
        updater = StateUpdater(session)
        project = updater.create_project(
            title="Historical Canon",
            premise="A corrected chapter must replace its old state contribution.",
            genre="thriller",
            runtime_policy=RuntimePolicy.for_profile("standard"),
            automation_json='{"primary_publish_platform":"qidian"}',
        )
        project.target_total_chapters = 20
        arc = updater.create_arc_plan(project.id, "Rewrite arc", chapter_start=2)
        chapter_one = updater.create_chapter_plan(
            project.id, arc.id, 1, "Chapter one", "Base event", ["Commit chapter one"]
        )
        chapter_two = updater.create_chapter_plan(
            project.id,
            arc.id,
            2,
            "Chapter two",
            "Original event",
            ["Commit chapter two"],
        )
        chapter_three = updater.create_chapter_plan(
            project.id,
            arc.id,
            3,
            "Chapter three",
            "Later consequence",
            ["Commit chapter three"],
        )
        metadata_checkpoint = None
        if missing_metadata:
            repo = BookStateRepository(session)
            repo.create_world_node(
                WorldNode(
                    id="site_state_node-ninth-workshop",
                    project_id=project.id,
                    node_type="site_state",
                    metadata={
                        "source": "map_generation",
                        "writer_state": {"access": "open"},
                    },
                )
            )
            metadata_checkpoint = [
                GraphDelta(
                    id="delta-base-event",
                    project_id=project.id,
                    chapter_number=1,
                    node_patches=[
                        NodePatch(
                            node_id="site_state_node-ninth-workshop",
                            node_type="site_state",
                            op="set",
                            field_path="metadata",
                            new_value=repo.get_world_node(
                                "site_state_node-ninth-workshop"
                            ).metadata,
                        )
                    ],
                )
            ]
        base_plan = _prepare_chapter_candidate(
            session,
            project_id=project.id,
            chapter=chapter_one,
            version=1,
            body="The base event enters Canon.",
            summary="The base event becomes Canon.",
            delta_id="delta-base-event",
            delta_summary="base event",
            node_id="event-order" if ordered_successor else "event-base",
            graph_deltas=metadata_checkpoint,
        )

    base_outcome = CanonAdmissionService(session_factory=SessionFactory).commit_plan(
        base_plan
    )
    assert base_outcome.blocked is False
    with SessionFactory.begin() as session:
        _complete_post_canon_barrier(
            session,
            commit_id=base_outcome.commit_id,
            project_id=project.id,
            chapter_number=1,
            candidate_id=base_plan.candidate_id,
        )
        chapter_two = session.get(ChapterPlan, chapter_two.id)
        assert chapter_two is not None
        original_plan = _prepare_chapter_candidate(
            session,
            project_id=project.id,
            chapter=chapter_two,
            version=1,
            body="The obsolete braking event enters Canon.",
            summary="The obsolete braking event becomes Canon.",
            delta_id="delta-obsolete-braking",
            delta_summary="obsolete braking event",
            node_id="event-obsolete-braking",
            graph_deltas=[
                GraphDelta(
                    id="delta-obsolete-braking",
                    project_id=project.id,
                    chapter_number=2,
                    node_patches=[
                        NodePatch(
                            node_id="site_state_node-ninth-workshop",
                            node_type="site_state",
                            op="set",
                            field_path="metadata.writer_state.controlled_by",
                            new_value="obsolete-owner",
                        )
                    ],
                )
            ]
            if missing_metadata
            else None,
        )

    old_outcome = CanonAdmissionService(session_factory=SessionFactory).commit_plan(
        original_plan
    )
    assert old_outcome.blocked is False
    with SessionFactory.begin() as session:
        _complete_post_canon_barrier(
            session,
            commit_id=old_outcome.commit_id,
            project_id=project.id,
            chapter_number=2,
            candidate_id=original_plan.candidate_id,
        )
        chapter_three = session.get(ChapterPlan, chapter_three.id)
        assert chapter_three is not None
        successor_plan = _prepare_chapter_candidate(
            session,
            project_id=project.id,
            chapter=chapter_three,
            version=1,
            body="The later accepted consequence follows the first event.",
            summary="The consequence is accepted after chapter two.",
            delta_id="delta-later-consequence",
            delta_summary="later accepted consequence",
            node_id="event-later-consequence",
            graph_deltas=[
                GraphDelta(
                    id=delta_id,
                    project_id=project.id,
                    chapter_number=3,
                    node_patches=[
                        NodePatch(
                            node_id="event-order",
                            node_type="event",
                            op="set",
                            field_path="state.phase",
                            new_value=phase,
                        )
                    ],
                )
                for delta_id, phase in (
                    ("z-first", "intermediate"),
                    ("a-second", "final"),
                )
            ]
            if ordered_successor
            else [
                GraphDelta(
                    id="delta-later-consequence",
                    project_id=project.id,
                    chapter_number=3,
                    node_patches=[
                        NodePatch(
                            node_id="site_state_node-ninth-workshop",
                            node_type="site_state",
                            op="set",
                            field_path="metadata.writer_state.usage",
                            new_value="retained-successor",
                        )
                    ],
                )
            ]
            if missing_metadata
            else None,
        )
        successor_candidate = session.get(
            CandidateDraftRecord, successor_plan.candidate_id
        )
        assert successor_candidate is not None
        successor_draft_id = successor_candidate.candidate_draft_id
    successor_outcome = CanonAdmissionService(
        session_factory=SessionFactory
    ).commit_plan(successor_plan)
    assert successor_outcome.blocked is False

    api = HttpRuntimeHarness(
        session_factory=SessionFactory,
        engine=engine,
        config=InfrastructureConfig(
            database_url=postgres_test_url("historical-canon-rewrite"),
            minimax_api_key="saved-key",
            minimax_base_url="https://api.minimaxi.com/v1",
            minimax_model="MiniMax-M2.7",
        ),
    )
    retry = api.retry_chapter_review(
        project.id,
        2,
        ChapterReviewRetryRequest(
            reason="correct historical drift",
            allow_accepted=True,
        ),
    )
    assert retry.ok is True

    with SessionFactory.begin() as session:
        chapter_two = session.get(ChapterPlan, chapter_two.id)
        assert chapter_two is not None
        rewritten_plan = _prepare_chapter_candidate(
            session,
            project_id=project.id,
            chapter=chapter_two,
            version=2,
            body="The corrected custody-only event replaces the old event.",
            summary="Only the custody event is Canon.",
            delta_id="delta-corrected-custody",
            delta_summary="corrected custody-only event",
            node_id="event-corrected-custody",
        )

    yield HistoricalRewriteScenario(
        Session=SessionFactory,
        project_id=project.id,
        chapter_two_id=chapter_two.id,
        old_commit_id=old_outcome.commit_id,
        successor_candidate_id=successor_plan.candidate_id,
        successor_draft_id=successor_draft_id,
        rewritten_plan=rewritten_plan,
    )
    engine.dispose()


def _active_delta_summaries(
    SessionFactory: sessionmaker[Session], project_id: str, chapter_number: int
) -> list[str]:
    with SessionFactory() as session:
        return list(
            session.scalars(
                select(GraphDeltaRow.summary)
                .where(
                    GraphDeltaRow.project_id == project_id,
                    GraphDeltaRow.chapter_number == chapter_number,
                )
                .order_by(GraphDeltaRow.created_at, GraphDeltaRow.id)
            )
        )


def _world_snapshot(
    SessionFactory: sessionmaker[Session], project_id: str, chapter_number: int
) -> str:
    with SessionFactory() as session:
        snapshot = session.scalar(
            select(WorldSnapshotRow)
            .where(
                WorldSnapshotRow.project_id == project_id,
                WorldSnapshotRow.as_of_chapter == chapter_number,
            )
            .order_by(WorldSnapshotRow.built_at.desc(), WorldSnapshotRow.id.desc())
        )
        assert snapshot is not None
        return snapshot.world_node_state_index_json


def _review_api(scenario, monkeypatch, tmp_path):
    config = InfrastructureConfig(
        database_url=scenario.Session.kw["bind"].url.render_as_string(
            hide_password=False
        ),
        artifact_root=str(tmp_path),
        qdrant_url=":memory:",
        embedding_backend="hash",
        minimax_api_key="",
    )
    pipeline = RuntimeContainer.from_config(
        config, policy=RuntimePolicy.for_profile("standard"), role="generation_worker"
    ).build_chapter_pipeline()
    # External model boundaries are fixed; artifact loading, acceptance, preparation,
    # entity verification, transactions, and projection replay remain real.
    with scenario.Session() as session:
        candidate = session.get(
            CandidateDraftRecord, scenario.rewritten_plan.candidate_id
        )
        draft = session.get(ChapterDraft, candidate.candidate_draft_id)
        chapter = session.get(ChapterPlan, candidate.chapter_plan_id)
        output = WriterOutput(
            project_id=scenario.project_id,
            chapter_number=chapter.chapter_number,
            title=chapter.title,
            body=draft.body_text,
            char_count=draft.char_count,
            end_of_chapter_summary=draft.summary,
            generation_meta={
                "entity_admission_plan": scenario.rewritten_plan.entity_admission_plan.model_dump(
                    mode="json"
                )
            },
        )
    paths = pipeline.artifact_store.save_writer_output(
        project_id=scenario.project_id, chapter_number=2, writer_output=output
    )
    with scenario.Session.begin() as session:
        candidate = session.get(
            CandidateDraftRecord, scenario.rewritten_plan.candidate_id
        )
        draft = session.get(ChapterDraft, candidate.candidate_draft_id)
        draft.llm_raw_response = paths["meta_path"]
        candidate.writer_artifact_ref = paths["meta_path"]
    monkeypatch.setattr(
        pipeline.canon_preparation,
        "quality_evaluator",
        lambda **_kwargs: SimpleNamespace(blocked=False,quality_admission_run_id=""),
    )
    monkeypatch.setattr(
        pipeline.canon_preparation.book_state_preparer,
        "prepare",
        lambda **_kwargs: BookStatePreparationOutcome(
            approved_changes=scenario.rewritten_plan.approved_book_state_changes
        ),
    )

    def defer_maintenance(**_kwargs):
        raise RuntimeError("post-Canon maintenance deferred by test")

    monkeypatch.setattr(pipeline, "_run_phase3_pass", defer_maintenance)
    return HttpRuntimeHarness(
        session_factory=scenario.Session, config=config, pipeline=pipeline
    ), pipeline


def test_failed_ordinary_candidate_cannot_be_reapproved(
    prepared_canon, monkeypatch, tmp_path
):
    from fastapi import HTTPException

    scenario = SimpleNamespace(
        Session=prepared_canon.Session,
        project_id=prepared_canon.project_id,
        rewritten_plan=prepared_canon.plan,
    )
    api, _pipeline = _review_api(scenario, monkeypatch, tmp_path)
    with scenario.Session.begin() as session:
        session.get(ChapterPlan, prepared_canon.chapter_plan_id).status = "needs_review"
    commit = _pipeline.canon_admission.commit_plan
    monkeypatch.setattr(
        _pipeline.canon_admission,
        "commit_plan",
        lambda plan: commit(plan, failure_injector=_fail_at("book_state")),
    )
    failed = api.approve_chapter_review(
        scenario.project_id,
        1,
        ChapterReviewApproveRequest(reason="initial ordinary approval"),
    )
    assert failed.status == "needs_review"
    assert "book_state" in failed.frozen_artifact
    monkeypatch.setattr(_pipeline.canon_admission, "commit_plan", commit)
    with pytest.raises(HTTPException) as error:
        api.approve_chapter_review(
            scenario.project_id, 1, ChapterReviewApproveRequest(reason="try review")
        )
    assert error.value.status_code == 400
    with scenario.Session() as session:
        assert (
            session.get(CandidateDraftRecord, prepared_canon.candidate_id).status
            == "failed"
        )


def _commit_same_chapter_world_edit(
    scenario: HistoricalRewriteScenario,
    *,
    node_id: str = "event-independent-edit",
    cognition_patches: list[CognitionPatch] | None = None,
) -> None:
    with scenario.Session.begin() as session:
        proposal = KnowledgeEditProposalRow(
            id="independent-world-edit",
            project_id=scenario.project_id,
            source="world_studio",
            status="pending",
        )
        session.add(proposal)
        session.flush()
        result = CanonAdmissionService().commit_world_edit(
            session=session,
            project_id=scenario.project_id,
            proposal_id=proposal.id,
            approved_changes=ApprovedGraphDeltaSet(
                project_id=scenario.project_id,
                chapter_number=2,
                graph_deltas=[
                    GraphDelta(
                        id="delta-independent-world-edit",
                        project_id=scenario.project_id,
                        chapter_number=2,
                        source_type="world_edit",
                        source_id=proposal.id,
                        world_line_id="independent-world-line",
                        story_time="independent edit time",
                        summary="independent world edit",
                        cognition_patches=cognition_patches or [],
                        node_patches=[
                            NodePatch(
                                node_id=node_id,
                                node_type="event",
                                op="create",
                                new_value={
                                    "id": node_id,
                                    "project_id": scenario.project_id,
                                    "node_type": "event",
                                    "name": "independent world edit",
                                    "state": {"event": "independent world edit"},
                                },
                            )
                        ],
                    )
                ],
            ),
            reason="Independent accepted world edit",
            trigger="test",
        )
        assert result.compile_result.committed is True



def _retained_mainline(scenario):
    from forwin.models.base import Base
    names=("canon_commit_records","world_nodes","world_node_states","world_edges","fact_nodes",
        "graph_deltas","graph_delta_patches","world_snapshots","map_snapshots","cognition_overlays",
        "book_cognition_snapshots","outbox_events")
    with scenario.Session() as session:
        result={name:[dict(row) for row in session.execute(select(Base.metadata.tables[name]).where(
            Base.metadata.tables[name].c.project_id==scenario.project_id)).mappings()]
            for name in names if "project_id" in Base.metadata.tables[name].c}
        result["chapters"]=[(row.id,row.chapter_number,row.active_commit_id,row.status) for row in session.scalars(
            select(ChapterPlan).where(ChapterPlan.project_id==scenario.project_id).order_by(ChapterPlan.chapter_number))]
        result["book_revision"]=session.get(Project,scenario.project_id).book_revision
        result["successor_body"]=session.get(ChapterDraft,scenario.successor_draft_id).body_text
        return result


@pytest.mark.parametrize("historical_rewrite_scenario",[None,"manifest_order","missing_old_metadata"],indirect=True)
def test_replay_only_historical_plan_cannot_authorize_replacement(historical_rewrite_scenario):
    scenario=historical_rewrite_scenario
    before=_retained_mainline(scenario)
    stages=[]
    outcome=CanonAdmissionService(session_factory=scenario.Session).commit_plan(scenario.rewritten_plan,failure_injector=stages.append)
    assert outcome.blocked and outcome.stale
    assert "full-suffix validation" in outcome.failure_reason
    assert stages==[], "no writer runs before reliable full-suffix approval"
    assert _retained_mainline(scenario)==before


@pytest.mark.parametrize("marker_state",["missing","invalid","consumed","ambiguous","backfill"])
def test_retry_marker_alone_never_authorizes_accepted_rewrite(historical_rewrite_scenario,marker_state):
    scenario=historical_rewrite_scenario
    with scenario.Session.begin() as session:
        marker=session.scalar(select(DecisionEvent).where(DecisionEvent.project_id==scenario.project_id,
            DecisionEvent.event_type==DecisionEventType.RETRY_ATTEMPT))
        if marker_state=="missing": session.delete(marker)
        elif marker_state=="invalid": marker.payload_json="not json"
        elif marker_state=="consumed": marker.payload_json=json.dumps({"replacement_commit_id":"unrelated"})
        elif marker_state=="backfill": marker.payload_json=json.dumps({"previous_status":"accepted"})
        else: session.add(DecisionEvent(project_id=scenario.project_id,chapter_number=2,event_type=DecisionEventType.RETRY_ATTEMPT,
            actor_type="api",payload_json=marker.payload_json))
    before=_retained_mainline(scenario)
    outcome=CanonAdmissionService(session_factory=scenario.Session).commit_plan(scenario.rewritten_plan)
    assert outcome.blocked and "full-suffix validation" in outcome.failure_reason
    assert _retained_mainline(scenario)==before


@pytest.mark.parametrize("candidate_state",["failed","reviewed","ready_for_canon"])
@pytest.mark.parametrize("artifact",["missing","stale-body","valid-old-output"])
def test_cached_artifact_and_old_manual_approval_cannot_bypass_revision_proposal(historical_rewrite_scenario,candidate_state,artifact):
    from forwin.generation.pipeline_core.acceptance import AcceptanceStage
    from forwin.state.repo import StateRepository
    scenario=historical_rewrite_scenario
    with scenario.Session.begin() as session:
        candidate=session.get(CandidateDraftRecord,scenario.rewritten_plan.candidate_id)
        candidate.status=candidate_state
        candidate.writer_artifact_ref=artifact
    before=_retained_mainline(scenario)
    class Pipeline(AcceptanceStage):
        _SessionFactory=scenario.Session
        def _make_state_helpers(self,session): return StateRepository(session),StateUpdater(session),None
        def _load_writer_output_from_meta(self,path): pytest.fail("old artifacts do not authorize a historical proposal")
    with pytest.raises(ValueError,match="distinct real revision proposal"):
        Pipeline().accept_review(scenario.project_id,2,reason="manual retry",source="chapter_review_approve_api")
    assert _retained_mainline(scenario)==before


@pytest.mark.parametrize("with_cognition",[False,True])
def test_rejected_old_replay_preserves_independent_world_edits(historical_rewrite_scenario,with_cognition):
    scenario=historical_rewrite_scenario
    _commit_same_chapter_world_edit(scenario,cognition_patches=[CognitionPatch(observer_type="character",observer_id="observer",
        op="append",field_path="visible_refs",new_value="event-independent-edit",evidence_refs=["proof-1"])] if with_cognition else [])
    before=_retained_mainline(scenario)
    outcome=CanonAdmissionService(session_factory=scenario.Session).commit_plan(scenario.rewritten_plan)
    assert outcome.blocked and "full-suffix validation" in outcome.failure_reason
    assert _retained_mainline(scenario)==before
    if with_cognition:
        with scenario.Session() as session:
            runtime=BookStateProjection(session).load_runtime_as_of(scenario.project_id,as_of_chapter=2)
            assert runtime.cognition_by_observer[("character","observer")].evidence_by_ref["event-independent-edit"]==["proof-1"]


@pytest.mark.parametrize("manifest",["not json","null","{}","[]",'[null]','[""]','["delta-obsolete-braking","delta-obsolete-braking"]','["delta-missing"]','["delta-base-event"]'])
def test_invalid_old_delta_manifest_cannot_become_revision_evidence(historical_rewrite_scenario,manifest):
    scenario=historical_rewrite_scenario
    with scenario.Session.begin() as session:
        session.get(CanonCommitRecord,scenario.old_commit_id).graph_delta_ids_json=manifest
    before=_retained_mainline(scenario)
    outcome=CanonAdmissionService(session_factory=scenario.Session).commit_plan(scenario.rewritten_plan)
    assert outcome.blocked and "full-suffix validation" in outcome.failure_reason
    assert _retained_mainline(scenario)==before


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
        roster_item = session.get(SubWorldRosterItem, prepared_canon.roster_item_id)
        assert candidate is not None
        assert roster_item is not None
        assert candidate.status == "failed"
        assert failure_stage in candidate.failure_reason
        roster_metadata = json.loads(roster_item.metadata_json)
        assert roster_metadata["pending_entity_admission"] is True
        assert "character_id" not in roster_metadata


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
    assert snapshot["outbox_events"] == 3
    assert snapshot["canon_commits"] == 1
    assert snapshot["chapter_status"] == "accepted"
    assert snapshot["obligation_status"] == "active"
    with prepared_canon.Session() as session:
        candidate = session.get(CandidateDraftRecord, prepared_canon.candidate_id)
        roster_item = session.get(SubWorldRosterItem, prepared_canon.roster_item_id)
        assert candidate is not None
        assert roster_item is not None
        assert candidate.status == "accepted"
        assert candidate.canon_commit_id == outcome.commit_id
        assert outcome.commit_id == prepared_canon.plan.canon_commit_id
        outbox_rows = (
            session.execute(select(OutboxEvent).order_by(OutboxEvent.event_type.asc()))
            .scalars()
            .all()
        )
        assert {row.event_type for row in outbox_rows} == {
            CANON_PROJECTION_REQUESTED,
            CANON_PHASE3_REQUESTED,
            CANON_PUBLISHER_REQUESTED,
        }
        for row in outbox_rows:
            payload = json.loads(row.payload_json)
            assert row.event_id == canon_event_id(
                prepared_canon.plan.idempotency_key,
                row.event_type,
            )
            assert payload["canon_commit_id"] == outcome.commit_id
            assert payload["canon_idempotency_key"] == prepared_canon.plan.idempotency_key
            assert payload["project_id"] == prepared_canon.project_id
            assert payload["chapter_number"] == 1
            assert payload["candidate_id"] == prepared_canon.candidate_id
        publisher_row = next(
            row for row in outbox_rows if row.event_type == CANON_PUBLISHER_REQUESTED
        )
        publisher_payload = json.loads(publisher_row.payload_json)
        assert publisher_payload["body_sha256"] == prepared_canon.plan.candidate_body_hash
        assert publisher_payload["publisher_bindings"] == [
            {
                "auto_cover_upload_enabled": True,
                "book_meta": {
                    "audience": "",
                    "intro": "",
                    "plot_tags": [],
                    "primary_category": "",
                    "protagonist_names": [],
                    "role_tags": [],
                    "theme_tags": [],
                },
                "book_name": "Prepared Publisher Snapshot",
                "cover_candidate_count": 4,
                "cover_confirmation_required": False,
                "cover_generation_enabled": True,
                "cover_style_hint": "",
                "create_if_missing": False,
                "platform": "qidian",
                "publisher_compliance_required": True,
                "upload_url": "https://write.example/prepared",
            }
        ]
        roster_metadata = json.loads(roster_item.metadata_json)
        assert roster_metadata["character_id"] == f"character-{candidate.id}"
        assert roster_metadata["book_state_node_id"] == f"character-{candidate.id}"
        assert roster_metadata["canon_source"] == "book_state"
        assert "pending_entity_admission" not in roster_metadata


def test_publisher_event_uses_prepared_snapshot_after_settings_change(
    prepared_canon: PreparedCanon,
) -> None:
    with prepared_canon.Session.begin() as session:
        project = session.get(Project, prepared_canon.project_id)
        assert project is not None
        project.automation_json = json.dumps(
            {
                "primary_publish_platform": "qidian",
                "publish_bindings": [
                    {"platform": "fanqie", "book_name": "Changed Too Late"}
                ]
            }
        )

    outcome = CanonAdmissionService(session_factory=prepared_canon.Session).commit_plan(
        prepared_canon.plan
    )

    assert outcome.blocked is False
    with prepared_canon.Session() as session:
        row = session.execute(
            select(OutboxEvent).where(
                OutboxEvent.event_type == CANON_PUBLISHER_REQUESTED
            )
        ).scalar_one()
        payload = json.loads(row.payload_json)
        assert [item["platform"] for item in payload["publisher_bindings"]] == [
            "qidian"
        ]
        assert payload["publisher_bindings"][0]["book_name"] == (
            "Prepared Publisher Snapshot"
        )


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
    first_plan = _rebuild_plan(
        prepared_canon.plan,
        approved_book_state_changes=empty_changes,
        entity_admission_plan=EntityAdmissionPlan(
            project_id=prepared_canon.project_id,
            chapter_number=1,
            candidate_fingerprint=(
                prepared_canon.plan.entity_admission_plan.candidate_fingerprint
            ),
        ),
    )
    with prepared_canon.Session.begin() as session:
        first_candidate = session.get(
            CandidateDraftRecord,
            prepared_canon.candidate_id,
        )
        assert first_candidate is not None
        first_candidate.idempotency_key = first_plan.idempotency_key
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
