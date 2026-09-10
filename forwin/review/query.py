from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.models.draft import ChapterDraft
from forwin.models.project import ChapterPlan
from forwin.protocol.context import ReviewNote
from forwin.state.query_helpers import (
    load_candidate_reviews_by_draft_id,
    load_latest_drafts_by_plan_id,
)


class ReviewQuery:
    """Read accepted chapter summaries and persisted review history."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def accepted_chapter_drafts(
        self,
        project_id: str,
        *,
        before_chapter: int,
        limit: int,
        band_start: int | None = None,
        band_end: int | None = None,
    ) -> list[tuple[ChapterPlan, ChapterDraft]]:
        """Return one active Canon draft per chapter, newest chapter first."""
        if int(limit) <= 0:
            return []
        statement = select(ChapterPlan).where(
            ChapterPlan.project_id == project_id,
            ChapterPlan.chapter_number < int(before_chapter),
            ChapterPlan.status == "accepted",
            ChapterPlan.active_commit_id.is_not(None),
        )
        if band_start is not None:
            statement = statement.where(ChapterPlan.chapter_number >= int(band_start))
        if band_end is not None:
            statement = statement.where(ChapterPlan.chapter_number <= int(band_end))
        plans = list(
            self.session.scalars(
                statement.order_by(ChapterPlan.chapter_number.desc())
                .limit(int(limit))
                .execution_options(populate_existing=True)
            )
        )
        drafts = load_latest_drafts_by_plan_id(
            self.session, [plan.id for plan in plans]
        )
        return [(plan, drafts[plan.id]) for plan in plans if plan.id in drafts]

    def chapter_summaries(
        self,
        project_id: str,
        *,
        before_chapter: int,
        limit: int = 3,
    ) -> list[str]:
        rows = self.accepted_chapter_drafts(
            project_id,
            before_chapter=before_chapter,
            limit=limit,
        )
        return [
            str(draft.summary)
            for _, draft in reversed(rows)
            if str(draft.summary or "").strip()
        ]

    def recent_notes(
        self,
        project_id: str,
        *,
        before_chapter: int,
        band_start: int | None = None,
        band_end: int | None = None,
        limit: int = 5,
    ) -> list[ReviewNote]:
        rows = self.accepted_chapter_drafts(
            project_id,
            before_chapter=before_chapter,
            limit=limit,
            band_start=band_start,
            band_end=band_end,
        )
        review_by_draft = load_candidate_reviews_by_draft_id(
            self.session,
            [draft.id for _, draft in rows],
        )
        notes: list[ReviewNote] = []
        for plan, draft in rows:
            review = review_by_draft.get(draft.id)
            if review is None:
                continue
            chapter_number = int(plan.chapter_number)
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
                    delivered_reward_tags=_string_list(
                        meta.get("delivered_reward_tags")
                    ),
                    review_notes=_string_list(meta.get("review_notes")),
                    evidence_refs=_string_list(meta.get("evidence_refs")),
                )
            )
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
