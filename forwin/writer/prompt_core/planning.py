"""Planning prompt templates; world content is supplied by BookState."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any


WORLD_DIGEST_TITLES = {
    "protagonist": "主角现状",
    "actors": "相关行动者",
    "news_in_transit": "在途消息",
    "offscreen_events": "幕后事件",
    "secrets_progress": "秘密揭示进度",
    "tensions": "冲突张力",
    "clocks": "运行时钟",
    "stalled_threads_obligations": "停滞剧情线与叙事义务",
}


ARC_BEATS_INSTRUCTIONS = (
    "你是长篇小说的卷规划者，为当前卷写一份节拍表，返回 JSON 对象，不输出正文或 Markdown。",
    "卷的章节范围固定为 arc.chapter_start 到 arc.chapter_end，不得改动。phases 与 band_breakdown 都按章节顺序"
    "恰好覆盖这个范围，不留空章、不重叠；每个 band 的章数在 band_limits 之内，以 band_size 为准，"
    "卷长不足下限时只给一个 band；band_index 从 0 起按顺序编号。",
    "phases 把本卷分成若干阶段，purpose 写该阶段要完成的叙事任务；purpose 与 milestones 的 description 都会原样交给写作者，不得写入秘密的真相或隐藏实体的名字。milestones 是本卷必须达成的节点："
    "id 在本表内唯一，target_range 在卷内，required_refs 列出达成它时必须仍然存在的行动者 id；hard 为 true 表示不可放弃。",
    "protections 保护里程碑依赖的世界状态：kind 只能取允许值；ref 是被保护的行动者 id；must_be_alive 只用于 kind 为 character "
    "的行动者；must_not_know 时 ref 与 observer_ref 都是不得知情的观察者（行动者 id，或允许值中的读者标记），并给出 fact_ref；"
    "must_hold_state 给出 field 与 value；until_chapter 取对应里程碑 target_range 的结束章，在卷内。",
    "reveal_targets 安排本卷的揭示：fact_ref 只用 facts 的 ref，observer 为允许值中的读者标记或行动者 id，"
    "target_state 只能取允许值，range 在卷内。reveal_candidates 给出事实的建议揭示卷，不要提前揭示后续卷的秘密。",
    "exit_state 列出本卷结束时必须成立的状态：ref 为行动者 id，field 与 value 是它的状态字段与取值，note 说明理由。"
    "must_hold_state 与 exit_state 的 field 只能取允许值 actor_fields 中该行动者 kind 的字段，可写字段名或加 state. / profile. 前缀。",
    "protagonist_ref 从 actors 中选一个 kind 为 character 且 seeded 为 true 的行动者作为本卷主角。reader_promise 与 arc_payoff_map "
    "写本卷对读者的承诺与回报安排。",
    "引用只能使用 actors 的 id 与 facts 的 ref，不得编造人物、地点或事实。世界摘要、上一卷收尾状态与未了义务是作者视角，"
    "可以含有秘密，只用于规划。给出 revision 时，在 current_beat_sheet 的基础上修订，消除 conflict_summary 所述的冲突。"
    "重试反馈列出上一次输出的问题，必须全部修正；所有空列表返回 []。完整输出结构如下，允许值列表补充其中的枚举。",
)


BAND_PLAN_INSTRUCTIONS = (
    "你是长篇小说的 band 规划者，为本卷当前 band 的指定章节逐章写计划，返回 JSON 对象，不输出正文或 Markdown。",
    "chapters 恰好包含 chapters_to_plan 中的每个章号各一次。kept_chapters 是本 band 中不再改写的章，只用来衔接前后；"
    "current_plans 是待改写章节的现有计划，notes 是这次规划的额外要求。",
    "每章写 title、one_line、goals（至少一条本章必须完成的剧情目标）与 hook（章末钩子）。pov_ref 必须是 actors 中 kind 为 "
    "character 的行动者，并且出现在 onstage_refs 中；onstage_refs 只用 actors 的 id，location_refs 只用 locations 的 id；"
    "expected_span 给出本章预计经过的故事时长，unit 只能取允许值 time_units。",
    "world_bindings 写本章承接的世界元素 {kind, ref, how}：kind 为 intent 时 ref 取 intents 的 id，event 取 events 的 id，"
    "clock 取 clocks 的 id，relation 取 relations 的 id，news 取 news 的 fact_ref，secret 取 facts 的 ref；how 写本章如何呈现它。"
    "due_onstage_intent_ids 中到点且需要在场的意图，应绑定到本 band 的某一章。",
    "reveal_directives 写本章让读者或某个行动者得知的事实：id 在本 band 内唯一，fact_ref 只用 facts 的 ref，observer 为允许值中的"
    "读者标记或 actors 的 id，target_state 只能取允许值；遵守 reveal_targets 的章节范围，不要提前揭示。hint_directives 只做暗示："
    "id 在本 band 内唯一，hint_text 不得写出该事实的 proposition。",
    "intended_changes 列出本章预期造成的状态变化：ref 为 actors 的 id，field 只能取允许值 actor_fields 中该行动者 kind 的字段，"
    "value 为变化后的取值。milestone_ids 列出本章承担的 milestones 的 id；target_range 结束在本 band 内的 hard milestone 必须由某一章承担。",
    "experience_schedule 是确定性的读者体验排期：reward_slot 取该章 rewards 中的一个 category，该章没有排期时留空；trope_ids 只能取"
    "该章 rewards 中的 template_id；curiosity_beats 与 immersion_anchor_scene_goal 用来安排悬念与沉浸场景。",
    "pending_responses 是已决定由剧情回应的世界变化，每一条都必须由某一章承担：把它的 goal 原文写进该章 goals，或在该章 "
    "world_bindings 中绑定它 refs 里的一个 id。open_obligations、stalled_threads 与 previous_band 用来安排义务兑现、停滞剧情线的"
    "推进，以及与上一 band 实际结果的衔接。",
    "引用只能使用输入给出的 id 与 ref，不得编造人物、地点、事实或编号。输入与世界摘要是作者视角，可以含有秘密与隐藏意图，只用于规划。"
    "title、one_line、goals 与 hook 会原样交给写作者：不得写入秘密的真相、只有作者知道的信息、隐藏实体的名字，或读者在该章不应得知的内容；"
    "只有本章 reveal_directives 揭示的事实及其相关实体可以在这些字段中点明。重试反馈列出上一次输出的问题，必须全部修正；所有空列表返回 []。"
    "完整输出结构如下，允许值列表补充其中的枚举。",
)


def build_band_plan_messages(
    *,
    payload: Mapping[str, Any],
    digest_text: str,
    output_schema: Mapping[str, Any],
    allowed: Mapping[str, Any],
    retry_feedback: Sequence[str] = (),
) -> list[dict]:
    """The ``plan.band`` prompt. Its payload and digest are the author's view: never give them to the writer."""
    return _planning_messages(BAND_PLAN_INSTRUCTIONS, payload, digest_text, output_schema, allowed, retry_feedback)


def build_arc_beats_messages(
    *,
    payload: Mapping[str, Any],
    digest_text: str,
    output_schema: Mapping[str, Any],
    allowed: Mapping[str, Any],
    retry_feedback: Sequence[str] = (),
) -> list[dict]:
    """The ``plan.arc_beats`` prompt. Its payload and digest are the author's view: never give them to the writer."""
    return _planning_messages(ARC_BEATS_INSTRUCTIONS, payload, digest_text, output_schema, allowed, retry_feedback)


def _planning_messages(
    instructions: Sequence[str],
    payload: Mapping[str, Any],
    digest_text: str,
    output_schema: Mapping[str, Any],
    allowed: Mapping[str, Any],
    retry_feedback: Sequence[str],
) -> list[dict]:
    system = ("\n".join(instructions)
              + "\n" + json.dumps(output_schema, ensure_ascii=False)
              + "\nAllowed values:\n" + json.dumps(allowed, ensure_ascii=False))
    user = json.dumps(payload, ensure_ascii=False) + "\n\nWorld digest (author view):\n" + digest_text
    if retry_feedback:
        user += "\n\nRetry validation feedback:\n" + "\n".join(retry_feedback)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]
