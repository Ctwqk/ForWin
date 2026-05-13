from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from forwin.models.draft import CandidateDraftRecord, ChapterDraft
from forwin.models.project import ChapterPlan, Project
from forwin.protocol.writer import WriterOutput

from .artifact_ledger import analyze_artifact_counts
from .character_state import analyze_character_state_transitions
from .countdown_ledger import analyze_countdowns
from .duplication import analyze_full_body_duplication
from .final_completion import analyze_final_completion
from .identity import analyze_identity_roles, extract_identity_role_facts
from .placeholder import analyze_placeholder_leakage, extract_expected_protagonist_names
from .repository import CanonQualityRepository
from .reveal_registry import analyze_reveals
from .signals import CanonQualitySignal
from .style import analyze_style_telemetry


class CanonQualityAnalysisResult(BaseModel):
    project_id: str
    chapter_number: int
    draft_id: str = ""
    signals: list[CanonQualitySignal] = Field(default_factory=list)
    deterministic_quality_report: dict[str, Any] = Field(default_factory=dict)


def analyze_writer_output_quality(
    *,
    session: Session,
    project_id: str,
    chapter_number: int,
    writer_output: WriterOutput,
    draft_id: str = "",
    persist: bool = False,
) -> CanonQualityAnalysisResult:
    repo = CanonQualityRepository(session)
    project = session.get(Project, project_id)
    is_final_chapter = _is_final_chapter(
        session=session,
        project=project,
        project_id=project_id,
        chapter_number=chapter_number,
        title=str(writer_output.title or ""),
        summary=str(writer_output.end_of_chapter_summary or ""),
    )
    body = str(writer_output.body or "")
    summary = str(writer_output.end_of_chapter_summary or "")

    signals: list[CanonQualitySignal] = []
    signals.extend(
        analyze_placeholder_leakage(
            project_id=project_id,
            chapter_number=chapter_number,
            draft_id=draft_id,
            body=body,
            summary=summary,
            expected_character_names=_expected_protagonist_names(project),
        )
    )

    character_signals, character_transitions = analyze_character_state_transitions(
        project_id=project_id,
        chapter_number=chapter_number,
        draft_id=draft_id,
        body=body,
        previous_transitions=repo.list_character_transitions(project_id, before_chapter=chapter_number),
        central_characters=_central_character_names(body),
    )
    signals.extend(character_signals)

    countdown_signals, countdown_entries = analyze_countdowns(
        project_id=project_id,
        chapter_number=chapter_number,
        draft_id=draft_id,
        body=body,
        previous_entries=repo.list_countdown_entries(project_id, before_chapter=chapter_number),
        is_final_chapter=is_final_chapter,
    )
    signals.extend(countdown_signals)

    signals.extend(
        analyze_final_completion(
            project_id=project_id,
            chapter_number=chapter_number,
            draft_id=draft_id,
            body=body,
            title=str(writer_output.title or ""),
            summary=summary,
            is_final_chapter=is_final_chapter,
        )
    )

    artifact_signals, artifact_entries = analyze_artifact_counts(
        project_id=project_id,
        chapter_number=chapter_number,
        draft_id=draft_id,
        body=body,
        previous_ledgers=[],
        target_total=_infer_artifact_target(project, body),
    )
    signals.extend(artifact_signals)

    reveal_signals, reveal_entries = analyze_reveals(
        project_id=project_id,
        chapter_number=chapter_number,
        draft_id=draft_id,
        reveal_claims=_reveal_claims(writer_output),
        previous_entries=[],
        body=body,
    )
    signals.extend(reveal_signals)

    duplicate_signals, body_metrics = analyze_full_body_duplication(
        project_id=project_id,
        chapter_number=chapter_number,
        draft_id=draft_id,
        body=body,
    )
    signals.extend(duplicate_signals)

    identity_text = "\n".join(part for part in (summary, body) if part)
    current_identity_names = {
        fact.character_name
        for fact in extract_identity_role_facts(
            project_id=project_id,
            chapter_number=chapter_number,
            draft_id=draft_id,
            text=identity_text,
        )
        if fact.character_name
    }
    identity_signals, _identity_facts = analyze_identity_roles(
        project_id=project_id,
        chapter_number=chapter_number,
        draft_id=draft_id,
        body=identity_text,
        previous_facts=_previous_identity_facts(
            session,
            project_id,
            before_chapter=chapter_number,
            known_names=current_identity_names,
        ),
        central_characters=_central_character_names(body),
    )
    signals.extend(identity_signals)

    style_signals, style_telemetry = analyze_style_telemetry(
        project_id=project_id,
        chapter_number=chapter_number,
        draft_id=draft_id,
        body=body,
        previous_metrics=[],
    )
    signals.extend(style_signals)
    body_metrics.style_motifs = list(style_telemetry.style_motifs)

    if persist:
        repo.save_signals(signals)
        repo.save_character_transitions(character_transitions)
        repo.save_countdown_entries(countdown_entries)
        repo.save_artifact_entries(artifact_entries)
        repo.save_reveal_entries(reveal_entries)
        repo.save_body_metrics(body_metrics)

    report = _quality_report(signals=signals, countdown_entries=countdown_entries)
    report["residual_open_signals"] = [
        signal.model_dump(mode="json")
        for signal in repo.list_open_signals(project_id, before_chapter=chapter_number, limit=20)
    ]
    return CanonQualityAnalysisResult(
        project_id=project_id,
        chapter_number=chapter_number,
        draft_id=draft_id,
        signals=signals,
        deterministic_quality_report=report,
    )


