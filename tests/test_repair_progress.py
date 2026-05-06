from __future__ import annotations

import unittest

from tests.postgres import postgres_test_url

from forwin.config import Config
from forwin.orchestrator.loop import WritingOrchestrator
from forwin.protocol.review import ContinuityIssue, RepairInstruction, ReviewVerdict
from forwin.protocol.writer import WriterOutput


class RepairProgressTests(unittest.TestCase):
    def test_blackbox_repair_emits_repair_stages(self) -> None:
        class FailThenPassReviewHub:
            def __init__(self) -> None:
                self.calls = 0

            def review(self, **_kwargs) -> ReviewVerdict:  # noqa: ANN003
                self.calls += 1
                if self.calls == 1:
                    return ReviewVerdict(
                        verdict="fail",
                        issues=[
                            ContinuityIssue(
                                rule_name="weak_hook",
                                severity="error",
                                description="章末钩子偏弱",
                                reviewer="webnovel_experience",
                                issue_type="hook_failure",
                                target_scope="scene",
                                evidence_refs=["tail:正文"],
                            )
                        ],
                        repair_instruction=RepairInstruction(
                            repair_scope="scene",
                            failure_type="hook_failure",
                            must_fix=["章末钩子偏弱"],
                            must_preserve=["第一章", "开场"],
                            design_patch={"hook_type": "hard_cliffhanger"},
                            evidence_refs=["tail:正文"],
                        ),
                    )
                return ReviewVerdict(verdict="pass", issues=[])

        events: list[tuple[str, dict[str, object]]] = []
        orchestrator = WritingOrchestrator(
            Config(
                database_url=postgres_test_url("repair-progress"),
                minimax_api_key="",
                minimax_model="fake-model",
                operation_mode="blackbox",
                review_fail_max_rewrites=1,
                auto_band_checkpoint=False,
                manual_checkpoints_enabled=False,
            ),
            progress_callback=lambda event, payload: events.append((event, dict(payload))),
        )
        try:
            orchestrator.arc_director.plan_arc = lambda _premise, _genre, _num_chapters: {
                "arc_synopsis": "repair progress",
                "setting_summary": "无",
                "chapters": [
                    {
                        "chapter_number": 1,
                        "title": "第一章",
                        "one_line": "开场",
                        "goals": ["推进主线"],
                    }
                ],
                "characters": [],
                "locations": [],
                "factions": [],
                "relations": [],
                "plot_threads": [],
                "initial_time": {"label": "开始", "description": "开始"},
            }

            def write_chapter(context) -> WriterOutput:  # noqa: ANN001
                return WriterOutput(
                    project_id=context.project_id,
                    chapter_number=context.chapter_number,
                    title=f"第{context.chapter_number}章",
                    body="正文" * 900,
                    char_count=1800,
                    end_of_chapter_summary="ok",
                    state_changes=[],
                    new_events=[],
                    thread_beats=[],
                    time_advance=None,
                )

            orchestrator.writer.write_chapter = write_chapter
            orchestrator.review_hub = FailThenPassReviewHub()

            result = orchestrator.run("p", "g", 1)
        finally:
            orchestrator.llm_client.close()
            orchestrator.engine.dispose()

        stages = [
            str(payload.get("stage") or "")
            for event, payload in events
            if event == "stage_changed"
        ]
        self.assertEqual(result.status, "completed")
        self.assertIn("continuity_review", stages)
        self.assertIn("repairing_chapter", stages)
        self.assertIn("repair_review", stages)
        self.assertLess(stages.index("continuity_review"), stages.index("repairing_chapter"))
        self.assertLess(stages.index("repairing_chapter"), stages.index("repair_review"))
