from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

import forwin.api as api_module
from forwin import api_project_ops
from forwin.api_schemas import (
    ChapterReviewApproveRequest,
    GenerateRequest,
    ProjectBulkDeleteRequest,
    ProjectCreateRequest,
    ProjectGovernanceUpdateRequest,
)
from forwin.config import Config
from forwin.models.base import get_engine, get_session_factory, init_db, new_id
from forwin.models.governance import BandCheckpoint
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from forwin.models.publisher import PublisherUploadJob
from forwin.models.task import GenerationTask


class ProjectOperationGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = TemporaryDirectory()
        self.engine = get_engine(postgres_test_url("operation-guards"))
        init_db(self.engine)
        self.session_factory = get_session_factory(self.engine)

        self.old_session_factory = api_module._SessionFactory
        self.old_config = api_module._config
        self.old_runtime_settings = api_module._runtime_settings
        self.old_orchestrator = api_module._orchestrator

        api_module._SessionFactory = self.session_factory
        api_module._config = Config(
            database_url=postgres_test_url("operation-guards"),
            minimax_api_key="saved-key",
            minimax_base_url="https://api.minimaxi.com/v1",
            minimax_model="MiniMax-M2.7",
        )
        api_module._runtime_settings = None
        api_module._orchestrator = None

    def tearDown(self) -> None:
        api_module._SessionFactory = self.old_session_factory
        api_module._config = self.old_config
        api_module._runtime_settings = self.old_runtime_settings
        api_module._orchestrator = self.old_orchestrator
        self.engine.dispose()
        self.tmpdir.cleanup()

    def _create_project(self, *, project_id: str | None = None) -> Project:
        with self.session_factory() as session:
            project = Project(
                id=project_id or new_id(),
                title="测试书",
                premise="测试 premise",
                genre="玄幻",
                setting_summary="测试设定",
            )
            session.add(project)
            session.commit()
            return project

    def test_generate_rejects_existing_project_with_active_generation_task(self) -> None:
        project = self._create_project(project_id="proj-active-generate")
        with self.session_factory() as session:
            session.add(
                GenerationTask(
                    id="task-active-generate",
                    project_id=project.id,
                    task_kind="generation",
                    status="running",
                    current_stage="writing_chapter",
                    message="still running",
                )
            )
            session.commit()

        with self.assertRaises(HTTPException) as ctx:
            api_module.generate(
                GenerateRequest(
                    project_id=project.id,
                    premise="测试 premise",
                    genre="玄幻",
                    num_chapters=1,
                    api_key="sk-inline",
                )
            )

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("运行中的生成任务", str(ctx.exception.detail))

    def test_approve_review_rejects_continue_when_active_generation_task_exists(self) -> None:
        project = self._create_project(project_id="proj-active-continue")
        with self.session_factory() as session:
            arc = ArcPlanVersion(
                id="arc-active-continue",
                project_id=project.id,
                arc_synopsis="测试弧线",
                status="active",
            )
            session.add(
                ChapterPlan(
                    id="plan-active-continue",
                    project_id=project.id,
                    arc_plan_id=arc.id,
                    chapter_number=1,
                    title="第一章",
                    status="accepted",
                )
            )
            session.add(arc)
            session.add(
                GenerationTask(
                    id="task-active-continue",
                    project_id=project.id,
                    task_kind="generation",
                    status="running",
                    current_stage="writing_chapter",
                    message="still running",
                )
            )
            session.commit()

        accept_calls = []

        def accept_review(*_args, **_kwargs):
            accept_calls.append("called")
            return {
                "message": "accepted",
                "frozen_artifact": "",
            }

        api_module._orchestrator = SimpleNamespace(
            accept_review=accept_review
        )

        with self.assertRaises(HTTPException) as ctx:
            api_module.approve_chapter_review(
                project.id,
                1,
                ChapterReviewApproveRequest(continue_generation=True, reason="guard regression"),
            )

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("运行中的生成任务", str(ctx.exception.detail))
        self.assertEqual(accept_calls, [])

    def test_start_writing_rolls_back_status_when_task_creation_fails(self) -> None:
        project = self._create_project(project_id="proj-start-writing-task-fail")
        with self.session_factory() as session:
            project_row = session.get(Project, project.id)
            project_row.creation_status = "genesis_ready"
            session.commit()

        class FakeGenesisService:
            def materialize_book_arcs(self, *, session, project, **_kwargs):
                arc = ArcPlanVersion(
                    id="arc-start-writing-task-fail",
                    project_id=project.id,
                    version=1,
                    arc_number=1,
                    arc_synopsis="测试弧线",
                    status="active",
                )
                session.add(arc)
                session.flush()
                return [arc]

            def materialize_arc_chapter_plans(self, *, session, project, **_kwargs):
                session.add(
                    ChapterPlan(
                        id="plan-start-writing-task-fail",
                        project_id=project.id,
                        arc_plan_id="arc-start-writing-task-fail",
                        chapter_number=1,
                        title="第一章",
                        status="planned",
                    )
                )
                session.flush()

            def load_pack(self, _revision):
                return {"world": {"world_bible": {"overview": "测试设定"}}}

        def fail_task_creation(**_kwargs):
            raise RuntimeError("task create failed")

        with (
            patch.object(api_project_ops, "_ensure_initial_book_map_from_genesis", lambda **_kwargs: None),
            patch.object(
                api_project_ops,
                "WorldModelCompiler",
                lambda _session: SimpleNamespace(bootstrap_from_genesis=lambda _project_id: None),
            ),
            self.assertRaises(RuntimeError),
        ):
            api_project_ops.start_project_writing(
                project.id,
                get_session=self.session_factory,
                config=api_module._config,
                saved_runtime_config_or_default=lambda: Config(
                    database_url=api_module._config.database_url,
                    minimax_api_key="saved-key",
                ),
                build_genesis_service=lambda _runtime_config: FakeGenesisService(),
                close_genesis_service=lambda _service: None,
                require_genesis_project=lambda _project: None,
                active_genesis_revision=lambda _session, _project: SimpleNamespace(id="revision-start-writing"),
                project_has_active_generation_task=lambda _project_id, *, session=None: False,
                generation_task_conflict_message=lambda _project_id: "conflict",
                create_continue_generation_task=fail_task_creation,
            )

        with self.session_factory() as session:
            project_row = session.get(Project, project.id)
            self.assertEqual(project_row.creation_status, "genesis_ready")

    def test_delete_project_rejects_running_generation_task(self) -> None:
        project = self._create_project(project_id="proj-delete-generation")
        with self.session_factory() as session:
            session.add(
                GenerationTask(
                    id="task-delete-generation",
                    project_id=project.id,
                    task_kind="generation",
                    status="running",
                    current_stage="writing_chapter",
                    message="still running",
                )
            )
            session.commit()

        with self.assertRaises(HTTPException) as ctx:
            api_module.delete_project(project.id)

        self.assertEqual(ctx.exception.status_code, 409)
        with self.session_factory() as session:
            self.assertIsNotNone(session.get(Project, project.id))

    def test_delete_project_rejects_running_upload_job(self) -> None:
        project = self._create_project(project_id="proj-delete-upload")
        with self.session_factory() as session:
            session.add(
                PublisherUploadJob(
                    id="upload-delete-running",
                    project_id=project.id,
                    platform_id="fanqie",
                    status="running",
                    book_name="测试书",
                    chapter_title="第一章",
                    body_text="正文",
                    publish=False,
                    result_message="running",
                )
            )
            session.commit()

        with self.assertRaises(HTTPException) as ctx:
            api_module.delete_project(project.id)

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("发布任务", str(ctx.exception.detail))

    def test_bulk_delete_projects_skips_projects_with_active_operations(self) -> None:
        blocked = self._create_project(project_id="proj-blocked")
        deletable = self._create_project(project_id="proj-deletable")
        with self.session_factory() as session:
            session.add(
                GenerationTask(
                    id="task-bulk-blocked",
                    project_id=blocked.id,
                    task_kind="generation",
                    status="running",
                    current_stage="writing_chapter",
                    message="still running",
                )
            )
            session.commit()

        response = api_module.bulk_delete_projects(
            ProjectBulkDeleteRequest(project_ids=[blocked.id, deletable.id])
        )

        self.assertEqual(response.deleted_count, 1)
        self.assertEqual(response.skipped_count, 1)
        self.assertEqual(response.message, "已删除 1 本书，跳过 1 本。")
        self.assertEqual(response.deleted_ids, [deletable.id])
        self.assertEqual(response.skipped_ids, [blocked.id])
        with self.session_factory() as session:
            self.assertIsNotNone(session.get(Project, blocked.id))
            self.assertIsNone(session.get(Project, deletable.id))

    def test_create_project_defaults_to_strict_governance(self) -> None:
        created = api_module.create_project(
            ProjectCreateRequest(
                title="治理测试书",
                premise="一个关于治理层的 premise",
                genre="玄幻",
            )
        )

        governance = api_module.get_project_governance(created.project_id)
        self.assertEqual(governance.project_id, created.project_id)
        self.assertEqual(governance.message, "已读取项目治理设置。")
        self.assertEqual(governance.governance.progression_mode, "serial_canon_band_guard")
        self.assertTrue(governance.governance.auto_band_checkpoint)
        self.assertTrue(governance.governance.manual_checkpoints_enabled)
        self.assertTrue(governance.governance.future_constraints_enabled)

    def test_continue_generation_rejects_failed_band_checkpoint(self) -> None:
        project = self._create_project(project_id="proj-band-checkpoint")
        with self.session_factory() as session:
            arc = ArcPlanVersion(
                id="arc-band-checkpoint",
                project_id=project.id,
                arc_synopsis="测试弧线",
                status="active",
            )
            session.add(arc)
            session.flush()
            session.add(
                ChapterPlan(
                    id="plan-band-1",
                    project_id=project.id,
                    arc_plan_id=arc.id,
                    chapter_number=1,
                    title="第一章",
                    status="accepted",
                )
            )
            session.add(
                ChapterPlan(
                    id="plan-band-2",
                    project_id=project.id,
                    arc_plan_id=arc.id,
                    chapter_number=2,
                    title="第二章",
                    status="planned",
                )
            )
            session.add(
                BandCheckpoint(
                    id="checkpoint-band-1",
                    project_id=project.id,
                    arc_id=arc.id,
                    band_id="band-1",
                    chapter_start=1,
                    chapter_end=1,
                    boundary_chapter=1,
                    status="fail",
                    summary="上一 band checkpoint 未通过",
                )
            )
            session.commit()

        with self.assertRaises(HTTPException) as ctx:
            api_module.continue_project_generation(project.id)

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("checkpoint", str(ctx.exception.detail))

    def test_update_governance_writes_decision_event(self) -> None:
        project = self._create_project(project_id="proj-governance-event")

        response = api_module.update_project_governance(
            project.id,
            ProjectGovernanceUpdateRequest(
                progression_mode="serial_canon",
                reason="切换到更严格的串行 canon gate",
            ),
        )

        self.assertEqual(response.project_id, project.id)
        self.assertEqual(response.message, "项目治理设置已保存。")
        self.assertEqual(response.governance.progression_mode, "serial_canon")
        events = api_module.list_project_decision_events(project.id)
        self.assertEqual(len(events.items), 1)
        self.assertEqual(events.items[0].event_type, "governance_updated")
        self.assertEqual(events.items[0].scope, "project")
        self.assertEqual(events.items[0].reason, "切换到更严格的串行 canon gate")


if __name__ == "__main__":
    unittest.main()