def _quality_report(*, signals: list[CanonQualitySignal], countdown_entries: list[Any]) -> dict[str, Any]:
    blocking = [signal.model_dump(mode="json") for signal in signals if signal.severity == "error" and signal.status == "open"]
    warnings = [signal.model_dump(mode="json") for signal in signals if signal.severity == "warning" and signal.status == "open"]
    return {
        "blocking_signals": blocking,
        "warning_signals": warnings,
        "open_obligations": [
            signal.model_dump(mode="json")
            for signal in signals
            if signal.signal_type.startswith("final_") and signal.status == "open"
        ],
        "ledger_conflicts": [
            signal.model_dump(mode="json")
            for signal in signals
            if signal.target_scope == "ledger" and signal.status == "open"
        ],
        "full_body_metrics": {
            "countdown_mentions": [getattr(item, "model_dump", lambda **_: {})(mode="json") for item in countdown_entries],
        },
    }


def _previous_identity_facts(
    session: Session,
    project_id: str,
    *,
    before_chapter: int,
    known_names: set[str] | None = None,
) -> list[Any]:
    rows = session.execute(
        select(CandidateDraftRecord, ChapterDraft)
        .join(ChapterDraft, ChapterDraft.id == CandidateDraftRecord.candidate_draft_id)
        .where(
            CandidateDraftRecord.project_id == project_id,
            CandidateDraftRecord.chapter_number < int(before_chapter or 0),
            CandidateDraftRecord.status == "canon_committed",
            CandidateDraftRecord.canon_status == "canon",
        )
        .order_by(CandidateDraftRecord.chapter_number.asc(), CandidateDraftRecord.updated_at.asc())
    ).all()
    facts: list[Any] = []
    names: set[str] = {str(name) for name in (known_names or set()) if str(name).strip()}
    for record, draft in rows:
        chapter_number = int(record.chapter_number or 0)
        text = "\n".join(part for part in (str(draft.summary or ""), str(draft.body_text or "")) if part)
        chapter_facts = extract_identity_role_facts(
            project_id=project_id,
            chapter_number=chapter_number,
            draft_id=str(draft.id or ""),
            text=text,
            known_names=names,
        )
        facts.extend(chapter_facts)
        names.update(fact.character_name for fact in chapter_facts if fact.character_name)
    return facts


def _is_final_chapter(
    *,
    session: Session,
    project: Project | None,
    project_id: str,
    chapter_number: int,
    title: str = "",
    summary: str = "",
) -> bool:
    current = int(chapter_number or 0)
    if current <= 0:
        return False
    target_total = int(getattr(project, "target_total_chapters", 0) or 0)
    if target_total and current >= target_total:
        return True
    max_materialized = session.execute(
        select(func.max(ChapterPlan.chapter_number)).where(ChapterPlan.project_id == project_id)
    ).scalar_one_or_none()
    if (
        max_materialized is not None
        and current >= int(max_materialized or 0)
        and _looks_like_final_chapter_label(title=title, summary=summary)
    ):
        return True
    return False


def _looks_like_final_chapter_label(*, title: str, summary: str) -> bool:
    text = f"{title}\n{summary}"
    return any(
        marker in text
        for marker in (
            "终章",
            "尾声",
            "大结局",
            "最终章",
            "最后一章",
            "最后一日",
            "最后一天",
            "最终决战",
            "finale",
            "Finale",
        )
    )


def _infer_artifact_target(project: Project | None, body: str) -> int:
    haystack = "\n".join(
        [
            str(getattr(project, "premise", "") or ""),
            str(getattr(project, "setting_summary", "") or ""),
            str(body or ""),
        ]
    )
    if "六十份档案" in haystack or "60份档案" in haystack or "60 份档案" in haystack:
        return 60
    match = re.search(r"([0-9]+)\s*份档案", haystack)
    if match:
        return int(match.group(1))
    return 0


def _reveal_claims(writer_output: WriterOutput) -> list[str]:
    claims: list[str] = []
    for event in getattr(writer_output, "reveal_events", []) or []:
        metadata = getattr(event, "metadata", {}) or {}
        for key in ("claim_summary", "summary", "claim"):
            value = str(metadata.get(key) or "").strip() if isinstance(metadata, dict) else ""
            if value:
                claims.append(value)
                break
        else:
            fallback = str(getattr(event, "reveals_fact_id", "") or getattr(event, "reveal_event_id", "") or "").strip()
            if fallback:
                claims.append(fallback)
    return list(dict.fromkeys(claims))


def _expected_protagonist_names(project: Project | None) -> set[str]:
    if project is None:
        return set()
    return extract_expected_protagonist_names(
        str(getattr(project, "premise", "") or ""),
        str(getattr(project, "setting_summary", "") or ""),
    )


def _central_character_names(body: str) -> set[str]:
    names = set()
    for candidate in ("沈砚", "林澈", "顾岚", "洛庭若", "林远", "林启明", "林远舟"):
        if candidate in body:
            names.add(candidate)
    return names
