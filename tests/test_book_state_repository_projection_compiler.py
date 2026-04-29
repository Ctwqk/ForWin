from __future__ import annotations

from sqlalchemy import func, select

from forwin.book_state import BookStateCompiler, BookStateProjection, BookStateRepository
from forwin.context.assembler import assemble_context
from forwin.api_book_state_routes import build_handlers
from forwin.models import ArcPlanVersion, ChapterPlan, Project
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.book_state import GraphDeltaPatchRow, GraphDeltaRow, WorldNodeStateRow
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
from forwin.state.repo import StateRepository


def _create_project(session, title: str = "BookState 测试") -> str:
    project = Project(
        title=title,
        premise="BookState persistence",
        genre="玄幻",
        setting_summary="黑石城与上古遗迹",
    )
    session.add(project)
    session.flush()
    return project.id


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
        state_count = session.scalar(select(func.count()).select_from(WorldNodeStateRow))

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
