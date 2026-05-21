from __future__ import annotations

import inspect
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select

from forwin.api_project_payloads import build_project_detail, build_project_summaries, normalize_project_automation
from forwin.api_runtime import build_saved_runtime_config, copy_config
from forwin.candidate_drafts import CandidateDraftRepository
from forwin.api_schemas import (
    BookGenesisDetail,
    BookGenesisNameGenerateRequest,
    BookGenesisNameGenerateResponse,
    BookGenesisPatchRequest,
    BookGenesisRefineRequest,
    BookGenesisStageRunRequest,
    BulkDeleteResponse,
    CandidateDraftDetail,
    ChapterDetail,
    ChapterListResponse,
    ChapterReviewApproveRequest,
    ChapterReviewApproveResponse,
    ChapterReviewDetail,
    ChapterReviewRetryRequest,
    ChapterRewriteAttemptInfo,
    ChapterReviewIssueInfo,
    ChapterInfo,
    FinalGateDecisionInfo,
    LintSignalInfo,
    ProjectAutomationUpdateRequest,
    ProjectAutomationUpdateResponse,
    ProjectBulkDeleteRequest,
    ProjectChapterPublishRequest,
    ProjectContinueGenerationRequest,
    ProjectCreateRequest,
    ProjectCreateResponse,
    ProjectDeleteResponse,
    ProjectDetail,
    ProjectExtendGenerationRequest,
    ProjectSummary,
    PublisherUploadJobResponse,
    RepairVerificationInfo,
    StartWritingRequest,
    StartWritingResponse,
    TaskResponse,
)
from forwin.book_genesis import GENESIS_STAGE_ORDER, StaleGenesisRevisionError
from forwin.generation.continue_workset import (
    ContinueGenerationWorkset,
    build_continue_generation_workset,
)
from forwin.genesis_handoff import StartWritingCommand
from forwin.governance import (
    DecisionEventInfo,
    DecisionEventType,
    derive_chapter_task_contract,
    new_project_governance,
    plan_task_contract_to_json,
)
from forwin.map.genesis_adapter import build_subworld_map_specs_from_genesis
from forwin.map.models import MapNodeRow
from forwin.map.service import build_interconnections_from_genesis_atlas, create_or_update_book_map
from forwin.models.draft import ChapterDraft, ChapterReview
from forwin.models.genesis import PromptTrace
from forwin.models.governance import DecisionEvent
from forwin.observability.payloads import audit_payload
from forwin.models.phase import ChapterRewriteAttempt
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from forwin.models.task import GenerationTask
from forwin.protocol.experience import ChapterExperiencePlan
from forwin.protocol.review import normalize_repair_scope
from forwin.state.query_helpers import load_latest_drafts_by_plan_id, load_latest_rewrite_attempts_by_chapter
from forwin.state.updater import StateUpdater


_DEFAULT_CHAPTER_PAGE_LIMIT = 60
_MAX_CHAPTER_PAGE_LIMIT = 200
_GENERATION_TASK_TERMINAL_STATUSES = {
    "completed",
    "partial_failed",
    "failed",
    "needs_review",
    "cancelled",
    "paused",
}

def _continue_workset_http_error(workset: ContinueGenerationWorkset) -> HTTPException:
    if workset.reason == "pending_review_blocker":
        return HTTPException(409, "仍有章节等待 review")
    if workset.reason == "pending_acceptance_blocker":
        return HTTPException(409, "仍有章节等待接受")
    if workset.reason == "project_completed":
        return HTTPException(400, "项目已完成，没有剩余章节需要继续生成")
    return HTTPException(400, "没有剩余章节需要继续生成")

def call_task_factory_with_supported_kwargs(create_continue_generation_task, kwargs: dict[str, Any]) -> str:
    try:
        signature = inspect.signature(create_continue_generation_task)
    except (TypeError, ValueError):
        return _call_task_factory_filtering_unexpected_keywords(
            create_continue_generation_task,
            kwargs,
        )

    accepted_names: set[str] = set()
    for parameter in signature.parameters.values():
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            return _call_task_factory_filtering_unexpected_keywords(
                create_continue_generation_task,
                kwargs,
            )
        if parameter.kind in {
            inspect.Parameter.KEYWORD_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        }:
            accepted_names.add(parameter.name)

    return _call_task_factory_filtering_unexpected_keywords(
        create_continue_generation_task,
        {name: value for name, value in kwargs.items() if name in accepted_names},
    )

