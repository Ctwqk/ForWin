"""Prompt builders for the ForWin chapter generation system.

All user-facing text in the prompts is written in Chinese so that the LLM
produces fluent Chinese web-novel prose and metadata without code-switching.
"""
from __future__ import annotations

import json
import re

from forwin.canon_names import canon_name_anchor_lines, extract_canon_name_anchors
from forwin.obsidian.frontmatter import frontmatter_hidden, parse_frontmatter, parse_sections
from forwin.protocol.book_state import WorldNodeType
from forwin.protocol.context import ChapterContextPack
from forwin.protocol.world_model import WorldModelPage


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
    protagonist_name = _extract_protagonist_name(context)
    if protagonist_name:
        lines.extend(
            [
                f"主角姓名：{protagonist_name}",
                (
                    "主角命名约束：正文叙事必须用这个姓名或自然代词承接，不要用“工作人员”、"
                    "“主角”、“主人公”、“相关人员”等泛称替代主角；第 1 章正文前 300 字内必须出现主角姓名。"
                    "无名追踪者、守卫或操作员也要写成具体职能称谓或阵营代号，例如“系统巡检员”，"
                    "不要把“工作人员”单独作为角色标签或揭示。"
                ),
                (
                    "倒计时约束：如果同章出现多个计时器，必须在正文中明确区分用途，例如“局部窗口剩余4小时”"
                    "和“主线倒计时剩余7天”是两个不同倒计时；不要让较短局部计时器和主线倒计时混在一起。"
                ),
                (
                    "人物身份连续性约束：不得突然改变已登场命名人物的性别、代词、亲属关系或辈分；"
                    "不要把前文女性角色改写成叔叔、父亲、祖父、男人，也不要把既有男性角色改写成母亲、姐姐或女人。"
                    "亲属/性别反转只有在前文已有明确伏笔且本章完整解释时才允许。"
                ),
            ]
        )
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
    facts = context.genesis_reference_facts
    omitted = context.genesis_reference_omitted_count
    if facts or omitted:
        lines.extend([
            "【Genesis 来源事实】",
            (
                "以下原文约束对应的写前历史与根规则；不能用新计划或正文静默改写同一历史事件。"
                "当前状态仍以已接纳 BookState 为准，长线目标不等于已经发生。"
                "character_secret 是作者掌握的背景，不代表其他人物已知，也不要求本章揭露；"
                "必须遵守人物知情边界和 must_not_reveal。区分客观事实、角色说法和不同历史事件。"
            ),
            json.dumps([fact.model_dump(mode="json") for fact in facts], ensure_ascii=False),
        ])
        if omitted:
            lines.append(f"受上下文预算限制，{omitted} 条来源事实未完整提供；缺失不表示不存在，不得据此编造历史。")
    return "\n".join(lines)


def _extract_protagonist_name(context: ChapterContextPack) -> str:
    candidates = [
        str(getattr(context, "premise", "") or ""),
        str(getattr(context, "genesis_story_engine_summary", "") or ""),
    ]
    for text in candidates:
        match = re.search(r"(?:主角|主人公|主视角)\s*(?:[：:是为]\s*|\s+)([\u4e00-\u9fff]{2,4})", text)
        if match:
            name = match.group(1).strip()
            if name and name not in {"工作人员", "相关人员", "主人公", "主角"}:
                return name
    return ""


def _chapter_plan_section(context: ChapterContextPack, title: str) -> str:
    return (
        f"【{title}】\n"
        f"章节编号：第 {context.chapter_number} 章\n"
        f"章节标题：{context.chapter_plan_title}\n"
        f"一句话概要：{context.chapter_plan_one_line}\n"
        "本章目标：\n" + "\n".join(f"  · {goal}" for goal in context.chapter_goals)
    )


def _clip_trope_text(text: str, limit: int = 260) -> str:
    clipped = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(clipped) <= limit:
        return clipped
    return clipped[: max(0, limit - 1)].rstrip() + "…"


