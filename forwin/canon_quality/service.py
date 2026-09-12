from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.canon_quality.placeholder import (
    analyze_placeholder_leakage,
    extract_expected_protagonist_names,
)
from forwin.canon_quality.readability import analyze_writer_output_readability
from forwin.config import FormBlockingPolicy, InfrastructureConfig
from forwin.models import Entity, Project
from forwin.protocol.writer import WriterOutput

from .cache import (
    build_quality_analysis_cache_key,
    cache_json_value,
    persist_quality_projection,
    rebind_cache_payload,
    result_for_caller,
)
from .chapter_review_form.service import (
    DRY_RUN_RESULT_MODE,
    persist_form_artifact,
    review_chapter_with_form,
)
from .repository import CanonQualityRepository
from .signals import CanonQualitySignal, dedupe_signals
from .types import CanonQualityAnalysisResult, QualityAnalysisCachePayload


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
    use_cache: bool = True,
) -> CanonQualityAnalysisResult:
    config = InfrastructureConfig.from_env()
    resolved_mode = _normalize_form_mode(mode or "primary")
    repo = CanonQualityRepository(session)
    protagonist_names = _load_protagonist_names(session=session, project_id=project_id)
    project = session.get(Project, project_id)
    character_rows = repo.list_character_transitions(
        project_id,
        before_chapter=chapter_number,
    )
    countdown_rows = repo.list_countdown_entries(
        project_id,
        before_chapter=chapter_number,
        include_details=True,
    )
    open_signal_rows = repo.list_open_signals(
        project_id,
        before_chapter=chapter_number,
        limit=20,
    )
    from forwin.narrative_obligations.resolution_evidence import context_obligations
    obligations = context_obligations(session, project_id, chapter_number, draft_id=draft_id)
    quality_context = {
        "obligations": cache_json_value(obligations),
        "project_premise": str(getattr(project, "premise", "") or ""),
        "project_setting_summary": str(
            getattr(project, "setting_summary", "") or ""
        ),
        "target_total_chapters": int(
            getattr(project, "target_total_chapters", 0) or 0
        ),
        "protagonist_names": sorted(protagonist_names),
        "character_rows": cache_json_value(character_rows),
        "countdown_rows": cache_json_value(countdown_rows),
        "open_signal_rows": cache_json_value(open_signal_rows),
    }
    cache_key = build_quality_analysis_cache_key(
        session=session,
        project_id=project_id,
        chapter_number=chapter_number,
        writer_output=writer_output,
        mode=resolved_mode,
        llm_client=llm_client,
        config=config,
        quality_context=quality_context,
    )
    if use_cache and resolved_mode != DRY_RUN_RESULT_MODE:
        cached_row = repo.find_quality_analysis_run(**cache_key)
        if cached_row is not None:
            try:
                cached_payload = QualityAnalysisCachePayload.model_validate_json(
                    cached_row.result_json
                )
            except ValueError:
                session.delete(cached_row)
                session.flush()
            else:
                rebound_payload = rebind_cache_payload(cached_payload, draft_id=draft_id)
                if persist:
                    persist_quality_projection(
                        repo,
                        project_id=project_id,
                        chapter_number=chapter_number,
                        payload=rebound_payload,
                    )
                return result_for_caller(
                    rebound_payload.analysis,
                    return_raw_analyzer_results=return_raw_analyzer_results,
                )
    deterministic_signals = dedupe_signals(
        [
            *analyze_placeholder_leakage(
                project_id=project_id,
                chapter_number=chapter_number,
                draft_id=draft_id,
                body=str(writer_output.body or ""),
                summary=str(writer_output.end_of_chapter_summary or ""),
                expected_character_names=protagonist_names,
            ),
            *analyze_writer_output_readability(
                project_id=project_id,
                chapter_number=chapter_number,
                draft_id=draft_id,
                writer_output=writer_output,
                protagonist_names=protagonist_names,
            ),
        ]
    )
    if resolved_mode == "off":
        report = _quality_report(
            signals=deterministic_signals,
            countdown_entries=[],
            review_issues=[],
            raw_results=[],
            summary="chapter review form disabled; deterministic canon quality enabled",
        )
        result = CanonQualityAnalysisResult(
            project_id=project_id,
            chapter_number=chapter_number,
            draft_id=draft_id,
            signals=deterministic_signals,
            deterministic_quality_report=report,
            mode="off",
            summary="chapter review form disabled; deterministic canon quality enabled",
            blocking=any(signal.status == "open" and signal.severity == "error" for signal in deterministic_signals),
            confidence=1.0 if deterministic_signals else 0.0,
        )
        payload = QualityAnalysisCachePayload(analysis=result)
        if persist:
            persist_quality_projection(
                repo,
                project_id=project_id,
                chapter_number=chapter_number,
                payload=payload,
            )
        if use_cache:
            repo.save_quality_analysis_run(
                **cache_key,
                result=rebind_cache_payload(payload, draft_id="").model_dump(mode="json"),
            )
        return result_for_caller(
            result,
            return_raw_analyzer_results=return_raw_analyzer_results,
        )
    min_blocking_confidence = 0.8
    token_budget_chars = int(config.chapter_review_form_token_budget_chars or 8000)
    max_schema_retries = int(config.chapter_review_form_max_llm_retries or 1)
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
        blocking_policy=FormBlockingPolicy(),
        mode=resolved_mode,
        obligations=obligations,
        character_rows=character_rows,
        countdown_rows=countdown_rows,
        open_signal_rows=open_signal_rows,
        target_total_chapters=int(
            getattr(project, "target_total_chapters", 0) or 0
        ),
    )
    artifact_path = (
        persist_form_artifact(config.artifact_root, form_result)
        if persist and resolved_mode == DRY_RUN_RESULT_MODE
        else None
    )

    result_signals = (
        list(form_result.signals)
        if resolved_mode == DRY_RUN_RESULT_MODE
        else dedupe_signals([*deterministic_signals, *form_result.signals])
    )
    report = _quality_report(
        signals=result_signals,
        countdown_entries=form_result.countdown_entries,
        review_issues=form_result.review_issues,
        raw_results=form_result.raw_analyzer_results,
        summary=form_result.summary,
        mode=resolved_mode,
    )
    if artifact_path is not None:
        report["chapter_review_form_artifact"] = str(artifact_path)
    report["residual_open_signals"] = [
        signal.model_dump(mode="json")
        for signal in repo.list_open_signals(project_id, before_chapter=chapter_number, limit=20)
    ]
    result = CanonQualityAnalysisResult(
        project_id=project_id,
        chapter_number=chapter_number,
        draft_id=draft_id,
        signals=result_signals,
        deterministic_quality_report=report,
        mode=resolved_mode if resolved_mode == DRY_RUN_RESULT_MODE else "chapter_review_form",
        summary=form_result.summary,
        form=form_result.form,
        answers=form_result.answers,
        validation_report=form_result.validation_report,
        review_issues=form_result.review_issues,
        raw_analyzer_results=form_result.raw_analyzer_results,
        blocking=(
            form_result.blocking
            or (
                resolved_mode != DRY_RUN_RESULT_MODE
                and any(signal.status == "open" and signal.severity == "error" for signal in deterministic_signals)
            )
        ),
        confidence=max(
            float(form_result.confidence or 0.0),
            0.0 if resolved_mode == DRY_RUN_RESULT_MODE else 1.0 if deterministic_signals else 0.0,
        ),
    )
    payload = QualityAnalysisCachePayload(
        analysis=result,
        character_transitions=form_result.character_transitions,
        countdown_entries=form_result.countdown_entries,
    )
    if persist and resolved_mode != DRY_RUN_RESULT_MODE:
        persist_quality_projection(
            repo,
            project_id=project_id,
            chapter_number=chapter_number,
            payload=payload,
        )
    if use_cache and resolved_mode == "primary" and form_result.answers is not None:
        repo.save_quality_analysis_run(
            **cache_key,
            result=rebind_cache_payload(payload, draft_id="").model_dump(mode="json"),
        )
    return result_for_caller(
        result,
        return_raw_analyzer_results=return_raw_analyzer_results,
    )


