from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class WorldStudioFrontendTests(unittest.TestCase):
    def test_world_studio_has_return_navigation_to_console(self) -> None:
        app_source = (REPO_ROOT / "frontend/world-studio/src/App.tsx").read_text(encoding="utf-8")
        css_source = (REPO_ROOT / "frontend/world-studio/src/styles.css").read_text(encoding="utf-8")

        self.assertIn('aria-label="ForWin primary navigation"', app_source)
        self.assertIn('href="/"', app_source)
        self.assertIn('创作台', app_source)
        self.assertIn('World Studio', app_source)
        self.assertIn("studio-nav", app_source)
        self.assertIn(".studio-nav", css_source)


if __name__ == "__main__":
    unittest.main()
