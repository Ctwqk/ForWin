from __future__ import annotations

from typing import Any

from forwin.protocol.context import ChapterContextPack, EntitySnapshot, PlotThreadSnapshot, TimelineSnapshot
from forwin.writer.prompts import (
    build_lore_timeline_notes_extraction_prompt,
    build_preview_chapter_prompt,
    build_scene_breakdown_prompt,
    build_state_event_extraction_prompt,
    build_thread_time_extraction_prompt,
)

from .schemas import EvalCase


def sample_context(*, variant: int = 0) -> ChapterContextPack:
    names = [("林夜", "周澜", "雾港"), ("沈烬", "许青鸢", "黑潮城"), ("陆沉", "白芷", "镜湖镇")]
    protagonist, ally, location = names[variant % len(names)]
    return ChapterContextPack(
        project_id=f"eval-project-{variant}",
        project_title=f"潮雾试炼 {variant + 1}",
        premise="主角在沿海旧城发现一枚会记录未来潮声的黑色罗盘。",
        genre="玄幻",
        setting_summary="近海城邦被潮雾、旧术和商会债务共同压迫。",
        chapter_number=variant + 2,
        chapter_plan_title="潮声里的账本",
        chapter_plan_one_line="主角在码头追查旧账时撞见同伴隐瞒的线索。",
        chapter_goals=["推进罗盘线索", "制造同伴信任裂缝", "留下章末悬念"],
        previous_chapter_summaries=["上一章主角得到罗盘，但尚不知道罗盘会记录未来。"],
        active_entities=[
            EntitySnapshot(
                entity_id="char-protagonist",
                kind="character",
                name=protagonist,
                importance=10,
                description="欠下旧术债务的年轻修士。",
                current_state={"location": location, "goal": "查清罗盘来源"},
            ),
            EntitySnapshot(
                entity_id="char-ally",
                kind="character",
                name=ally,
                importance=8,
                description="熟悉商会账本但藏有秘密的同伴。",
                current_state={"location": location, "secret": "认识账本主人"},
            ),
        ],
        active_threads=[
            PlotThreadSnapshot(
                thread_id="thread-compass",
                name="黑色罗盘",
                description="罗盘能记录未来潮声，但代价未知。",
                status="open",
                priority=9,
                recent_beats=["罗盘第一次发热。"],
            )
        ],
        timeline=TimelineSnapshot(current_time_label="第三日黄昏", ordinal=3),
        allowed_entities=[protagonist, ally],
        active_world_lines=["潮雾会遮蔽旧术痕迹。"],
        must_not_reveal=["罗盘真正来源不能在本章揭示。"],
    )


def _json_response_format() -> dict[str, str]:
    return {"type": "json_object"}


