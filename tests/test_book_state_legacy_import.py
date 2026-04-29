from __future__ import annotations

import json

from sqlalchemy import func, select

from forwin.book_state import LegacyBookStateImporter
from forwin.map.models import MapNodeRow, MapRegionRow
from forwin.models import Project
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.book_state import NarrativeNodeRow, WorldEdgeRow, WorldNodeRow, WorldNodeStateRow
from forwin.models.entity import Entity, EntityState, RelationEdge
from forwin.models.subworld import SubWorld
from forwin.protocol.world_v4 import (
    DeltaSource,
    DeltaSourceType,
    RevealEvent,
    VisibilityState,
    WorldLine,
)
from forwin.world_model_v4.repository import WorldModelRepository


def test_legacy_import_maps_entities_relations_and_v4_narrative_nodes() -> None:
    engine = get_engine(postgres_test_url())
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        project = Project(
            title="legacy import",
            premise="旧状态迁移",
            genre="科幻",
            setting_summary="母星线",
        )
        session.add(project)
        session.flush()
        project_id = project.id

        char = Entity(
            project_id=project_id,
            kind="character",
            name="陆沉",
            aliases_json=json.dumps(["主角"], ensure_ascii=False),
            description="主角",
            importance=9,
            created_at_chapter=1,
        )
        ally = Entity(
            project_id=project_id,
            kind="faction",
            name="青云宗",
            description="盟友势力",
            created_at_chapter=1,
        )
        session.add_all([char, ally])
        session.flush()
        session.add(
            EntityState(
                entity_id=char.id,
                as_of_chapter=2,
                state_json=json.dumps({"location_id": "loc_city", "status": "active"}, ensure_ascii=False),
            )
        )
        session.add(
            RelationEdge(
                project_id=project_id,
                source_entity_id=char.id,
                target_entity_id=ally.id,
                relation_type="ally",
                description="公开盟友",
                established_at_chapter=2,
            )
        )

        v4_repo = WorldModelRepository(session)
        v4_repo.create_world_line(
            WorldLine(
                world_line_id="line_homeworld_siege",
                project_id=project_id,
                line_type="hidden_parallel_line",
                title="母星围困线",
                objective_state_summary="母星危机幕后推进",
            )
        )
        v4_repo.create_or_update_gap(
            project_id=project_id,
            gap_id="gap_homeworld_siege",
            objective_truth="母星已经被围",
            related_world_line_id="line_homeworld_siege",
            observer_states={"reader": {"visibility": "hidden"}},
            status="open",
        )
        v4_repo.append_reveal_event(
            RevealEvent(
                reveal_event_id="reveal_homeworld_hint",
                project_id=project_id,
                related_gap_id="gap_homeworld_siege",
                reveal_to_reader=True,
                reveal_method="hint",
                from_state=VisibilityState.HIDDEN,
                to_state=VisibilityState.HINTED,
                narrative_function="公平提示母星线",
                metadata={"source": "test"},
            )
        )

        counts = LegacyBookStateImporter(session).import_project(project_id)

        node_count = session.scalar(select(func.count()).select_from(WorldNodeRow))
        state_count = session.scalar(select(func.count()).select_from(WorldNodeStateRow))
        edge = session.execute(select(WorldEdgeRow).where(WorldEdgeRow.project_id == project_id)).scalar_one()
        narrative_node_types = {
            row.node_type
            for row in session.execute(
                select(NarrativeNodeRow).where(NarrativeNodeRow.project_id == project_id)
            ).scalars()
        }

    assert counts["world_nodes"] == 2
    assert counts["world_node_states"] == 2
    assert counts["world_edges"] == 1
    assert counts["narrative_nodes"] == 3
    assert node_count == 2
    assert state_count == 2
    assert edge.edge_type == "ally_of"
    assert edge.edge_family == "social"
    assert {"world_line", "knowledge_gap", "reveal_plan"}.issubset(narrative_node_types)


