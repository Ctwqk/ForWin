from __future__ import annotations

import json

import pytest

from forwin.book_state.map_graph import MapGraph
from forwin.context.assembler_core.map_context import _build_genesis_map_overview
from forwin.map.generator import generate_subworld_map
from forwin.map.genesis_adapter import build_subworld_map_specs_from_genesis
from forwin.protocol.context import ReviewContextPack
from forwin.protocol.writer import WriterOutput
from forwin.review.llm_webnovel import LLMWebNovelReviewer
from forwin.review.map_movement import _duration_to_travel_time_budget


def atlas() -> dict:
    return {
        "overview": "现实城市内的办公区，不存在超自然交通。",
        "submaps": [{"id": "city", "name": "城区", "travel_rules": ["门禁登记需20至60分钟"]}],
        "regions": [{"id": "admin", "name": "办公区", "subworld_name": "city"}],
        "nodes": [
            {"id": key, "name": name, "kind": "building", "parent_subworld": "city", "parent_region_id": "admin"}
            for key, name in [("office", "办公楼"), ("archive", "档案馆"), ("finance", "结算处")]
        ],
        "edges": [
            {"id": "walk", "from": "office", "to": "archive", "relation": "步行", "travel_cost": "10分钟；另需20至60分钟登记"},
            {"id": "transfer", "from": "archive", "to": "finance", "relation": "跨楼", "travel_cost": "15分钟；复制手续约半日"},
        ],
    }


def test_authored_routes_survive_materialization_without_invented_shortcuts():
    spec = build_subworld_map_specs_from_genesis(project_id="p", map_atlas=atlas())[0]
    result = generate_subworld_map(spec)
    assert result.validation_report.valid, result.validation_report.errors
    assert {n.name for n in result.map_nodes} == {"办公楼", "档案馆", "结算处"}
    nodes = {n.metadata["source_node_id"]: n.id for n in result.map_nodes}
    graph = MapGraph(nodes=result.map_nodes, edges=result.map_edges)
    assert graph.shortest_path(nodes["office"], nodes["archive"], metric="travel_time").total_travel_time == pytest.approx(10 / 60)
    assert graph.shortest_path(nodes["office"], nodes["finance"], metric="travel_time").total_travel_time == pytest.approx(25 / 60)
    assert {e.metadata["source_edge_id"] for e in result.map_edges} == {"walk", "transfer"}
    assert all(e.edge_type != "portal" for e in result.map_edges)
    assert next(e for e in result.map_edges if e.metadata["source_edge_id"] == "walk").metadata["source_travel_cost"] == "10分钟；另需20至60分钟登记"


def test_gate_registration_does_not_create_portals_in_procedural_fallback():
    source = atlas()
    source["edges"] = []
    spec = build_subworld_map_specs_from_genesis(project_id="p", map_atlas=source)[0]
    assert "传送阵" not in spec.required_connection_roles


def test_unknown_authored_travel_time_stays_explicitly_unknown():
    source = atlas()
    source["edges"][0]["travel_cost"] = "随潮汐而定；办理许可可能一天"
    result = generate_subworld_map(build_subworld_map_specs_from_genesis(project_id="p", map_atlas=source)[0])
    edge = next(e for e in result.map_edges if e.metadata.get("source_edge_id") == "walk")
    assert edge.metadata["travel_time_known"] is False
    assert edge.metadata["source_travel_cost"] == "随潮汐而定；办理许可可能一天"


def test_cross_world_routes_keep_actual_endpoints_and_each_route():
    from forwin.map.service import build_interconnections_from_genesis_atlas

    source = atlas()
    source["submaps"].append({"id": "bank-district", "name": "银行区"})
    source["regions"].append({"id": "bank-region", "name": "银行街", "subworld_name": "bank-district"})
    source["nodes"].append({"id": "bank", "name": "银行", "parent_subworld": "bank-district", "parent_region_id": "bank-region"})
    source["edges"].extend([
        {"id": "office-bank", "from": "office", "to": "bank", "travel_cost": "55分钟"},
        {"id": "finance-bank", "from": "finance", "to": "bank", "travel_cost": "65分钟", "bidirectional": False},
    ])
    specs = build_subworld_map_specs_from_genesis(project_id="p", map_atlas=source)
    connections, origin = build_interconnections_from_genesis_atlas(project_id="p", specs=specs, map_atlas=source)
    assert origin == "atlas_edges"
    assert len(connections) == 2
    assert sorted(c.travel_time for c in connections) == pytest.approx([55 / 60, 65 / 60])
    assert {c.metadata["source_from_ref"] for c in connections} == {"office", "finance"}
    assert all(c.metadata["source_to_ref"] == "bank" for c in connections)
    assert all(c.edge_type != "world_gate" for c in connections)
    assert next(c for c in connections if c.metadata["source_edge_id"] == "finance-bank").bidirectional is False


