"""Prompt builders for the ForWin chapter generation system.

All user-facing text in the prompts is written in Chinese so that the LLM
produces fluent Chinese web-novel prose and metadata without code-switching.
"""
from __future__ import annotations

import json

from forwin.protocol.context import ChapterContextPack
from forwin.protocol.scene import SceneOutput, ScenePlan


def _apply_skill_layers(
    messages: list[dict[str, str]],
    skill_layers: list[object] | None = None,
) -> list[dict[str, str]]:
    if not skill_layers:
        return messages
    converted: list[dict[str, str]] = []
    for item in skill_layers:
        if hasattr(item, "message_payload"):
            payload = item.message_payload()
        elif isinstance(item, dict):
            payload = {
                "role": str(item.get("role", "system")).strip() or "system",
                "content": str(item.get("content", "")),
            }
        else:
            continue
        converted.append(
            {
                "role": str(payload.get("role", "system")).strip() or "system",
                "content": str(payload.get("content", "")),
            }
        )
    if not converted:
        return messages
    result: list[dict[str, str]] = []
    index = 0
    while index < len(messages) and str(messages[index].get("role", "")).strip() == "system":
        result.append(messages[index])
        index += 1
    result.extend(converted)
    result.extend(messages[index:])
    return result


def _normalize_char_targets(
    *,
    target_chars: int,
    min_chars: int,
    max_chars: int,
) -> tuple[int, int, int]:
    min_chars = max(300, int(min_chars))
    max_chars = max(min_chars, int(max_chars))
    target_chars = max(min_chars, min(int(target_chars), max_chars))
    return target_chars, min_chars, max_chars


def _story_basics_section(context: ChapterContextPack) -> str:
    lines = [
        "【故事基本信息】",
        f"类型：{context.genre}",
        f"前提：{context.premise}",
        f"世界背景：{context.setting_summary}",
    ]
    if getattr(context, "genesis_context_refs", None):
        revision_id = str(context.genesis_context_refs.get("genesis_revision_id", "") or "")
        if revision_id:
            lines.append(f"Genesis 根层版本：{revision_id}")
    if getattr(context, "genesis_world_overview", ""):
        lines.append(f"Genesis 世界总览：{context.genesis_world_overview}")
    if getattr(context, "genesis_map_overview", ""):
        lines.append(f"Genesis 地图总览：{context.genesis_map_overview}")
    if getattr(context, "genesis_story_engine_summary", ""):
        lines.append(f"Genesis 长线引擎：{context.genesis_story_engine_summary}")
    return "\n".join(lines)


def _chapter_plan_section(context: ChapterContextPack, title: str) -> str:
    return (
        f"【{title}】\n"
        f"章节编号：第 {context.chapter_number} 章\n"
        f"章节标题：{context.chapter_plan_title}\n"
        f"一句话概要：{context.chapter_plan_one_line}\n"
        "本章目标：\n" + "\n".join(f"  · {goal}" for goal in context.chapter_goals)
    )


