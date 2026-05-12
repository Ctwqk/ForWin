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
    ProjectSummary,
    PublisherUploadJobResponse,
    RepairVerificationInfo,
    StartWritingResponse,
    TaskResponse,
)
from forwin.book_genesis import GENESIS_STAGE_ORDER, StaleGenesisRevisionError
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
from forwin.protocol.review import normalize_repair_scope
from forwin.state.query_helpers import load_latest_drafts_by_plan_id
from forwin.state.updater import StateUpdater


_GENERATION_TASK_TERMINAL_STATUSES = {
    "completed",
    "partial_failed",
    "failed",
    "needs_review",
    "cancelled",
    "paused",
}


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
            "can_pause": status in {"starting", "running"} and not pause_requested,
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


def list_projects(
    *,
    get_session,
    config,
    display_datetime,
) -> list[ProjectSummary]:
    session = get_session()
    try:
        projects = session.execute(
            select(Project).order_by(Project.created_at.desc())
        ).scalars().all()
        return build_project_summaries(
            session=session,
            projects=projects,
            display_datetime=display_datetime,
            review_interval_chapters=max(0, int(config.review_interval_chapters if config else 0)),
        )
    finally:
        session.close()


def create_project(
    req: ProjectCreateRequest,
    *,
    get_session,
    config,
    build_genesis_service,
    close_genesis_service,
    log_decision_event,
) -> ProjectCreateResponse:
    session = get_session()
    genesis_service = None
    try:
        title = str(req.title or "").strip()
        premise = str(req.premise or "").strip()
        if not title:
            raise HTTPException(400, "书名不能为空")
        if not premise:
            raise HTTPException(400, "作品 premise 不能为空")

        publish_bindings = [
            {
                "platform": str(binding.platform or "").strip(),
                "book_name": str(binding.book_name or "").strip() or title,
                "upload_url": str(binding.upload_url or "").strip(),
                "create_if_missing": bool(binding.create_if_missing),
                "book_meta": binding.book_meta.model_dump(mode="json"),
            }
            for binding in req.publish_bindings
            if str(binding.platform or "").strip()
        ]
        if publish_bindings:
            default_publish = publish_bindings[0]
        else:
            publish_platform = str(req.publish_platform or "").strip()
            publish_book_name = str(req.publish_book_name or "").strip() or title
            publish_upload_url = str(req.publish_upload_url or "").strip()
            platform_has_existing_book = bool(req.platform_has_existing_book)
            default_publish = {
                "platform": publish_platform,
                "book_name": publish_book_name,
                "upload_url": publish_upload_url,
                "create_if_missing": bool(publish_platform) and not platform_has_existing_book,
            }
            if publish_platform:
                publish_bindings = [default_publish]
        automation = normalize_project_automation(
            {
                "publish": default_publish,
                "publish_bindings": publish_bindings,
            }
        )
        governance = new_project_governance(
            default_operation_mode="blackbox",
            review_interval_chapters=config.review_interval_chapters if config is not None else 0,
        )
        updater = StateUpdater(session)
        project = updater.create_project(
            title=title,
            premise=premise,
            genre=str(req.genre or "").strip() or "玄幻",
            setting_summary=str(req.setting_summary or "").strip(),
            target_total_chapters=max(1, int(req.target_total_chapters or 1)),
            governance=governance,
            creation_status="creating",
            automation_json=json.dumps(
                automation.model_dump(mode="json"),
                ensure_ascii=False,
            ),
        )
        genesis_service = build_genesis_service()
        genesis_revision = genesis_service.create_initial_revision(
            session=session,
            updater=updater,
            project=project,
            brief_seed={
                "audience_hint": req.audience_hint,
                "core_emotion": req.core_emotion,
                "core_delight": req.core_delight,
                "inspiration_notes": req.inspiration_notes,
                "content_guardrails": req.content_guardrails,
            },
        )
        log_decision_event(
            session,
            project_id=project.id,
            event_family="business_event",
            event_type=DecisionEventType.PROJECT_CREATED,
            summary="项目已创建并启用默认治理策略。",
            payload={
                "governance": governance.model_dump(mode="json"),
                "creation_status": "creating",
            },
        )
        session.commit()
        session.refresh(project)
        return ProjectCreateResponse(
            ok=True,
            project_id=project.id,
            title=project.title,
            target_total_chapters=int(project.target_total_chapters or 3),
            creation_status=str(project.creation_status or "creating"),
            active_genesis_revision_id=str(genesis_revision.id or ""),
            workspace_url=f"/?workspace=genesis&project_id={project.id}",
            message=f"书本《{project.title}》已创建，已进入 Genesis 工作台。",
        )
    finally:
        close_genesis_service(genesis_service)
        session.close()


def delete_project(
    project_id: str,
    *,
    get_session,
    config,
    delete_project_impl,
    project_delete_blockers,
    project_delete_conflict_message,
    operation_id: str = "",
    test_run_id: str = "",
) -> ProjectDeleteResponse:
    normalized_operation_id = _new_operation_id(operation_id)
    session = get_session()
    try:
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        blockers = project_delete_blockers(project_id, session=session)
        if blockers:
            raise HTTPException(409, project_delete_conflict_message(blockers))
        _export_project_audit_bundle(
            session=session,
            config=config,
            project=project,
            operation_id=normalized_operation_id,
            test_run_id=test_run_id,
        )
        delete_project_impl(session, project_id)
        session.commit()
        return ProjectDeleteResponse(
            ok=True,
            project_id=project_id,
            message=f"项目《{project.title}》已删除。",
            operation_id=normalized_operation_id,
        )
    finally:
        session.close()


