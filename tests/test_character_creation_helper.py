from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from forwin.api_book_state_routes import build_handlers
from forwin.api_schemas import (
    CharacterPersonalityActiveContextPreviewRequest,
    CharacterCreateRequest,
    CharacterPersonalityPreviewRequest,
    CharacterPersonalityReassignRequest,
    PersonalityLoadoutUpdateRequest,
)
from forwin.book_state import BookStateRepository
from forwin.characters.creation import CharacterCreationHelper
from forwin.characters.models import CharacterCreationRequest
from forwin.characters.registry import CharacterRegistry
from forwin.models import CharacterIdentityMapRow, DecisionEvent, Entity, Project, WorldNodeRow
from forwin.models.base import get_engine, get_session_factory, init_db, upgrade_db
from forwin.personality.library import CharacterPersonalityLibrary
from forwin.personality.policy import CharacterPersonalityPolicyResolver
from forwin.protocol.book_state import WorldNode
from tests.postgres import postgres_test_url
from tests.test_personality_assignment import _library_root


def test_upgrade_backfills_identity_map_timestamps_for_existing_world_nodes() -> None:
    engine = get_engine(postgres_test_url("character-identity-map-backfill"))
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        project = Project(title="身份回填", premise="p", genre="玄幻", setting_summary="s")
        session.add(project)
        session.flush()
        session.add(
            WorldNodeRow(
                id="char_legacy_backfill",
                project_id=project.id,
                node_type="character",
                name="苏时雨",
                aliases_json='["苏时雨"]',
                metadata_json='{"legacy_entity_id":"legacy_su"}',
            )
        )

    upgrade_db(engine)

    with Session.begin() as session:
        identity = session.execute(
            select(CharacterIdentityMapRow).where(
                CharacterIdentityMapRow.book_state_node_id == "char_legacy_backfill"
            )
        ).scalar_one()

    assert identity.display_name == "苏时雨"
    assert identity.legacy_entity_id == "legacy_su"
    assert identity.created_at is not None
    assert identity.updated_at is not None


def test_create_character_writes_book_state_loadout_metadata_legacy_and_audit(tmp_path: Path) -> None:
    engine = get_engine(postgres_test_url("character-creation-helper"))
    init_db(engine)
    Session = get_session_factory(engine)
    library_root = _library_root(tmp_path)

    with Session.begin() as session:
        project = Project(title="人物创建", premise="p", genre="玄幻", setting_summary="s")
        session.add(project)
        session.flush()

        result = CharacterCreationHelper(
            session,
            personality_library=CharacterPersonalityLibrary(library_root),
        ).create_character(
            CharacterCreationRequest(
                project_id=project.id,
                source="api_manual",
                name="沈临川",
                aliases=["沈师兄"],
                description="外门执事，冷静克制，负责保护主角。他对承诺极重。",
                importance=7,
                profile={
                    "public_identity": "外门执事",
                    "role_archetype": "protector",
                    "narrative_role": "supporting_ally",
                    "personality_tags": ["protector", "professional"],
                },
                state={"goal": "护送主角进入内城", "location_id": "loc_gate"},
                personality_tags=["protector", "professional"],
                audit_reason="测试创建人物",
            )
        )

        node = BookStateRepository(session).list_world_nodes(project.id)[0]
        legacy = session.get(Entity, result.legacy_entity_id)
        events = session.execute(select(DecisionEvent).where(DecisionEvent.project_id == project.id)).scalars().all()

    assert result.created is True
    assert result.world_node["id"] == result.character_id
    assert node.node_type == "character"
    assert node.name == "沈临川"
    assert node.aliases == ["沈师兄"]
    assert node.profile["personality_loadout"]["dominant"]["skill"] == "trait-loyal-protector"
    assert node.metadata["legacy_entity_id"] == result.legacy_entity_id
    assert node.metadata["character_identity"]["canonical_character_id"] == result.character_id
    assert node.metadata["character_identity"]["legacy_entity_id"] == result.legacy_entity_id
    assert node.metadata["personality_assignment"]["assignment_mode"] == "auto_rule"
    assert node.metadata["character_creation"]["source"] == "api_manual"
    assert legacy is not None
    assert legacy.kind == "character"
    assert {event.event_type for event in events} >= {"character_created", "personality_loadout_auto_assigned"}


