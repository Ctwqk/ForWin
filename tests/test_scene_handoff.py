"""Transport checks for provisional intra-chapter evidence, not model quality."""

from __future__ import annotations

import json

import pytest

from forwin.protocol.book_state import MapEdge, MapNode
from forwin.protocol.context import ChapterContextPack
from forwin.protocol.scene import SceneContinuation, SceneOutput, ScenePlan
from forwin.review.map_movement import MapMovementReviewer
from forwin.writer.chapter_writer import ChapterWriter
from forwin.writer.prompt_core.builders import build_scene_generation_prompt


@pytest.fixture
def context() -> ChapterContextPack:
    return ChapterContextPack(
        project_id="handoff",
        project_title="场间合同",
        premise="三场共同推进一项核查。",
        genre="悬疑",
        setting_summary="旧城",
        chapter_number=1,
        chapter_plan_title="核查",
        chapter_plan_one_line="发现、对照、核验。",
        chapter_goals=["核验凭据"],
        must_not_reveal=["封存证人的真实姓名不可揭露"],
    )


def plans() -> list[ScenePlan]:
    return [
        ScenePlan(
            scene_no=n, objective=f"计划标记{n}：核查不同来源的关联", target_chars=850
        )
        for n in (1, 2, 3)
    ]


def draft(n: int, text: str | None = None) -> SceneOutput:
    return SceneOutput(
        scene_no=n,
        scene_objective=f"动作{n}",
        scene_location_id=f"site-{n}",
        text=text or f"前稿标记{n}：角色声称柜中有铜钥匙，尚未核验。",
        micro_summary=f"摘要{n}",
        continuation=SceneContinuation(
            scene_no=n,
            continuity_anchor=f"衔接标记{n}",
            next_scene_bridge=f"下一步标记{n}",
        ),
    )


class RecordingClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[dict]]] = []
        self.scene_index = 0

    def chat(self, messages, *, stage_key, **_kwargs) -> str:
        self.calls.append((stage_key, messages))
        if stage_key == "scene_breakdown":
            self.scene_index = 0
            return json.dumps(
                {"scenes": [p.model_dump() for p in plans()]}, ensure_ascii=False
            )
        if stage_key == "scene_generation":
            self.scene_index += 1
            n = self.scene_index
            return (
                f"<<FORWIN_BODY>>\n前稿标记{n}：铜钥匙留在保管人手中。\n"
                f"<<FORWIN_SUMMARY>>\n摘要{n}\n"
                f"<<FORWIN_LOCATION>>\nsite-{n}\n"
                f"<<FORWIN_CONTINUITY_ANCHOR>>\n衔接标记{n}\n"
            )
        if stage_key == "scene_stitch":
            return (
                "<<FORWIN_TITLE>>\n核查\n<<FORWIN_BODY>>\n"
                + "保管人仍站在柜前。" * 360
                + "\n<<FORWIN_SUMMARY>>\n完成核验。"
            )
        if stage_key == "state_event_extraction":
            return '{"state_changes":[],"new_events":[],"delivered_payoffs":[]}'
        if stage_key == "thread_time_extraction":
            return '{"thread_beats":[],"time_advance":null}'
        if stage_key == "lore_timeline_notes_extraction":
            return '{"lore_candidates":[],"timeline_hints":[],"writer_notes":[],"entity_mentions":[]}'
        raise AssertionError(f"unexpected model stage: {stage_key}")


def test_writer_hands_off_plans_and_only_completed_scenes(context):
    client = RecordingClient()
    writer = ChapterWriter(client)
    original = context.model_dump(mode="json")
    output = writer.write_chapter(context)
    calls = [
        messages[-1]["content"]
        for stage, messages in client.calls
        if stage == "scene_generation"
    ]
    assert len(calls) == 3
    for index, prompt in enumerate(calls):
        for n in (1, 2, 3):
            assert f"计划标记{n}" in prompt
            assert (f"前稿标记{n}" in prompt) is (n <= index)
            assert (f"衔接标记{n}" in prompt) is (n <= index)
        assert "封存证人的真实姓名不可揭露" in prompt
    assert context.model_dump(mode="json") == original
    assert len(client.calls) == 8
    assert output.generation_meta["call_count"] == 8
    assert [s.scene_location_id for s in output.scene_outputs] == [
        "site-1",
        "site-2",
        "site-3",
    ]
    assert len(output.scene_continuation) == 3


def test_previous_draft_is_reset_for_next_chapter_and_rewrite(context):
    client = RecordingClient()
    writer = ChapterWriter(client)
    writer.write_chapter(context)
    writer.write_chapter(
        context.model_copy(update={"chapter_number": 2}),
        trace_stage_key="chapter_rewrite",
    )
    calls = [
        messages[-1]["content"]
        for stage, messages in client.calls
        if stage == "scene_generation"
    ]
    assert "前稿标记1" in calls[2]
    assert "前稿标记" not in calls[3]
    assert "前稿标记1" in calls[4]
    assert len(client.calls) == 16


