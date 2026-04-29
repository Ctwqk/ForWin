from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from forwin.api_book_state_routes import build_handlers
from forwin.book_state import BookStateRepository
from forwin.characters.creation import CharacterCreationHelper
from forwin.characters.models import CharacterCreationRequest
from forwin.models import DecisionEvent, Project, RelationEdge, new_id
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.personality.library import CharacterPersonalityLibrary
from forwin.state.updater import StateUpdater
from tests.postgres import postgres_test_url
from tests.test_personality_assignment import _library_root


def _create_character_pair(session, project_id: str, library_root: Path):
    helper = CharacterCreationHelper(session, personality_library=CharacterPersonalityLibrary(library_root))
    source = helper.create_character(
        CharacterCreationRequest(
            project_id=project_id,
            source="api_manual",
            name="沈临川",
            description="保护同伴，重视承诺。",
            personality_tags=["protector"],
        )
    )
    target = helper.create_character(
        CharacterCreationRequest(
            project_id=project_id,
            source="api_manual",
            name="顾清",
            description="冷静观察局势。",
        )
    )
    return source, target


def _loadout_for(session, project_id: str, character_id: str) -> dict:
    for node in BookStateRepository(session).list_world_nodes(project_id):
        if node.id == character_id:
            return dict(node.profile.get("personality_loadout") or {})
    raise AssertionError(f"missing character: {character_id}")


def test_rival_relation_enriches_both_sides_and_records_diff(tmp_path: Path) -> None:
    engine = get_engine(postgres_test_url("relationship-enrichment-rival"))
    init_db(engine)
    Session = get_session_factory(engine)
    library_root = _library_root(tmp_path)

    with Session.begin() as session:
        project = Project(title="关系人格", premise="p", genre="玄幻", setting_summary="s")
        session.add(project)
        session.flush()
        source, target = _create_character_pair(session, project.id, library_root)

        edge = StateUpdater(session).create_relation(
            project.id,
            source.legacy_entity_id,
            target.legacy_entity_id,
            "rival",
            description="两人是长期竞争的对手。",
        )
        source_loadout = _loadout_for(session, project.id, source.character_id)
        target_loadout = _loadout_for(session, project.id, target.character_id)
        events = session.execute(
            select(DecisionEvent).where(
                DecisionEvent.project_id == project.id,
                DecisionEvent.event_type == "personality_relationship_enriched",
            )
        ).scalars().all()

    assert edge.id
    assert source_loadout["relationship_patterns"] == [
        {"skill": "rel-rival-respect", "weight": 0.48, "target": target.character_id}
    ]
    assert target_loadout["relationship_patterns"] == [
        {"skill": "rel-rival-respect", "weight": 0.48, "target": source.character_id}
    ]
    assert events
    assert "rel-rival-respect" in (events[0].payload_json or "")


def test_relationship_enrichment_preserves_manual_override_and_skips_duplicates(tmp_path: Path) -> None:
    engine = get_engine(postgres_test_url("relationship-enrichment-manual"))
    init_db(engine)
    Session = get_session_factory(engine)
    library_root = _library_root(tmp_path)

    explicit = {
        "dominant": {"skill": "trait-loyal-protector", "weight": 0.72},
        "secondary": [],
        "social_mask": [],
        "stress_modes": [],
        "relationship_patterns": [],
        "overrides": {},
    }

    with Session.begin() as session:
        project = Project(title="关系人格手动", premise="p", genre="玄幻", setting_summary="s")
        session.add(project)
        session.flush()
        helper = CharacterCreationHelper(session, personality_library=CharacterPersonalityLibrary(library_root))
        locked = helper.create_character(
            CharacterCreationRequest(
                project_id=project.id,
                source="world_studio_manual",
                name="锁定角色",
                description="用户锁定。",
                personality_loadout=explicit,
                personality_policy="manual",
            )
        )
        other = helper.create_character(
            CharacterCreationRequest(project_id=project.id, source="api_manual", name="被扶持者", description="需要保护。")
        )

        updater = StateUpdater(session)
        updater.create_relation(project.id, locked.legacy_entity_id, other.legacy_entity_id, "mentor", description="导师扶持并保护对方。")
        updater.create_relation(project.id, locked.legacy_entity_id, other.legacy_entity_id, "mentor", description="导师扶持并保护对方。")
        locked_loadout = _loadout_for(session, project.id, locked.character_id)
        other_loadout = _loadout_for(session, project.id, other.character_id)

    assert locked_loadout["relationship_patterns"] == []
    assert other_loadout["relationship_patterns"] == [
        {"skill": "rel-mentor-protector", "weight": 0.48, "target": locked.character_id}
    ]


def test_manual_relationship_enrichment_api_scans_project(tmp_path: Path) -> None:
    engine = get_engine(postgres_test_url("relationship-enrichment-api"))
    init_db(engine)
    Session = get_session_factory(engine)
    library_root = _library_root(tmp_path)

    with Session.begin() as session:
        project = Project(title="关系人格 API", premise="p", genre="玄幻", setting_summary="s")
        session.add(project)
        session.flush()
        source, target = _create_character_pair(session, project.id, library_root)
        session.add(
            RelationEdge(
                id=new_id(),
                project_id=project.id,
                source_entity_id=source.legacy_entity_id,
                target_entity_id=target.legacy_entity_id,
                relation_type="rival",
                description="长期竞争的对手。",
                is_active=True,
            )
        )
        project_id = project.id
        source_id = source.character_id
        target_id = target.character_id

    handlers = build_handlers(get_session=Session, personality_library_root=str(library_root))
    response = handlers["enrich_character_relationships"](project_id, {"reason": "api test"})

    with Session() as session:
        source_loadout = _loadout_for(session, project_id, source_id)

    assert response["schema_version"] == "character.relationship_personality_enrichment.v1"
    assert response["enriched"] == 2
    assert source_loadout["relationship_patterns"][0]["target"] == target_id