def test_create_character_persists_identity_map_for_book_state_legacy_and_roster(tmp_path: Path) -> None:
    engine = get_engine(postgres_test_url("character-identity-map"))
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        project = Project(title="人物身份", premise="p", genre="玄幻", setting_summary="s")
        session.add(project)
        session.flush()

        result = CharacterCreationHelper(
            session,
            personality_library=CharacterPersonalityLibrary(_library_root(tmp_path)),
        ).create_character(
            CharacterCreationRequest(
                project_id=project.id,
                source="subworld_planned_slot_materialization",
                source_ref="roster_a",
                roster_item_id="roster_a",
                name="周怀瑾",
                aliases=["周执事"],
                description="负责联络内城。",
                creation_context={"genesis_ref_id": "genesis:core_cast:zhou"},
            )
        )
        identity = session.execute(
            select(CharacterIdentityMapRow).where(
                CharacterIdentityMapRow.project_id == project.id,
                CharacterIdentityMapRow.book_state_node_id == result.character_id,
            )
        ).scalar_one()

    assert identity.canonical_character_id == result.character_id
    assert identity.display_name == "周怀瑾"
    assert identity.legacy_entity_id == result.legacy_entity_id
    assert identity.genesis_ref_id == "genesis:core_cast:zhou"
    assert identity.roster_item_ids_json == '["roster_a"]'
    assert identity.aliases_json == '["周执事", "周怀瑾"]'
    assert identity.status == "active"


def test_create_character_preserves_explicit_loadout_as_manual_override(tmp_path: Path) -> None:
    engine = get_engine(postgres_test_url("character-creation-manual-loadout"))
    init_db(engine)
    Session = get_session_factory(engine)
    library_root = _library_root(tmp_path)

    explicit = {
        "dominant": {"skill": "trait-suspicious-survivor", "weight": 0.8},
        "secondary": [],
        "social_mask": [],
        "stress_modes": [],
        "relationship_patterns": [],
        "overrides": {},
    }

    with Session.begin() as session:
        project = Project(title="手动人物", premise="p", genre="玄幻", setting_summary="s")
        session.add(project)
        session.flush()

        result = CharacterCreationHelper(
            session,
            personality_library=CharacterPersonalityLibrary(library_root),
        ).create_character(
            CharacterCreationRequest(
                project_id=project.id,
                source="world_studio_manual",
                name="陆明",
                description="用户手动配置的角色。",
                personality_loadout=explicit,
                personality_policy="manual",
            )
        )

        node = BookStateRepository(session).list_world_nodes(project.id)[0]

    assert result.personality_loadout["dominant"]["skill"] == "trait-suspicious-survivor"
    assert node.metadata["personality_assignment"]["manual_override"] is True
    assert node.metadata["personality_assignment"]["assignment_mode"] == "explicit_loadout"


def test_get_or_create_character_reuses_existing_exact_name(tmp_path: Path) -> None:
    engine = get_engine(postgres_test_url("character-creation-reuse"))
    init_db(engine)
    Session = get_session_factory(engine)
    library_root = _library_root(tmp_path)

    with Session.begin() as session:
        project = Project(title="复用人物", premise="p", genre="玄幻", setting_summary="s")
        session.add(project)
        session.flush()
        helper = CharacterCreationHelper(session, personality_library=CharacterPersonalityLibrary(library_root))

        first = helper.get_or_create_character(
            CharacterCreationRequest(project_id=project.id, source="api_manual", name="阿青", description="保护同伴。")
        )
        second = helper.get_or_create_character(
            CharacterCreationRequest(project_id=project.id, source="api_manual", name="阿青", description="重复创建。")
        )
        nodes = BookStateRepository(session).list_world_nodes(project.id)

    assert first.character_id == second.character_id
    assert first.created is True
    assert second.created is False
    assert second.merged_existing is True
    assert len(nodes) == 1


def test_registry_resolves_identity_before_name_or_alias(tmp_path: Path) -> None:
    engine = get_engine(postgres_test_url("character-registry-priority"))
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        project = Project(title="解析人物", premise="p", genre="玄幻", setting_summary="s")
        session.add(project)
        session.flush()
        repo = BookStateRepository(session)
        repo.create_world_node(
            WorldNode(
                id="char_legacy",
                project_id=project.id,
                node_type="character",
                name="同名角色",
                metadata={"legacy_entity_id": "legacy_a"},
            )
        )
        repo.create_world_node(
            WorldNode(
                id="char_roster",
                project_id=project.id,
                node_type="character",
                name="同名角色",
                metadata={"roster_item_ids": ["roster_b"]},
            )
        )
        repo.create_world_node(
            WorldNode(
                id="char_alias",
                project_id=project.id,
                node_type="character",
                name="本名",
                aliases=["别名"],
            )
        )
        registry = CharacterRegistry(session)

        by_legacy = registry.resolve(project_id=project.id, legacy_entity_id="legacy_a", name="别名")
        by_roster = registry.resolve(project_id=project.id, roster_item_id="roster_b", name="别名")
        by_alias = registry.resolve(project_id=project.id, name="别名")

    assert by_legacy.node is not None
    assert by_legacy.node.id == "char_legacy"
    assert by_legacy.resolution == "explicit_legacy_entity_id"
    assert by_roster.node is not None
    assert by_roster.node.id == "char_roster"
    assert by_roster.resolution == "explicit_roster_item_id"
    assert by_alias.node is not None
    assert by_alias.node.id == "char_alias"
    assert by_alias.resolution == "canonical_alias"


