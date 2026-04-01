"""Prompt builders for the ForWin chapter generation system.

All user-facing text in the prompts is written in Chinese so that the LLM
produces fluent Chinese web-novel prose and metadata without code-switching.
"""
from __future__ import annotations

import json

from forwin.protocol.context import ChapterContextPack
from forwin.protocol.scene import SceneOutput, ScenePlan


# ---------------------------------------------------------------------------
# Arc planning prompt
# ---------------------------------------------------------------------------

def build_arc_planning_prompt(
    premise: str,
    genre: str,
    num_chapters: int = 3,
) -> list[dict]:
    """Build messages for the arc planning call.

    Returns a two-element list: [system_message, user_message].
    The LLM is asked to return a single JSON object – no markdown, no prose.
    """

    system_content = (
        "你是一位经验丰富的中文网文作者，擅长构建引人入胜的剧情、塑造鲜活立体的人物，"
        "以及设计节奏紧凑、张力十足的故事弧线。\n"
        "你的任务是为一部中文网络小说规划故事弧线。\n"
        "请严格按照编辑的要求输出，不要添加任何额外的解释或说明。"
    )

    # Build a concrete JSON schema example to guide the model.
    schema_example = {
        "arc_synopsis": "（2-3段，描述整体弧线的核心冲突、转折与高潮）",
        "setting_summary": "（世界观与背景的简要描述）",
        "chapters": [
            {
                "chapter_number": 1,
                "title": "章节标题",
                "one_line": "一句话概括本章内容",
                "goals": [
                    "目标一：具体说明本章需完成的情节任务",
                    "目标二：角色或关系的推进",
                ],
            }
        ],
        "characters": [
            {
                "name": "角色姓名",
                "kind": "character",
                "description": "2-3句话描述外貌、性格与背景",
                "aliases": ["别名或称号"],
                "importance": 9,
                "initial_state": {
                    "location": "当前所在地点",
                    "status": "当前状态（健康/受伤/潜伏等）",
                    "goal": "当前最迫切的目标",
                    "power_level": "实力描述",
                    "mood": "心理状态",
                },
            }
        ],
        "locations": [
            {
                "name": "地点名称",
                "kind": "location",
                "description": "地点描述",
                "aliases": [],
                "importance": 7,
                "initial_state": {
                    "status": "当前状态",
                    "controlled_by": "控制势力或人物",
                },
            }
        ],
        "factions": [
            {
                "name": "势力名称",
                "kind": "faction",
                "description": "势力描述",
                "aliases": ["简称"],
                "importance": 6,
                "initial_state": {
                    "status": "当前状态",
                    "location": "主要据点",
                    "goal": "势力目标",
                    "power_level": "整体实力",
                },
            }
        ],
        "relations": [
            {
                "source_name": "实体A的名称",
                "target_name": "实体B的名称",
                "relation_type": "关系类型（如：师徒、敌对、盟友、暗恋等）",
                "description": "关系的具体描述",
            }
        ],
        "plot_threads": [
            {
                "name": "线索名称",
                "description": "该剧情线的核心悬念或冲突",
                "priority": 1,
            }
        ],
        "initial_time": {
            "label": "故事开始的时间标签（如：天元历1023年春）",
            "description": "对这一时间点的简要背景说明",
        },
    }

    schema_str = json.dumps(schema_example, ensure_ascii=False, indent=2)

    user_content = (
        f"请为以下设定的中文网络小说规划一个完整的故事弧线，共 {num_chapters} 章。\n\n"
        f"【小说类型】\n{genre}\n\n"
        f"【故事前提】\n{premise}\n\n"
        "【输出要求】\n"
        "1. 只输出一个合法的 JSON 对象，不得有任何 markdown 代码块标记、注释或解释文字。\n"
        "2. 所有文字内容均使用中文。\n"
        "3. 人物性格各有特色，避免脸谱化；每个主要角色都有清晰的动机和成长轨迹。\n"
        "4. 从第一章起就要设置悬念和冲突的钩子，让读者迫不及待地想读下去。\n"
        "5. 故事节奏要符合该类型网文的惯例——有张弛，有爽点，有反转。\n"
        f"6. chapters 数组必须恰好包含 {num_chapters} 个元素，每章设置 2-3 个具体可操作的目标。\n"
        "7. plot_threads 的 priority 字段取值为整数 1（最高）到 3（次要）。\n"
        "8. initial_state 请优先使用固定字段：character 使用 "
        "location/status/goal/power_level/mood，location 使用 "
        "status/controlled_by，faction 使用 status/location/goal/power_level。"
        "如确有必要可补充少量额外字段，但不要随意发明大量新键。\n\n"
        "【JSON 结构参考】\n"
        f"{schema_str}\n\n"
        "现在请输出完整的 JSON 对象："
    )

    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content},
    ]


