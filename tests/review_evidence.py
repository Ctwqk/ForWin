"""Frozen-review fixtures explicitly capture Canon when the review is created."""
import json
from dataclasses import asdict

from forwin.models.draft import ChapterDraft
from forwin.models.project import ChapterPlan
from forwin.retrieval.source_identity import CanonReadBaseline


def bind_frozen_review(session, review):
    draft = session.get(ChapterDraft, review.draft_id)
    chapter = session.get(ChapterPlan, draft.chapter_plan_id)
    baseline = CanonReadBaseline.capture(session, chapter.project_id,
                                        as_of_chapter=max(0, chapter.chapter_number - 1))
    metadata = json.loads(review.review_meta_json or "{}")
    metadata["canon_read_baseline"] = asdict(baseline)
    review.review_meta_json = json.dumps(metadata)
