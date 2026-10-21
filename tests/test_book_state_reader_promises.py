from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from forwin.book_state import BookStateCompiler, BookStateRepository
from forwin.book_state.narrative import promise_node_payload, reader_promise_from_node
from forwin.book_state.query import BookStateQuery
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.knowledge import KnowledgeEditProposalRow
from forwin.proposals.proposal_review import approve_world_edit_proposal
from forwin.protocol.book_state import (
    ApprovedGraphDeltaSet,
    GraphDelta,
    NarrativeNode,
    NarrativePatch,
    ReaderPromise,
)
from forwin.runtime.policy import RuntimePolicy
from forwin.state.updater import StateUpdater
from tests.postgres import postgres_test_url


def _session_factory():
    engine = get_engine(postgres_test_url())
    init_db(engine)
    return engine, get_session_factory(engine)


def _create_project(session) -> str:
    return StateUpdater(session).create_project(
        title="Reader promises",
        premise="promises",
        genre="test",
        setting_summary="test",
        runtime_policy=RuntimePolicy.for_profile("standard"),
    ).id


def _compile(session, project_id: str, chapter: int, patches: list[NarrativePatch], *, delta_id: str = ""):
    return BookStateCompiler(session).compile(
        ApprovedGraphDeltaSet(
            project_id=project_id,
            chapter_number=chapter,
            graph_deltas=[
                GraphDelta(
                    id=delta_id or f"delta_{project_id}_{chapter}",
                    project_id=project_id,
                    chapter_number=chapter,
                    narrative_patches=patches,
                )
            ],
        )
    )


def _create_promise(promise_id: str, **fields) -> NarrativePatch:
    return NarrativePatch(
        target_ref=f"promise:{promise_id}",
        op="create",
        new_value=promise_node_payload(
            ReaderPromise(promise_id=promise_id, project_id="ignored", summary="the vault opens", **fields)
        ),
    )


def _propose(Session, project_id: str, operation: dict, *, chapter: int) -> str:
    with Session.begin() as session:
        row = KnowledgeEditProposalRow(
            project_id=project_id,
            source="world_studio",
            target_node_id=str(operation.get("promise_id", "")),
            proposed_patch_json=json.dumps(
                {"forwin_patch": [operation], "frontmatter": {"as_of_chapter": chapter}}
            ),
            status="pending",
        )
        session.add(row)
        session.flush()
        return row.id


def test_promise_node_payload_and_view_roundtrip() -> None:
    promise = ReaderPromise(
        promise_id="promise_a",
        project_id="project",
        promise_type="mystery",
        summary="the vault opens",
        created_at_chapter=3,
        expected_payoff_window="ch 10-12",
        maximum_safe_delay=5,
        current_debt_level=2,
        reward_tags=["reveal"],
        linked_threads=["thread_a"],
        linked_knowledge_gaps=["gap_a"],
        status="active",
        source_refs=["chapter:3"],
    )

    payload = promise_node_payload(promise)

    assert payload == {
        "node_type": "promise",
        "title": "the vault opens",
        "status": "active",
        "payload": {
            "promise_type": "mystery",
            "expected_payoff_window": "ch 10-12",
            "maximum_safe_delay": 5,
            "current_debt_level": 2,
            "reward_tags": ["reveal"],
            "linked_threads": ["thread_a"],
            "linked_knowledge_gaps": ["gap_a"],
            "source_refs": ["chapter:3"],
            "created_at_chapter": 3,
        },
    }
    node = NarrativeNode(id="promise_a", project_id="project", **payload)
    restored = reader_promise_from_node(node)
    assert restored.model_dump(exclude={"metadata"}) == promise.model_dump(exclude={"metadata"})


def test_reader_promise_view_defaults_missing_payload_fields() -> None:
    node = NarrativeNode(
        id="promise_bare",
        project_id="project",
        node_type="promise",
        status="open",
        metadata={"created_at_chapter": 4},
    )

    promise = reader_promise_from_node(node)

    assert promise.promise_id == "promise_bare"
    assert promise.project_id == "project"
    assert promise.status == "open"
    assert promise.created_at_chapter == 4
    assert promise.reward_tags == []


