from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError

import forwin.api as api_module
from forwin import api_project_ops
from forwin.api_schemas import (
    ChapterReviewApproveRequest,
    ChapterReviewRetryRequest,
    GenerateRequest,
    ProjectBulkDeleteRequest,
    ProjectContinueGenerationRequest,
    ProjectCreateRequest,
    ProjectExtendGenerationRequest,
    ProjectGovernanceUpdateRequest,
    StartWritingRequest,
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

    def test_project_detail_overlays_active_generation_task_stage(self) -> None:
        project = self._create_project(project_id="proj-active-detail")
        with self.session_factory() as session:
            arc = ArcPlanVersion(
                id="arc-active-detail",
                project_id=project.id,
                arc_synopsis="测试弧线",
                status="active",
            )
            session.add(arc)
            session.add(
                ChapterPlan(
                    id="plan-active-detail-1",
                    project_id=project.id,
                    arc_plan_id=arc.id,
                    chapter_number=1,
                    title="第一章",
                    status="planned",
                )
            )
            session.add(
                GenerationTask(
                    id="task-active-detail",
                    project_id=project.id,
                    task_kind="generation",
                    status="running",
                    requested_chapters=12,
                    current_stage="repairing_chapter",
                    current_chapter=1,
                    message="repairing",
                    pause_requested=True,
                )
            )
            session.commit()

        detail = api_module.get_project(project.id)

        self.assertEqual(detail.latest_stage, "repairing_chapter")
        self.assertEqual(detail.generation_control.current_stage, "repairing_chapter")
        self.assertEqual(detail.generation_control.current_chapter, 1)
        self.assertTrue(detail.generation_control.pause_requested)
        self.assertFalse(detail.generation_control.can_resume)

    def test_extend_generation_appends_future_plans_after_completed_workset(self) -> None:
        project = self._create_project(project_id="proj-extend-after-24")
        with self.session_factory() as session:
            project = session.get(Project, project.id)
            project.creation_status = "writing"
            project.target_total_chapters = 24
            arc = ArcPlanVersion(
                id="arc-extend-1",
                project_id=project.id,
                arc_synopsis="已完成弧线",
                status="active",
                arc_number=1,
                chapter_start=1,
                chapter_end=24,
                planned_target_size=24,
            )
            session.add(arc)
            for chapter_number in range(1, 25):
                session.add(
                    ChapterPlan(
                        id=f"plan-extend-{chapter_number}",
                        project_id=project.id,
                        arc_plan_id=arc.id,
                        chapter_number=chapter_number,
                        title=f"第{chapter_number}章",
                        status="accepted",
                    )
                )
            session.commit()

        detail = api_project_ops.extend_project_generation(
            project.id,
            ProjectExtendGenerationRequest(
                additional_chapters=12,
                continuity_guard="第24章 canon 已确认记忆重置剩余79分钟，不要回退成几天或数日。",
                reason="operator wants to continue the interrupted sixty-chapter run",
            ),
            get_session=self.session_factory,
            display_datetime=lambda value: str(value),
            project_has_active_generation_task=lambda *_args, **_kwargs: False,
            generation_task_conflict_message=lambda project_id: f"active {project_id}",
        )

        self.assertEqual(detail.target_total_chapters, 36)
        self.assertEqual(detail.generation_control.next_chapter, 25)
        self.assertEqual(detail.generation_control.planned_chapters, list(range(25, 37)))

        with self.session_factory() as session:
            plans = session.query(ChapterPlan).filter(
                ChapterPlan.project_id == project.id,
                ChapterPlan.chapter_number >= 25,
            ).order_by(ChapterPlan.chapter_number).all()
            arcs = session.query(ArcPlanVersion).filter(
                ArcPlanVersion.project_id == project.id,
                ArcPlanVersion.arc_number == 2,
            ).all()

        self.assertEqual([plan.chapter_number for plan in plans], list(range(25, 37)))
        self.assertEqual([plan.status for plan in plans], ["planned"] * 12)
        self.assertEqual(len(arcs), 1)
        self.assertEqual(arcs[0].chapter_start, 25)
        self.assertEqual(arcs[0].chapter_end, 36)
        first_future_contract = " ".join(
            [
                plans[0].one_line,
                plans[0].goals_json,
                plans[0].experience_plan_json,
            ]
        )
        self.assertIn("79分钟", first_future_contract)
        self.assertIn("不要回退成几天", first_future_contract)

    def test_extend_generation_rejects_active_generation_task(self) -> None:
        project = self._create_project(project_id="proj-extend-active")

        with self.assertRaises(HTTPException) as ctx:
            api_project_ops.extend_project_generation(
                project.id,
                ProjectExtendGenerationRequest(additional_chapters=12),
                get_session=self.session_factory,
                display_datetime=lambda value: str(value),
                project_has_active_generation_task=lambda *_args, **_kwargs: True,
                generation_task_conflict_message=lambda project_id: f"active {project_id}",
            )

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("active proj-extend-active", str(ctx.exception.detail))

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

    def test_approve_review_does_not_continue_when_canon_gate_keeps_review_pending(self) -> None:
        project = self._create_project(project_id="proj-review-gate-block")
        with self.session_factory() as session:
            arc = ArcPlanVersion(
                id="arc-review-gate-block",
                project_id=project.id,
                arc_synopsis="测试弧线",
                status="active",
            )
            session.add(arc)
            session.add(
                ChapterPlan(
                    id="plan-review-gate-block",
                    project_id=project.id,
                    arc_plan_id=arc.id,
                    chapter_number=1,
                    title="第一章",
                    status="needs_review",
                )
            )
            session.commit()

        def accept_review(*_args, **_kwargs):
            return {
                "status": "needs_review",
                "message": "第1章 canon gate 阻止接受，已转为 needs_review。",
                "frozen_artifact": "artifact.json",
            }

        api_module._orchestrator = SimpleNamespace(accept_review=accept_review)

        payload = api_module.approve_chapter_review(
            project.id,
            1,
            ChapterReviewApproveRequest(continue_generation=True, reason="guard regression"),
        )

        self.assertEqual(payload.status, "needs_review")
        self.assertEqual(payload.task_id, "")
        self.assertIn("未启动后续章节", payload.message)
        with self.session_factory() as session:
            task_count = session.query(GenerationTask).filter(
                GenerationTask.project_id == project.id
            ).count()
        self.assertEqual(task_count, 0)

    def test_approve_review_continue_uses_remaining_active_workset_count(self) -> None:
        project = self._create_project(project_id="proj-approve-workset-count")
        with self.session_factory() as session:
            project_row = session.get(Project, project.id)
            project_row.creation_status = "writing"
            arc = ArcPlanVersion(
                id="arc-approve-workset-count",
                project_id=project.id,
                arc_synopsis="测试弧线",
                status="active",
                arc_number=1,
                chapter_start=1,
                chapter_end=4,
            )
            session.add(arc)
            session.flush()
            for chapter_number, status in [
                (1, "accepted"),
                (2, "needs_review"),
                (3, "planned"),
                (4, "planned"),
            ]:
                session.add(
                    ChapterPlan(
                        id=f"plan-approve-workset-{chapter_number}",
                        project_id=project.id,
                        arc_plan_id=arc.id,
                        chapter_number=chapter_number,
                        title=f"第{chapter_number}章",
                        status=status,
                    )
                )
            session.commit()

        captured: dict[str, object] = {}

        def accept_review(_project_id, chapter_number, **_kwargs):
            with self.session_factory() as session:
                plan = session.query(ChapterPlan).filter(
                    ChapterPlan.project_id == _project_id,
                    ChapterPlan.chapter_number == chapter_number,
                ).one()
                plan.status = "accepted"
                session.commit()
            return {"status": "accepted", "message": "accepted", "frozen_artifact": ""}

        def capture_task_creation(**kwargs):
            captured.update(kwargs)
            return "task-approve-workset"

        api_module._orchestrator = SimpleNamespace(accept_review=accept_review)

        with patch("forwin.api._create_continue_generation_task", new=capture_task_creation):
            payload = api_module.approve_chapter_review(
                project.id,
                2,
                ChapterReviewApproveRequest(continue_generation=True, reason="accept and continue"),
            )

        self.assertEqual(payload.task_id, "task-approve-workset")
        self.assertEqual(captured["requested_chapters"], 2)

    def test_retry_chapter_review_resets_needs_review_to_planned(self) -> None:
        project = self._create_project(project_id="proj-review-retry")
        with self.session_factory() as session:
            arc = ArcPlanVersion(
                id="arc-review-retry",
                project_id=project.id,
                arc_synopsis="测试弧线",
                status="active",
            )
            session.add(arc)
            session.add(
                ChapterPlan(
                    id="plan-review-retry",
                    project_id=project.id,
                    arc_plan_id=arc.id,
                    chapter_number=3,
                    title="第三章",
                    status="needs_review",
                    repair_attempt_count=3,
                    residual_review_issues_json='[{"rule_name":"sub_world_unknown_named_entity"}]',
                    canon_risk_level="high",
                )
            )
            session.commit()

        payload = api_module.retry_chapter_review(
            project.id,
            3,
            ChapterReviewRetryRequest(reason="regenerate after root cause fix"),
        )

        self.assertTrue(payload.ok)
        self.assertEqual(payload.status, "planned")
        with self.session_factory() as session:
            plan = session.get(ChapterPlan, "plan-review-retry")
            self.assertEqual(plan.status, "planned")
            self.assertEqual(plan.repair_attempt_count, 0)
            self.assertEqual(plan.residual_review_issues_json, "[]")
            self.assertEqual(plan.canon_risk_level, "")

    def test_retry_chapter_review_continue_uses_retry_workset_count(self) -> None:
        project = self._create_project(project_id="proj-retry-workset-count")
        with self.session_factory() as session:
            project_row = session.get(Project, project.id)
            project_row.creation_status = "writing"
            arc = ArcPlanVersion(
                id="arc-retry-workset-count",
                project_id=project.id,
                arc_synopsis="测试弧线",
                status="active",
                arc_number=1,
                chapter_start=1,
                chapter_end=5,
            )
            session.add(arc)
            session.flush()
            for chapter_number, status in [
                (1, "accepted"),
                (2, "accepted"),
                (3, "needs_review"),
                (4, "planned"),
                (5, "planned"),
            ]:
                session.add(
                    ChapterPlan(
                        id=f"plan-retry-workset-{chapter_number}",
                        project_id=project.id,
                        arc_plan_id=arc.id,
                        chapter_number=chapter_number,
                        title=f"第{chapter_number}章",
                        status=status,
                    )
                )
            session.commit()

        captured: dict[str, object] = {}

        def capture_task_creation(**kwargs):
            captured.update(kwargs)
            return "task-retry-workset"

        with patch("forwin.api._create_continue_generation_task", new=capture_task_creation):
            payload = api_module.retry_chapter_review(
                project.id,
                3,
                ChapterReviewRetryRequest(continue_generation=True, reason="retry and continue"),
            )

        self.assertEqual(payload.task_id, "task-retry-workset")
        self.assertEqual(captured["requested_chapters"], 3)

    def test_retry_chapter_review_can_reset_accepted_when_explicitly_allowed(self) -> None:
        project = self._create_project(project_id="proj-review-retry-accepted")
        with self.session_factory() as session:
            arc = ArcPlanVersion(
                id="arc-review-retry-accepted",
                project_id=project.id,
                arc_synopsis="arc",
                status="active",
            )
            session.add(arc)
            session.add(
                ChapterPlan(
                    id="plan-review-retry-accepted",
                    project_id=project.id,
                    arc_plan_id=arc.id,
                    chapter_number=3,
                    title="第三章",
                    status="accepted",
                    acceptance_mode="normal",
                    repair_attempt_count=3,
                    residual_review_issues_json="[]",
                    canon_risk_level="",
                )
            )
            session.commit()

        payload = api_module.retry_chapter_review(
            project.id,
            3,
            ChapterReviewRetryRequest(
                reason="regenerate accepted chapter after deterministic canon drift fix",
                allow_accepted=True,
            ),
        )

        self.assertTrue(payload.ok)
        with self.session_factory() as session:
            plan = session.get(ChapterPlan, "plan-review-retry-accepted")
            self.assertEqual(plan.status, "planned")
            self.assertEqual(plan.acceptance_mode, "")
            self.assertEqual(plan.repair_attempt_count, 0)

    def test_retry_chapter_review_resets_drafted_candidate_to_planned(self) -> None:
        project = self._create_project(project_id="proj-review-retry-drafted")
        with self.session_factory() as session:
            arc = ArcPlanVersion(
                id="arc-review-retry-drafted",
                project_id=project.id,
                arc_synopsis="arc",
                status="active",
            )
            session.add(arc)
            session.add(
                ChapterPlan(
                    id="plan-review-retry-drafted",
                    project_id=project.id,
                    arc_plan_id=arc.id,
                    chapter_number=4,
                    title="第四章",
                    status="drafted",
                    repair_attempt_count=1,
                    residual_review_issues_json='[{"rule_name":"canon_name_drift"}]',
                    canon_risk_level="medium",
                )
            )
            session.commit()

        payload = api_module.retry_chapter_review(
            project.id,
            4,
            ChapterReviewRetryRequest(reason="regenerate drafted chapter after prior chapter rewrite"),
        )

        self.assertTrue(payload.ok)
        self.assertEqual(payload.status, "planned")
        with self.session_factory() as session:
            plan = session.get(ChapterPlan, "plan-review-retry-drafted")
            self.assertEqual(plan.status, "planned")
            self.assertEqual(plan.repair_attempt_count, 0)
            self.assertEqual(plan.residual_review_issues_json, "[]")
            self.assertEqual(plan.canon_risk_level, "")

    def test_start_writing_rolls_back_status_when_task_creation_fails(self) -> None:
        project = self._create_project(project_id="proj-start-writing-task-fail")
        with self.session_factory() as session:
            project_row = session.get(Project, project.id)
            project_row.creation_status = "genesis_ready"
            session.commit()

        class FakeGenesisService:
            class Handoff:
                def start_writing(self, *, session, updater, command):
                    project = session.get(Project, command.project_id)
                    arc = ArcPlanVersion(
                        id="arc-start-writing-task-fail",
                        project_id=project.id,
                        version=1,
                        arc_number=1,
                        arc_synopsis="测试弧线",
                        status="active",
                    )
                    session.add(arc)
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
                    project.creation_status = "writing"
                    session.add(project)
                    session.flush()
                    return SimpleNamespace(
                        active_chapter_plan_count=1,
                        project_status="writing",
                    )

            def __init__(self):
                self.handoff = self.Handoff()

        def fail_task_creation(**_kwargs):
            raise RuntimeError("task create failed")

        with (
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

    def test_start_writing_forwards_auto_continue_target_to_task_creation(self) -> None:
        project = self._create_project(project_id="proj-start-writing-target")
        with self.session_factory() as session:
            project_row = session.get(Project, project.id)
            project_row.creation_status = "genesis_ready"
            project_row.target_total_chapters = 6
            session.commit()

        class FakeGenesisService:
            class Handoff:
                def start_writing(self, *, session, updater, command):
                    project = session.get(Project, command.project_id)
                    arc = ArcPlanVersion(
                        id="arc-start-writing-target",
                        project_id=project.id,
                        version=1,
                        arc_number=1,
                        arc_synopsis="测试弧线",
                        status="active",
                    )
                    session.add(arc)
                    session.flush()
                    for chapter_number in range(1, 7):
                        session.add(
                            ChapterPlan(
                                id=f"plan-start-writing-target-{chapter_number}",
                                project_id=project.id,
                                arc_plan_id=arc.id,
                                chapter_number=chapter_number,
                                title=f"第{chapter_number}章",
                                status="planned",
                            )
                        )
                    project.creation_status = "writing"
                    session.add(project)
                    session.flush()
                    return SimpleNamespace(
                        active_chapter_plan_count=6,
                        project_status="writing",
                    )

            def __init__(self):
                self.handoff = self.Handoff()

        captured: dict[str, object] = {}

        def capture_task_creation(**kwargs):
            captured.update(kwargs)
            return "task-start-writing-target"

        response = api_project_ops.start_project_writing(
            project.id,
            StartWritingRequest(auto_continue=False, max_chapters=2, run_until_chapter=2),
            get_session=self.session_factory,
            config=api_module._config,
            saved_runtime_config_or_default=lambda: Config(
                database_url=api_module._config.database_url,
                minimax_api_key="saved-key",
            ),
            build_genesis_service=lambda _runtime_config: FakeGenesisService(),
            close_genesis_service=lambda _service: None,
            require_genesis_project=lambda _project: None,
            active_genesis_revision=lambda _session, _project: SimpleNamespace(id="revision-start-writing-target"),
            project_has_active_generation_task=lambda _project_id, *, session=None: False,
            generation_task_conflict_message=lambda _project_id: "conflict",
            create_continue_generation_task=capture_task_creation,
        )

        self.assertEqual(response.task_id, "task-start-writing-target")
        self.assertIs(captured["auto_continue"], False)
        self.assertEqual(captured["max_chapters"], 2)
        self.assertEqual(captured["run_until_chapter"], 2)

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

    def test_continue_generation_rejects_drafted_chapter_waiting_for_acceptance(self) -> None:
        project = self._create_project(project_id="proj-drafted-waits")
        with self.session_factory() as session:
            arc = ArcPlanVersion(
                id="arc-drafted-waits",
                project_id=project.id,
                arc_synopsis="测试弧线",
                status="active",
            )
            session.add(arc)
            session.flush()
            session.add(
                ChapterPlan(
                    id="plan-drafted-1",
                    project_id=project.id,
                    arc_plan_id=arc.id,
                    chapter_number=1,
                    title="第一章",
                    status="accepted",
                )
            )
            session.add(
                ChapterPlan(
                    id="plan-drafted-2",
                    project_id=project.id,
                    arc_plan_id=arc.id,
                    chapter_number=2,
                    title="第二章",
                    status="drafted",
                )
            )
            session.add(
                ChapterPlan(
                    id="plan-drafted-3",
                    project_id=project.id,
                    arc_plan_id=arc.id,
                    chapter_number=3,
                    title="第三章",
                    status="planned",
                )
            )
            session.commit()

        with self.assertRaises(HTTPException) as ctx:
            api_module.continue_project_generation(project.id)

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("章节等待接受", str(ctx.exception.detail))
        self.assertIn("2", str(ctx.exception.detail))

    def test_continue_generation_task_requested_chapters_honors_max_chapters(self) -> None:
        project = self._create_project(project_id="proj-continue-sized-task")
        with self.session_factory() as session:
            project_row = session.get(Project, project.id)
            project_row.creation_status = "writing"
            arc = ArcPlanVersion(
                id="arc-continue-sized-task",
                project_id=project.id,
                arc_synopsis="测试弧线",
                status="active",
            )
            session.add(arc)
            session.flush()
            for chapter_number in range(1, 37):
                session.add(
                    ChapterPlan(
                        id=f"plan-continue-sized-{chapter_number}",
                        project_id=project.id,
                        arc_plan_id=arc.id,
                        chapter_number=chapter_number,
                        title=f"第{chapter_number}章",
                        status="accepted" if chapter_number <= 30 else "planned",
                    )
                )
            session.commit()

        captured: dict[str, object] = {}

        def capture_task_creation(**kwargs):
            captured.update(kwargs)
            task_id = "task-continue-sized"
            task = api_module._create_task_record(
                title=str(kwargs.get("title") or ""),
                subtitle=str(kwargs.get("subtitle") or ""),
                message=str(kwargs.get("message") or ""),
                requested_chapters=int(kwargs.get("requested_chapters") or 0),
            )
            task["project_id"] = project.id
            api_module._persist_generation_task(task_id, task)
            return task_id

        with patch("forwin.api._create_continue_generation_task", new=capture_task_creation):
            response = api_module.continue_project_generation(
                project.id,
                ProjectContinueGenerationRequest(max_chapters=2),
            )

        self.assertEqual(response.requested_chapters, 2)
        self.assertEqual(captured["requested_chapters"], 2)
        self.assertEqual(captured["max_chapters"], 2)

    def test_continue_generation_passes_auto_continue_target_to_task_creation(self) -> None:
        project = self._create_project(project_id="proj-continue-auto-target")
        with self.session_factory() as session:
            project_row = session.get(Project, project.id)
            project_row.creation_status = "writing"
            project_row.target_total_chapters = 60
            arc = ArcPlanVersion(
                id="arc-continue-auto-target",
                project_id=project.id,
                arc_synopsis="测试弧线",
                status="active",
                arc_number=3,
                chapter_start=25,
                chapter_end=36,
            )
            session.add(arc)
            session.flush()
            for chapter_number in range(25, 37):
                session.add(
                    ChapterPlan(
                        id=f"plan-continue-auto-target-{chapter_number}",
                        project_id=project.id,
                        arc_plan_id=arc.id,
                        chapter_number=chapter_number,
                        title=f"第{chapter_number}章",
                        status="planned",
                    )
                )
            session.commit()

        captured: dict[str, object] = {}

        def capture_task_creation(**kwargs):
            captured.update(kwargs)
            task_id = "task-continue-auto-target"
            task = api_module._create_task_record(
                title=str(kwargs.get("title") or ""),
                subtitle=str(kwargs.get("subtitle") or ""),
                message=str(kwargs.get("message") or ""),
                requested_chapters=int(kwargs.get("requested_chapters") or 0),
            )
            task["project_id"] = project.id
            api_module._persist_generation_task(task_id, task)
            return task_id

        with patch("forwin.api._create_continue_generation_task", new=capture_task_creation):
            response = api_module.continue_project_generation(
                project.id,
                ProjectContinueGenerationRequest(run_until_chapter=36),
            )

        self.assertEqual(response.task_id, "task-continue-auto-target")
        self.assertIs(captured["auto_continue"], True)
        self.assertEqual(captured["run_until_chapter"], 36)
        self.assertEqual(captured["requested_chapters"], 12)
        self.assertEqual(captured["max_chapters"], 12)

    def test_continue_generation_auto_continue_false_preserves_short_batch(self) -> None:
        project = self._create_project(project_id="proj-continue-auto-false")
        with self.session_factory() as session:
            project_row = session.get(Project, project.id)
            project_row.creation_status = "writing"
            project_row.target_total_chapters = 60
            arc = ArcPlanVersion(
                id="arc-continue-auto-false",
                project_id=project.id,
                arc_synopsis="测试弧线",
                status="active",
                arc_number=1,
                chapter_start=1,
                chapter_end=12,
            )
            session.add(arc)
            session.flush()
            for chapter_number in range(1, 13):
                session.add(
                    ChapterPlan(
                        id=f"plan-continue-auto-false-{chapter_number}",
                        project_id=project.id,
                        arc_plan_id=arc.id,
                        chapter_number=chapter_number,
                        title=f"第{chapter_number}章",
                        status="planned",
                    )
                )
            session.commit()

        captured: dict[str, object] = {}

        def capture_task_creation(**kwargs):
            captured.update(kwargs)
            task_id = "task-continue-auto-false"
            task = api_module._create_task_record(
                message=str(kwargs.get("message") or ""),
                requested_chapters=int(kwargs.get("requested_chapters") or 0),
            )
            task["project_id"] = project.id
            api_module._persist_generation_task(task_id, task)
            return task_id

        with patch("forwin.api._create_continue_generation_task", new=capture_task_creation):
            response = api_module.continue_project_generation(
                project.id,
                ProjectContinueGenerationRequest(auto_continue=False, max_chapters=3),
            )

        self.assertEqual(response.requested_chapters, 3)
        self.assertIs(captured["auto_continue"], False)
        self.assertEqual(captured["max_chapters"], 3)
        self.assertEqual(captured["requested_chapters"], 3)

    def test_continue_generation_filters_auto_continue_target_for_strict_task_factory(self) -> None:
        project = self._create_project(project_id="proj-continue-strict-factory")
        with self.session_factory() as session:
            project_row = session.get(Project, project.id)
            project_row.creation_status = "writing"
            project_row.target_total_chapters = 60
            arc = ArcPlanVersion(
                id="arc-continue-strict-factory",
                project_id=project.id,
                arc_synopsis="测试弧线",
                status="active",
                arc_number=1,
                chapter_start=1,
                chapter_end=4,
            )
            session.add(arc)
            session.flush()
            for chapter_number in range(1, 5):
                session.add(
                    ChapterPlan(
                        id=f"plan-continue-strict-factory-{chapter_number}",
                        project_id=project.id,
                        arc_plan_id=arc.id,
                        chapter_number=chapter_number,
                        title=f"第{chapter_number}章",
                        status="planned",
                    )
                )
            session.commit()

        def strict_task_creation(project_id, runtime_config, requested_chapters, max_chapters, title, subtitle, message):
            task_id = "task-continue-strict-factory"
            task = api_module._create_task_record(
                title=title,
                subtitle=subtitle,
                message=message,
                requested_chapters=requested_chapters,
            )
            task["project_id"] = project_id
            api_module._persist_generation_task(task_id, task)
            return task_id

        with patch("forwin.api._create_continue_generation_task", new=strict_task_creation):
            response = api_module.continue_project_generation(
                project.id,
                ProjectContinueGenerationRequest(auto_continue=True, run_until_chapter=4),
            )

        self.assertEqual(response.task_id, "task-continue-strict-factory")

    def test_continue_generation_count_is_scoped_to_active_arc(self) -> None:
        project = self._create_project(project_id="proj-continue-active-arc-only")
        with self.session_factory() as session:
            project_row = session.get(Project, project.id)
            project_row.creation_status = "writing"
            active_arc = ArcPlanVersion(
                id="arc-continue-active-only",
                project_id=project.id,
                arc_synopsis="当前弧线",
                status="active",
                arc_number=1,
                chapter_start=1,
                chapter_end=3,
            )
            future_arc = ArcPlanVersion(
                id="arc-continue-future-only",
                project_id=project.id,
                arc_synopsis="后续弧线",
                status="planned",
                arc_number=2,
                chapter_start=4,
                chapter_end=6,
            )
            session.add_all([active_arc, future_arc])
            session.flush()
            for chapter_number, status, arc_id in [
                (1, "accepted", active_arc.id),
                (2, "accepted", active_arc.id),
                (3, "planned", active_arc.id),
                (4, "planned", future_arc.id),
                (5, "planned", future_arc.id),
            ]:
                session.add(
                    ChapterPlan(
                        id=f"plan-continue-active-only-{chapter_number}",
                        project_id=project.id,
                        arc_plan_id=arc_id,
                        chapter_number=chapter_number,
                        title=f"第{chapter_number}章",
                        status=status,
                    )
                )
            session.commit()

        captured: dict[str, object] = {}

        def capture_task_creation(**kwargs):
            captured.update(kwargs)
            task_id = "task-continue-active-only"
            task = api_module._create_task_record(
                message=str(kwargs.get("message") or ""),
                requested_chapters=int(kwargs.get("requested_chapters") or 0),
            )
            task["project_id"] = project.id
            api_module._persist_generation_task(task_id, task)
            return task_id

        with patch("forwin.api._create_continue_generation_task", new=capture_task_creation):
            response = api_module.continue_project_generation(
                project.id,
                ProjectContinueGenerationRequest(max_chapters=10),
            )

        self.assertEqual(response.requested_chapters, 1)
        self.assertEqual(captured["requested_chapters"], 1)
        self.assertEqual(captured["max_chapters"], 10)

    def test_project_continue_generation_request_rejects_non_positive_run_limits(self) -> None:
        with self.assertRaises(ValidationError):
            ProjectContinueGenerationRequest(max_chapters=0)
        with self.assertRaises(ValidationError):
            ProjectContinueGenerationRequest(max_chapters=-1)
        with self.assertRaises(ValidationError):
            ProjectContinueGenerationRequest(run_until_chapter=0)
        with self.assertRaises(ValidationError):
            ProjectContinueGenerationRequest(run_until_chapter=-1)
        self.assertEqual(ProjectContinueGenerationRequest(max_chapters=1).max_chapters, 1)
        self.assertEqual(ProjectContinueGenerationRequest(run_until_chapter=1).run_until_chapter, 1)

    def test_generation_control_drafted_chapter_blocks_future_arc_resume(self) -> None:
        project = self._create_project(project_id="proj-drafted-future-arc")
        with self.session_factory() as session:
            project_row = session.get(Project, project.id)
            project_row.creation_status = "writing"
            active_arc = ArcPlanVersion(
                id="arc-drafted-future-current",
                project_id=project.id,
                arc_synopsis="当前弧线",
                status="active",
                arc_number=1,
                chapter_start=1,
                chapter_end=3,
            )
            future_arc = ArcPlanVersion(
                id="arc-drafted-future-next",
                project_id=project.id,
                arc_synopsis="后续弧线",
                status="planned",
                arc_number=2,
                chapter_start=4,
                chapter_end=6,
            )
            session.add_all([active_arc, future_arc])
            session.flush()
            session.add_all(
                [
                    ChapterPlan(
                        id="plan-drafted-future-1",
                        project_id=project.id,
                        arc_plan_id=active_arc.id,
                        chapter_number=1,
                        title="第一章",
                        status="accepted",
                    ),
                    ChapterPlan(
                        id="plan-drafted-future-2",
                        project_id=project.id,
                        arc_plan_id=active_arc.id,
                        chapter_number=2,
                        title="第二章",
                        status="drafted",
                    ),
                    ChapterPlan(
                        id="plan-drafted-future-3",
                        project_id=project.id,
                        arc_plan_id=active_arc.id,
                        chapter_number=3,
                        title="第三章",
                        status="planned",
                    ),
                ]
            )
            session.commit()

        detail = api_module.get_project(project.id)
        summary = next(item for item in api_module.list_projects() if item.id == project.id)

        self.assertEqual(detail.generation_control.plan_state, "pending_acceptance")
        self.assertEqual(detail.generation_control.review_state, "pending_acceptance")
        self.assertEqual(detail.generation_control.next_gate, "chapter_2_accept")
        self.assertFalse(detail.generation_control.can_resume)
        self.assertFalse(summary.generation_control.can_resume)

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
