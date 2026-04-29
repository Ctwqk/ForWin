from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from forwin.book_state import BookStateRepository
from forwin.context.assembler import assemble_context
from forwin.models import DecisionEvent
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.personality.context import build_active_personality_context
from forwin.personality.library import CharacterPersonalityLibrary
from forwin.personality.models import PersonalityLoadout
from forwin.protocol.book_state import WorldNode
from forwin.state.repo import StateRepository
from forwin.state.updater import StateUpdater
from tests.postgres import postgres_test_url
from tests.test_personality_assignment import _write_skill


def _seed_allowed_bare_character(session, *, creation_status: str, strict: bool | None):
    updater = StateUpdater(session)
    automation = {}
    if strict is not None:
        automation = {"character_personality": {"strict_integrity": strict}}
    project = updater.create_project(
        title="人格运行时",
        premise="p",
        genre="玄幻",
        creation_status=creation_status,
        automation_json=json.dumps(automation, ensure_ascii=False),
    )
    arc = updater.create_arc_plan(project.id, "弧线")
    chapter = updater.create_chapter_plan(
        project_id=project.id,
        arc_plan_id=arc.id,
        chapter_number=1,
        title="第一章",
        one_line="开场",
        goals=["推进"],
    )
    entity = updater.create_entity(project.id, "character", "裸角色", "允许角色", chapter=0)
    global_core = updater.create_subworld(
        project_id=project.id,
        origin_arc_id=arc.id,
        parent_subworld_id=None,
        name="global_core",
        purpose="核心角色",
        scope="global_core",
        metadata={},
    )
    updater.create_roster_item(
        project_id=project.id,
        subworld_id=global_core.id,
        entity_id=entity.id,
        display_name=entity.name,
        description=entity.description,
        is_core=True,
        status="seeded_named",
    )
    BookStateRepository(session).create_world_node(
        WorldNode(
            id="char_bare",
            project_id=project.id,
            node_type="character",
            name=entity.name,
            description=entity.description,
            metadata={"legacy_entity_id": entity.id},
        )
    )
    return project.id, chapter


def test_assemble_context_warns_for_missing_personality_loadout_in_legacy_project() -> None:
    engine = get_engine(postgres_test_url("character-personality-context-warn"))
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        project_id, chapter = _seed_allowed_bare_character(session, creation_status="legacy", strict=None)

        context = assemble_context(StateRepository(session), project_id, chapter)

    assert context.personality_integrity_issues
    assert context.personality_integrity_issues[0]["code"] == "personality_missing_loadout"
    assert context.active_personality_contexts == []


def test_assemble_context_blocks_missing_personality_loadout_when_strict() -> None:
    engine = get_engine(postgres_test_url("character-personality-context-strict"))
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        project_id, chapter = _seed_allowed_bare_character(session, creation_status="writing", strict=True)

        with pytest.raises(ValueError, match="personality_missing_loadout"):
            assemble_context(StateRepository(session), project_id, chapter)
        events = session.execute(
            select(DecisionEvent).where(
                DecisionEvent.project_id == project_id,
                DecisionEvent.event_type == "character_integrity_check_failed",
            )
        ).scalars().all()

    assert events
    payload = json.loads(events[0].payload_json or "{}")
    assert payload["issues"][0]["code"] == "personality_missing_loadout"


def test_relationship_pattern_active_context_requires_target_relationship(tmp_path) -> None:
    _write_skill(
        tmp_path,
        "skills/traits/trait-loyal-protector",
        skill_type="trait",
        description="先保护承诺对象。",
    )
    _write_skill(
        tmp_path,
        "skills/relationship_patterns/rel-rival-respect",
        skill_type="relationship_pattern",
        description="竞争中承认对方实力。",
    )
    loadout = PersonalityLoadout.model_validate(
        {
            "dominant": {"skill": "trait-loyal-protector", "weight": 0.72},
            "secondary": [],
            "social_mask": [],
            "stress_modes": [],
            "relationship_patterns": [
                {"skill": "rel-rival-respect", "weight": 0.48, "target": "char_rival"}
            ],
            "overrides": {},
        }
    )
    library = CharacterPersonalityLibrary(tmp_path)

    inactive = build_active_personality_context(
        character_id="char_a",
        character_name="甲",
        loadout=loadout,
        library=library,
        relationship_targets=["char_other"],
    )
    active = build_active_personality_context(
        character_id="char_a",
        character_name="甲",
        loadout=loadout,
        library=library,
        relationship_targets=["char_rival"],
    )

    assert inactive.active_skills.relationship_pattern == []
    assert active.active_skills.relationship_pattern == ["rel-rival-respect"]
