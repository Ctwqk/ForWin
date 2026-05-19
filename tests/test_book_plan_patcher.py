from __future__ import annotations

from forwin.planning.book_plan_patcher import BookPlanPatcher


def test_book_plan_patcher_creates_book_scope_patch() -> None:
    patch = BookPlanPatcher().build_patch(
        project_id="project-1",
        origin_chapter_number=12,
        issue_kind="book_structure_violation",
        summary="全书结构承诺需要调整。",
        source_signal_ids=["sig-book"],
        source_obligation_ids=[],
        payoff_test="终章前必须完成结构承诺。",
        affected_chapters=[13, 14],
    )

    assert patch.target_scope == "book"
    assert patch.patch_type == "book_defer_acceptance"
    assert patch.affected_chapters == [13, 14]
    assert patch.source_signal_ids == ["sig-book"]
    assert patch.expected_resolution_tests == ["终章前必须完成结构承诺。"]