def test_prompt_preserves_provisional_text_and_does_not_mutate_inputs(context):
    previous = [draft(8), draft(2)]
    scene_plans = [
        ScenePlan(scene_no=8, objective="先执行"),
        ScenePlan(scene_no=2, objective="后执行"),
    ]
    before = [s.model_dump(mode="json") for s in previous]
    prompt = build_scene_generation_prompt(
        context,
        ScenePlan(scene_no=3, objective="继续"),
        scene_plans=scene_plans,
        previous_scenes=previous,
        handoff_budget_chars=3200,
    )[-1]["content"]
    assert prompt.index("前稿标记8") < prompt.index("前稿标记2")
    assert "角色声称柜中有铜钥匙，尚未核验。" in prompt
    assert "衔接标记8" in prompt and "下一步标记2" in prompt
    assert "尚未发生" in prompt and "Canon" in prompt
    assert "不代表所有角色知情" in prompt
    assert before == [s.model_dump(mode="json") for s in previous]


def test_handoff_budget_keeps_complete_recent_records_and_marks_omissions(context):
    old = draft(1, "不可被截成新事实的旧记录。" * 400)
    recent = draft(2, "最近记录完整保留。")
    prompt = build_scene_generation_prompt(
        context,
        ScenePlan(scene_no=3, objective="继续"),
        scene_plans=plans(),
        previous_scenes=[old, recent],
        handoff_budget_chars=600,
    )[-1]["content"]
    section = prompt.split("【本章分场衔接输入】", 1)[1].split(
        "【本章分场衔接输入结束】", 1
    )[0]
    assert "最近记录完整保留。" in section
    assert "不可被截成新事实" not in section
    assert "省略前稿 1" in section
    assert len("【本章分场衔接输入】" + section + "【本章分场衔接输入结束】") <= 600
    assert old.text == "不可被截成新事实的旧记录。" * 400


def test_oversized_latest_draft_does_not_mislabel_an_older_one_as_the_prefix(context):
    prompt = build_scene_generation_prompt(
        context,
        ScenePlan(scene_no=3, objective="继续"),
        scene_plans=[],
        previous_scenes=[draft(1), draft(2, "过长前场。" * 300)],
        handoff_budget_chars=500,
    )[-1]["content"]
    assert "前稿标记1" not in prompt
    assert "省略前稿 2" in prompt


def test_first_scene_without_handoff_keeps_existing_prompt_shape(context):
    prompt = build_scene_generation_prompt(context, plans()[0])[-1]["content"]
    assert "【本章分场衔接输入】" not in prompt
    assert "scene 编号：1" in prompt


def test_map_blocker_still_receives_all_scene_locations(context):
    nodes = [
        MapNode(id=f"site-{n}", project_id="handoff", node_type="site", name=f"地点{n}")
        for n in (1, 2, 3)
    ]
    edges = [
        MapEdge(
            id=f"road-{n}",
            project_id="handoff",
            from_node_id=f"site-{n}",
            to_node_id=f"site-{n + 1}",
            edge_type="road",
            travel_time=2.0,
        )
        for n in (1, 2)
    ]
    context.map_context = {
        "chapter_travel_time_budget": 0.25,
        "review_graph": {
            "available": True,
            "map_nodes": [n.model_dump(mode="json") for n in nodes],
            "map_edges": [e.model_dump(mode="json") for e in edges],
        },
    }
    output = ChapterWriter(RecordingClient()).write_chapter(context)
    verdict = MapMovementReviewer().review(context, output)
    assert verdict.verdict == "fail"
    assert verdict.issues[0].rule_name == "map_travel_time_exceeds_chapter_time"


def test_failed_scene_prefix_is_not_sent_to_fallback_or_next_attempt(context):
    class OnceFailingClient(RecordingClient):
        failed = False

        def chat(self, messages, *, stage_key, **kwargs):
            if (
                stage_key == "scene_generation"
                and self.scene_index == 1
                and not self.failed
            ):
                self.failed = True
                self.calls.append((stage_key, messages))
                raise ValueError("recorded scene failure")
            if stage_key == "chapter_draft":
                self.calls.append((stage_key, messages))
                return (
                    "<<FORWIN_TITLE>>\n重写\n<<FORWIN_BODY>>\n"
                    + "保管人仍站在柜前。" * 360
                    + "\n<<FORWIN_SUMMARY>>\n核验。"
                )
            return super().chat(messages, stage_key=stage_key, **kwargs)

    client = OnceFailingClient()
    writer = ChapterWriter(client)
    fallback = writer.write_chapter(context, trace_stage_key="chapter_draft")
    assert fallback.generation_meta["fallback_from_scene"] is True
    assert fallback.generation_meta["structured_extraction_calls"] == 3
    single_prompt = next(
        messages[-1]["content"]
        for stage, messages in client.calls
        if stage == "chapter_draft"
    )
    assert "前稿标记" not in single_prompt
    client.calls.clear()
    output = writer.write_chapter(context)
    first = next(
        messages[-1]["content"]
        for stage, messages in client.calls
        if stage == "scene_generation"
    )
    assert "前稿标记" not in first
    assert len(output.scene_outputs) == 3


def test_whole_oversized_plan_is_marked_missing_without_partial_fact(context):
    long_plan = ScenePlan(scene_no=2, objective="完整计划不可剪成肯定事实。" * 100)
    prompt = build_scene_generation_prompt(
        context,
        plans()[0],
        scene_plans=[long_plan],
        handoff_budget_chars=300,
    )[-1]["content"]
    assert "完整计划不可剪" not in prompt
    assert "省略计划 1" in prompt
