from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import forwin.api as api_module
from forwin.api_schemas import ProjectBulkDeleteRequest, TaskBulkDeleteRequest
from forwin.models.base import get_engine, get_session_factory, init_db, new_id
from forwin.models.project import Project
from forwin.models.task import GenerationTask


class BulkDeleteApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = TemporaryDirectory()
        self.engine = get_engine(str(Path(self.tmpdir.name) / "bulk-delete.db"))
        init_db(self.engine)
        self.session_factory = get_session_factory(self.engine)
        self.old_session_factory = api_module._SessionFactory
        api_module._SessionFactory = self.session_factory

    def tearDown(self) -> None:
        api_module._SessionFactory = self.old_session_factory
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

        self.assertTrue(response.ok)
        self.assertEqual(response.deleted_count, 2)
        self.assertEqual(response.skipped_count, 1)
        with self.session_factory() as session:
            self.assertEqual(session.query(Project).count(), 0)

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

        self.assertTrue(response.ok)
        self.assertEqual(response.deleted_count, 1)
        self.assertEqual(response.skipped_count, 1)
        with self.session_factory() as session:
            row = session.get(GenerationTask, task_id)
            self.assertTrue(row is None or row.deleted_at is not None)


if __name__ == "__main__":
    unittest.main()
