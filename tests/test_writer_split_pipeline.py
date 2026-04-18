from __future__ import annotations

import unittest

from forwin.protocol.context import ChapterContextPack
from forwin.writer.chapter_writer import ChapterWriter


class SplitWriterPipelineTests(unittest.TestCase):
    def test_single_writer_uses_tagged_body_and_split_structured_extraction(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.responses = [
                    (
                        "<<FORWIN_TITLE>>\n"
                        "雨夜旧站\n"
                        "<<FORWIN_BODY>>\n"
                        + "林夜踩着积水穿过旧站台，听见广播里多出了一段不属于这个时代的报站声。" * 20
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

            def chat(self, _messages, temperature: float, max_tokens: int, **_kwargs) -> str:
                return self.responses.pop(0)

        writer = ChapterWriter(FakeClient(), writer_mode="single")
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
        self.assertEqual(output.state_changes[0].new_value, "旧站台")
        self.assertEqual(output.thread_beats[0].thread_name, "旧站疑云")
        self.assertEqual(output.time_advance.new_time_label, "深夜")
        self.assertEqual(output.lore_candidates[0].subject_name, "旧站台")
        self.assertEqual(output.timeline_hints[0].projected_time_label, "深夜继续")
        self.assertEqual(output.writer_notes[0].target_name, "旧站疑云")
        self.assertEqual(output.generation_meta["mode"], "single")
        self.assertEqual(output.generation_meta["call_count"], 4)
        self.assertEqual(output.generation_meta["structured_extraction_calls"], 3)

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
                        + "林夜走进旧站台，雨水沿着檐角落下，广播忽然响起陌生报站。" * 12
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

            def chat(self, _messages, temperature: float, max_tokens: int, **_kwargs) -> str:
                return self.responses.pop(0)

        writer = ChapterWriter(FakeClient(), writer_mode="scene")
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

        self.assertEqual(output.generation_meta["mode"], "scene")
        self.assertEqual(output.generation_meta["call_count"], 6)
        self.assertEqual(output.scene_outputs[0].continuation.continuity_anchor, "陌生报站声还在继续")
        self.assertEqual(output.scene_continuation[0].next_scene_bridge, "追查广播室")


if __name__ == "__main__":
    unittest.main()