def bulk_delete_projects(
    req: ProjectBulkDeleteRequest,
    *,
    get_session,
    config,
    delete_project_impl,
    project_delete_blockers,
    operation_id: str = "",
    test_run_id: str = "",
) -> BulkDeleteResponse:
    normalized_operation_id = _new_operation_id(operation_id)
    session = get_session()
    deleted_ids: list[str] = []
    skipped_ids: list[str] = []
    try:
        seen: set[str] = set()
        for project_id in req.project_ids:
            normalized = str(project_id or "").strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            project = session.get(Project, normalized)
            if project is None:
                skipped_ids.append(normalized)
                continue
            if project_delete_blockers(normalized, session=session):
                skipped_ids.append(normalized)
                continue
            _export_project_audit_bundle(
                session=session,
                config=config,
                project=project,
                operation_id=normalized_operation_id,
                test_run_id=test_run_id,
            )
            delete_project_impl(session, normalized)
            deleted_ids.append(normalized)
        session.commit()
        return BulkDeleteResponse(
            ok=True,
            deleted_count=len(deleted_ids),
            skipped_count=len(skipped_ids),
            deleted_ids=deleted_ids,
            skipped_ids=skipped_ids,
            message=f"已删除 {len(deleted_ids)} 本书，跳过 {len(skipped_ids)} 本。",
            operation_id=normalized_operation_id,
        )
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_project(
    project_id: str,
    *,
    get_session,
    config,
    display_datetime,
) -> ProjectDetail:
    session = get_session()
    try:
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        detail = build_project_detail(
            session=session,
            project=project,
            display_datetime=display_datetime,
            review_interval_chapters=max(0, int(config.review_interval_chapters if config else 0)),
        )
        return _overlay_active_generation_task(
            detail,
            _latest_active_generation_task(session, project_id),
        )
    finally:
        session.close()


def get_project_genesis(
    project_id: str,
    *,
    get_session,
    build_genesis_service,
    close_genesis_service,
    require_genesis_project,
) -> BookGenesisDetail:
    session = get_session()
    genesis_service = None
    try:
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        require_genesis_project(project)
        genesis_service = build_genesis_service()
        return BookGenesisDetail.model_validate(
            genesis_service.build_detail(session=session, project=project)
        )
    finally:
        close_genesis_service(genesis_service)
        session.close()


def patch_project_genesis(
    project_id: str,
    req: BookGenesisPatchRequest,
    *,
    get_session,
    build_genesis_service,
    close_genesis_service,
    require_genesis_project,
    active_genesis_revision,
    genesis_patch_payload,
) -> BookGenesisDetail:
    session = get_session()
    genesis_service = None
    try:
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        require_genesis_project(project)
        revision = active_genesis_revision(session, project)
        if revision is None:
            raise HTTPException(409, "项目 Genesis revision 不存在")
        patch_payload = genesis_patch_payload(req)
        if not patch_payload:
            raise HTTPException(400, "没有可更新的 Genesis 字段")
        genesis_service = build_genesis_service()
        updater = StateUpdater(session)
        try:
            genesis_service.patch_pack(
                session=session,
                updater=updater,
                project=project,
                revision=revision,
                patch=patch_payload,
                reason=req.reason,
            )
        except StaleGenesisRevisionError as exc:
            raise HTTPException(409, str(exc)) from exc
        session.commit()
        session.refresh(project)
        return BookGenesisDetail.model_validate(
            genesis_service.build_detail(session=session, project=project)
        )
    finally:
        close_genesis_service(genesis_service)
        session.close()


def generate_project_genesis_stage(
    project_id: str,
    stage_key: str,
    req: BookGenesisStageRunRequest | None = None,
    *,
    get_session,
    build_genesis_service,
    close_genesis_service,
    require_genesis_project,
    active_genesis_revision,
) -> BookGenesisDetail:
    session = get_session()
    genesis_service = None
    try:
        normalized_stage = str(stage_key or "").strip()
        if normalized_stage not in GENESIS_STAGE_ORDER:
            raise HTTPException(404, "Genesis stage 不存在")
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        require_genesis_project(project)
        revision = active_genesis_revision(session, project)
        if revision is None:
            raise HTTPException(409, "项目 Genesis revision 不存在")
        genesis_service = build_genesis_service(
            model_profile_id=str((req.model_profile_id if req else "") or "").strip()
        )
        updater = StateUpdater(session)
        try:
            genesis_service.generate_stage(
                session=session,
                updater=updater,
                project=project,
                revision=revision,
                stage_key=normalized_stage,
            )
        except StaleGenesisRevisionError as exc:
            raise HTTPException(409, str(exc)) from exc
        session.commit()
        session.refresh(project)
        return BookGenesisDetail.model_validate(
            genesis_service.build_detail(session=session, project=project)
        )
    finally:
        close_genesis_service(genesis_service)
        session.close()


def lock_project_genesis_stage(
    project_id: str,
    stage_key: str,
    *,
    get_session,
    build_genesis_service,
    close_genesis_service,
    require_genesis_project,
    active_genesis_revision,
) -> BookGenesisDetail:
    session = get_session()
    genesis_service = None
    try:
        normalized_stage = str(stage_key or "").strip()
        if normalized_stage not in GENESIS_STAGE_ORDER:
            raise HTTPException(404, "Genesis stage 不存在")
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        require_genesis_project(project)
        revision = active_genesis_revision(session, project)
        if revision is None:
            raise HTTPException(409, "项目 Genesis revision 不存在")
        genesis_service = build_genesis_service()
        updater = StateUpdater(session)
        try:
            genesis_service.lock_stage(
                session=session,
                updater=updater,
                project=project,
                revision=revision,
                stage_key=normalized_stage,
            )
        except StaleGenesisRevisionError as exc:
            raise HTTPException(409, str(exc)) from exc
        session.commit()
        session.refresh(project)
        return BookGenesisDetail.model_validate(
            genesis_service.build_detail(session=session, project=project)
        )
    finally:
        close_genesis_service(genesis_service)
        session.close()


