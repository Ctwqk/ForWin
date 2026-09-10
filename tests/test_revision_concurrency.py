"""Two real frozen proposals compete against one accepted book revision."""

import pytest
from sqlalchemy import select

from forwin.canon.admission import CanonAdmissionService
from forwin.canon.revision_service import (
    RevisionValidationService,
    revision_model_identity,
    save_revision_proposal,
)
from forwin.models.canon import CanonCommitRecord
from forwin.models.project import ChapterPlan, Project
from forwin.runtime.policy import RuntimePolicy
from forwin.writer import ChapterWriter
from tests import test_canon_atomic_transaction as atomic
from tests.test_revision_full_suffix import BodyModel, _book

prepared_canon = atomic.prepared_canon


def test_successor_revision_invalidates_earlier_full_suffix_proposal_without_partial_switch(
    prepared_canon,
):
    fixture = prepared_canon
    ids, body, old_ids, _ = _book(fixture)
    writer = ChapterWriter(BodyModel())
    validation = RevisionValidationService(
        session_factory=fixture.Session,
        writer=writer,
        policy=RuntimePolicy.for_profile("standard"),
    )
    canon = CanonAdmissionService(session_factory=fixture.Session)
    with fixture.Session.begin() as session:
        seed = save_revision_proposal(
            session,
            project_id=ids[0],
            chapter_number=1,
            body=body.replace("安静地", "静静地"),
        )
        seed_id = seed.id
    prepared = validation.prepare(project_id=ids[0], candidate_id=seed_id)
    assert not prepared.blocked, prepared
    accepted = canon.commit_plan(
        prepared.plan, revision_model_identity=revision_model_identity(writer)
    )
    assert not accepted.blocked, accepted
    # Both accepted chapters now have exact quality/projection provenance. The
    # two proposals capture the same revision before either changes live Canon.
    with fixture.Session.begin() as session:
        earlier = save_revision_proposal(
            session,
            project_id=ids[0],
            chapter_number=1,
            body=body.replace("安静地", "悄然"),
            expected_book_revision=3,
        )
        later = save_revision_proposal(
            session,
            project_id=ids[0],
            chapter_number=2,
            body=body.replace("安静地", "默默地"),
            expected_book_revision=3,
        )
        earlier_id, later_id = earlier.id, later.id
        original_pointers = [
            session.get(ChapterPlan, p).active_commit_id for p in ids[1:]
        ]
    before = validation.prepare(project_id=ids[0], candidate_id=earlier_id)
    after = validation.prepare(project_id=ids[0], candidate_id=later_id)
    assert not before.blocked, before
    if after.blocked:
        from forwin.models.canon import CanonRevisionValidationRecord

        with fixture.Session() as session:
            pytest.fail(
                session.get(
                    CanonRevisionValidationRecord, after.blocked_path
                ).result_json
            )
    winner = canon.commit_plan(
        after.plan, revision_model_identity=revision_model_identity(writer)
    )
    assert not winner.blocked, winner
    rejected = canon.commit_plan(
        before.plan, revision_model_identity=revision_model_identity(writer)
    )
    assert rejected.blocked
    with fixture.Session() as session:
        assert session.get(Project, ids[0]).book_revision == 4
        assert [session.get(ChapterPlan, p).active_commit_id for p in ids[1:]] == [
            original_pointers[0],
            winner.commit_id,
        ]
        assert session.get(CanonCommitRecord, before.plan.canon_commit_id) is None
        assert (
            len(
                list(
                    session.scalars(
                        select(CanonCommitRecord).where(
                            CanonCommitRecord.project_id == ids[0]
                        )
                    )
                )
            )
            == 5
        )

        # Historical evidence remains readable by its immutable acceptance
        # manifest even after its current materialization has been removed.
        import json

        from forwin.book_state.repository import BookStateRepository
        from forwin.book_state.runtime import ObjectiveWorldGraph
        from forwin.models.book_state import (
            GraphDeltaPatchRow,
            GraphDeltaRow,
            WorldSnapshotRow,
        )

        world = ObjectiveWorldGraph()
        repo = BookStateRepository(session)
        for old_id in old_ids:
            commit = session.get(CanonCommitRecord, old_id)
            for delta_id in json.loads(commit.graph_delta_ids_json):
                raw = session.get(GraphDeltaRow, delta_id)
                patches = list(
                    session.scalars(
                        select(GraphDeltaPatchRow).where(
                            GraphDeltaPatchRow.delta_id == delta_id
                        )
                    )
                )
                patches.sort(
                    key=lambda row: json.loads(row.metadata_json or "{}").get(
                        "sequence", 0
                    )
                )
                world.apply_delta(repo._graph_delta_from_row(raw, patches))
            old_snapshot = session.get(WorldSnapshotRow, commit.world_snapshot_id)
            assert world.states_by_node_id == json.loads(
                old_snapshot.world_node_state_index_json
            )
            assert set(world.nodes_by_id) == set(
                json.loads(old_snapshot.world_node_state_index_json)
            )
