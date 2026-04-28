from __future__ import annotations

import json
from pathlib import Path

from forwin.api_book_state_routes import build_handlers
from forwin.api_schemas import PersonalityLoadoutUpdateRequest
from forwin.book_state import BookStateRepository
from forwin.book_state.schema import validate_world_node
from forwin.models import Project
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.protocol.book_state import WorldNode
from forwin.protocol.context import ChapterContextPack, ReviewContextPack
from forwin.protocol.scene import ScenePlan
from forwin.protocol.writer import WriterOutput
from forwin.reviewer.webnovel import WebNovelExperienceReviewer
from forwin.writer.prompts import build_scene_generation_prompt
from tests.postgres import postgres_test_url


REPO_ROOT = Path(__file__).resolve().parents[1]


def _write_skill(root: Path, relative: str, *, skill_type: str, description: str) -> None:
    path = root / relative / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "---",
                f"name: {path.parent.name}",
                "version: 1.0.0",
                f"description: {description}",
                "forwin_scope: character_personality",
                "category: character_personality_skill",
                f"skill_type: {skill_type}",
                "trait_axes:",
                "  trust_baseline: low",
                "mode: instruction_only",
                "---",
                f"# Skill: {path.parent.name}",
                "",
                "## Core Function",
                description,
            ]
        ),
        encoding="utf-8",
    )


def test_personality_library_loads_nested_metadata_and_builds_active_context(tmp_path: Path) -> None:
    from forwin.personality.context import build_active_personality_context
    from forwin.personality.library import CharacterPersonalityLibrary
    from forwin.personality.models import PersonalityLoadout

    _write_skill(
        tmp_path,
        "skills/traits/trait-suspicious-survivor",
        skill_type="trait",
        description="先判断动机、收益者和隐藏代价。",
    )
    _write_skill(
        tmp_path,
        "skills/social_masks/mask-cold-professional",
        skill_type="social_mask",
        description="对外保持职业冷静，少解释情绪。",
    )
    _write_skill(
        tmp_path,
        "skills/stress_modes/stress-paranoid-controller",
        skill_type="stress_mode",
        description="失控时控制信息并提前准备退路。",
    )

    library = CharacterPersonalityLibrary(tmp_path)
    skill = library.get("trait-suspicious-survivor")
    assert skill is not None
    assert skill.metadata["trait_axes"]["trust_baseline"] == "low"

    loadout = PersonalityLoadout.model_validate(
        {
            "dominant": {"skill": "trait-suspicious-survivor", "weight": 0.75},
            "social_mask": [{"skill": "mask-cold-professional", "weight": 0.6, "active_when": ["public_scene"]}],
            "stress_modes": [
                {
                    "skill": "stress-paranoid-controller",
                    "trigger": ["betrayal_suspected"],
                }
            ],
        }
    )
    context = build_active_personality_context(
        character_id="char_mc",
        character_name="沈砚",
        loadout=loadout,
        library=library,
        scene_flags=["public_scene"],
        pressure_triggers=["betrayal_suspected"],
    )

    assert context.active_skills.dominant == ["trait-suspicious-survivor"]
    assert context.active_skills.social_mask == ["mask-cold-professional"]
    assert context.active_skills.stress_mode == ["stress-paranoid-controller"]
    assert any("动机" in item for item in context.current_behavior_bias.decision)
    assert "Do not override canon." in context.constraints


def test_active_context_prefers_prompt_compression_biases(tmp_path: Path) -> None:
    from forwin.personality.context import build_active_personality_context
    from forwin.personality.library import CharacterPersonalityLibrary
    from forwin.personality.models import PersonalityLoadout

    path = tmp_path / "skills/traits/trait-suspicious-survivor/SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "---",
                "name: trait-suspicious-survivor",
                "version: 1.0",
                "description: 通过信息优势、试探与预案维持安全感的人格机制。",
                "forwin_scope: character_personality",
                "category: character_personality_skill",
                "skill_type: trait",
                "mode: instruction_only",
                "---",
                "# Skill: trait-suspicious-survivor",
                "",
                "## Prompt Compression",
                "",
                "```yaml",
                "prompt_compression:",
                "  one_line_summary: 高警觉、低信任、靠情报与预案换安全。",
                "  perception_bias:",
                "    - 先看谁受益、谁隐瞒、哪里能撤",
                "  decision_bias:",
                "    - 先保退路，再承诺",
                "  dialogue_bias:",
                "    - 短句、反问、要求证据、少自我暴露",
                "  body_language_bias:",
                "    - 扫出口、不背对陌生人、接触前停顿",
                "  relationship_bias:",
                "    - 陌生人低披露，盟友条件信任",
                "  stress_bias:",
                "    - 压力越大，越控制信息和关系边界",
                "```",
            ]
        ),
        encoding="utf-8",
    )

    context = build_active_personality_context(
        character_id="char_mc",
        loadout=PersonalityLoadout.model_validate(
            {"dominant": {"skill": "trait-suspicious-survivor", "weight": 0.75}}
        ),
        library=CharacterPersonalityLibrary(tmp_path),
    )

    assert context.current_behavior_bias.perception == ["先看谁受益、谁隐瞒、哪里能撤"]
    assert context.current_behavior_bias.decision == ["先保退路，再承诺"]
    assert context.current_behavior_bias.dialogue == ["短句、反问、要求证据、少自我暴露"]
    assert context.current_behavior_bias.body_language == ["扫出口、不背对陌生人、接触前停顿"]
    assert "Do not infer behavior from model labels." in context.constraints


