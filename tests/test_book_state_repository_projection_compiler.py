from __future__ import annotations

import json
from sqlalchemy import func, select
import pytest

from forwin.book_state import BookStateCompiler, BookStateProjection, BookStateRepository
from forwin.context.assembler_core import assemble_context
from forwin.http.adapters.api_book_state_routes import build_handlers
from forwin.models import ArcPlanVersion, ChapterPlan
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.book_state import (
    BookReaderExperienceDeltaRow,
    BookReaderPromiseRow,
    GraphDeltaPatchRow,
    GraphDeltaRow,
    WorldNodeStateRow,
    WorldSnapshotRow,
)
from forwin.protocol.book_state import (
    ApprovedGraphDeltaSet,
    CognitionOverlay,
    CognitionPatch,
    GraphDelta,
    MapEdge,
    MapNode,
    MapPatch,
    NarrativePatch,
    NodePatch,
    WorldNode,
)
from forwin.runtime.policy import RuntimePolicy
from forwin.state.repo import StateRepository
from forwin.state.updater import StateUpdater
from tests.postgres import postgres_test_url


def _create_project(session, title: str = "BookState 测试") -> str:
    project = StateUpdater(session).create_project(
        title=title,
        premise="BookState persistence",
        genre="玄幻",
        setting_summary="黑石城与上古遗迹",
        runtime_policy=RuntimePolicy.for_profile("standard"),
    )
    return project.id


@pytest.mark.parametrize("old_value", ["Genesis name", None])
def test_historical_invalidation_restores_base_structure_or_fails_closed(
    old_value,
) -> None:
    engine = get_engine(postgres_test_url())
    init_db(engine)
    Session = get_session_factory(engine)
    with Session.begin() as session:
        project_id = _create_project(session)
        repo = BookStateRepository(session)
        repo.create_world_node(
            WorldNode(
                id="base-item",
                project_id=project_id,
                node_type="event",
                name="Genesis name",
            )
        )
        result = BookStateCompiler(session).compile(
            ApprovedGraphDeltaSet(
                project_id=project_id,
                chapter_number=1,
                graph_deltas=[
                    GraphDelta(
                        id="rename-base",
                        project_id=project_id,
                        chapter_number=1,
                        node_patches=[
                            NodePatch(
                                node_id="base-item",
                                node_type="event",
                                op="set",
                                field_path="name",
                                old_value=old_value,
                                new_value="Retired name",
                            )
                        ],
                    )
                ],
            )
        )
        assert result.committed
        # A non-GraphDelta row is not a Canon materialization to discard.
        repo.create_world_node(
            WorldNode(
                id="external-item",
                project_id=project_id,
                node_type="event",
                name="Independent material",
                created_at_chapter=1,
            )
        )

    if old_value is None:
        with pytest.raises(ValueError, match="cannot restore historical base"):
            with Session.begin() as session:
                BookStateRepository(session).invalidate_project_range(
                    project_id,
                    from_chapter=1,
                    through_chapter=1,
                )
        with Session() as session:
            assert (
                BookStateRepository(session).get_world_node("base-item").name
                == "Retired name"
            )
    else:
        with Session.begin() as session:
            repo = BookStateRepository(session)
            repo.invalidate_project_range(project_id, from_chapter=1, through_chapter=1)
            assert repo.get_world_node("base-item").name == "Genesis name"
            assert repo.get_world_node("external-item").name == "Independent material"
    engine.dispose()


def test_historical_invalidation_retires_reader_experience_by_delta_provenance() -> (
    None
):
    engine = get_engine(postgres_test_url())
    init_db(engine)
    Session = get_session_factory(engine)
    with Session.begin() as session:
        project_id = _create_project(session)
        result = BookStateCompiler(session).compile(
            ApprovedGraphDeltaSet(
                project_id=project_id,
                chapter_number=1,
                graph_deltas=[
                    GraphDelta(
                        id="old-reader-delta",
                        project_id=project_id,
                        chapter_number=1,
                        metadata={
                            "reader_experience_delta": {
                                "reader_experience_delta_id": "retired-reader-experience",
                                "reader_state_after": "Obsolete event",
                            }
                        },
                    )
                ],
            )
        )
        assert result.committed
        BookStateRepository(session).invalidate_project_range(
            project_id,
            from_chapter=1,
            through_chapter=1,
        )
        assert session.scalar(select(func.count(BookReaderExperienceDeltaRow.id))) == 0
        assert session.scalar(select(func.count(BookReaderPromiseRow.id))) == 0
    engine.dispose()