def _call_task_factory_filtering_unexpected_keywords(create_continue_generation_task, kwargs: dict[str, Any]) -> str:
    remaining = dict(kwargs)
    while True:
        try:
            return create_continue_generation_task(**remaining)
        except TypeError as exc:
            message = str(exc)
            marker = "unexpected keyword argument "
            if marker not in message:
                raise
            unsupported = message.split(marker, 1)[1].strip().strip("'\"")
            if not unsupported or unsupported not in remaining:
                raise
            remaining.pop(unsupported)

def _load_json_object(raw: str, default):
    try:
        value = json.loads(raw or "")
    except (json.JSONDecodeError, TypeError):
        return default
    return value if isinstance(value, type(default)) else default

def _load_json_int_list(raw: str | None) -> list[int]:
    try:
        value = json.loads(raw or "")
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(value, list):
        return []
    result: list[int] = []
    for item in value:
        try:
            result.append(int(item))
        except (TypeError, ValueError):
            continue
    return result

def _latest_active_generation_task(session, project_id: str) -> GenerationTask | None:
    return session.execute(
        select(GenerationTask)
        .where(
            GenerationTask.deleted_at.is_(None),
            GenerationTask.task_kind == "generation",
            GenerationTask.project_id == project_id,
            GenerationTask.status.notin_(tuple(_GENERATION_TASK_TERMINAL_STATUSES)),
        )
        .order_by(GenerationTask.updated_at.desc(), GenerationTask.id.desc())
        .limit(1)
    ).scalar_one_or_none()

def latest_rewrite_attempts_by_chapter(
    session,
    project_id: str,
    chapter_numbers: list[int] | None = None,
) -> dict[int, ChapterRewriteAttempt]:
    return load_latest_rewrite_attempts_by_chapter(session, project_id, chapter_numbers)

def _overlay_active_generation_task(detail: ProjectDetail, task: GenerationTask | None) -> ProjectDetail:
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
            "can_pause": status in {"queued", "starting", "running"} and not pause_requested,
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
        return value.astimezone(timezone.utc).isoformat() if value.tzinfo else value.isoformat()
    return value

def _serialize_model_row(row) -> dict[str, Any]:  # noqa: ANN001
    return {
        column.name: _jsonable(getattr(row, column.name))
        for column in row.__table__.columns
    }

def _ensure_initial_book_map_from_genesis(
    *,
    session,
    updater: StateUpdater,
    project: Project,
    revision,
    pack: dict[str, Any],
    decision_event_id: str,
) -> dict[str, Any]:
    existing_nodes = int(
        session.execute(
            select(func.count(MapNodeRow.id)).where(MapNodeRow.project_id == project.id)
        ).scalar_one()
        or 0
    )
    if existing_nodes > 0:
        summary = {
            "skipped": True,
            "reason": "existing_book_map",
            "map_node_count": existing_nodes,
        }
        updater.save_decision_event(
            DecisionEventInfo(
                project_id=project.id,
                scope="project",
                event_family="runtime_observation",
                event_type=DecisionEventType.MAP_GENERATION_SUCCEEDED,
                actor_type="system",
                summary="项目已有 BookMap，跳过 Genesis 自动地图生成。",
                payload=summary,
                related_object_type="book_genesis_revision",
                related_object_id=str(getattr(revision, "id", "") or ""),
                parent_event_id=decision_event_id,
            )
        )
        return summary

    world = pack.get("world") if isinstance(pack.get("world"), dict) else {}
    if not world:
        world = {
            "map_atlas": pack.get("map_atlas") if isinstance(pack.get("map_atlas"), dict) else {},
        }
    map_atlas = world.get("map_atlas") if isinstance(world.get("map_atlas"), dict) else {}
    specs = build_subworld_map_specs_from_genesis(
        project_id=project.id,
        genesis_revision_id=str(getattr(revision, "id", "") or ""),
        map_atlas=map_atlas,
    )
    if not specs:
        raise ValueError("Genesis map_atlas 未能生成 BookMap spec。")

    updater.save_decision_event(
        DecisionEventInfo(
            project_id=project.id,
            scope="project",
            event_family="runtime_observation",
            event_type=DecisionEventType.MAP_GENERATION_STARTED,
            actor_type="system",
            summary="开始从 Genesis map_atlas 生成 Scheme C BookMap。",
            payload=audit_payload(
                stage="map_generation",
                status="started",
                subworld_count=len(specs),
                subworld_ids=[spec.subworld_id for spec in specs],
            ),
            related_object_type="book_genesis_revision",
            related_object_id=str(getattr(revision, "id", "") or ""),
            parent_event_id=decision_event_id,
        )
    )
    interconnections, interconnection_source = build_interconnections_from_genesis_atlas(
        project_id=project.id,
        specs=specs,
        map_atlas=map_atlas,
        genesis_revision_id=str(getattr(revision, "id", "") or ""),
    )
    result = create_or_update_book_map(
        session,
        specs,
        interconnections=interconnections if interconnections else None,
        interconnection_source=interconnection_source,
        commit=False,
    )
    if not result.validation_report.valid:
        message = "；".join(result.validation_report.errors) or "BookMap validation failed."
        raise ValueError(message)

    summary = {
        "skipped": False,
        "subworld_count": len(result.subworld_results),
        "region_count": sum(len(item.regions) for item in result.subworld_results),
        "map_node_count": sum(len(item.map_nodes) for item in result.subworld_results),
        "map_edge_count": sum(len(item.map_edges) for item in result.subworld_results)
        + len(result.inter_subworld_edges),
        "inter_subworld_edge_count": len(result.inter_subworld_edges),
        "interconnection_source": result.summary.get("interconnection_source", interconnection_source),
        "generation_run_count": len(result.subworld_results),
        "subworld_ids": [item.subworld_id for item in result.subworld_results],
    }
    updater.save_decision_event(
        DecisionEventInfo(
            project_id=project.id,
            scope="project",
            event_family="runtime_observation",
            event_type=DecisionEventType.MAP_GENERATION_SUCCEEDED,
            actor_type="system",
            summary="Genesis map_atlas 已生成 Scheme C BookMap。",
            payload=audit_payload(stage="map_generation", status="succeeded", **summary),
            related_object_type="book_genesis_revision",
            related_object_id=str(getattr(revision, "id", "") or ""),
            parent_event_id=decision_event_id,
        )
    )
    return summary