def _experience_overlay_section(context: ChapterContextPack) -> str | None:
    plan = getattr(context, "chapter_experience_plan", None)
    band = getattr(context, "band_delight_schedule", None)
    promise = getattr(context, "reader_promise", None)
    if not any((plan, band, promise)):
        return None
    lines = ["【读者体验 Overlay】"]
    if promise is not None:
        if promise.genre_promise:
            lines.append(f"  · 题材承诺：{promise.genre_promise}")
        if promise.pleasure_promise:
            lines.append(f"  · 阅读快感承诺：{promise.pleasure_promise}")
        if promise.core_pleasures:
            lines.append(f"  · 核心快感：{'、'.join(promise.core_pleasures[:4])}")
        if promise.cliffhanger_aggressiveness:
            lines.append(f"  · 章末钩子强度：{promise.cliffhanger_aggressiveness}")
        if promise.acceptable_drag_level:
            lines.append(f"  · 可接受拖感：{promise.acceptable_drag_level}")
        if promise.acceptable_exposition_density:
            lines.append(f"  · 可接受说明密度：{promise.acceptable_exposition_density}")
        if promise.ambiguity_mode:
            lines.append(f"  · 模糊度策略：{promise.ambiguity_mode}")
        if promise.world_legibility_target:
            lines.append(f"  · 规则可读性目标：{promise.world_legibility_target}")
    if band is not None:
        lines.append(
            f"  · 当前 band：{band.band_id}（第 {band.chapter_start}-{band.chapter_end} 章，stall guard {band.stall_guard_max_gap}）"
        )
        if band.immersion_anchor_scene_goal:
            lines.append(f"  · band 沉浸锚点：{band.immersion_anchor_scene_goal}")
        if band.curiosity_beats:
            lines.append("  · 问题梯子：")
            lines.extend(
                f"    · 第{item.chapter_hint}章：开={item.question_open}；解={item.question_resolve}；再开={item.escalated_question}"
                for item in band.curiosity_beats[:2]
            )
        if band.ambiguity_payoffs:
            lines.append("  · ambiguity payoff：")
            lines.extend(
                f"    · 第{item.chapter_hint}章 {item.payoff_type}：{item.summary}"
                for item in band.ambiguity_payoffs[:3]
            )
    if plan is not None:
        if plan.planned_reward_tags:
            lines.append(f"  · 本章计划奖励：{'、'.join(plan.planned_reward_tags)}")
        if plan.selected_template_ids:
            lines.append(f"  · 选用模板：{'、'.join(plan.selected_template_ids)}")
        if plan.hook_type:
            lines.append(f"  · 钩子类型：{plan.hook_type}")
        if plan.question_hook:
            lines.append(f"  · 本章要提的问题：{plan.question_hook}")
        if plan.question_resolution:
            lines.append(f"  · 本章至少解决：{plan.question_resolution}")
        if plan.immersion_anchors:
            lines.append("  · 沉浸锚点：")
            lines.extend(f"    · {item}" for item in plan.immersion_anchors[:3])
        if plan.progress_markers:
            lines.append("  · 进展标记：")
            lines.extend(f"    · {item}" for item in plan.progress_markers[:3])
        if plan.rule_anchors:
            lines.append("  · 规则锚点：")
            lines.extend(f"    · {item}" for item in plan.rule_anchors[:3])
        if plan.relationship_or_status_shift:
            lines.append(f"  · 关系/地位变化：{plan.relationship_or_status_shift}")
    return "\n".join(lines)


def _world_model_v4_section(context: ChapterContextPack) -> str | None:
    intent = getattr(context, "chapter_world_delta_intent", None)
    if not any(
        (
            getattr(context, "active_world_lines", None),
            getattr(context, "active_knowledge_gaps", None),
            getattr(context, "must_not_reveal", None),
            intent,
        )
    ):
        return None
    lines = ["【V4 世界模型意图】"]
    if getattr(context, "visible_world_lines", None):
        lines.append("  · 台前 world lines：" + "、".join(context.visible_world_lines))
    if getattr(context, "hidden_world_lines", None):
        lines.append("  · 幕后 world lines（只按允许的 hint 写，不要直说真相）：" + "、".join(context.hidden_world_lines))
    if getattr(context, "active_knowledge_gaps", None):
        lines.append("  · active gaps：" + "、".join(context.active_knowledge_gaps))
    if intent is not None:
        if intent.visible_delta_intents:
            lines.append("  · 本章台前推进：" + "、".join(intent.visible_delta_intents))
        if intent.hint_delta_intents:
            lines.append("  · 本章允许线索：" + "、".join(intent.hint_delta_intents))
        if intent.knowledge_delta_intents:
            lines.append("  · 本章认知推进：" + "、".join(intent.knowledge_delta_intents))
        if intent.reader_experience_intents:
            lines.append("  · 读者体验目标：" + "、".join(intent.reader_experience_intents))
        if intent.expected_observer_state_changes:
            transitions = [
                f"{observer}:{transition}"
                for observer, transition in intent.expected_observer_state_changes.items()
            ]
            lines.append("  · 预期 observer state：" + "、".join(transitions))
    if getattr(context, "must_not_reveal", None):
        lines.append("  · 绝对不得揭示：" + "、".join(context.must_not_reveal))
    if getattr(context, "fair_misdirection_requirements", None):
        lines.append("  · 公平误导证据：" + "、".join(context.fair_misdirection_requirements))
    lines.append("  · 注意：这些是写作意图，最终 canon 只由 extractor/reviewer/compiler 决定。")
    return "\n".join(lines)


def _previous_summaries_section(context: ChapterContextPack, *, limit: int) -> str | None:
    if not context.previous_chapter_summaries:
        return None
    return "【前情提要】\n" + "\n".join(
        f"  · {item}" for item in context.previous_chapter_summaries[-limit:]
    )


def _active_entities_section(context: ChapterContextPack, *, limit: int) -> str | None:
    if not context.active_entities:
        return None
    return "【当前允许命名角色】\n" + "\n".join(
        f"  · {item.name}：{item.description}"
        for item in context.active_entities[:limit]
    )