def test_map_context_and_body_reviewer_receive_authored_travel_constraints():
    overview = _build_genesis_map_overview(atlas(), [])
    assert "办公楼" in overview and "档案馆" in overview
    assert "10分钟；另需20至60分钟登记" in overview
    assert "15分钟；复制手续约半日" in overview
    context = ReviewContextPack(project_title="测试", chapter_number=2, chapter_plan_title="核查", chapter_plan_one_line="核查", genesis_map_overview=overview)
    output = WriterOutput(project_id="p", chapter_number=2, title="核查", body="11:47离开办公楼，11:49进入档案馆。", end_of_chapter_summary="核查")
    payload = LLMWebNovelReviewer()._llm_payload(context, output)
    assert payload["world"]["genesis_map_overview"] == overview
    assert any(item["evidence_id"] == "world:genesis_map" for item in payload["evidence_index"])
    assert payload["draft"]["body"] == output.body
    assert "10分钟" in json.dumps(payload, ensure_ascii=False)


def test_hidden_route_does_not_leak_through_writer_genesis_overview():
    source = atlas()
    source["edges"][0].update(hidden=True, travel_cost="秘密捷径3分钟")
    assert "秘密捷径3分钟" not in _build_genesis_map_overview(source, [])


def test_unknown_route_duration_is_not_rendered_as_instant_travel():
    from types import SimpleNamespace

    from forwin.writer.prompt_core.sections import _map_runtime_section

    context = SimpleNamespace(map_context={"map_node_count": 2, "map_edge_count": 1, "active_locations": [
        {"entity_name": "核查员", "location_name": "办公楼", "nearby_nodes": [
            {"name": "档案馆", "travel_time": None},
        ]},
    ]})
    prompt = _map_runtime_section(context)
    assert "耗时未知" in prompt
    assert "0.0" not in prompt


def test_body_reviewer_receives_current_runtime_route_and_its_source_evidence():
    context = ReviewContextPack(project_title="书", chapter_number=2, chapter_plan_title="核查", chapter_plan_one_line="核查", map_context={
        "objective_review_graph": {"available": True, "map_nodes": [{"id": "a", "name": "办公楼"}, {"id": "b", "name": "档案馆"}], "map_edges": [
            {"id": "ab", "from_node_id": "a", "to_node_id": "b", "travel_time": 1 / 6, "metadata": {"source_travel_cost": "10分钟；登记另计", "travel_time_known": True}},
        ]},
    })
    output = WriterOutput(project_id="p", chapter_number=2, title="核查", body="从办公楼到档案馆。", end_of_chapter_summary="核查")
    payload = LLMWebNovelReviewer()._llm_payload(context, output)
    assert payload["world"]["map_context"]["objective_review_graph"]["map_edges"][0]["metadata"]["source_travel_cost"] == "10分钟；登记另计"
    assert any(item["evidence_id"] == "world:map" for item in payload["evidence_index"])


def test_explicit_node_topology_does_not_generate_unbound_key_location_hints():
    source = atlas()
    source["submaps"][0]["key_locations"] = ["办公楼", "以后可能去的旧渡口"]
    result = generate_subworld_map(build_subworld_map_specs_from_genesis(project_id="p", map_atlas=source)[0])
    assert {n.name for n in result.map_nodes} == {"办公楼", "档案馆", "结算处"}


def test_partial_broken_route_is_not_silently_replaced_by_procedural_map():
    source = atlas()
    source["edges"][0]["to"] = "missing-location"
    with pytest.raises(ValueError, match="endpoint"):
        build_subworld_map_specs_from_genesis(project_id="p", map_atlas=source)


def test_ambiguous_location_name_does_not_select_an_arbitrary_endpoint():
    source = atlas()
    source["nodes"][2]["name"] = "档案馆"
    source["edges"][0]["to"] = "档案馆"
    with pytest.raises(ValueError, match="endpoint"):
        build_subworld_map_specs_from_genesis(project_id="p", map_atlas=source)


def test_disconnected_authored_map_keeps_existing_validation_failure():
    source = atlas()
    source["edges"] = source["edges"][:1]
    result = generate_subworld_map(build_subworld_map_specs_from_genesis(project_id="p", map_atlas=source)[0])
    assert not result.validation_report.valid
    assert any("disconnected" in error for error in result.validation_report.errors)
    assert len(result.map_edges) == 1


