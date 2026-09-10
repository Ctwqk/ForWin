"""Complete model maps must not acquire missing geography from a scaffold."""

import copy
import json
from types import SimpleNamespace

import pytest

from forwin.genesis import BookGenesisService
from forwin.map.genesis_adapter import build_subworld_map_specs_from_genesis


def complete_atlas():
    return {
        "overview": "河谷调查地图",
        "topology_rules": ["两处设施经步道相连"],
        "submaps": [{"id": "valley", "name": "河谷", "scope": "macro_region"}],
        "regions": [
            {"id": "east", "name": "东岸", "subworld_name": "河谷", "level": 1}
        ],
        "nodes": [
            {
                "id": key,
                "name": name,
                "kind": "building",
                "parent_subworld": "valley",
                "parent_region_id": "east",
            }
            for key, name in [("station", "观测站"), ("depot", "样本库")]
        ],
        "edges": [
            {
                "id": "walk",
                "from": "station",
                "to": "depot",
                "duration_text": "约十至二十分钟",
                "mode": "步行",
                "conditions": ["携带取样凭证"],
                "risks": ["涨水关闭"],
            }
        ],
    }


def model_service(payload, calls=None):
    client = SimpleNamespace(api_key="test", codex_enabled=False, last_call_result=None)

    def chat(*args, **kwargs):
        if calls is not None:
            calls.append(kwargs)
        return json.dumps(payload, ensure_ascii=False)

    client.chat = chat
    return BookGenesisService(llm_client=client)


def generate(service):
    return service._call_json_with_trace(
        messages=[{"role": "user", "content": "生成完整地图"}],
        fallback=complete_atlas(),
        stage_key="map",
    )[0]


def test_model_output_schema_includes_geography_needed_by_route_endpoints():
    source, calls = complete_atlas(), []
    payload = generate(model_service(source, calls))
    schema = calls[0]["output_schema"]
    assert set(schema["required"]) == {
        "overview",
        "topology_rules",
        "submaps",
        "regions",
        "nodes",
        "edges",
    }
    assert set(schema["properties"]) == set(source)
    assert (
        payload == source
    )  # Validation must not replace authored values with defaults.
    specs = build_subworld_map_specs_from_genesis(project_id="p", map_atlas=payload)
    assert {node.name for node in specs[0].required_anchor_nodes} == {
        "观测站",
        "样本库",
    }


@pytest.mark.parametrize(
    "missing", ["overview", "topology_rules", "submaps", "regions", "nodes", "edges"]
)
def test_incomplete_generated_map_is_rejected_before_fallback_can_invent_geography(
    missing,
):
    source = complete_atlas()
    del source[missing]
    with pytest.raises(ValueError, match=missing):
        generate(model_service(source))


@pytest.mark.parametrize(
    "damage,path",
    [
        ("endpoint", r"edges\[0\]"),
        ("node_parent", r"nodes\[0\].parent_subworld"),
        ("node_region", r"nodes\[0\].parent_region_id"),
        ("region_world", r"regions\[0\].subworld_name"),
        ("region_parent", r"regions\[0\].parent_region_id"),
        ("duplicate_node", r"nodes\[1\].id"),
        ("wrong_nodes_type", "nodes"),
    ],
)
def test_generated_map_references_and_structure_are_checked_before_normalization(
    damage, path
):
    source = complete_atlas()
    if damage == "endpoint":
        source["edges"][0]["to"] = "missing"
    elif damage == "node_parent":
        source["nodes"][0]["parent_subworld"] = "missing"
    elif damage == "node_region":
        source["nodes"][0]["parent_region_id"] = "missing"
    elif damage == "region_world":
        source["regions"][0]["subworld_name"] = "missing"
    elif damage == "region_parent":
        source["regions"][0].update(level=2, parent_region_id="missing")
    elif damage == "duplicate_node":
        source["nodes"][1]["id"] = source["nodes"][0]["id"]
    else:
        source["nodes"] = {"station": "观测站"}
    with pytest.raises(ValueError, match=path):
        generate(model_service(source))