def _subworld_control_section(context: ChapterContextPack) -> str | None:
    if not any(
        (
            getattr(context, "active_subworlds", None),
            getattr(context, "allowed_entities", None),
            getattr(context, "chapter_entry_targets", None),
            getattr(context, "entity_admission_rule", ""),
        )
    ):
        return None
    lines = ["【人物准入规则】"]
    if context.active_subworlds:
        lines.append(
            "  · 当前激活 subworld："
            + "、".join(item.name for item in context.active_subworlds if str(item.name or "").strip())
        )
    if context.allowed_entities:
        lines.append(
            "  · 当前允许直接使用的命名人物："
            + "、".join(context.allowed_entities[:10])
        )
    if context.chapter_entry_targets:
        lines.append("  · 本章允许首次引入的新人物：")
        lines.extend(
            f"    · 第{item.chapter_hint}章：{item.entity_name}（{item.role_hint or '新角色'}）"
            for item in context.chapter_entry_targets[:4]
        )
    if context.entity_admission_rule:
        lines.append(f"  · 准入模式：{context.entity_admission_rule}")
    lines.extend(
        [
            "  · 规则1：命名人物只能使用允许名单里的名字。",
            "  · 规则2：若要引入新命名人物，只能使用上面的 chapter_entry_targets。",
            "  · 规则3：可以写无名泛称路人，但不要把无名角色写成新的专名人物。",
        ]
    )
    return "\n".join(lines)


def _map_runtime_section(context: ChapterContextPack) -> str | None:
    map_context = getattr(context, "map_context", {}) or {}
    if not isinstance(map_context, dict) or not int(map_context.get("map_node_count") or 0):
        return None
    lines = [
        "【地图运行时】",
        f"  · 地图规模：{int(map_context.get('map_node_count') or 0)} 个地点，{int(map_context.get('map_edge_count') or 0)} 条路线",
    ]
    active_locations = map_context.get("active_locations") if isinstance(map_context.get("active_locations"), list) else []
    if active_locations:
        lines.append("  · 当前角色位置：")
        for item in active_locations[:5]:
            if not isinstance(item, dict):
                continue
            entity_name = str(item.get("entity_name", "") or "角色").strip()
            location_name = str(item.get("location_name", "") or item.get("location_id", "") or "").strip()
            region_name = str(item.get("region_name", "") or "").strip()
            location_line = f"    · {entity_name}：{location_name}"
            if region_name:
                location_line += f" / {region_name}"
            lines.append(location_line)
            nearby_nodes = item.get("nearby_nodes") if isinstance(item.get("nearby_nodes"), list) else []
            nearby = [
                f"{str(node.get('name', '') or node.get('node_id', '')).strip()}({float(node.get('travel_time') or 0):.1f})"
                for node in nearby_nodes[:5]
                if isinstance(node, dict) and str(node.get("name", "") or node.get("node_id", "")).strip()
            ]
            if nearby:
                lines.append("      · 附近可达：" + "、".join(nearby))
    else:
        anchors = map_context.get("visible_anchor_nodes") if isinstance(map_context.get("visible_anchor_nodes"), list) else []
        if anchors:
            lines.append(
                "  · 公开锚点："
                + "、".join(
                    str(item.get("name", "") or item.get("node_id", "")).strip()
                    for item in anchors[:8]
                    if isinstance(item, dict) and str(item.get("name", "") or item.get("node_id", "")).strip()
                )
            )
    return "\n".join(lines)


def _active_threads_section(context: ChapterContextPack, *, limit: int) -> str | None:
    if not context.active_threads:
        return None
    return "【当前剧情线】\n" + "\n".join(
        f"  · {item.name}：{item.description}"
        for item in context.active_threads[:limit]
    )


def _arc_envelope_section(context: ChapterContextPack, *, compact: bool = False) -> str | None:
    envelope = getattr(context, "current_arc_envelope", None)
    if not envelope:
        return None
    if compact:
        return (
            "【当前 Arc Envelope】\n"
            f"  · tier：{envelope.source_policy_tier}\n"
            f"  · target：{envelope.resolved_target_size} 章\n"
            f"  · range：{envelope.resolved_soft_min} ~ {envelope.resolved_soft_max} 章\n"
            f"  · detailed band：{envelope.detailed_band_size} 章\n"
            f"  · frozen zone：{envelope.frozen_zone_size} 章"
        )
    return (
        "【当前 Arc Envelope】\n"
        f"  · policy tier：{envelope.source_policy_tier}\n"
        f"  · resolved target：{envelope.resolved_target_size} 章\n"
        f"  · soft range：{envelope.resolved_soft_min} ~ {envelope.resolved_soft_max} 章\n"
        f"  · detailed band：{envelope.detailed_band_size} 章\n"
        f"  · frozen zone：{envelope.frozen_zone_size} 章"
    )


