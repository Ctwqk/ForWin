from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable

from fastapi import HTTPException
from sqlalchemy import select

from forwin.api_schemas import (
    ArtifactReadResponse,
    ChapterLedgerResponse,
    PromptTraceDetailResponse,
    TaskTimelineResponse,
)
from forwin.config import Config
from forwin.models.draft import ChapterDraft
from forwin.models.genesis import PromptTrace
from forwin.models.governance import DecisionEvent
from forwin.models.project import ChapterPlan
from forwin.models.task import GenerationTask


def build_handlers(
    *,
    get_config: Callable[[], Any],
    get_session: Callable[[], Any],
    list_decision_event_rows: Callable[..., list[DecisionEvent]],
    serialize_decision_event: Callable[[DecisionEvent], Any],
    display_datetime: Callable[[Any], str],
    json_load_object: Callable[[str | None], dict[str, Any]],
    json_load_list: Callable[[str | None], list[Any]],
) -> dict[str, Callable[..., Any]]:
    def serialize_prompt_trace_detail(row: PromptTrace) -> PromptTraceDetailResponse:
        return PromptTraceDetailResponse(
            id=row.id,
            trace_scope=str(row.trace_scope or "genesis"),
            stage_key=str(row.stage_key or ""),
            template_id=str(row.template_id or ""),
            template_version=str(row.template_version or "v1"),
            effective_system_prompt=str(row.effective_system_prompt or ""),
            prompt_layers=json_load_list(row.prompt_layers_json),
            input_snapshot=json_load_object(row.input_snapshot_json),
            model_profile=json_load_object(row.model_profile_json),
            attempts=json_load_list(row.attempts_json),
            output_summary=json_load_object(row.output_summary_json),
            decision_event_id=str(row.decision_event_id or ""),
            parent_trace_id=str(row.parent_trace_id or ""),
            created_at=display_datetime(row.created_at),
        )

    def get_task_timeline(task_id: str) -> TaskTimelineResponse:
        normalized_task_id = str(task_id or "").strip()
        if not normalized_task_id:
            raise HTTPException(404, "任务不存在")
        with get_session() as session:
            task = session.get(GenerationTask, normalized_task_id)
            project_id = str(getattr(task, "project_id", "") or "")
            if not project_id:
                first_event = session.execute(
                    select(DecisionEvent)
                    .where(DecisionEvent.task_id == normalized_task_id)
                    .order_by(DecisionEvent.created_at.asc(), DecisionEvent.id.asc())
                    .limit(1)
                ).scalar_one_or_none()
                project_id = str(getattr(first_event, "project_id", "") or "")
            if not project_id:
                raise HTTPException(404, "任务不存在")
            rows = list_decision_event_rows(
                session,
                project_id=project_id,
                task_id=normalized_task_id,
                limit=500,
                ascending=True,
            )
            rows = sorted(
                rows,
                key=lambda row: (
                    0 if not str(getattr(row, "parent_event_id", "") or "").strip() else 1,
                    str(getattr(row, "created_at", "") or ""),
                    str(getattr(row, "id", "") or ""),
                ),
            )
            return TaskTimelineResponse(
                task_id=normalized_task_id,
                project_id=project_id,
                events=[serialize_decision_event(row) for row in rows],
            )

    def get_prompt_trace_detail(trace_id: str) -> PromptTraceDetailResponse:
        normalized_trace_id = str(trace_id or "").strip()
        if not normalized_trace_id:
            raise HTTPException(404, "prompt trace 不存在")
        with get_session() as session:
            row = session.get(PromptTrace, normalized_trace_id)
            if row is None:
                raise HTTPException(404, "prompt trace 不存在")
            return serialize_prompt_trace_detail(row)

    def collect_artifact_uris(value: Any) -> list[str]:
        found: list[str] = []
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).endswith("_uri") or str(key).endswith("_path") or "artifact" in str(key):
                    if isinstance(item, str) and item.strip():
                        found.append(item.strip())
                found.extend(collect_artifact_uris(item))
        elif isinstance(value, list):
            for item in value:
                found.extend(collect_artifact_uris(item))
        return found

    def get_chapter_observability_ledger(project_id: str, chapter_number: int) -> ChapterLedgerResponse:
        normalized_project_id = str(project_id or "").strip()
        normalized_chapter = int(chapter_number or 0)
        if not normalized_project_id or normalized_chapter <= 0:
            raise HTTPException(404, "章节不存在")
        with get_session() as session:
            plan = session.execute(
                select(ChapterPlan).where(
                    ChapterPlan.project_id == normalized_project_id,
                    ChapterPlan.chapter_number == normalized_chapter,
                )
            ).scalar_one_or_none()
            if plan is None:
                raise HTTPException(404, "章节不存在")
            event_rows = list_decision_event_rows(
                session,
                project_id=normalized_project_id,
                chapter_number=normalized_chapter,
                limit=500,
                ascending=True,
            )
            traces = session.execute(
                select(PromptTrace)
                .where(PromptTrace.project_id == normalized_project_id)
                .order_by(PromptTrace.created_at.asc(), PromptTrace.id.asc())
                .limit(500)
            ).scalars().all()
            trace_ids: list[str] = []
            artifact_uris: list[str] = []
            for trace in traces:
                input_snapshot = json_load_object(trace.input_snapshot_json)
                output_summary = json_load_object(trace.output_summary_json)
                trace_chapter = int(input_snapshot.get("chapter_number") or output_summary.get("chapter_number") or 0)
                if trace_chapter in {0, normalized_chapter}:
                    trace_ids.append(trace.id)
                    artifact_uris.extend(collect_artifact_uris(input_snapshot))
                    artifact_uris.extend(collect_artifact_uris(output_summary))
            drafts = session.execute(
                select(ChapterDraft)
                .where(ChapterDraft.chapter_plan_id == plan.id)
                .order_by(ChapterDraft.created_at.asc(), ChapterDraft.id.asc())
            ).scalars().all()
            for draft in drafts:
                raw_response = str(draft.llm_raw_response or "").strip()
                if raw_response:
                    artifact_uris.append(raw_response)
            for event in event_rows:
                artifact_uris.extend(collect_artifact_uris(json_load_object(event.payload_json)))
            deduped_artifacts = list(dict.fromkeys(item for item in artifact_uris if item))
            return ChapterLedgerResponse(
                project_id=normalized_project_id,
                chapter_number=normalized_chapter,
                plan_status=str(plan.status or ""),
                events=[serialize_decision_event(row) for row in event_rows],
                prompt_trace_ids=list(dict.fromkeys(trace_ids)),
                artifact_uris=deduped_artifacts,
            )

    def read_artifact_preview(uri: str, preview_chars: int = 20000) -> ArtifactReadResponse:
        normalized_uri = str(uri or "").strip()
        if not normalized_uri:
            raise HTTPException(404, "artifact 不存在")
        config = get_config() or Config.from_env()
        root = Path(config.artifact_root).resolve()
        path = Path(normalized_uri).expanduser()
        try:
            resolved = path.resolve()
        except OSError as exc:
            raise HTTPException(400, "artifact URI 无效") from exc
        if root not in {resolved, *resolved.parents}:
            raise HTTPException(403, "artifact URI 不在允许读取范围内")
        if not resolved.is_file():
            raise HTTPException(404, "artifact 不存在")
        raw = resolved.read_bytes()
        text = raw.decode("utf-8", errors="replace")
        limit = max(1, min(int(preview_chars or 20000), 20000))
        preview = text[:limit]
        return ArtifactReadResponse(
            uri=normalized_uri,
            size=len(raw),
            hash=hashlib.sha256(raw).hexdigest(),
            preview=preview,
            truncated=len(text) > len(preview),
        )

    return {
        "get_task_timeline": get_task_timeline,
        "get_chapter_observability_ledger": get_chapter_observability_ledger,
        "get_prompt_trace_detail": get_prompt_trace_detail,
        "read_artifact_preview": read_artifact_preview,
    }