@pytest.mark.parametrize(
    "proof",
    ["valid", "missing-snapshot", "wrong-digest", "missing-checkpoint"],
)
@pytest.mark.parametrize(
    ("rewound_path", "obsolete_value"),
    [
        ("metadata.writer_state.controlled_by", "obsolete-owner"),
        ("metadata.writer_location", "obsolete-location"),
    ],
    ids=["writer-state", "writer-location"],
)
@pytest.mark.parametrize(
    "writer_state",
    [
        None,
        {},
        {"controlled_by": None},
        {"controlled_by": "base-owner", "access": "open"},
    ],
    ids=["absent-parent", "empty-parent", "explicit-null", "prior-value"],
)
def test_historical_invalidation_recovers_missing_node_metadata_before_image(
    writer_state,
    proof,
    rewound_path,
    obsolete_value,
) -> None:
    engine = get_engine(postgres_test_url())
    init_db(engine)
    Session = get_session_factory(engine)
    node_id = "site_state_node-ninth-workshop"
    with Session.begin() as session:
        project_id = _create_project(session)
        repo = BookStateRepository(session)
        metadata = {"source": "map_generation"}
        if writer_state is not None:
            metadata["writer_state"] = writer_state
        repo.create_world_node(
            WorldNode(
                id=node_id,
                project_id=project_id,
                node_type="site_state",
                metadata=metadata,
            )
        )
        preceding_patches = [
            NodePatch(
                node_id=node_id,
                node_type="site_state",
                op="set",
                field_path="metadata",
                new_value=repo.get_world_node(node_id).metadata,
            )
        ]
        if writer_state and "controlled_by" in writer_state:
            preceding_patches.append(
                NodePatch(
                    node_id=node_id,
                    node_type="site_state",
                    op="set",
                    field_path="metadata.writer_state.controlled_by",
                    new_value=writer_state["controlled_by"],
                )
            )
        if proof == "missing-checkpoint":
            preceding_patches = []
        compiler = BookStateCompiler(session)
        base = compiler.compile(
            ApprovedGraphDeltaSet(
                project_id=project_id,
                chapter_number=1,
                graph_deltas=[
                    GraphDelta(
                        id="base-control",
                        project_id=project_id,
                        chapter_number=1,
                        node_patches=preceding_patches,
                    )
                ],
            )
        )
        assert base.committed
        before_metadata = repo.get_world_node(node_id).metadata
        replaced = compiler.compile(
            ApprovedGraphDeltaSet(
                project_id=project_id,
                chapter_number=2,
                graph_deltas=[
                    GraphDelta(
                        id="obsolete-control",
                        project_id=project_id,
                        chapter_number=2,
                        node_patches=[
                            NodePatch(
                                node_id=node_id,
                                node_type="site_state",
                                op="set",
                                field_path=rewound_path,
                                new_value=obsolete_value,
                            )
                        ],
                    )
                ],
            )
        )
        assert replaced.committed
        if proof == "missing-snapshot":
            session.delete(session.get(WorldSnapshotRow, base.world_snapshot_id))
        elif proof == "wrong-digest":
            session.get(
                WorldSnapshotRow, base.world_snapshot_id
            ).objective_graph_digest = "0" * 64

    requires_fail_closed = proof != "valid" and not (
        rewound_path == "metadata.writer_location" and proof == "missing-checkpoint"
    )
    if requires_fail_closed:
        with pytest.raises(ValueError, match="cannot restore historical base"):
            with Session.begin() as session:
                BookStateRepository(session).invalidate_project_range(
                    project_id, from_chapter=2, through_chapter=2
                )
        with Session() as session:
            metadata = BookStateRepository(session).get_world_node(node_id).metadata
            if rewound_path == "metadata.writer_state.controlled_by":
                assert metadata["writer_state"]["controlled_by"] == obsolete_value
            else:
                assert metadata["writer_location"] == obsolete_value
            assert session.get(WorldSnapshotRow, replaced.world_snapshot_id) is not None
        engine.dispose()
        return

    with pytest.raises(RuntimeError, match="after verified metadata rewind"):
        with Session.begin() as session:
            repo = BookStateRepository(session)
            repo.invalidate_project_range(project_id, from_chapter=2, through_chapter=2)
            assert repo.get_world_node(node_id).metadata == before_metadata
            raise RuntimeError("after verified metadata rewind")
    with Session() as session:
        metadata = BookStateRepository(session).get_world_node(node_id).metadata
        if rewound_path == "metadata.writer_state.controlled_by":
            assert metadata["writer_state"]["controlled_by"] == obsolete_value
        else:
            assert metadata["writer_location"] == obsolete_value
        assert session.get(WorldSnapshotRow, replaced.world_snapshot_id) is not None
    with Session.begin() as session:
        repo = BookStateRepository(session)
        repo.invalidate_project_range(project_id, from_chapter=2, through_chapter=2)
        assert repo.get_world_node(node_id).metadata == before_metadata
        assert session.get(GraphDeltaRow, "obsolete-control") is not None
        assert session.get(WorldSnapshotRow, base.world_snapshot_id) is not None
    engine.dispose()