def test_create_new_allows_same_name_with_disambiguation_metadata(tmp_path: Path) -> None:
    engine = get_engine(postgres_test_url("character-disambiguation"))
    init_db(engine)
    Session = get_session_factory(engine)
    library_root = _library_root(tmp_path)

    with Session.begin() as session:
        project = Project(title="同名人物", premise="p", genre="玄幻", setting_summary="s")
        session.add(project)
        session.flush()
        helper = CharacterCreationHelper(session, personality_library=CharacterPersonalityLibrary(library_root))

        first = helper.create_character(
            CharacterCreationRequest(project_id=project.id, source="api_manual", name="阿青", description="保护同伴。")
        )
        second = helper.create_character(
            CharacterCreationRequest(
                project_id=project.id,
                source="api_manual",
                source_ref="faction_b",
                name="阿青",
                description="另一个同名角色。",
                existing_resolution="create_new",
                creation_context={"disambiguation": "faction_b"},
            )
        )
        nodes = {node.id: node for node in BookStateRepository(session).list_world_nodes(project.id)}

    assert first.character_id != second.character_id
    assert len(nodes) == 2
    assert nodes[second.character_id].metadata["character_creation"]["dedupe_resolution"] == "created_new_disambiguated"
    assert nodes[second.character_id].metadata["character_creation"]["disambiguation"] == "faction_b"


def test_generic_token_is_rejected_by_default(tmp_path: Path) -> None:
    engine = get_engine(postgres_test_url("character-creation-generic"))
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        project = Project(title="泛称人物", premise="p", genre="玄幻", setting_summary="s")
        session.add(project)
        session.flush()
        helper = CharacterCreationHelper(session, personality_library=CharacterPersonalityLibrary(_library_root(tmp_path)))

        result = helper.create_character(
            CharacterCreationRequest(project_id=project.id, source="api_manual", name="路人", description="围观群众。")
        )

    assert result.created is False
    assert result.integrity_report.ok is False
    assert result.warnings
    assert result.warnings[0]["code"] == "generic_character_rejected"


def test_character_integrity_gate_checks_existing_book_state_node(tmp_path: Path) -> None:
    engine = get_engine(postgres_test_url("character-integrity-gate"))
    init_db(engine)
    Session = get_session_factory(engine)
    library_root = _library_root(tmp_path)

    with Session.begin() as session:
        project = Project(title="完整性", premise="p", genre="玄幻", setting_summary="s")
        session.add(project)
        session.flush()
        repo = BookStateRepository(session)
        repo.create_world_node(
            WorldNode(id="char_bare", project_id=project.id, node_type="character", name="裸角色")
        )
        repo.create_world_node(
            WorldNode(
                id="char_ready",
                project_id=project.id,
                node_type="character",
                name="沈临川",
                profile={
                    "personality_loadout": {
                        "dominant": {"skill": "trait-loyal-protector", "weight": 0.72},
                        "secondary": [],
                        "social_mask": [],
                        "stress_modes": [],
                        "relationship_patterns": [],
                        "overrides": {},
                    }
                },
            )
        )
        helper = CharacterCreationHelper(session, personality_library=CharacterPersonalityLibrary(library_root))

        bare_report = helper.ensure_character_integrity("char_bare", reason="writer context assembly")
        ready_report = helper.ensure_character_integrity("char_ready", reason="writer context assembly")

    assert bare_report.ok is False
    assert bare_report.errors[0].code == "personality_missing_loadout"
    assert ready_report.ok is True


