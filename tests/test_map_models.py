from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy import inspect

from forwin.map.protocol import MapEdge, MapNode, RegionNode
from forwin.map.repository import MapRepository
from forwin.models import Project, SubWorld
from forwin.models.base import get_engine, get_session_factory, init_db


def test_init_db_exposes_graph_map_tables_and_columns() -> None:
    engine = get_engine(postgres_test_url())
    init_db(engine)
    inspector = inspect(engine)

    assert {"map_regions", "map_region_edges", "map_generation_runs"}.issubset(inspector.get_table_names())
    assert {"subworld_type", "scale_level", "culture_profile_json", "terrain_profile_json", "danger_profile_json", "generation_seed", "map_status"}.issubset(
        {column["name"] for column in inspector.get_columns("sub_worlds")}
    )
    assert {"subworld_id", "region_id", "description"}.issubset(
        {column["name"] for column in inspector.get_columns("map_nodes")}
    )
    assert "subworld_id" in {column["name"] for column in inspector.get_columns("map_edges")}


def test_repository_persists_region_node_and_non_negative_edge() -> None:
    engine = get_engine(postgres_test_url())
    init_db(engine)
    Session = get_session_factory(engine)
    with Session() as session:
        session.add(Project(id="p1", title="书", premise="premise"))
        session.add(SubWorld(id="sw1", project_id="p1", name="苍澜大陆", subworld_type="continent"))
        session.commit()
        repo = MapRepository(session)
        region = RegionNode(
            id="region_core",
            project_id="p1",
            subworld_id="sw1",
            region_type="kingdom",
            name="帝国核心区",
        )
        node_a = MapNode(
            id="loc_a",
            project_id="p1",
            subworld_id="sw1",
            region_id="region_core",
            node_type="settlement",
            name="帝都",
        )
        node_b = MapNode(
            id="loc_b",
            project_id="p1",
            subworld_id="sw1",
            region_id="region_core",
            node_type="waypoint",
            name="东门",
        )
        edge = MapEdge(
            id="edge_ab",
            project_id="p1",
            subworld_id="sw1",
            from_node_id="loc_a",
            to_node_id="loc_b",
            edge_type="road",
            distance=2,
            travel_time=1,
        )

        repo.upsert_region(region)
        repo.upsert_map_node(node_a)
        repo.upsert_map_node(node_b)
        repo.upsert_map_edge(edge)

        assert repo.list_regions("p1", "sw1")[0].name == "帝国核心区"
        assert repo.list_map_nodes("p1", "sw1")[0].subworld_id == "sw1"
        assert repo.list_map_edges("p1", "sw1")[0].travel_time == 1


def test_map_edge_rejects_negative_weights() -> None:
    with pytest.raises(ValidationError):
        MapEdge(
            id="bad",
            project_id="p1",
            subworld_id="sw1",
            from_node_id="a",
            to_node_id="b",
            edge_type="road",
            distance=-1,
        )
