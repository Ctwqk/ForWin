from __future__ import annotations

import unittest

from forwin.api_runtime import _build_task_progress_changes


class ApiRuntimeProgressTests(unittest.TestCase):
    def test_nonterminal_generation_stages_keep_task_running(self) -> None:
        writing = _build_task_progress_changes(
            "stage_changed",
            {"stage": "writing_chapter", "current_chapter": 2},
        )
        chapter_failed = _build_task_progress_changes(
            "stage_changed",
            {"stage": "chapter_failed", "current_chapter": 1},
        )

        self.assertEqual(writing["status"], "running")
        self.assertEqual(chapter_failed["status"], "running")

    def test_terminal_failed_stage_marks_task_failed(self) -> None:
        changes = _build_task_progress_changes(
            "stage_changed",
            {"stage": "failed", "current_chapter": 2},
        )

        self.assertEqual(changes["status"], "failed")
        self.assertEqual(changes["current_stage"], "failed")


if __name__ == "__main__":
    unittest.main()
