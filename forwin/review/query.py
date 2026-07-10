from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.models.draft import ChapterDraft, ChapterReview
from forwin.models.project import ChapterPlan
from forwin.protocol.context import ReviewNote


class ReviewQuery:
    """Read accepted chapter summaries and persisted review history."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def chapter_summaries(
        self,
        project_id: str,
        *,
        before_chapter: int,
        limit: int = 3,
    ) -> list[str]:
        rows = self.session.execute(
            select(ChapterDraft.summary, ChapterPlan.chapter_number)
            .join(ChapterPlan, ChapterDraft.chapter_plan_id == ChapterPlan.id)
            .where(
                ChapterPlan.project_id == project_id,
                ChapterPlan.chapter_number < int(before_chapter),
                ChapterPlan.status == "accepted",
            )
            .order_by(ChapterPlan.chapter_number.desc(), ChapterDraft.version.desc())
            .limit(max(0, int(limit)))
        ).all()
        return [str(summary) for summary, _ in reversed(rows) if str(summary or "").strip()]

    def recent_notes(
        self,
        project_id: str,
        *,
        before_chapter: int,
        band_start: int | None = None,
        band_end: int | None = None,
        limit: int = 5,
    ) -> list[ReviewNote]:
        rows = self.session.execute(
            select(ChapterReview, ChapterDraft, ChapterPlan)
            .join(ChapterDraft, ChapterDraft.id == ChapterReview.draft_id)
            .join(ChapterPlan, ChapterPlan.id == ChapterDraft.chapter_plan_id)
            .where(
                ChapterPlan.project_id == project_id,
                ChapterPlan.chapter_number < int(before_chapter),
                ChapterPlan.status == "accepted",
            )
            .order_by(
                ChapterPlan.chapter_number.desc(),
                ChapterReview.created_at.desc(),
            )
        ).all()
        notes: list[ReviewNote] = []
        seen_chapters: set[int] = set()
        for review, draft, plan in rows:
            chapter_number = int(plan.chapter_number)
            if chapter_number in seen_chapters:
                continue
            if band_start is not None and chapter_number < int(band_start):
                continue
            if band_end is not None and chapter_number > int(band_end):
                continue
            meta = _json_object(review.review_meta_json)
            issues = _json_list(review.issues_json)
            notes.append(
                ReviewNote(
                    chapter_number=chapter_number,
                    verdict=str(review.verdict or ""),
                    summary=str(meta.get("review_summary") or draft.summary or ""),
                    issue_types=[
                        str(item.get("issue_type") or item.get("rule_name") or "")
                        for item in issues
                        if isinstance(item, dict)
                    ],
                    planned_reward_tags=_string_list(meta.get("planned_reward_tags")),
                    delivered_reward_tags=_string_list(meta.get("delivered_reward_tags")),
                    review_notes=_string_list(meta.get("review_notes")),
                    evidence_refs=_string_list(meta.get("evidence_refs")),
                )
            )
            seen_chapters.add(chapter_number)
            if len(notes) >= int(limit):
                break
        return notes


def _json_object(raw: str) -> dict:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _json_list(raw: str) -> list:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def _string_list(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if str(item or "").strip()]


__all__ = ["ReviewQuery"]
