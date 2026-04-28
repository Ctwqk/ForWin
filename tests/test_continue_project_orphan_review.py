from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from forwin.config import Config
from forwin.models.base import get_engine, get_session_factory, init_db, new_id
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from forwin.orchestrator.loop import RunResult, WritingOrchestrator


class ContinueProjectOrphanReviewTests(unittest.TestCase):
    def test_continue_project_commits_arc_resolution_before_running_chapters(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = postgres_test_url("continue-project-commit")
            engine = get_engine(db_path)
            init_db(engine)
            session_factory = get_session_factory(engine)

            project_id = new_id()
            arc_id = new_id()
            with session_factory() as session:
                session.add(
                    Project(
                        id=project_id,
                        title="事务提交测试",
                        premise="测试 premise",
                        genre="玄幻",
                        setting_summary="before",
                    )
                )
                session.commit()
                session.add(
                    ArcPlanVersion(
                        id=arc_id,
                        project_id=project_id,
                        version=1,
                        arc_synopsis="测试弧线",
                        status="active",
                    )
                )
                session.add(
                    ChapterPlan(
                        id=new_id(),
                        project_id=project_id,
                        arc_plan_id=arc_id,
                        chapter_number=1,
                        title="第一章",
                        one_line="一",
                        goals_json='["g1"]',
                        status="planned",
                    )
                )
                session.commit()

            orchestrator = WritingOrchestrator(
                Config(
                    database_url=db_path,
                    minimax_api_key="",
                    minimax_model="fake-model",
                    operation_mode="copilot",
                )
            )
            try:
                captured: dict[str, object] = {}

                def fake_ensure_active_arc_resolution(*, session, project_id, activation_chapter):
                    project = session.get(Project, project_id)
                    project.setting_summary = "after"
                    session.add(project)

                def fake_run_project_chapters(**kwargs):
                    with session_factory() as verify_session:
                        project = verify_session.get(Project, project_id)
                        captured["visible_setting_summary"] = project.setting_summary
                    return RunResult(
                        project_id=project_id,
                        requested_chapters=1,
                    )

                with (
                    patch.object(
                        orchestrator.arc_envelope_manager,
                        "ensure_active_arc_resolution",
                        side_effect=fake_ensure_active_arc_resolution,
                    ),
                    patch.object(
                        orchestrator,
                        "_run_project_chapters",
                        side_effect=fake_run_project_chapters,
                    ),
                ):
                    result = orchestrator.continue_project(project_id)

                self.assertEqual(result.project_id, project_id)
                self.assertEqual(captured["visible_setting_summary"], "after")
            finally:
                orchestrator.llm_client.close()
                orchestrator.engine.dispose()
                engine.dispose()

    def test_run_existing_project_delegates_to_continue_when_plans_exist(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = postgres_test_url("existing-project-continue")
            engine = get_engine(db_path)
            init_db(engine)
            session_factory = get_session_factory(engine)

            project_id = new_id()
            arc_id = new_id()
            with session_factory() as session:
                session.add(
                    Project(
                        id=project_id,
                        title="已有规划项目",
                        premise="测试 premise",
                        genre="玄幻",
                        setting_summary="",
                    )
                )
                session.commit()
                session.add(
                    ArcPlanVersion(
                        id=arc_id,
                        project_id=project_id,
                        version=1,
                        arc_synopsis="测试弧线",
                        status="active",
                    )
                )
                session.add(
                    ChapterPlan(
                        id=new_id(),
                        project_id=project_id,
                        arc_plan_id=arc_id,
                        chapter_number=1,
                        title="第一章",
                        one_line="一",
                        goals_json='["g1"]',
                        status="planned",
                    )
                )
                session.commit()

            orchestrator = WritingOrchestrator(
                Config(
                    database_url=db_path,
                    minimax_api_key="",
                    minimax_model="fake-model",
                    operation_mode="copilot",
                )
            )
            try:
                expected = RunResult(
                    project_id=project_id,
                    requested_chapters=1,
                    completed_chapters=[1],
                )
                with patch.object(
                    orchestrator,
                    "continue_project",
                    return_value=expected,
                ) as mocked_continue:
                    result = orchestrator.run_existing_project(project_id, num_chapters=1)

                self.assertIs(result, expected)
                mocked_continue.assert_called_once_with(project_id, max_chapters=1)
            finally:
                orchestrator.llm_client.close()
                orchestrator.engine.dispose()
                engine.dispose()

    def test_continue_project_resets_needs_review_without_draft(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = postgres_test_url("orphan-review")
            engine = get_engine(db_path)
            init_db(engine)
            session_factory = get_session_factory(engine)

            project_id = new_id()
            arc_id = new_id()
            with session_factory() as session:
                session.add(
                    Project(
                        id=project_id,
                        title="孤儿检查点测试",
                        premise="测试 premise",
                        genre="玄幻",
                        setting_summary="",
                    )
                )
                session.commit()
                session.add(
                    ArcPlanVersion(
                        id=arc_id,
                        project_id=project_id,
                        version=1,
                        arc_synopsis="测试弧线",
                        status="active",
                    )
                )
                session.add_all(
                    [
                        ChapterPlan(
                            id=new_id(),
                            project_id=project_id,
                            arc_plan_id=arc_id,
                            chapter_number=1,
                            title="第一章",
                            one_line="一",
                            goals_json='["g1"]',
                            status="needs_review",
                        ),
                        ChapterPlan(
                            id=new_id(),
                            project_id=project_id,
                            arc_plan_id=arc_id,
                            chapter_number=2,
                            title="第二章",
                            one_line="二",
                            goals_json='["g2"]',
                            status="planned",
                        ),
                    ]
                )
                session.commit()

            orchestrator = WritingOrchestrator(
                Config(
                    database_url=db_path,
                    minimax_api_key="",
                    minimax_model="fake-model",
                    operation_mode="copilot",
                )
            )
            try:
                captured: dict[str, object] = {}

                def fake_run_project_chapters(**kwargs):
                    captured["chapter_numbers"] = list(kwargs["chapter_numbers"])
                    return RunResult(
                        project_id=project_id,
                        requested_chapters=2,
                    )

                with (
                    patch.object(
                        orchestrator.arc_envelope_manager,
                        "ensure_active_arc_resolution",
                        return_value=None,
                    ),
                    patch.object(
                        orchestrator,
                        "_run_project_chapters",
                        side_effect=fake_run_project_chapters,
                    ),
                ):
                    result = orchestrator.continue_project(project_id)

                self.assertEqual(result.project_id, project_id)
                self.assertEqual(captured["chapter_numbers"], [1, 2])
                with session_factory() as session:
                    statuses = {
                        row.chapter_number: row.status
                        for row in session.query(ChapterPlan)
                        .filter(ChapterPlan.project_id == project_id)
                        .all()
                    }
                self.assertEqual(statuses[1], "planned")
                self.assertEqual(statuses[2], "planned")
            finally:
                orchestrator.llm_client.close()
                orchestrator.engine.dispose()
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