def test_proposal_creates_promise_node() -> None:
    engine, Session = _session_factory()
    with Session.begin() as session:
        project_id = _create_project(session)
    proposal_id = _propose(
        Session,
        project_id,
        {
            "op": "create_reader_promise",
            "promise_id": "promise_truth",
            "new_value": {"promise_type": "mystery", "summary": "the truth surfaces", "current_debt_level": 2},
        },
        chapter=1,
    )

    with Session.begin() as session:
        approve_world_edit_proposal(session, project_id=project_id, proposal_id=proposal_id)

    with Session() as session:
        promises = BookStateQuery(session).reader_promises(project_id, as_of_chapter=1)
        snapshot = BookStateRepository(session).latest_world_snapshot(project_id, 1)
        node = BookStateRepository(session).load_narrative_graph(project_id).get_node("promise_truth")
        delta = BookStateRepository(session).list_graph_deltas(project_id, through_chapter=1)[0]

    assert [promise.promise_id for promise in promises] == ["promise_truth"]
    assert node is not None and node.node_type == "promise"
    assert promises[0].summary == "the truth surfaces"
    assert promises[0].current_debt_level == 2
    assert promises[0].status == "open"
    assert promises[0].created_at_chapter == 1
    assert "promise_truth" in snapshot.active_promise_ids
    assert "reader_promise_patches" not in delta.metadata
    assert [patch.target_ref for patch in delta.narrative_patches] == ["promise:promise_truth"]
    engine.dispose()


def test_proposal_field_edits_map_onto_node_fields() -> None:
    engine, Session = _session_factory()
    with Session.begin() as session:
        project_id = _create_project(session)
    create = _propose(
        Session,
        project_id,
        {
            "op": "create_reader_promise",
            "promise_id": "promise_truth",
            "new_value": {"summary": "the truth surfaces", "current_debt_level": 2},
        },
        chapter=1,
    )
    with Session.begin() as session:
        approve_world_edit_proposal(session, project_id=project_id, proposal_id=create)

    rewrite = _propose(
        Session,
        project_id,
        {
            "op": "set_reader_promise_field",
            "promise_id": "promise_truth",
            "field_path": "summary",
            "old_value": "the truth surfaces",
            "new_value": "the archive proves the truth",
        },
        chapter=2,
    )
    with Session.begin() as session:
        approve_world_edit_proposal(session, project_id=project_id, proposal_id=rewrite)
    settle = _propose(
        Session,
        project_id,
        {"op": "resolve_reader_promise", "promise_id": "promise_truth", "old_value": "open"},
        chapter=3,
    )
    with Session.begin() as session:
        approve_world_edit_proposal(session, project_id=project_id, proposal_id=settle)

    with Session() as session:
        query = BookStateQuery(session)
        at_two = query.reader_promises(project_id, as_of_chapter=2)[0]
        latest = query.reader_promises(project_id)[0]
        delta = BookStateRepository(session).list_graph_deltas(project_id, after_chapter=1, through_chapter=2)[0]

    assert (at_two.summary, at_two.status, at_two.current_debt_level) == ("the archive proves the truth", "open", 2)
    assert (latest.status, latest.current_debt_level) == ("resolved", 0)
    assert [(patch.field_path, patch.old_value) for patch in delta.narrative_patches] == [
        ("title", "the truth surfaces")
    ]
    engine.dispose()


def test_proposal_rejects_unknown_promise_and_unknown_field() -> None:
    engine, Session = _session_factory()
    with Session.begin() as session:
        project_id = _create_project(session)
        _compile(session, project_id, 0, [_create_promise("promise_a")])

    missing = _propose(
        Session,
        project_id,
        {"op": "set_reader_promise_field", "promise_id": "nope", "field_path": "status", "new_value": "x"},
        chapter=1,
    )
    unknown_field = _propose(
        Session,
        project_id,
        {"op": "set_reader_promise_field", "promise_id": "promise_a", "field_path": "bogus", "new_value": "x"},
        chapter=1,
    )
    for proposal_id in (missing, unknown_field):
        with pytest.raises(HTTPException) as rejected:
            with Session.begin() as session:
                approve_world_edit_proposal(session, project_id=project_id, proposal_id=proposal_id)
        assert rejected.value.status_code == 400
    engine.dispose()


@pytest.mark.parametrize(
    "operation",
    [
        {"op": "set_reader_promise_field", "field_path": "maximum_safe_delay", "new_value": "soon"},
        {"op": "set_reader_promise_field", "field_path": "current_debt_level", "new_value": "high"},
        {"op": "set_reader_promise_field", "field_path": "reward_tags", "new_value": "reveal"},
        {"op": "set_reader_promise_field", "field_path": "status", "new_value": ["resolved"]},
        {"op": "resolve_reader_promise", "current_debt_level": "none"},
    ],
)
def test_proposal_rejects_mistyped_promise_values(operation: dict) -> None:
    engine, Session = _session_factory()
    with Session.begin() as session:
        project_id = _create_project(session)
        _compile(session, project_id, 0, [_create_promise("promise_a", current_debt_level=2)])
    proposal_id = _propose(Session, project_id, {"promise_id": "promise_a", **operation}, chapter=1)

    with pytest.raises(HTTPException) as rejected:
        with Session.begin() as session:
            approve_world_edit_proposal(session, project_id=project_id, proposal_id=proposal_id)

    assert rejected.value.status_code == 400
    with Session() as session:
        assert session.get(KnowledgeEditProposalRow, proposal_id).status == "pending"
        promises = BookStateQuery(session).reader_promises(project_id)
    assert [(promise.status, promise.current_debt_level) for promise in promises] == [("open", 2)]
    engine.dispose()


