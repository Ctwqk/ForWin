from __future__ import annotations

import unittest

from forwin.api_pages import render_home_page


class ApiPagesRenderingTests(unittest.TestCase):
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
        self.assertIn('value="2500"', html)
        self.assertIn("chapterStatusLabel(chapter.status)", html)
        self.assertNotIn("第${chapter.chapter_number}章 ${chapter.status}", html)


if __name__ == "__main__":
    unittest.main()
