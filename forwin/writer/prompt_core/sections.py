"""Prompt builders for the ForWin chapter generation system.

All user-facing text in the prompts is written in Chinese so that the LLM
produces fluent Chinese web-novel prose and metadata without code-switching.
"""
from __future__ import annotations

import re
from collections.abc import Sequence

from forwin.canon_names import CanonNameAnchor, canon_name_anchor_lines
from forwin.protocol.context import ChapterContextPack


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
                    "无名追踪者、守卫或操作员也要写成具体职能称谓或阵营代号，"
                    "不要把“工作人员”单独作为角色标签或揭示。"
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
    section = (
        f"【{title}】\n"
        f"章节编号：第 {context.chapter_number} 章\n"
        f"章节标题：{context.chapter_plan_title}\n"
        f"一句话概要：{context.chapter_plan_one_line}\n"
        "本章目标：\n" + "\n".join(f"  · {goal}" for goal in context.chapter_goals)
    )
    hook = str(getattr(context, "chapter_plan_hook", "") or "").strip()
    purpose = str(getattr(context, "arc_phase_purpose", "") or "").strip()
    if hook:
        section += f"\n本章钩子：{hook}"
    if purpose:
        section += f"\n当前阶段目的：{purpose}"
    return section


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


def _previous_summaries_section(context: ChapterContextPack, *, limit: int) -> str | None:
    if not context.previous_chapter_summaries:
        return None
    return "【前情提要】\n" + "\n".join(
        f"  · {item}" for item in context.previous_chapter_summaries[-limit:]
    )


def _active_entities_section(context: ChapterContextPack, *, limit: int) -> str | None:
    """Proper names from the world view only: its onstage entities, then up to ``limit`` the POV knows of.

    Characters, groups, places and items alike; a concealed onstage entity is not named.
    """
    view = getattr(context, "world_view", None)
    if view is None:
        return None
    lines = [f"  · {entity.name}（在场）" for entity in view.onstage if entity.name and not entity.concealed]
    lines.extend(
        f"  · {entity.name}" + (f"（别名：{'、'.join(entity.aliases)}）" if entity.aliases else "")
        for entity in view.known_entities[:limit]
    )
    if not lines:
        return None
    return "【当前允许使用的专名】\n" + "\n".join(lines)


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
    view = getattr(context, "world_view", None)
    onstage_names = (
        [entity.name for entity in view.onstage if entity.name and not entity.concealed] if view is not None else []
    )
    if not any(
        (
            getattr(context, "active_subworlds", None),
            onstage_names,
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
    if onstage_names:
        lines.append("  · 本章出场名单：" + "、".join(onstage_names))
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
            "  · 规则1：命名人物只能使用本章出场名单与【当前允许使用的专名】里的名字。",
            "  · 规则2：若要引入新命名人物，只能使用上面的 chapter_entry_targets。",
            "  · 规则3：可以写无名泛称路人，但不要把无名角色写成新的专名人物。",
            "  · 规则4：不得把前情中已经出现的姓名扩写、替换或另造别名，必须逐字沿用上下文中的写法。",
        ]
    )
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


def _canon_name_anchor_section(
    context: ChapterContextPack,
    *,
    anchors: Sequence[CanonNameAnchor] = (),
) -> str | None:
    lines = canon_name_anchor_lines(anchors)
    if not lines:
        return None
    return (
        "【Canon 命名锚点】\n"
        + "\n".join(f"  · {line}；必须逐字沿用，不得扩写、替换或另造别名。" for line in lines)
    )


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


__all__ = [
    '_apply_skill_layers',
    '_normalize_char_targets',
    '_story_basics_section',
    '_extract_protagonist_name',
    '_chapter_plan_section',
    '_experience_overlay_section',
    '_previous_summaries_section',
    '_active_entities_section',
    '_personality_context_section',
    '_subworld_control_section',
    '_active_threads_section',
    '_canon_name_anchor_section',
    '_audience_hints_section',
    '_retrieved_memories_section',
]
