from __future__ import annotations

from forwin.book_genesis_core.constants import *
from forwin.book_genesis_core.helpers import *
from forwin.book_genesis_core.fallbacks import *
from forwin.book_genesis_core.names_paths import *

def _build_stage_generation_messages(
    self,
    *,
    project: Project,
    pack: dict[str, Any],
    stage_key: str,
    fallback: dict[str, Any],
) -> list[dict[str, str]]:
    label = _GENESIS_STAGE_LABELS.get(stage_key, stage_key)
    current_payload = _pack_stage_payload(pack, stage_key)
    locked_context = _locked_stage_context(pack, stage_key)
    book_brief = pack.get("book_brief") if isinstance(pack.get("book_brief"), dict) else {}
    world_root = _pack_stage_payload(pack, "world")
    world_bible = world_root.get("world_bible") if isinstance(world_root.get("world_bible"), dict) else {}
    map_atlas = world_root.get("map_atlas") if isinstance(world_root.get("map_atlas"), dict) else {}
    story_engine = world_root.get("story_engine") if isinstance(world_root.get("story_engine"), dict) else {}

    if stage_key == "brief":
        user_content = _prompt_sections(
            ("阶段", f"{label} ({stage_key})"),
            ("输出要求", "返回 BookBrief JSON，至少包含 title、one_line、audience、core_emotion、core_delight、promise、guardrails。"),
            ("阶段硬约束", _prompt_bullets(_GENESIS_STAGE_HARD_RULES[stage_key])),
            ("已锁定阶段上下文（视为当前真值）", locked_context),
            ("新书输入", pack.get("book_brief") or {}),
            ("参考骨架", fallback),
        )
    elif stage_key == "world":
        user_content = _prompt_sections(
            ("阶段", f"{label} ({stage_key})"),
            (
                "输出要求",
                "返回统一 WorldRoot JSON，至少包含 minimum_world_system、minimum_extension_pack、world_bible、map_atlas、story_engine、"
                "institution_profiles、resource_economy_profiles、world_extensions、template_libraries。"
                "其中 world_bible 至少包含 overview、axioms、history_slice、naming_style、forbidden_zones、culture_profiles。"
            ),
            ("阶段硬约束", _prompt_bullets(_GENESIS_STAGE_HARD_RULES[stage_key])),
            ("已锁定阶段上下文（视为当前真值）", locked_context),
            ("BookBrief", book_brief),
            ("当前 WorldRoot", current_payload),
            ("参考骨架", fallback),
        )
    elif stage_key == "map":
        user_content = _prompt_sections(
            ("阶段", f"{label} ({stage_key})"),
            ("输出要求", "返回 MapAtlas JSON，至少包含 overview、topology_rules、submaps、regions、nodes、edges。"),
            ("阶段硬约束", _prompt_bullets(_GENESIS_STAGE_HARD_RULES[stage_key])),
            ("已锁定阶段上下文（视为当前真值）", locked_context),
            ("BookBrief", book_brief),
            ("WorldBible", world_bible),
            ("当前 MapAtlas", current_payload),
            ("命名辅助", _name_hint_block(world_bible, seed_prefix=f"{project.id}:map")),
            ("参考骨架", fallback),
        )
    elif stage_key == "story_engine":
        user_content = _prompt_sections(
            ("阶段", f"{label} ({stage_key})"),
            (
                "输出要求",
                "返回 StoryEngine JSON，至少包含 core_cast、factions、opposition、relationship_axes、reader_promises、long_arcs。"
                "角色、势力、对手要尽量复用已有 map/world 锚点。"
            ),
            ("阶段硬约束", _prompt_bullets(_GENESIS_STAGE_HARD_RULES[stage_key])),
            ("已锁定阶段上下文（视为当前真值）", locked_context),
            ("BookBrief", book_brief),
            ("WorldBible", world_bible),
            ("MapAtlas", map_atlas),
            ("当前 StoryEngine", current_payload),
            ("命名辅助", _name_hint_block(world_bible, seed_prefix=f"{project.id}:story_engine")),
            ("参考骨架", fallback),
        )
    elif stage_key == "book_blueprint":
        user_content = _prompt_sections(
            ("阶段", f"{label} ({stage_key})"),
            ("输出要求", "返回 BookArcBlueprint JSON，顶层至少包含 summary 和 arcs；每个 arc 都要给出完整章节区间与目标。"),
            ("阶段硬约束", _prompt_bullets(_GENESIS_STAGE_HARD_RULES[stage_key])),
            ("已锁定阶段上下文（视为当前真值）", locked_context),
            ("BookBrief", book_brief),
            ("WorldBible", world_bible),
            ("StoryEngine", story_engine),
            ("当前 BookArcBlueprint", current_payload),
            ("建议 Arc 尺寸骨架", fallback.get("arcs") or []),
            ("参考骨架", fallback),
        )
    elif stage_key == "bootstrap":
        user_content = _prompt_sections(
            ("阶段", f"{label} ({stage_key})"),
            (
                "输出要求",
                "返回 ExecutionBootstrap JSON，至少包含 operation_mode、governance_defaults、root_ready、start_policy。"
                "这是执行契约，不是新一轮世界观创作。"
            ),
            ("阶段硬约束", _prompt_bullets(_GENESIS_STAGE_HARD_RULES[stage_key])),
            ("已锁定阶段上下文（视为当前真值）", locked_context),
            ("BookBrief", book_brief),
            ("WorldBible", world_bible),
            ("BookArcBlueprint", pack.get("book_arc_blueprint") or {}),
            ("当前 ExecutionBootstrap", current_payload),
            ("参考骨架", fallback),
        )
    else:
        raise ValueError(f"未知 Genesis stage: {stage_key}")

    return [
        {"role": "system", "content": _GENESIS_SYSTEM_FOUNDATION},
        {"role": "system", "content": _GENESIS_STAGE_SYSTEM_PROMPTS.get(stage_key, "")},
        {"role": "user", "content": user_content},
    ]