def _npc_intents_section(
    context: ChapterContextPack,
    *,
    limit: int,
    detailed: bool,
) -> str | None:
    intents = getattr(context, "npc_intents", None)
    if not intents:
        return None
    if detailed:
        return "【NPC 当前意图】\n" + "\n".join(
            f"  · {item.entity_name}（{item.intent_kind}，紧急度{item.urgency}）：{item.objective}"
            + (f"；策略：{item.tactic}" if item.tactic else "")
            for item in intents[:limit]
        )
    return "【NPC 当前意图】\n" + "\n".join(
        f"  · {item.entity_name}：{item.objective}"
        for item in intents[:limit]
    )


def _world_pressure_section(context: ChapterContextPack) -> str | None:
    pressure = getattr(context, "world_pressure", None)
    if not pressure:
        return None
    return (
        "【世界压力】\n"
        f"  · 等级：{pressure.pressure_level}\n"
        f"  · 概览：{pressure.pressure_summary}"
    )


def _world_model_section(context: ChapterContextPack) -> str | None:
    world_context = getattr(context, "world_context", None)
    if not world_context or not world_context.snapshot_id:
        return None
    lines = [
        "【WorldModel 当前世界状态】",
        f"  · snapshot：第 {world_context.as_of_chapter} 章后 / {world_context.snapshot_id}",
    ]
    if world_context.active_world_conflicts:
        lines.append("  · 禁止忽略的世界矛盾：")
        lines.extend(
            f"    - {item.severity} {item.conflict_type}：{item.description}"
            for item in world_context.active_world_conflicts[:4]
        )
    if world_context.relevant_world_pages:
        lines.append("  · 相关世界页：")
        for page in world_context.relevant_world_pages[:6]:
            summary = page.markdown.split("## Current State", 1)[0]
            summary = summary.replace("\n", " ")[:220]
            lines.append(f"    - {page.title}（{page.page_type}）：{summary}")
    if world_context.active_promises:
        lines.append("  · 当前读者承诺：")
        lines.extend(f"    - {page.title}" for page in world_context.active_promises[:4])
    if world_context.active_secrets:
        lines.append("  · 秘密可见性：不得提前揭示 secret 页面中的 hidden truth，除非本章计划明确要求。")
    return "\n".join(lines)


def _audience_hints_section(context: ChapterContextPack) -> str | None:
    hints = getattr(context, "audience_hints", None)
    if not hints:
        return None
    lines: list[str] = []
    if hints.risk_flags:
        lines.append("  ⚠ 风险提示：")
        lines.extend(f"    · {h}" for h in hints.risk_flags)
    if hints.pacing_hints:
        lines.append("  节奏建议：")
        lines.extend(f"    · {h}" for h in hints.pacing_hints)
    if hints.clarity_hints:
        lines.append("  清晰度建议：")
        lines.extend(f"    · {h}" for h in hints.clarity_hints)
    if hints.character_heat_changes:
        lines.append("  角色热度：")
        lines.extend(f"    · {h}" for h in hints.character_heat_changes)
    if not lines:
        return None
    return "【读者信号提示（仅供参考，自然融入情节）】\n" + "\n".join(lines)


def _retrieved_memories_section(
    context: ChapterContextPack,
    *,
    limit: int,
    excerpt_chars: int,
) -> str | None:
    memories = getattr(context, "retrieved_memories", [])
    if not memories:
        return None
    return "【检索到的关键记忆】\n" + "\n".join(
        f"  · 第{item.chapter_number}章《{item.title}》：{item.summary or item.excerpt[:excerpt_chars]}"
        for item in memories[:limit]
    )


def _timeline_section(context: ChapterContextPack) -> str | None:
    if not context.timeline:
        return None
    return "【当前时间】\n" f"  · {context.timeline.current_time_label}"


def _join_sections(*sections: str | None) -> str:
    return "\n\n".join(section for section in sections if section)


def _scene_task_section(
    scene_plan: ScenePlan,
    *,
    include_target_chars: bool,
) -> str:
    lines = [
        "【Scene 任务】",
        f"scene 编号：{scene_plan.scene_no}",
        f"scene 目标：{scene_plan.objective}",
        "必须推进点：",
        *[f"  · {point}" for point in scene_plan.must_progress_points],
        f"时间：{scene_plan.time_hint or '承接上文'}",
        f"地点：{scene_plan.location_hint or '沿用当前场景'}",
        (
            f"参与角色：{'、'.join(scene_plan.involved_entities)}"
            if scene_plan.involved_entities
            else "参与角色：按上下文决定"
        ),
        f"结尾 micro-hook：{scene_plan.micro_hook}",
    ]
    if include_target_chars:
        lines.append(f"目标字数：约 {scene_plan.target_chars} 字。")
    return "\n".join(lines)


