from __future__ import annotations

import unittest

from forwin.checker.rules import ContinuityChecker
from forwin.protocol.writer import WriterOutput


class FakeRepo:
    def get_active_entities(self, _project_id: str) -> list[object]:
        return []

    def get_thread_by_name(self, _project_id: str, _name: str) -> object | None:
        return None

    def get_allowed_entity_names(self, _project_id: str, _chapter_number: int) -> set[str]:
        return set()


class ContinuityCheckerTests(unittest.TestCase):
    def test_flags_chapter_body_that_ends_mid_sentence(self) -> None:
        checker = ContinuityChecker(FakeRepo(), min_chars=100, max_chars=5000)
        body = (
            "黑暗吞没一切。但在彻底失明前的最后一帧，"
            "林澈看清了编号下方那行"
        )

        verdict = checker.check(
            "project-1",
            WriterOutput(
                chapter_number=2,
                title="第一日·锚定医师",
                body=body * 8,
                end_of_chapter_summary="林澈发现线索。",
            ),
        )

        self.assertEqual(verdict.verdict, "fail")
        self.assertTrue(
            any(issue.rule_name == "body_incomplete_ending" for issue in verdict.issues)
        )


if __name__ == "__main__":
    unittest.main()