def test_historical_invalidation_rewinds_unversioned_writer_location_from_contract(
) -> None:
    engine = get_engine(postgres_test_url())
    init_db(engine)
    Session = get_session_factory(engine)
    node_id = "item_node-blue-copper"
    location = {
        "reported_old": "",
        "reported_new": "旧库六号货位",
    }
    with Session.begin() as session:
        project_id = _create_project(session)
        repo = BookStateRepository(session)
        repo.create_world_node(
            WorldNode(
                id=node_id,
                project_id=project_id,
                node_type="item",
                metadata={"source": "arc_plan_seed"},
            )
        )
        compiler = BookStateCompiler(session)
        base = compiler.compile(
            ApprovedGraphDeltaSet(
                project_id=project_id,
                chapter_number=1,
                graph_deltas=[
                    GraphDelta(
                        id="base-without-metadata-provenance",
                        project_id=project_id,
                        chapter_number=1,
                    )
                ],
            )
        )
        assert base.committed
        replaced = compiler.compile(
            ApprovedGraphDeltaSet(
                project_id=project_id,
                chapter_number=2,
                graph_deltas=[
                    GraphDelta(
                        id="writer-location-contract",
                        project_id=project_id,
                        chapter_number=2,
                        node_patches=[
                            NodePatch(
                                node_id=node_id,
                                node_type="item",
                                op="set",
                                field_path="metadata.writer_location",
                                old_value=None,
                                new_value=location,
                            )
                        ],
                    )
                ],
            )
        )
        assert replaced.committed

    with Session.begin() as session:
        repo = BookStateRepository(session)
        repo.invalidate_project_range(project_id, from_chapter=2, through_chapter=2)
        assert "writer_location" not in repo.get_world_node(node_id).metadata
    engine.dispose()


def test_historical_invalidation_rejects_unmatched_writer_location_trace() -> None:
    engine = get_engine(postgres_test_url())
    init_db(engine)
    Session = get_session_factory(engine)
    node_id = "item_node-blue-copper"
    recorded_location = {
        "reported_old": "",
        "reported_new": "旧库六号货位",
    }
    with Session.begin() as session:
        project_id = _create_project(session)
        repo = BookStateRepository(session)
        repo.create_world_node(
            WorldNode(
                id=node_id,
                project_id=project_id,
                node_type="item",
                metadata={"source": "arc_plan_seed"},
            )
        )
        compiler = BookStateCompiler(session)
        assert compiler.compile(
            ApprovedGraphDeltaSet(
                project_id=project_id,
                chapter_number=1,
                graph_deltas=[
                    GraphDelta(
                        id="base-without-metadata-provenance",
                        project_id=project_id,
                        chapter_number=1,
                    )
                ],
            )
        ).committed
        assert compiler.compile(
            ApprovedGraphDeltaSet(
                project_id=project_id,
                chapter_number=2,
                graph_deltas=[
                    GraphDelta(
                        id="writer-location-contract",
                        project_id=project_id,
                        chapter_number=2,
                        node_patches=[
                            NodePatch(
                                node_id=node_id,
                                node_type="item",
                                op="set",
                                field_path="metadata.writer_location",
                                old_value=None,
                                new_value=recorded_location,
                            )
                        ],
                    )
                ],
            )
        ).committed
        node = repo.get_world_node(node_id)
        assert node is not None
        repo.create_world_node(
            node.model_copy(
                update={
                    "metadata": {
                        **node.metadata,
                        "writer_location": {
                            "reported_old": "",
                            "reported_new": "不在受审补丁中的位置",
                        },
                    }
                }
            )
        )

    with pytest.raises(ValueError, match="cannot restore historical base"):
        with Session.begin() as session:
            BookStateRepository(session).invalidate_project_range(
                project_id, from_chapter=2, through_chapter=2
            )
    engine.dispose()


