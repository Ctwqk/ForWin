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
    "phases 把本卷分成若干阶段，purpose 写该阶段要完成的叙事任务。milestones 是本卷必须达成的节点："
    "id 在本表内唯一，target_range 在卷内，required_refs 列出达成它时必须仍然存在的行动者 id；hard 为 true 表示不可放弃。",
    "protections 保护里程碑依赖的世界状态：kind 只能取允许值；ref 是被保护的行动者 id，must_not_know 时 ref 与 observer_ref "
    "都是不得知情的观察者，并给出 fact_ref；must_hold_state 给出 field 与 value；until_chapter 取对应里程碑 target_range 的结束章。",
    "reveal_targets 安排本卷的揭示：fact_ref 只用 facts 的 ref，observer 为允许值中的读者标记或行动者 id，"
    "target_state 只能取允许值，range 在卷内。reveal_candidates 给出事实的建议揭示卷，不要提前揭示后续卷的秘密。",
    "exit_state 列出本卷结束时必须成立的状态：ref 为行动者 id，field 与 value 是它的状态字段与取值，note 说明理由。",
    "protagonist_ref 从 actors 中选一个 kind 为 character 的行动者作为本卷主角。reader_promise 与 arc_payoff_map "
    "写本卷对读者的承诺与回报安排。",
    "引用只能使用 actors 的 id 与 facts 的 ref，不得编造人物、地点或事实。世界摘要、上一卷收尾状态与未了义务是作者视角，"
    "可以含有秘密，只用于规划。给出 revision 时，在 current_beat_sheet 的基础上修订，消除 conflict_summary 所述的冲突。"
    "重试反馈列出上一次输出的问题，必须全部修正；所有空列表返回 []。完整输出结构如下，允许值列表补充其中的枚举。",
)


def build_arc_beats_messages(
    *,
    payload: Mapping[str, Any],
    digest_text: str,
    output_schema: Mapping[str, Any],
    allowed: Mapping[str, Any],
    retry_feedback: Sequence[str] = (),
) -> list[dict]:
    """The ``plan.arc_beats`` prompt. Its payload and digest are the author's view: never give them to the writer."""
    system = ("\n".join(ARC_BEATS_INSTRUCTIONS)
              + "\n" + json.dumps(output_schema, ensure_ascii=False)
              + "\nAllowed values:\n" + json.dumps(allowed, ensure_ascii=False))
    user = json.dumps(payload, ensure_ascii=False) + "\n\nWorld digest (author view):\n" + digest_text
    if retry_feedback:
        user += "\n\nRetry validation feedback:\n" + "\n".join(retry_feedback)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]