def test_character_api_create_preview_coverage_report_and_backfill(tmp_path: Path) -> None:
    engine = get_engine(postgres_test_url("character-creation-api"))
    init_db(engine)
    Session = get_session_factory(engine)
    library_root = _library_root(tmp_path)

    with Session.begin() as session:
        project = Project(title="人物 API", premise="p", genre="玄幻", setting_summary="s")
        session.add(project)
        session.flush()
        project_id = project.id
        BookStateRepository(session).create_world_node(
            WorldNode(id="char_bare", project_id=project.id, node_type="character", name="赵平", description="镇民。")
        )

    handlers = build_handlers(get_session=Session, personality_library_root=str(library_root))
    preview = handlers["preview_character_personality"](
        project_id,
        CharacterPersonalityPreviewRequest(
            name="沈临川",
            description="外门执事，冷静克制，负责保护主角。他对承诺极重。",
            profile={
                "public_identity": "外门执事",
                "role_archetype": "protector",
                "narrative_role": "supporting_ally",
                "personality_tags": ["protector", "professional"],
            },
            personality_tags=["protector", "professional"],
        ),
    )
    created = handlers["create_character"](
        project_id,
        CharacterCreateRequest(
            source="api_manual",
            name="沈临川",
            description="外门执事，冷静克制，负责保护主角。他对承诺极重。",
            profile={
                "public_identity": "外门执事",
                "role_archetype": "protector",
                "narrative_role": "supporting_ally",
                "personality_tags": ["protector", "professional"],
            },
            personality_tags=["protector", "professional"],
        ),
    )
    report = handlers["get_character_assignment_report"](project_id, created["character_id"])
    report_by_assignment_id = handlers["get_character_assignment_report_by_id"](
        project_id,
        created["personality_assignment"]["assignment_id"],
    )
    active_context_preview = handlers["preview_character_active_personality_context"](
        project_id,
        CharacterPersonalityActiveContextPreviewRequest(
            character_id=created["character_id"],
            character_name=created["character_name"],
            personality_loadout=created["personality_loadout"],
            scene_flags=["public_scene"],
        ),
    )
    coverage_before = handlers["get_character_personality_coverage"](project_id)
    missing_only = handlers["get_character_personality_coverage"](project_id, filter="missing_loadout")
    backfill = handlers["backfill_character_personalities"](project_id, {"dry_run": False, "reason": "test"})
    coverage_after = handlers["get_character_personality_coverage"](project_id)
    handlers["set_character_personality_loadout"](
        project_id,
        created["character_id"],
        PersonalityLoadoutUpdateRequest(
            personality_loadout={
                "dominant": {"skill": "trait-suspicious-survivor", "weight": 0.8},
                "secondary": [],
                "social_mask": [],
                "stress_modes": [],
                "relationship_patterns": [],
                "overrides": {},
            },
            reason="test manual override",
        ),
    )
    reassigned = handlers["reassign_character_personality"](
        project_id,
        created["character_id"],
        CharacterPersonalityReassignRequest(force=True, reason="test force reassign"),
    )
    metrics = handlers["get_character_personality_metrics"](project_id)

    assert preview["personality_loadout"]["dominant"]["skill"] == "trait-loyal-protector"
    assert created["schema_version"] == "character.creation.v1"
    assert created["personality_assignment"]["assignment_mode"] == "auto_rule"
    assert report["personality_assignment"]["assignment_id"] == created["personality_assignment"]["assignment_id"]
    assert report_by_assignment_id["personality_assignment"]["assignment_id"] == created["personality_assignment"]["assignment_id"]
    assert report_by_assignment_id["character_id"] == created["character_id"]
    assert active_context_preview["active_personality_context"]["character_id"] == created["character_id"]
    assert "trait-loyal-protector" in active_context_preview["active_personality_context"]["active_skills"]["dominant"]
    assert coverage_before["missing_loadout"] == 1
    assert missing_only["characters"][0]["character_id"] == "char_bare"
    assert backfill["assigned"] == 1
    assert coverage_after["missing_loadout"] == 0
    assert reassigned["diff"]["old_loadout"]["dominant"]["skill"] == "trait-suspicious-survivor"
    assert reassigned["diff"]["new_loadout"]["dominant"]["skill"] == "trait-loyal-protector"
    assert "trait-loyal-protector" in reassigned["diff"]["added_skill_ids"]
    assert "trait-suspicious-survivor" in reassigned["diff"]["removed_skill_ids"]
    assert metrics["schema_version"] == "character.personality_metrics.v1"
    assert metrics["character_creation_total"] >= 1
    assert metrics["character_creation_auto_personality_assigned_total"] >= 1
    assert metrics["character_creation_manual_override_total"] >= 1
    assert metrics["most_used_dominant_skills"]


def test_project_personality_policy_resolver_reads_automation_json() -> None:
    engine = get_engine(postgres_test_url("character-policy-resolver"))
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        project = Project(
            title="项目策略",
            premise="p",
            genre="玄幻",
            setting_summary="s",
            automation_json=(
                '{"character_personality": {'
                '"strict_integrity": true,'
                '"low_confidence_blocks_core": true,'
                '"fallback_character_class": "unknown_core_character",'
                '"cast_diversity_enabled": false,'
                '"relationship_enrichment_enabled": false'
                "}}"
            ),
        )
        session.add(project)
        session.flush()

        policy = CharacterPersonalityPolicyResolver(session).resolve_for_project(project.id)

    assert policy.strict_integrity is True
    assert policy.low_confidence_blocks_core is True
    assert policy.fallback_character_class == "unknown_core_character"
    assert policy.cast_diversity_enabled is False
    assert policy.relationship_enrichment_enabled is False