def test_historical_invalidation_rejects_recreated_base_identity() -> None:
    engine = get_engine(postgres_test_url())
    init_db(engine)
    Session = get_session_factory(engine)
    with Session.begin() as session:
        project_id = _create_project(session)
        for chapter, name in ((0, "Genesis event"), (1, "Overwritten event")):
            result = BookStateCompiler(session).compile(
                ApprovedGraphDeltaSet(
                    project_id=project_id,
                    chapter_number=chapter,
                    graph_deltas=[
                        GraphDelta(
                            id=f"create-shared-{chapter}",
                            project_id=project_id,
                            chapter_number=chapter,
                            node_patches=[
                                NodePatch(
                                    node_id="shared-event",
                                    node_type="event",
                                    op="create",
                                    new_value={
                                        "id": "shared-event",
                                        "project_id": project_id,
                                        "node_type": "event",
                                        "name": name,
                                    },
                                )
                            ],
                        )
                    ],
                )
            )
            assert result.committed
    with pytest.raises(ValueError, match="cannot restore historical base"):
        with Session.begin() as session:
            BookStateRepository(session).invalidate_project_range(
                project_id,
                from_chapter=1,
                through_chapter=1,
            )
    with Session() as session:
        assert BookStateRepository(session).get_world_node("shared-event") is not None
    engine.dispose()


@pytest.mark.parametrize("valid_order", [True, False])
def test_historical_metadata_before_image_uses_snapshot_delta_order(
    valid_order,
) -> None:
    engine = get_engine(postgres_test_url())
    init_db(engine)
    Session = get_session_factory(engine)
    with Session.begin() as session:
        project_id = _create_project(session)
        compiler = BookStateCompiler(session)
        base = compiler.compile(
            ApprovedGraphDeltaSet(
                project_id=project_id,
                chapter_number=1,
                graph_deltas=[
                    GraphDelta(
                        id="z-first",
                        project_id=project_id,
                        chapter_number=1,
                        node_patches=[
                            NodePatch(
                                node_id="ordered-site",
                                node_type="site_state",
                                op="create",
                                new_value={
                                    "project_id": project_id,
                                    "metadata": {
                                        "created_at_chapter": 0,
                                        "writer_state": {
                                            "controlled_by": "intermediate"
                                        },
                                    },
                                },
                            )
                        ],
                    ),
                    GraphDelta(
                        id="a-second",
                        project_id=project_id,
                        chapter_number=1,
                        node_patches=[
                            NodePatch(
                                node_id="ordered-site",
                                node_type="site_state",
                                op="set",
                                field_path="metadata.writer_state.controlled_by",
                                new_value="final-base",
                            )
                        ],
                    ),
                ],
            )
        )
        assert base.committed
        assert (
            session.get(GraphDeltaRow, "z-first").created_at
            == session.get(GraphDeltaRow, "a-second").created_at
        )
        assert compiler.compile(
            ApprovedGraphDeltaSet(
                project_id=project_id,
                chapter_number=2,
                graph_deltas=[
                    GraphDelta(
                        id="new-controller",
                        project_id=project_id,
                        chapter_number=2,
                        node_patches=[
                            NodePatch(
                                node_id="ordered-site",
                                node_type="site_state",
                                op="set",
                                field_path="metadata.writer_state.controlled_by",
                                new_value="obsolete",
                            )
                        ],
                    )
                ],
            )
        ).committed
        if not valid_order:
            session.get(
                WorldSnapshotRow, base.world_snapshot_id
            ).source_delta_ids_json = json.dumps(["a-second"])
    if valid_order:
        with Session.begin() as session:
            repo = BookStateRepository(session)
            repo.invalidate_project_range(project_id, from_chapter=2, through_chapter=2)
            assert (
                repo.get_world_node("ordered-site").metadata["writer_state"][
                    "controlled_by"
                ]
                == "final-base"
            )
    else:
        with pytest.raises(ValueError, match="cannot restore historical base"):
            with Session.begin() as session:
                BookStateRepository(session).invalidate_project_range(
                    project_id, from_chapter=2, through_chapter=2
                )
        with Session() as session:
            assert (
                BookStateRepository(session)
                .get_world_node("ordered-site")
                .metadata["writer_state"]["controlled_by"]
                == "obsolete"
            )
    engine.dispose()


def test_objective_digest_preserves_existing_snapshot_encoding() -> None:
    from forwin.book_state.projection import _digest
    from forwin.book_state.runtime import ObjectiveWorldGraph

    graph = ObjectiveWorldGraph(
        nodes=[
            WorldNode(
                id="digest-node",
                project_id="project",
                node_type="site_state",
                state={"phase": "基础", "nullable": None, "values": [1, False]},
                metadata={"writer_state": {"controlled_by": "第九工坊"}},
            )
        ]
    )
    assert graph.objective_digest() == _digest(
        {
            "nodes": graph.nodes_by_id,
            "edges": graph.edges_by_id,
            "facts": graph.facts_by_id,
            "states": graph.states_by_node_id,
        }
    )


