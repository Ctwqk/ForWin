from __future__ import annotations

import unittest
from tempfile import TemporaryDirectory

from sqlalchemy import select

from forwin.api_schema import ProjectAutomationUpdateRequest, ProjectCreateRequest
from forwin.config import InfrastructureConfig
from forwin.application.read_models import normalize_project_automation
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.project import Project
from tests.http_runtime_harness import HttpRuntimeHarness
from tests.postgres import postgres_test_url


class ProjectPublishBindingTests(unittest.TestCase):
    def test_normalize_project_automation_keeps_two_unique_bindings(self) -> None:
        automation = normalize_project_automation(
            {
                "publish": {
                    "platform": "qidian",
                    "book_name": "起点主书名",
                    "create_if_missing": True,
                },
                "publish_bindings": [
                    {
                        "platform": "fanqie",
                        "book_name": "番茄书名",
                        "create_if_missing": False,
                    },
                    {
                        "platform": "qidian",
                        "book_name": "旧起点书名",
                        "create_if_missing": False,
                    },
                ],
            }
        )

        self.assertEqual(automation.publish.platform, "qidian")
        self.assertEqual(automation.publish.book_name, "起点主书名")
        self.assertTrue(automation.publish.create_if_missing)
        self.assertEqual(
            [item.platform for item in automation.publish_bindings],
            ["qidian", "fanqie"],
        )

    def test_project_automation_rejects_unsupported_platform(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported publisher platform: qidain"):
            ProjectAutomationUpdateRequest.model_validate(
                {
                    "publish_bindings": [
                        {"platform": "qidain", "book_name": "拼写错误"}
                    ]
                }
            )

    def test_normalize_project_automation_backfills_bindings_from_legacy_publish(self) -> None:
        automation = normalize_project_automation(
            {
                "publish": {
                    "platform": "fanqie",
                    "book_name": "旧数据书名",
                    "upload_url": "https://example.com/upload",
                    "create_if_missing": True,
                }
            }
        )

        self.assertEqual(len(automation.publish_bindings), 1)
        self.assertEqual(automation.publish_bindings[0].platform, "fanqie")
        self.assertEqual(automation.publish_bindings[0].book_name, "旧数据书名")
        self.assertTrue(automation.publish_bindings[0].create_if_missing)

    def test_create_project_supports_two_publish_bindings(self) -> None:
        tmpdir = TemporaryDirectory()
        engine = get_engine(postgres_test_url("projects"))
        init_db(engine)
        session_factory = get_session_factory(engine)
        api = HttpRuntimeHarness(
            session_factory=session_factory,
            config=InfrastructureConfig(database_url=postgres_test_url("projects")),
            engine=engine,
        )

        try:
            req = ProjectCreateRequest.model_validate(
                {
                    "title": "双平台测试书",
                    "premise": "测试 premise",
                    "genre": "都市",
                    "publish_bindings": [
                        {
                            "platform": "fanqie",
                            "book_name": "番茄版作品名",
                            "upload_url": "https://fanqie.example/upload",
                            "create_if_missing": True,
                        },
                        {
                            "platform": "qidian",
                            "book_name": "起点版作品名",
                            "upload_url": "https://qidian.example/upload",
                            "create_if_missing": False,
                        },
                    ],
                }
            )

            response = api.create_project(req)

            with session_factory() as session:
                project = session.execute(select(Project)).scalar_one()
                automation = normalize_project_automation(project.automation_json)

            self.assertEqual(response.project_id, project.id)
            self.assertEqual(response.title, "双平台测试书")
            self.assertEqual(response.creation_status, "creating")
            self.assertTrue(response.active_genesis_revision_id)
            self.assertIn("Genesis 工作台", response.message)
            self.assertEqual(automation.publish.platform, "fanqie")
            self.assertEqual(automation.publish.book_name, "番茄版作品名")
            self.assertTrue(automation.publish.create_if_missing)
            self.assertEqual(
                [item.platform for item in automation.publish_bindings],
                ["fanqie", "qidian"],
            )
        finally:
            engine.dispose()
            tmpdir.cleanup()

    def test_update_project_automation_supports_two_publish_bindings(self) -> None:
        tmpdir = TemporaryDirectory()
        engine = get_engine(postgres_test_url("project-automation"))
        init_db(engine)
        session_factory = get_session_factory(engine)
        api = HttpRuntimeHarness(
            session_factory=session_factory,
            config=InfrastructureConfig(
                database_url=postgres_test_url("project-automation")
            ),
            engine=engine,
        )

        try:
            created = api.create_project(
                ProjectCreateRequest.model_validate(
                    {
                        "title": "自动化绑定测试书",
                        "premise": "测试 premise",
                        "genre": "都市",
                    }
                )
            )

            updated = api.update_project_automation(
                created.project_id,
                ProjectAutomationUpdateRequest.model_validate(
                    {
                        "enabled": True,
                        "auto_publish": True,
                        "publish": {
                            "platform": "fanqie",
                            "book_name": "番茄主绑定",
                            "create_if_missing": True,
                        },
                        "publish_bindings": [
                            {
                                "platform": "fanqie",
                                "book_name": "番茄主绑定",
                                "create_if_missing": True,
                            },
                            {
                                "platform": "qidian",
                                "book_name": "起点副绑定",
                                "create_if_missing": False,
                            },
                        ],
                    }
                ),
            )

            self.assertEqual(updated.project_id, created.project_id)
            self.assertEqual(updated.message, "书本自动化设置已保存。")
            self.assertEqual(
                [item.platform for item in updated.automation.publish_bindings],
                ["fanqie", "qidian"],
            )
            self.assertEqual(updated.automation.publish_bindings[1].book_name, "起点副绑定")

            with session_factory() as session:
                project = session.execute(select(Project)).scalar_one()
                automation = normalize_project_automation(project.automation_json)

            self.assertEqual(
                [item.platform for item in automation.publish_bindings],
                ["fanqie", "qidian"],
            )
        finally:
            engine.dispose()
            tmpdir.cleanup()


if __name__ == "__main__":
    unittest.main()