def _selected_trope_instruction_lines(template_ids: list[str]) -> list[str]:
    if not template_ids:
        return []

    from forwin.protocol.trope_library import trope_template_index

    template_by_id = trope_template_index()
    lines = [
        "  · 本章爽点指令：",
        "    · 执行约束：必须按欲望建立、阻力加压、爽点兑现、余波钩子落成动作链，不能只贴关键词。",
    ]
    emitted_count = 0
    for template_id in template_ids:
        if emitted_count >= 2:
            break
        template = template_by_id.get(template_id)
        if template is None:
            continue
        emitted_count += 1
        name = _clip_trope_text(getattr(template, "display_name", "") or template_id, limit=80)
        lines.append(f"    · {name}")
        for label, field_name in (
            ("欲望建立", "desire_setup"),
            ("阻力加压", "resistance"),
            ("爽点兑现", "payoff"),
            ("余波钩子", "aftermath"),
        ):
            value = _clip_trope_text(getattr(template, field_name, ""))
            if value:
                lines.append(f"      · {label}：{value}")
        anti_patterns = []
        for item in getattr(template, "anti_patterns", [])[:3]:
            clipped_item = _clip_trope_text(item, limit=80)
            if clipped_item:
                anti_patterns.append(clipped_item)
        if anti_patterns:
            lines.append(f"      · 禁止：{'；'.join(anti_patterns)}")

    if emitted_count <= 0:
        return []
    return lines


def _experience_overlay_section(context: ChapterContextPack) -> str | None:
    plan = getattr(context, "chapter_experience_plan", None)
    band = getattr(context, "band_delight_schedule", None)
    promise = getattr(context, "reader_promise", None)
    if not any((plan, band, promise)):
        return None
    lines: list[str] = []
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
        contract = getattr(band, "band_obligation_contract", None)
        if contract is not None and getattr(contract, "open_obligations", None):
            lines.append("  · band 叙事义务：")
            for obligation_id in contract.open_obligations[:5]:
                payoff = contract.payoff_tests.get(obligation_id, "") if contract.payoff_tests else ""
                affected = contract.affected_chapters.get(obligation_id, []) if contract.affected_chapters else []
                marker = "本 band 结束前必须清偿" if obligation_id in contract.must_resolve_by_band_end else "允许后续 band 继续推进"
                chapter_text = f"；影响章节：{', '.join(str(item) for item in affected)}" if affected else ""
                payoff_text = f"；payoff：{payoff}" if payoff else ""
                lines.append(f"    · {obligation_id}：{marker}{chapter_text}{payoff_text}")
    if plan is not None:
        if plan.planned_reward_tags:
            lines.append(f"  · 本章计划奖励：{'、'.join(plan.planned_reward_tags)}")
        if plan.selected_template_ids:
            lines.extend(_selected_trope_instruction_lines(list(plan.selected_template_ids)))
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
            lines.extend(f"    · {item}" for item in plan.rule_anchors[:8])
        if plan.relationship_or_status_shift:
            lines.append(f"  · 关系/地位变化：{plan.relationship_or_status_shift}")
    if not lines:
        return None
    return "\n".join(["【读者体验 Overlay】", *lines])


def _world_intent_section(context: ChapterContextPack) -> str | None:
    intent = getattr(context, "chapter_world_delta_intent", None)
    if not any(
        (
            getattr(context, "active_world_lines", None),
            getattr(context, "active_knowledge_gaps", None),
            getattr(context, "must_not_reveal", None),
            context.character_cognition_states,
            context.observer_visibility_states,
            intent,
        )
    ):
        return None
    lines = ["【世界状态意图】"]
    if context.character_cognition_states:
        lines.append("  · 已有的人物认知（缺项为未知）：" + json.dumps(context.character_cognition_states, ensure_ascii=False))
    if context.observer_visibility_states:
        lines.append("  · 观察者可见状态：" + json.dumps(context.observer_visibility_states, ensure_ascii=False))
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


def _repair_contract_section(context: ChapterContextPack) -> str | None:
    contract = context.repair_contract
    if contract is None:
        return None
    lines = [
        "【本次重写合同】",
        "以下要求适用于本次修复的完整最终正文；必须同时满足，不能只修复前几项。",
        "以已接纳 Canon 为事实依据；修复要求不授权改写 Canon、提前揭密或重演已完成事件。",
    ]
    for kind, label in (
        ("must_fix", "必须修复"),
        ("must_preserve", "必须保留"),
        ("must_not_reveal", "不得揭示"),
    ):
        for index, value in enumerate(getattr(contract, kind), start=1):
            lines.append(f"  · {label} {index}：{value}")
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


