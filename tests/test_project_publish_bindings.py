from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import select

import forwin.api as api_module
from forwin.api_project_payloads import normalize_project_automation
from forwin.api_schemas import ProjectCreateRequest
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.project import Project


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
                    {
                        "platform": "zhihu",
                        "book_name": "知乎盐言版",
                        "create_if_missing": True,
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
        engine = get_engine(str(Path(tmpdir.name) / "projects.db"))
        init_db(engine)
        session_factory = get_session_factory(engine)
        old_session_factory = api_module._SessionFactory

        try:
            api_module._SessionFactory = session_factory
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

            response = api_module.create_project(req)

            self.assertTrue(response.ok)
            with session_factory() as session:
                project = session.execute(select(Project)).scalar_one()
                automation = normalize_project_automation(project.automation_json)

            self.assertEqual(automation.publish.platform, "fanqie")
            self.assertEqual(automation.publish.book_name, "番茄版作品名")
            self.assertTrue(automation.publish.create_if_missing)
            self.assertEqual(
                [item.platform for item in automation.publish_bindings],
                ["fanqie", "qidian"],
            )
        finally:
            api_module._SessionFactory = old_session_factory
            engine.dispose()
            tmpdir.cleanup()

    def test_update_project_automation_supports_two_publish_bindings(self) -> None:
        tmpdir = TemporaryDirectory()
        engine = get_engine(str(Path(tmpdir.name) / "project-automation.db"))
        init_db(engine)
        session_factory = get_session_factory(engine)
        old_session_factory = api_module._SessionFactory

        try:
            api_module._SessionFactory = session_factory
            created = api_module.create_project(
                ProjectCreateRequest.model_validate(
                    {
                        "title": "自动化绑定测试书",
                        "premise": "测试 premise",
                        "genre": "都市",
                    }
                )
            )

            updated = api_module.update_project_automation(
                created.project_id,
                api_module.ProjectAutomationUpdateRequest.model_validate(
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

            self.assertTrue(updated.ok)
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
            api_module._SessionFactory = old_session_factory
            engine.dispose()
            tmpdir.cleanup()


if __name__ == "__main__":
    unittest.main()
