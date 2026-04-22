from __future__ import annotations

import json
import unittest
from pathlib import Path

from forwin.protocol.context import ChapterContextPack
from forwin.protocol.experience import ChapterExperiencePlan
from forwin.protocol.review import ReviewVerdict
from forwin.protocol.writer import WriterOutput
from forwin.reviewer import HistoricalReviewHub
from forwin.skills import build_skill_runtime_components
from forwin.writer.chapter_writer import ChapterWriter


class _FakeWriterLLM:
    def __init__(self) -> None:
        self.profile_id = "writer-default"
        self.profile_name = "Writer Default"
        self.model = "fake-model"
        self.base_url = "http://fake.invalid/v1"

    def chat(self, messages, **kwargs):  # noqa: ANN001, ANN003
        if kwargs.get("response_format"):
            return json.dumps(
                {
                    "state_changes": [],
                    "new_events": [],
                    "thread_beats": [],
                    "time_advance": None,
                    "lore_candidates": [],
                    "timeline_hints": [],
                    "writer_notes": [],
                    "entity_mentions": [],
                },
                ensure_ascii=False,
            )
        return (
            "<<FORWIN_TITLE>>\n"
            "第一章 雨夜\n"
            "<<FORWIN_BODY>>\n"
            "雨夜里，他第一次看见那面会说话的镜子。\n"
            "<<FORWIN_SUMMARY>>\n"
            "主角在雨夜得到了危险线索。"
        )

    def drain_model_fallback_events(self):  # noqa: ANN201
        return []


class _PassChecker:
    def check(self, project_id, writer_output):  # noqa: ANN001, ANN201
        return ReviewVerdict(verdict="pass", issues=[], review_summary="ok")


class _ReviewerLLM:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    def chat(self, messages, **kwargs):  # noqa: ANN001, ANN003, ANN201
        self.messages = [dict(item) for item in messages]
        return json.dumps(
            {
                "verdict": "pass",
                "planned_reward_tags": ["mystery"],
                "delivered_reward_tags": ["mystery"],
                "experience_scores": {
                    "narrative_understanding": 0.8,
                    "attentional_focus": 0.7,
                    "emotional_engagement": 0.6,
                    "narrative_presence": 0.7,
                    "payoff_delivery": 0.8,
                    "stall_tolerance": 0.7,
                    "hook_efficiency": 0.6,
                },
                "issues": [],
                "review_notes": ["skill-guided reviewer"],
                "repair_instruction": None,
                "evidence_refs": ["overlay:chapter_experience_plan"],
                "review_summary": "ok",
            },
            ensure_ascii=False,
        )


class SkillRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.skill_root = Path(__file__).resolve().parents[1] / "forwin_skills"

    def _context(self) -> ChapterContextPack:
        return ChapterContextPack(
            project_id="project-skill",
            project_title="技能测试书",
            premise="雨夜里，主角得到一面会说话的镜子。",
            genre="玄幻",
            setting_summary="旧城与禁术并存。",
            chapter_number=1,
            chapter_plan_title="第一章 雨夜",
            chapter_plan_one_line="主角拿到危险线索。",
            chapter_goals=["建立危机", "抛出主线问题"],
            chapter_experience_plan=ChapterExperiencePlan(
                planned_reward_tags=["mystery"],
                hook_type="question",
                question_hook="镜子为什么会说话",
            ),
        )

    def test_registry_router_and_prompt_layer_load_repository_skills(self) -> None:
        registry, router, builder = build_skill_runtime_components(
            root=self.skill_root,
            enabled=True,
            strictness="strict",
        )

        manifests = registry.list_manifests()
        self.assertTrue(any(item.name == "genesis.world-bible" for item in manifests))

        selections = router.select(
            scope="genesis",
            stage_key="world",
            task_family="generate_stage_payload",
        )
        self.assertEqual([item.manifest.name for item in selections], ["genesis.world-bible"])

        layers = builder.build(selections)
        self.assertEqual(layers[0].skill_id, "genesis.world-bible")
        self.assertTrue(layers[0].skill_hash.startswith("sha256:"))
        self.assertIn("Skill: genesis.world-bible", layers[0].content)

    def test_enabled_skill_groups_filter_by_scope_directory(self) -> None:
        _registry, router, _builder = build_skill_runtime_components(
            root=self.skill_root,
            enabled=True,
            strictness="normal",
            enabled_skill_groups=["writer"],
        )

        writer_selections = router.select(
            scope="writer",
            stage_key="chapter_draft",
            task_family="write_chapter",
        )
        genesis_selections = router.select(
            scope="genesis",
            stage_key="world",
            task_family="generate_stage_payload",
        )

        self.assertTrue(writer_selections)
        self.assertTrue(all(item.manifest.group == "writer" for item in writer_selections))
        self.assertEqual(genesis_selections, [])

    def test_chapter_writer_records_skill_prompt_trace(self) -> None:
        _registry, router, builder = build_skill_runtime_components(
            root=self.skill_root,
            enabled=True,
            strictness="normal",
        )
        skill_layers = builder.build(
            router.select(
                scope="writer",
                stage_key="chapter_draft",
                task_family="write_chapter",
            )
        )
        writer = ChapterWriter(_FakeWriterLLM(), writer_mode="single")

        output = writer.write_chapter(
            self._context(),
            skill_layers=skill_layers,
            trace_stage_key="chapter_draft",
        )

        prompt_trace = output.generation_meta.get("prompt_trace") or {}
        self.assertEqual(prompt_trace.get("trace_scope"), "writer")
        self.assertEqual(prompt_trace.get("stage_key"), "chapter_draft")
        self.assertEqual(prompt_trace.get("template_id"), "writer:single")
        self.assertTrue(prompt_trace.get("input_snapshot", {}).get("selected_skills"))
        self.assertTrue(
            any(
                item.get("kind") == "skill"
                for item in (prompt_trace.get("prompt_layers") or [])
            )
        )

    def test_review_hub_adds_skill_notes_without_overriding_verdict(self) -> None:
        _registry, router, builder = build_skill_runtime_components(
            root=self.skill_root,
            enabled=True,
            strictness="normal",
        )
        reviewer_skill_layers = builder.build(
            router.select(
                scope="reviewer",
                stage_key="chapter_review",
                task_family="review_chapter",
            )
        )
        hub = HistoricalReviewHub(
            experience_review_enabled=False,
            lint_review_enabled=False,
        )

        verdict = hub.review(
            project_id="project-skill",
            repo=None,
            context=self._context(),
            writer_output=WriterOutput(
                project_id="project-skill",
                chapter_number=1,
                title="第一章 雨夜",
                body="雨夜里，他第一次看见那面会说话的镜子。",
                char_count=20,
                end_of_chapter_summary="主角拿到了危险线索。",
            ),
            continuity_checker=_PassChecker(),
            reviewer_skill_layers=reviewer_skill_layers,
        )

        self.assertEqual(verdict.verdict, "pass")
        self.assertTrue(any("reviewer skills" in note for note in verdict.review_notes))
        self.assertEqual(verdict.prompt_trace.get("trace_scope"), "reviewer")
        self.assertTrue(verdict.prompt_trace.get("input_snapshot", {}).get("selected_skills"))

    def test_reviewer_skill_layers_are_injected_into_reviewer_llm_prompt(self) -> None:
        _registry, router, builder = build_skill_runtime_components(
            root=self.skill_root,
            enabled=True,
            strictness="strict",
        )
        reviewer_skill_layers = builder.build(
            router.select(
                scope="reviewer",
                stage_key="chapter_review",
                task_family="review_chapter",
            )
        )
        llm = _ReviewerLLM()
        hub = HistoricalReviewHub(
            experience_review_enabled=True,
            lint_review_enabled=False,
            llm_client=llm,
            llm_enabled=True,
        )

        verdict = hub.review(
            project_id="project-skill",
            repo=None,
            context=self._context(),
            writer_output=WriterOutput(
                project_id="project-skill",
                chapter_number=1,
                title="第一章 雨夜",
                body="雨夜里，他第一次看见那面会说话的镜子。",
                char_count=20,
                end_of_chapter_summary="主角拿到了危险线索。",
            ),
            continuity_checker=_PassChecker(),
            reviewer_skill_layers=reviewer_skill_layers,
        )

        self.assertEqual(verdict.reviewer_mode, "llm")
        system_messages = [item["content"] for item in llm.messages if item.get("role") == "system"]
        self.assertTrue(any("Skill: reviewer.chapter-continuity" in item for item in system_messages))
        self.assertTrue(any("Skill: reviewer.repair-plan" in item for item in system_messages))


if __name__ == "__main__":
    unittest.main()
