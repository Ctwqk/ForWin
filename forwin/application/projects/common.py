from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select

from forwin.api_schema import (
    ChapterInfo,
    ProjectDetail,
    ProjectExtendGenerationRequest,
)
from forwin.generation.continue_workset import (
    ContinueWorkset,
)
from forwin.generation.task_repository import TERMINAL_GENERATION_STATUSES
from forwin.audit.events import (
    DecisionEventType,
)
from forwin.models.draft import ChapterReview
from forwin.models.genesis import PromptTrace
from forwin.models.audit import DecisionEvent
from forwin.models.project import ChapterPlan, Project
from forwin.models.task import GenerationTask
from forwin.protocol.review import normalize_repair_scope
from forwin.state.query_helpers import (
    load_latest_drafts_by_plan_id,
    load_latest_rewrite_attempts_by_chapter,
)


_DEFAULT_CHAPTER_PAGE_LIMIT = 60
_MAX_CHAPTER_PAGE_LIMIT = 200


def _continue_workset_http_error(workset: ContinueWorkset) -> HTTPException:
    if workset.reason == "pending_review_blocker":
        return HTTPException(409, "仍有章节等待 review")
    if workset.reason == "pending_acceptance_blocker":
        return HTTPException(409, "仍有章节等待接受")
    if workset.reason == "project_completed":
        return HTTPException(400, "项目已完成，没有剩余章节需要继续生成")
    return HTTPException(400, "没有剩余章节需要继续生成")


def _load_json_object(raw: str, default):
    try:
        value = json.loads(raw or "")
    except (json.JSONDecodeError, TypeError):
        return default
    return value if isinstance(value, type(default)) else default