# ---------------------------------------------------------------------------
# Chapter writing prompt
# ---------------------------------------------------------------------------

def build_chapter_writing_prompt(context: ChapterContextPack) -> list[dict]:
    """Build messages for a single chapter-writing call.

    Returns a two-element list: [system_message, user_message].
    The LLM is asked to return a single JSON object containing the chapter
    body (Chinese prose) together with structured metadata.
    """

    system_content = (
        f"你正在撰写中文网络小说《{context.project_title}》的第 {context.chapter_number} 章。\n"
        "你既是一名文笔出色的网文作者，也是一名严谨的故事编辑：\n"
        "  · 作者职责：用生动、流畅的中文写出引人入胜的正文，节奏紧凑，情感真实，"
        "语言风格符合该类型网文的读者期待。\n"
        "  · 编辑职责：精确记录本章中发生的状态变化、事件和剧情线推进，"
        "为后续章节的连贯性提供可靠的元数据。\n"
        "任何情况下，都只输出要求格式的 JSON，不添加任何额外文字。"
    )

    # ---- Build context sections ----
    sections: list[str] = []

    # 1. Basic story info
    sections.append(
        "【故事基本信息】\n"
        f"类型：{context.genre}\n"
        f"前提：{context.premise}\n"
        f"世界背景：{context.setting_summary}"
    )

    # 2. Current chapter plan
    sections.append(
        f"【本章计划】\n"
        f"章节编号：第 {context.chapter_number} 章\n"
        f"章节标题：{context.chapter_plan_title}\n"
        f"一句话概要：{context.chapter_plan_one_line}\n"
        "本章目标：\n" + "\n".join(f"  · {g}" for g in context.chapter_goals)
    )

    # 3. Previous chapter summaries
    if context.previous_chapter_summaries:
        summary_lines = "\n".join(
            f"  [{i + 1}] {s}"
            for i, s in enumerate(context.previous_chapter_summaries)
        )
        sections.append(f"【前情提要（最近 {len(context.previous_chapter_summaries)} 章）】\n{summary_lines}")
    else:
        sections.append("【前情提要】\n  这是第一章，尚无前情。")

    # 4. Active characters / entities
    if context.active_entities:
        entity_blocks: list[str] = []
        for ent in context.active_entities:
            alias_str = "、".join(ent.aliases) if ent.aliases else "无"
            state_lines = "\n".join(
                f"      {k}：{v}" for k, v in ent.current_state.items()
            )
            entity_blocks.append(
                f"  ▸ [{ent.kind}] {ent.name}（别名/称号：{alias_str}）\n"
                f"    描述：{ent.description}\n"
                f"    当前状态：\n{state_lines}"
            )
        sections.append("【活跃实体（角色/地点/势力）】\n" + "\n".join(entity_blocks))

    # 5. Active relationships
    if context.active_relations:
        rel_lines = "\n".join(
            f"  · {r.source_name} ↔ {r.target_name}（{r.relation_type}）：{r.description}"
            for r in context.active_relations
        )
        sections.append(f"【当前关系网络】\n{rel_lines}")

    # 6. Active plot threads
    if context.active_threads:
        thread_blocks: list[str] = []
        for t in context.active_threads:
            beats_str = (
                "\n".join(f"      · {b}" for b in t.recent_beats)
                if t.recent_beats
                else "      （暂无记录）"
            )
            thread_blocks.append(
                f"  ▸ 【{t.name}】优先级 {t.priority}，状态：{t.status}\n"
                f"    描述：{t.description}\n"
                f"    近期进展：\n{beats_str}"
            )
        sections.append("【剧情线索】\n" + "\n".join(thread_blocks))

    # 7. Timeline
    if context.timeline:
        sections.append(
            f"【当前时间】\n"
            f"  时间标签：{context.timeline.current_time_label}\n"
            f"  时序序号：{context.timeline.ordinal}"
        )

    context_block = "\n\n".join(sections)

    # ---- JSON output schema ----
    output_schema = json.dumps(
        {
            "title": "本章标题（简洁有力，符合网文风格）",
            "body": "正文内容（1500-2200字，地道流畅的中文网文叙事）",
            "end_of_chapter_summary": "本章结尾摘要，1-2句话概括核心事件，供后续章节参考",
            "state_changes": [
                {
                    "entity_name": "实体名称",
                    "entity_kind": "character/location/faction",
                    "field": "发生变化的字段名",
                    "old_value": "变化前的值",
                    "new_value": "变化后的值",
                    "reason": "变化原因（简要说明）",
                }
            ],
            "new_events": [
                {
                    "summary": "事件的中文描述",
                    "significance": "major/minor/background",
                    "involved_entity_names": ["实体名称1", "实体名称2"],
                    "roles": ["protagonist", "antagonist"],
                }
            ],
            "thread_beats": [
                {
                    "thread_name": "线索名称（与上方剧情线索中的名称完全一致）",
                    "beat_type": "setup/escalation/twist/climax/resolution",
                    "description": "本章推进了该线索的哪一步",
                }
            ],
            "time_advance": {
                "new_time_label": "章末时间标签（如无时间推进可省略整个字段）",
                "duration_description": "时间推进的描述，如『翌日清晨』或『三日后』",
            },
        },
        ensure_ascii=False,
        indent=2,
    )

    user_content = (
        f"{context_block}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "【写作指示】\n"
        "1. 正文（body）是本次输出的核心产品。请写出节奏紧凑、细节生动的网文正文，"
        "目标字数 1500-2200 汉字。\n"
        "2. 语言风格：符合上方类型的网文读者期待——该紧张的地方制造张力，"
        "该爽快的地方痛快出手，情感描写要有代入感。\n"
        "3. 章节结尾必须设置钩子（悬念、反转、冲击性信息或令人窒息的对峙），"
        "让读者欲罢不能、迫切想读下一章。\n"
        "4. 必须完成【本章计划】中列出的所有章节目标，不得遗漏。\n"
        "5. state_changes：只记录正文中实际发生的变化，不要凭空杜撰。"
        "字段名称请优先使用固定 schema：character 使用 "
        "location/status/goal/power_level/mood，location 使用 "
        "status/controlled_by，faction 使用 status/location/goal/power_level。"
        "如果某实体已有额外字段，也只能沿用已有字段名。\n"
        "6. new_events：significance 字段只能是 major、minor 或 background 之一。\n"
        "7. thread_beats：thread_name 必须与【剧情线索】中的名称完全一致。"
        "beat_type 只能是 setup、escalation、twist、climax 或 resolution 之一。\n"
        "8. time_advance：若本章时间未推进，请将整个 time_advance 字段设为 null 或省略。\n"
        "9. 只输出合法 JSON，不要有任何 markdown 代码块标记或解释文字。\n\n"
        "【输出 JSON 格式参考】\n"
        f"{output_schema}\n\n"
        "请现在开始输出本章的完整 JSON："
    )

    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content},
    ]


def build_single_chapter_draft_prompt(
    context: ChapterContextPack,
    *,
    target_chars: int = 1800,
) -> list[dict]:
    target_chars = max(1500, min(target_chars, 2200))
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
        f"1. 只输出一个合法 JSON 对象，目标正文长度 {target_chars} 到 2200 中文字。\n"
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
    user_content = (
        f"为《{context.project_title}》第 {context.chapter_number} 章写第 {scene_plan.scene_no} 个 scene。\n"
        f"scene 目标：{scene_plan.objective}\n"
        "必须推进点：\n" + "\n".join(f"- {point}" for point in scene_plan.must_progress_points) + "\n"
        f"时间：{scene_plan.time_hint or '承接上文'}\n"
        f"地点：{scene_plan.location_hint or '沿用当前场景'}\n"
        f"参与角色：{'、'.join(scene_plan.involved_entities) if scene_plan.involved_entities else '按上下文决定'}\n"
        f"结尾 micro-hook：{scene_plan.micro_hook}\n"
        f"目标字数：约 {scene_plan.target_chars} 字。\n"
        "只输出 JSON，不要解释。\n\n"
        f"{schema}"
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