def test_historical_invalidation_rejects_unversioned_base_promise_metadata() -> None:
    engine = get_engine(postgres_test_url())
    init_db(engine)
    Session = get_session_factory(engine)
    with Session.begin() as session:
        project_id = _create_project(session)
        for chapter, patch in (
            (
                0,
                {
                    "promise_id": "base-promise",
                    "op": "create",
                    "new_value": {"status": "open"},
                },
            ),
            (
                1,
                {
                    "promise_id": "base-promise",
                    "op": "set",
                    "field_path": "status",
                    "old_value": "open",
                    "new_value": "resolved",
                    "evidence_refs": ["obsolete-event"],
                },
            ),
        ):
            result = BookStateCompiler(session).compile(
                ApprovedGraphDeltaSet(
                    project_id=project_id,
                    chapter_number=chapter,
                    graph_deltas=[
                        GraphDelta(
                            id=f"promise-delta-{chapter}",
                            project_id=project_id,
                            chapter_number=chapter,
                            metadata={"reader_promise_patches": [patch]},
                        )
                    ],
                )
            )
            assert result.committed
    with pytest.raises(ValueError, match="cannot restore historical base"):
        with Session.begin() as session:
            BookStateRepository(session).invalidate_project_range(
                project_id,
                from_chapter=1,
                through_chapter=1,
            )
    with Session() as session:
        promise = BookStateRepository(session).list_reader_promises_native(project_id)[
            0
        ]
        assert promise.status == "resolved"
        assert "obsolete-event" in promise.source_refs
    engine.dispose()


def test_repository_roundtrip_loads_runtime_with_map_and_cognition() -> None:
    engine = get_engine(postgres_test_url())
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        project_id = _create_project(session)
        repo = BookStateRepository(session)
        repo.create_world_node(
            WorldNode(
                id="char_mc",
                project_id=project_id,
                node_type="character",
                name="陆沉",
                state={"location_id": "loc_city"},
            )
        )
        repo.append_world_node_state(
            project_id=project_id,
            node_id="char_mc",
            node_type="character",
            as_of_chapter=0,
            state={"location_id": "loc_city", "status": "idle"},
        )
        repo.create_map_node(MapNode(id="loc_city", project_id=project_id, node_type="settlement", name="黑石城"))
        repo.create_map_node(MapNode(id="loc_inner", project_id=project_id, node_type="room", name="遗迹内殿"))
        repo.create_map_edge(
            MapEdge(
                id="edge_secret",
                project_id=project_id,
                from_node_id="loc_city",
                to_node_id="loc_inner",
                edge_type="hidden_route",
                travel_time=0.5,
                status="hidden",
                discovered_by_default=False,
            )
        )
        repo.upsert_cognition_overlay(
            CognitionOverlay(
                id="cog_mc_0",
                project_id=project_id,
                observer_type="character",
                observer_id="char_mc",
                as_of_chapter=0,
                hidden_refs=["map_edge:edge_secret"],
            )
        )

        runtime = BookStateProjection(session).load_runtime_as_of(
            project_id,
            as_of_chapter=0,
            observer_keys=[("character", "char_mc")],
        )

    assert runtime.world.get_state("char_mc")["status"] == "idle"
    assert runtime.map.shortest_path("loc_city", "loc_inner").reachable is True
    known = runtime.map.shortest_path(
        "loc_city",
        "loc_inner",
        observer=("character", "char_mc"),
    )
    assert known.reachable is False
    assert runtime.cognition_by_observer[("character", "char_mc")].get_belief("map_edge:edge_secret") == "hidden"


def test_projection_honors_created_at_chapter_for_world_and_map_rows() -> None:
    engine = get_engine(postgres_test_url())
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        project_id = _create_project(session)
        repo = BookStateRepository(session)
        repo.create_world_node(
            WorldNode(
                id="char_future",
                project_id=project_id,
                node_type="character",
                name="未来角色",
                created_at_chapter=5,
            )
        )
        repo.create_map_node(MapNode(id="loc_now", project_id=project_id, node_type="settlement"))
        repo.create_map_node(
            MapNode(
                id="loc_future",
                project_id=project_id,
                node_type="site",
                metadata={"created_at_chapter": 5},
            )
        )
        repo.create_map_edge(
            MapEdge(
                id="edge_future",
                project_id=project_id,
                from_node_id="loc_now",
                to_node_id="loc_future",
                edge_type="road",
                metadata={"created_at_chapter": 5},
            )
        )

        chapter_zero = BookStateProjection(session).load_runtime_as_of(project_id, as_of_chapter=0)
        chapter_five = BookStateProjection(session).load_runtime_as_of(project_id, as_of_chapter=5)

    assert "char_future" not in chapter_zero.world.nodes_by_id
    assert "loc_future" not in chapter_zero.map.nodes_by_id
    assert "edge_future" not in chapter_zero.map.edges_by_id
    assert "char_future" in chapter_five.world.nodes_by_id
    assert "loc_future" in chapter_five.map.nodes_by_id
    assert "edge_future" in chapter_five.map.edges_by_id