def _latest_active_generation_task(session, project_id: str) -> GenerationTask | None:
    return session.execute(
        select(GenerationTask)
        .where(
            GenerationTask.deleted_at.is_(None),
            GenerationTask.task_kind == "generation",
            GenerationTask.project_id == project_id,
            GenerationTask.status.notin_(tuple(TERMINAL_GENERATION_STATUSES)),
        )
        .order_by(GenerationTask.updated_at.desc(), GenerationTask.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def _overlay_active_generation_task(
    detail: ProjectDetail, task: GenerationTask | None
) -> ProjectDetail:
    if task is None:
        return detail
    stage = str(task.current_stage or "queued").strip() or "queued"
    current_chapter = int(task.current_chapter or 0)
    pause_requested = bool(getattr(task, "pause_requested", False))
    status = str(task.status or "").strip()
    detail.latest_stage = stage
    detail.next_gate = ""
    detail.generation_control = detail.generation_control.model_copy(
        update={
            "current_stage": stage,
            "current_chapter": current_chapter,
            "can_pause": status in {"queued", "starting", "running"}
            and not pause_requested,
            "can_resume": status == "paused",
            "pause_requested": pause_requested,
            "next_gate": "",
        }
    )
    return detail


def _new_operation_id(value: str = "") -> str:
    normalized = str(value or "").strip()
    return normalized or uuid.uuid4().hex


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return (
            value.astimezone(timezone.utc).isoformat()
            if value.tzinfo
            else value.isoformat()
        )
    return value


def _serialize_model_row(row) -> dict[str, Any]:  # noqa: ANN001
    return {
        column.name: _jsonable(getattr(row, column.name))
        for column in row.__table__.columns
    }


def _export_project_audit_bundle(
    *,
    session,
    config,
    project: Project,
    operation_id: str,
    test_run_id: str = "",
) -> str:
    artifact_root = Path(
        str(getattr(config, "artifact_root", "data/artifacts") or "data/artifacts")
    )
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    bundle_dir = artifact_root / "audit_bundles" / "projects" / project.id
    bundle_dir.mkdir(parents=True, exist_ok=True)
    path = bundle_dir / f"{timestamp}_{operation_id}.json"
    decision_events = (
        session.execute(
            select(DecisionEvent)
            .where(DecisionEvent.project_id == project.id)
            .order_by(DecisionEvent.created_at.asc(), DecisionEvent.id.asc())
        )
        .scalars()
        .all()
    )
    prompt_traces = (
        session.execute(
            select(PromptTrace)
            .where(PromptTrace.project_id == project.id)
            .order_by(PromptTrace.created_at.asc(), PromptTrace.id.asc())
        )
        .scalars()
        .all()
    )
    tasks = (
        session.execute(
            select(GenerationTask)
            .where(GenerationTask.project_id == project.id)
            .order_by(GenerationTask.created_at.asc(), GenerationTask.id.asc())
        )
        .scalars()
        .all()
    )
    chapter_plans = (
        session.execute(
            select(ChapterPlan)
            .where(ChapterPlan.project_id == project.id)
            .order_by(ChapterPlan.chapter_number.asc(), ChapterPlan.id.asc())
        )
        .scalars()
        .all()
    )
    payload = {
        "schema_version": "v3.8",
        "bundle_type": "project_delete_audit",
        "project_id": project.id,
        "project_title": project.title,
        "operation_id": operation_id,
        "test_run_id": str(test_run_id or "").strip(),
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "audit_events": [
            {
                "event_type": DecisionEventType.PROJECT_DELETE_REQUESTED,
                "operation_id": operation_id,
                "test_run_id": str(test_run_id or "").strip(),
            },
            {
                "event_type": DecisionEventType.AUDIT_BUNDLE_EXPORTED,
                "operation_id": operation_id,
                "uri": str(path),
            },
        ],
        "project": _serialize_model_row(project),
        "generation_tasks": [_serialize_model_row(row) for row in tasks],
        "chapter_plans": [_serialize_model_row(row) for row in chapter_plans],
        "decision_events": [_serialize_model_row(row) for row in decision_events],
        "prompt_traces": [
            {
                "id": row.id,
                "trace_scope": row.trace_scope,
                "stage_key": row.stage_key,
                "template_id": row.template_id,
                "decision_event_id": row.decision_event_id,
                "parent_trace_id": row.parent_trace_id,
                "model_profile": _load_json_object(row.model_profile_json, {}),
                "attempts": _load_json_object(row.attempts_json, []),
                "output_summary": _load_json_object(row.output_summary_json, {}),
                "created_at": _jsonable(row.created_at),
            }
            for row in prompt_traces
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def _extension_arc_synopsis(
    *,
    req: ProjectExtendGenerationRequest,
    start_chapter: int,
    end_chapter: int,
) -> str:
    title = (
        str(req.arc_title or "").strip()
        or f"续写弧线：第{start_chapter}-{end_chapter}章"
    )
    parts = [title]
    for raw in (req.arc_synopsis, req.continuity_guard):
        value = str(raw or "").strip()
        if value:
            parts.append(value)
    return "\n".join(parts)


def _normalize_chapter_page(offset: int, limit: int) -> tuple[int, int]:
    try:
        normalized_offset = max(0, int(offset or 0))
    except (TypeError, ValueError):
        normalized_offset = 0
    try:
        normalized_limit = int(limit or _DEFAULT_CHAPTER_PAGE_LIMIT)
    except (TypeError, ValueError):
        normalized_limit = _DEFAULT_CHAPTER_PAGE_LIMIT
    normalized_limit = min(_MAX_CHAPTER_PAGE_LIMIT, max(1, normalized_limit))
    return normalized_offset, normalized_limit


def _chapter_infos_for_plans(
    session, project_id: str, plans: list[ChapterPlan]
) -> list[ChapterInfo]:
    draft_map = load_latest_drafts_by_plan_id(session, [plan.id for plan in plans])
    review_draft_ids = (
        {
            draft_id
            for draft_id in session.execute(
                select(ChapterReview.draft_id)
                .where(
                    ChapterReview.draft_id.in_(
                        [draft.id for draft in draft_map.values()]
                    )
                )
                .distinct()
            )
            .scalars()
            .all()
        }
        if draft_map
        else set()
    )
    latest_attempt_map = load_latest_rewrite_attempts_by_chapter(
        session,
        project_id,
        [int(plan.chapter_number or 0) for plan in plans],
    )

    result = []
    for plan in plans:
        draft = draft_map.get(plan.id)
        latest_attempt = latest_attempt_map.get(plan.chapter_number)
        result.append(
            ChapterInfo(
                chapter_number=plan.chapter_number,
                title=plan.title,
                status=plan.status,
                char_count=draft.char_count if draft else 0,
                summary=draft.summary if draft else "",
                has_draft=draft is not None,
                has_review=bool(draft and draft.id in review_draft_ids),
                acceptance_mode=str(getattr(plan, "acceptance_mode", "") or ""),
                repair_attempt_count=int(getattr(plan, "repair_attempt_count", 0) or 0),
                canon_risk_level=str(getattr(plan, "canon_risk_level", "") or ""),
                latest_repair_scope=normalize_repair_scope(
                    getattr(latest_attempt, "repair_scope", ""),
                    default="",
                ),
            )
        )
    return result


__all__ = [
    name
    for name, value in globals().items()
    if name.startswith("_") and callable(value)
] + []
