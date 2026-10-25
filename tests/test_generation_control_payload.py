from __future__ import annotations

import unittest

from forwin.application.projects.common import _overlay_active_generation_task
from forwin.application.read_models import build_generation_control
from forwin.api_schema import GenerationControlInfo, ProjectDetail
from forwin.audit.events import DecisionEventInfo
from forwin.models.planning_control import BandCheckpoint
from forwin.models.project import ChapterPlan
from forwin.models.task import GenerationTask
from forwin.runtime.policy import RuntimePolicy


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
            review_interval_chapters=2,
        )

        self.assertEqual(control.plan_state, "needs_review")
        self.assertEqual(control.review_state, "pending")
        self.assertEqual(control.accepted_chapters, [1])
        self.assertEqual(control.pending_review_chapters, [2])
        self.assertEqual(control.next_chapter, 3)
        self.assertFalse(control.can_resume)
        self.assertEqual(control.chapters_until_review, 0)
        self.assertNotIn("chapters_until_" + "replan_eligible", control.model_dump())

    def test_review_interval_counts_from_accepted_chapters(self) -> None:
        control = build_generation_control(
            plans=[
                _plan(1, "accepted"),
                _plan(2, "accepted"),
                _plan(3, "planned"),
                _plan(4, "planned"),
            ],
            review_interval_chapters=2,
        )

        self.assertEqual(control.plan_state, "in_progress")
        self.assertEqual(control.review_state, "none")
        self.assertTrue(control.can_resume)
        self.assertEqual(control.next_chapter, 3)
        self.assertEqual(control.chapters_until_review, 2)

    def test_drafted_chapter_waits_for_acceptance_not_rewrite(self) -> None:
        control = build_generation_control(
            plans=[
                _plan(1, "accepted"),
                _plan(2, "drafted"),
                _plan(3, "planned"),
            ],
            review_interval_chapters=0,
        )

        self.assertEqual(control.plan_state, "pending_acceptance")
        self.assertEqual(control.review_state, "pending_acceptance")
        self.assertEqual(control.drafted_chapters, [2])
        self.assertEqual(control.next_gate, "chapter_2_accept")
        self.assertFalse(control.can_resume)

    def test_band_checkpoint_warning_exposes_blocking_reason(self) -> None:
        control = build_generation_control(
            plans=[
                _plan(1, "accepted"),
                _plan(2, "planned"),
            ],
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

    def test_completed_project_ignores_stale_band_checkpoint_warning(self) -> None:
        control = build_generation_control(
            plans=[
                _plan(1, "accepted"),
                _plan(2, "accepted"),
            ],
            review_interval_chapters=0,
            latest_band_checkpoint=BandCheckpoint(
                id="cp-completed-1",
                project_id="project-control-1",
                arc_id="arc-control-1",
                band_id="band-completed",
                boundary_chapter=2,
                status="warn",
                summary="final band warning",
            ),
        )

        self.assertEqual(control.plan_state, "completed")
        self.assertEqual(control.writing_state, "completed")
        self.assertEqual(control.next_gate, "completed")
        self.assertEqual(control.blocking_reason.code, "")


    def test_active_task_overlay_does_not_replace_project_chapter_sets(self) -> None:
        detail = ProjectDetail(
            id="project-control-1",
            title="Project Control",
            premise="premise",
            genre="genre",
            setting_summary="",
            runtime_policy=RuntimePolicy.for_profile("standard"),
            runtime_policy_version=1,
            generation_control=GenerationControlInfo(
                accepted_chapters=[1, 2, 3],
                generated_chapters=[1, 2, 3],
                planned_chapters=[4, 5],
                failed_chapters=[],
                pending_review_chapters=[],
                current_stage="",
                current_chapter=3,
                can_resume=True,
            ),
        )
        task = GenerationTask(
            id="task-control-1",
            project_id="project-control-1",
            status="running",
            current_stage="writing_chapter",
            current_chapter=4,
            completed_chapters_json="[4]",
            failed_chapters_json="[]",
            paused_chapters_json="[]",
        )

        updated = _overlay_active_generation_task(detail, task)

        self.assertEqual(updated.generation_control.accepted_chapters, [1, 2, 3])
        self.assertEqual(updated.generation_control.generated_chapters, [1, 2, 3])
        self.assertEqual(updated.generation_control.planned_chapters, [4, 5])
        self.assertEqual(updated.generation_control.current_stage, "writing_chapter")
        self.assertEqual(updated.generation_control.current_chapter, 4)
        self.assertTrue(updated.generation_control.can_pause)
        self.assertFalse(updated.generation_control.can_resume)


if __name__ == "__main__":
    unittest.main()


def test_latest_task_stage_ignores_deleted_and_other_task_kinds() -> None:
    from datetime import datetime, timedelta
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from sqlalchemy.schema import CreateTable
    from forwin.state.query_helpers import load_latest_task_stage_by_project

    engine = create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            # The production active-task partial index is PostgreSQL-specific.
            connection.execute(CreateTable(GenerationTask.__table__))
        with Session(engine) as session:
            now = datetime(2026, 10, 23)
            session.add_all([
                GenerationTask(id="old", project_id="p", current_stage="review", updated_at=now),
                GenerationTask(id="new-a", project_id="p", current_stage="plan", updated_at=now + timedelta(seconds=1)),
                GenerationTask(id="new-b", project_id="p", current_stage="writer", updated_at=now + timedelta(seconds=1)),
                GenerationTask(id="deleted", project_id="p", current_stage="deleted", updated_at=now + timedelta(seconds=2), deleted_at=now),
                GenerationTask(id="upload", project_id="p", task_kind="upload", current_stage="upload", updated_at=now + timedelta(seconds=3)),
                GenerationTask(id="other", project_id="other", current_stage="queued", updated_at=now),
            ])
            session.flush()
            assert load_latest_task_stage_by_project(session, ["p", "missing"]) == {"p": "writer"}
            assert load_latest_task_stage_by_project(session, []) == {}
    finally:
        engine.dispose()


def test_project_read_models_take_stage_from_generation_task() -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from forwin.models import Base, Project
    from forwin.application.read_models import build_project_detail, build_project_summaries
    from forwin.runtime.policy_store import ProjectPolicyStore

    engine = create_engine("sqlite:///:memory:")
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            project = Project(id="stage-project", title="Title", premise="Premise", genre="Genre")
            ProjectPolicyStore(session).initialize(project, RuntimePolicy.for_profile("standard"))
            session.add(project)
            session.flush()
            assert build_project_detail(session=session, project=project, display_datetime=str).latest_stage == ""
            session.add(GenerationTask(id="stage-task", project_id=project.id, current_stage="writer"))
            session.flush()
            detail = build_project_detail(session=session, project=project, display_datetime=str)
            summary = build_project_summaries(session=session, projects=[project], display_datetime=str)[0]
            assert detail.latest_stage == summary.latest_stage == "writer"
            removed = {"pacing_verdict", "pacing_summary", "recent_replans", "last_replan_status",
                       "last_replan_strategy", "last_replan_reason", "current_time_label", "progress_ratio"}
            assert removed.isdisjoint(detail.model_dump())
            assert removed.isdisjoint(summary.model_dump())
    finally:
        engine.dispose()
