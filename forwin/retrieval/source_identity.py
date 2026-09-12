"""Read fences and proof of active, immutable Canon text sources."""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
from hashlib import sha256
from sqlalchemy import select, event
from forwin.models import Project, ChapterPlan, ChapterDraft
from forwin.models.canon import CanonCommitRecord
from forwin.models.draft import CandidateDraftRecord
from forwin.protocol.context import MemorySnippet


@contextmanager
def fresh_orm_reads(session):
    """Refresh selected ORM rows, preserving normal autoflush and pending writes."""
    if session is None:
        yield
        return

    def refresh_selected_rows(execution):
        if execution.is_select:
            execution.update_execution_options(populate_existing=True)

    event.listen(session, "do_orm_execute", refresh_selected_rows)
    try:
        yield
    finally:
        event.remove(session, "do_orm_execute", refresh_selected_rows)


class CanonBaselineChanged(RuntimeError):
    """The caller must discard the entire context, never individual fragments."""


@dataclass(frozen=True, slots=True)
class CanonReadBaseline:
    project_id: str
    book_revision: int
    as_of_chapter: int

    @classmethod
    def capture(cls, session, project_id: str, *, as_of_chapter: int):
        revision = session.scalar(
            select(Project.book_revision).where(Project.id == project_id)
        )
        if revision is None:
            raise CanonBaselineChanged("Canon project is unavailable")
        return cls(project_id, int(revision), max(0, int(as_of_chapter)))

    def assert_current(self, session, *, project_id=None, as_of_chapter=None):
        if (
            (project_id is not None and project_id != self.project_id)
            or (as_of_chapter is not None and int(as_of_chapter) != self.as_of_chapter)
            or self.capture(session, self.project_id, as_of_chapter=self.as_of_chapter)
            != self
        ):
            raise CanonBaselineChanged("Canon read baseline changed")


def text_hash(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def embedding_input(title: str, summary: str, body: str) -> str:
    return f"{title}\n{summary}\n{body[:500]}"


def active_sources(session, baseline: CanonReadBaseline, *, commit_ids=None):
    """Batch join checks ownership at every edge; no latest-draft fallback."""
    baseline.assert_current(session)
    stmt = (
        select(CanonCommitRecord, CandidateDraftRecord, ChapterDraft)
        .join(
            ChapterPlan,
            (ChapterPlan.active_commit_id == CanonCommitRecord.id)
            & (ChapterPlan.id == CanonCommitRecord.chapter_plan_id)
            & (ChapterPlan.project_id == CanonCommitRecord.project_id)
            & (ChapterPlan.chapter_number == CanonCommitRecord.chapter_number),
        )
        .join(
            CandidateDraftRecord,
            (CandidateDraftRecord.id == CanonCommitRecord.candidate_id)
            & (CandidateDraftRecord.project_id == ChapterPlan.project_id)
            & (CandidateDraftRecord.chapter_plan_id == ChapterPlan.id)
            & (CandidateDraftRecord.chapter_number == ChapterPlan.chapter_number),
        )
        .join(
            ChapterDraft,
            (ChapterDraft.id == CandidateDraftRecord.candidate_draft_id)
            & (ChapterDraft.chapter_plan_id == ChapterPlan.id),
        )
        .where(
            CanonCommitRecord.project_id == baseline.project_id,
            CanonCommitRecord.status == "committed",
            ChapterPlan.status == "accepted",
            CandidateDraftRecord.status == "accepted",
            CanonCommitRecord.chapter_number <= baseline.as_of_chapter,
        )
    )
    if commit_ids is not None:
        stmt = stmt.where(CanonCommitRecord.id.in_(commit_ids))
    sources = {}
    for commit, candidate, draft in session.execute(
        stmt.execution_options(populate_existing=True)
    ):
        if not candidate.body_hash or candidate.body_hash != text_hash(draft.body_text):
            continue
        title = commit.chapter_title or ""
        sources[commit.id] = MemorySnippet(
            project_id=baseline.project_id,
            chapter_number=commit.chapter_number,
            title=title,
            summary=draft.summary or "",
            excerpt=draft.body_text[:500],
            canon_commit_id=commit.id,
            candidate_id=candidate.id,
            draft_id=draft.id,
            body_hash=candidate.body_hash,
            embedding_input_hash=text_hash(
                embedding_input(title, draft.summary or "", draft.body_text)
            ),
        )
    baseline.assert_current(session)
    return sources


def validate_memories(session, baseline, memories):
    sources = active_sources(
        session,
        baseline,
        commit_ids={m.canon_commit_id for m in memories if m.canon_commit_id},
    )
    result = []
    fields = (
        "project_id",
        "chapter_number",
        "canon_commit_id",
        "candidate_id",
        "draft_id",
        "body_hash",
        "embedding_input_hash",
    )
    for memory in memories:
        source = sources.get(memory.canon_commit_id)
        if (
            source is not None
            and memory.embedding_identity
            and all(getattr(source, key) == getattr(memory, key) for key in fields)
        ):
            result.append(
                source.model_copy(
                    update={
                        "score": memory.score,
                        "embedding_identity": memory.embedding_identity,
                    }
                )
            )
    return result
