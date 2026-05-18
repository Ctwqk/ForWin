from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from forwin.config import Config
from forwin.protocol.writer import WriterOutput

from .chapter_review_form.service import review_chapter_with_form
from .repository import CanonQualityRepository
from .signals import CanonQualitySignal


class CanonQualityAnalysisResult(BaseModel):
    project_id: str
    chapter_number: int
    draft_id: str = ""
    signals: list[CanonQualitySignal] = Field(default_factory=list)
    deterministic_quality_report: dict[str, Any] = Field(default_factory=dict)
    mode: str = "chapter_review_form"
    summary: str = ""
    review_issues: list[dict[str, Any]] = Field(default_factory=list)
    raw_analyzer_results: list[dict[str, Any]] = Field(default_factory=list)
    blocking: bool = False
    confidence: float = 0.0


def analyze_writer_output_quality(
    *,
    session: Session,
    project_id: str,
    chapter_number: int,
    writer_output: WriterOutput,
    draft_id: str = "",
    persist: bool = False,
    mode: str | None = None,
    llm_client: object | None = None,
    return_raw_analyzer_results: bool = False,
) -> CanonQualityAnalysisResult:
    config = Config.from_env()
    resolved_mode = _normalize_form_mode(mode or config.chapter_review_form_mode)
    if resolved_mode == "off":
        report = _quality_report(
            signals=[],
            countdown_entries=[],
            review_issues=[],
            raw_results=[],
            summary="chapter review form disabled",
        )
        return CanonQualityAnalysisResult(
            project_id=project_id,
            chapter_number=chapter_number,
            draft_id=draft_id,
            deterministic_quality_report=report,
            mode="off",
            summary="chapter review form disabled",
        )
    min_blocking_confidence = float(config.chapter_review_form_min_blocking_confidence or 0.8)
    token_budget_chars = int(config.chapter_review_form_token_budget_chars or 8000)
    max_schema_retries = int(config.chapter_review_form_max_llm_retries or 1)
    repo = CanonQualityRepository(session)
    form_result = review_chapter_with_form(
        session=session,
        project_id=project_id,
        chapter_number=chapter_number,
        writer_output=writer_output,
        draft_id=draft_id,
        llm_client=llm_client,
        min_blocking_confidence=min_blocking_confidence,
        token_budget_chars=token_budget_chars,
        max_schema_retries=max_schema_retries,
        blocking_policy=config.form_blocking_policy,
    )
    if persist:
        repo.supersede_chapter_signals(project_id, chapter_number)
        repo.save_signals(form_result.signals)
        repo.save_character_transitions(form_result.character_transitions)
        repo.save_countdown_entries(form_result.countdown_entries)

    report = _quality_report(
        signals=form_result.signals,
        countdown_entries=form_result.countdown_entries,
        review_issues=form_result.review_issues,
        raw_results=form_result.raw_analyzer_results,
        summary=form_result.summary,
    )
    report["residual_open_signals"] = [
        signal.model_dump(mode="json")
        for signal in repo.list_open_signals(project_id, before_chapter=chapter_number, limit=20)
    ]
    return CanonQualityAnalysisResult(
        project_id=project_id,
        chapter_number=chapter_number,
        draft_id=draft_id,
        signals=form_result.signals,
        deterministic_quality_report=report,
        mode="chapter_review_form",
        summary=form_result.summary,
        review_issues=form_result.review_issues,
        raw_analyzer_results=form_result.raw_analyzer_results if return_raw_analyzer_results else [],
        blocking=form_result.blocking,
        confidence=form_result.confidence,
    )


def _normalize_form_mode(value: str | None) -> str:
    normalized = str(value or "primary").strip().lower()
    if normalized in {"off", "disabled"}:
        return "off"
    return "primary"


def _quality_report(
    *,
    signals: list[CanonQualitySignal],
    countdown_entries: list[Any],
    review_issues: list[dict[str, Any]],
    raw_results: list[dict[str, Any]],
    summary: str,
) -> dict[str, Any]:
    blocking = [
        signal.model_dump(mode="json")
        for signal in signals
        if signal.severity == "error" and signal.status == "open"
    ]
    warnings = [
        signal.model_dump(mode="json")
        for signal in signals
        if signal.severity == "warning" and signal.status == "open"
    ]
    return {
        "mode": "chapter_review_form",
        "summary": summary,
        "blocking": bool(blocking),
        "blocking_signals": blocking,
        "warning_signals": warnings,
        "open_obligations": [
            signal.model_dump(mode="json")
            for signal in signals
            if "obligation" in signal.signal_type and signal.status == "open"
        ],
        "ledger_conflicts": [
            signal.model_dump(mode="json")
            for signal in signals
            if "countdown" in signal.signal_type and signal.status == "open"
        ],
        "full_body_metrics": {
            "countdown_mentions": [
                getattr(item, "model_dump", lambda **_: {})(mode="json")
                for item in countdown_entries
            ],
        },
        "review_issues": list(review_issues),
        "chapter_review_form_results": list(raw_results),
    }
