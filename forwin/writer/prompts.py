"""Prompt builders for the ForWin chapter generation system.

All user-facing text in the prompts is written in Chinese so that the LLM
produces fluent Chinese web-novel prose and metadata without code-switching.
"""
from __future__ import annotations

import json

from forwin.protocol.context import ChapterContextPack
from forwin.protocol.scene import SceneOutput, ScenePlan


def build_single_chapter_draft_prompt(
    context: ChapterContextPack,
    *,
    target_chars: int = 1800,
    min_chars: int = 1500,
    max_chars: int = 2200,
) -> list[dict]:
    min_chars = max(300, int(min_chars))
    max_chars = max(min_chars, int(max_chars))
    target_chars = max(min_chars, min(int(target_chars), max_chars))
    retrieved_memories = getattr(context, "retrieved_memories", [])
    sections = [
        "【故事基本信息】\n"
        f"类型：{context.genre}\n"
        f"前提：{context.premise}\n"
        f"世界背景：{context.setting_summary}",
        "【本章计划】\n"
        f"章节编号：第 {context.chapter_number} 章\n"
        f"章节标题：{context.chapter_plan_title}\n"
        f"一句话概要：{context.chapter_plan_one_line}\n"
        "本章目标：\n" + "\n".join(f"  · {goal}" for goal in context.chapter_goals),
    ]
    if context.previous_chapter_summaries:
        sections.append(
            "【前情提要】\n" + "\n".join(
                f"  · {item}" for item in context.previous_chapter_summaries[-3:]
            )
        )
    if context.active_entities:
        sections.append(
            "【当前主要角色】\n" + "\n".join(
                f"  · {item.name}：{item.description}"
                for item in context.active_entities[:6]
            )
        )
    if context.active_threads:
        sections.append(
            "【当前剧情线】\n" + "\n".join(
                f"  · {item.name}：{item.description}"
                for item in context.active_threads[:3]
            )
        )
    if getattr(context, "current_arc_envelope", None):
        envelope = context.current_arc_envelope
        sections.append(
            "【当前 Arc Envelope】\n"
            f"  · policy tier：{envelope.source_policy_tier}\n"
            f"  · resolved target：{envelope.resolved_target_size} 章\n"
            f"  · soft range：{envelope.resolved_soft_min} ~ {envelope.resolved_soft_max} 章\n"
            f"  · detailed band：{envelope.detailed_band_size} 章\n"
            f"  · frozen zone：{envelope.frozen_zone_size} 章"
        )
    if getattr(context, "npc_intents", None):
        sections.append(
            "【NPC 当前意图】\n"
            + "\n".join(
                f"  · {item.entity_name}（{item.intent_kind}，紧急度{item.urgency}）：{item.objective}"
                + (f"；策略：{item.tactic}" if item.tactic else "")
                for item in context.npc_intents[:4]
            )
        )
    if getattr(context, "world_pressure", None):
        pressure = context.world_pressure
        sections.append(
            "【世界压力】\n"
            f"  · 等级：{pressure.pressure_level}\n"
            f"  · 概览：{pressure.pressure_summary}"
        )
    if getattr(context, "reader_feedback", None):
        feedback = context.reader_feedback
        sections.append(
            "【读者反馈】\n"
            + "\n".join(
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
        )
    if retrieved_memories:
        sections.append(
            "【检索到的关键记忆】\n" + "\n".join(
                f"  · 第{item.chapter_number}章《{item.title}》：{item.summary or item.excerpt[:80]}"
                for item in retrieved_memories[:3]
            )
        )
    if context.timeline:
        sections.append(
            "【当前时间】\n"
            f"  · {context.timeline.current_time_label}"
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
        "\n\n".join(sections)
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


def build_scene_breakdown_prompt(
    context: ChapterContextPack,
    default_scene_count: int = 3,
    max_scene_count: int = 4,
) -> list[dict]:
    scene_target = min(max(default_scene_count, 2), max_scene_count)
    retrieved_memories = getattr(context, "retrieved_memories", [])
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
    user_content = (
        f"你正在为《{context.project_title}》第 {context.chapter_number} 章拆分场景。\n"
        f"本章标题：{context.chapter_plan_title}\n"
        f"一句话概要：{context.chapter_plan_one_line}\n"
        "本章目标：\n" + "\n".join(f"- {goal}" for goal in context.chapter_goals) + "\n\n"
    )
    if retrieved_memories:
        user_content += (
            "可参考的历史记忆：\n"
            + "\n".join(
                f"- 第{item.chapter_number}章《{item.title}》：{item.summary or item.excerpt[:80]}"
                for item in retrieved_memories[:3]
            )
            + "\n\n"
        )
    if getattr(context, "npc_intents", None):
        user_content += (
            "NPC 当前意图：\n"
            + "\n".join(
                f"- {item.entity_name}：{item.objective}"
                for item in context.npc_intents[:4]
            )
            + "\n\n"
        )
    if getattr(context, "current_arc_envelope", None):
        envelope = context.current_arc_envelope
        user_content += (
            "当前 Arc Envelope：\n"
            f"- policy tier：{envelope.source_policy_tier}\n"
            f"- resolved target：{envelope.resolved_target_size} 章\n"
            f"- soft range：{envelope.resolved_soft_min} ~ {envelope.resolved_soft_max} 章\n"
            f"- detailed band：{envelope.detailed_band_size} 章\n"
            f"- frozen zone：{envelope.frozen_zone_size} 章\n\n"
        )
    if getattr(context, "world_pressure", None):
        user_content += (
            "世界压力概览：\n"
            f"- {context.world_pressure.pressure_level}：{context.world_pressure.pressure_summary}\n\n"
        )
    if getattr(context, "reader_feedback", None):
        user_content += (
            "最近读者反馈：\n"
            f"- {context.reader_feedback.feedback_summary}\n"
            + "\n".join(
                f"- {item.author_name or '读者'}：{item.body_text}"
                for item in context.reader_feedback.recent_highlights[:2]
            )
            + "\n\n"
        )
    user_content += (
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
    retrieved_memories = getattr(context, "retrieved_memories", [])
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
    user_content = (
        f"为《{context.project_title}》第 {context.chapter_number} 章写第 {scene_plan.scene_no} 个 scene。\n"
        f"scene 目标：{scene_plan.objective}\n"
        "必须推进点：\n" + "\n".join(f"- {point}" for point in scene_plan.must_progress_points) + "\n"
        f"时间：{scene_plan.time_hint or '承接上文'}\n"
        f"地点：{scene_plan.location_hint or '沿用当前场景'}\n"
        f"参与角色：{'、'.join(scene_plan.involved_entities) if scene_plan.involved_entities else '按上下文决定'}\n"
        f"结尾 micro-hook：{scene_plan.micro_hook}\n"
        f"目标字数：约 {scene_plan.target_chars} 字。\n"
    )
    if retrieved_memories:
        user_content += (
            "相关历史记忆：\n"
            + "\n".join(
                f"- 第{item.chapter_number}章《{item.title}》：{item.summary or item.excerpt[:80]}"
                for item in retrieved_memories[:2]
            )
            + "\n"
        )
    if getattr(context, "npc_intents", None):
        user_content += (
            "当前 NPC 意图：\n"
            + "\n".join(
                f"- {item.entity_name}：{item.objective}"
                for item in context.npc_intents[:3]
            )
            + "\n"
        )
    if getattr(context, "world_pressure", None):
        user_content += (
            f"世界压力：{context.world_pressure.pressure_level} / "
            f"{context.world_pressure.pressure_summary}\n"
        )
    if getattr(context, "reader_feedback", None):
        user_content += (
            f"读者反馈摘要：{context.reader_feedback.feedback_summary}\n"
        )
    user_content += f"只输出 JSON，不要解释。\n\n{schema}"
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
    user_content = (
        f"请把以下 scenes 拼接成《{context.project_title}》第 {context.chapter_number} 章的完整章节。\n"
        "要求：\n"
        "1. 保持人称、文风、时间地点衔接一致。\n"
        "2. 只做轻量衔接和润色，不要改写核心事件。\n"
        "3. 保留章末钩子。\n"
        "4. 只输出 JSON。\n\n"
        f"{stitched_input}\n\n{schema}"
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
