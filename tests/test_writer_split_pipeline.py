from __future__ import annotations

import unittest

import httpx

from forwin.protocol.context import ChapterContextPack
from forwin.protocol.scene import ScenePlan, SceneOutput
from forwin.writer.chapter_writer import ChapterWriter
from forwin.writer.prompt_core.extraction import build_state_event_extraction_prompt


class SplitWriterPipelineTests(unittest.TestCase):
    def test_state_event_prompt_requires_verbatim_delivered_payoff(self) -> None:
        context = ChapterContextPack(
            project_id="p1",
            project_title="测试书",
            premise="前提",
            genre="悬疑",
            setting_summary="旧城站台",
            chapter_number=3,
            chapter_plan_title="第三章",
            chapter_plan_one_line="主角继续追查",
            chapter_goals=["确认异响来源"],
        )

        prompt = build_state_event_extraction_prompt(
            context,
            "雨夜旧站",
            "林夜确认异常报站。",
        )[-1]["content"]

        self.assertIn('"delivered_payoffs"', prompt)
        self.assertIn("只记录主角在本章已经兑现的直接收益", prompt)
        self.assertIn("同时包含 entity_name 和 after_state", prompt)
        self.assertIn('同一位置的 roles 必须为 "protagonist"', prompt)

    def test_structured_extraction_retries_required_payoff_schema(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.responses = [
                    '{"state_changes":[],"new_events":[]}',
                    (
                        '{"state_changes":[],"new_events":[],"delivered_payoffs":['
                        '{"entity_name":"林夜","category":"mystery","direction":"gain",'
                        '"before_state":"未知","after_state":" ",'
                        '"evidence_quote":"林夜确认了异常报站"}]}'
                    ),
                    '{"state_changes":[],"new_events":[],"delivered_payoffs":[]}',
                    '{"thread_beats":[],"time_advance":null}',
                    (
                        '{"lore_candidates":[],"timeline_hints":[],"writer_notes":[],'
                        '"entity_mentions":[]}'
                    ),
                ]
                self.calls = 0

            def chat(
                self, _messages, temperature: float, max_tokens: int, **_kwargs
            ) -> str:
                self.calls += 1
                return self.responses.pop(0)

        client = FakeClient()
        writer = ChapterWriter(client, writer_mode="single")
        context = ChapterContextPack(
            project_id="p1",
            project_title="测试书",
            premise="前提",
            genre="悬疑",
            setting_summary="旧城站台",
            chapter_number=3,
            chapter_plan_title="第三章",
            chapter_plan_one_line="主角继续追查",
            chapter_goals=["确认异响来源"],
        )

        metadata = writer._extract_structured(context, "雨夜旧站", "林夜确认异常报站。")

        self.assertEqual(metadata["delivered_payoffs"], [])
        self.assertEqual(client.calls, 5)
        self.assertEqual(client.responses, [])

    def test_preview_generation_rejects_removed_json_preview_fallback(self) -> None:
        class FakeClient:
            def chat(
                self, _messages, temperature: float, max_tokens: int, **_kwargs
            ) -> str:
                body = "林夜沿着旧站台继续追查异常报站声" * 40
                return (
                    '{"title":"雨夜旧站","body":"'
                    + body
                    + '","micro_summary":"主角继续追查"}'
                )

        writer = ChapterWriter(
            FakeClient(),
            min_chapter_chars=500,
            target_chapter_chars=500,
            max_chapter_chars=800,
        )

        with self.assertRaises(ValueError):
            writer._chat_preview_text(
                [{"role": "user", "content": "写一段"}],
                temperature=0.2,
                max_tokens=1024,
                max_attempts=1,
                stage_key="scene_generation",
            )
        self.assertFalse(hasattr(ChapterWriter, "_legacy_json_preview_is_accepted"))

    def test_scene_reward_tags_from_llm_are_normalized_before_validation(self) -> None:
        plan = ScenePlan(
            scene_no=1,
            objective="推进调查",
            reward_beat_tag="progress",
        )
        output = SceneOutput(
            scene_no=1,
            scene_objective="推进调查",
            text="林夜继续调查。",
            reward_beat_tag="relationship",
        )

        self.assertEqual(plan.reward_beat_tag, "mystery")
        self.assertEqual(output.reward_beat_tag, "social")

    def test_single_writer_uses_tagged_body_and_defers_structured_extraction(
        self,
    ) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.responses = [
                    (
                        "<<FORWIN_TITLE>>\n"
                        "雨夜旧站\n"
                        "<<FORWIN_BODY>>\n"
                        + "林夜踩着积水穿过旧站台，听见广播里多出了一段不属于这个时代的报站声。"
                        * 20
                        + "\n<<FORWIN_SUMMARY>>\n"
                        "林夜确认旧站台里还藏着第二条线索。"
                    ),
                    (
                        '{"state_changes":[{"entity_name":"林夜","entity_kind":"character",'
                        '"field":"location","old_value":"街口","new_value":"旧站台","reason":"进入调查现场"}],'
                        '"new_events":[{"summary":"林夜进入旧站台调查","significance":"major",'
                        '"involved_entity_names":["林夜"],"roles":["protagonist"]}]}'
                    ),
                    (
                        '{"thread_beats":[{"thread_name":"旧站疑云","beat_type":"escalation",'
                        '"description":"主角确认异常报站并继续深挖"}],'
                        '"time_advance":{"new_time_label":"深夜","duration_description":"片刻后"}}'
                    ),
                    (
                        '{"lore_candidates":[{"subject_name":"旧站台","subject_type":"location",'
                        '"description":"旧站台存在不属于当前时代的报站声","evidence_refs":["body:报站声"],'
                        '"confidence":0.8}],'
                        '"timeline_hints":[{"current_time_label":"深夜","projected_time_label":"深夜继续",'
                        '"duration_hint":"片刻后","evidence_refs":["body:深夜"],"confidence":0.7}],'
                        '"writer_notes":[{"note_type":"continuity","target_name":"旧站疑云",'
                        '"note":"下一章继续承接异常报站声","evidence_refs":["body:报站声"]}]}'
                    ),
                ]

            def chat(
                self, _messages, temperature: float, max_tokens: int, **_kwargs
            ) -> str:
                return self.responses.pop(0)

        writer = ChapterWriter(
            FakeClient(),
            writer_mode="single",
            min_chapter_chars=300,
        )
        context = ChapterContextPack(
            project_id="p1",
            project_title="测试书",
            premise="前提",
            genre="悬疑",
            setting_summary="旧城站台",
            chapter_number=3,
            chapter_plan_title="第三章",
            chapter_plan_one_line="主角继续追查",
            chapter_goals=["确认异响来源", "推进旧站线索"],
        )

        output = writer.write_chapter(context)

        self.assertEqual(output.title, "雨夜旧站")
        self.assertGreater(output.char_count, 200)
        self.assertEqual(output.state_changes, [])
        self.assertEqual(output.thread_beats, [])
        self.assertIsNone(output.time_advance)
        self.assertEqual(output.lore_candidates, [])
        self.assertEqual(output.timeline_hints, [])
        self.assertEqual(output.writer_notes, [])
        self.assertEqual(output.generation_meta["mode"], "single")
        self.assertEqual(output.generation_meta["call_count"], 1)
        self.assertEqual(output.generation_meta["structured_extraction"], "deferred")
        self.assertEqual(output.generation_meta["structured_extraction_calls"], 0)
        self.assertEqual(output.generation_meta["state_event_extraction"], "deferred")
        self.assertEqual(output.generation_meta["thread_time_extraction"], "deferred")
        self.assertEqual(
            output.generation_meta["lore_timeline_notes_extraction"], "deferred"
        )

    def test_scene_fallback_completes_structured_extraction(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.responses = [
                    (
                        "<<FORWIN_TITLE>>\n"
                        "雨夜旧站\n"
                        "<<FORWIN_BODY>>\n"
                        + "林夜踩着积水穿过旧站台，听见广播里多出了一段不属于这个时代的报站声。"
                        * 20
                        + "\n<<FORWIN_SUMMARY>>\n"
                        "林夜确认旧站台里还藏着第二条线索。"
                    ),
                    (
                        '{"state_changes":[{"entity_name":"林夜","entity_kind":"character",'
                        '"field":"location","old_value":"街口","new_value":"旧站台","reason":"进入调查现场"}],'
                        '"new_events":[{"summary":"林夜进入旧站台调查","significance":"major",'
                        '"involved_entity_names":["林夜"],"roles":["protagonist"]}],'
                        '"delivered_payoffs":[{"entity_name":"林夜","category":"mystery",'
                        '"direction":"gain","before_state":"尚未发现异常报站",'
                        '"after_state":"听见广播里多出了一段不属于这个时代的报站声",'
                        '"evidence_quote":"林夜踩着积水穿过旧站台，听见广播里多出了一段不属于这个时代的报站声"}]}'
                    ),
                    (
                        '{"thread_beats":[{"thread_name":"旧站疑云","beat_type":"escalation",'
                        '"description":"主角确认异常报站并继续深挖"}],'
                        '"time_advance":{"new_time_label":"深夜","duration_description":"片刻后"}}'
                    ),
                    (
                        '{"lore_candidates":[{"subject_name":"旧站台","subject_type":"location",'
                        '"description":"旧站台存在不属于当前时代的报站声","evidence_refs":["body:报站声"],'
                        '"confidence":0.8}],'
                        '"timeline_hints":[{"current_time_label":"深夜","projected_time_label":"深夜继续",'
                        '"duration_hint":"片刻后","evidence_refs":["body:深夜"],"confidence":0.7}],'
                        '"writer_notes":[{"note_type":"continuity","target_name":"旧站疑云",'
                        '"note":"下一章继续承接异常报站声","evidence_refs":["body:报站声"]}]}'
                    ),
                ]

            def chat(
                self, _messages, temperature: float, max_tokens: int, **_kwargs
            ) -> str:
                return self.responses.pop(0)

        class FallbackWriter(ChapterWriter):
            def _plan_scenes(self, context, *, skill_layers=None):  # type: ignore[no-untyped-def]
                raise ValueError("scene failed")

        writer = FallbackWriter(
            FakeClient(),
            writer_mode="scene",
            min_chapter_chars=300,
        )
        context = ChapterContextPack(
            project_id="p1",
            project_title="测试书",
            premise="前提",
            genre="悬疑",
            setting_summary="旧城站台",
            chapter_number=3,
            chapter_plan_title="第三章",
            chapter_plan_one_line="主角继续追查",
            chapter_goals=["确认异响来源", "推进旧站线索"],
        )

        output = writer.write_chapter(context)

        self.assertTrue(output.generation_meta["fallback_from_scene"])
        self.assertEqual(
            output.generation_meta["fallback_structured_extraction"], "performed"
        )
        self.assertEqual(output.generation_meta["structured_extraction"], "completed")
        self.assertEqual(output.generation_meta["structured_extraction_calls"], 3)
        self.assertEqual(output.generation_meta["call_count"], 4)
        self.assertEqual(output.state_changes[0].entity_name, "林夜")
        self.assertEqual(output.delivered_payoffs[0].category, "mystery")
        self.assertEqual(output.thread_beats[0].thread_name, "旧站疑云")
        self.assertEqual(output.lore_candidates[0].subject_name, "旧站台")

    def test_single_writer_prompt_trace_records_business_retry_reason(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.responses = [
                    "",
                    (
                        "<<FORWIN_TITLE>>\n"
                        "雨夜旧站\n"
                        "<<FORWIN_BODY>>\n"
                        + "林夜踩着积水穿过旧站台，听见广播里多出了一段不属于这个时代的报站声。"
                        * 20
                        + "\n<<FORWIN_SUMMARY>>\n"
                        "林夜确认旧站台里还藏着第二条线索。"
                    ),
                    '{"state_changes":[],"new_events":[]}',
                    '{"thread_beats":[],"time_advance":null}',
                    '{"lore_candidates":[],"timeline_hints":[],"writer_notes":[]}',
                ]

            def chat(
                self, _messages, temperature: float, max_tokens: int, **_kwargs
            ) -> str:
                return self.responses.pop(0)

        writer = ChapterWriter(
            FakeClient(),
            writer_mode="single",
            min_chapter_chars=300,
        )
        context = ChapterContextPack(
            project_id="p1",
            project_title="测试书",
            premise="前提",
            genre="悬疑",
            setting_summary="旧城站台",
            chapter_number=3,
            chapter_plan_title="第三章",
            chapter_plan_one_line="主角继续追查",
            chapter_goals=["确认异响来源"],
        )

        output = writer.write_chapter(context)

        trace = output.generation_meta["prompt_trace"]
        retry_events = trace["output_summary"]["business_retry_events"]
        self.assertEqual(retry_events[0]["stage"], "preview_generation")
        self.assertEqual(retry_events[0]["attempt_no"], 1)
        self.assertIn("preview response body is empty", retry_events[0]["reason"])

    def test_preview_text_retries_incomplete_body_without_shrinking_budget(
        self,
    ) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.max_tokens: list[int] = []
                self.responses = [
                    "<<FORWIN_TITLE>>\n断章\n<<FORWIN_BODY>>\n林夜抬头看见门后站着",
                    "<<FORWIN_TITLE>>\n完整章\n<<FORWIN_BODY>>\n林夜抬头看见门后站着另一个自己。",
                ]

            def chat(
                self, _messages, temperature: float, max_tokens: int, **_kwargs
            ) -> str:
                self.max_tokens.append(max_tokens)
                return self.responses.pop(0)

        client = FakeClient()
        writer = ChapterWriter(client, writer_mode="single")

        raw = writer._chat_preview_text(
            [{"role": "user", "content": "写一章"}],
            temperature=0.6,
            max_tokens=2400,
            max_attempts=2,
        )

        parsed = writer._parse_preview_text(raw, fallback_title="")
        self.assertEqual(parsed["title"], "完整章")
        self.assertEqual(client.max_tokens, [2400, 2400])
        self.assertIn("appears incomplete", writer._business_retry_events[0]["reason"])

    def test_preview_text_retries_complete_but_underlength_body(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.responses = [
                    "<<FORWIN_BODY>>\n短稿。",
                    "<<FORWIN_BODY>>\n" + "林夜沿着旧站台追查异常报站声。" * 8,
                ]

            def chat(self, _messages, **_kwargs) -> str:
                return self.responses.pop(0)

        writer = ChapterWriter(FakeClient(), writer_mode="single")

        raw = writer._chat_preview_text(
            [{"role": "user", "content": "写一章"}],
            temperature=0.6,
            max_tokens=2400,
            max_attempts=2,
            min_body_chars=40,
        )

        parsed = writer._parse_preview_text(raw, fallback_title="")
        self.assertGreaterEqual(len(parsed["body"]), 40)
        self.assertIn("below minimum 40", writer._business_retry_events[0]["reason"])

    def test_scene_stitch_uses_full_chapter_token_budget(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.max_tokens: list[int] = []

            def chat(
                self, _messages, temperature: float, max_tokens: int, **_kwargs
            ) -> str:
                self.max_tokens.append(max_tokens)
                return (
                    "<<FORWIN_TITLE>>\n旧站来声\n"
                    "<<FORWIN_BODY>>\n"
                    + "林夜走进旧站台，雨声和陌生报站声纠缠在一起。" * 80
                    + "\n<<FORWIN_SUMMARY>>\n林夜确认旧站广播异常。"
                )

        client = FakeClient()
        writer = ChapterWriter(
            client,
            writer_mode="scene",
            max_tokens=10000,
            min_chapter_chars=300,
            target_chapter_chars=2800,
            max_chapter_chars=3200,
        )
        context = ChapterContextPack(
            project_id="p1",
            project_title="测试书",
            premise="前提",
            genre="悬疑",
            setting_summary="旧城站台",
            chapter_number=4,
            chapter_plan_title="第四章",
            chapter_plan_one_line="主角进入旧站",
            chapter_goals=["进入旧站", "确认广播异常"],
        )

        writer._stitch_scenes(
            context,
            [
                SceneOutput(
                    scene_no=1, scene_objective="进入旧站", text="林夜进入旧站。"
                )
            ],
        )

        self.assertGreaterEqual(client.max_tokens[0], 5000)

    def test_scene_stitch_prompt_includes_runtime_length_contract(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.messages: list[list[dict]] = []

            def chat(self, messages, **_kwargs) -> str:
                self.messages.append(messages)
                return (
                    "<<FORWIN_TITLE>>\n旧站来声\n<<FORWIN_BODY>>\n"
                    + "林夜沿着旧站台追查异常报站声。" * 25
                    + "\n<<FORWIN_SUMMARY>>\n林夜确认广播异常。"
                )

        client = FakeClient()
        writer = ChapterWriter(
            client,
            writer_mode="scene",
            min_chapter_chars=300,
            target_chapter_chars=350,
            max_chapter_chars=400,
        )
        context = ChapterContextPack(
            project_id="p1",
            project_title="测试书",
            premise="前提",
            genre="悬疑",
            setting_summary="旧城站台",
            chapter_number=4,
            chapter_plan_title="第四章",
            chapter_plan_one_line="主角进入旧站",
            chapter_goals=["进入旧站", "确认广播异常"],
        )

        writer._stitch_scenes(
            context,
            [
                SceneOutput(
                    scene_no=1, scene_objective="进入旧站", text="林夜进入旧站。"
                )
            ],
        )

        prompt = client.messages[0][1]["content"]
        self.assertIn("目标正文长度 350 到 400 中文字", prompt)
        self.assertIn("不得低于 300 中文字", prompt)

    def test_scene_stitch_timeout_uses_single_attempt_before_outer_fallback(
        self,
    ) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.calls = 0

            def chat(self, _messages, **_kwargs) -> str:
                self.calls += 1
                raise httpx.ReadTimeout("stitch timed out")

        client = FakeClient()
        writer = ChapterWriter(client, writer_mode="scene", max_tokens=10000)
        context = ChapterContextPack(
            project_id="p1",
            project_title="测试书",
            premise="前提",
            genre="悬疑",
            setting_summary="旧城站台",
            chapter_number=4,
            chapter_plan_title="第四章",
            chapter_plan_one_line="主角进入旧站",
            chapter_goals=["进入旧站", "确认广播异常"],
        )

        with self.assertRaises(ValueError):
            writer._stitch_scenes(
                context,
                [
                    SceneOutput(
                        scene_no=1, scene_objective="进入旧站", text="林夜进入旧站。"
                    )
                ],
            )

        self.assertEqual(client.calls, 1)

    def test_scene_generation_timeout_uses_single_attempt_before_outer_fallback(
        self,
    ) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.scene_generation_calls = 0
                self.json_calls = 0

            def chat(
                self,
                _messages,
                temperature: float,
                max_tokens: int,
                *,
                stage_key: str = "",
                **_kwargs,
            ) -> str:
                if stage_key == "scene_breakdown":
                    return '{"scenes":[{"scene_no":1,"objective":"进入旧站","must_progress_points":["进入旧站"],"target_chars":500}]}'
                if stage_key == "scene_generation":
                    self.scene_generation_calls += 1
                    raise httpx.ReadTimeout("scene generation timed out")
                if stage_key in {"chapter_draft", "writer_preview"}:
                    return (
                        "<<FORWIN_TITLE>>\n"
                        "单章回退\n"
                        "<<FORWIN_BODY>>\n"
                        + "林夜踩着积水穿过旧站台，听见广播里多出了一段不属于这个时代的报站声。"
                        * 20
                        + "\n<<FORWIN_SUMMARY>>\n"
                        "林夜确认旧站台里还藏着第二条线索。"
                    )
                self.json_calls += 1
                return [
                    '{"state_changes":[],"new_events":[]}',
                    '{"thread_beats":[],"time_advance":null}',
                    '{"lore_candidates":[],"timeline_hints":[],"writer_notes":[]}',
                ][self.json_calls - 1]

        client = FakeClient()
        writer = ChapterWriter(
            client,
            writer_mode="scene",
            min_chapter_chars=300,
        )
        context = ChapterContextPack(
            project_id="p1",
            project_title="测试书",
            premise="前提",
            genre="悬疑",
            setting_summary="旧城站台",
            chapter_number=4,
            chapter_plan_title="第四章",
            chapter_plan_one_line="主角进入旧站",
            chapter_goals=["进入旧站", "确认广播异常"],
        )

        output = writer.write_chapter(context)

        self.assertEqual(client.scene_generation_calls, 1)
        self.assertEqual(output.generation_meta["mode"], "single")
        self.assertTrue(output.generation_meta["fallback_from_scene"])

    def test_scene_writer_outputs_continuation_contract(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.responses = [
                    (
                        '{"scenes":[{"scene_no":1,"objective":"进入旧站","must_progress_points":["进入旧站"],'
                        '"time_hint":"夜里","location_hint":"旧站台","involved_entities":["林夜"],'
                        '"micro_hook":"广播异响","target_chars":500,"reward_beat_tag":"mystery",'
                        '"immersion_anchor":"雨声","progress_marker":"发现异响"}]}'
                    ),
                    (
                        "<<FORWIN_BODY>>\n"
                        + "林夜走进旧站台，雨水沿着檐角落下，广播忽然响起陌生报站。"
                        * 12
                        + "\n<<FORWIN_SUMMARY>>\n林夜发现旧站广播异常。"
                        "\n<<FORWIN_TIME>>\n夜里"
                        "\n<<FORWIN_LOCATION>>\n旧站台"
                        "\n<<FORWIN_ENTITIES>>\n林夜"
                        "\n<<FORWIN_REWARD>>\nmystery"
                        "\n<<FORWIN_IMMERSION>>\n雨声与空站台"
                        "\n<<FORWIN_PROGRESS>>\n确认广播异常"
                        "\n<<FORWIN_CONTINUITY_ANCHOR>>\n陌生报站声还在继续"
                        "\n<<FORWIN_UNRESOLVED_HOOK>>\n报站声来自哪里"
                        "\n<<FORWIN_NEXT_BRIDGE>>\n追查广播室"
                        "\n<<FORWIN_TIME_CONTINUITY>>\n同一夜晚"
                        "\n<<FORWIN_LOCATION_CONTINUITY>>\n从站台转向广播室"
                        "\n<<FORWIN_CHARACTER_FOCUS>>\n林夜"
                    ),
                    (
                        "<<FORWIN_TITLE>>\n旧站来声\n<<FORWIN_BODY>>\n"
                        + "林夜走进旧站台，雨声和陌生报站声纠缠在一起。" * 20
                        + "\n<<FORWIN_SUMMARY>>\n林夜确认旧站广播异常。"
                    ),
                    '{"state_changes":[],"new_events":[]}',
                    '{"thread_beats":[],"time_advance":null}',
                    '{"lore_candidates":[],"timeline_hints":[],"writer_notes":[]}',
                ]

            def chat(
                self, _messages, temperature: float, max_tokens: int, **_kwargs
            ) -> str:
                return self.responses.pop(0)

        writer = ChapterWriter(
            FakeClient(),
            writer_mode="scene",
            min_chapter_chars=300,
        )
        context = ChapterContextPack(
            project_id="p1",
            project_title="测试书",
            premise="前提",
            genre="悬疑",
            setting_summary="旧城站台",
            chapter_number=4,
            chapter_plan_title="第四章",
            chapter_plan_one_line="主角进入旧站",
            chapter_goals=["进入旧站", "确认广播异常"],
            active_entities=[{
                "entity_id": "lin-ye", "kind": "character", "name": "林夜",
                "description": "调查者", "current_state": {"location": "旧站台"},
            }],
        )

        output = writer.write_chapter(context)

        self.assertEqual(output.generation_meta["mode"], "scene")
        self.assertEqual(output.generation_meta["call_count"], 6)
        self.assertEqual(
            output.scene_outputs[0].continuation.continuity_anchor, "陌生报站声还在继续"
        )
        self.assertEqual(output.scene_continuation[0].next_scene_bridge, "追查广播室")


if __name__ == "__main__":
    unittest.main()
