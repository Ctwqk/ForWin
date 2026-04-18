from __future__ import annotations

import unittest
from types import SimpleNamespace

from forwin.api_project_payloads import build_generation_control
from forwin.governance import DecisionEventInfo
from forwin.models.governance import BandCheckpoint
from forwin.models.project import ChapterPlan


def _plan(chapter_number: int, status: str) -> ChapterPlan:
    return ChapterPlan(
        id=f"plan-{chapter_number}",
        project_id="project-control-1",
        arc_plan_id="arc-control-1",
        chapter_number=chapter_number,
        title=f"第{chapter_number}章",
        one_line="",
        goals_json="[]",
        status=status,
    )


class GenerationControlPayloadTests(unittest.TestCase):
    def test_pending_review_blocks_resume_and_reports_next_chapter(self) -> None:
        control = build_generation_control(
            plans=[
                _plan(1, "accepted"),
                _plan(2, "needs_review"),
                _plan(3, "planned"),
            ],
            latest_replan=SimpleNamespace(cooldown_until_chapter=5),
            review_interval_chapters=2,
        )

        self.assertEqual(control.plan_state, "needs_review")
        self.assertEqual(control.review_state, "pending")
        self.assertEqual(control.accepted_chapters, [1])
        self.assertEqual(control.pending_review_chapters, [2])
        self.assertEqual(control.next_chapter, 3)
        self.assertFalse(control.can_resume)
        self.assertEqual(control.chapters_until_review, 0)
        self.assertEqual(control.chapters_until_replan_eligible, 3)

    def test_review_interval_counts_from_accepted_chapters(self) -> None:
        control = build_generation_control(
            plans=[
                _plan(1, "accepted"),
                _plan(2, "accepted"),
                _plan(3, "planned"),
                _plan(4, "planned"),
            ],
            latest_replan=None,
            review_interval_chapters=2,
        )

        self.assertEqual(control.plan_state, "in_progress")
        self.assertEqual(control.review_state, "none")
        self.assertTrue(control.can_resume)
        self.assertEqual(control.next_chapter, 3)
        self.assertEqual(control.chapters_until_review, 2)

    def test_band_checkpoint_warning_exposes_blocking_reason(self) -> None:
        control = build_generation_control(
            plans=[
                _plan(1, "accepted"),
                _plan(2, "planned"),
            ],
            latest_replan=None,
            review_interval_chapters=0,
            latest_band_checkpoint=BandCheckpoint(
                id="cp-1",
                project_id="project-control-1",
                arc_id="arc-control-1",
                band_id="band-1",
                boundary_chapter=1,
                status="warn",
                summary="band checkpoint warning",
            ),
            decision_events=[
                DecisionEventInfo(
                    id="evt-checkpoint-1",
                    project_id="project-control-1",
                    band_id="band-1",
                    scope="band",
                    event_family="evaluation_verdict",
                    event_type="band_checkpoint_created",
                    related_object_type="band_checkpoint",
                    related_object_id="cp-1",
                )
            ],
        )

        self.assertEqual(control.blocking_reason.code, "band_checkpoint_warn")
        self.assertIn("checkpoint", control.blocking_reason.message)
        self.assertEqual(control.blocking_reason.decision_event_id, "evt-checkpoint-1")
        self.assertEqual(control.latest_band_checkpoint.status, "warn")
        self.assertEqual(control.next_gate, "band_checkpoint_warn")

    def test_future_constraint_failure_exposes_blocking_reason_and_event_id(self) -> None:
        control = build_generation_control(
            plans=[
                _plan(1, "failed"),
                _plan(2, "planned"),
            ],
            latest_replan=None,
            review_interval_chapters=0,
            decision_events=[
                DecisionEventInfo(
                    id="evt-future-constraint-1",
                    project_id="project-control-1",
                    chapter_number=1,
                    scope="chapter",
                    event_family="evaluation_verdict",
                    event_type="review_verdict_recorded",
                    payload={
                        "verdict": "fail",
                        "issue_types": ["future_constraint"],
                    },
                )
            ],
        )

        self.assertEqual(control.blocking_reason.code, "future_constraint_block")
        self.assertEqual(control.blocking_reason.chapter_number, 1)
        self.assertEqual(control.blocking_reason.decision_event_id, "evt-future-constraint-1")
        self.assertEqual(control.next_gate, "future_constraint_block")


if __name__ == "__main__":
    unittest.main()
