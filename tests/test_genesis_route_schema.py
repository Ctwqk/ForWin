"""One route contract must retain author information across production owners."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from forwin.context.assembler_core.map_context import _build_genesis_map_overview
from forwin.genesis import BookGenesisService
from forwin.map.generator import generate_subworld_map
from forwin.map.genesis_adapter import build_subworld_map_specs_from_genesis
from forwin.protocol.context import ChapterContextPack
from forwin.protocol.scene import ScenePlan
from forwin.writer.prompt_core import (
    build_preview_chapter_prompt,
    build_scene_breakdown_prompt,
    build_scene_generation_prompt,
    build_scene_stitch_prompt,
    build_single_chapter_draft_prompt,
)
from tests.test_genesis_route_contract import atlas


def imported(fields):
    source = atlas()
    source["edges"][0] = {"id": "walk", "from": "office", "to": "archive", **fields}
    result = generate_subworld_map(
        build_subworld_map_specs_from_genesis(project_id="p", map_atlas=source)[0]
    )
    return (
        source,
        result,
        next(e for e in result.map_edges if e.metadata["source_edge_id"] == "walk"),
    )


@pytest.mark.parametrize("stage", ["single", "preview", "breakdown", "scene", "stitch"])
def test_generated_route_shape_reaches_each_writer_with_its_endpoints(stage):
    source, result, edge = imported(
        {
            "travel_time": "45分钟",
            "mode": "渡船转步行",
            "constraints": ["持本次调阅函", "第二名保管员同行"],
            "risk": "封航时关闭",
        }
    )
    assert edge.metadata["travel_time_known"] is True
    assert edge.travel_time == pytest.approx(0.75)
    assert edge.metadata["source_route"] == source["edges"][0]
    context = ChapterContextPack(
        project_id="p",
        project_title="调查",
        premise="核对原件",
        genre="悬疑",
        setting_summary="城市",
        chapter_number=1,
        chapter_plan_title="调档",
        chapter_plan_one_line="核验签字",
        chapter_goals=[],
        map_context={
            "map_node_count": len(result.map_nodes),
            "map_edge_count": len(result.map_edges),
            "review_graph": {
                "available": False,
                "map_nodes": [n.model_dump(mode="json") for n in result.map_nodes],
                "map_edges": [e.model_dump(mode="json") for e in result.map_edges],
            },
        },
    )
    builders = {
        "single": lambda: build_single_chapter_draft_prompt(context),
        "preview": lambda: build_preview_chapter_prompt(context),
        "breakdown": lambda: build_scene_breakdown_prompt(context),
        "scene": lambda: build_scene_generation_prompt(
            context, ScenePlan(scene_no=1, objective="核验签字")
        ),
        "stitch": lambda: build_scene_stitch_prompt(context, []),
    }
    text = "\n".join(m["content"] for m in builders[stage]())
    overview = _build_genesis_map_overview(source, [])
    for prompt in (text, overview):
        route_line = next(
            line for line in prompt.splitlines() if "办公楼↔档案馆" in line
        )
        assert all(
            value in route_line
            for value in ["45分钟", "渡船转步行", "持本次调阅函", "第二名保管员同行"]
        )
        assert "封航时关闭" in prompt


@pytest.mark.parametrize(
    "fields,hours",
    [
        (
            {
                "duration_text": "一小时二十分钟",
                "conditions": ["登记后通过"],
                "risks": ["雨天关闭"],
            },
            4 / 3,
        ),
        ({"travel_time": 0.5}, 0.5),
        ({"travel_time": "30分钟"}, 0.5),
        ({"travel_time": 0.5, "travel_cost": "30分钟；登记另计"}, 0.5),
        ({"travel_time": 0.5, "travel_cost": "一枚渡资；须登记"}, 0.5),
        ({"duration_text": "随潮汐而定", "travel_time": "随潮汐而定"}, None),
        ({"travel_time": "30"}, None),
        ({"travel_time": "35—55分钟"}, None),
    ],
)
def test_duration_sources_keep_units_and_uncertainty(fields, hours):
    source, _, edge = imported(fields)
    assert edge.metadata["source_route"] == source["edges"][0]
    assert edge.metadata["travel_time_known"] is (hours is not None)
    if hours is not None:
        assert edge.travel_time == pytest.approx(hours)
    for key in ("duration_text", "travel_time", "travel_cost"):
        if isinstance(fields.get(key), str):
            assert fields[key] in edge.metadata["source_travel_cost"]


@pytest.mark.parametrize(
    "fields",
    [
        {"travel_time": 0.5, "travel_cost": "45分钟"},
        {"travel_time": 0.5, "travel_cost": "40—60分钟"},
        {"travel_time": 0.5, "travel_cost": "约半小时；需渡资"},
        {"duration_text": "45分钟", "travel_time": "30分钟"},
        {"travel_time": True},
        {"travel_time": float("inf")},
        {"constraints": {"permission": "需批准"}},
        {"constraints": ["需批准", 7]},
        {"hidden": ["true"]},
        {"discovered_by_default": []},
        {"bidirectional": "maybe"},
        {"status": {}},
        {"visibility_default": "mysterious"},
        {"travel_time": "30分钟", "unhandled_access": "必须二人同行"},
    ],
)
def test_inconsistent_or_unhandled_author_fields_are_not_silently_imported(fields):
    with pytest.raises(ValueError, match=r"edges\[0\]"):
        imported(fields)


def test_legacy_node_reference_and_parent_are_distinct():
    source = atlas()
    source["edges"][0] = {
        "id": "walk",
        "from_node_id": "office",
        "from_subworld_id": "city",
        "to_node_id": "archive",
        "to_subworld_id": "city",
        "travel_time": 0.5,
    }
    result = generate_subworld_map(
        build_subworld_map_specs_from_genesis(project_id="p", map_atlas=source)[0]
    )
    assert (
        next(
            e for e in result.map_edges if e.metadata["source_edge_id"] == "walk"
        ).travel_time
        == 0.5
    )
    source["edges"][0]["from_subworld_id"] = "different-world"
    with pytest.raises(ValueError, match="from_subworld"):
        build_subworld_map_specs_from_genesis(project_id="p", map_atlas=source)


@pytest.mark.parametrize(
    "protection",
    [
        {"discovered_by_default": False},
        {"status": "closed"},
        {"edge_type": "hidden_route"},
    ],
)
def test_subworld_routes_keep_individual_facts_and_visibility_after_persistence(
    protection,
):
    from forwin.map.service import (
        ensure_book_map_from_genesis_atlas,
        get_book_map_runtime,
    )
    from forwin.map.visibility import is_writer_visible_map_edge
    from forwin.models import Project
    from forwin.models.base import get_engine, get_session_factory, init_db
    from tests.postgres import postgres_test_url

    source = atlas()
    source["submaps"].append({"id": "island", "name": "离岛"})
    source["regions"].append(
        {"id": "island-region", "name": "岛区", "subworld_name": "island"}
    )
    source["nodes"].append(
        {
            "id": "island-dock",
            "name": "岛港",
            "parent_subworld": "island",
            "parent_region_id": "island-region",
        }
    )
    source["edges"] += [
        {
            "id": "ferry-private",
            "from": "city",
            "to": "island",
            "travel_time": "45分钟",
            "mode": "渡船",
            "constraints": ["调阅函"],
            "risk": "封航",
            **protection,
        },
        {
            "id": "ferry-public",
            "from": "city",
            "to": "island",
            "travel_time": "随潮汐而定",
        },
    ]
    engine = get_engine(postgres_test_url("genesis-coarse-route"))
    init_db(engine)
    try:
        with get_session_factory(engine)() as session:
            session.add(Project(id="p", title="跨海", premise="调档", genre="悬疑"))
            session.flush()
            result = ensure_book_map_from_genesis_atlas(
                session, project_id="p", map_atlas=source
            )
            assert result.validation_report.valid, result.validation_report.errors
            edges = {
                e.metadata.get("source_edge_id"): e
                for e in get_book_map_runtime(session, "p").map_edges_by_id.values()
            }
            private, public = edges["ferry-private"], edges["ferry-public"]
            assert private.travel_time == 0.75
            assert private.metadata["source_mode"] == "渡船"
            assert private.metadata["source_control"] == "调阅函"
            assert private.metadata["source_hazard"] == "封航"
            assert not is_writer_visible_map_edge(private)
            assert is_writer_visible_map_edge(public)
            assert public.metadata["travel_time_known"] is False
            assert public.id != private.id
    finally:
        engine.dispose()


@pytest.mark.parametrize("bad_edges", ["broken", [123], [None], {}, None])
def test_invalid_raw_edges_cannot_be_filtered_into_a_procedural_map(bad_edges):
    source = atlas()
    source["edges"] = bad_edges
    service = BookGenesisService(llm_client=SimpleNamespace())
    with pytest.raises(ValueError, match="edges"):
        service._normalize_map_payload(payload=source, fallback=atlas(), world_bible={})


def test_explicit_empty_edges_and_missing_edges_are_distinct():
    service = BookGenesisService(llm_client=SimpleNamespace())
    source = atlas()
    source["edges"] = []
    assert (
        service._normalize_map_payload(
            payload=source, fallback=atlas(), world_bible={}
        )["edges"]
        == []
    )
    del source["edges"]
    assert (
        service._normalize_map_payload(
            payload=source, fallback=atlas(), world_bible={}
        )["edges"]
        == atlas()["edges"]
    )


def test_damaged_hidden_route_is_not_disclosed_by_preview_diagnostics():
    source = atlas()
    source["edges"][0].update(hidden=True, unknown_secret="秘密密令不能输出")
    overview = _build_genesis_map_overview(source, [])
    assert "秘密密令不能输出" not in overview
    assert "办公楼↔档案馆" not in overview


def test_legacy_type_hidden_route_is_not_disclosed_by_preview():
    source = atlas()
    source["edges"][0].update(type="hidden_route", constraints=["隐藏路线仅限暗号"])
    assert "隐藏路线仅限暗号" not in _build_genesis_map_overview(source, [])


def test_unsupported_visible_route_read_preserves_source_and_reports_incomplete_contract():
    source = atlas()
    source["edges"][0]["unhandled_access"] = "未经解读的许可"
    original = copy.deepcopy(source)
    overview = _build_genesis_map_overview(source, [])
    assert source == original
    assert "unhandled_access" in overview and "无法解析" in overview
    assert "办公楼↔档案馆：10分钟" not in overview


def test_invalid_generated_route_does_not_fall_back_to_a_different_map():
    source = atlas()
    source["topology_rules"] = []
    source["edges"][0]["unhandled_access"] = "需要额外许可"
    calls = []
    client = SimpleNamespace(api_key="test", codex_enabled=False, last_call_result=None)

    def chat(*args, **kwargs):
        calls.append(kwargs)
        return json.dumps(source)

    client.chat = chat
    service = BookGenesisService(llm_client=client)
    with pytest.raises(ValueError, match="edges"):
        service._call_json_with_trace(
            messages=[{"role": "user", "content": "地图"}],
            fallback=atlas(),
            stage_key="map",
        )
    assert len(calls) <= 2
    assert (
        calls[0]["output_schema"]["properties"]["edges"]["items"][
            "additionalProperties"
        ]
        is False
    )


def test_new_full_map_generation_must_declare_its_routes():
    client = SimpleNamespace(api_key="test", codex_enabled=False, last_call_result=None)
    client.chat = lambda *args, **kwargs: json.dumps({"overview": "没有声明路线"})
    service = BookGenesisService(llm_client=client)
    with pytest.raises(ValueError, match="edges"):
        service._call_json_with_trace(
            messages=[{"role": "user", "content": "地图"}],
            fallback=atlas(),
            stage_key="map",
        )


@pytest.mark.parametrize("operation", ["patch", "refine", "refine_item", "lock"])
def test_invalid_route_write_keeps_active_revision_and_original_evidence(
    operation, monkeypatch
):
    from sqlalchemy import select

    from forwin.models import Project
    from forwin.models.base import get_engine, get_session_factory, init_db
    from forwin.models.genesis import BookGenesisRevision
    from forwin.state.updater import StateUpdater
    from tests.postgres import postgres_test_url

    engine = get_engine(postgres_test_url("genesis-route-write"))
    init_db(engine)
    service = BookGenesisService(
        llm_client=SimpleNamespace(api_key="", codex_enabled=False)
    )
    try:
        with get_session_factory(engine)() as session:
            project = Project(
                id="route-write", title="路线合同", premise="调查", genre="悬疑"
            )
            session.add(project)
            session.flush()
            updater = StateUpdater(session)
            revision = service.create_initial_revision(
                session=session, updater=updater, project=project
            )
            pack = service.load_pack(revision)
            source = atlas()
            damaged = copy.deepcopy(source)
            damaged["edges"][0]["unhandled_access"] = "需要双人证明"
            # Model an immutable legacy revision created before this validation.
            pack["world"]["map_atlas"] = damaged if operation == "lock" else source
            revision.pack_json = json.dumps(pack, ensure_ascii=False)
            session.commit()
            original_id, original_json = revision.id, revision.pack_json
            common = {
                "session": session,
                "updater": updater,
                "project": project,
                "revision": revision,
            }
            if operation.startswith("refine"):
                response = (
                    damaged["edges"][0] if operation == "refine_item" else damaged
                )
                monkeypatch.setattr(
                    service, "_call_json_with_trace", lambda **kwargs: (response, {})
                )
            with pytest.raises(ValueError, match=r"edges\[0\].*unhandled_access"):
                if operation == "patch":
                    service.patch_pack(
                        **common, patch={"world": {"map_atlas": damaged}}
                    )
                elif operation == "lock":
                    service.lock_stage(**common, stage_key="map")
                else:
                    service.refine_stage(
                        **common,
                        stage_key="map",
                        instruction="修订通行条件",
                        target_path="edges[0]" if operation == "refine_item" else "",
                    )
            session.commit()  # Rejection must not leave a partial revision to commit.
            assert project.active_genesis_revision_id == original_id
            assert (
                session.get(BookGenesisRevision, original_id).pack_json == original_json
            )
            assert len(session.scalars(select(BookGenesisRevision)).all()) == 1
            assert service.load_pack(revision)["world"]["map_atlas"] == (
                damaged if operation == "lock" else source
            )
    finally:
        engine.dispose()


def test_legacy_invalid_route_handoff_leaves_no_partial_writing_state():
    from sqlalchemy import select

    from forwin.genesis.handoff.commands import StartWritingCommand
    from forwin.map.repository import MapRepository
    from forwin.models.base import get_engine, get_session_factory, init_db
    from forwin.models.project import ArcPlanVersion, ChapterPlan
    from forwin.models.task import GenerationTask
    from forwin.state.updater import StateUpdater
    from tests.postgres import postgres_test_url
    from tests.test_genesis_handoff_service import GenesisHandoffServiceTests

    engine = get_engine(postgres_test_url("genesis-route-handoff"))
    init_db(engine)
    fixture = GenesisHandoffServiceTests()
    service = fixture._service()
    try:
        with get_session_factory(engine)() as session:
            project = fixture._create_ready_project(
                session, service, project_id="legacy-route"
            )
            revision = service.active_revision(session, project)
            pack = service.load_pack(revision)
            pack["world"]["map_atlas"] = atlas()
            pack["world"]["map_atlas"]["edges"][0]["unhandled_access"] = "双人证明"
            revision.pack_json = json.dumps(pack, ensure_ascii=False)
            session.commit()
            original_id, original_json = revision.id, revision.pack_json
            with pytest.raises(ValueError, match="unhandled_access"):
                service.handoff.start_writing(
                    session=session,
                    updater=StateUpdater(session),
                    command=StartWritingCommand(
                        project_id=project.id, actor_type="manual_ui"
                    ),
                )
            session.commit()
            assert project.creation_status == "genesis_ready"
            assert project.active_genesis_revision_id == original_id
            assert service.active_revision(session, project).pack_json == original_json
            for model in (ArcPlanVersion, ChapterPlan, GenerationTask):
                assert session.scalars(select(model)).all() == []
            assert MapRepository(session).list_map_edges(project.id) == []
            assert MapRepository(session).list_map_nodes(project.id) == []
    finally:
        engine.dispose()