def _scene_prompt_sections(
    context: ChapterContextPack,
    *,
    plan_title: str,
    previous_limit: int,
    entity_limit: int,
    thread_limit: int,
    memory_limit: int,
    npc_limit: int,
    feedback_detailed: bool,
    envelope_compact: bool,
    extra_sections: list[str] | None = None,
) -> str:
    sections: list[str | None] = [
        _story_basics_section(context),
        _chapter_plan_section(context, plan_title),
        _previous_summaries_section(context, limit=previous_limit),
        _active_entities_section(context, limit=entity_limit),
        _subworld_control_section(context),
        _map_runtime_section(context),
        _active_threads_section(context, limit=thread_limit),
        _experience_overlay_section(context),
        _world_model_v4_section(context),
        _arc_envelope_section(context, compact=envelope_compact),
        _npc_intents_section(
            context,
            limit=npc_limit,
            detailed=feedback_detailed,
        ),
        _world_pressure_section(context),
        _world_model_section(context),
        _audience_hints_section(context),
        _retrieved_memories_section(context, limit=memory_limit, excerpt_chars=80),
        _timeline_section(context),
    ]
    if extra_sections:
        sections.extend(extra_sections)
    return _join_sections(*sections)


def build_single_chapter_draft_prompt(
    context: ChapterContextPack,
    *,
    target_chars: int = 2800,
    min_chars: int = 2500,
    max_chars: int = 3200,
    skill_layers: list[object] | None = None,
) -> list[dict]:
    target_chars, min_chars, max_chars = _normalize_char_targets(
        target_chars=target_chars,
        min_chars=min_chars,
        max_chars=max_chars,
    )
    user_sections = _join_sections(
        _story_basics_section(context),
        _chapter_plan_section(context, "本章计划"),
        _previous_summaries_section(context, limit=3),
        _active_entities_section(context, limit=6),
        _subworld_control_section(context),
        _map_runtime_section(context),
        _active_threads_section(context, limit=3),
        _experience_overlay_section(context),
        _world_model_v4_section(context),
        _arc_envelope_section(context, compact=False),
        _npc_intents_section(context, limit=4, detailed=True),
        _world_pressure_section(context),
        _audience_hints_section(context),
        _retrieved_memories_section(context, limit=3, excerpt_chars=80),
        _timeline_section(context),
    )

    user_content = (
        user_sections
        + "\n\n【输出要求】\n"
        f"1. 请写出一章可直接落稿的完整正文，目标正文长度 {target_chars} 到 {max_chars} 中文字，"
        f"不得低于 {min_chars} 中文字。\n"
        "2. 正文必须是自然流畅的网文叙事，不要分点，不要写提纲。\n"
        "3. 本章结尾必须留下明确钩子。\n"
        "4. 不要输出 JSON，不要输出 markdown，不要解释。\n"
        "5. 严格使用下面这个纯文本结构输出，并保留标签本身：\n"
        "<<FORWIN_TITLE>>\n"
        "这里写章节标题\n"
        "<<FORWIN_BODY>>\n"
        "这里写完整正文\n"
        "<<FORWIN_SUMMARY>>\n"
        "这里写一句到两句总结\n"
        "如果你更习惯，也可以使用【标题】【正文】【摘要】这一组标签，但整篇只保留一组最终结果。"
    )
    return _apply_skill_layers([
        {"role": "system", "content": "你是中文网文作者，只输出指定标签格式的纯文本，不要解释。"},
        {"role": "user", "content": user_content},
    ], skill_layers)


def build_preview_chapter_prompt(
    context: ChapterContextPack,
    *,
    target_chars: int = 900,
    min_chars: int = 600,
    max_chars: int = 1200,
    skill_layers: list[object] | None = None,
) -> list[dict]:
    target_chars, min_chars, max_chars = _normalize_char_targets(
        target_chars=target_chars,
        min_chars=min_chars,
        max_chars=max_chars,
    )
    user_sections = _join_sections(
        _story_basics_section(context),
        _chapter_plan_section(context, "预演章节计划"),
        _previous_summaries_section(context, limit=2),
        _active_entities_section(context, limit=5),
        _map_runtime_section(context),
        _active_threads_section(context, limit=3),
        _experience_overlay_section(context),
        _world_model_v4_section(context),
        _arc_envelope_section(context, compact=True),
        _npc_intents_section(context, limit=3, detailed=False),
        _world_pressure_section(context),
        _audience_hints_section(context),
        _retrieved_memories_section(context, limit=2, excerpt_chars=60),
        _timeline_section(context),
    )

    user_content = (
        user_sections
        + "\n\n【输出要求】\n"
        f"1. 这是 Arc 预演稿，请写出一章可读的网文正文，目标长度 {target_chars} 到 {max_chars} 中文字，"
        f"不得低于 {min_chars} 中文字。\n"
        "2. 不要输出 JSON，不要输出 markdown，不要解释。\n"
        "3. 严格使用下面这个纯文本结构输出，并保留标签本身：\n"
        "<<FORWIN_TITLE>>\n"
        "这里写章节标题\n"
        "<<FORWIN_BODY>>\n"
        "这里写完整正文\n"
        "<<FORWIN_SUMMARY>>\n"
        "这里写一句到两句总结\n"
        "如果你更习惯，也可以使用【标题】【正文】【摘要】这一组标签，但整篇只保留一组最终结果。\n"
        "4. 正文必须是自然叙事，不要列提纲。\n"
        "5. 结尾必须保留钩子。"
    )
    return _apply_skill_layers([
        {"role": "system", "content": "你是中文网文作者，只输出指定标签格式的纯文本，不要解释。"},
        {"role": "user", "content": user_content},
    ], skill_layers)


