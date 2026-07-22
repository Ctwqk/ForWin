from __future__ import annotations

import importlib
import importlib.util
import unittest
from pathlib import Path


class ApiSplitModuleTests(unittest.TestCase):
    def _import_required_module(self, module_name: str):
        spec = importlib.util.find_spec(module_name)
        self.assertIsNotNone(spec, f"expected split module {module_name} to exist")
        return importlib.import_module(module_name)

    def test_api_pages_split_modules_are_available(self) -> None:
        shared = self._import_required_module("forwin.api_pages_shared")
        home = self._import_required_module("forwin.api_pages_home")
        publishers = self._import_required_module("forwin.api_pages_publishers")

        self.assertFalse(hasattr(shared, "LLM_PROVIDER_PRESETS"))
        self.assertIn(
            "function clearNode(node)", getattr(shared, "PAGE_DOM_HELPERS_JS", "")
        )

        home_html = home.render_home_page()
        self.assertIn("ForWin 工作台", home_html)
        self.assertIn("runtime_policy_min_chapter_chars", home_html)

        publishers_html = publishers.render_publishers_page(
            backend_ready={"extension_api_key_configured": True},
            extension_install_path="browser_extension/forwin-publisher",
        )
        self.assertIn("ForWin 发布", publishers_html)
        self.assertIn("下载扩展包", publishers_html)
        self.assertIn("function clearNode(node)", publishers_html)

    def test_application_services_are_available(self) -> None:
        publisher = self._import_required_module("forwin.application.publisher")
        projects = self._import_required_module("forwin.application.projects")
        project_control_ops = self._import_required_module(
            "forwin.application.project_control.operations"
        )
        task_center = self._import_required_module("forwin.application.task_center")

        for owner, names in (
            (
                publisher.PublisherApplicationService,
                (
                    "download_publisher_extension_package",
                    "download_publisher_firefox_extension_package",
                    "create_publisher_upload_job",
                    "publisher_extension_heartbeat",
                ),
            ),
            (
                projects.ProjectApplicationService,
                (
                    "create_project",
                    "continue_project_generation",
                    "get_chapter_review",
                    "approve_chapter_review",
                    "retry_chapter_review",
                ),
            ),
            (
                project_control_ops,
                (
                    "create_manual_checkpoint",
                    "get_project_causal_replay",
                    "get_project_audit_insights",
                    "override_band_experience",
                ),
            ),
            (
                task_center.TaskCenterService,
                (
                    "load_generation_task",
                    "list_generation_tasks",
                    "list_project_backed_task_items",
                ),
            ),
        ):
            for name in names:
                self.assertTrue(
                    callable(getattr(owner, name, None)),
                    f"expected {owner.__name__}.{name}",
                )

    def test_api_entrypoint_exports_only_asgi_contract(self) -> None:
        api_module = self._import_required_module("forwin.api")
        http_app = self._import_required_module("forwin.http")

        self.assertEqual(api_module.__all__, ["app", "create_app", "lifespan"])
        self.assertIs(api_module.create_app, http_app.create_app)
        self.assertIs(api_module.lifespan, http_app.lifespan)
        self.assertFalse(hasattr(api_module, "_SessionFactory"))
        self.assertFalse(hasattr(api_module, "_create_continue_generation_task"))

    def test_api_files_stay_split_instead_of_regressing_into_giants(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        line_limits = {
            "forwin/api.py": 20,
            "forwin/http/app.py": 420,
            "forwin/http/automation.py": 220,
            "forwin/http/generation.py": 420,
            "forwin/http/project_support.py": 420,
            "forwin/http/request_support.py": 240,
            "forwin/http/runtime.py": 220,
            "forwin/http/tasks.py": 950,
            "forwin/api_pages.py": 80,
            "forwin/api_pages_home.py": 300,
            "forwin/api_pages_publishers.py": 250,
            "forwin/http/adapters/api_system_routes.py": 500,
            "forwin/http/adapters/api_task_routes.py": 500,
            "forwin/http/adapters/api_publisher_routes.py": 200,
            "forwin/http/adapters/api_publisher_extension_attempt_routes.py": 180,
            "forwin/http/adapters/api_project_routes.py": 80,
            "forwin/application/publisher/service.py": 400,
            "forwin/application/projects/service.py": 500,
            "forwin/http/adapters/api_project_control_routes.py": 500,
            "forwin/application/tasks.py": 420,
            "forwin/application/task_center.py": 650,
            "forwin/application/project_control/service.py": 420,
            "forwin/application/project_control/support.py": 900,
            "forwin/api_automation.py": 450,
        }
        for relative_path, max_lines in line_limits.items():
            path = repo_root / relative_path
            self.assertTrue(
                path.exists(), f"expected split file {relative_path} to exist"
            )
            self.assertLessEqual(
                len(path.read_text(encoding="utf-8").splitlines()),
                max_lines,
                f"{relative_path} should stay below {max_lines} lines",
            )

        asset_limits = {
            "forwin/ui_assets/home": {"min_files": 5, "max_file_lines": 1400},
            "forwin/ui_assets/publishers": {"min_files": 3, "max_file_lines": 900},
        }
        for relative_dir, rules in asset_limits.items():
            directory = repo_root / relative_dir
            self.assertTrue(
                directory.is_dir(), f"expected asset directory {relative_dir}"
            )
            files = [path for path in directory.rglob("*") if path.is_file()]
            self.assertGreaterEqual(
                len(files),
                rules["min_files"],
                f"{relative_dir} should contain at least {rules['min_files']} split assets",
            )
            for path in files:
                self.assertLessEqual(
                    len(path.read_text(encoding="utf-8").splitlines()),
                    rules["max_file_lines"],
                    f"{path.relative_to(repo_root)} should stay below {rules['max_file_lines']} lines",
                )


if __name__ == "__main__":
    unittest.main()
