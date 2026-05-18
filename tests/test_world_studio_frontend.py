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
        self.assertIn('书本', app_source)
        self.assertIn('任务', app_source)
        self.assertIn('世界档案', app_source)
        self.assertIn('发布', app_source)
        self.assertIn('配置', app_source)
        self.assertIn("nav-tabs nav-tabs--primary", app_source)
        self.assertIn("nav-tabs nav-tabs--secondary", app_source)
        self.assertIn(".nav-tabs", css_source)
        self.assertIn(".nav-tabs--secondary", css_source)
        self.assertNotIn("studio-nav", app_source)
        self.assertNotIn(".studio-nav", css_source)

    def test_world_studio_uses_forwin_design_system_baseline(self) -> None:
        app_source = (REPO_ROOT / "frontend/world-studio/src/App.tsx").read_text(encoding="utf-8")
        css_source = (REPO_ROOT / "frontend/world-studio/src/styles.css").read_text(encoding="utf-8")

        self.assertIn("fw-logo", app_source)
        self.assertIn("brand-mark", app_source)
        self.assertIn("ForWin Archive", app_source)
        self.assertIn("Canon、Graph、Proposal、人物性格 loadout", app_source)
        self.assertIn("--paper: #efe4d4", css_source)
        self.assertIn("--accent: #b24b31", css_source)
        self.assertIn("Source Serif 4", css_source)
        self.assertIn("Plus Jakarta Sans", css_source)

    def test_world_studio_exposes_personality_loadout_editor(self) -> None:
        app_source = (REPO_ROOT / "frontend/world-studio/src/App.tsx").read_text(encoding="utf-8")
        css_source = (REPO_ROOT / "frontend/world-studio/src/styles.css").read_text(encoding="utf-8")

        self.assertIn('"personality"', app_source)
        self.assertIn("人物性格", app_source)
        self.assertIn("/api/personality-skills", app_source)
        self.assertIn("/api/projects/${projectId}/proposals", app_source)
        self.assertIn("PersonalityLoadoutProposal", app_source)
        self.assertNotIn("/book-state/characters/${selectedCharacterId}/personality-loadout", app_source)
        self.assertIn("PersonalityEditor", app_source)
        self.assertIn(".personality-editor", css_source)

    def test_world_studio_exposes_personality_coverage_filters(self) -> None:
        app_source = (REPO_ROOT / "frontend/world-studio/src/App.tsx").read_text(encoding="utf-8")
        css_source = (REPO_ROOT / "frontend/world-studio/src/styles.css").read_text(encoding="utf-8")

        self.assertIn("characters/personality/coverage", app_source)
        self.assertIn("PersonalityCoveragePanel", app_source)
        self.assertIn("missing_loadout", app_source)
        self.assertIn("stress_mode_without_trigger", app_source)
        self.assertIn(".coverage-panel", css_source)

    def test_world_studio_exposes_final_gap_closure_personality_workflow(self) -> None:
        app_source = (REPO_ROOT / "frontend/world-studio/src/App.tsx").read_text(encoding="utf-8")
        css_source = (REPO_ROOT / "frontend/world-studio/src/styles.css").read_text(encoding="utf-8")

        self.assertIn("CharacterCreateForm", app_source)
        self.assertIn("PersonalityPreviewPanel", app_source)
        self.assertIn("AssignmentReportView", app_source)
        self.assertIn("ActiveContextPreview", app_source)
        self.assertIn("characters/personality/active-context/preview", app_source)
        self.assertIn("characters/personality/relationships/enrich", app_source)
        self.assertIn("characters/personality/metrics", app_source)
        self.assertIn("personality/reassign", app_source)
        self.assertIn(".character-create-form", css_source)
        self.assertIn(".assignment-report", css_source)

    def test_world_studio_exposes_projection_graph_search_and_page_proposals(self) -> None:
        app_source = (REPO_ROOT / "frontend/world-studio/src/App.tsx").read_text(encoding="utf-8")
        css_source = (REPO_ROOT / "frontend/world-studio/src/styles.css").read_text(encoding="utf-8")

        self.assertIn('type TabKey = "pages" | "graph" | "search" | "proposals" | "personality"', app_source)
        self.assertIn("GraphView", app_source)
        self.assertIn("SearchResultsPanel", app_source)
        self.assertIn("PageEditor", app_source)
        self.assertIn("ContextPanel", app_source)
        self.assertIn("/api/projects/${projectId}/world-studio/search", app_source)
        self.assertIn("Manual Notes", app_source)
        self.assertIn("Human Questions", app_source)
        self.assertIn("Proposed Correction", app_source)
        self.assertIn("createPageProposal", app_source)
        self.assertIn(".graph-view", css_source)
        self.assertIn(".search-results", css_source)
        self.assertIn(".page-editor", css_source)
        self.assertIn(".context-panel", css_source)


if __name__ == "__main__":
    unittest.main()