def test_world_node_schema_accepts_character_personality_loadout() -> None:
    node = WorldNode(
        id="char_mc",
        project_id="project",
        node_type="character",
        profile={
            "personality_loadout": {
                "dominant": {"skill": "trait-suspicious-survivor", "weight": 0.75},
                "secondary": [],
                "social_mask": [],
                "stress_modes": [],
            }
        },
    )
    assert validate_world_node(node, strict=True).ok


def _chapter_context(active_personality_contexts: list[dict]) -> ChapterContextPack:
    return ChapterContextPack(
        project_id="project",
        project_title="技能测试书",
        premise="雨夜里，主角得到一面会说话的镜子。",
        genre="玄幻",
        setting_summary="旧城与禁术并存。",
        chapter_number=1,
        chapter_plan_title="第一章 雨夜",
        chapter_plan_one_line="主角拿到危险线索。",
        chapter_goals=["建立危机"],
        active_personality_contexts=active_personality_contexts,
    )


def test_writer_prompt_includes_compressed_personality_context() -> None:
    prompt = build_scene_generation_prompt(
        _chapter_context(
            [
                {
                    "character_id": "char_mc",
                    "character_name": "沈砚",
                    "active_skills": {"dominant": ["trait-suspicious-survivor"], "stress_mode": []},
                    "current_behavior_bias": {
                        "decision": ["先判断动机、收益者和隐藏代价。"],
                        "dialogue": ["少解释情绪。"],
                    },
                    "constraints": ["Do not override canon."],
                }
            ]
        ),
        ScenePlan(scene_no=1, objective="试探盟友", must_progress_points=["发现异常"]),
    )

    user_content = prompt[1]["content"]
    assert "【人物性格运行时】" in user_content
    assert "trait-suspicious-survivor" in user_content
    assert "先判断动机" in user_content
    assert "Do not override canon." in user_content


def test_reviewer_payload_indexes_personality_context_as_evidence() -> None:
    reviewer = WebNovelExperienceReviewer()
    context = ReviewContextPack(
        project_id="project",
        project_title="技能测试书",
        chapter_number=1,
        chapter_plan_title="第一章 雨夜",
        chapter_plan_one_line="主角拿到危险线索。",
        active_personality_contexts=[
            {
                "character_id": "char_mc",
                "character_name": "沈砚",
                "active_skills": {"dominant": ["trait-suspicious-survivor"]},
                "current_behavior_bias": {"decision": ["先判断动机。"]},
                "constraints": ["Do not override canon."],
            }
        ],
    )

    payload = reviewer._llm_payload(
        context,
        WriterOutput(
            project_id="project",
            chapter_number=1,
            title="第一章 雨夜",
            body="沈砚没有立刻答应，而是先问消息从哪里来。",
            char_count=24,
            end_of_chapter_summary="沈砚开始核验线索。",
        ),
    )

    assert payload["personality"][0]["character_id"] == "char_mc"
    assert any(item["evidence_id"] == "personality:char_mc" for item in payload["evidence_index"])


def test_book_state_personality_handlers_update_character_profile(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "skills/traits/trait-suspicious-survivor",
        skill_type="trait",
        description="先判断动机。",
    )
    engine = get_engine(postgres_test_url("personality-api"))
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        project = Project(title="人物性格", premise="p", genre="g", setting_summary="s")
        session.add(project)
        session.flush()
        BookStateRepository(session).create_world_node(
            WorldNode(
                id="char_mc",
                project_id=project.id,
                node_type="character",
                name="沈砚",
                profile={"public_identity": "军师"},
            )
        )
        project_id = project.id

    handlers = build_handlers(get_session=Session, personality_library_root=tmp_path)
    response = handlers["set_character_personality_loadout"](
        project_id,
        "char_mc",
        PersonalityLoadoutUpdateRequest(
            personality_loadout={
                "dominant": {"skill": "trait-suspicious-survivor", "weight": 0.8},
                "secondary": [],
                "social_mask": [],
                "stress_modes": [],
                "relationship_patterns": [],
                "overrides": {},
            }
        ),
    )

    assert response["personality_loadout"]["dominant"]["skill"] == "trait-suspicious-survivor"

    with Session() as session:
        row = BookStateRepository(session).list_world_nodes(project_id)[0]
        assert row.profile["personality_loadout"]["dominant"]["weight"] == 0.8

    summary = handlers["list_character_personality_loadouts"](project_id)
    assert summary["characters"][0]["character_name"] == "沈砚"
    assert summary["characters"][0]["personality_loadout"]["dominant"]["skill"] == "trait-suspicious-survivor"
    assert json.loads(json.dumps(handlers["list_personality_skills"]()))["skills"][0]["name"] == "trait-suspicious-survivor"