def test_normalization_preserves_child_before_parent_and_world_id_reference():
    source = complete_atlas()
    source["submaps"].append({"id": "hill", "name": "山地"})
    source["regions"] = [
        {
            "id": "inner",
            "name": "内圈",
            "subworld_name": "hill",
            "level": 2,
            "parent_region_id": "outer",
        },
        {"id": "outer", "name": "外圈", "subworld_name": "hill", "level": 1},
        *source["regions"],
    ]
    source["nodes"][0].update(parent_subworld="hill", parent_region_id="inner")
    result = model_service(source)._normalize_map_payload(
        payload=source, fallback=complete_atlas()
    )
    child = result["regions"][0]
    assert (child["subworld_name"], child["level"], child["parent_region_id"]) == (
        "山地",
        2,
        "outer",
    )
    assert result["nodes"][0]["parent_region_id"] == "inner"


@pytest.mark.parametrize("second_name", ["河谷", "valley"])
def test_normalization_preserves_world_identity_when_names_repeat_or_collide_with_ids(
    second_name,
):
    from forwin.map.genesis_atlas import (
        validate_complete_genesis_map,
        validate_genesis_map_references,
    )

    source = complete_atlas()
    source["submaps"].append({"id": "hill", "name": second_name})
    source["regions"][0]["subworld_name"] = "valley"
    source["regions"].append(
        {"id": "upper", "name": "高地", "subworld_name": "hill", "level": 1}
    )
    source["nodes"][1].update(parent_subworld="hill", parent_region_id="upper")
    validate_complete_genesis_map(source)
    result = model_service(source)._normalize_map_payload(
        payload=source, fallback=complete_atlas()
    )
    validate_genesis_map_references(result)
    assert result["nodes"][0]["parent_subworld"] == "valley"
    assert result["nodes"][1]["parent_subworld"] == "hill"


def test_explicit_empty_geography_does_not_restore_fallback_sections():
    source = complete_atlas()
    source.update(submaps=[], regions=[], nodes=[], edges=[], topology_rules=[])
    result = model_service(source)._normalize_map_payload(
        payload=source, fallback=complete_atlas()
    )
    assert result == source


@pytest.mark.parametrize("bad_map", [None, [], "missing map"])
def test_world_normalization_rejects_an_explicit_non_object_map(bad_map):
    from forwin.models import Project

    project = Project(id="p", title="河谷", premise="调查", genre="悬疑")
    with pytest.raises(ValueError, match="world.map_atlas"):
        model_service({})._normalize_world_root_payload(
            project=project,
            payload={"map_atlas": bad_map},
            fallback={"map_atlas": complete_atlas()},
        )


@pytest.mark.parametrize(
    "damage", ["cross_world_parent", "cross_world_node", "blank_identity"]
)
def test_full_map_cannot_reassign_declared_ownership_or_blank_identity(damage):
    source = complete_atlas()
    source["submaps"].append({"id": "hill", "name": "山地"})
    if damage == "cross_world_parent":
        source["regions"].append(
            {
                "id": "inner",
                "name": "内圈",
                "subworld_name": "hill",
                "level": 2,
                "parent_region_id": "east",
            }
        )
    elif damage == "cross_world_node":
        source["nodes"][0]["parent_subworld"] = "hill"
    else:
        source["nodes"][0]["id"] = " "
        source["edges"] = []
    with pytest.raises(ValueError):
        generate(model_service(source))


def test_complete_refinement_rejects_missing_sections_but_preserves_legacy_route_text():
    source = complete_atlas()
    source["edges"][0] = {
        "from": "station",
        "to": "depot",
        "travel_time": "10分钟",
        "control": "双人取样",
    }
    service = model_service(source)
    common = {
        "messages": [{"role": "user", "content": "更新地图"}],
        "fallback": complete_atlas(),
        "stage_key": "map:refine",
    }
    payload, _ = service._call_json_with_trace(**common)
    assert payload == source
    del source["nodes"]
    with pytest.raises(ValueError, match="nodes"):
        service._call_json_with_trace(**common)


