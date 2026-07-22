from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import delete

from forwin.config import InfrastructureConfig
from forwin.generation.task_repository import GenerationTaskRepository
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.draft import ChapterDraft
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from forwin.models.task import GenerationTask
from forwin.runtime.policy import RuntimePolicy
from forwin.runtime.policy_store import ProjectPolicyStore
from tests.postgres import postgres_test_url
from tests.http_runtime_harness import HttpRuntimeHarness


api_module: HttpRuntimeHarness


class GenerationTaskPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        global api_module
        self.tmpdir = TemporaryDirectory()
        self.engine = get_engine(postgres_test_url("generation-tasks"))
        init_db(self.engine)
        self.session_factory = get_session_factory(self.engine)
        api_module = HttpRuntimeHarness(
            session_factory=self.session_factory,
            config=InfrastructureConfig(
                database_url=postgres_test_url("generation-tasks-config")
            ),
            engine=self.engine,
        )

    def tearDown(self) -> None:
        self.engine.dispose()
        self.tmpdir.cleanup()

    def _replace_task_before_row_lock(
        self,
        *,
        task_id: str,
        status: str,
        current_stage: str,
    ):
        original = GenerationTaskRepository.get_for_update
        replaced = False

        def get_for_update(repository, requested_task_id):
            nonlocal replaced
            if requested_task_id == task_id and not replaced:
                replaced = True
                with self.session_factory.begin() as session:
                    row = session.get(GenerationTask, task_id)
                    self.assertIsNotNone(row)
                    assert row is not None
                    row.status = status
                    row.current_stage = current_stage
                    row.finished_at = (
                        datetime.now(timezone.utc)
                        if status == "completed"
                        else None
                    )
            return original(repository, requested_task_id)

        return patch.object(
            GenerationTaskRepository,
            "get_for_update",
            get_for_update,
        )

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

    def test_old_terminal_task_survives_repeated_list_polling(self) -> None:
        old = datetime.now(timezone.utc) - timedelta(days=2)
        with self.session_factory.begin() as session:
            session.add(
                GenerationTask(
                    id="task-old-terminal",
                    task_kind="generation",
                    status="completed",
                    current_stage="completed",
                    title="durable task history",
                    created_at=old,
                    updated_at=old,
                )
            )

        for _ in range(3):
            listed = api_module._list_generation_tasks(10)
            self.assertIn("task-old-terminal", [task_id for task_id, _ in listed])

        loaded = api_module._get_generation_task_or_404("task-old-terminal")
        self.assertEqual(loaded["status"], "completed")
        with self.session_factory() as session:
            self.assertIsNotNone(session.get(GenerationTask, "task-old-terminal"))

    def test_task_record_defaults_to_queued_for_worker_cutover(self) -> None:
        task = api_module._create_task_record(title="Queue 默认", requested_chapters=1)

        self.assertEqual(task["status"], "queued")
        self.assertEqual(task["current_stage"], "queued")
        self.assertEqual(task["execution_payload"], {})

    def test_generation_task_execution_payload_round_trips_to_database(self) -> None:
        task = api_module._create_task_record(title="payload", requested_chapters=1)
        task["execution_payload"] = {
            "mode": "continue",
            "root_event_id": "event-1",
            "auto_continue": True,
            "policy_version": 4,
            "policy_snapshot": RuntimePolicy.for_profile("pulp").model_dump(
                mode="json"
            ),
        }

        api_module._persist_generation_task("task-payload-1", task)
        loaded = api_module._get_generation_task_or_404("task-payload-1")

        self.assertEqual(loaded["execution_payload"]["mode"], "continue")
        self.assertEqual(loaded["execution_payload"]["root_event_id"], "event-1")
        self.assertEqual(loaded["execution_payload"]["policy_version"], 4)
        self.assertEqual(
            loaded["execution_payload"]["policy_snapshot"]["quality_profile"],
            "pulp",
        )

    def test_create_continue_generation_task_enqueues_without_starting_thread(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory.begin() as session:
            project = Project(
                id="project-enqueue-only",
                title="继续入队测试",
                premise="测试",
                genre="玄幻",
                creation_status="writing",
                created_at=now,
                updated_at=now,
            )
            session.add(project)
            session.flush()
            ProjectPolicyStore(session).initialize(
                project,
                RuntimePolicy.for_profile("standard"),
            )

        task_id = api_module._create_continue_generation_task(
            project_id="project-enqueue-only",
            requested_chapters=3,
            max_chapters=3,
            auto_continue=False,
            run_until_chapter=8,
            title="继续入队测试",
            subtitle="继续生成",
        )
        task = api_module._get_generation_task_or_404(task_id)
        self.assertEqual(task["status"], "queued")
        self.assertEqual(task["project_id"], "project-enqueue-only")
        self.assertEqual(task["execution_payload"]["mode"], "continue")
        self.assertFalse(task["execution_payload"]["auto_continue"])
        self.assertEqual(task["execution_payload"]["run_until_chapter"], 8)
        self.assertEqual(task["execution_payload"]["max_chapters"], 3)

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

    def test_running_progress_update_refreshes_generation_task_lease(self) -> None:
        task = api_module._create_task_record(title="lease progress", requested_chapters=1)
        original_heartbeat = datetime.now(timezone.utc) - timedelta(minutes=10)
        original_lease = original_heartbeat + timedelta(minutes=5)
        task["status"] = "running"
        task["current_stage"] = "writing_chapter"
        task["lease_owner"] = "worker-progress"
        task["heartbeat_at"] = original_heartbeat
        task["lease_expires_at"] = original_lease
        api_module._persist_generation_task("task-lease-progress", task)

        api_module._update_task(
            "task-lease-progress",
            current_stage="continuity_review",
            current_chapter=1,
        )

        loaded = api_module._get_generation_task_or_404("task-lease-progress")
        loaded_heartbeat = api_module._coerce_task_datetime(loaded["heartbeat_at"])
        loaded_lease = api_module._coerce_task_datetime(loaded["lease_expires_at"])
        self.assertEqual(loaded["status"], "running")
        self.assertEqual(loaded["current_stage"], "continuity_review")
        self.assertGreater(loaded_heartbeat, original_heartbeat)
        self.assertGreater(loaded_lease, original_lease)
        self.assertGreater(
            (loaded_lease - loaded_heartbeat).total_seconds(),
            290,
        )
        with self.session_factory() as session:
            row = session.get(GenerationTask, "task-lease-progress")
            self.assertIsNotNone(row)
            assert row is not None
            self.assertGreater(api_module._coerce_task_datetime(row.heartbeat_at), original_heartbeat)
            self.assertGreater(api_module._coerce_task_datetime(row.lease_expires_at), original_lease)

    def test_low_level_pause_request_finishes_queued_task(self) -> None:
        task = api_module._create_task_record(title="queued pause", requested_chapters=1)
        task["project_id"] = "project-queued-pause"
        api_module._persist_generation_task("task-queued-pause", task)

        loaded = api_module._get_generation_task_or_404("task-queued-pause")
        self.assertTrue(api_module._task_is_pausable(loaded))

        api_module._update_task(
            "task-queued-pause",
            pause_requested=True,
            message="任务尚未开始，已安全暂停。",
        )
        paused = api_module._get_generation_task_or_404("task-queued-pause")

        self.assertTrue(paused["pause_requested"])
        self.assertEqual(paused["status"], "paused")
        self.assertEqual(paused["current_stage"], "paused")

    def test_pause_endpoint_finishes_unclaimed_task_immediately(self) -> None:
        task = api_module._create_task_record(
            title="queued endpoint pause",
            requested_chapters=1,
        )
        api_module._persist_generation_task("task-queued-endpoint-pause", task)

        response = api_module.pause_task("task-queued-endpoint-pause")

        self.assertEqual(response.status, "paused")
        paused = api_module._get_generation_task_or_404(
            "task-queued-endpoint-pause"
        )
        self.assertEqual(paused["current_stage"], "paused")
        self.assertTrue(paused["pause_requested"])

    def test_terminate_endpoint_finishes_unclaimed_task_immediately(self) -> None:
        task = api_module._create_task_record(
            title="queued endpoint terminate",
            requested_chapters=1,
        )
        api_module._persist_generation_task("task-queued-endpoint-terminate", task)

        response = api_module.terminate_task("task-queued-endpoint-terminate")

        self.assertEqual(response.status, "cancelled")
        cancelled = api_module._get_generation_task_or_404(
            "task-queued-endpoint-terminate"
        )
        self.assertEqual(cancelled["current_stage"], "cancelled")
        self.assertTrue(cancelled["cancel_requested"])

    def test_pause_rejects_task_completed_before_locked_mutation(self) -> None:
        task = api_module._create_task_record(
            title="pause lock race",
            requested_chapters=1,
        )
        task["status"] = "running"
        task["current_stage"] = "writing_chapter"
        api_module._persist_generation_task("task-pause-lock-race", task)

        with self._replace_task_before_row_lock(
            task_id="task-pause-lock-race",
            status="completed",
            current_stage="completed",
        ):
            with self.assertRaises(HTTPException) as raised:
                api_module.pause_task("task-pause-lock-race")

        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(raised.exception.detail, "当前任务状态不支持安全暂停")
        with self.session_factory() as session:
            row = session.get(GenerationTask, "task-pause-lock-race")
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row.status, "completed")
            self.assertEqual(row.current_stage, "completed")
            self.assertFalse(row.pause_requested)

    def test_terminate_rejects_task_completed_before_locked_mutation(self) -> None:
        task = api_module._create_task_record(
            title="terminate lock race",
            requested_chapters=1,
        )
        task["status"] = "running"
        task["current_stage"] = "writing_chapter"
        api_module._persist_generation_task("task-terminate-lock-race", task)

        with self._replace_task_before_row_lock(
            task_id="task-terminate-lock-race",
            status="completed",
            current_stage="completed",
        ):
            with self.assertRaises(HTTPException) as raised:
                api_module.terminate_task("task-terminate-lock-race")

        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(raised.exception.detail, "当前任务状态不支持终止")
        with self.session_factory() as session:
            row = session.get(GenerationTask, "task-terminate-lock-race")
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row.status, "completed")
            self.assertEqual(row.current_stage, "completed")
            self.assertFalse(row.cancel_requested)

    def test_delete_validates_terminal_status_from_locked_row(self) -> None:
        task = api_module._create_task_record(
            title="delete lock race",
            requested_chapters=1,
        )
        task["status"] = "completed"
        task["current_stage"] = "completed"
        api_module._persist_generation_task("task-delete-lock-race", task)

        with self._replace_task_before_row_lock(
            task_id="task-delete-lock-race",
            status="running",
            current_stage="writing_chapter",
        ):
            with self.assertRaises(HTTPException) as raised:
                api_module.delete_task("task-delete-lock-race")

        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(raised.exception.detail, "只有终态任务可以删除")
        with self.session_factory() as session:
            row = session.get(GenerationTask, "task-delete-lock-race")
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row.status, "running")
            self.assertIsNone(row.deleted_at)

    def test_pause_audit_failure_rolls_back_task_mutation(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory.begin() as session:
            session.add(
                Project(
                    id="project-pause-audit-rollback",
                    title="pause audit rollback",
                    premise="test",
                    genre="test",
                    creation_status="writing",
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                GenerationTask(
                    id="task-pause-audit-rollback",
                    project_id="project-pause-audit-rollback",
                    task_kind="generation",
                    status="running",
                    current_stage="writing_chapter",
                    created_at=now,
                    updated_at=now,
                )
            )

        with patch(
            "forwin.application.project_control.support.log_decision_event",
            side_effect=RuntimeError("audit write failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "audit write failed"):
                api_module.pause_task("task-pause-audit-rollback")

        with self.session_factory() as session:
            row = session.get(GenerationTask, "task-pause-audit-rollback")
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row.status, "running")
            self.assertEqual(row.current_stage, "writing_chapter")
            self.assertFalse(row.pause_requested)

    def test_progress_update_cannot_expand_requested_chapters_contract(self) -> None:
        task = api_module._create_task_record(title="继续生成计数契约", requested_chapters=2)
        task["project_id"] = "project-progress-contract"
        task["status"] = "running"
        task["current_stage"] = "queued"
        api_module._persist_generation_task("task-progress-contract", task)

        api_module._update_task(
            "task-progress-contract",
            current_stage="resolving_arc_envelope",
            requested_chapters=12,
            current_chapter=1,
        )

        loaded = api_module._get_generation_task_or_404("task-progress-contract")
        self.assertEqual(loaded["requested_chapters"], 2)
        self.assertEqual(loaded["current_stage"], "resolving_arc_envelope")
        with self.session_factory() as session:
            row = session.get(GenerationTask, "task-progress-contract")
            self.assertEqual(row.requested_chapters, 2)

    def test_checkpoint_pause_does_not_become_user_pause_request(self) -> None:
        task = api_module._create_task_record(title="检查点暂停语义", requested_chapters=2)
        task["status"] = "running"
        task["current_stage"] = "paused_for_review"
        task["paused_chapters"] = [2]
        api_module._persist_generation_task("task-checkpoint-pause-1", task)

        api_module._update_task(
            "task-checkpoint-pause-1",
            status="paused",
            current_stage="paused",
            paused_chapters=[2],
            message="进入 band checkpoint，等待处理。",
        )

        paused = api_module._get_generation_task_or_404("task-checkpoint-pause-1")
        self.assertEqual(paused["status"], "paused")
        self.assertEqual(paused["current_stage"], "paused")
        self.assertFalse(paused["pause_requested"])
        self.assertEqual(paused["paused_chapters"], [2])

    def test_continue_generation_api_rejects_pending_review_chapters(self) -> None:
        old_config = api_module._config
        api_module._config = InfrastructureConfig(
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
                    creation_status="writing",
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
                session.flush()
                ProjectPolicyStore(session).initialize(
                    project,
                    RuntimePolicy.for_profile("standard"),
                )
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
                session.flush()
                session.add(
                    ChapterDraft(
                        id="draft-review-block-1",
                        chapter_plan_id="plan-review-block-1",
                        version=1,
                        body_text="待审草稿",
                        summary="待审",
                        char_count=4,
                    )
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

    def test_active_generation_check_reports_only_persisted_nonterminal_rows(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory.begin() as session:
            session.add_all(
                [
                    GenerationTask(
                        id="task-active-persisted",
                        project_id="project-db-authority",
                        task_kind="generation",
                        status="running",
                        current_stage="writing_chapter",
                        created_at=now,
                        updated_at=now,
                    ),
                    GenerationTask(
                        id="task-terminal-persisted",
                        project_id="project-db-authority",
                        task_kind="generation",
                        status="completed",
                        current_stage="completed",
                        created_at=now - timedelta(minutes=1),
                        updated_at=now - timedelta(minutes=1),
                    ),
                ]
            )

        response = api_module.active_generation_task_check("project-db-authority")

        self.assertTrue(response.has_active_generation_task)
        self.assertEqual(response.active_task_ids, ["task-active-persisted"])

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

    def test_active_generation_check_keeps_nonterminal_pause_request_active(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            session.add(
                GenerationTask(
                    id="task-paused-queued-active-check",
                    project_id="project-paused-queued-active-check",
                    task_kind="generation",
                    status="queued",
                    current_stage="queued",
                    pause_requested=True,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

        response = api_module.active_generation_task_check("project-paused-queued-active-check")

        self.assertTrue(response.has_active_generation_task)
        self.assertFalse(response.safe_to_restart)
        self.assertEqual(
            response.active_task_ids,
            ["task-paused-queued-active-check"],
        )

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

    def test_active_generation_check_does_not_report_terminal_db_task(self) -> None:
        running = api_module._create_task_record(title="终态任务", requested_chapters=1)
        running["project_id"] = "project-terminal-task"
        running["status"] = "running"
        running["current_stage"] = "repair_review"
        api_module._persist_generation_task("task-terminal-db-1", running)

        with self.session_factory.begin() as session:
            row = session.get(GenerationTask, "task-terminal-db-1")
            self.assertIsNotNone(row)
            row.status = "needs_review"
            row.current_stage = "paused_for_review"

        response = api_module.active_generation_task_check("project-terminal-task")

        self.assertFalse(response.has_active_generation_task)
        self.assertTrue(response.safe_to_restart)
        self.assertEqual(response.active_task_ids, [])

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

    def test_update_task_does_not_recreate_row_deleted_before_locked_write(self) -> None:
        task = api_module._create_task_record(title="missing update", requested_chapters=1)
        api_module._persist_generation_task("task-deleted-before-update", task)

        from forwin.http.tasks import _run_generation_task_db_write

        def delete_before_write(operation, **kwargs):
            with self.session_factory.begin() as session:
                session.execute(
                    delete(GenerationTask).where(
                        GenerationTask.id == "task-deleted-before-update"
                    )
                )
            return _run_generation_task_db_write(operation, **kwargs)

        with patch(
            "forwin.http.tasks._run_generation_task_db_write",
            side_effect=delete_before_write,
        ):
            api_module._update_task(
                "task-deleted-before-update",
                status="running",
                current_stage="writing_chapter",
            )

        with self.session_factory() as session:
            self.assertIsNone(
                session.get(GenerationTask, "task-deleted-before-update")
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

if __name__ == "__main__":
    unittest.main()