def test_character_personality_skill_repository_skeleton_exists() -> None:
    root = REPO_ROOT / "forwin_skills/character_personality"
    required_files = [
        root / "README.md",
        root / "docs/six_layer_model.md",
        root / "docs/reference_model_policy.md",
        root / "docs/runtime_loading.md",
        root / "docs/conflict_resolution.md",
        root / "docs/skill_authoring_guide.md",
        root / "docs/reviewer_personality_consistency.md",
        root / "docs/references.md",
        root / "catalog/skill_catalog_mapping.md",
        root / "catalog/trigger_taxonomy.md",
        root / "catalog/value_taxonomy.md",
        root / "catalog/expression_taxonomy.md",
        root / "catalog/relationship_target_taxonomy.md",
        root / "schema/personality_skill.schema.json",
        root / "schema/character_personality_loadout.schema.json",
        root / "schema/active_personality_context.schema.json",
        root / "templates/SKILL_TEMPLATE.md",
        root / "templates/SKILL_TEMPLATE_TRAIT.md",
        root / "templates/SKILL_TEMPLATE_SOCIAL_MASK.md",
        root / "templates/SKILL_TEMPLATE_STRESS_MODE.md",
        root / "templates/SKILL_TEMPLATE_RELATIONSHIP_PATTERN.md",
        root / "templates/SKILL_TEMPLATE_ARCHETYPE.md",
        root / "templates/CHARACTER_LOADOUT_TEMPLATE.yaml",
        root / "templates/ACTIVE_PERSONALITY_CONTEXT_TEMPLATE.yaml",
        root / "examples/character_loadout_example.yaml",
        root / "examples/scene_active_personality_context.yaml",
        root / "examples/merged_prompt_example.md",
        root / "tests/consistency_tests.md",
        root / "tests/conflict_tests.md",
        root / "tests/stress_mode_tests.md",
        root / "tests/growth_arc_tests.md",
        root / "skills/traits/trait-suspicious-survivor/SKILL.md",
        root / "skills/traits/trait-loyal-protector/SKILL.md",
        root / "skills/traits/trait-ambitious-climber/SKILL.md",
        root / "skills/social_masks/mask-cold-professional/SKILL.md",
        root / "skills/social_masks/mask-gentle-caretaker/SKILL.md",
        root / "skills/social_masks/mask-cynical-joker/SKILL.md",
        root / "skills/stress_modes/stress-paranoid-controller/SKILL.md",
        root / "skills/stress_modes/stress-dissociated-analyst/SKILL.md",
        root / "skills/stress_modes/stress-pleasing-collapse/SKILL.md",
        root / "skills/relationship_patterns/rel-avoidant-loner/SKILL.md",
        root / "skills/relationship_patterns/rel-dependent-pleaser/SKILL.md",
        root / "skills/relationship_patterns/rel-jealous-possessive/SKILL.md",
        root / "skills/relationship_patterns/rel-caretaker-rescuer/SKILL.md",
        root / "skills/relationship_patterns/rel-mentor-protector/SKILL.md",
        root / "skills/relationship_patterns/rel-rival-respect/SKILL.md",
        root / "skills/archetypes/archetype-ruler/SKILL.md",
        root / "skills/archetypes/archetype-rebel/SKILL.md",
        root / "skills/archetypes/archetype-sage/SKILL.md",
        root / "skills/archetypes/archetype-trickster/SKILL.md",
        root / "skills/archetypes/archetype-caregiver/SKILL.md",
        root / "skills/archetypes/archetype-hero/SKILL.md",
        root / "skills/archetypes/archetype-shadow/SKILL.md",
    ]
    missing = [path.relative_to(root).as_posix() for path in required_files if not path.exists()]
    assert not missing

    library = __import__("forwin.personality", fromlist=["CharacterPersonalityLibrary"]).CharacterPersonalityLibrary(root)
    catalog = library.catalog_payload()["skills"]
    assert len(catalog) == 41
    assert any(item["name"] == "trait-suspicious-survivor" for item in catalog)
    for relative in [
        "skills/traits/trait-suspicious-survivor/SKILL.md",
        "skills/traits/trait-loyal-protector/SKILL.md",
        "skills/traits/trait-ambitious-climber/SKILL.md",
        "skills/social_masks/mask-cold-professional/SKILL.md",
        "skills/stress_modes/stress-paranoid-controller/SKILL.md",
    ]:
        text = (root / relative).read_text(encoding="utf-8")
        assert "## Prompt Compression" in text
        assert "Body Language" in text
