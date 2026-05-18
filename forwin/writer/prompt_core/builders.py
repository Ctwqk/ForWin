"""Prompt builders for the ForWin chapter generation system.

All user-facing text in the prompts is written in Chinese so that the LLM
produces fluent Chinese web-novel prose and metadata without code-switching.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from forwin.canon_names import canon_name_anchor_lines, extract_canon_name_anchors
from forwin.canon_quality.rule_profile import (
    countdown_profiles_from_quality_context,
    display_countdown_label,
)
from forwin.protocol.context import ChapterContextPack
from forwin.protocol.scene import SceneOutput, ScenePlan
from .constraints import _canon_quality_context_section
from .sections import (
    _active_entities_section,
    _active_threads_section,
    _apply_skill_layers,
    _arc_envelope_section,
    _audience_hints_section,
    _canon_name_anchor_section,
    _chapter_plan_section,
    _experience_overlay_section,
    _map_runtime_section,
    _normalize_char_targets,
    _npc_intents_section,
    _personality_context_section,
    _previous_summaries_section,
    _retrieved_memories_section,
    _story_basics_section,
    _subworld_control_section,
    _timeline_section,
    _world_model_section,
    _world_model_v4_section,
    _world_pressure_section,
)


def _chapter_hook_requirement(context: ChapterContextPack) -> str:
    quality = getattr(context, "canon_quality_context", {}) or {}
    target_total = int(getattr(context, "project_target_total_chapters", 0) or 0)
    is_final = bool(
        isinstance(quality, dict)
        and quality.get("is_final_chapter")
        or (target_total and int(getattr(context, "chapter_number", 0) or 0) >= target_total)
    )
    if is_final:
        return (
            "本章是终章，结尾必须呈现主线危机已被关闭、公开、阻止或完成代价结算；"
            "如果写到关闭方法、关键道具、坐标或入口，必须在本章完成使用、关闭或公开；"
            "不要把最后一段记录、剩余证据、最后一份材料或去某地交付真相写成结尾后的任务；"
            "不要把“被困在最终设施内”当作终章结局；牺牲必须写成已完成的终局代价，"
            "必须给出被救出、死亡/牺牲确认、或后日谈确认主线已结清；"
            "不要留下追兵、被困、关键道具损坏、准备公开、正要关闭等主线未完成钩子。"
        )
    return "本章结尾必须留下明确钩子。"


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
        _personality_context_section(context),
        _subworld_control_section(context),
        _map_runtime_section(context),
        _active_threads_section(context, limit=thread_limit),
        _canon_name_anchor_section(context),
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
        _canon_quality_context_section(context),
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
        _canon_quality_context_section(context),
        _chapter_plan_section(context, "本章计划"),
        _previous_summaries_section(context, limit=3),
        _active_entities_section(context, limit=6),
        _personality_context_section(context),
        _subworld_control_section(context),
        _map_runtime_section(context),
        _active_threads_section(context, limit=8),
        _canon_name_anchor_section(context),
        _experience_overlay_section(context),
        _world_model_v4_section(context),
        _arc_envelope_section(context, compact=False),
        _npc_intents_section(context, limit=4, detailed=True),
        _world_pressure_section(context),
        _audience_hints_section(context),
        _retrieved_memories_section(context, limit=3, excerpt_chars=80),
        _timeline_section(context),
        _canon_quality_context_section(context),
    )

    user_content = (
        user_sections
        + "\n\n【输出要求】\n"
        f"1. 请写出一章可直接落稿的完整正文，目标正文长度 {target_chars} 到 {max_chars} 中文字，"
        f"不得低于 {min_chars} 中文字。\n"
        "2. 正文必须是自然流畅的网文叙事，不要分点，不要写提纲。\n"
        f"3. {_chapter_hook_requirement(context)}\n"
        "4. 不要输出 JSON，不要输出 markdown，不要解释。\n"
        "5. 只保留一条线性时间线；不要把多个备选版本的同一场景拼接进正文，"
        "同一事件只能发生一次，后文时间数字必须承接前文递减。\n"
        "6. 若【Canon 质量连续性约束】与本章计划、前情摘要或旧设定冲突，必须以 Canon 约束为准；"
        "尤其是倒计时 ledger，不得写回更大的剩余时间。\n"
        "7. 严格使用下面这个纯文本结构输出，并保留标签本身：\n"
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
        _personality_context_section(context),
        _map_runtime_section(context),
        _active_threads_section(context, limit=6),
        _canon_name_anchor_section(context),
        _experience_overlay_section(context),
        _world_model_v4_section(context),
        _arc_envelope_section(context, compact=True),
        _npc_intents_section(context, limit=3, detailed=False),
        _world_pressure_section(context),
        _audience_hints_section(context),
        _retrieved_memories_section(context, limit=2, excerpt_chars=60),
        _timeline_section(context),
        _canon_quality_context_section(context),
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
        f"5. {_chapter_hook_requirement(context)}"
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
        thread_limit=6,
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
        thread_limit=6,
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
        "这里只写本 scene 直接出场的命名人物；不要填写档案、录音、历史记录、组织、地点、物品或未直接到场的提及对象\n"
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
        thread_limit=6,
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
        f"4. {_chapter_hook_requirement(context)}\n"
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


__all__ = [
    '_chapter_hook_requirement',
    '_join_sections',
    '_scene_task_section',
    '_scene_prompt_sections',
    'build_single_chapter_draft_prompt',
    'build_preview_chapter_prompt',
    'build_scene_breakdown_prompt',
    'build_scene_generation_prompt',
    'build_scene_stitch_prompt',
]
