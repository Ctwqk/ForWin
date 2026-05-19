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

        self.assertTrue(getattr(shared, "LLM_PROVIDER_PRESETS", None))
        self.assertIn("function clearNode(node)", getattr(shared, "PAGE_DOM_HELPERS_JS", ""))

        home_html = home.render_home_page(
            has_api_key=False,
            base_url="https://api.minimaxi.com/v1",
            model="MiniMax-M2.7",
            operation_mode="blackbox",
            freeze_failed_candidates=True,
        )
        self.assertIn("ForWin 工作台", home_html)
        self.assertIn("config_generation_min_chapter_chars", home_html)

        publishers_html = publishers.render_publishers_page(
            backend_ready={"extension_api_key_configured": True},
            extension_install_path="browser_extension/forwin-publisher",
        )
        self.assertIn("ForWin 发布", publishers_html)
        self.assertIn("下载扩展包", publishers_html)
        self.assertIn("function clearNode(node)", publishers_html)

    def test_api_operation_split_modules_are_available(self) -> None:
        publisher_ops = self._import_required_module("forwin.api_publisher_ops")
        project_ops = self._import_required_module("forwin.api_project_ops")
        governance_ops = self._import_required_module("forwin.api_governance_ops")

        for module, names in (
            (
                publisher_ops,
                (
                    "download_publisher_extension_package",
                    "download_publisher_firefox_extension_package",
                    "create_publisher_upload_job",
                    "publisher_extension_heartbeat",
                ),
            ),
            (
                project_ops,
                (
                    "create_project",
                    "continue_project_generation",
                    "get_chapter_review",
                    "approve_chapter_review",
                    "retry_chapter_review",
                ),
            ),
            (
                governance_ops,
                (
                    "get_project_governance",
                    "create_manual_checkpoint",
                    "get_project_causal_replay",
                    "override_band_experience",
                ),
            ),
        ):
            for name in names:
                self.assertTrue(callable(getattr(module, name, None)), f"expected {module.__name__}.{name}")

    def test_api_files_stay_split_instead_of_regressing_into_giants(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        line_limits = {
            "forwin/api.py": 2200,
            "forwin/api_pages.py": 80,
            "forwin/api_pages_home.py": 300,
            "forwin/api_pages_publishers.py": 250,
            "forwin/api_system_routes.py": 500,
            "forwin/api_task_routes.py": 500,
            "forwin/api_publisher_routes.py": 400,
            "forwin/api_project_routes.py": 500,
            "forwin/api_governance_routes.py": 500,
            "forwin/api_governance_support.py": 900,
            "forwin/api_automation.py": 450,
        }
        for relative_path, max_lines in line_limits.items():
            path = repo_root / relative_path
            self.assertTrue(path.exists(), f"expected split file {relative_path} to exist")
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
            self.assertTrue(directory.is_dir(), f"expected asset directory {relative_dir}")
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
