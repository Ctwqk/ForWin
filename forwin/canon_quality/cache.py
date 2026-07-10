from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.config import InfrastructureConfig
from forwin.models import ChapterPlan
from forwin.protocol.writer import WriterOutput

from .chapter_review_form import FORM_SCHEMA_VERSION
from .repository import CanonQualityRepository
from .types import CanonQualityAnalysisResult, QualityAnalysisCachePayload


QUALITY_ANALYSIS_VERSION = "v1"


def build_quality_analysis_cache_key(
    *,
    session: Session,
    project_id: str,
    chapter_number: int,
    writer_output: WriterOutput,
    mode: str,
    llm_client: object | None,
    config: InfrastructureConfig,
    quality_context: dict[str, Any],
) -> dict[str, Any]:
    content_payload = {
        "chapter_number": int(chapter_number or 0),
        "title": str(writer_output.title or ""),
        "body": str(writer_output.body or ""),
        "end_of_chapter_summary": str(writer_output.end_of_chapter_summary or ""),
    }
    plan = session.execute(
        select(ChapterPlan).where(
            ChapterPlan.project_id == project_id,
            ChapterPlan.chapter_number == int(chapter_number or 0),
        )
    ).scalar_one_or_none()
    plan_payload = (
        {
            "id": str(plan.id or ""),
            "arc_plan_id": str(plan.arc_plan_id or ""),
            "title": str(plan.title or ""),
            "one_line": str(plan.one_line or ""),
            "goals_json": str(plan.goals_json or "[]"),
            "task_contract_json": str(plan.task_contract_json or "[]"),
            "experience_plan_json": str(plan.experience_plan_json or "{}"),
        }
        if plan is not None
        else {"chapter_plan": "absent"}
    )
    analyzer_payload = {
        "quality_analysis_version": QUALITY_ANALYSIS_VERSION,
        "form_schema_version": FORM_SCHEMA_VERSION,
        "mode": mode,
        "client_type": (
            f"{type(llm_client).__module__}.{type(llm_client).__qualname__}"
            if llm_client is not None
            else "none"
        ),
        "profile_id": str(getattr(llm_client, "profile_id", "") or ""),
        "model": str(getattr(llm_client, "model", "") or ""),
        "token_budget_chars": int(config.chapter_review_form_token_budget_chars or 8000),
        "max_schema_retries": int(config.chapter_review_form_max_llm_retries or 1),
        "quality_context": quality_context,
    }
    return {
        "project_id": project_id,
        "chapter_number": int(chapter_number or 0),
        "content_hash": _stable_hash(content_payload),
        "plan_fingerprint": _stable_hash(plan_payload),
        "analysis_mode": mode,
        "analyzer_fingerprint": _stable_hash(analyzer_payload),
    }


def cache_json_value(value: Any) -> Any:
    if isinstance(value, list):
        return [cache_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): cache_json_value(item) for key, item in value.items()}
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    return value


def rebind_cache_payload(
    payload: QualityAnalysisCachePayload,
    *,
    draft_id: str,
) -> QualityAnalysisCachePayload:
    report = dict(payload.analysis.deterministic_quality_report)
    for key in (
        "blocking_signals",
        "warning_signals",
        "open_obligations",
        "ledger_conflicts",
        "full_body_metrics",
        "chapter_review_form_results",
    ):
        if key in report:
            report[key] = _replace_draft_ids(report[key], draft_id=draft_id)
    analysis = payload.analysis.model_copy(
        update={
            "draft_id": draft_id,
            "signals": [
                signal.model_copy(
                    update={"payload": {**signal.payload, "draft_id": draft_id}}
                )
                for signal in payload.analysis.signals
            ],
            "deterministic_quality_report": report,
            "raw_analyzer_results": _replace_draft_ids(
                payload.analysis.raw_analyzer_results,
                draft_id=draft_id,
            ),
        }
    )
    return QualityAnalysisCachePayload(
        analysis=analysis,
        character_transitions=[
            item.model_copy(
                update={"payload": {**item.payload, "draft_id": draft_id}}
            )
            for item in payload.character_transitions
        ],
        countdown_entries=[
            item.model_copy(
                update={"payload": {**item.payload, "draft_id": draft_id}}
            )
            for item in payload.countdown_entries
        ],
    )


def persist_quality_projection(
    repo: CanonQualityRepository,
    *,
    project_id: str,
    chapter_number: int,
    payload: QualityAnalysisCachePayload,
) -> None:
    repo.supersede_chapter_signals(project_id, chapter_number)
    repo.save_signals(payload.analysis.signals)
    repo.save_character_transitions(payload.character_transitions)
    repo.save_countdown_entries(payload.countdown_entries)


def result_for_caller(
    result: CanonQualityAnalysisResult,
    *,
    return_raw_analyzer_results: bool,
) -> CanonQualityAnalysisResult:
    if return_raw_analyzer_results:
        return result
    return result.model_copy(update={"raw_analyzer_results": []})


def _stable_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _replace_draft_ids(value: Any, *, draft_id: str) -> Any:
    if isinstance(value, dict):
        return {
            key: draft_id if key == "draft_id" else _replace_draft_ids(item, draft_id=draft_id)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_replace_draft_ids(item, draft_id=draft_id) for item in value]
    return value


__all__ = [
    "QualityAnalysisCachePayload",
    "build_quality_analysis_cache_key",
    "cache_json_value",
    "persist_quality_projection",
    "rebind_cache_payload",
    "result_for_caller",
]
