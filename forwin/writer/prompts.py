"""Prompt builders for the ForWin chapter generation system.

All user-facing text in the prompts is written in Chinese so that the LLM
produces fluent Chinese web-novel prose and metadata without code-switching.
"""
from __future__ import annotations

import json

from forwin.protocol.context import ChapterContextPack
from forwin.protocol.scene import SceneOutput, ScenePlan


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
    return (
        "【故事基本信息】\n"
        f"类型：{context.genre}\n"
        f"前提：{context.premise}\n"
        f"世界背景：{context.setting_summary}"
    )


def _chapter_plan_section(context: ChapterContextPack, title: str) -> str:
    return (
        f"【{title}】\n"
        f"章节编号：第 {context.chapter_number} 章\n"
        f"章节标题：{context.chapter_plan_title}\n"
        f"一句话概要：{context.chapter_plan_one_line}\n"
        "本章目标：\n" + "\n".join(f"  · {goal}" for goal in context.chapter_goals)
    )


def _previous_summaries_section(context: ChapterContextPack, *, limit: int) -> str | None:
    if not context.previous_chapter_summaries:
        return None
    return "【前情提要】\n" + "\n".join(
        f"  · {item}" for item in context.previous_chapter_summaries[-limit:]
    )


def _active_entities_section(context: ChapterContextPack, *, limit: int) -> str | None:
    if not context.active_entities:
        return None
    return "【当前主要角色】\n" + "\n".join(
        f"  · {item.name}：{item.description}"
        for item in context.active_entities[:limit]
    )


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


def _reader_feedback_section(context: ChapterContextPack, *, detailed: bool) -> str | None:
    feedback = getattr(context, "reader_feedback", None)
    if not feedback:
        return None
    if detailed:
        return "【读者反馈】\n" + "\n".join(
            [
                f"  · 评论数：{feedback.comment_count}",
                f"  · 主情绪：{feedback.dominant_sentiment}",
                f"  · 摘要：{feedback.feedback_summary}",
                *[
                    f"  · {item.author_name or '读者'}@{item.platform_id}：{item.body_text}"
                    for item in feedback.recent_highlights[:3]
                ],
            ]
        )
    return "【读者反馈】\n" f"  · {feedback.feedback_summary}"


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
        _active_threads_section(context, limit=thread_limit),
        _arc_envelope_section(context, compact=envelope_compact),
        _npc_intents_section(
            context,
            limit=npc_limit,
            detailed=feedback_detailed,
        ),
        _world_pressure_section(context),
        _reader_feedback_section(context, detailed=feedback_detailed),
        _retrieved_memories_section(context, limit=memory_limit, excerpt_chars=80),
        _timeline_section(context),
    ]
    if extra_sections:
        sections.extend(extra_sections)
    return _join_sections(*sections)


def build_single_chapter_draft_prompt(
    context: ChapterContextPack,
    *,
    target_chars: int = 1800,
    min_chars: int = 1500,
    max_chars: int = 2200,
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
        _active_threads_section(context, limit=3),
        _arc_envelope_section(context, compact=False),
        _npc_intents_section(context, limit=4, detailed=True),
        _world_pressure_section(context),
        _reader_feedback_section(context, detailed=True),
        _retrieved_memories_section(context, limit=3, excerpt_chars=80),
        _timeline_section(context),
    )

    schema_example = json.dumps(
        {
            "title": "章节标题",
            "body": "完整章节正文",
            "end_of_chapter_summary": "本章结尾总结",
        },
        ensure_ascii=False,
        indent=2,
    )
    user_content = (
        user_sections
        + "\n\n【输出要求】\n"
        f"1. 只输出一个合法 JSON 对象，目标正文长度 {target_chars} 到 {max_chars} 中文字，"
        f"不得低于 {min_chars} 中文字。\n"
        "2. 正文必须是自然流畅的网文叙事，不要分点，不要写提纲。\n"
        "3. 本章结尾必须留下明确钩子。\n"
        "4. JSON 只能包含 title、body、end_of_chapter_summary 三个字段。\n"
        "5. 不要输出 markdown，不要输出解释。\n\n"
        f"【JSON 结构示例】\n{schema_example}"
    )
    return [
        {"role": "system", "content": "你是中文网文作者，只输出 JSON 对象，不要解释。"},
        {"role": "user", "content": user_content},
    ]