def default_eval_cases(*, suite: str = "medium") -> list[EvalCase]:
    context = sample_context()
    body = (
        "潮雾从码头尽头压下来。林夜翻开账本时，黑色罗盘在袖中微微发烫，"
        "周澜却先一步按住纸页，说这不是他们现在该看的东西。"
    )
    cases = [
        EvalCase(
            case_id="writer_preview",
            stage_key="chapter_preview",
            task_family="writer",
            expected_output_kind="tagged_prose",
            schema_name="writer_preview",
            messages=build_preview_chapter_prompt(context, target_chars=900, min_chars=500, max_chars=1200),
            input_snapshot={"chapter_number": context.chapter_number},
            temperature=0.55,
            max_tokens=1600,
        ),
        EvalCase(
            case_id="scene_breakdown",
            stage_key="scene_breakdown",
            task_family="writer",
            expected_output_kind="json",
            schema_name="scene_breakdown",
            messages=build_scene_breakdown_prompt(context, default_scene_count=3, max_scene_count=4),
            input_snapshot={"chapter_number": context.chapter_number},
            response_format=_json_response_format(),
        ),
        EvalCase(
            case_id="state_event_extraction",
            stage_key="state_event_extraction",
            task_family="writer",
            expected_output_kind="json",
            schema_name="state_event_extraction",
            messages=build_state_event_extraction_prompt(context, "潮声里的账本", body),
            input_snapshot={"chapter_number": context.chapter_number},
            response_format=_json_response_format(),
        ),
        EvalCase(
            case_id="thread_time_extraction",
            stage_key="thread_time_extraction",
            task_family="writer",
            expected_output_kind="json",
            schema_name="thread_time_extraction",
            messages=build_thread_time_extraction_prompt(context, "潮声里的账本", body),
            input_snapshot={"chapter_number": context.chapter_number},
            response_format=_json_response_format(),
        ),
        EvalCase(
            case_id="lore_timeline_notes",
            stage_key="lore_timeline_notes",
            task_family="writer",
            expected_output_kind="json",
            schema_name="lore_timeline_notes",
            messages=build_lore_timeline_notes_extraction_prompt(context, "潮声里的账本", body),
            input_snapshot={"chapter_number": context.chapter_number},
            response_format=_json_response_format(),
        ),
        EvalCase(
            case_id="genesis_brief",
            stage_key="genesis_brief",
            task_family="genesis",
            expected_output_kind="json",
            schema_name="genesis_brief",
            messages=[
                {"role": "system", "content": "你是中文长篇网文的 Genesis 协作编辑，只输出 JSON。"},
                {
                    "role": "user",
                    "content": (
                        "请根据 premise 生成 BookBrief JSON，至少包含 title、one_line、audience。"
                        " premise=主角在潮雾旧城得到一枚会记录未来声音的罗盘。"
                    ),
                },
            ],
            response_format=_json_response_format(),
        ),
        EvalCase(
            case_id="arc_plan",
            stage_key="launch_arc_1",
            task_family="genesis",
            expected_output_kind="json",
            schema_name="arc_plan",
            messages=[
                {"role": "system", "content": "你是 Arc 细化编辑，只输出 JSON 对象。"},
                {
                    "role": "user",
                    "content": (
                        "请为当前 arc 规划恰好 3 章，只返回 JSON，顶层格式为 "
                        "{\"chapters\":[{\"title\":\"...\",\"one_line\":\"...\",\"goals\":[\"...\"]}]}。"
                        "Arc：潮雾旧城开局，目标是让主角查清罗盘第一层代价。"
                    ),
                },
            ],
            response_format=_json_response_format(),
        ),
        EvalCase(
            case_id="review_json",
            stage_key="chapter_review",
            task_family="reviewer",
            expected_output_kind="json",
            schema_name="review_json",
            messages=[
                {"role": "system", "content": "你是 Web-Novel Experience Reviewer。只输出 JSON。"},
                {
                    "role": "user",
                    "content": (
                        "请审查章节是否违反连续性，只返回 {\"verdict\":\"pass|warn|fail\",\"issues\":[]}。"
                        "章节摘要：林夜发现账本，周澜阻止他继续翻阅。"
                    ),
                },
            ],
            response_format=_json_response_format(),
        ),
        EvalCase(
            case_id="comment_analysis",
            stage_key="comment_analysis",
            task_family="phase4",
            expected_output_kind="json",
            schema_name="comment_analysis",
            messages=[
                {"role": "system", "content": "你是网文评论分析器，只输出 JSON。"},
                {
                    "role": "user",
                    "content": (
                        "提取评论信号，返回 {\"signals\":[]}。评论："
                        "[{\"body\":\"罗盘线很好奇，但别太快揭秘\"}]"
                    ),
                },
            ],
            response_format=_json_response_format(),
        ),
        EvalCase(
            case_id="npc_intents",
            stage_key="npc_intents",
            task_family="phase4",
            expected_output_kind="json",
            schema_name="npc_intents",
            messages=[
                {"role": "system", "content": "你是网文角色调度器，只输出 JSON，不要解释。"},
                {
                    "role": "user",
                    "content": (
                        "请为下一章生成 NPC 意图，只输出 {\"intents\":[]}。"
                        "角色：周澜；当前目标：隐藏账本主人身份。"
                    ),
                },
            ],
            response_format=_json_response_format(),
        ),
        EvalCase(
            case_id="world_pressure",
            stage_key="world_pressure",
            task_family="phase4",
            expected_output_kind="json",
            schema_name="world_pressure",
            messages=[
                {"role": "system", "content": "你是网文世界模拟器，只输出 JSON，不要解释。"},
                {
                    "role": "user",
                    "content": (
                        "请评估当前世界压力，只输出 "
                        "{\"pressure_level\":\"low|medium|high\",\"pressure_summary\":\"...\",\"notable_shifts\":[]}。"
                        "背景：潮雾增强，商会开始封锁码头。"
                    ),
                },
            ],
            response_format=_json_response_format(),
        ),
    ]
    if suite == "smoke":
        return cases[:2]
    return cases
