"""Current projection selection across repeated Canon branch revisions."""

import pytest
from sqlalchemy import select

from forwin.book_state.projection import BookStateProjection
from forwin.book_state.repository import BookStateRepository
from forwin.canon.admission import CanonAdmissionService
from forwin.canon.revision_replica import CandidateReplica, capture_revision
from forwin.canon.revision_service import (
    RevisionValidationService,
    revision_model_identity,
    revision_policy_fingerprint,
    save_revision_proposal,
)
from forwin.models.book_state import GraphDeltaRow, WorldNodeRow, WorldNodeStateRow
from forwin.models.canon import CanonCommitRecord
from forwin.models.project import ChapterPlan, Project
from forwin.protocol.book_state import GraphDelta, NodePatch
from forwin.runtime.policy import RuntimePolicy
from forwin.state.updater import StateUpdater
from forwin.writer import ChapterWriter
from tests import test_canon_atomic_transaction as atomic
from tests.test_revision_full_suffix import BodyModel, _book

prepared_canon = atomic.prepared_canon


def _revision(fixture, ids, body):
    writer = ChapterWriter(BodyModel())
    validation = RevisionValidationService(
        session_factory=fixture.Session,
        writer=writer,
        policy=RuntimePolicy.for_profile("standard"),
    )
    with fixture.Session.begin() as session:
        candidate = save_revision_proposal(
            session,
            project_id=ids[0],
            chapter_number=1,
            body=body.replace("安静地", "静静地"),
        )
        candidate_id = candidate.id
    prepared = validation.prepare(project_id=ids[0], candidate_id=candidate_id)
    return writer, prepared


def test_retired_states_do_not_leak_after_removing_current_nodes(prepared_canon):
    fixture = prepared_canon
    ids, body, _old_ids, _ = _book(fixture)
    with fixture.Session() as session:
        old_rows = {
            row.id: (row.state_json, row.source_delta_id)
            for row in session.scalars(
                select(WorldNodeStateRow).where(WorldNodeStateRow.project_id == ids[0])
            )
        }
        assert old_rows
    writer, prepared = _revision(fixture, ids, body)
    assert not prepared.blocked, prepared
    result = CanonAdmissionService(session_factory=fixture.Session).commit_plan(
        prepared.plan, revision_model_identity=revision_model_identity(writer)
    )
    assert not result.blocked, result
    with fixture.Session() as session:
        for row_id, evidence in old_rows.items():
            row = session.get(WorldNodeStateRow, row_id)
            assert (row.state_json, row.source_delta_id) == evidence
        assert not list(
            session.scalars(
                select(WorldNodeRow).where(WorldNodeRow.project_id == ids[0])
            )
        )
        for chapter in (0, 1, 2):
            repo = BookStateRepository(session)
            assert repo._latest_state_index(ids[0], chapter) == {}
            runtime = BookStateProjection(session).load_runtime_as_of(
                ids[0], as_of_chapter=chapter
            )
            assert runtime.world.states_by_node_id == {}
            assert runtime.world.nodes_by_id == {}


def test_retired_prefix_create_does_not_block_recreated_identity_in_later_chapter(
    prepared_canon,
):
    fixture = prepared_canon
    ids, body, _, _ = _book(fixture)
    writer, prepared = _revision(fixture, ids, body)
    assert not prepared.blocked, prepared
    owner = CanonAdmissionService(session_factory=fixture.Session)
    result = owner.commit_plan(
        prepared.plan, revision_model_identity=revision_model_identity(writer)
    )
    assert not result.blocked, result
    with fixture.Session.begin() as session:
        chapter_two = session.get(ChapterPlan, ids[2])
        active_two = session.get(CanonCommitRecord, chapter_two.active_commit_id)
        atomic._complete_post_canon_barrier(
            session,
            commit_id=active_two.id,
            project_id=ids[0],
            chapter_number=2,
            candidate_id=active_two.candidate_id,
        )
        third = StateUpdater(session).create_chapter_plan(
            project_id=ids[0],
            arc_plan_id=chapter_two.arc_plan_id,
            chapter_number=3,
            title="Archive return",
            one_line="Archive",
            goals=[],
        )
        third_plan = atomic._prepare_chapter_candidate(
            session,
            project_id=ids[0],
            chapter=third,
            version=1,
            body=body,
            summary="Archive",
            delta_id="new-archive-three",
            delta_summary="Archive",
            node_id="archive-event-one",
        )
    accepted = owner.commit_plan(third_plan)
    assert not accepted.blocked, accepted
    with fixture.Session.begin() as session:
        candidate = save_revision_proposal(
            session,
            project_id=ids[0],
            chapter_number=3,
            body=body.replace("安静地", "默默地"),
        )
        snapshot = capture_revision(
            session,
            project_id=ids[0],
            candidate_id=candidate.id,
            model_identity=revision_model_identity(writer),
            policy_fingerprint=revision_policy_fingerprint(
                session.get(Project, ids[0])
            ),
        )
    with CandidateReplica(snapshot) as replica:
        assert replica.session.get(WorldNodeRow, "archive-event-one") is None