def test_real_map_persistence_keeps_route_endpoints_and_unknown_context():
    from types import SimpleNamespace

    from forwin.context.assembler_core.map_context import _build_map_context
    from forwin.map.service import (
        ensure_book_map_from_genesis_atlas,
        get_book_map_runtime,
    )
    from forwin.models import Project
    from forwin.models.base import get_engine, get_session_factory, init_db
    from tests.postgres import postgres_test_url

    source = atlas()
    source["edges"][0]["travel_cost"] = "随潮汐而定"
    source["submaps"].append({"id": "bank-district", "name": "银行区"})
    source["regions"].append({"id": "bank-region", "name": "银行街", "subworld_name": "bank-district"})
    source["nodes"].append({"id": "bank", "name": "银行", "parent_subworld": "bank-district", "parent_region_id": "bank-region"})
    source["edges"].extend([
        {"id": "office-bank", "from": "office", "to": "bank", "travel_cost": "55分钟"},
        {"id": "finance-bank", "from": "finance", "to": "bank", "travel_cost": "65分钟", "bidirectional": False,
         "status": "open", "visibility": "hidden", "discovered_by_default": False, "access_rule_id": "bank-permit"},
    ])
    engine = get_engine(postgres_test_url())
    init_db(engine)
    with get_session_factory(engine)() as session:
        session.add(Project(id="p", title="书", premise="核查"))
        session.commit()
        result = ensure_book_map_from_genesis_atlas(session, project_id="p", map_atlas=source)
        assert result.validation_report.valid, result.validation_report.errors
        runtime = get_book_map_runtime(session, "p")
        nodes = {node.metadata["source_node_id"]: node.id for node in runtime.map_nodes_by_id.values()}
        edges = {edge.metadata["source_edge_id"]: edge for edge in runtime.map_edges_by_id.values()}
        assert set(nodes) == {"office", "archive", "finance", "bank"}
        assert set(edges) == {"walk", "transfer", "office-bank", "finance-bank"}
        assert (edges["office-bank"].from_node_id, edges["office-bank"].to_node_id) == (nodes["office"], nodes["bank"])
        assert (edges["finance-bank"].from_node_id, edges["finance-bank"].to_node_id) == (nodes["finance"], nodes["bank"])
        assert edges["office-bank"].travel_time == pytest.approx(55 / 60)
        assert edges["finance-bank"].travel_time == pytest.approx(65 / 60)
        assert edges["finance-bank"].bidirectional is False
        assert edges["finance-bank"].status == "open"
        assert edges["finance-bank"].visibility_default == "hidden"
        assert edges["finance-bank"].discovered_by_default is False
        assert edges["finance-bank"].access_rule_id == "bank-permit"
        context = _build_map_context(session, "p", [SimpleNamespace(entity_id="person", name="核查员", current_state={"location_id": nodes["office"]})])
        neighbor = next(n for n in context["active_locations"][0]["nearby_nodes"] if n["node_id"] == nodes["archive"])
        assert neighbor["travel_time"] is None
        payload_edge = next(e for e in context["review_graph"]["map_edges"] if e["id"] == edges["walk"].id)
        assert payload_edge["metadata"]["travel_time_known"] is False
        assert edges["finance-bank"].id not in {e["id"] for e in context["review_graph"]["map_edges"]}


def test_disconnected_authored_book_rolls_back_every_map_write():
    from forwin.map.repository import MapRepository
    from forwin.map.service import ensure_book_map_from_genesis_atlas
    from forwin.models import Project
    from forwin.models.base import get_engine, get_session_factory, init_db
    from tests.postgres import postgres_test_url

    source = atlas()
    source["edges"] = source["edges"][:1]
    engine = get_engine(postgres_test_url())
    init_db(engine)
    with get_session_factory(engine)() as session:
        session.add(Project(id="p", title="书", premise="核查"))
        session.commit()
        result = ensure_book_map_from_genesis_atlas(session, project_id="p", map_atlas=source)
        assert not result.validation_report.valid
        assert any("disconnected" in error for error in result.validation_report.errors)
        session.commit()
        repo = MapRepository(session)
        assert repo.list_map_nodes("p") == []
        assert repo.list_map_edges("p") == []
        assert session.get(Project, "p").title == "书"