def _export_project_audit_bundle(
    *,
    session,
    config,
    project: Project,
    operation_id: str,
    test_run_id: str = "",
) -> str:
    artifact_root = Path(str(getattr(config, "artifact_root", "data/artifacts") or "data/artifacts"))
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    bundle_dir = artifact_root / "audit_bundles" / "projects" / project.id
    bundle_dir.mkdir(parents=True, exist_ok=True)
    path = bundle_dir / f"{timestamp}_{operation_id}.json"
    decision_events = session.execute(
        select(DecisionEvent)
        .where(DecisionEvent.project_id == project.id)
        .order_by(DecisionEvent.created_at.asc(), DecisionEvent.id.asc())
    ).scalars().all()
    prompt_traces = session.execute(
        select(PromptTrace)
        .where(PromptTrace.project_id == project.id)
        .order_by(PromptTrace.created_at.asc(), PromptTrace.id.asc())
    ).scalars().all()
    tasks = session.execute(
        select(GenerationTask)
        .where(GenerationTask.project_id == project.id)
        .order_by(GenerationTask.created_at.asc(), GenerationTask.id.asc())
    ).scalars().all()
    chapter_plans = session.execute(
        select(ChapterPlan)
        .where(ChapterPlan.project_id == project.id)
        .order_by(ChapterPlan.chapter_number.asc(), ChapterPlan.id.asc())
    ).scalars().all()
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


_EXTENSION_BEATS: tuple[tuple[str, str], ...] = (
    (
        "协议启动的代价",
        "紧接上一章启动的关键协议，确认倒计时仍是分钟级危机，并让主角付出明确代价。",
    ),
    (
        "核心区的回声",
        "主角在核心区域取得恢复后门的第一段证据，同时确认被扣押盟友的位置和救援代价。",
    ),
    (
        "被扣押的证人",
        "推进盟友被扣押后的桥接，不让角色无解释脱身，并让证人提供能验证协议状态的线索。",
    ),
    (
        "恢复后门的第一把锁",
        "解释父亲在算法中留下的恢复后门如何运作，避免把倒计时重置成几天或新的无约束周期。",
    ),
    (
        "反派的反制",
        "让主要反派针对紧急协议发起反制，迫使主角在公开真相和保住个人记忆之间做选择。",
    ),
    (
        "组织旧账的清算",
        "让关键组织旧账浮出水面，补足谁授权、谁执行、谁受益的因果证据链。",
    ),
    (
        "旧城集体记忆震荡",
        "展示协议造成的城市级后果，但时间推进必须受最新 canon 约束，不能跳成数日后。",
    ),
    (
        "盟友的救援窗口",
        "在不破坏被捕状态的前提下打开救援窗口，并明确盟友是否仍受追踪器或系统权限限制。",
    ),
    (
        "家族守门人的真相",
        "揭示家族维护者身份的完整因果，关闭家族档案为何被拆散和抹除的主线缺口。",
    ),
    (
        "核心层的审判",
        "把关键盟友、主要反派、父辈后门和系统规则集中到核心层冲突中验证。",
    ),
    (
        "倒计时的终止条件",
        "明确倒计时是被终止、锁定、转移还是以代价完成，不留下主线 ledger 悬空。",
    ),
    (
        "遗档的归档",
        "终章关闭记忆重置、家族档案、盟友状态和公开真相等主线承诺，只保留非主线续作钩子。",
    ),
)


