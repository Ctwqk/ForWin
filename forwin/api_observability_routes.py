from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable

from fastapi import HTTPException
from sqlalchemy import select

from forwin.api_schemas import (
    ArtifactManifestItem,
    ArtifactReadResponse,
    ChapterLedgerResponse,
    PerformanceSpanInfo,
    PromptTraceDetailResponse,
    StageDurationAggregate,
    TaskTimelineResponse,
)
from forwin.config import Config
from forwin.models.draft import ChapterDraft
from forwin.models.genesis import PromptTrace
from forwin.models.governance import DecisionEvent
from forwin.models.project import ChapterPlan
from forwin.models.task import GenerationTask
from forwin.observability.query_service import ObservabilityQueryService


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
    def query_service() -> ObservabilityQueryService:
        return ObservabilityQueryService(
            session_factory=get_session,
            display_datetime=display_datetime,
        )

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
                stage_durations=stage_duration_aggregates(rows),
                operation_ids=list(
                    dict.fromkeys(
                        [normalized_task_id]
                        + [
                            item
                            for row in rows
                            for item in collect_operation_ids(json_load_object(row.payload_json))
                        ]
                    )
                ),
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
                normalized_key = str(key)
                if (
                    normalized_key == "uri"
                    or normalized_key.endswith("_uri")
                    or normalized_key.endswith("_path")
                    or "artifact" in normalized_key
                ):
                    if isinstance(item, str) and item.strip():
                        found.append(item.strip())
                found.extend(collect_artifact_uris(item))
        elif isinstance(value, list):
            for item in value:
                found.extend(collect_artifact_uris(item))
        return found

    def collect_operation_ids(value: Any) -> list[str]:
        found: list[str] = []
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key) == "operation_id" and isinstance(item, str) and item.strip():
                    found.append(item.strip())
                found.extend(collect_operation_ids(item))
        elif isinstance(value, list):
            for item in value:
                found.extend(collect_operation_ids(item))
        return found

    def stage_duration_aggregates(event_rows: list[DecisionEvent]) -> list[StageDurationAggregate]:
        grouped: dict[str, dict[str, int]] = {}
        for event in event_rows:
            payload = json_load_object(event.payload_json)
            event_type = str(event.event_type or "")
            if event_type not in {"stage_duration_summary", "stage_exited"} and "duration_ms" not in payload:
                continue
            try:
                duration_ms = max(0, int(payload.get("duration_ms") or 0))
            except (TypeError, ValueError):
                continue
            if duration_ms <= 0:
                continue
            stage = str(payload.get("stage") or event_type or "unknown").strip() or "unknown"
            current = grouped.setdefault(
                stage,
                {"event_count": 0, "total_duration_ms": 0, "max_duration_ms": 0, "last_duration_ms": 0},
            )
            current["event_count"] += 1
            current["total_duration_ms"] += duration_ms
            current["max_duration_ms"] = max(current["max_duration_ms"], duration_ms)
            current["last_duration_ms"] = duration_ms
        return [
            StageDurationAggregate(stage=stage, **values)
            for stage, values in sorted(grouped.items(), key=lambda item: item[0])
        ]

    def normalize_artifact_manifest_item(
        value: Any,
        *,
        source_event_id: str = "",
        trace_id: str = "",
    ) -> ArtifactManifestItem | None:
        if not isinstance(value, dict):
            return None
        uri = str(value.get("uri") or value.get("artifact_uri") or "").strip()
        if not uri:
            return None
        try:
            size = max(0, int(value.get("size") or 0))
        except (TypeError, ValueError):
            size = 0
        return ArtifactManifestItem(
            uri=uri,
            kind=str(value.get("kind") or value.get("artifact_kind") or "").strip(),
            redaction_state=str(value.get("redaction_state") or "").strip(),
            source_event_id=str(value.get("source_event_id") or source_event_id or "").strip(),
            trace_id=str(value.get("trace_id") or trace_id or "").strip(),
            hash=str(value.get("hash") or "").strip(),
            size=size,
        )

    def collect_artifact_manifest(
        value: Any,
        *,
        source_event_id: str = "",
        trace_id: str = "",
    ) -> list[ArtifactManifestItem]:
        found: list[ArtifactManifestItem] = []
        if isinstance(value, dict):
            items = value.get("artifact_manifest")
            if isinstance(items, list):
                for item in items:
                    normalized = normalize_artifact_manifest_item(
                        item,
                        source_event_id=source_event_id,
                        trace_id=trace_id,
                    )
                    if normalized is not None:
                        found.append(normalized)
            elif items is not None:
                normalized = normalize_artifact_manifest_item(
                    items,
                    source_event_id=source_event_id,
                    trace_id=trace_id,
                )
                if normalized is not None:
                    found.append(normalized)
            normalized_self = normalize_artifact_manifest_item(
                value,
                source_event_id=source_event_id,
                trace_id=trace_id,
            )
            if normalized_self is not None and (
                normalized_self.redaction_state or normalized_self.kind or normalized_self.source_event_id
            ):
                found.append(normalized_self)
            for item in value.values():
                found.extend(
                    collect_artifact_manifest(item, source_event_id=source_event_id, trace_id=trace_id)
                )
        elif isinstance(value, list):
            for item in value:
                found.extend(
                    collect_artifact_manifest(item, source_event_id=source_event_id, trace_id=trace_id)
                )
        return found

    def dedupe_manifest(items: list[ArtifactManifestItem]) -> list[ArtifactManifestItem]:
        deduped: dict[tuple[str, str, str], ArtifactManifestItem] = {}
        for item in items:
            key = (item.uri, item.kind, item.source_event_id)
            if item.uri and key not in deduped:
                deduped[key] = item
        return list(deduped.values())

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
            operation_ids: list[str] = []
            artifact_manifest: list[ArtifactManifestItem] = []
            for trace in traces:
                input_snapshot = json_load_object(trace.input_snapshot_json)
                output_summary = json_load_object(trace.output_summary_json)
                trace_chapter = int(input_snapshot.get("chapter_number") or output_summary.get("chapter_number") or 0)
                if trace_chapter in {0, normalized_chapter}:
                    trace_ids.append(trace.id)
                    artifact_uris.extend(collect_artifact_uris(input_snapshot))
                    artifact_uris.extend(collect_artifact_uris(output_summary))
                    operation_ids.extend(collect_operation_ids(input_snapshot))
                    operation_ids.extend(collect_operation_ids(output_summary))
                    artifact_manifest.extend(collect_artifact_manifest(input_snapshot, trace_id=trace.id))
                    artifact_manifest.extend(collect_artifact_manifest(output_summary, trace_id=trace.id))
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
                payload = json_load_object(event.payload_json)
                artifact_uris.extend(collect_artifact_uris(payload))
                if str(event.task_id or "").strip():
                    operation_ids.append(str(event.task_id or "").strip())
                operation_ids.extend(collect_operation_ids(payload))
                artifact_manifest.extend(
                    collect_artifact_manifest(payload, source_event_id=str(event.id or ""))
                )
            deduped_artifacts = list(dict.fromkeys(item for item in artifact_uris if item))
            return ChapterLedgerResponse(
                project_id=normalized_project_id,
                chapter_number=normalized_chapter,
                plan_status=str(plan.status or ""),
                events=[serialize_decision_event(row) for row in event_rows],
                prompt_trace_ids=list(dict.fromkeys(trace_ids)),
                artifact_uris=deduped_artifacts,
                stage_durations=stage_duration_aggregates(event_rows),
                operation_ids=list(dict.fromkeys(item for item in operation_ids if item)),
                artifact_manifest=dedupe_manifest(artifact_manifest),
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

    def get_task_performance_report(task_id: str):
        normalized_task_id = str(task_id or "").strip()
        if not normalized_task_id:
            raise HTTPException(404, "任务不存在")
        return query_service().task_performance_report(normalized_task_id)

    def get_project_performance_report(project_id: str, limit: int = 1000):
        normalized_project_id = str(project_id or "").strip()
        if not normalized_project_id:
            raise HTTPException(404, "项目不存在")
        return query_service().project_performance_report(normalized_project_id, limit=limit)

    def get_chapter_performance_report(project_id: str, chapter_number: int, limit: int = 1000):
        normalized_project_id = str(project_id or "").strip()
        normalized_chapter = int(chapter_number or 0)
        if not normalized_project_id or normalized_chapter <= 0:
            raise HTTPException(404, "章节不存在")
        return query_service().chapter_performance_report(
            normalized_project_id,
            normalized_chapter,
            limit=limit,
        )

    def get_slow_performance_spans(
        project_id: str = "",
        task_id: str = "",
        limit: int = 50,
    ) -> list[PerformanceSpanInfo]:
        return query_service().slow_spans(
            project_id=str(project_id or "").strip(),
            task_id=str(task_id or "").strip(),
            limit=max(1, min(200, int(limit or 50))),
        )

    def get_llm_performance_report(project_id: str = "", days: int = 7):
        return query_service().llm_performance_report(
            project_id=str(project_id or "").strip(),
            days=max(1, int(days or 7)),
        )

    def get_db_performance_report(project_id: str = "", days: int = 7):
        return query_service().db_performance_report(
            project_id=str(project_id or "").strip(),
            days=max(1, int(days or 7)),
        )

    return {
        "get_task_timeline": get_task_timeline,
        "get_chapter_observability_ledger": get_chapter_observability_ledger,
        "get_prompt_trace_detail": get_prompt_trace_detail,
        "read_artifact_preview": read_artifact_preview,
        "get_task_performance_report": get_task_performance_report,
        "get_project_performance_report": get_project_performance_report,
        "get_chapter_performance_report": get_chapter_performance_report,
        "get_slow_performance_spans": get_slow_performance_spans,
        "get_llm_performance_report": get_llm_performance_report,
        "get_db_performance_report": get_db_performance_report,
    }