def _personality_context_section(context: ChapterContextPack, *, limit: int = 6) -> str | None:
    raw_contexts = list(getattr(context, "active_personality_contexts", []) or [])
    if not raw_contexts:
        return None
    lines = ["【人物性格运行时】"]
    for raw in raw_contexts[:limit]:
        item = raw.model_dump(mode="json") if hasattr(raw, "model_dump") else dict(raw or {})
        character_name = str(item.get("character_name") or item.get("character_id") or "").strip()
        active_skills = item.get("active_skills") if isinstance(item.get("active_skills"), dict) else {}
        bias = item.get("current_behavior_bias") if isinstance(item.get("current_behavior_bias"), dict) else {}
        constraints = [str(value) for value in (item.get("constraints") or []) if str(value).strip()]
        skill_parts: list[str] = []
        for key, label in (
            ("dominant", "主"),
            ("secondary", "副"),
            ("social_mask", "面具"),
            ("stress_mode", "压力"),
            ("relationship_pattern", "关系"),
        ):
            values = [str(value) for value in (active_skills.get(key) or []) if str(value).strip()]
            if values:
                skill_parts.append(f"{label}={','.join(values[:3])}")
        lines.append(f"  · {character_name}：" + ("；".join(skill_parts) if skill_parts else "无激活 skill"))
        for key, label in (
            ("perception", "感知"),
            ("decision", "决策"),
            ("dialogue", "对白"),
            ("body_language", "动作"),
            ("relationship_behavior", "关系"),
            ("stress_behavior", "压力"),
        ):
            values = [str(value) for value in (bias.get(key) or []) if str(value).strip()]
            if values:
                lines.append(f"    · {label}：" + "；".join(values[:3]))
        if constraints:
            lines.append("    · 约束：" + "；".join(constraints[:3]))
    lines.append("  · 人格 skill 只控制倾向，不覆盖 canon、当前剧情状态、scene 目标或关系状态。")
    return "\n".join(lines)


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
            "  · 规则4：不得把前情中已经出现的姓名扩写、替换或另造别名，必须逐字沿用上下文中的写法。",
        ]
    )
    return "\n".join(lines)


def _map_runtime_section(context: ChapterContextPack) -> str | None:
    from types import SimpleNamespace

    from forwin.map.visibility import is_writer_visible_map_edge

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
                f"{str(node.get('name', '') or node.get('node_id', '')).strip()}"
                + (f"({float(node['travel_time']):.3g}小时)" if node.get("travel_time") is not None else "(耗时未知)")
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
    graph = map_context.get("review_graph")
    if isinstance(graph, dict):
        names = {node.get("id"): str(node.get("name") or node.get("id")) for node in graph.get("map_nodes", []) if isinstance(node, dict)}
        routes = [edge for edge in graph.get("map_edges", []) if isinstance(edge, dict) and is_writer_visible_map_edge(SimpleNamespace(**edge))]
        if routes:
            lines.append("  · 当前BookMap路线（小时为数值单位；优先于Genesis写前总览；未知耗时不可当作零）：")
            lines.append("  · 原文通行条件不代表人物已获许可；风险说明不代表事件已经发生。")
        if graph.get("available") is False:
            lines.append("  · 当前地图上下文不完整；只展示已提供路线，未列出的路线和条件保持未知。")
        for edge in routes[:24]:
            metadata = edge.get("metadata") if isinstance(edge.get("metadata"), dict) else {}
            hours = edge.get("travel_time") if metadata.get("travel_time_known") is not False else None
            duration = f"{float(hours):.3g}小时" if hours is not None else "耗时未知"
            source_cost = str(metadata.get("source_travel_cost") or "")
            mode = str(metadata.get("source_mode") or "")
            controls = str(metadata.get("source_control") or "")
            hazards = str(metadata.get("source_hazard") or "")
            access_rule = str(edge.get("access_rule_id") or "")
            arrow = "↔" if edge.get("bidirectional") else "→"
            lines.append(f"    · {names.get(edge.get('from_node_id'), edge.get('from_node_id'))}{arrow}{names.get(edge.get('to_node_id'), edge.get('to_node_id'))}：{duration}；来源约束：{source_cost or '未提供'}{('；交通方式：' + mode) if mode else ''}{('；通行条件：' + controls) if controls else ''}")
            if hazards:
                lines.append("      · 风险说明：" + hazards)
            if access_rule:
                lines.append(f"      · 通行规则引用：{access_rule}；须满足对应规则，未知规则不视为许可。")
        if len(routes) > 24:
            lines.append(f"  · 另有{len(routes) - 24}条可见路线未展开，不代表不存在。")
    return "\n".join(lines)


