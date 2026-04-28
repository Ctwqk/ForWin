from __future__ import annotations

from sqlalchemy import inspect

from forwin.models.base import get_engine, init_db


def test_init_db_exposes_final_book_state_tables() -> None:
    engine = get_engine(postgres_test_url())

    init_db(engine)

    table_names = set(inspect(engine).get_table_names())
    assert {
        "world_nodes",
        "world_node_states",
        "world_edges",
        "fact_nodes",
        "map_nodes",
        "map_edges",
        "map_regions",
        "map_region_edges",
        "map_generation_runs",
        "graph_deltas",
        "graph_delta_patches",
        "cognition_overlays",
        "cognition_overlay_patches",
        "world_snapshots",
        "map_snapshots",
        "book_cognition_snapshots",
        "narrative_nodes",
        "narrative_edges",
    }.issubset(table_names)