@pytest.mark.parametrize("provenance", ["active", "unowned", "missing_delta"])
def test_current_or_unproven_prefix_reference_still_blocks_identity_recreation(
    prepared_canon, provenance
):
    fixture = prepared_canon
    outcome = CanonAdmissionService(session_factory=fixture.Session).commit_plan(
        fixture.plan
    )
    assert not outcome.blocked, outcome
    with fixture.Session.begin() as session:
        repo = BookStateRepository(session)
        if provenance == "active":
            node_id = (
                fixture.plan.approved_book_state_changes.graph_deltas[0]
                .node_patches[0]
                .node_id
            )
        else:
            node_id = "unproven-prefix-node"
            delta = GraphDelta(
                id="unowned-prefix",
                project_id=fixture.project_id,
                chapter_number=0,
                node_patches=[
                    NodePatch(node_id=node_id, node_type="event", op="create")
                ],
            )
            repo.append_graph_delta(delta)
            if provenance == "missing_delta":
                session.delete(session.get(GraphDeltaRow, delta.id))
                session.flush()
        replacement = GraphDelta(
            id="replacement-create",
            project_id=fixture.project_id,
            chapter_number=2,
            node_patches=[NodePatch(node_id=node_id, node_type="event", op="create")],
        )
        with pytest.raises(ValueError, match="recreated identities"):
            repo._rewind_materialized_rows(
                fixture.project_id, [replacement], from_chapter=2
            )


def test_character_canon_compile_preserves_state_provenance_for_private_rewind(
    prepared_canon,
):
    fixture = prepared_canon
    outcome = CanonAdmissionService(session_factory=fixture.Session).commit_plan(
        fixture.plan
    )
    assert not outcome.blocked, outcome
    delta = fixture.plan.approved_book_state_changes.graph_deltas[0]
    node_id = delta.node_patches[0].node_id
    with fixture.Session() as session:
        assert session.get(WorldNodeRow, node_id).node_type == "character"
        rows = list(
            session.scalars(
                select(WorldNodeStateRow).where(
                    WorldNodeStateRow.project_id == fixture.project_id,
                    WorldNodeStateRow.node_id == node_id,
                )
            )
        )
        assert rows
        assert all(row.source_delta_id == delta.id for row in rows)
        evidence = {row.id: (row.state_json, row.source_delta_id) for row in rows}
        snapshot = capture_revision(
            session,
            project_id=fixture.project_id,
            candidate_id=fixture.candidate_id,
            model_identity={"provider": "fixture", "model": "frozen"},
            policy_fingerprint=revision_policy_fingerprint(
                session.get(Project, fixture.project_id)
            ),
        )
    with CandidateReplica(snapshot) as replica:
        assert replica.session.get(WorldNodeRow, node_id) is None
    with fixture.Session() as session:
        assert session.get(WorldNodeRow, node_id) is not None
        for row_id, original in evidence.items():
            row = session.get(WorldNodeStateRow, row_id)
            assert (row.state_json, row.source_delta_id) == original


@pytest.mark.parametrize("source", ["api_manual", "arc_plan_seed"])
def test_non_canon_character_source_ref_is_not_a_graph_delta(prepared_canon, source):
    from forwin.characters.creation import CharacterCreationHelper
    from forwin.characters.models import CharacterCreationRequest

    fixture = prepared_canon
    with fixture.Session.begin() as session:
        result = CharacterCreationHelper(session).create_character(
            CharacterCreationRequest(
                project_id=fixture.project_id,
                source=source,
                source_ref="arbitrary-source-reference",
                name="周怀瑾",
                created_at_chapter=0,
            )
        )
        assert result.created
        rows = list(
            session.scalars(
                select(WorldNodeStateRow).where(
                    WorldNodeStateRow.project_id == fixture.project_id,
                    WorldNodeStateRow.node_id == result.character_id,
                )
            )
        )
        assert rows
        assert all(row.source_delta_id == "" for row in rows)
