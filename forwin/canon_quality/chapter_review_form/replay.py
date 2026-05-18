from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.canon_quality.service import analyze_writer_output_quality
from forwin.models import CandidateDraftRecord, ChapterDraft, ChapterPlan
from forwin.protocol.writer import WriterOutput

from .replay_state import ReplayRangeOptions, ReplayState, state_file_path, write_state_atomic


class ChapterDraftNotFound(RuntimeError):
    """Raised when canon replay cannot find an accepted draft for a chapter."""


class ReplayLLMUnavailable(RuntimeError):
    """Raised before replay when no LLM client is configured."""


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


class ReplayTokenUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    estimated: bool = True


class ReplayChapterResult(BaseModel):
    chapter_number: int
    mode: str
    status: str
    blocking: bool = False
    signal_counts_by_severity: dict[str, int] = Field(default_factory=dict)
    character_transitions_written: int = 0
    countdown_entries_written: int = 0
    validation_report_summary: dict[str, Any] = Field(default_factory=dict)
    candidate_rows: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    token_usage: ReplayTokenUsage = Field(default_factory=ReplayTokenUsage)
    error_message: str = ""


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


def replay_single_chapter(
    *,
    session: Session,
    project_id: str,
    chapter_number: int,
    llm_client: object | None,
    persist: bool,
    mode: str,
) -> ReplayChapterResult:
    if llm_client is None:
        raise ReplayLLMUnavailable("No LLM client configured for canon replay.")

    accepted = load_accepted_draft_ref(
        session=session,
        project_id=project_id,
        chapter_number=chapter_number,
    )
    writer_output = reconstruct_writer_output(
        session=session,
        project_id=project_id,
        chapter_number=chapter_number,
    )
    resolved_mode = "dry_run" if str(mode or "").strip().lower().replace("-", "_") == "dry_run" else "primary"
    result = analyze_writer_output_quality(
        session=session,
        project_id=project_id,
        chapter_number=chapter_number,
        writer_output=writer_output,
        draft_id=accepted.draft_id,
        persist=persist,
        mode=resolved_mode,
        llm_client=llm_client,
        return_raw_analyzer_results=True,
    )

    counts: dict[str, int] = {}
    for signal in result.signals:
        counts[signal.severity] = counts.get(signal.severity, 0) + 1
    report = dict(result.deterministic_quality_report or {})
    character_rows = _candidate_character_rows(result)
    countdown_rows = _candidate_countdown_rows(result)
    return ReplayChapterResult(
        chapter_number=int(chapter_number),
        mode=resolved_mode,
        status="success",
        blocking=bool(result.blocking),
        signal_counts_by_severity=counts,
        character_transitions_written=len(character_rows) if persist else 0,
        countdown_entries_written=len(countdown_rows) if persist else 0,
        validation_report_summary={
            "blocking": bool(report.get("blocking")),
            "review_issue_count": len(report.get("review_issues") or []),
        },
        candidate_rows={
            "signals": [signal.model_dump(mode="json") for signal in result.signals],
            "characters": character_rows,
            "countdowns": countdown_rows,
        },
    )


def _candidate_character_rows(result: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in result.raw_analyzer_results or []:
        for item in raw.get("character_transitions") or []:
            rows.append(dict(item))
    return rows


def _candidate_countdown_rows(result: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in result.raw_analyzer_results or []:
        for item in raw.get("countdown_entries") or []:
            rows.append(dict(item))
    if rows:
        return rows
    metrics = (result.deterministic_quality_report or {}).get("full_body_metrics") or {}
    return [dict(item) for item in metrics.get("countdown_mentions") or []]


def replay_chapter_range(
    *,
    session_factory,
    project_id: str,
    from_chapter: int,
    to_chapter: int,
    llm_client_factory,
    state_root: Path,
    options: ReplayRangeOptions,
) -> list[ReplayChapterResult]:
    """Replay a range sequentially.

    ``llm_client_factory`` is intentionally chapter-aware: production may return
    the same client for every chapter, while tests can return chapter-specific
    fake responses without changing production behavior.
    """
    path = state_file_path(
        root=state_root,
        project_id=project_id,
        from_chapter=from_chapter,
        to_chapter=to_chapter,
    )
    state = ReplayState.prepare_existing_state(
        path=path,
        project_id=project_id,
        from_chapter=from_chapter,
        to_chapter=to_chapter,
        resume=options.resume,
        force_restart=options.force_restart,
    )
    results: list[ReplayChapterResult] = []
    for chapter_number in range(int(from_chapter), int(to_chapter) + 1):
        if state.should_skip_completed(chapter_number, force_rerun=options.force_rerun):
            continue
        try:
            with session_factory() as session:
                result = replay_single_chapter(
                    session=session,
                    project_id=project_id,
                    chapter_number=chapter_number,
                    llm_client=llm_client_factory(chapter_number),
                    persist=options.persist,
                    mode=options.mode,
                )
                session.commit()
            results.append(result)
            state = state.mark_completed(chapter_number, result.model_dump(mode="json"))
            write_state_atomic(path, state)
        except Exception as exc:  # noqa: BLE001
            with session_factory() as session:
                session.rollback()
            state = state.mark_error(chapter_number, str(exc))
            write_state_atomic(path, state)
            if options.abort_on_error:
                break
    return results