def rerun_project_genesis_stage(
    project_id: str,
    stage_key: str,
    req: BookGenesisStageRunRequest | None = None,
    *,
    get_session,
    build_genesis_service,
    close_genesis_service,
    require_genesis_project,
    active_genesis_revision,
) -> BookGenesisDetail:
    session = get_session()
    genesis_service = None
    try:
        normalized_stage = str(stage_key or "").strip()
        if normalized_stage not in GENESIS_STAGE_ORDER:
            raise HTTPException(404, "Genesis stage 不存在")
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        require_genesis_project(project)
        revision = active_genesis_revision(session, project)
        if revision is None:
            raise HTTPException(409, "项目 Genesis revision 不存在")
        genesis_service = build_genesis_service(
            model_profile_id=str((req.model_profile_id if req else "") or "").strip()
        )
        updater = StateUpdater(session)
        try:
            genesis_service.generate_stage(
                session=session,
                updater=updater,
                project=project,
                revision=revision,
                stage_key=normalized_stage,
                event_type=DecisionEventType.GENESIS_STAGE_RERUN,
            )
        except StaleGenesisRevisionError as exc:
            raise HTTPException(409, str(exc)) from exc
        session.commit()
        session.refresh(project)
        return BookGenesisDetail.model_validate(
            genesis_service.build_detail(session=session, project=project)
        )
    finally:
        close_genesis_service(genesis_service)
        session.close()


