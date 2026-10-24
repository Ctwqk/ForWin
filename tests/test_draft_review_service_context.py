from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from forwin.protocol.context import ChapterContextPack, ReaderFeedbackView
from forwin.review.context_builder import build_review_context_pack
from forwin.review.draft_service import DraftReviewService
from forwin.review.webnovel import WebNovelExperienceReviewer


def _context(**kwargs) -> ChapterContextPack:
    return ChapterContextPack(
        project_id="p", project_title="Title", premise="Premise", genre="test",
        setting_summary="Setting", chapter_number=3, chapter_plan_title="Chapter",
        chapter_plan_one_line="Action", chapter_goals=[], **kwargs,
    )


def test_draft_review_has_no_unwired_reviewer_dependencies() -> None:
    removed = {"continuity_reviewer", "llm_webnovel_reviewer"}
    assert removed.isdisjoint(inspect.signature(DraftReviewService).parameters)
    assert removed.isdisjoint(vars(DraftReviewService()))


def test_chapter_context_has_no_dead_fields() -> None:
    fields = ChapterContextPack.model_fields
    assert {"reader_feedback", "character_cognition_states", "promise_debts"}.isdisjoint(fields)
    assert not [name for name in fields if name.startswith("recent_")]


def test_review_loads_reader_feedback_from_repository() -> None:
    feedback = ReaderFeedbackView(comment_count=2)
    calls = []

    def get_feedback(project_id, *, before_chapter):
        calls.append((project_id, before_chapter))
        return feedback

    review = build_review_context_pack(
        repo=SimpleNamespace(get_recent_reader_feedback=get_feedback), context=_context(),
    )
    assert review.reader_feedback == feedback
    assert calls == [("p", 3)]
    assert build_review_context_pack(context=_context()).reader_feedback is None
    assert WebNovelExperienceReviewer()._normalize_context(_context()).reader_feedback is None


@pytest.mark.parametrize("direct", [False, True])
def test_review_preserves_obligation_and_audit_fallbacks(direct) -> None:
    obligations = [{"id": "obligation-1"}]
    audit = {"status": "needs_review"}
    values = {"active_narrative_obligations": obligations, "future_plan_audit_summary": audit}
    context = _context(**values) if direct else _context(canon_quality_context=values)
    review = build_review_context_pack(context=context)
    assert review.active_narrative_obligations == obligations
    assert review.future_plan_audit_summary == audit