def test_no_default_gate_when_authored_worlds_have_no_cross_route():
    from forwin.map.service import build_interconnections_from_genesis_atlas

    source = atlas()
    source["submaps"].append({"id": "island", "name": "离岛"})
    source["regions"].append({"id": "island-region", "name": "岛区", "subworld_name": "island"})
    source["nodes"].append({"id": "island-dock", "name": "岛港", "parent_subworld": "island", "parent_region_id": "island-region"})
    specs = build_subworld_map_specs_from_genesis(project_id="p", map_atlas=source)
    connections, origin = build_interconnections_from_genesis_atlas(project_id="p", specs=specs, map_atlas=source)
    assert connections == [] and origin == "atlas_edges"


def test_mixed_node_and_subworld_routes_are_both_preserved():
    from forwin.map.service import build_interconnections_from_genesis_atlas

    source = atlas()
    source["submaps"].append({"id": "island", "name": "离岛"})
    source["regions"].append({"id": "island-region", "name": "岛区", "subworld_name": "island"})
    source["nodes"].append({"id": "island-dock", "name": "岛港", "parent_subworld": "island", "parent_region_id": "island-region"})
    source["edges"].extend([
        {"id": "ferry", "from": "office", "to": "island-dock", "travel_cost": "2小时", "kind": "sea_lane"},
        {"id": "explicit-world-gate", "from": "city", "to": "island", "kind": "world_gate"},
    ])
    specs = build_subworld_map_specs_from_genesis(project_id="p", map_atlas=source)
    connections, origin = build_interconnections_from_genesis_atlas(project_id="p", specs=specs, map_atlas=source)
    assert origin == "atlas_edges"
    assert {edge.metadata["source_edge_id"] for edge in connections} == {"ferry", "explicit-world-gate"}


def test_authored_book_checks_connectivity_after_cross_world_routes_exist():
    from forwin.map.service import (
        ensure_book_map_from_genesis_atlas,
        get_book_map_runtime,
    )
    from forwin.models import Project
    from forwin.models.base import get_engine, get_session_factory, init_db
    from tests.postgres import postgres_test_url

    source = atlas()
    source["submaps"].append({"id": "other", "name": "对岸"})
    source["regions"].append({"id": "other-region", "name": "对岸街区", "subworld_name": "other"})
    source["nodes"][2].update(parent_subworld="other", parent_region_id="other-region")
    source["edges"] = [
        {"from": "office", "to": "finance", "travel_cost": "10分钟"},
        {"from": "finance", "to": "archive", "travel_cost": "15分钟"},
    ]
    engine = get_engine(postgres_test_url())
    init_db(engine)
    with get_session_factory(engine)() as session:
        session.add(Project(id="p", title="书", premise="核查"))
        session.commit()
        result = ensure_book_map_from_genesis_atlas(session, project_id="p", map_atlas=source)
        assert result.validation_report.valid, result.validation_report.errors
        runtime = get_book_map_runtime(session, "p")
        assert len(runtime.map_nodes_by_id) == 3 and len(runtime.map_edges_by_id) == 2
        nodes = {n.name: n.id for n in runtime.map_nodes_by_id.values()}
        graph = MapGraph(nodes=list(runtime.map_nodes_by_id.values()), edges=list(runtime.map_edges_by_id.values()))
        assert graph.shortest_path(nodes["办公楼"], nodes["档案馆"], metric="travel_time").total_travel_time == pytest.approx(25 / 60)


def test_distinct_routes_without_ids_get_stable_distinct_identities():
    source = atlas()
    source["edges"][0].pop("id")
    source["edges"].append({"from": "office", "to": "archive", "relation": "摆渡", "travel_cost": "5分钟"})
    specs = build_subworld_map_specs_from_genesis(project_id="p", map_atlas=source)
    result = generate_subworld_map(specs[0])
    again = generate_subworld_map(build_subworld_map_specs_from_genesis(project_id="p", map_atlas=source)[0])
    assert len(result.map_edges) == 3
    assert {e.id for e in result.map_edges} == {e.id for e in again.map_edges}
    assert len({e.id for e in result.map_edges}) == 3


@pytest.mark.parametrize("project_id", ["p", "p3"])
@pytest.mark.parametrize("reverse_nodes", [False, True])
def test_authored_directed_connectivity_is_independent_of_node_order(project_id, reverse_nodes):
    from forwin.map.validator import book_map_connectivity_errors

    source = atlas()
    for edge in source["edges"]:
        edge["bidirectional"] = False
    if reverse_nodes:
        source["nodes"].reverse()
    result = generate_subworld_map(build_subworld_map_specs_from_genesis(project_id=project_id, map_atlas=source)[0])
    assert result.validation_report.valid, result.validation_report.errors
    assert book_map_connectivity_errors(sorted(result.map_nodes, key=lambda n: n.id), result.map_edges) == []
    nodes = {node.metadata["source_node_id"]: node.id for node in result.map_nodes}
    graph = MapGraph(nodes=result.map_nodes, edges=result.map_edges)
    assert graph.shortest_path(nodes["office"], nodes["finance"]).reachable
    assert not graph.shortest_path(nodes["finance"], nodes["office"]).reachable


