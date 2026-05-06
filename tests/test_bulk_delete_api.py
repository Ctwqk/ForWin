from __future__ import annotations

import unittest
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import forwin.api as api_module
from forwin.api_schemas import ProjectBulkDeleteRequest, TaskBulkDeleteRequest
from forwin.models.base import get_engine, get_session_factory, init_db, new_id
from forwin.models.governance import DecisionEvent
from forwin.models.phase import ChapterRewriteAttempt
from forwin.models.draft import CandidateDraftRecord, ChapterDraft, ChapterReview
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from forwin.models.task import GenerationTask


class BulkDeleteApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = TemporaryDirectory()
        self.engine = get_engine(postgres_test_url("bulk-delete"))
        init_db(self.engine)
        self.session_factory = get_session_factory(self.engine)
        self.old_session_factory = api_module._SessionFactory
        self.old_config = api_module._config
        api_module._SessionFactory = self.session_factory
        api_module._config = api_module.Config(
            database_url=postgres_test_url("bulk-delete"),
            artifact_root=str(Path(self.tmpdir.name) / "artifacts"),
            minimax_api_key="",
        )

    def tearDown(self) -> None:
        api_module._SessionFactory = self.old_session_factory
        api_module._config = self.old_config
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
        audit_root = Path(api_module._config.artifact_root) / "audit_bundles" / "projects" / project_id
        bundles = sorted(audit_root.glob("*.json"))
        self.assertEqual(len(bundles), 1)
        bundle = json.loads(bundles[0].read_text(encoding="utf-8"))
        self.assertEqual(bundle["project_id"], project_id)
        self.assertEqual(bundle["operation_id"], response.operation_id)
        self.assertIn("decision_events", bundle)
        with self.session_factory() as session:
            rows = session.query(DecisionEvent).filter(DecisionEvent.project_id == project_id).all()
        # The DB rows may be deleted with the project, so the exported bundle is the durable audit evidence.
        self.assertEqual(rows, [])

    def test_delete_project_removes_candidate_draft_records_before_reviews(self) -> None:
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
