from __future__ import annotations

import re
import shutil
import os
import subprocess
import tempfile
import unittest

from forwin.api_pages import render_home_page, render_publishers_page


class ApiPagesRenderingTests(unittest.TestCase):
    def _assert_rendered_inline_scripts_parse(self, html: str) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is required for rendered page JavaScript syntax checks")

        scripts = re.findall(r"<script(?:[^>]*)>(.*?)</script>", html, flags=re.DOTALL)
        self.assertTrue(scripts, "expected rendered page to include inline scripts")

        with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as handle:
            handle.write("\n\n".join(scripts))
            script_path = handle.name

        try:
            result = subprocess.run(
                [node, "--check", script_path],
                check=False,
                capture_output=True,
                text=True,
            )
        finally:
            os.unlink(script_path)

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_home_page_uses_incremental_drawer_refresh_and_hides_raw_planned_label(self) -> None:
        html = render_home_page(
            has_api_key=False,
            base_url="https://api.minimaxi.com/v1",
            model="MiniMax-M2.7",
            operation_mode="blackbox",
            freeze_failed_candidates=True,
        )

        self.assertIn("refreshCurrentDrawerIfChanged", html)
        self.assertIn("chapterStatusLabel", html)
        self.assertIn("待生成正文", html)
        self.assertIn("config_generation_min_chapter_chars", html)
        self.assertIn("task_generation_min_chapter_chars", html)
        self.assertIn("task_generation_progression_mode", html)
        self.assertIn("task_generation_auto_band_checkpoint", html)
        self.assertIn('aria-label="ForWin primary navigation"', html)
        self.assertIn('class="nav-tabs nav-tabs--primary"', html)
        self.assertIn('class="nav-tab active"', html)
        self.assertIn('id="tab_book"', html)
        self.assertIn('id="tab_task"', html)
        self.assertIn('href="/world-studio"', html)
        self.assertIn(">世界档案</a>", html)
        self.assertIn('href="/publishers"', html)
        self.assertIn(">发布</a>", html)
        self.assertIn('id="tab_config"', html)
        self.assertNotIn("tab_world_v4", html)
        self.assertNotIn("panel_world_v4", html)
        self.assertNotIn("world_v4_debug_output", html)
        self.assertNotIn("V4 世界", html)
        self.assertNotIn("高级发布页", html)
        self.assertNotIn(">World Studio</a>", html)
        self.assertIn("浏览器扩展", html)
        self.assertIn("打开扩展设置", html)
        self.assertIn("下载扩展包（Chrome/Edge）", html)
        self.assertIn("下载 Firefox 扩展包", html)
        self.assertIn('href="/api/publishers/extension-package"', html)
        self.assertIn('href="/api/publishers/extension-package/firefox"', html)
        self.assertIn("Scenario Rehearsal", html)
        self.assertIn("Legacy Preview", html)
        self.assertIn("Candidate Draft Review", html)
        self.assertNotIn("loadWorldModelV4Debug", html)
        self.assertIn("saveProjectGovernanceFromDrawer", html)
        self.assertIn("renderDecisionTimeline", html)
        self.assertIn("renderCausalReplayCard", html)
        self.assertIn("renderGovernanceInsightsCard", html)
        self.assertIn("scope === 'arc'", html)
        self.assertIn("issue_group_distribution", html)
        self.assertIn("jumpToReviewDecisionChain", html)
        self.assertIn("jumpToCheckpointDecisionChain", html)
        self.assertIn("focusDecisionEvent", html)
        self.assertIn("范围筛选", html)
        self.assertIn("跳到阻断决策", html)
        self.assertIn("查看 Checkpoint 决策链", html)
        self.assertIn("Review 决策链", html)
        self.assertIn("governance_action_modal_shell", html)
        self.assertIn("submitGovernanceActionModal", html)
        self.assertIn("因果回放", html)
        self.assertIn("治理洞察", html)
        self.assertIn("future constraints 仅保存/展示", html)
        self.assertIn("editNarrativeConstraintFromDrawer", html)
        self.assertIn("archiveNarrativeConstraintFromDrawer", html)
        self.assertIn("PATCH", html)
        self.assertIn("task_modal_title", html)
        self.assertIn("task_modal_description", html)
        self.assertIn("继续生成", html)
        self.assertIn("继续生成目标", html)
        self.assertIn("fw-logo", html)
        self.assertIn("ForWin Workspace", html)
        self.assertIn('value="2500"', html)
        self.assertIn("chapterStatusLabel(chapter.status)", html)
        self.assertIn("function parseTextareaLines(value)", html)
        self.assertIn(
            "content_guardrails: parseTextareaLines(document.getElementById('book_form_content_guardrails').value)",
            html,
        )
        self.assertIn("genesis_refine_instruction", html)
        self.assertIn("refineGenesisCurrentStage", html)
        self.assertIn("refineGenesisSelectedItem", html)
        self.assertIn("genesis_model_profile_id", html)
        self.assertIn("changeGenesisModelProfile", html)
        self.assertIn("genesis_lock_stage_btn", html)
        self.assertIn("genesis_lock_stage_status", html)
        self.assertIn("genesis_save_stage_btn", html)
        self.assertIn("genesis_generate_stage_btn", html)
        self.assertIn("genesis_rerun_stage_btn", html)
        self.assertIn("genesis_refine_stage_btn", html)
        self.assertIn("genesis_stage_form", html)
        self.assertIn("genesis_item_form", html)
        self.assertIn("handleGenesisEditorInput", html)
        self.assertIn("applyGenesisLockedState", html)
        self.assertIn("isGenesisStageLocked", html)
        self.assertIn("最终 JSON / 高级编辑", html)
        self.assertIn("GENESIS_STAGE_ITEM_TARGETS", html)
        self.assertIn("genesis_item_collection_select", html)
        self.assertIn("collection: 'arcs'", html)
        self.assertIn("collection: 'regions'", html)
        self.assertIn("culture_traits", html)
        self.assertIn("parent_region_id", html)
        self.assertIn("home_subworld", html)
        self.assertIn("home_region", html)
        self.assertIn("current_region", html)
        self.assertIn("faction_memberships", html)
        self.assertIn("base_subworld", html)
        self.assertIn("headquarters_region", html)
        self.assertIn("footprint", html)
        self.assertIn("base_region", html)
        self.assertIn("backing_factions", html)
        self.assertIn('rel="icon"', html)
        self.assertIn("data:image/svg+xml", html)
        self.assertIn("object_list", html)
        self.assertIn("renderGenesisStageForm", html)
        self.assertIn("renderGenesisStructuredFields", html)
        self.assertIn("path: 'world_bible.overview'", html)
        self.assertIn("path: 'world_bible.axioms'", html)
        self.assertIn("path: 'world_bible.history_slice'", html)
        self.assertIn("collection: 'world_bible.culture_profiles'", html)
        self.assertIn("path: 'minimum_world_system'", html)
        self.assertIn("path: 'minimum_extension_pack'", html)
        self.assertIn("collection: 'institution_profiles'", html)
        self.assertIn("collection: 'resource_economy_profiles'", html)
        self.assertIn("collection: 'world_extensions.daily_life_profiles'", html)
        self.assertIn("collection: 'world_extensions.belief_mythos_profiles'", html)
        self.assertIn("collection: 'world_extensions.information_profiles'", html)
        self.assertIn("collection: 'world_extensions.ecology_profiles'", html)
        self.assertIn("collection: 'world_extensions.aesthetic_profiles'", html)
        self.assertIn("collection: 'world_extensions.secrets_codex'", html)
        self.assertIn("collection: 'world_extensions.value_conflicts'", html)
        self.assertIn("collection: 'world_extensions.story_interfaces'", html)
        self.assertIn("character_name_examples", html)
        self.assertIn("region_name_examples", html)
        self.assertIn("location_name_examples", html)
        self.assertIn("generator_civilization", html)
        self.assertIn("generator_overlays", html)
        self.assertIn("source: 'culture_profiles'", html)
        self.assertIn("culture_profile_id", html)
        self.assertIn("reference_value: 'name'", html)
        self.assertIn("name_generation_kind", html)
        self.assertIn("generateGenesisFieldValue", html)
        self.assertIn("自动生成", html)
        self.assertIn("/genesis/generate-name", html)
        self.assertIn("path: 'id', label: '小世界 ID'", html)
        self.assertIn("path: 'id', label: '地点 ID'", html)
        self.assertIn("path: 'id', label: '势力 ID'", html)
        self.assertIn("Arc 蓝图默认仍沿用自动生成", html)
        self.assertIn("const loc = Array.isArray(item.loc) ? item.loc.join('.') : '';", html)
        self.assertIn("JSON.stringify(detail, null, 2)", html)
        self.assertNotIn("第${chapter.chapter_number}章 ${chapter.status}", html)

    def test_home_page_renders_javascript_that_passes_node_syntax_check(self) -> None:
        html = render_home_page(
            has_api_key=True,
            base_url="https://api.minimaxi.com/v1",
            model="MiniMax-M2.7",
            operation_mode="blackbox",
            freeze_failed_candidates=True,
        )

        self._assert_rendered_inline_scripts_parse(html)

    def test_publishers_page_renders_javascript_that_passes_node_syntax_check(self) -> None:
        html = render_publishers_page(
            backend_ready={"extension_api_key_configured": True},
            extension_install_path="browser_extension/forwin-publisher",
        )

        self.assertIn("browser_extension/forwin-publisher", html)
        self.assertIn('aria-label="ForWin primary navigation"', html)
        self.assertIn('class="nav-tabs nav-tabs--primary"', html)
        self.assertIn('class="nav-tab"', html)
        self.assertIn('href="/">书本</a>', html)
        self.assertIn('href="/#task">任务</a>', html)
        self.assertIn('href="/world-studio">世界档案</a>', html)
        self.assertIn('aria-current="page">发布</a>', html)
        self.assertIn('href="/#config">配置</a>', html)
        self.assertIn("fw-logo", html)
        self.assertIn("ForWin Publisher", html)
        self.assertNotIn("高级发布页", html)
        self.assertNotIn("{extension_install_path}", html)
        self.assertNotIn("@@EXTENSION_INSTALL_PATH@@", html)
        self._assert_rendered_inline_scripts_parse(html)


if __name__ == "__main__":
    unittest.main()