def test_proposal_stores_promise_values_in_their_validated_type() -> None:
    engine, Session = _session_factory()
    with Session.begin() as session:
        project_id = _create_project(session)
        _compile(session, project_id, 0, [_create_promise("promise_a")])
    proposal_id = _propose(
        Session,
        project_id,
        {
            "op": "set_reader_promise_field",
            "promise_id": "promise_a",
            "field_path": "maximum_safe_delay",
            "new_value": "4",
        },
        chapter=1,
    )

    with Session.begin() as session:
        approve_world_edit_proposal(session, project_id=project_id, proposal_id=proposal_id)

    with Session() as session:
        node = BookStateQuery(session).runtime(project_id, as_of_chapter=1).narrative.get_node("promise_a")
    assert node.payload["maximum_safe_delay"] == 4
    engine.dispose()


@pytest.mark.parametrize(
    ("field_path", "stale", "fresh"),
    [
        ("status", "paid_off", "open"),
        ("title", "something else", "the vault opens"),
        ("payload.current_debt_level", 9, 2),
    ],
)
def test_promise_update_cas_mismatch_blocks(field_path: str, stale: object, fresh: object) -> None:
    engine, Session = _session_factory()
    with Session.begin() as session:
        project_id = _create_project(session)
        assert _compile(session, project_id, 0, [_create_promise("promise_a", current_debt_level=2)]).committed

        def edit(delta_id: str, old_value: object):
            return _compile(
                session,
                project_id,
                1,
                [
                    NarrativePatch(
                        target_ref="promise:promise_a",
                        op="set",
                        field_path=field_path,
                        old_value=old_value,
                        new_value="changed" if field_path != "payload.current_debt_level" else 0,
                    )
                ],
                delta_id=delta_id,
            )

        blocked = edit("delta_stale", stale)
        assert not blocked.committed
        assert blocked.blocked_reasons
        assert "old_value mismatch" in blocked.blocked_reasons[0]
        assert f"narrative:promise:promise_a:{field_path}" in blocked.blocked_reasons[0]
        assert edit("delta_fresh", fresh).committed
    engine.dispose()


def test_promise_status_is_versioned() -> None:
    engine, Session = _session_factory()
    with Session.begin() as session:
        project_id = _create_project(session)
        assert _compile(session, project_id, 1, [_create_promise("promise_a")]).committed
    with Session.begin() as session:
        assert _compile(
            session,
            project_id,
            3,
            [
                NarrativePatch(
                    target_ref="promise:promise_a",
                    op="set",
                    field_path="status",
                    old_value="open",
                    new_value="paid_off",
                )
            ],
        ).committed

    with Session() as session:
        query = BookStateQuery(session)
        statuses = {
            chapter: [promise.status for promise in query.reader_promises(project_id, as_of_chapter=chapter)]
            for chapter in (0, 1, 2, 3)
        }
        latest = [promise.status for promise in query.reader_promises(project_id)]
        active = {
            chapter: BookStateRepository(session).latest_world_snapshot(project_id, chapter).active_promise_ids
            for chapter in (1, 3)
        }

    assert statuses == {0: [], 1: ["open"], 2: ["open"], 3: ["paid_off"]}
    assert latest == ["paid_off"]
    assert active == {1: ["promise_a"], 3: []}
    engine.dispose()


def test_reader_promises_ignore_other_narrative_nodes_and_sort_by_creation() -> None:
    engine, Session = _session_factory()
    with Session.begin() as session:
        project_id = _create_project(session)
        assert _compile(
            session,
            project_id,
            2,
            [
                _create_promise("promise_b", created_at_chapter=2),
                _create_promise("promise_a", created_at_chapter=2),
                _create_promise("promise_early", created_at_chapter=1),
                NarrativePatch(
                    target_ref="plot_thread:thread_a",
                    op="create",
                    new_value={"title": "thread", "status": "active"},
                ),
            ],
        ).committed

    with Session() as session:
        promises = BookStateQuery(session).reader_promises(project_id)

    assert [promise.promise_id for promise in promises] == ["promise_early", "promise_a", "promise_b"]
    engine.dispose()