def _extension_continuity_guard(req: ProjectExtendGenerationRequest) -> str:
    explicit = str(getattr(req, "continuity_guard", "") or "").strip()
    if explicit:
        return explicit
    return (
        "必须紧接最新 accepted canon；尊重最新倒计时、地点、身份和角色状态。"
        "如果 canon 已进入分钟级危机，后续计划不得回退成几天或数日；"
        "任何时间跳跃都必须先解释倒计时被中止、锁定、转移或分支。"
    )

def _extension_arc_synopsis(
    *,
    req: ProjectExtendGenerationRequest,
    start_chapter: int,
    end_chapter: int,
) -> str:
    title = str(req.arc_title or "").strip() or f"续写弧线：第{start_chapter}-{end_chapter}章"
    synopsis = str(req.arc_synopsis or "").strip()
    guard = _extension_continuity_guard(req)
    parts = [title]
    if synopsis:
        parts.append(synopsis)
    parts.append(guard)
    return "\n".join(parts)

def _extension_chapter_blueprint(
    *,
    chapter_number: int,
    offset: int,
    guard: str,
    end_chapter: int,
) -> tuple[str, str, list[str], ChapterExperiencePlan]:
    beat_title, beat_line = _EXTENSION_BEATS[offset] if offset < len(_EXTENSION_BEATS) else (
        f"续写推进 {offset + 1}",
        "推进最新 canon 后果，关闭已登记的主线缺口，并保持倒计时、地点、身份和角色状态连续。",
    )
    title = f"第{chapter_number}章 {beat_title}"
    one_line = f"{beat_line} 连续性护栏：{guard}"
    goals = [
        "承接上一章 accepted canon，不改写已发生事实。",
        guard,
        beat_line,
    ]
    if chapter_number == end_chapter:
        goals.append("作为当前追加段落的终点，必须关闭 P0/P1 主线缺口，不能留下主线倒计时或身份债。")
    experience_plan = ChapterExperiencePlan(
        question_hook="最新 canon 的分钟级危机如何被推进或关闭？",
        question_resolution=beat_line,
        immersion_anchors=["核心区域", "倒计时终端", "家族档案后门"],
        progress_markers=[guard, beat_line],
        rule_anchors=["canon 优先于旧计划", "分钟级倒计时不得回退成几天或数日"],
        relationship_or_status_shift="跟踪主角、盟友、反派与系统权限的最新状态变化。",
    )
    return title, one_line, goals, experience_plan

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

def _chapter_infos_for_plans(session, project_id: str, plans: list[ChapterPlan]) -> list[ChapterInfo]:
    draft_map = load_latest_drafts_by_plan_id(session, [plan.id for plan in plans])
    review_draft_ids = {
        draft_id
        for draft_id in session.execute(
            select(ChapterReview.draft_id)
            .where(ChapterReview.draft_id.in_([draft.id for draft in draft_map.values()]))
            .distinct()
        ).scalars().all()
    } if draft_map else set()
    latest_attempt_map = latest_rewrite_attempts_by_chapter(
        session,
        project_id,
        [int(plan.chapter_number or 0) for plan in plans],
    )

    result = []
    for plan in plans:
        draft = draft_map.get(plan.id)
        latest_attempt = latest_attempt_map.get(plan.chapter_number)
        result.append(ChapterInfo(
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
        ))
    return result


__all__ = [
    name
    for name, value in globals().items()
    if name.startswith("_") and callable(value)
] + [
    "call_task_factory_with_supported_kwargs",
]
