from __future__ import annotations

from forwin.genesis.constants import (
    _GENESIS_REFINE_SYSTEM_PROMPT,
    _GENESIS_STAGE_HARD_RULES,
    _GENESIS_STAGE_LABELS,
    _GENESIS_STAGE_SYSTEM_PROMPTS,
    _GENESIS_SYSTEM_FOUNDATION,
)
from typing import Any
from forwin.book_state.knowledge import KNOWLEDGE_STATES
from forwin.book_state.schema import (
    ACTOR_TIERS,
    CLOCK_SCOPES,
    IMPACT_LEVELS,
    INTENT_ACTION_TYPES,
    RULE_HARDNESS,
)
from forwin.models.project import Project
from forwin.protocol.book_state import WORLD_EDGE_TYPES
from forwin.genesis.helpers import (
    _book_arc_list,
    _locked_stage_context,
    _map_node_table,
    _pack_stage_payload,
    _prompt_bullets,
    _prompt_sections,
)
from forwin.genesis.names_paths import (
    _name_hint_block,
)
from forwin.genesis.world_seed import (
    FACT_SENSITIVITIES,
    RELATION_VISIBILITIES,
    WORLD_SEED_KEYS,
)


def _world_seed_output_requirement() -> str:
    """Describe the normalized world seed contract, with enum values taken from the validators."""
    return (
        f"返回 WorldSeed JSON，顶层必须包含 {', '.join(WORLD_SEED_KEYS)}；列表字段没有内容时给空列表。"
        "calendar 为 {epoch_label, opening_minute, units}：epoch_label 是纪元名称，opening_minute 是开篇时刻距纪元起点的非负整数分钟，"
        "units 为 [{name, minutes}]，name 是本书自己的时间单位名称（不重复），minutes 是该单位折合的正整数分钟。"
        "lexicon 为 {time_unit_aliases: [{name, minutes}], rank_ladders: [{id, name, ranks}], currencies: [{id, name}], "
        "appellations: [字符串], generic_character_refs: [字符串], travel_modes: [{id, name, speed_multiplier, allowed_edge_types}], "
        "news_speed_multiplier}；时间单位别名、等级、货币、称谓和出行方式都按本书题材自拟。"
        "actors 每项为 {ref, actor_tier, location_ref, travel_mode_ids, resources, agendas, intents}，"
        f"actor_tier 取 {' / '.join(ACTOR_TIERS)}；"
        "agendas 每项为 {id, goal, motivation, priority, horizon, means_refs, constraints, secrecy_refs}，priority 为 1–5 的整数；"
        "intents 每项为 {id, agenda_id, action_type, summary, target_refs, location_ref, scheduled_in: {amount, unit}, requires_onstage, impact_level}，"
        f"action_type 取 {' / '.join(INTENT_ACTION_TYPES)}，impact_level 取 {' / '.join(IMPACT_LEVELS)}。"
        "relations 每项为 {source, target, edge_type, note, visibility, appellation}，"
        f"edge_type 取 {' / '.join(sorted(WORLD_EDGE_TYPES))}，visibility 取 {' / '.join(RELATION_VISIBILITIES)}。"
        "appellation 是 source 对 target 的称谓，即 target 相对于 source 的角色；"
        "关系边从 source 指向 target，称谓锚点读作 source 的 appellation → target。"
        "facts 每项为 {id, proposition, truth_value, sensitivity, related_refs, nobody_knows}，"
        f"sensitivity 取 {' / '.join(FACT_SENSITIVITIES)}；"
        "knowledge 每项为 {observer_ref, fact_id, state, channel}，"
        f"state 取 {' / '.join(KNOWLEDGE_STATES)}，channel 固定为 seeded。"
        "rules 每项为 {id, statement, hardness, checkable}，"
        f"hardness 取 {' / '.join(RULE_HARDNESS)}，checkable 为 null 或含非空 kind 的对象；"
        "clocks 每项为 {id, label, due_in: {amount, unit}, owner_ref, scope, on_expire}，"
        f"scope 取 {' / '.join(CLOCK_SCOPES)}；"
        "reader_promises 每项为 {id, summary, promise_type, expected_payoff_window}；reveal_candidates 每项为 {fact_id, arc_number, note}。"
        "means_refs、secrecy_refs、target_refs、related_refs 只能引用行动者 ref 或 facts 的 id；travel_mode_ids 引用 lexicon.travel_modes 的 id；"
        "intents.agenda_id 引用同一行动者的 agenda id；clocks.owner_ref 为空或引用行动者 ref。"
    )


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
                "core_cast、factions、opposition 每项必须有稳定且全局唯一的 id，默认分别为 cast-{n}、faction-{n}、opp-{n}。"
                "core_cast 使用 secrets 字符串列表，不使用 secret。"
                "relationship_axes 每项为 {source, target, edge_type, note, visibility}，source/target 引用上述实体 id，visibility 为 public 或 secret（默认 public）。"
                "long_arcs 每项为 {id, summary, owner_refs}，默认 id 为 long-arc-{n}，owner_refs 为上述实体 id 列表；reader_promises 保持字符串列表。"
                "角色、势力、对手要尽量复用已有 map/world 锚点。"
            ),
            ("relationship_axes.edge_type", sorted(WORLD_EDGE_TYPES)),
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
    elif stage_key == "world_seed":
        user_content = _prompt_sections(
            ("阶段", f"{label} ({stage_key})"),
            ("输出要求", _world_seed_output_requirement()),
            ("阶段硬约束", _prompt_bullets(_GENESIS_STAGE_HARD_RULES[stage_key])),
            ("已锁定阶段上下文（视为当前真值）", locked_context),
            ("项目 genre", project.genre),
            ("WorldBible", world_bible),
            ("地图节点表", _map_node_table(map_atlas)),
            ("StoryEngine", story_engine),
            ("卷列表", _book_arc_list(pack)),
            ("当前 WorldSeed", current_payload),
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
