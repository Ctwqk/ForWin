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
        "4. state_changes 最多 8 条，只保留会影响后续章节连续性的变化。\n"
        "5. new_events 最多 4 条，只保留本章关键事件。\n"
        "6. character 的 field 必须优先使用英文白名单：location、status、goal、power_level、mood、role_state、knowledge_state、possession_state、life_state、custody_state、injury_state、participation_state。\n"
        "7. location 的 field 优先使用 status、controlled_by；faction 的 field 优先使用 status、location、goal、power_level。\n"
        "8. 字段值必须短句，不要展开分析。\n"
        "9. 没有对应内容就返回空数组。\n"
        "10. 只输出 JSON。\n\n"
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
        "5. entity_mentions 只记录正文中出现的命名实体，尤其是命名角色；new_events 引用的命名组织、地点、物件、规则也必须记录，并使用 character/location/faction/item/rule；泛称路人不要记为 is_named=true。\n"
        "6. evidence_refs 使用 body:短语 格式指向正文证据。\n"
        "7. lore_candidates 最多 6 条，timeline_hints 最多 3 条，writer_notes 最多 5 条，entity_mentions 最多 8 条。\n"
        "8. 每个 description、note 和 evidence_refs 项都必须短，不要复述整段正文。\n"
        "9. 没有对应内容就返回空数组。\n"
        "10. 只输出 JSON。\n\n"
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


__all__ = [
    'build_state_event_extraction_prompt',
    'build_thread_time_extraction_prompt',
    'build_lore_timeline_notes_extraction_prompt',
    'build_structured_extraction_prompt',
]