def test_context_assembly_prefers_book_state_runtime_overlay() -> None:
    engine = get_engine(postgres_test_url())
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        project_id = _create_project(session)
        arc = ArcPlanVersion(
            id="arc1",
            project_id=project_id,
            arc_number=1,
            chapter_start=1,
            chapter_end=1,
            arc_synopsis="BookState arc",
            status="active",
        )
        chapter = ChapterPlan(
            id="chapter1",
            project_id=project_id,
            arc_plan_id=arc.id,
            chapter_number=2,
            title="入城",
            one_line="主角进入黑石城。",
        )
        session.add_all([arc, chapter])
        repo = BookStateRepository(session)
        repo.create_map_node(MapNode(id="loc_city", project_id=project_id, node_type="settlement", name="黑石城"))
        repo.create_world_node(
            WorldNode(
                id="char_mc",
                project_id=project_id,
                node_type="character",
                name="陆沉",
                state={"location_id": "loc_city"},
                profile={
                    "personality_loadout": {
                        "dominant": {
                            "skill": "trait-loyal-protector",
                            "weight": 0.72,
                        },
                        "secondary": [],
                        "social_mask": [],
                        "stress_modes": [],
                        "relationship_patterns": [],
                        "overrides": {},
                    }
                },
            )
        )
        repo.append_world_node_state(
            project_id=project_id,
            node_id="char_mc",
            node_type="character",
            as_of_chapter=1,
            state={"location_id": "loc_city"},
        )
        BookStateCompiler(session).compile(
            ApprovedGraphDeltaSet(
                project_id=project_id,
                chapter_number=1,
                graph_deltas=[
                    GraphDelta(
                        id="delta_narrative",
                        project_id=project_id,
                        chapter_number=1,
                        narrative_patches=[
                            NarrativePatch(
                                target_ref="world_line:line_main",
                                op="create",
                                new_value={"title": "黑石城主线", "status": "active"},
                            ),
                            NarrativePatch(
                                target_ref="knowledge_gap:gap_secret",
                                op="create",
                                new_value={"title": "密道真相", "status": "open"},
                            ),
                        ],
                    )
                ],
            )
        )

        context = assemble_context(StateRepository(session), project_id, chapter)

    assert "line_main" in context.active_world_lines
    assert "gap_secret" in context.active_knowledge_gaps
    active_location = next(item for item in context.map_context["active_locations"] if item["entity_id"] == "char_mc")
    assert active_location["location_id"] == "loc_city"
    assert active_location["source"] == "book_state"