def test_legacy_character_import_assigns_personality_loadout() -> None:
    engine = get_engine(postgres_test_url("legacy-character-personality"))
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        project = Project(title="legacy personality", premise="p", genre="玄幻", setting_summary="s")
        session.add(project)
        session.flush()
        legacy = Entity(
            project_id=project.id,
            kind="character",
            name="沈临川",
            description="冷静护卫，负责保护主角，对承诺极重。",
            importance=7,
            created_at_chapter=1,
        )
        session.add(legacy)
        session.flush()

        LegacyBookStateImporter(session).import_project(project.id)
        node = session.execute(select(WorldNodeRow).where(WorldNodeRow.id == legacy.id)).scalar_one()
        profile = json.loads(node.profile_json)
        metadata = json.loads(node.metadata_json)

    assert profile["personality_loadout"]["dominant"]["skill"] == "trait-loyal-protector"
    assert metadata["legacy_entity_id"] == legacy.id
    assert metadata["personality_assignment"]["assignment_mode"] in {"auto_rule", "fallback_minimal"}


def test_legacy_import_reports_site_state_map_bindings_without_subworld_scope_leak() -> None:
    engine = get_engine(postgres_test_url())
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        project = Project(
            title="legacy import map",
            premise="旧地点迁移",
            genre="玄幻",
            setting_summary="黑石城",
        )
        session.add(project)
        session.flush()
        city = Entity(
            project_id=project.id,
            kind="city",
            name="黑石城",
            description="主舞台城市",
            created_at_chapter=1,
        )
        session.add(city)
        session.flush()

        counts = LegacyBookStateImporter(session).import_project(project.id)

        world_node = session.get(WorldNodeRow, city.id)
        map_node_count = session.scalar(select(func.count()).select_from(MapNodeRow))
        subworld_count = session.scalar(select(func.count()).select_from(SubWorld))
        profile = json.loads(world_node.profile_json)

    assert counts["map_nodes"] == 1
    assert counts["site_state_bindings"] == 1
    assert counts["migration_report"]["created_site_state_map_bindings"][0]["site_state_id"] == city.id
    assert profile["map_node_id"].startswith("legacy_map_node_")
    assert map_node_count == 1
    assert subworld_count == 0


def test_legacy_import_promotes_subworld_region_drafts_idempotently() -> None:
    engine = get_engine(postgres_test_url())
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        project = Project(
            title="legacy import regions",
            premise="旧 region 草案迁移",
            genre="玄幻",
            setting_summary="秘境",
        )
        session.add(project)
        session.flush()
        subworld = SubWorld(
            id="sw_legacy_realm",
            project_id=project.id,
            name="旧秘境",
            purpose="承接旧运行时生成的 region_drafts",
            scope="arc_local",
            status="active",
            metadata_json=json.dumps(
                {
                    "region_source": "runtime_generated",
                    "region_promotion_state": "draft",
                    "region_drafts": [
                        {
                            "name": "秘境核心区",
                            "kind": "ancient_ruin",
                            "summary": "旧 runtime 草案中的核心区域。",
                            "terrain": ["stone_forest", "rift"],
                            "culture_traits": ["ancient_oath"],
                        },
                        {"name": "", "kind": "invalid_region"},
                    ],
                },
                ensure_ascii=False,
            ),
        )
        session.add(subworld)
        session.flush()

        first_counts = LegacyBookStateImporter(session).import_project(project.id)
        first_report = first_counts["migration_report"]
        region = session.execute(select(MapRegionRow).where(MapRegionRow.project_id == project.id)).scalar_one()
        metadata_after_first = json.loads(session.get(SubWorld, subworld.id).metadata_json)

        second_counts = LegacyBookStateImporter(session).import_project(project.id)
        second_report = second_counts["migration_report"]
        region_count = session.scalar(select(func.count()).select_from(MapRegionRow))
        subworld_count = session.scalar(select(func.count()).select_from(SubWorld))

    assert first_report["promoted_region_draft_count"] == 1
    assert first_report["created_region_ids"] == [region.id]
    assert first_report["skipped_region_drafts"][0]["reason"] == "missing_name"
    assert region.name == "秘境核心区"
    assert region.region_type == "ancient_ruin"
    assert "stone_forest" in region.terrain
    assert json.loads(region.metadata_json)["legacy_source"] == "sub_worlds.metadata_json.region_drafts"
    assert metadata_after_first["region_promotion_state"] == "promoted"
    assert metadata_after_first["region_promotion_report"]["promoted_region_draft_count"] == 1
    assert metadata_after_first["region_drafts"][0]["name"] == "秘境核心区"
    assert second_report["promoted_region_draft_count"] == 1
    assert second_report["created_region_ids"] == []
    assert region_count == 1
    assert subworld_count == 1
