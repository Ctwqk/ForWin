from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.models.draft import CandidateDraftRecord, ChapterDraft, ChapterReview
from forwin.models.project import ChapterPlan
from forwin.protocol.writer import WriterOutput


def _dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _model_list(items: list[Any]) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for item in items or []:
        if hasattr(item, "model_dump"):
            payload.append(item.model_dump(mode="json"))
        elif isinstance(item, dict):
            payload.append(dict(item))
        else:
            payload.append({"value": str(item)})
    return payload


class CandidateDraftRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def latest_for_chapter(
        self,
        *,
        project_id: str,
        chapter_number: int,
    ) -> CandidateDraftRecord | None:
        return self.session.execute(
            select(CandidateDraftRecord)
            .where(
                CandidateDraftRecord.project_id == project_id,
                CandidateDraftRecord.chapter_number == int(chapter_number),
            )
            .order_by(CandidateDraftRecord.updated_at.desc(), CandidateDraftRecord.created_at.desc(), CandidateDraftRecord.id.desc())
            .limit(1)
        ).scalar_one_or_none()

    def upsert_from_review(
        self,
        *,
        project_id: str,
        chapter_plan: ChapterPlan,
        draft: ChapterDraft,
        review: ChapterReview,
        writer_output: WriterOutput,
        repair_attempt_count: int = 0,
    ) -> CandidateDraftRecord:
        row = self.session.execute(
            select(CandidateDraftRecord)
            .where(CandidateDraftRecord.candidate_draft_id == draft.id)
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            row = CandidateDraftRecord(
                project_id=project_id,
                chapter_plan_id=chapter_plan.id,
                chapter_number=int(chapter_plan.chapter_number or writer_output.chapter_number or 0),
                candidate_draft_id=draft.id,
            )
            self.session.add(row)
        row.project_id = project_id
        row.chapter_plan_id = chapter_plan.id
        row.chapter_number = int(chapter_plan.chapter_number or writer_output.chapter_number or 0)
        row.candidate_draft_id = draft.id
        row.review_id = review.id
        row.version = int(draft.version or 1)
        row.status = "reviewed"
        row.canon_status = "candidate"
        row.scene_outputs_json = _dump_json(_model_list(writer_output.scene_outputs))
        row.state_change_candidates_json = _dump_json(_model_list(writer_output.state_changes))
        row.event_candidates_json = _dump_json(_model_list(writer_output.new_events))
        row.thread_beat_candidates_json = _dump_json(_model_list(writer_output.thread_beats))
        row.repair_attempt_count = max(0, int(repair_attempt_count or 0))
        row.failure_reason = ""
        metadata = dict(writer_output.generation_meta or {})
        metadata.setdefault("title", writer_output.title)
        metadata.setdefault("summary", writer_output.end_of_chapter_summary)
        metadata.setdefault("char_count", int(writer_output.char_count or len(writer_output.body or "")))
        row.metadata_json = _dump_json(metadata)
        self.session.add(row)
        self.session.flush()
        return row

    def mark_canon_committed(
        self,
        *,
        project_id: str,
        chapter_number: int,
        canon_artifact_path: str = "",
    ) -> CandidateDraftRecord | None:
        row = self.latest_for_chapter(project_id=project_id, chapter_number=chapter_number)
        if row is None:
            return None
        row.status = "canon_committed"
        row.canon_status = "canon"
        row.canon_artifact_path = str(canon_artifact_path or "")
        row.failure_reason = ""
        self.session.add(row)
        self.session.flush()
        return row

    def mark_canon_failed(
        self,
        *,
        project_id: str,
        chapter_number: int,
        failure_reason: str,
        canon_artifact_path: str = "",
    ) -> CandidateDraftRecord | None:
        row = self.latest_for_chapter(project_id=project_id, chapter_number=chapter_number)
        if row is None:
            return None
        row.status = "canon_failed"
        row.canon_status = "candidate"
        row.failure_reason = str(failure_reason or "")
        row.canon_artifact_path = str(canon_artifact_path or "")
        self.session.add(row)
        self.session.flush()
        return row