def test_compiler_commits_delta_patches_and_rebuilds_snapshots() -> None:
    engine = get_engine(postgres_test_url())
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        project_id = _create_project(session)
        repo = BookStateRepository(session)
        repo.create_world_node(
            WorldNode(
                id="char_mc",
                project_id=project_id,
                node_type="character",
                name="陆沉",
                state={"location_id": "loc_village"},
            )
        )
        repo.append_world_node_state(
            project_id=project_id,
            node_id="char_mc",
            node_type="character",
            as_of_chapter=0,
            state={"location_id": "loc_village"},
        )
        repo.create_map_node(MapNode(id="loc_village", project_id=project_id, node_type="settlement"))
        repo.create_map_node(MapNode(id="loc_city", project_id=project_id, node_type="settlement"))
        repo.create_map_edge(
            MapEdge(
                id="edge_village_city",
                project_id=project_id,
                from_node_id="loc_village",
                to_node_id="loc_city",
                edge_type="road",
                travel_time=2,
                status="open",
            )
        )

        result = BookStateCompiler(session).compile(
            ApprovedGraphDeltaSet(
                project_id=project_id,
                chapter_number=1,
                graph_deltas=[
                    GraphDelta(
                        id="delta_ch1",
                        project_id=project_id,
                        chapter_number=1,
                        story_time="第一日",
                        summary="主角进入黑石城，道路随后封锁。",
                        node_patches=[
                            NodePatch(
                                node_id="char_mc",
                                node_type="character",
                                op="set",
                                field_path="state.location_id",
                                old_value="loc_village",
                                new_value="loc_city",
                            )
                        ],
                        map_patches=[
                            MapPatch(
                                target_type="map_edge",
                                target_id="edge_village_city",
                                op="set",
                                field_path="status",
                                old_value="open",
                                new_value="blocked",
                            )
                        ],
                        cognition_patches=[
                            CognitionPatch(
                                observer_type="character",
                                observer_id="char_mc",
                                op="append",
                                field_path="suspected_refs",
                                new_value="fact:road_blockade",
                            )
                        ],
                    )
                ],
            )
        )

        runtime = BookStateProjection(session).load_runtime_as_of(
            project_id,
            as_of_chapter=1,
            observer_keys=[("character", "char_mc")],
        )
        delta_count = session.scalar(select(func.count()).select_from(GraphDeltaRow))
        patch_count = session.scalar(select(func.count()).select_from(GraphDeltaPatchRow))

    assert result.committed is True
    assert result.graph_delta_ids == ["delta_ch1"]
    assert result.world_snapshot_id
    assert result.map_snapshot_id
    assert runtime.world.get_state("char_mc")["location_id"] == "loc_city"
    assert runtime.map.edges_by_id["edge_village_city"].status == "blocked"
    assert "fact:road_blockade" in runtime.cognition_by_observer[("character", "char_mc")].suspected_refs
    assert delta_count == 1
    assert patch_count == 3


def test_compiler_create_character_patch_assigns_personality_loadout() -> None:
    engine = get_engine(postgres_test_url("compiler-character-personality"))
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        project_id = _create_project(session)
        result = BookStateCompiler(session).compile(
            ApprovedGraphDeltaSet(
                project_id=project_id,
                chapter_number=1,
                graph_deltas=[
                    GraphDelta(
                        id="delta_create_character",
                        project_id=project_id,
                        chapter_number=1,
                        node_patches=[
                            NodePatch(
                                node_id="char_shen",
                                node_type="character",
                                op="create",
                                new_value={
                                    "id": "char_shen",
                                    "project_id": project_id,
                                    "node_type": "character",
                                    "name": "沈临川",
                                    "description": "冷静护卫，负责保护主角。",
                                    "profile": {"role_archetype": "护卫"},
                                },
                            )
                        ],
                    )
                ],
            )
        )
        node = BookStateRepository(session).list_world_nodes(project_id)[0]
        state_count = session.scalar(select(func.count()).select_from(WorldNodeStateRow))

    assert result.committed is True
    assert node.id == "char_shen"
    assert node.profile["personality_loadout"]["dominant"]["skill"] == "trait-loyal-protector"
    assert node.metadata["personality_assignment"]["assignment_mode"] in {"auto_rule", "fallback_minimal"}
    assert state_count == 2


def test_compiler_preserves_explicit_character_id_when_future_name_duplicate_exists() -> None:
    engine = get_engine(postgres_test_url("compiler-character-explicit-id"))
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        project_id = _create_project(session)
        repo = BookStateRepository(session)
        repo.create_world_node(
            WorldNode(
                id="char_planning_leak",
                project_id=project_id,
                node_type="character",
                name="舟七",
                created_at_chapter=5,
            )
        )
        result = BookStateCompiler(session).compile(
            ApprovedGraphDeltaSet(
                project_id=project_id,
                chapter_number=5,
                graph_deltas=[
                    GraphDelta(
                        id="delta_admit_zhou_qi",
                        project_id=project_id,
                        chapter_number=5,
                        node_patches=[
                            NodePatch(
                                node_id="char_canon_zhou_qi",
                                node_type="character",
                                op="create",
                                new_value={
                                    "project_id": project_id,
                                    "name": "舟七",
                                    "created_at_chapter": 5,
                                },
                            )
                        ],
                    )
                ],
            )
        )
        admitted = repo.get_world_node("char_canon_zhou_qi")

    assert result.committed is True
    assert admitted is not None
    assert admitted.id == "char_canon_zhou_qi"
    assert admitted.name == "舟七"


