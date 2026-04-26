from __future__ import annotations

import json

from sqlalchemy import func, select

from forwin.book_state import LegacyBookStateImporter
from forwin.models import Project
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.book_state import NarrativeNodeRow, WorldEdgeRow, WorldNodeRow, WorldNodeStateRow
from forwin.models.entity import Entity, EntityState, RelationEdge
from forwin.protocol.world_v4 import (
    DeltaSource,
    DeltaSourceType,
    RevealEvent,
    VisibilityState,
    WorldLine,
)
from forwin.world_model_v4.repository import WorldModelRepository


def test_legacy_import_maps_entities_relations_and_v4_narrative_nodes() -> None:
    engine = get_engine(":memory:")
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
    assert counts["world_node_states"] == 1
    assert counts["world_edges"] == 1
    assert counts["narrative_nodes"] == 3
    assert node_count == 2
    assert state_count == 1
    assert edge.edge_type == "ally_of"
    assert edge.edge_family == "social"
    assert {"world_line", "knowledge_gap", "reveal_plan"}.issubset(narrative_node_types)
