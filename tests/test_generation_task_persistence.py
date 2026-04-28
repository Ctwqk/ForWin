from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import forwin.api as api_module
from forwin.config import Config
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.phase import ProvisionalBandExecution
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from forwin.models.task import GenerationTask


class GenerationTaskPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = TemporaryDirectory()
        self.engine = get_engine(postgres_test_url("generation-tasks"))
        init_db(self.engine)
        self.session_factory = get_session_factory(self.engine)
        self.old_session_factory = api_module._SessionFactory
        api_module._SessionFactory = self.session_factory
        with api_module._tasks_lock:
            self.old_tasks = dict(api_module._tasks)
            api_module._tasks.clear()

    def tearDown(self) -> None:
        with api_module._tasks_lock:
            api_module._tasks.clear()
            api_module._tasks.update(self.old_tasks)
        api_module._SessionFactory = self.old_session_factory
        self.engine.dispose()
        self.tmpdir.cleanup()

    def test_persisted_generation_task_is_listed_from_database(self) -> None:
        task = api_module._create_task_record(
            message="开始生成 3 章。",
            title="测试任务",
            subtitle="玄幻 · 3 章",
            requested_chapters=3,
        )
        task["project_id"] = "project-1"
        task["status"] = "running"
        task["current_stage"] = "writing_chapter"

        api_module._persist_generation_task("task-db-1", task)

        loaded = api_module._get_generation_task_or_404("task-db-1")
        listed = api_module._list_generation_tasks(10)

        self.assertEqual(loaded["title"], "测试任务")
        self.assertEqual(loaded["project_id"], "project-1")
        self.assertEqual(loaded["current_stage"], "writing_chapter")
        self.assertEqual([item[0] for item in listed], ["task-db-1"])

    def test_recover_interrupted_generation_tasks_marks_running_tasks_failed(self) -> None:
        task = api_module._create_task_record(
            message="正在写作。",
            title="重启恢复测试",
            requested_chapters=2,
        )
        task["status"] = "running"
        task["current_stage"] = "writing_chapter"
        task["current_chapter"] = 1

        api_module._persist_generation_task("task-recover-1", task)

        recovered_ids = api_module._recover_interrupted_generation_tasks()
        recovered = api_module._get_generation_task_or_404("task-recover-1")

        self.assertEqual(recovered_ids, ["task-recover-1"])
        self.assertEqual(recovered["status"], "failed")
        self.assertEqual(recovered["current_stage"], "failed")
        self.assertEqual(recovered["error"], "generation_interrupted_after_restart")
        serialized = api_module._serialize_generation_task_center_item("task-recover-1", recovered)
        self.assertTrue(serialized.interrupted_by_restart)
        self.assertEqual(serialized.recovery_suggestion, "rerun")
        self.assertEqual(recovered["stage_history"][-1]["stage"], "failed")

    def test_recover_interrupted_pause_requested_task_marks_paused(self) -> None:
        task = api_module._create_task_record(
            message="等待安全暂停。",
            title="暂停恢复测试",
            requested_chapters=2,
        )
        task["status"] = "running"
        task["current_stage"] = "writing_chapter"
        task["current_chapter"] = 1
        task["pause_requested"] = True

        api_module._persist_generation_task("task-recover-pause-1", task)

        recovered_ids = api_module._recover_interrupted_generation_tasks()
        recovered = api_module._get_generation_task_or_404("task-recover-pause-1")

        self.assertEqual(recovered_ids, ["task-recover-pause-1"])
        self.assertEqual(recovered["status"], "paused")
        self.assertEqual(recovered["current_stage"], "paused")
        self.assertTrue(recovered["pause_requested"])
        self.assertEqual(recovered["stage_history"][-1]["stage"], "paused")

    def test_pause_request_blocks_stale_running_progress_updates(self) -> None:
        task = api_module._create_task_record(title="暂停竞态测试", requested_chapters=1)
        task["status"] = "running"
        task["current_stage"] = "writing_chapter"
        api_module._persist_generation_task("task-pause-race-1", task)

        api_module._update_task(
            "task-pause-race-1",
            pause_requested=True,
            message="已请求暂停",
        )
        api_module._update_task(
            "task-pause-race-1",
            status="running",
            current_stage="continuity_review",
            current_chapter=1,
        )

        loaded = api_module._get_generation_task_or_404("task-pause-race-1")
        self.assertEqual(loaded["status"], "running")
        self.assertEqual(loaded["current_stage"], "writing_chapter")
        self.assertEqual(loaded["current_chapter"], 1)
        self.assertTrue(loaded["pause_requested"])

        api_module._update_task("task-pause-race-1", status="paused", message="已安全暂停")
        paused = api_module._get_generation_task_or_404("task-pause-race-1")
        self.assertEqual(paused["status"], "paused")
        self.assertEqual(paused["current_stage"], "paused")
        self.assertTrue(paused["pause_requested"])

    def test_continue_generation_api_rejects_pending_review_chapters(self) -> None:
        old_config = api_module._config
        api_module._config = Config(
            database_url=postgres_test_url("generation-tasks"),
            minimax_api_key="sk-test",
        )
        now = datetime.now(timezone.utc)
        try:
            with self.session_factory() as session:
                project = Project(
                    id="project-review-block-1",
                    title="Review 阻塞测试",
                    premise="测试",
                    genre="玄幻",
                    created_at=now,
                    updated_at=now,
                )
                arc = ArcPlanVersion(
                    id="arc-review-block-1",
                    project_id=project.id,
                    version=1,
                    arc_synopsis="测试 arc",
                    status="active",
                    created_at=now,
                )
                session.add(project)
                session.commit()
                session.add(arc)
                session.add_all(
                    [
                        ChapterPlan(
                            id="plan-review-block-1",
                            project_id=project.id,
                            arc_plan_id=arc.id,
                            chapter_number=1,
                            title="第一章",
                            one_line="一",
                            goals_json="[]",
                            status="needs_review",
                        ),
                        ChapterPlan(
                            id="plan-review-block-2",
                            project_id=project.id,
                            arc_plan_id=arc.id,
                            chapter_number=2,
                            title="第二章",
                            one_line="二",
                            goals_json="[]",
                            status="planned",
                        ),
                    ]
                )
                session.commit()

            with self.assertRaises(Exception) as raised:
                api_module.continue_project_generation("project-review-block-1")

            self.assertEqual(getattr(raised.exception, "status_code", None), 409)
            self.assertIn("等待 review", str(getattr(raised.exception, "detail", "")))
        finally:
            api_module._config = old_config

    def test_project_active_generation_detection_uses_persisted_tasks(self) -> None:
        completed = api_module._create_task_record(title="已完成", requested_chapters=1)
        completed["project_id"] = "project-2"
        completed["status"] = "completed"
        completed["current_stage"] = "completed"
        api_module._persist_generation_task("task-complete-1", completed)

        running = api_module._create_task_record(title="进行中", requested_chapters=1)
        running["project_id"] = "project-2"
        running["status"] = "running"
        running["current_stage"] = "writing_chapter"
        api_module._persist_generation_task("task-running-1", running)

        self.assertTrue(api_module._project_has_active_generation_task("project-2"))

        api_module._update_task("task-running-1", status="completed", current_stage="completed")

        self.assertFalse(api_module._project_has_active_generation_task("project-2"))

    def test_active_generation_check_reports_restart_safety(self) -> None:
        running = api_module._create_task_record(title="进行中", requested_chapters=1)
        running["project_id"] = "project-active-check"
        running["status"] = "running"
        running["current_stage"] = "writing_chapter"
        api_module._persist_generation_task("task-active-check-1", running)

        response = api_module.active_generation_task_check("project-active-check")

        self.assertTrue(response.has_active_generation_task)
        self.assertFalse(response.safe_to_restart)
        self.assertEqual(response.active_task_ids, ["task-active-check-1"])

    def test_active_generation_check_finds_old_active_task_beyond_list_limit(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            session.add(
                GenerationTask(
                    id="task-old-running",
                    project_id="project-active-check-old",
                    task_kind="generation",
                    status="running",
                    current_stage="writing_chapter",
                    created_at=now - timedelta(days=2),
                    updated_at=now - timedelta(days=2),
                )
            )
            for index in range(105):
                session.add(
                    GenerationTask(
                        id=f"task-new-complete-{index}",
                        project_id=f"project-complete-{index}",
                        task_kind="generation",
                        status="completed",
                        current_stage="completed",
                        created_at=now + timedelta(seconds=index),
                        updated_at=now + timedelta(seconds=index),
                    )
                )
            session.commit()

        response = api_module.active_generation_task_check("project-active-check-old")

        self.assertTrue(response.has_active_generation_task)
        self.assertFalse(response.safe_to_restart)
        self.assertEqual(response.active_task_ids, ["task-old-running"])

    def test_database_rejects_two_active_generation_tasks_for_same_project(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            session.add(
                GenerationTask(
                    id="task-unique-running-1",
                    project_id="project-unique-active",
                    task_kind="generation",
                    status="running",
                    current_stage="writing_chapter",
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()
            session.add(
                GenerationTask(
                    id="task-unique-running-2",
                    project_id="project-unique-active",
                    task_kind="generation",
                    status="starting",
                    current_stage="queued",
                    created_at=now + timedelta(seconds=1),
                    updated_at=now + timedelta(seconds=1),
                )
            )

            with self.assertRaises(Exception):
                session.commit()

    def test_update_task_keeps_cache_when_db_write_is_locked(self) -> None:
        task = api_module._create_task_record(title="锁冲突测试", requested_chapters=1)
        api_module._persist_generation_task("task-lock-1", task)

        with patch.object(api_module, "_run_generation_task_db_write", return_value=False):
            api_module._update_task(
                "task-lock-1",
                status="running",
                current_stage="writing_chapter",
                current_chapter=1,
                message="继续推进中",
            )

        with api_module._tasks_lock:
            cached = dict(api_module._tasks["task-lock-1"])
        self.assertEqual(cached["status"], "running")
        self.assertEqual(cached["current_stage"], "writing_chapter")
        self.assertEqual(cached["current_chapter"], 1)

        loaded = api_module._get_generation_task_or_404("task-lock-1")
        self.assertEqual(loaded["status"], "running")
        self.assertEqual(loaded["current_stage"], "writing_chapter")
        self.assertEqual(loaded["current_chapter"], 1)

    def test_task_read_paths_do_not_trigger_db_prune_writes(self) -> None:
        task = api_module._create_task_record(title="轮询读路径测试", requested_chapters=1)
        task["status"] = "running"
        task["current_stage"] = "resolving_arc_envelope"
        api_module._persist_generation_task("task-read-no-prune-1", task)

        with patch.object(api_module, "_prune_generation_tasks_db", side_effect=AssertionError("read path pruned db")):
            loaded = api_module._get_generation_task_or_404("task-read-no-prune-1")
            listed = api_module._list_generation_tasks(10)

        self.assertEqual(loaded["current_stage"], "resolving_arc_envelope")
        self.assertIn("task-read-no-prune-1", [task_id for task_id, _ in listed])

    def test_project_active_generation_detection_prefers_terminal_cache_over_stale_db_row(self) -> None:
        task = api_module._create_task_record(title="缓存终态测试", requested_chapters=1)
        task["project_id"] = "project-stale-active"
        task["status"] = "running"
        task["current_stage"] = "running_provisional_preview"
        api_module._persist_generation_task("task-stale-active-1", task)

        cached = dict(task)
        cached["status"] = "failed"
        cached["current_stage"] = "failed"
        api_module._sync_task_cache("task-stale-active-1", cached)

        with self.session_factory() as session:
            self.assertFalse(
                api_module._project_has_active_generation_task(
                    "project-stale-active",
                    session=session,
                )
            )

    def test_terminal_status_forces_terminal_stage(self) -> None:
        task = api_module._create_task_record(title="终态一致性测试", requested_chapters=1)
        task["status"] = "running"
        task["current_stage"] = "applying_canon"
        task["current_chapter"] = 1
        api_module._persist_generation_task("task-terminal-1", task)

        api_module._update_task("task-terminal-1", status="failed", message="生成失败")

        loaded = api_module._get_generation_task_or_404("task-terminal-1")
        self.assertEqual(loaded["status"], "failed")
        self.assertEqual(loaded["current_stage"], "failed")
        self.assertEqual(loaded["stage_history"][-1]["stage"], "failed")

    def test_provisional_preview_history_is_backfilled_from_execution(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            project = Project(
                id="project-provisional-1",
                title="预演测试",
                premise="测试",
                genre="玄幻",
                created_at=now,
                updated_at=now,
            )
            arc = ArcPlanVersion(
                id="arc-provisional-1",
                project_id=project.id,
                version=1,
                arc_synopsis="测试 arc",
                status="active",
                created_at=now,
            )
            session.add(project)
            session.commit()
            session.add(arc)
            session.commit()
            session.add(
                ProvisionalBandExecution(
                    id="preview-provisional-1",
                    project_id=project.id,
                    arc_id=arc.id,
                    band_id="band:1:4",
                    chapter_numbers_json="[1, 2, 3, 4]",
                    aggregate_verdict="pass",
                    failure_count=0,
                    created_at=now + timedelta(seconds=10),
                )
            )
            session.commit()

        task = api_module._create_task_record(title="预演补偿测试", requested_chapters=4)
        task["project_id"] = "project-provisional-1"
        task["status"] = "failed"
        task["current_stage"] = "failed"
        task["created_at"] = now
        task["updated_at"] = now + timedelta(seconds=20)
        api_module._persist_generation_task("task-provisional-1", task)

        loaded = api_module._get_generation_task_or_404("task-provisional-1")
        stages = [entry["stage"] for entry in loaded["stage_history"]]
        self.assertIn("running_provisional_preview", stages)


if __name__ == "__main__":
    unittest.main()