def _build_stage_refine_messages(
    self,
    *,
    pack: dict[str, Any],
    stage_key: str,
    instruction: str,
    target_path: str,
    current_payload: dict[str, Any],
    support_context: dict[str, Any],
    fallback_stage_payload: dict[str, Any],
    current_target: Any | None = None,
    wrap_scalar_value: bool = False,
) -> list[dict[str, str]]:
    label = _GENESIS_STAGE_LABELS.get(stage_key, stage_key)
    scope_prompt = (
        "当前是定向改写模式。只返回目标值对应的 JSON 对象，格式必须是 {\"value\": <更新后的 JSON 值>}。"
        if wrap_scalar_value
        else "当前是定向改写模式。只返回目标子对象的新 JSON，不要把整个阶段或兄弟字段一起返回。"
        if target_path
        else "当前是阶段全量改写模式。请返回更新后的完整阶段 JSON。"
    )

    sections: list[tuple[str, Any]] = [
        ("阶段", f"{label} ({stage_key})"),
        ("用户指令", instruction),
        ("阶段硬约束", _prompt_bullets(_GENESIS_STAGE_HARD_RULES.get(stage_key, []))),
        (
            "改写原则",
            _prompt_bullets(
                [
                    "只处理用户明确要求的变化，避免无关重写。",
                    "尽量保留已有 id、命名体系、culture_profile_id、parent_region_id、势力归属、章节区间等稳定锚点。",
                    "新增内容必须与上游 Genesis 上下文兼容，不要制造自相矛盾的新设定。",
                ]
            ),
        ),
    ]
    if target_path:
        sections.append(("目标路径", target_path))
        sections.append(("当前目标值", current_target))
    sections.extend(
        [
            ("当前阶段 JSON", current_payload),
            ("上游 Genesis 上下文", support_context),
            ("参考骨架", fallback_stage_payload),
        ]
    )
    return [
        {"role": "system", "content": _GENESIS_REFINE_SYSTEM_PROMPT},
        {"role": "system", "content": _GENESIS_STAGE_SYSTEM_PROMPTS.get(stage_key, "")},
        {"role": "system", "content": scope_prompt},
        {"role": "user", "content": _prompt_sections(*sections)},
    ]



__all__ = ['_build_stage_generation_messages', '_build_stage_refine_messages']