def refine_project_genesis_stage(
    project_id: str,
    stage_key: str,
    req: BookGenesisRefineRequest,
    *,
    get_session,
    build_genesis_service,
    close_genesis_service,
    require_genesis_project,
    active_genesis_revision,
) -> BookGenesisDetail:
    session = get_session()
    genesis_service = None
    try:
        normalized_stage = str(stage_key or "").strip()
        if normalized_stage not in GENESIS_STAGE_ORDER:
            raise HTTPException(404, "Genesis stage 不存在")
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        require_genesis_project(project)
        revision = active_genesis_revision(session, project)
        if revision is None:
            raise HTTPException(409, "项目 Genesis revision 不存在")
        genesis_service = build_genesis_service(
            model_profile_id=str(req.model_profile_id or "").strip()
        )
        updater = StateUpdater(session)
        try:
            genesis_service.refine_stage(
                session=session,
                updater=updater,
                project=project,
                revision=revision,
                stage_key=normalized_stage,
                instruction=req.instruction,
                target_path=req.target_path,
                reason=req.reason,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except StaleGenesisRevisionError as exc:
            raise HTTPException(409, str(exc)) from exc
        session.commit()
        session.refresh(project)
        return BookGenesisDetail.model_validate(
            genesis_service.build_detail(session=session, project=project)
        )
    finally:
        close_genesis_service(genesis_service)
        session.close()


def generate_project_genesis_name(
    project_id: str,
    req: BookGenesisNameGenerateRequest,
    *,
    get_session,
    build_genesis_service,
    close_genesis_service,
    require_genesis_project,
    active_genesis_revision,
) -> BookGenesisNameGenerateResponse:
    session = get_session()
    genesis_service = None
    try:
        normalized_stage = str(req.stage_key or "").strip()
        if normalized_stage not in GENESIS_STAGE_ORDER:
            raise HTTPException(404, "Genesis stage 不存在")
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        require_genesis_project(project)
        revision = active_genesis_revision(session, project)
        if revision is None:
            raise HTTPException(409, "项目 Genesis revision 不存在")
        genesis_service = build_genesis_service()
        try:
            payload = genesis_service.generate_name_suggestions(
                project=project,
                revision=revision,
                stage_key=normalized_stage,
                target_path=req.target_path,
                field_path=req.field_path,
                kind=req.kind,
                count=req.count,
                nonce=req.nonce,
                stage_payload_override=req.stage_payload_override,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return BookGenesisNameGenerateResponse.model_validate(payload)
    finally:
        close_genesis_service(genesis_service)
        session.close()


def start_project_writing(
    project_id: str,
    *,
    get_session,
    config,
    saved_runtime_config_or_default,
    build_genesis_service,
    close_genesis_service,
    require_genesis_project,
    active_genesis_revision,
    project_has_active_generation_task,
    generation_task_conflict_message,
    create_continue_generation_task,
) -> StartWritingResponse:
    if not config:
        raise HTTPException(503, "服务尚未初始化")
    runtime_config = saved_runtime_config_or_default()
    if not runtime_config.minimax_api_key and not bool(getattr(runtime_config, "codex_enabled", False)):
        raise HTTPException(400, "MINIMAX_API_KEY 未设置。请先配置模型，再启动写作。")
    session = get_session()
    genesis_service = None
    try:
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        require_genesis_project(project)
        if str(project.creation_status or "") != "genesis_ready":
            raise HTTPException(409, "Genesis 尚未完成锁定，不能启动写作。")
        if project_has_active_generation_task(project_id, session=session):
            raise HTTPException(409, generation_task_conflict_message(project_id))
        revision = active_genesis_revision(session, project)
        if revision is None:
            raise HTTPException(409, "Genesis revision 不存在。")
        genesis_service = build_genesis_service(runtime_config)
        updater = StateUpdater(session)
        try:
            handoff_result = genesis_service.handoff.start_writing(
                session=session,
                updater=updater,
                command=StartWritingCommand(
                    project_id=project.id,
                    actor_type="manual_ui",
                    runtime_config=runtime_config,
                ),
            )
        except ValueError as exc:
            failure_summary = str(exc) or "Genesis map_atlas 无法生成 BookMap。"
            if "map" in failure_summary.lower() or "地图" in failure_summary or "BookMap" in failure_summary:
                session.commit()
                raise HTTPException(409, f"地图生成失败，不能启动写作：{failure_summary}") from exc
            session.rollback()
            raise HTTPException(409, failure_summary) from exc
        try:
            task_id = create_continue_generation_task(
                project_id=project.id,
                runtime_config=runtime_config,
                requested_chapters=handoff_result.active_chapter_plan_count,
                title=project.title,
                subtitle=f"启动写作 · {project.genre}",
                message="Genesis 完成，准备进入写作主链。",
            )
        except Exception:
            session.rollback()
            raise
        session.commit()
        return StartWritingResponse(
            ok=True,
            project_id=project.id,
            creation_status=handoff_result.project_status,
            task_id=task_id,
            message="Genesis 已完成交接，开始写作。",
        )
    finally:
        close_genesis_service(genesis_service)
        session.close()


def continue_project_generation(
    project_id: str,
    req: ProjectContinueGenerationRequest | None = None,
    *,
    get_session,
    config,
    runtime_settings,
    display_datetime,
    active_generation_task_error_cls,
    resolve_project_governance,
    governance_request_payload,
    project_has_active_generation_task,
    generation_task_conflict_message,
    log_decision_event,
    create_continue_generation_task,
    serialize_task,
    get_generation_task_or_404,
) -> TaskResponse:
    if not config:
        raise HTTPException(503, "服务尚未初始化")
    runtime_config = build_saved_runtime_config(
        base_config=config,
        runtime_settings=runtime_settings,
    )
    session = get_session()
    try:
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        if str(project.creation_status or "") in {"creating", "genesis_ready"}:
            raise HTTPException(409, "该项目仍在 Genesis 阶段，请先完成创世并点击“启动写作”。")
        governance = resolve_project_governance(
            project,
            overrides=governance_request_payload(req),
            base_config=config,
        )
        runtime_config = copy_config(
            runtime_config,
            operation_mode=governance.default_operation_mode,
            review_interval_chapters=governance.review_interval_chapters,
            progression_mode=governance.progression_mode,
            auto_band_checkpoint=governance.auto_band_checkpoint,
            band_warn_action=governance.band_warn_action,
            manual_checkpoints_enabled=governance.manual_checkpoints_enabled,
            future_constraints_enabled=governance.future_constraints_enabled,
        )
        if project_has_active_generation_task(project_id, session=session):
            raise HTTPException(409, generation_task_conflict_message(project_id))
        plans = session.execute(
            select(ChapterPlan)
            .where(ChapterPlan.project_id == project_id)
            .order_by(ChapterPlan.chapter_number.asc())
        ).scalars().all()
        waiting_review = [plan.chapter_number for plan in plans if plan.status == "needs_review"]
        if waiting_review:
            raise HTTPException(409, f"仍有章节等待 review：{', '.join(str(item) for item in waiting_review)}")
        waiting_acceptance = [plan.chapter_number for plan in plans if plan.status == "drafted"]
        if waiting_acceptance:
            raise HTTPException(409, f"仍有章节等待接受：{', '.join(str(item) for item in waiting_acceptance)}")
        remaining = [plan.chapter_number for plan in plans if plan.status in {"planned", "failed"}]
        planned_future_arc = session.execute(
            select(ArcPlanVersion.id)
            .where(
                ArcPlanVersion.project_id == project_id,
                ArcPlanVersion.status == "planned",
            )
            .order_by(ArcPlanVersion.arc_number.asc(), ArcPlanVersion.created_at.asc())
            .limit(1)
        ).scalar_one_or_none()
        if not remaining and planned_future_arc is None:
            raise HTTPException(400, "没有剩余章节需要继续生成")
        project_detail = build_project_detail(
            session=session,
            project=project,
            display_datetime=display_datetime,
            review_interval_chapters=governance.review_interval_chapters,
        )
        if project_detail.blocking_reason.code:
            log_decision_event(
                session,
                project_id=project_id,
                event_family="evaluation_verdict",
                event_type=DecisionEventType.HARD_GATE_HIT,
                actor_type="api",
                scope="project",
                summary=project_detail.blocking_reason.message or project_detail.blocking_reason.code,
                payload={"blocking_reason": project_detail.blocking_reason.code},
                band_id=project_detail.blocking_reason.band_id,
                chapter_number=int(project_detail.blocking_reason.chapter_number or 0),
                related_object_type="project",
                related_object_id=project_id,
            )
            session.commit()
            raise HTTPException(409, project_detail.blocking_reason.message)
        max_chapters = req.max_chapters if req is not None else None
        requested_chapters = len(remaining)
        if max_chapters is not None:
            max_chapters = max(1, int(max_chapters or 1))
            requested_chapters = min(requested_chapters or max_chapters, max_chapters)
        requested_chapters = max(1, int(requested_chapters or 0))
        task_id = create_continue_generation_task(
            project_id=project_id,
            runtime_config=runtime_config,
            requested_chapters=requested_chapters,
            max_chapters=max_chapters,
            title=project.title,
            subtitle=f"继续生成 · {project.genre}",
            message="准备继续生成剩余章节。",
        )
    except active_generation_task_error_cls as exc:
        raise HTTPException(409, str(exc)) from exc
    finally:
        session.close()
    return serialize_task(task_id, get_generation_task_or_404(task_id))


def update_project_automation(
    project_id: str,
    req: ProjectAutomationUpdateRequest,
    *,
    get_session,
    persist_project_automation,
) -> ProjectAutomationUpdateResponse:
    session = get_session()
    try:
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        current = normalize_project_automation(project.automation_json)
        payload = current.model_dump(mode="json")
        payload.update(
            {
                "enabled": bool(req.enabled),
                "daily_start_time": req.daily_start_time,
                "daily_chapter_quota": req.daily_chapter_quota,
                "auto_publish": bool(req.auto_publish),
            }
        )
        if req.publish is not None:
            payload["publish"] = req.publish.model_dump(mode="json")
        if req.publish_bindings is not None:
            payload["publish_bindings"] = [
                binding.model_dump(mode="json")
                for binding in req.publish_bindings
            ]
        updated = normalize_project_automation(payload)
        stored = persist_project_automation(session, project, updated)
        session.commit()
        return ProjectAutomationUpdateResponse(
            ok=True,
            project_id=project_id,
            automation=stored,
            message="书本自动化设置已保存。",
        )
    finally:
        session.close()


def list_chapters(project_id: str, *, get_session) -> list[ChapterInfo]:
    session = get_session()
    try:
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")

        plans = session.execute(
            select(ChapterPlan).where(ChapterPlan.project_id == project_id).order_by(ChapterPlan.chapter_number)
        ).scalars().all()
        draft_map = load_latest_drafts_by_plan_id(session, [plan.id for plan in plans])
        review_draft_ids = {
            draft_id
            for draft_id in session.execute(
                select(ChapterReview.draft_id)
                .where(ChapterReview.draft_id.in_([draft.id for draft in draft_map.values()]))
                .distinct()
            ).scalars().all()
        } if draft_map else set()
        latest_attempt_map: dict[int, ChapterRewriteAttempt] = {}
        for attempt in session.execute(
            select(ChapterRewriteAttempt)
            .where(ChapterRewriteAttempt.project_id == project_id)
            .order_by(ChapterRewriteAttempt.chapter_number.asc(), ChapterRewriteAttempt.attempt_no.desc(), ChapterRewriteAttempt.created_at.desc())
        ).scalars().all():
            latest_attempt_map.setdefault(int(attempt.chapter_number or 0), attempt)

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
    finally:
        session.close()


def get_chapter(
    project_id: str,
    chapter_number: int,
    *,
    get_session,
) -> ChapterDetail:
    session = get_session()
    try:
        plan = session.execute(
            select(ChapterPlan).where(
                ChapterPlan.project_id == project_id,
                ChapterPlan.chapter_number == chapter_number,
            )
        ).scalar_one_or_none()
        if plan is None:
            raise HTTPException(404, f"第{chapter_number}章不存在")

        draft = session.execute(
            select(ChapterDraft).where(ChapterDraft.chapter_plan_id == plan.id).order_by(ChapterDraft.version.desc()).limit(1)
        ).scalar_one_or_none()
        if draft is None:
            raise HTTPException(404, f"第{chapter_number}章尚未生成")
        has_review = session.execute(
            select(ChapterReview.id).where(ChapterReview.draft_id == draft.id).limit(1)
        ).scalar_one_or_none() is not None

        return ChapterDetail(
            chapter_number=chapter_number,
            title=plan.title,
            body=draft.body_text,
            char_count=draft.char_count,
            summary=draft.summary,
            status=plan.status,
            has_draft=True,
            has_review=has_review,
            version=draft.version,
            acceptance_mode=str(getattr(plan, "acceptance_mode", "") or ""),
            repair_attempt_count=int(getattr(plan, "repair_attempt_count", 0) or 0),
            canon_risk_level=str(getattr(plan, "canon_risk_level", "") or ""),
            residual_review_issues=_load_json_object(getattr(plan, "residual_review_issues_json", "[]"), []),
        )
    finally:
        session.close()


def create_project_chapter_upload_job(
    project_id: str,
    req: ProjectChapterPublishRequest,
    *,
    get_session,
    publisher_manager,
) -> PublisherUploadJobResponse:
    session = get_session()
    try:
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        plan = session.execute(
            select(ChapterPlan).where(
                ChapterPlan.project_id == project_id,
                ChapterPlan.chapter_number == req.chapter_number,
            )
        ).scalar_one_or_none()
        if plan is None:
            raise HTTPException(404, f"第{req.chapter_number}章不存在")
        draft = session.execute(
            select(ChapterDraft)
            .where(ChapterDraft.chapter_plan_id == plan.id)
            .order_by(ChapterDraft.version.desc())
            .limit(1)
        ).scalar_one_or_none()
        if draft is None:
            raise HTTPException(404, f"第{req.chapter_number}章尚未生成")
    finally:
        session.close()

    try:
        payload = publisher_manager.create_upload_job(
            project_id=project_id,
            platform=req.platform,
            book_name=req.book_name,
            chapter_title=plan.title,
            body=draft.body_text,
            upload_url=req.upload_url,
            publish=req.publish,
            create_if_missing=req.create_if_missing,
            book_meta=req.book_meta.model_dump() if req.book_meta else None,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return PublisherUploadJobResponse(**payload)


def get_chapter_review(
    project_id: str,
    chapter_number: int,
    *,
    get_session,
    decision_refs_for_chapter_review,
) -> ChapterReviewDetail:
    session = get_session()
    try:
        plan = session.execute(
            select(ChapterPlan).where(
                ChapterPlan.project_id == project_id,
                ChapterPlan.chapter_number == chapter_number,
            )
        ).scalar_one_or_none()
        if plan is None:
            raise HTTPException(404, f"第{chapter_number}章不存在")

        draft = session.execute(
            select(ChapterDraft)
            .where(ChapterDraft.chapter_plan_id == plan.id)
            .order_by(ChapterDraft.version.desc())
            .limit(1)
        ).scalar_one_or_none()
        if draft is None:
            raise HTTPException(404, f"第{chapter_number}章尚未生成 draft")

        review = session.execute(
            select(ChapterReview)
            .where(ChapterReview.draft_id == draft.id)
            .order_by(ChapterReview.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if review is None:
            raise HTTPException(404, f"第{chapter_number}章尚未生成 review")

        issues = _load_json_object(review.issues_json, [])
        review_meta = _load_json_object(review.review_meta_json, {})
        rewrite_attempts = session.execute(
            select(ChapterRewriteAttempt)
            .where(
                ChapterRewriteAttempt.project_id == project_id,
                ChapterRewriteAttempt.chapter_number == chapter_number,
            )
            .order_by(ChapterRewriteAttempt.attempt_no.desc(), ChapterRewriteAttempt.created_at.desc())
        ).scalars().all()
        decision_refs = decision_refs_for_chapter_review(
            session,
            project_id=project_id,
            chapter_number=chapter_number,
            review_id=review.id,
        )
        latest_attempt = rewrite_attempts[0] if rewrite_attempts else None
        residual_review_issues = (
            review_meta.get("residual_review_issues")
            if isinstance(review_meta.get("residual_review_issues"), list)
            else _load_json_object(getattr(plan, "residual_review_issues_json", "[]"), [])
        )
        return ChapterReviewDetail(
            project_id=project_id,
            chapter_number=chapter_number,
            title=plan.title,
            status=plan.status,
            draft_id=draft.id,
            version=draft.version,
            body=draft.body_text,
            summary=draft.summary,
            verdict=review.verdict,
            issues=[
                ChapterReviewIssueInfo.model_validate(issue)
                for issue in issues
                if isinstance(issue, dict)
            ],
            artifact_meta_path=draft.llm_raw_response,
            recommended_action=str(review_meta.get("recommended_action") or ""),
            review_summary=str(review_meta.get("review_summary") or ""),
            planned_reward_tags=[
                str(item)
                for item in (review_meta.get("planned_reward_tags") or [])
                if str(item).strip()
            ],
            delivered_reward_tags=[
                str(item)
                for item in (review_meta.get("delivered_reward_tags") or [])
                if str(item).strip()
            ],
            experience_scores={
                str(key): float(value)
                for key, value in (review_meta.get("experience_scores") or {}).items()
            },
            review_notes=[
                str(item)
                for item in (review_meta.get("review_notes") or [])
                if str(item).strip()
            ],
            lint_signals=[
                LintSignalInfo.model_validate(item)
                for item in (review_meta.get("lint_signals") or [])
                if isinstance(item, dict)
            ],
            evidence_refs=[
                str(item)
                for item in (review_meta.get("evidence_refs") or [])
                if str(item).strip()
            ],
            confirmed_signal_refs=[
                str(item)
                for item in (review_meta.get("confirmed_signal_refs") or [])
                if str(item).strip()
            ],
            reviewer_mode=str(review_meta.get("reviewer_mode") or ""),
            proposed_design_patch=(
                dict((review_meta.get("repair_instruction") or {}).get("design_patch") or {})
                if isinstance(review_meta.get("repair_instruction"), dict)
                else {}
            ),
            rewrite_attempt_count=len(rewrite_attempts),
            latest_repair_scope=(
                normalize_repair_scope(latest_attempt.repair_scope or "", default="")
                if latest_attempt
                else ""
            ),
            latest_repair_scope_reason=str(review_meta.get("scope_reason") or ""),
            forced_accept_applied=bool(review_meta.get("forced_accept_applied")),
            acceptance_mode=str(getattr(plan, "acceptance_mode", "") or ""),
            repair_attempt_count=int(getattr(plan, "repair_attempt_count", 0) or 0),
            canon_risk_level=str(getattr(plan, "canon_risk_level", "") or ""),
            residual_review_issues=[
                ChapterReviewIssueInfo.model_validate(item)
                for item in residual_review_issues
                if isinstance(item, dict)
            ],
            repair_verification=(
                RepairVerificationInfo.model_validate(review_meta.get("repair_verification"))
                if isinstance(review_meta.get("repair_verification"), dict)
                else None
            ),
            final_gate_decision=(
                FinalGateDecisionInfo.model_validate(review_meta.get("final_gate_decision"))
                if isinstance(review_meta.get("final_gate_decision"), dict)
                else None
            ),
            repair_exhausted=bool(review_meta.get("repair_exhausted")),
            rewrite_attempts=[
                ChapterRewriteAttemptInfo(
                    attempt_no=int(item.attempt_no or 0),
                    repair_scope=normalize_repair_scope(item.repair_scope or "", default=""),
                    result_verdict=str(item.result_verdict or ""),
                    result_review_id=str(getattr(item, "result_review_id", "") or ""),
                    failure_reason=str(getattr(item, "failure_reason", "") or ""),
                    forced_accept_applied=bool(item.forced_accept_applied),
                    design_patch=_load_json_object(item.design_patch_json, {}),
                    verification=(
                        RepairVerificationInfo.model_validate(_load_json_object(getattr(item, "verification_json", "{}"), {}))
                        if _load_json_object(getattr(item, "verification_json", "{}"), {})
                        else None
                    ),
                    source_chapter_plan=_load_json_object(getattr(item, "source_chapter_plan_json", "{}"), {}),
                    result_chapter_plan=_load_json_object(getattr(item, "result_chapter_plan_json", "{}"), {}),
                    source_band_plan=_load_json_object(getattr(item, "source_band_plan_json", "{}"), {}),
                    result_band_plan=_load_json_object(getattr(item, "result_band_plan_json", "{}"), {}),
                )
                for item in reversed(rewrite_attempts)
            ],
            decision_refs=decision_refs,
        )
    finally:
        session.close()


def get_candidate_draft(
    project_id: str,
    chapter_number: int,
    *,
    get_session,
    decision_refs_for_chapter_review,
) -> CandidateDraftDetail:
    try:
        review = get_chapter_review(
            project_id,
            chapter_number,
            get_session=get_session,
            decision_refs_for_chapter_review=decision_refs_for_chapter_review,
        )
    except HTTPException as exc:
        if exc.status_code == 404 and "draft" in str(exc.detail).lower():
            raise HTTPException(404, f"第{chapter_number}章尚未生成 candidate draft") from exc
        raise
    session = get_session()
    try:
        record = CandidateDraftRepository(session).latest_for_chapter(
            project_id=project_id,
            chapter_number=chapter_number,
        )
        record_status = str(getattr(record, "status", "") or review.status or "")
        canon_status = str(getattr(record, "canon_status", "") or "")
        if not canon_status:
            canon_status = "canon" if str(review.status or "") == "accepted" else "candidate"
        canon_ready = canon_status == "canon" or str(review.status or "") == "accepted"
        return CandidateDraftDetail(
            project_id=review.project_id,
            chapter_number=review.chapter_number,
            title=review.title,
            status=record_status,
            candidate_draft_id=review.draft_id,
            version=review.version,
            body=review.body,
            summary=review.summary,
            char_count=len(review.body or ""),
            scene_outputs=_load_json_object(getattr(record, "scene_outputs_json", "[]"), []) if record else [],
            state_change_candidates=_load_json_object(getattr(record, "state_change_candidates_json", "[]"), []) if record else [],
            event_candidates=_load_json_object(getattr(record, "event_candidates_json", "[]"), []) if record else [],
            thread_beat_candidates=_load_json_object(getattr(record, "thread_beat_candidates_json", "[]"), []) if record else [],
            review_verdict=review.verdict,
            review_summary=review.review_summary,
            repair_attempts=review.rewrite_attempts,
            repair_attempt_count=(
                int(getattr(record, "repair_attempt_count", 0) or 0)
                if record is not None
                else int(getattr(review, "repair_attempt_count", 0) or len(review.rewrite_attempts))
            ),
            canon_ready=canon_ready,
            canon_status=canon_status,
            canon_artifact_path=str(getattr(record, "canon_artifact_path", "") or ""),
            failure_reason=str(getattr(record, "failure_reason", "") or ""),
        )
    finally:
        session.close()


def approve_chapter_review(
    project_id: str,
    chapter_number: int,
    req: ChapterReviewApproveRequest,
    *,
    config,
    orchestrator,
    runtime_settings,
    get_session,
    display_datetime,
    active_generation_task_error_cls,
    require_reason,
    resolve_project_governance,
    project_has_active_generation_task,
    generation_task_conflict_message,
    log_decision_event,
    create_continue_generation_task,
    update_task,
) -> ChapterReviewApproveResponse:
    if config is None or orchestrator is None:
        raise HTTPException(500, "服务尚未完成初始化")

    runtime_config = build_saved_runtime_config(
        base_config=config,
        runtime_settings=runtime_settings,
    )
    reason = require_reason(req.reason, action="接受 review")
    task_id = ""
    total_chapters = 0
    if req.continue_generation:
        session = get_session()
        try:
            project = session.get(Project, project_id)
            if project is None:
                raise HTTPException(404, "项目不存在")
            governance = resolve_project_governance(project, base_config=config)
            runtime_config = copy_config(
                runtime_config,
                operation_mode=governance.default_operation_mode,
                review_interval_chapters=governance.review_interval_chapters,
                progression_mode=governance.progression_mode,
                auto_band_checkpoint=governance.auto_band_checkpoint,
                band_warn_action=governance.band_warn_action,
                manual_checkpoints_enabled=governance.manual_checkpoints_enabled,
                future_constraints_enabled=governance.future_constraints_enabled,
            )
            if project_has_active_generation_task(project_id, session=session):
                raise HTTPException(409, generation_task_conflict_message(project_id))
            project_detail = build_project_detail(
                session=session,
                project=project,
                display_datetime=display_datetime,
                review_interval_chapters=governance.review_interval_chapters,
            )
            if project_detail.blocking_reason.code:
                log_decision_event(
                    session,
                    project_id=project_id,
                    event_family="evaluation_verdict",
                    event_type=DecisionEventType.HARD_GATE_HIT,
                    actor_type="api",
                    scope="project",
                    summary=project_detail.blocking_reason.message or project_detail.blocking_reason.code,
                    payload={"blocking_reason": project_detail.blocking_reason.code},
                    band_id=project_detail.blocking_reason.band_id,
                    chapter_number=int(project_detail.blocking_reason.chapter_number or 0),
                    related_object_type="project",
                    related_object_id=project_id,
                )
                session.commit()
                raise HTTPException(409, project_detail.blocking_reason.message)
            total_chapters = session.execute(
                select(func.count(ChapterPlan.id)).where(ChapterPlan.project_id == project_id)
            ).scalar_one()
        finally:
            session.close()

    try:
        accept_review_parameters = inspect.signature(orchestrator.accept_review).parameters
        if "reason" in accept_review_parameters:
            result = orchestrator.accept_review(project_id, chapter_number, reason=reason)
        else:
            result = orchestrator.accept_review(project_id, chapter_number)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc

    accepted_status = str(result.get("status") or "accepted")
    message = result["message"]
    if req.continue_generation and accepted_status == "accepted":
        try:
            task_id = create_continue_generation_task(
                project_id=project_id,
                runtime_config=runtime_config,
                requested_chapters=int(total_chapters or 0),
                message=f"已接受第{chapter_number}章，准备继续后续章节。",
            )
        except active_generation_task_error_cls as exc:
            raise HTTPException(409, str(exc)) from exc
        update_task(
            task_id,
            frozen_artifacts=[result["frozen_artifact"]] if result["frozen_artifact"] else [],
        )
        message = f"{message} 已启动后续章节继续执行。"
    elif req.continue_generation:
        message = f"{message} 未启动后续章节。"

    return ChapterReviewApproveResponse(
        ok=True,
        project_id=project_id,
        chapter_number=chapter_number,
        status=accepted_status,
        message=message,
        task_id=task_id,
        frozen_artifact=result.get("frozen_artifact") or "",
    )


def retry_chapter_review(
    project_id: str,
    chapter_number: int,
    req: ChapterReviewRetryRequest,
    *,
    config,
    runtime_settings,
    get_session,
    active_generation_task_error_cls,
    require_reason,
    resolve_project_governance,
    project_has_active_generation_task,
    generation_task_conflict_message,
    log_decision_event,
    create_continue_generation_task,
) -> ChapterReviewApproveResponse:
    if config is None:
        raise HTTPException(500, "服务尚未完成初始化")

    reason = require_reason(req.reason, action="重试 review 章节")
    runtime_config = build_saved_runtime_config(
        base_config=config,
        runtime_settings=runtime_settings,
    )
    task_id = ""
    total_chapters = 0
    session = get_session()
    try:
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        if project_has_active_generation_task(project_id, session=session):
            raise HTTPException(409, generation_task_conflict_message(project_id))
        governance = resolve_project_governance(project, base_config=config)
        runtime_config = copy_config(
            runtime_config,
            operation_mode=governance.default_operation_mode,
            review_interval_chapters=governance.review_interval_chapters,
            progression_mode=governance.progression_mode,
            auto_band_checkpoint=governance.auto_band_checkpoint,
            band_warn_action=governance.band_warn_action,
            manual_checkpoints_enabled=governance.manual_checkpoints_enabled,
            future_constraints_enabled=governance.future_constraints_enabled,
        )
        plan = session.execute(
            select(ChapterPlan).where(
                ChapterPlan.project_id == project_id,
                ChapterPlan.chapter_number == chapter_number,
            )
        ).scalar_one_or_none()
        if plan is None:
            raise HTTPException(404, f"第{chapter_number}章不存在")
        previous_status = str(plan.status or "")
        allowed_statuses = {"needs_review", "drafted"}
        if bool(getattr(req, "allow_accepted", False)):
            allowed_statuses.add("accepted")
        if previous_status not in allowed_statuses:
            raise HTTPException(
                400,
                f"第{chapter_number}章不是可 retry 状态（当前 {previous_status or 'unknown'}）",
            )
        plan.status = "planned"
        plan.acceptance_mode = ""
        plan.repair_attempt_count = 0
        plan.residual_review_issues_json = "[]"
        plan.canon_risk_level = ""
        goals_payload = _load_json_object(plan.goals_json, [])
        if isinstance(goals_payload, list):
            cleaned_goals = [
                str(item).strip()
                for item in goals_payload
                if len(str(item).strip()) >= 2
            ]
            if cleaned_goals != goals_payload:
                plan.goals_json = json.dumps(cleaned_goals, ensure_ascii=False)
                plan.task_contract_json = plan_task_contract_to_json(
                    derive_chapter_task_contract(cleaned_goals)
                )
        session.add(plan)
        log_decision_event(
            session,
            project_id=project_id,
            event_family="audit_action",
            event_type=DecisionEventType.RETRY_ATTEMPT,
            actor_type="api",
            scope="chapter",
            summary=f"第{chapter_number}章 review 候选已重置为 planned，等待重写。",
            reason=reason,
            payload={"chapter_number": chapter_number, "previous_status": previous_status},
            chapter_number=chapter_number,
            related_object_type="chapter",
            related_object_id=str(plan.id),
        )
        total_chapters = session.execute(
            select(func.count(ChapterPlan.id)).where(ChapterPlan.project_id == project_id)
        ).scalar_one()
        session.commit()
    finally:
        session.close()

    message = f"第{chapter_number}章已重置为 planned。"
    if req.continue_generation:
        try:
            task_id = create_continue_generation_task(
                project_id=project_id,
                runtime_config=runtime_config,
                requested_chapters=int(total_chapters or 0),
                message=f"已重置第{chapter_number}章，准备重新生成。",
            )
        except active_generation_task_error_cls as exc:
            raise HTTPException(409, str(exc)) from exc
        message = f"{message} 已启动后续章节继续执行。"

    return ChapterReviewApproveResponse(
        ok=True,
        project_id=project_id,
        chapter_number=chapter_number,
        status="planned",
        message=message,
        task_id=task_id,
        frozen_artifact="",
    )