def _normalize_form_mode(value: str | None) -> str:
    normalized = str(value or "primary").strip().lower().replace("-", "_")
    if normalized in {"off", "disabled"}:
        return "off"
    if normalized in {"dry_run", "dryrun", "shadow"}:
        return DRY_RUN_RESULT_MODE
    return "primary"


def _quality_report(
    *,
    signals: list[CanonQualitySignal],
    countdown_entries: list[Any],
    review_issues: list[dict[str, Any]],
    raw_results: list[dict[str, Any]],
    summary: str,
    mode: str = "primary",
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
        "mode": mode if mode == DRY_RUN_RESULT_MODE else "chapter_review_form",
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


def _load_protagonist_names(*, session: Session, project_id: str) -> set[str]:
    names: set[str] = set()
    project = session.get(Project, project_id)
    if project is not None:
        names.update(
            extract_expected_protagonist_names(
                str(getattr(project, "premise", "") or ""),
                str(getattr(project, "setting_summary", "") or ""),
            )
        )
    entity_rows = session.execute(
        select(Entity).where(
            Entity.project_id == project_id,
            Entity.kind == "character",
            Entity.is_active == True,  # noqa: E712
        )
    ).scalars().all()
    for entity in entity_rows:
        description = str(getattr(entity, "description", "") or "")
        if int(getattr(entity, "importance", 0) or 0) >= 9 or "主角" in description or "主人公" in description:
            name = str(getattr(entity, "name", "") or "").strip()
            if name:
                names.add(name)
    return names
