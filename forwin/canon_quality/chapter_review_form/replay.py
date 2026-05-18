from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.models import CandidateDraftRecord, ChapterDraft, ChapterPlan
from forwin.protocol.writer import WriterOutput


class ChapterDraftNotFound(RuntimeError):
    """Raised when canon replay cannot find an accepted draft for a chapter."""


@dataclass(frozen=True)
class AcceptedDraftRef:
    project_id: str
    chapter_number: int
    plan_id: str
    draft_id: str
    title: str
    body: str
    summary: str
    char_count: int


def load_accepted_draft_ref(*, session: Session, project_id: str, chapter_number: int) -> AcceptedDraftRef:
    row = session.execute(
        select(CandidateDraftRecord, ChapterDraft, ChapterPlan)
        .join(ChapterDraft, ChapterDraft.id == CandidateDraftRecord.candidate_draft_id)
        .join(ChapterPlan, ChapterPlan.id == CandidateDraftRecord.chapter_plan_id)
        .where(
            CandidateDraftRecord.project_id == project_id,
            CandidateDraftRecord.chapter_number == int(chapter_number),
            CandidateDraftRecord.status == "canon_committed",
            CandidateDraftRecord.canon_status == "canon",
        )
        .order_by(
            CandidateDraftRecord.version.desc(),
            CandidateDraftRecord.updated_at.desc(),
            ChapterDraft.version.desc(),
            ChapterDraft.created_at.desc(),
            CandidateDraftRecord.id.desc(),
        )
        .limit(1)
    ).first()
    if row is None:
        raise ChapterDraftNotFound(
            f"accepted draft not found for project={project_id} chapter={chapter_number}"
        )
    _candidate, draft, plan = row
    body = str(draft.body_text or "")
    if not body.strip():
        raise ChapterDraftNotFound(
            f"accepted draft body is empty for project={project_id} chapter={chapter_number}"
        )
    return AcceptedDraftRef(
        project_id=project_id,
        chapter_number=int(chapter_number),
        plan_id=str(plan.id or ""),
        draft_id=str(draft.id or ""),
        title=str(plan.title or f"第{chapter_number}章"),
        body=body,
        summary=str(draft.summary or ""),
        char_count=int(draft.char_count or len(body)),
    )


def reconstruct_writer_output(*, session: Session, project_id: str, chapter_number: int) -> WriterOutput:
    draft = load_accepted_draft_ref(session=session, project_id=project_id, chapter_number=chapter_number)
    return WriterOutput(
        project_id=draft.project_id,
        chapter_number=draft.chapter_number,
        title=draft.title,
        body=draft.body,
        char_count=draft.char_count,
        end_of_chapter_summary=draft.summary,
        prompt_revision_hash="replay",
        generation_meta={
            "source": "canon_replay",
            "draft_id": draft.draft_id,
            "chapter_plan_id": draft.plan_id,
        },
    )


def find_missing_accepted_chapters(
    *,
    session: Session,
    project_id: str,
    from_chapter: int,
    to_chapter: int,
) -> list[int]:
    missing: list[int] = []
    for chapter_number in range(int(from_chapter), int(to_chapter) + 1):
        try:
            load_accepted_draft_ref(session=session, project_id=project_id, chapter_number=chapter_number)
        except ChapterDraftNotFound:
            missing.append(chapter_number)
    return missing