def _active_threads_section(context: ChapterContextPack, *, limit: int) -> str | None:
    if not context.active_threads:
        return None
    lines = ["【当前剧情线】"]
    indexed_threads = list(enumerate(context.active_threads))

    def sort_key(pair: tuple[int, object]) -> tuple[int, int]:
        index, item = pair
        status = str(getattr(item, "status", "") or "").strip().lower()
        if status == "active":
            status_rank = 0
        elif status in {"resolved", "abandoned"}:
            status_rank = 2
        else:
            status_rank = 1
        return status_rank, index

    for _, item in sorted(indexed_threads, key=sort_key)[:limit]:
        lines.append(f"  · {item.name}：{item.description}")
        for beat in list(getattr(item, "recent_beats", []) or [])[:3]:
            beat_text = str(beat or "").strip()
            if beat_text:
                lines.append(f"    · 最近推进：{beat_text}")
    return "\n".join(lines)


def _canon_name_anchor_section(context: ChapterContextPack) -> str | None:
    texts: list[str] = []
    for thread in list(getattr(context, "active_threads", []) or []):
        texts.append(str(getattr(thread, "description", "") or ""))
        texts.extend(str(beat or "") for beat in (getattr(thread, "recent_beats", []) or []))
    lines = canon_name_anchor_lines(extract_canon_name_anchors(texts))
    if not lines:
        return None
    return (
        "【Canon 命名锚点】\n"
        + "\n".join(f"  · {line}；必须逐字沿用，不得扩写、替换或另造别名。" for line in lines)
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


def _world_pressure_section(context: ChapterContextPack) -> str | None:
    pressure = getattr(context, "world_pressure", None)
    if not pressure:
        return None
    return (
        "【世界压力】\n"
        f"  · 等级：{pressure.pressure_level}\n"
        f"  · 概览：{pressure.pressure_summary}"
    )


def _writer_page_frontmatter(page: WorldModelPage) -> dict | None:
    embedded, _ = parse_frontmatter(page.markdown)
    if any(frontmatter_hidden(metadata) for metadata in (
        embedded, page.frontmatter, {"node_type": page.page_type, "status": page.status},
    )):
        return None
    return {**embedded, **page.frontmatter}


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
    visible_pages = [
        (page, metadata)
        for page in world_context.relevant_world_pages
        if (metadata := _writer_page_frontmatter(page)) is not None
    ]
    if visible_pages:
        lines.append("  · 相关世界页：")
        lines.append("    truth_relation=false/unknown 不作客观真相；读者可见不代表所有人物已知。")
        for page, metadata in visible_pages[:6]:
            sections = parse_sections(page.markdown)
            individual = (
                page.page_type in {item.value for item in WorldNodeType} | {"map_node"}
                and (metadata.get("node_id") or (
                    page.canonical_source_type == "book_state_node" and page.canonical_source_id
                ))
            )
            section_fields = [("Canon Summary", "概要")]
            if individual:
                section_fields.append(("Current State", "状态"))
            parts = [
                (label, " ".join(sections.get(key, "").split()))
                for key, label in section_fields
                if sections.get(key, "").strip() not in {"", "_empty_", "_none_"}
            ]
            if not parts:
                continue
            # Share the existing per-page text allowance so state cannot be
            # crowded out by either frontmatter or a long summary.
            limit = 220 // len(parts)
            content = "；".join(f"{label}：{text[:limit]}" for label, text in parts)
            visibility = metadata.get("visibility", "unknown")
            truth = metadata.get("truth_relation", "unknown")
            lines.append(
                f"    - {page.title}（{page.page_type}；visibility={visibility}；"
                f"truth_relation={truth}）：{content}"
            )
    visible_promises = [
        page for page in world_context.active_promises
        if _writer_page_frontmatter(page) is not None
    ]
    if visible_promises:
        lines.append("  · 当前读者承诺：")
        lines.extend(f"    - {page.title}" for page in visible_promises[:4])
    if world_context.active_secrets:
        lines.append("  · 秘密可见性：不得提前揭示 secret 页面中的 hidden truth，除非本章计划明确要求。")
    return "\n".join(lines)


def _audience_hints_section(context: ChapterContextPack) -> str | None:
    from forwin.writer.feedback_input import render_feedback_hints

    hints = getattr(context, "audience_hints", None)
    return render_feedback_hints(hints) if hints else None


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


__all__ = [
    '_apply_skill_layers',
    '_normalize_char_targets',
    '_story_basics_section',
    '_extract_protagonist_name',
    '_chapter_plan_section',
    '_experience_overlay_section',
    '_world_intent_section',
    '_previous_summaries_section',
    '_active_entities_section',
    '_personality_context_section',
    '_subworld_control_section',
    '_map_runtime_section',
    '_active_threads_section',
    '_canon_name_anchor_section',
    '_arc_envelope_section',
    '_world_pressure_section',
    '_world_model_section',
    '_audience_hints_section',
    '_retrieved_memories_section',
    '_timeline_section',
]