def build_scene_breakdown_prompt(
    context: ChapterContextPack,
    default_scene_count: int = 3,
    max_scene_count: int = 4,
    skill_layers: list[object] | None = None,
) -> list[dict]:
    scene_target = min(max(default_scene_count, 2), max_scene_count)
    schema = json.dumps(
        {
            "scenes": [
                {
                    "scene_no": 1,
                    "objective": "本 scene 的核心目标",
                    "must_progress_points": ["必须推进点1", "必须推进点2"],
                    "time_hint": "当前 scene 的时间点",
                    "location_hint": "当前 scene 的地点",
                    "involved_entities": ["角色A", "角色B"],
                    "micro_hook": "该 scene 结尾的小钩子",
                    "target_chars": 850,
                    "reward_beat_tag": "mystery",
                    "immersion_anchor": "让读者感到置身现场的感官锚点",
                    "progress_marker": "这一 scene 明确推进了什么",
                }
            ]
        },
        ensure_ascii=False,
        indent=2,
    )
    user_sections = _scene_prompt_sections(
        context,
        plan_title="本章计划",
        previous_limit=3,
        entity_limit=6,
        thread_limit=3,
        memory_limit=3,
        npc_limit=4,
        feedback_detailed=True,
        envelope_compact=False,
    )
    user_content = (
        f"你正在为《{context.project_title}》第 {context.chapter_number} 章拆分场景。\n\n"
        f"{user_sections}\n\n"
        f"请将本章拆成 2 到 {max_scene_count} 个 scene，默认目标是 {scene_target} 个。\n"
        "要求：\n"
        "1. 每个 scene 都要有明确目标和必须推进点。\n"
        "2. scenes 合起来必须覆盖本章全部目标。\n"
        "3. 每个 scene 还必须给出 reward_beat_tag、immersion_anchor、progress_marker。\n"
        "3. 只输出 JSON，不要解释。\n\n"
        "JSON 结构参考：\n"
        f"{schema}"
    )
    return _apply_skill_layers([
        {"role": "system", "content": "你是网文场景导演，只负责拆 scene，不写正文。"},
        {"role": "user", "content": user_content},
    ], skill_layers)