@pytest.mark.parametrize(
    "operation",
    ["generate", "patch", "patch_nonobject", "refine", "refine_item", "lock"],
)
def test_map_with_missing_route_endpoint_cannot_advance_active_revision(operation):
    from sqlalchemy import select

    from forwin.models import Project
    from forwin.models.base import get_engine, get_session_factory, init_db
    from forwin.models.genesis import BookGenesisRevision
    from forwin.state.updater import StateUpdater
    from tests.postgres import postgres_test_url

    source = complete_atlas()
    damaged = copy.deepcopy(source)
    damaged["edges"][0]["to"] = "missing"
    response = damaged["edges"][0] if operation == "refine_item" else damaged
    service = model_service(response)
    engine = get_engine(postgres_test_url("genesis-map-completeness"))
    init_db(engine)
    try:
        with get_session_factory(engine)() as session:
            project = Project(
                id="map-write", title="河谷", premise="核查", genre="悬疑"
            )
            session.add(project)
            session.flush()
            updater = StateUpdater(session)
            revision = service.create_initial_revision(
                session=session, updater=updater, project=project
            )
            pack = service.load_pack(revision)
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
            with pytest.raises(
                ValueError,
                match="world.map_atlas"
                if operation == "patch_nonobject"
                else r"edges\[0\]",
            ):
                if operation == "generate":
                    service.generate_stage(**common, stage_key="map")
                elif operation == "patch":
                    service.patch_pack(
                        **common, patch={"world": {"map_atlas": damaged}}
                    )
                elif operation == "patch_nonobject":
                    service.patch_pack(**common, patch={"world": {"map_atlas": []}})
                elif operation == "lock":
                    service.lock_stage(**common, stage_key="map")
                else:
                    service.refine_stage(
                        **common,
                        stage_key="map",
                        instruction="修订路线",
                        target_path="edges[0]" if operation == "refine_item" else "",
                    )
            session.commit()
            assert project.active_genesis_revision_id == original_id
            assert (
                session.get(BookGenesisRevision, original_id).pack_json == original_json
            )
            assert len(session.scalars(select(BookGenesisRevision)).all()) == 1
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "parent_key", ["parent_subworld_id", "subworld_id", "subworld_name"]
)
def test_import_preserves_existing_node_parent_aliases(parent_key):
    from forwin.map.genesis_adapter import authored_edges_from_atlas

    source = complete_atlas()
    for node in source["nodes"]:
        node[parent_key] = node.pop("parent_subworld")
    source["edges"][0].update(from_subworld_id="valley", to_subworld_id="valley")
    original = copy.deepcopy(source)
    edges = authored_edges_from_atlas(project_id="p", map_atlas=source)
    assert len(edges) == 1
    assert edges[0].metadata["source_route"] == source["edges"][0]
    assert source == original
    with pytest.raises(ValueError, match="nodes"):
        generate(model_service(source))


def test_import_uses_same_id_priority_as_map_validation_for_declared_route_parents():
    from forwin.map.genesis_adapter import authored_edges_from_atlas

    source = complete_atlas()
    source["submaps"].append({"id": "hill", "name": "valley"})
    source["edges"][0].update(from_subworld_id="河谷", to_subworld_id="河谷")
    edges = authored_edges_from_atlas(project_id="p", map_atlas=source)
    assert len(edges) == 1
    assert edges[0].from_node_id == "station"


@pytest.mark.parametrize("bad_field", ["id", "parent_subworld"])
def test_rejected_node_alias_input_preserves_original_dict_and_invalid_type(bad_field):
    from forwin.map.genesis_atlas import validate_genesis_map_references

    source = complete_atlas()
    node = source["nodes"][0]
    node["parent_subworld_id"] = node.pop("parent_subworld")
    node[bad_field] = 0
    original = copy.deepcopy(source)
    with pytest.raises(ValueError, match=bad_field):
        validate_genesis_map_references(source)
    assert source == original