def build_preview_chapter_prompt(
    context: ChapterContextPack,
    *,
    target_chars: int = 900,
    min_chars: int = 600,
    max_chars: int = 1200,
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
        _active_threads_section(context, limit=3),
        _arc_envelope_section(context, compact=True),
        _npc_intents_section(context, limit=3, detailed=False),
        _world_pressure_section(context),
        _reader_feedback_section(context, detailed=False),
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
    return [
        {"role": "system", "content": "你是中文网文作者，只输出指定标签格式的纯文本，不要解释。"},
        {"role": "user", "content": user_content},
    ]


def build_scene_breakdown_prompt(
    context: ChapterContextPack,
    default_scene_count: int = 3,
    max_scene_count: int = 4,
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
        "3. 只输出 JSON，不要解释。\n\n"
        "JSON 结构参考：\n"
        f"{schema}"
    )
    return [
        {"role": "system", "content": "你是网文场景导演，只负责拆 scene，不写正文。"},
        {"role": "user", "content": user_content},
    ]


def build_scene_generation_prompt(
    context: ChapterContextPack,
    scene_plan: ScenePlan,
) -> list[dict]:
    schema = json.dumps(
        {
            "text": "本 scene 正文",
            "micro_summary": "本 scene 的一句话小结",
            "scene_time_point": "scene 时间点",
            "scene_location_id": "scene 地点名或 id",
            "involved_entities": ["角色A", "角色B"],
        },
        ensure_ascii=False,
        indent=2,
    )
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
        "请显式参考当前 NPC 意图、世界压力和读者反馈来组织这个 scene。\n\n"
        f"只输出 JSON，不要解释。\n\n{schema}"
    )
    return [
        {"role": "system", "content": "你是中文网文写手，只负责写单个 scene。"},
        {"role": "user", "content": user_content},
    ]


def build_scene_stitch_prompt(
    context: ChapterContextPack,
    scene_outputs: list[SceneOutput],
) -> list[dict]:
    stitched_input = "\n\n".join(
        f"[Scene {scene.scene_no}]\n目标：{scene.scene_objective}\n摘要：{scene.micro_summary}\n正文：\n{scene.text}"
        for scene in scene_outputs
    )
    schema = json.dumps(
        {
            "title": "章节标题",
            "body": "拼接后的完整章节正文",
            "end_of_chapter_summary": "1-2句章节总结",
        },
        ensure_ascii=False,
        indent=2,
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
        "2. 只做轻量衔接和润色，不要改写核心事件。\n"
        "3. 保留章末钩子。\n"
        "4. 只输出 JSON。\n\n"
        f"{schema}"
    )
    return [
        {"role": "system", "content": "你是章节拼接编辑，只做 scenes 的轻量 stitch。"},
        {"role": "user", "content": user_content},
    ]


def build_structured_extraction_prompt(
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
        f"请从《{context.project_title}》第 {context.chapter_number} 章《{chapter_title}》中抽取结构化元数据。\n"
        "要求：\n"
        "1. 只抽取正文真实发生的内容。\n"
        "2. state_changes 的字段名遵循固定 schema。\n"
        "3. 若没有时间推进，time_advance 设为 null。\n"
        "4. 只输出 JSON。\n\n"
        f"正文：\n{chapter_body}\n\n{schema}"
    )
    return [
        {"role": "system", "content": "你是结构化提取器，只负责从正文抽取元数据。"},
        {"role": "user", "content": user_content},
    ]