def build_scene_generation_prompt(
    context: ChapterContextPack,
    scene_plan: ScenePlan,
    skill_layers: list[object] | None = None,
) -> list[dict]:
    scene_sections = _scene_prompt_sections(
        context,
        plan_title="本章计划",
        previous_limit=2,
        entity_limit=5,
        thread_limit=3,
        memory_limit=2,
        npc_limit=3,
        feedback_detailed=False,
        envelope_compact=True,
        extra_sections=[_scene_task_section(scene_plan, include_target_chars=True)],
    )
    user_content = (
        f"{scene_sections}\n\n"
        "请显式参考当前 NPC 意图、世界压力和读者信号提示来组织这个 scene。\n\n"
        "输出要求：\n"
        "1. 只写当前这个 scene，不要偷跑到下一 scene。\n"
        "2. 不要输出 JSON，不要解释。\n"
        "3. 严格使用下面这个纯文本结构输出，并保留标签本身：\n"
        "<<FORWIN_BODY>>\n"
        "这里写 scene 正文\n"
        "<<FORWIN_SUMMARY>>\n"
        "这里写一句话 scene 小结\n"
        "<<FORWIN_TIME>>\n"
        "这里写 scene 时间点，没有就写沿用上一场景\n"
        "<<FORWIN_LOCATION>>\n"
        "这里写 scene 地点，没有就写沿用当前地点\n"
        "<<FORWIN_ENTITIES>>\n"
        "这里写本 scene 直接出场或被明确提及的实体，使用顿号或逗号分隔\n"
        "<<FORWIN_REWARD>>\n"
        "这里写 reward tag，必须从 power、social、justice、mystery、emotion 中选一个；拿不准就写 scene_plan 里的 reward tag\n"
        "<<FORWIN_IMMERSION>>\n"
        "这里写感官锚点\n"
        "<<FORWIN_PROGRESS>>\n"
        "这里写这一 scene 推进了什么\n"
        "<<FORWIN_CONTINUITY_ANCHOR>>\n"
        "这里写下一 scene 必须承接的动作、信息或情绪锚点\n"
        "<<FORWIN_UNRESOLVED_HOOK>>\n"
        "这里写本 scene 结尾仍未解决的小钩子\n"
        "<<FORWIN_NEXT_BRIDGE>>\n"
        "这里写下一 scene 最自然的衔接方向\n"
        "<<FORWIN_TIME_CONTINUITY>>\n"
        "这里写时间如何承接，没有变化就写沿用上一场景\n"
        "<<FORWIN_LOCATION_CONTINUITY>>\n"
        "这里写地点如何承接，没有变化就写沿用当前地点\n"
        "<<FORWIN_CHARACTER_FOCUS>>\n"
        "这里写下一 scene 应优先关注的角色，使用顿号或逗号分隔"
    )
    return _apply_skill_layers([
        {"role": "system", "content": "你是中文网文写手，只负责写单个 scene，并按指定标签输出纯文本。"},
        {"role": "user", "content": user_content},
    ], skill_layers)


def build_scene_stitch_prompt(
    context: ChapterContextPack,
    scene_outputs: list[SceneOutput],
    skill_layers: list[object] | None = None,
) -> list[dict]:
    stitched_input = "\n\n".join(
        (
            f"[Scene {scene.scene_no}]\n"
            f"目标：{scene.scene_objective}\n"
            f"摘要：{scene.micro_summary}\n"
            f"continuation：锚点={scene.continuation.continuity_anchor}；"
            f"未解钩子={scene.continuation.unresolved_micro_hook}；"
            f"下一衔接={scene.continuation.next_scene_bridge}；"
            f"时间={scene.continuation.time_continuity}；地点={scene.continuation.location_continuity}；"
            f"角色焦点={'、'.join(scene.continuation.character_focus)}\n"
            f"正文：\n{scene.text}"
        )
        for scene in scene_outputs
    )
    user_sections = _scene_prompt_sections(
        context,
        plan_title="本章计划",
        previous_limit=2,
        entity_limit=5,
        thread_limit=3,
        memory_limit=2,
        npc_limit=3,
        feedback_detailed=False,
        envelope_compact=True,
        extra_sections=[
            f"请把以下 scenes 拼接成《{context.project_title}》第 {context.chapter_number} 章的完整章节。",
            "【待拼接 Scenes】\n" + stitched_input,
        ],
    )
    user_content = (
        f"{user_sections}\n\n"
        "要求：\n"
        "1. 保持人称、文风、时间地点衔接一致。\n"
        "2. 必须显式利用每个 scene 的 continuation 信息承接动作、时间、地点和角色焦点。\n"
        "3. 只做轻量衔接和润色，不要改写核心事件。\n"
        "4. 保留章末钩子。\n"
        "5. 不要输出 JSON，不要解释。\n"
        "6. 严格使用下面这个纯文本结构输出，并保留标签本身：\n"
        "<<FORWIN_TITLE>>\n"
        "这里写章节标题\n"
        "<<FORWIN_BODY>>\n"
        "这里写拼接后的完整章节正文\n"
        "<<FORWIN_SUMMARY>>\n"
        "这里写 1-2 句章节总结"
    )
    return _apply_skill_layers([
        {"role": "system", "content": "你是章节拼接编辑，只做 scenes 的轻量 stitch，并按指定标签输出纯文本。"},
        {"role": "user", "content": user_content},
    ], skill_layers)


