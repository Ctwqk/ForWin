from datetime import datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from forwin.models import Base, Project
from forwin.models.book_state import MapSnapshotRow, WorldSnapshotRow, BookCognitionSnapshotRow, WorldNodeStateRow, GraphDeltaRow
from forwin.book_state.repository import BookStateRepository
from forwin.book_state.reviewer import BookStateReviewGate
from forwin.book_state.runtime import BookStateRuntime, ObjectiveWorldGraph
from forwin.book_state.map_graph import MapGraph
from forwin.protocol.book_state import ApprovedGraphDeltaSet, GraphDelta, NodePatch, NarrativePatch, NarrativeNode, WorldNode


@pytest.mark.parametrize("old", [{}, {"old_value": None}])
def test_gate_requires_old_value_on_existing_target(old):
    runtime = BookStateRuntime(project_id="p", as_of_chapter=0, world=ObjectiveWorldGraph(), map_graph=MapGraph())
    runtime.world.add_node(WorldNode(id="actor", project_id="p", node_type="character", state={"status": "well"}))
    patch = NodePatch(node_id="actor", node_type="character", op="set", field_path="state.status", new_value="hurt", **old)
    changes = ApprovedGraphDeltaSet(project_id="p", chapter_number=1, graph_deltas=[GraphDelta(id="d", project_id="p", chapter_number=1, node_patches=[patch])])
    verdict = BookStateReviewGate(None).review(changes, base_chapter=0, runtime=runtime)
    assert not verdict.accepted
    assert "missing_old_value" in {issue.code for issue in verdict.issues}


def test_gate_accepts_parent_merge_and_absent_protection_status():
    runtime = BookStateRuntime(project_id="p", as_of_chapter=0, world=ObjectiveWorldGraph(), map_graph=MapGraph())
    runtime.world.add_node(WorldNode(id="actor", project_id="p", node_type="character"))
    runtime.narrative.nodes_by_id["guard"] = NarrativeNode(id="guard", project_id="p", node_type="review_constraint", payload={"kind": "must_be_alive", "ref": "actor", "until_chapter": 0})
    changes = ApprovedGraphDeltaSet(project_id="p", chapter_number=1, graph_deltas=[GraphDelta(id="d", project_id="p", chapter_number=1,
        node_patches=[NodePatch(node_id="actor", node_type="character", op="merge", field_path="profile", old_value={}, new_value={"public_identity": "clerk"})],
        narrative_patches=[NarrativePatch(target_ref="narrative_node:guard", op="set", field_path="payload.status", old_value=None, new_value="released")])])
    verdict = BookStateReviewGate(None).review(changes, base_chapter=0, runtime=runtime)
    assert verdict.accepted, verdict.issues


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("row_type", [WorldSnapshotRow, MapSnapshotRow, BookCognitionSnapshotRow, WorldNodeStateRow, GraphDeltaRow])
def test_sequences_order_same_transaction_rows_with_tied_timestamps(row_type, backend):
    from tests.postgres import postgres_empty_test_url

    engine = create_engine("sqlite://" if backend == "sqlite" else postgres_empty_test_url("task16-invariants"))
    Base.metadata.create_all(engine)
    stamp = datetime(2026, 10, 22)
    with Session(engine) as session, session.begin():
        session.add(Project(id="p", title="test", premise="test", genre="test"))
        if backend == "postgresql":
            session.flush()
        extra = {"built_at": stamp, "as_of_chapter": 1}
        if row_type is BookCognitionSnapshotRow:
            extra.update(observer_type="reader", observer_id="reader")
        if row_type is WorldNodeStateRow:
            extra = {"created_at": stamp, "as_of_chapter": 1, "node_id": "actor"}
        if row_type is GraphDeltaRow:
            extra = {"created_at": stamp, "chapter_number": 1}
        rows = [row_type(id=row_id, project_id="p", **extra) for row_id in ("z", "a")]
        session.add_all(rows)
        session.flush()
        assert rows[0].seq < rows[1].seq
        assert session.scalars(select(row_type.id).order_by(row_type.seq)).all() == ["z", "a"]
        repo = BookStateRepository(session)
        if row_type is WorldNodeStateRow:
            assert repo._latest_version_row("p", "actor", as_of_chapter=1).id == "a"
        if row_type is GraphDeltaRow:
            assert [delta.id for delta in repo.list_graph_deltas("p", through_chapter=1)] == ["z", "a"]
        if row_type is MapSnapshotRow:
            assert repo.latest_map_snapshot("p", 1).id == "a"
        if row_type is WorldSnapshotRow:
            assert repo.latest_world_snapshot("p", 1).id == "a"
    engine.dispose()


def test_cas_classification_ignores_compiler_message(monkeypatch):
    from forwin.book_state.compiler import BookStateCompiler
    from forwin.canon.admission import CanonAdmissionService, CanonStaleVersion

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session, session.begin():
        session.add(Project(id="p", title="test", premise="test", genre="test"))
        session.flush()
        BookStateRepository(session).create_world_node(
            WorldNode(id="actor", project_id="p", node_type="character", state={"status": "well"}),
            version_chapter=0, version_state={"status": "well"})
        session.add(WorldSnapshotRow(id="seed", project_id="p", as_of_chapter=0))
        session.flush()
        delta = GraphDelta(id="tick-delta", project_id="p", chapter_number=0, source_type="world_engine", metadata={"tick_id": "tick"},
            node_patches=[NodePatch(node_id="actor", node_type="character", op="set", field_path="state.status", old_value="busy", new_value="hurt")])
        changes = ApprovedGraphDeltaSet(project_id="p", chapter_number=0, graph_deltas=[delta])
        validate = BookStateCompiler._validate_delta
        def renamed(self, *args, **kwargs):
            return ["changed diagnostic" for reason in validate(self, *args, **kwargs)]
        monkeypatch.setattr(BookStateCompiler, "_validate_delta", renamed)
        result = BookStateCompiler(session).compile(changes, base_chapter=0)
        assert result.cas_failed is True
        assert result.blocked_reasons == ["changed diagnostic"]
        with pytest.raises(CanonStaleVersion, match="changed diagnostic"):
            CanonAdmissionService().commit_world_tick(session=session, project_id="p", chapter_number=0, approved_changes=changes, tick_id="tick")
    engine.dispose()


def test_cas_classification_does_not_infer_from_schema_message(monkeypatch):
    from forwin.book_state.compiler import BookStateCompiler
    from forwin.canon.admission import CanonAdmissionService, CanonWriteFailure, CanonStaleVersion

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session, session.begin():
        session.add(Project(id="p", title="test", premise="test", genre="test"))
        session.add(WorldSnapshotRow(id="seed", project_id="p", as_of_chapter=0))
        session.flush()
        delta = GraphDelta(id="tick-delta", project_id="p", chapter_number=0,
                           source_type="world_engine", metadata={"tick_id": "tick"})
        changes = ApprovedGraphDeltaSet(project_id="p", chapter_number=0, graph_deltas=[delta])
        monkeypatch.setattr(BookStateCompiler, "_validate_delta", lambda *args, **kwargs: ["old_value mismatch in schema documentation"])
        result = BookStateCompiler(session).compile(changes, base_chapter=0)
        assert result.cas_failed is False
        with pytest.raises(CanonWriteFailure) as error:
            CanonAdmissionService().commit_world_tick(session=session, project_id="p", chapter_number=0,
                                                     approved_changes=changes, tick_id="tick")
        assert not isinstance(error.value, CanonStaleVersion)
    engine.dispose()