@pytest.mark.parametrize("available", [True, False])
def test_writer_gets_current_routes_even_when_active_locations_are_unresolved(available):
    from types import SimpleNamespace

    from forwin.writer.prompt_core.sections import _map_runtime_section

    context = SimpleNamespace(map_context={"map_node_count": 2, "map_edge_count": 1, "active_locations": [], "review_graph": {
        "available": available, "map_nodes": [{"id": "a", "name": "办公楼"}, {"id": "b", "name": "档案馆"}], "map_edges": [
            {"id": "ab", "from_node_id": "a", "to_node_id": "b", "travel_time": 1.0, "status": "open", "access_rule_id": "registered-permit", "metadata": {"source_travel_cost": "60分钟；持新通行证"}},
        ],
    }})
    prompt = _map_runtime_section(context)
    assert "办公楼" in prompt and "档案馆" in prompt
    assert "1小时" in prompt and "60分钟；持新通行证" in prompt
    assert "registered-permit" in prompt
    if not available:
        assert "不完整" in prompt


@pytest.mark.parametrize("text,hours", [
    ("从十一点四十七分推进至十二点，历时十三分钟。", 13 / 60),
    ("历时25分钟", 25 / 60),
    ("三十五分钟", 35 / 60),
    ("一个半小时", 1.5),
    ("一个小时", 1.0),
    ("两个小时", 2.0),
    ("2个小时", 2.0),
    ("两小时三十分钟", 2.5),
    ("半小时", 0.5),
    ("半日又两小时", 14.0),
    ("两天", 48.0),
    ("时长未知", None),
    ("十一点四十七分至十二点", None),
])
def test_movement_duration_reads_units_without_confusing_wall_clock(text, hours):
    result = _duration_to_travel_time_budget(text)
    assert result == pytest.approx(hours) if hours is not None else result is None


@pytest.mark.parametrize("fields", [
    {"status": "open", "visibility": "hidden"},
    {"status": "open", "visibility_default": "hidden"},
    {"status": "open", "discovered_by_default": False},
    {"status": "secret"},
])
def test_authored_visibility_and_access_survive_runtime_and_context(fields):
    from forwin.context.assembler_core.map_context import _map_edge_payload
    from forwin.map.visibility import is_writer_visible_map_edge

    source = atlas()
    source["edges"][0].update(fields, access_rule_id="registered-permit")
    result = generate_subworld_map(build_subworld_map_specs_from_genesis(project_id="p", map_atlas=source)[0])
    edge = next(e for e in result.map_edges if e.metadata["source_edge_id"] == "walk")
    assert not is_writer_visible_map_edge(edge)
    assert edge.access_rule_id == "registered-permit"
    assert _map_edge_payload(edge)["access_rule_id"] == "registered-permit"
    assert "10分钟；另需20至60分钟登记" not in _build_genesis_map_overview(source, [])


def test_map_movement_resolves_stable_genesis_node_ids():
    from forwin.protocol.scene import SceneOutput
    from forwin.protocol.state_change import TimeAdvance
    from forwin.review.map_movement import MapMovementReviewer

    result = generate_subworld_map(build_subworld_map_specs_from_genesis(project_id="p", map_atlas=atlas())[0])
    context = ReviewContextPack(project_title="书", chapter_number=2, chapter_plan_title="核查", chapter_plan_one_line="核查", map_context={"review_graph": {
        "available": True, "map_nodes": [n.model_dump(mode="json") for n in result.map_nodes], "map_edges": [e.model_dump(mode="json") for e in result.map_edges],
    }})
    output = WriterOutput(project_id="p", chapter_number=2, title="核查", body="从档案馆赶到结算处。", end_of_chapter_summary="核查", scene_outputs=[
        SceneOutput(scene_no=1, scene_location_id="archive", scene_objective="离开", text="离开。"),
        SceneOutput(scene_no=2, scene_location_id="finance", scene_objective="抵达", text="抵达。"),
    ], time_advance=TimeAdvance(new_time_label="中午", duration_description="十三分钟"))
    verdict = MapMovementReviewer().review(context, output)
    assert verdict.verdict == "fail"
    assert verdict.issues[0].rule_name == "map_travel_time_exceeds_chapter_time"
