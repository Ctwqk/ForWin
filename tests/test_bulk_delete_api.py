from __future__ import annotations

import unittest
import json
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from forwin.api_schema import ProjectBulkDeleteRequest, TaskBulkDeleteRequest
from forwin.config import InfrastructureConfig
from forwin.models.base import get_engine, get_session_factory, init_db, new_id
from forwin.models.audit import DecisionEvent
from forwin.models.phase import ChapterRewriteAttempt
from forwin.models.draft import CandidateDraftRecord, ChapterDraft, ChapterReview
from forwin.models.outbox import OutboxEvent
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from forwin.models.publisher import (
    PublisherMilestone,
    PublisherOperatorAction,
    PublisherUploadAttempt,
    PublisherUploadJob,
    PublisherUploadReceipt,
    PublisherWorkBinding,
)
from forwin.models.task import GenerationTask
from tests.http_runtime_harness import HttpRuntimeHarness
from tests.postgres import postgres_test_url


api_module: HttpRuntimeHarness


class BulkDeleteApiTests(unittest.TestCase):
    def setUp(self) -> None:
        global api_module
        self.tmpdir = TemporaryDirectory()
        self.engine = get_engine(postgres_test_url("bulk-delete"))
        init_db(self.engine)
        self.session_factory = get_session_factory(self.engine)
        api_module = HttpRuntimeHarness(
            session_factory=self.session_factory,
            config=InfrastructureConfig(
                database_url=postgres_test_url("bulk-delete"),
                artifact_root=str(Path(self.tmpdir.name) / "artifacts"),
                minimax_api_key="",
            ),
            engine=self.engine,
        )

    def tearDown(self) -> None:
        self.engine.dispose()
        self.tmpdir.cleanup()

    def test_bulk_delete_projects_removes_existing_projects(self) -> None:
        with self.session_factory() as session:
            project_ids = []
            for index in range(2):
                project = Project(
                    id=new_id(),
                    title=f"测试书{index + 1}",
                    premise="测试 premise",
                    genre="玄幻",
                    setting_summary="",
                )
                session.add(project)
                project_ids.append(project.id)
            session.commit()

        response = api_module.bulk_delete_projects(
            ProjectBulkDeleteRequest(project_ids=project_ids + ["missing-project"])
        )

        self.assertEqual(response.deleted_count, 2)
        self.assertEqual(response.skipped_count, 1)
        self.assertEqual(response.deleted_ids, project_ids)
        self.assertEqual(response.skipped_ids, ["missing-project"])
        self.assertEqual(response.message, "已删除 2 本书，跳过 1 本。")
        with self.session_factory() as session:
            self.assertEqual(session.query(Project).count(), 0)

    def test_delete_project_exports_audit_bundle_and_records_operation_id(self) -> None:
        project_id = new_id()
        with self.session_factory() as session:
            session.add(
                Project(
                    id=project_id,
                    title="审计删除",
                    premise="测试 premise",
                    genre="玄幻",
                    setting_summary="",
                )
            )
            session.commit()

        response = api_module.delete_project(project_id)

        self.assertTrue(response.operation_id)
        audit_root = (
            Path(api_module._config.artifact_root)
            / "audit_bundles"
            / "projects"
            / project_id
        )
        bundles = sorted(audit_root.glob("*.json"))
        self.assertEqual(len(bundles), 1)
        bundle = json.loads(bundles[0].read_text(encoding="utf-8"))
        self.assertEqual(bundle["project_id"], project_id)
        self.assertEqual(bundle["operation_id"], response.operation_id)
        self.assertIn("decision_events", bundle)
        with self.session_factory() as session:
            rows = (
                session.query(DecisionEvent)
                .filter(DecisionEvent.project_id == project_id)
                .all()
            )
        # The DB rows may be deleted with the project, so the exported bundle is the durable audit evidence.
        self.assertEqual(rows, [])

    def test_delete_project_removes_candidate_draft_records_before_reviews(
        self,
    ) -> None:
        project_id = new_id()
        with self.session_factory() as session:
            project = Project(
                id=project_id,
                title="候选删除",
                premise="测试 premise",
                genre="玄幻",
                setting_summary="",
            )
            arc = ArcPlanVersion(
                id=new_id(),
                project_id=project_id,
                arc_synopsis="测试弧线",
                status="active",
            )
            plan = ChapterPlan(
                id=new_id(),
                project_id=project_id,
                arc_plan_id=arc.id,
                chapter_number=1,
                title="第一章",
                status="drafted",
            )
            draft = ChapterDraft(
                id=new_id(),
                chapter_plan_id=plan.id,
                version=1,
                body_text="正文。" * 500,
                summary="摘要",
                char_count=1500,
            )
            review = ChapterReview(
                id=new_id(),
                draft_id=draft.id,
                verdict="warn",
                issues_json="[]",
            )
            candidate = CandidateDraftRecord(
                id=new_id(),
                project_id=project_id,
                chapter_plan_id=plan.id,
                chapter_number=1,
                candidate_draft_id=draft.id,
                review_id=review.id,
            )
            rewrite_attempt = ChapterRewriteAttempt(
                id=new_id(),
                project_id=project_id,
                chapter_number=1,
                attempt_no=1,
                trigger_review_id=review.id,
                source_draft_id=draft.id,
                result_draft_id=draft.id,
                result_review_id=review.id,
            )
            session.add(project)
            session.flush()
            session.add(arc)
            session.flush()
            session.add(plan)
            session.flush()
            session.add(draft)
            session.flush()
            session.add(review)
            session.flush()
            session.add_all([candidate, rewrite_attempt])
            session.commit()

        response = api_module.delete_project(project_id)

        self.assertTrue(response.ok)
        with self.session_factory() as session:
            self.assertIsNone(session.get(Project, project_id))
            self.assertEqual(session.query(CandidateDraftRecord).count(), 0)
            self.assertEqual(session.query(ChapterRewriteAttempt).count(), 0)
            self.assertEqual(session.query(ChapterReview).count(), 0)

    def test_delete_project_removes_canon_commit_chain_before_candidates(self) -> None:
        # Production purge failed here: canon commits, post-Canon runs, projection checkpoints and
        # upload jobs point at the candidate (directly or through the commit) and must go first.
        from forwin.models.canon import CanonCommitRecord
        from forwin.models.maintenance import PostCanonMaintenanceRun
        from forwin.models.projection import ProjectionCheckpoint

        project_id = new_id()
        with self.session_factory() as session:
            session.add(Project(id=project_id, title="提交链删除", premise="测试 premise", genre="玄幻",
                                setting_summary=""))
            session.flush()
            arc = ArcPlanVersion(id=new_id(), project_id=project_id, arc_synopsis="测试弧线", status="active")
            session.add(arc)
            session.flush()
            plan = ChapterPlan(id=new_id(), project_id=project_id, arc_plan_id=arc.id, chapter_number=1,
                               title="第一章", status="accepted")
            session.add(plan)
            session.flush()
            draft = ChapterDraft(id=new_id(), chapter_plan_id=plan.id, version=1, body_text="正文。" * 500,
                                 summary="摘要", char_count=1500)
            session.add(draft)
            session.flush()
            review = ChapterReview(id=new_id(), draft_id=draft.id, verdict="pass", issues_json="[]")
            session.add(review)
            session.flush()
            candidate = CandidateDraftRecord(id=new_id(), project_id=project_id, chapter_plan_id=plan.id,
                                             chapter_number=1, candidate_draft_id=draft.id, review_id=review.id)
            session.add(candidate)
            session.flush()
            commit = CanonCommitRecord(id=new_id(), idempotency_key=new_id(), candidate_id=candidate.id,
                                       project_id=project_id, chapter_number=1)
            session.add(commit)
            session.flush()
            session.add(PostCanonMaintenanceRun(canon_commit_id=commit.id, project_id=project_id,
                                                chapter_number=1, candidate_id=candidate.id, step_name="world",
                                                idempotency_key=new_id()))
            session.add(ProjectionCheckpoint(project_id=project_id, projection_kind="obsidian",
                                             projected_canon_commit_id=commit.id,
                                             target_canon_commit_id=commit.id))
            session.add(PublisherUploadJob(project_id=project_id, chapter_number=1, canon_commit_id=commit.id,
                                           platform_id="fanqie", body_text="正文", status="succeeded"))
            session.commit()

        response = api_module.delete_project(project_id)

        self.assertTrue(response.ok)
        with self.session_factory() as session:
            self.assertIsNone(session.get(Project, project_id))
            for model in (CanonCommitRecord, PostCanonMaintenanceRun, ProjectionCheckpoint, PublisherUploadJob,
                          CandidateDraftRecord, ChapterReview, ChapterDraft):
                self.assertEqual(session.query(model).count(), 0, model.__tablename__)

    def test_delete_project_removes_publisher_child_rows_and_project_outbox_events(
        self,
    ) -> None:
        project_a = new_id()
        project_b = new_id()
        job_id = new_id()
        attempt_id = new_id()
        binding_id = new_id()
        with self.session_factory() as session:
            for project_id, title in ((project_a, "发布子表删除"), (project_b, "保留项目")):
                session.add(
                    Project(
                        id=project_id,
                        title=title,
                        premise="测试 premise",
                        genre="玄幻",
                        setting_summary="",
                    )
                )
            session.flush()
            session.add(
                PublisherUploadJob(
                    id=job_id,
                    project_id=project_a,
                    platform_id="fanqie",
                    status="succeeded",
                    book_name="测试书",
                    chapter_title="第一章",
                    body_text="正文",
                    publish=False,
                )
            )
            session.add(
                PublisherWorkBinding(
                    id=binding_id,
                    project_id=project_a,
                    platform_id="fanqie",
                    book_name="测试书",
                )
            )
            session.flush()
            session.add(
                PublisherUploadAttempt(
                    id=attempt_id,
                    upload_job_id=job_id,
                    attempt_number=1,
                    attempt_kind="upload",
                    status="succeeded",
                )
            )
            session.add(
                PublisherOperatorAction(
                    id=new_id(),
                    upload_job_id=job_id,
                    action="pause",
                    pause_token="token-1",
                    actor_id="operator",
                    auth_method="test",
                    reason="test",
                    old_state_json="{}",
                    new_state_json="{}",
                    occurred_at=datetime(2026, 1, 1),
                )
            )
            session.add(
                PublisherMilestone(
                    id=new_id(),
                    work_binding_id=binding_id,
                    milestone_type="first_chapter_published",
                )
            )
            session.flush()
            session.add(
                PublisherUploadReceipt(
                    id=new_id(),
                    upload_job_id=job_id,
                    upload_attempt_id=attempt_id,
                    receipt_key="receipt-a",
                )
            )
            session.add_all(
                [
                    OutboxEvent(
                        id=new_id(),
                        event_id=new_id(),
                        aggregate_type="project",
                        aggregate_id=project_a,
                        event_type="project.test",
                    ),
                    OutboxEvent(
                        id=new_id(),
                        event_id=new_id(),
                        aggregate_type="project",
                        aggregate_id=project_b,
                        event_type="project.test",
                    ),
                ]
            )
            session.commit()

        response = api_module.delete_project(project_a)

        self.assertTrue(response.ok)
        with self.session_factory() as session:
            self.assertIsNone(session.get(Project, project_a))
            self.assertEqual(session.query(PublisherUploadJob).count(), 0)
            self.assertEqual(session.query(PublisherUploadAttempt).count(), 0)
            self.assertEqual(session.query(PublisherUploadReceipt).count(), 0)
            self.assertEqual(session.query(PublisherOperatorAction).count(), 0)
            self.assertEqual(session.query(PublisherWorkBinding).count(), 0)
            self.assertEqual(session.query(PublisherMilestone).count(), 0)
            remaining_outbox = session.query(OutboxEvent).all()
            self.assertEqual(
                [(row.aggregate_type, row.aggregate_id) for row in remaining_outbox],
                [("project", project_b)],
            )
            self.assertIsNotNone(session.get(Project, project_b))

    def test_bulk_delete_tasks_marks_generation_tasks_deleted(self) -> None:
        task_id = "task-delete-1"
        with self.session_factory() as session:
            session.add(
                GenerationTask(
                    id=task_id,
                    task_kind="generation",
                    status="completed",
                    current_stage="completed",
                    message="",
                )
            )
            session.commit()

        response = api_module.bulk_delete_tasks(
            TaskBulkDeleteRequest(
                items=[
                    {"task_kind": "generation", "task_id": task_id},
                    {"task_kind": "generation", "task_id": "missing-task"},
                ]
            )
        )

        self.assertEqual(response.deleted_count, 1)
        self.assertEqual(response.skipped_count, 1)
        self.assertEqual(response.deleted_ids, [f"generation:{task_id}"])
        self.assertEqual(response.skipped_ids, ["generation:missing-task"])
        self.assertEqual(response.message, "已删除 1 条任务，跳过 1 条。")
        with self.session_factory() as session:
            row = session.get(GenerationTask, task_id)
            self.assertTrue(row is None or row.deleted_at is not None)


if __name__ == "__main__":
    unittest.main()