def test_compiler_rejects_graph_delta_id_owned_by_another_project() -> None:
    engine = get_engine(postgres_test_url("compiler-cross-project-delta-id"))
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        first_project_id = _create_project(session)
        second_project_id = _create_project(session)
        first = BookStateCompiler(session).compile(
            ApprovedGraphDeltaSet(
                project_id=first_project_id,
                chapter_number=1,
                graph_deltas=[
                    GraphDelta(
                        id="shared_delta_id",
                        project_id=first_project_id,
                        chapter_number=1,
                    )
                ],
            )
        )
        replayed = BookStateCompiler(session).compile(
            ApprovedGraphDeltaSet(
                project_id=first_project_id,
                chapter_number=1,
                graph_deltas=[
                    GraphDelta(
                        id="shared_delta_id",
                        project_id=first_project_id,
                        chapter_number=1,
                    )
                ],
            )
        )
        second = BookStateCompiler(session).compile(
            ApprovedGraphDeltaSet(
                project_id=second_project_id,
                chapter_number=1,
                graph_deltas=[
                    GraphDelta(
                        id="shared_delta_id",
                        project_id=second_project_id,
                        chapter_number=1,
                    )
                ],
            )
        )

    assert first.committed is True
    assert replayed.committed is True
    assert replayed.metadata == {"idempotent": True}
    assert second.committed is False
    assert second.metadata.get("idempotent") is not True
    assert second.blocked_reasons == [
        "graph_delta ids belong to another project: ['shared_delta_id']"
    ]


def test_projection_does_not_replay_persisted_cognition_overlay_evidence() -> None:
    engine = get_engine(postgres_test_url())
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        project_id = _create_project(session)
        result = BookStateCompiler(session).compile(
            ApprovedGraphDeltaSet(
                project_id=project_id,
                chapter_number=1,
                graph_deltas=[
                    GraphDelta(
                        id="delta_cognition",
                        project_id=project_id,
                        chapter_number=1,
                        cognition_patches=[
                            CognitionPatch(
                                observer_type="character",
                                observer_id="char_mc",
                                op="append",
                                field_path="hidden_refs",
                                new_value="map_edge:secret",
                                evidence_refs=["chapter:1"],
                            )
                        ],
                    )
                ],
            )
        )
        runtime = BookStateProjection(session).load_runtime_as_of(
            project_id,
            as_of_chapter=1,
            observer_keys=[("character", "char_mc")],
        )

    view = runtime.cognition_by_observer[("character", "char_mc")]
    assert result.committed is True
    assert sorted(view.hidden_refs) == ["map_edge:secret"]
    assert view.evidence_by_ref["map_edge:secret"] == ["chapter:1"]


def test_book_state_api_defaults_to_latest_but_keeps_explicit_chapter_zero() -> None:
    engine = get_engine(postgres_test_url())
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        project_id = _create_project(session)
        BookStateCompiler(session).compile(
            ApprovedGraphDeltaSet(
                project_id=project_id,
                chapter_number=1,
                graph_deltas=[
                    GraphDelta(
                        id="delta_latest",
                        project_id=project_id,
                        chapter_number=1,
                        node_patches=[
                            NodePatch(
                                node_id="char_mc",
                                node_type="character",
                                op="create",
                                new_value={
                                    "id": "char_mc",
                                    "project_id": project_id,
                                    "node_type": "character",
                                    "state": {"location_id": ""},
                                },
                            )
                        ],
                    )
                ],
            )
        )

    handlers = build_handlers(get_session=Session)

    latest = handlers["get_book_state_runtime"](project_id)
    chapter_zero = handlers["get_book_state_runtime"](project_id, as_of_chapter=0)

    assert latest["schema_version"] == "book_state.runtime.v1"
    assert latest["as_of_chapter"] == 1
    assert chapter_zero["as_of_chapter"] == 0


def test_compiler_blocks_stale_old_value_without_writing_delta() -> None:
    engine = get_engine(postgres_test_url())
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        project_id = _create_project(session)
        repo = BookStateRepository(session)
        repo.create_world_node(WorldNode(id="char_mc", project_id=project_id, node_type="character"))
        repo.append_world_node_state(
            project_id=project_id,
            node_id="char_mc",
            node_type="character",
            as_of_chapter=0,
            state={"status": "idle"},
        )

        result = BookStateCompiler(session).compile(
            ApprovedGraphDeltaSet(
                project_id=project_id,
                chapter_number=1,
                graph_deltas=[
                    GraphDelta(
                        id="delta_bad",
                        project_id=project_id,
                        chapter_number=1,
                        node_patches=[
                            NodePatch(
                                node_id="char_mc",
                                node_type="character",
                                op="set",
                                field_path="state.status",
                                old_value="busy",
                                new_value="injured",
                            )
                        ],
                    )
                ],
            )
        )
        delta_count = session.scalar(select(func.count()).select_from(GraphDeltaRow))

    assert result.committed is False
    assert result.blocked_reasons
    assert delta_count == 0
