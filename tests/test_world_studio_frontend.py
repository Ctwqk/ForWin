from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class WorldStudioFrontendTests(unittest.TestCase):
    def test_world_studio_has_return_navigation_to_console(self) -> None:
        index_source = (REPO_ROOT / "frontend/world-studio/index.html").read_text(encoding="utf-8")
        main_source = (REPO_ROOT / "frontend/world-studio/src/main.tsx").read_text(encoding="utf-8")
        app_source = (REPO_ROOT / "frontend/world-studio/src/App.tsx").read_text(encoding="utf-8")
        css_source = (REPO_ROOT / "frontend/world-studio/src/styles.css").read_text(encoding="utf-8")
        route_source = (REPO_ROOT / "forwin/api_world_model_routes.py").read_text(encoding="utf-8")

        self.assertIn('<forwin-topbar active="world"></forwin-topbar>', index_source)
        self.assertNotIn('import "./topbar-runtime";', main_source)
        self.assertFalse((REPO_ROOT / "frontend/world-studio/src/topbar-runtime.ts").exists())
        self.assertIn("join_page_assets", route_source)
        self.assertIn("shared/topbar.css", route_source)
        self.assertIn("shared/i18n.js", route_source)
        self.assertIn("shared/forwin-topbar.js", route_source)
        self.assertNotIn('aria-label="ForWin primary navigation"', app_source)
        self.assertNotIn("nav-tabs nav-tabs--primary", app_source)
        self.assertIn("nav-tabs nav-tabs--secondary", app_source)
        self.assertNotIn("LanguageToggle", app_source)
        self.assertIn("forwin-lang", app_source)
        self.assertIn("World archive", app_source)
        self.assertIn("Books", (REPO_ROOT / "forwin/ui_assets/shared/i18n.js").read_text(encoding="utf-8"))
        self.assertIn("Tasks", (REPO_ROOT / "forwin/ui_assets/shared/i18n.js").read_text(encoding="utf-8"))
        self.assertIn("Archive", (REPO_ROOT / "forwin/ui_assets/shared/i18n.js").read_text(encoding="utf-8"))
        self.assertIn("Publish", (REPO_ROOT / "forwin/ui_assets/shared/i18n.js").read_text(encoding="utf-8"))
        self.assertIn("Settings", (REPO_ROOT / "forwin/ui_assets/shared/i18n.js").read_text(encoding="utf-8"))
        self.assertIn(".nav-tabs", css_source)
        self.assertIn(".nav-tabs--secondary", css_source)
        self.assertIn(".topbar-shell", css_source)
        self.assertNotIn(".lang-toggle", css_source)
        self.assertNotIn(".forwin-topbar-wrap", css_source)
        self.assertNotIn("studio-nav", app_source)
        self.assertNotIn(".studio-nav", css_source)

    def test_world_studio_route_html_injects_shared_topbar_assets(self) -> None:
        from forwin.api_world_model_routes import _world_studio_html

        html = _world_studio_html()

        self.assertIn('id="forwin-world-studio-shared-topbar"', html)
        self.assertIn("class ForwinTopbar extends HTMLElement", html)
        self.assertIn('<forwin-topbar active="world"></forwin-topbar>', html)
        self.assertEqual(html.count('id="forwin-world-studio-shared-topbar"'), 1)

    def test_world_studio_uses_forwin_design_system_baseline(self) -> None:
        app_source = (REPO_ROOT / "frontend/world-studio/src/App.tsx").read_text(encoding="utf-8")
        css_source = (REPO_ROOT / "frontend/world-studio/src/styles.css").read_text(encoding="utf-8")

        self.assertIn("fw-logo", app_source)
        self.assertIn("brand-mark", app_source)
        self.assertIn("ForWin Archive", app_source)
        self.assertIn("ForWin Archive\", en: \"ForWin Archive", app_source)
        self.assertIn('<h1>{t("archiveTitle")}</h1>', app_source)
        self.assertIn("World archive", app_source)
        self.assertIn("还没有页面", app_source)
        self.assertIn("先去 Genesis 锁定一个阶段，第 0 章会自动建好。", app_source)
        self.assertIn("还没有搜索结果", app_source)
        self.assertIn("先选索引范围，再输入关键词。", app_source)
        self.assertNotIn("masthead-copy", app_source)
        self.assertNotIn("Canon、Graph、Proposal、人物性格 loadout", app_source)
        self.assertNotIn("还没有世界档案页面", app_source)
        self.assertNotIn("World Studio Search", app_source)
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