def build_state_event_extraction_prompt(
    context: ChapterContextPack,
    chapter_title: str,
    chapter_body: str,
) -> list[dict]:
    schema = json.dumps(
        {
            "state_changes": [
                {
                    "entity_name": "实体名称",
                    "entity_kind": "character",
                    "field": "location",
                    "old_value": "旧值",
                    "new_value": "新值",
                    "reason": "变化原因",
                }
            ],
            "new_events": [
                {
                    "summary": "事件摘要",
                    "significance": "major",
                    "involved_entity_names": ["实体A"],
                    "roles": ["protagonist"],
                }
            ],
        },
        ensure_ascii=False,
        indent=2,
    )
    user_content = (
        f"请从《{context.project_title}》第 {context.chapter_number} 章《{chapter_title}》中抽取结构化事实。\n"
        "要求：\n"
        "1. 只抽取正文真实发生的内容。\n"
        "2. state_changes.entity_kind 只能是 character、location、faction、item、rule 之一。\n"
        "3. new_events.significance 只能是 major、minor、background 之一。\n"
        "4. 没有对应内容就返回空数组。\n"
        "5. 只输出 JSON。\n\n"
        f"正文：\n{chapter_body}\n\n{schema}"
    )
    return [
        {"role": "system", "content": "你是结构化提取器，只负责从正文抽取状态变化和事件。"},
        {"role": "user", "content": user_content},
    ]


def build_thread_time_extraction_prompt(
    context: ChapterContextPack,
    chapter_title: str,
    chapter_body: str,
) -> list[dict]:
    schema = json.dumps(
        {
            "thread_beats": [
                {
                    "thread_name": "线索名",
                    "beat_type": "setup",
                    "description": "推进描述",
                }
            ],
            "time_advance": {
                "new_time_label": "新的时间标签",
                "duration_description": "时间推进描述",
            },
        },
        ensure_ascii=False,
        indent=2,
    )
    user_content = (
        f"请从《{context.project_title}》第 {context.chapter_number} 章《{chapter_title}》中抽取剧情线推进和时间推进信息。\n"
        "要求：\n"
        "1. 只抽取正文真实发生的内容。\n"
        "2. thread_beats.beat_type 只能是 setup、escalation、twist、climax、resolution 之一。\n"
        "3. 若没有时间推进，time_advance 设为 null。\n"
        "4. 若没有明确的剧情线推进，thread_beats 返回空数组。\n"
        "5. 只输出 JSON。\n\n"
        f"正文：\n{chapter_body}\n\n{schema}"
    )
    return [
        {"role": "system", "content": "你是结构化提取器，只负责从正文抽取剧情线推进和时间推进。"},
        {"role": "user", "content": user_content},
    ]


def build_lore_timeline_notes_extraction_prompt(
    context: ChapterContextPack,
    chapter_title: str,
    chapter_body: str,
) -> list[dict]:
    schema = json.dumps(
        {
            "lore_candidates": [
                {
                    "subject_name": "设定/规则/地点/组织/物品名称",
                    "subject_type": "rule",
                    "description": "正文中新出现或被强化的设定事实",
                    "evidence_refs": ["body:关键短语"],
                    "confidence": 0.7,
                }
            ],
            "timeline_hints": [
                {
                    "current_time_label": "当前章节明确时间",
                    "projected_time_label": "下一章自然承接时间",
                    "duration_hint": "时间跨度提示",
                    "evidence_refs": ["body:关键短语"],
                    "confidence": 0.7,
                }
            ],
            "writer_notes": [
                {
                    "note_type": "continuity",
                    "target_name": "角色/线索/设定",
                    "note": "下一章写作必须记住的提示",
                    "evidence_refs": ["body:关键短语"],
                }
            ],
            "entity_mentions": [
                {
                    "entity_name": "角色名",
                    "entity_kind": "character",
                    "is_named": True,
                    "is_on_stage": True,
                    "evidence_refs": ["body:关键短语"],
                }
            ],
        },
        ensure_ascii=False,
        indent=2,
    )
    user_content = (
        f"请从《{context.project_title}》第 {context.chapter_number} 章《{chapter_title}》中抽取写作续航信息。\n"
        "要求：\n"
        "1. 只抽取正文中有证据的设定、时间提示和下一章写作注意事项。\n"
        "2. lore_candidates 只放可进入后续设定候选池的事实，不要写评价。\n"
        "3. timeline_hints 用来帮助下一章保持时间连续，不确定就返回空数组。\n"
        "4. writer_notes 是给下一章 writer 的短提示，不能改 canon。\n"
        "5. entity_mentions 只记录正文中出现的命名实体，尤其是命名角色；泛称路人不要记为 is_named=true。\n"
        "6. evidence_refs 使用 body:短语 格式指向正文证据。\n"
        "7. 没有对应内容就返回空数组。\n"
        "8. 只输出 JSON。\n\n"
        f"正文：\n{chapter_body}\n\n{schema}"
    )
    return [
        {"role": "system", "content": "你是写作续航信息抽取器，只输出 JSON。"},
        {"role": "user", "content": user_content},
    ]


def build_structured_extraction_prompt(
    context: ChapterContextPack,
    chapter_title: str,
    chapter_body: str,
) -> list[dict]:
    return build_state_event_extraction_prompt(context, chapter_title, chapter_body)
