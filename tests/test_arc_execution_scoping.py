from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sqlalchemy import select

from forwin.config import Config
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.project import ArcPlanVersion, ChapterPlan
from forwin.orchestrator.loop import RunResult, WritingOrchestrator
from forwin.orchestrator.phase24 import ArcEnvelopeManager
from forwin.state.repo import StateRepository
from forwin.state.updater import StateUpdater


def _chapter_payloads(total: int) -> list[dict]:
    return [
        {
            "chapter_number": index,
            "title": f"第{index}章",
            "one_line": f"推进第{index}章冲突",
            "goals": ["推进主线", "加压升级"],
        }
        for index in range(1, total + 1)
    ]


class ArcExecutionScopingTests(unittest.TestCase):
    def test_seed_state_distributes_chapters_across_arc_outlines(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "seed-state.db")
            orchestrator = WritingOrchestrator(
                Config(db_path=db_path, minimax_api_key="", minimax_model="fake-model")
            )
            try:
                session = orchestrator._SessionFactory()
                try:
                    updater = StateUpdater(session)
                    project = updater.create_project(
                        title="测试书",
                        premise="前提",
                        genre="玄幻",
                        target_total_chapters=20,
                    )
                    orchestrator._seed_state(
                        updater,
                        project.id,
                        {
                            "arc_synopsis": "总弧线",
                            "chapters": _chapter_payloads(20),
                            "arc_outlines": [
                                {
                                    "arc_number": 1,
                                    "chapter_start": 1,
                                    "chapter_end": 11,
                                    "chapter_count": 11,
                                    "arc_synopsis": "开篇弧",
                                },
                                {
                                    "arc_number": 2,
                                    "chapter_start": 12,
                                    "chapter_end": 20,
                                    "chapter_count": 9,
                                    "arc_synopsis": "后续弧",
                                },
                            ],
                            "characters": [],
                            "locations": [],
                            "factions": [],
                            "relations": [],
                            "plot_threads": [],
                            "initial_time": {"label": "起点", "description": "开始"},
                        },
                        20,
                    )
                    session.commit()

                    arcs = session.execute(
                        select(ArcPlanVersion)
                        .where(ArcPlanVersion.project_id == project.id)
                    ).scalars().all()
                    plans = session.execute(
                        select(ChapterPlan)
                        .where(ChapterPlan.project_id == project.id)
                        .order_by(ChapterPlan.chapter_number.asc())
                    ).scalars().all()
                finally:
                    session.close()
            finally:
                orchestrator.llm_client.close()
                orchestrator.engine.dispose()

        self.assertEqual(len(arcs), 2)
        active_arc = next(arc for arc in arcs if arc.status == "active")
        planned_arc = next(arc for arc in arcs if arc.status == "planned")
        self.assertEqual(sum(1 for plan in plans if plan.arc_plan_id == active_arc.id), 11)
        self.assertEqual(sum(1 for plan in plans if plan.arc_plan_id == planned_arc.id), 9)
        self.assertEqual([plan.chapter_number for plan in plans], list(range(1, 21)))

    def test_new_project_run_executes_only_first_active_arc(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "run-scope.db")
            orchestrator = WritingOrchestrator(
                Config(db_path=db_path, minimax_api_key="", minimax_model="fake-model")
            )
            captured: dict[str, object] = {}

            def fake_run_project_chapters(**kwargs):
                captured["chapter_numbers"] = list(kwargs["chapter_numbers"])
                captured["requested_chapters"] = kwargs["requested_chapters"]
                return RunResult(
                    project_id=kwargs["project_id"],
                    requested_chapters=kwargs["requested_chapters"],
                )

            try:
                with (
                    patch.object(
                        orchestrator.arc_director,
                        "plan_arc",
                        return_value={
                            "arc_synopsis": "总弧线",
                            "setting_summary": "设定",
                            "chapters": _chapter_payloads(4),
                            "arc_outlines": [
                                {
                                    "arc_number": 1,
                                    "chapter_start": 1,
                                    "chapter_end": 2,
                                    "chapter_count": 2,
                                    "arc_synopsis": "开篇弧",
                                },
                                {
                                    "arc_number": 2,
                                    "chapter_start": 3,
                                    "chapter_end": 4,
                                    "chapter_count": 2,
                                    "arc_synopsis": "第二弧",
                                },
                            ],
                            "characters": [],
                            "locations": [],
                            "factions": [],
                            "relations": [],
                            "plot_threads": [],
                            "initial_time": {"label": "开始", "description": "开场"},
                        },
                    ),
                    patch.object(
                        orchestrator,
                        "_run_project_chapters",
                        side_effect=fake_run_project_chapters,
                    ),
                ):
                    result = orchestrator.run("故事前提", "玄幻", 4)

                session = orchestrator._SessionFactory()
                try:
                    plans = session.execute(
                        select(ChapterPlan)
                        .where(ChapterPlan.project_id == result.project_id)
                        .order_by(ChapterPlan.chapter_number.asc())
                    ).scalars().all()
                finally:
                    session.close()
            finally:
                orchestrator.llm_client.close()
                orchestrator.engine.dispose()

        self.assertEqual(captured["chapter_numbers"], [1, 2])
        self.assertEqual(captured["requested_chapters"], 2)
        self.assertEqual(len(plans), 4)

    def test_continue_project_only_runs_active_arc_pending_chapters(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "continue-scope.db")
            engine = get_engine(db_path)
            init_db(engine)
            session_factory = get_session_factory(engine)

            with session_factory() as session:
                updater = StateUpdater(session)
                project = updater.create_project(
                    title="测试书",
                    premise="前提",
                    genre="玄幻",
                    target_total_chapters=20,
                )
                arc_one = updater.create_arc_plan(project.id, "开篇弧", status="active")
                arc_two = updater.create_arc_plan(project.id, "第二弧", status="planned")
                for chapter_number in (1, 2):
                    updater.create_chapter_plan(
                        project_id=project.id,
                        arc_plan_id=arc_one.id,
                        chapter_number=chapter_number,
                        title=f"第{chapter_number}章",
                        one_line="开篇推进",
                        goals=["推进", "升级"],
                    )
                for chapter_number in (3, 4):
                    updater.create_chapter_plan(
                        project_id=project.id,
                        arc_plan_id=arc_two.id,
                        chapter_number=chapter_number,
                        title=f"第{chapter_number}章",
                        one_line="后续推进",
                        goals=["推进", "升级"],
                    )
                session.commit()

            orchestrator = WritingOrchestrator(
                Config(db_path=db_path, minimax_api_key="", minimax_model="fake-model")
            )
            captured: dict[str, object] = {}

            def fake_run_project_chapters(**kwargs):
                captured["chapter_numbers"] = list(kwargs["chapter_numbers"])
                captured["requested_chapters"] = kwargs["requested_chapters"]
                return RunResult(
                    project_id=kwargs["project_id"],
                    requested_chapters=kwargs["requested_chapters"],
                )

            try:
                with patch.object(
                    orchestrator,
                    "_run_project_chapters",
                    side_effect=fake_run_project_chapters,
                ):
                    orchestrator.continue_project(project.id)
            finally:
                orchestrator.llm_client.close()
                orchestrator.engine.dispose()
                engine.dispose()

        self.assertEqual(captured["chapter_numbers"], [1, 2])
        self.assertEqual(captured["requested_chapters"], 2)

    def test_arc_resolution_activates_target_arc_and_uses_project_total(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "arc-resolution.db")
            engine = get_engine(db_path)
            init_db(engine)
            session = get_session_factory(engine)()
            try:
                updater = StateUpdater(session)
                project = updater.create_project(
                    title="测试书",
                    premise="前提",
                    genre="玄幻",
                    target_total_chapters=100,
                )
                arc_one = updater.create_arc_plan(project.id, "开篇弧", status="active")
                arc_two = updater.create_arc_plan(project.id, "第二弧", status="planned")
                for chapter_number in range(1, 11):
                    updater.create_chapter_plan(
                        project_id=project.id,
                        arc_plan_id=arc_one.id,
                        chapter_number=chapter_number,
                        title=f"第{chapter_number}章",
                        one_line="开篇推进",
                        goals=["推进", "升级"],
                    )
                for chapter_number in range(11, 21):
                    updater.create_chapter_plan(
                        project_id=project.id,
                        arc_plan_id=arc_two.id,
                        chapter_number=chapter_number,
                        title=f"第{chapter_number}章",
                        one_line="后续推进",
                        goals=["推进", "升级"],
                    )

                manager = ArcEnvelopeManager(director=None)
                envelope = manager.ensure_active_arc_resolution(
                    session=session,
                    project_id=project.id,
                    activation_chapter=11,
                )
                active_arc = StateRepository(session).get_active_arc_plan(project.id)
            finally:
                session.close()
                engine.dispose()

        self.assertIsNotNone(envelope)
        self.assertEqual(active_arc.id, arc_two.id)
        self.assertEqual(envelope.arc_id, arc_two.id)
        self.assertEqual(envelope.base_target_size, 18)


if __name__ == "__main__":
    unittest.main()
