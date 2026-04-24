from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select

from forwin.api_project_payloads import (
    build_provisional_band_detail,
    build_scenario_rehearsal_detail,
    latest_provisional_band_execution,
    latest_scenario_rehearsal_run,
)
from forwin.api_schemas import (
    BandCheckpointApproveRequest,
    BandCheckpointDetail,
    BandExperienceOverrideRequest,
    BandExperienceOverrideResponse,
    CausalReplayResponse,
    ChapterInfo,
    DecisionEventsResponse,
    GovernanceInsightsResponse,
    ManualCheckpointRequest,
    NarrativeConstraintCreateRequest,
    NarrativeConstraintUpdateRequest,
    NarrativeConstraintsResponse,
    ProjectGovernanceResponse,
    ProjectGovernanceUpdateRequest,
    ProvisionalBandDetail,
    ScenarioPlanPatchApproveRequest,
    ScenarioRehearsalDetail,
    TaskContractResponse,
    TaskContractUpdateRequest,
    TropeRegistrySummaryResponse,
    TropeTemplateInfo,
    TropeTemplateValidationRequest,
    TropeTemplateValidationResponse,
)
from forwin.governance import (
    DecisionEventType,
    load_plan_task_contract,
    plan_task_contract_to_json,
)
from forwin.models.base import Base
from forwin.models.governance import BandCheckpoint, NarrativeConstraint
from forwin.models.phase import BandExperiencePlan
from forwin.models.project import ChapterPlan, Project
from forwin.models.world_v4 import ScenarioPlanPatchRow, ScenarioRehearsalRunRow
from forwin.planning.scenario_rehearsal_resolution import ScenarioRehearsalCoordinator
from forwin.protocol.scenario_rehearsal import ScenarioPlanPatch, ScenarioRehearsalReport
from forwin.protocol.experience import BandDelightSchedule
from forwin.protocol.trope_library import (
    TROPE_TEMPLATE_LIBRARY,
    trope_registry_summary,
    validate_trope_template_payload,
)
from forwin.state.repo import StateRepository


def get_project_governance(
    project_id: str,
    *,
    get_session,
    config,
    resolve_project_governance,
) -> ProjectGovernanceResponse:
    session = get_session()
    try:
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        governance = resolve_project_governance(project, base_config=config)
        return ProjectGovernanceResponse(
            ok=True,
            project_id=project_id,
            governance=governance,
            message="已读取项目治理设置。",
        )
    finally:
        session.close()


def update_project_governance(
    project_id: str,
    req: ProjectGovernanceUpdateRequest,
    *,
    get_session,
    config,
    require_reason,
    governance_request_payload,
    resolve_project_governance,
    persist_project_governance,
    log_decision_event,
) -> ProjectGovernanceResponse:
    session = get_session()
    try:
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        reason = require_reason(req.reason, action="修改项目治理设置")
        governance = resolve_project_governance(
            project,
            overrides=governance_request_payload(req),
            base_config=config,
        )
        stored = persist_project_governance(session, project, governance)
        log_decision_event(
            session,
            project_id=project_id,
            event_family="audit_action",
            event_type=DecisionEventType.GOVERNANCE_UPDATED,
            actor_type="manual_ui",
            summary="项目治理设置已更新。",
            reason=reason,
            payload={"governance": stored.model_dump(mode="json")},
        )
        session.commit()
        return ProjectGovernanceResponse(
            ok=True,
            project_id=project_id,
            governance=stored,
            message="项目治理设置已保存。",
        )
    finally:
        session.close()


def create_manual_checkpoint(
    project_id: str,
    req: ManualCheckpointRequest,
    *,
    get_session,
    config,
    require_reason,
    resolve_project_governance,
    serialize_band_checkpoint,
    log_decision_event,
) -> BandCheckpointDetail:
    session = get_session()
    try:
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        reason = require_reason(req.reason, action="创建 manual checkpoint")
        governance = resolve_project_governance(project, base_config=config)
        if not governance.manual_checkpoints_enabled:
            raise HTTPException(409, "当前项目未启用 manual checkpoint。")
        boundary_kind = str(req.boundary_kind or "").strip()
        if boundary_kind not in {"chapter_start", "chapter_accepted", "band_end"}:
            raise HTTPException(400, "manual checkpoint 仅支持章开始前、章 accepted 后、band 结束处。")
        active_arc = session.execute(
            select(Base.metadata.tables["arc_plan_versions"].c.id)
            .where(
                Base.metadata.tables["arc_plan_versions"].c.project_id == project_id,
                Base.metadata.tables["arc_plan_versions"].c.status == "active",
            )
            .order_by(Base.metadata.tables["arc_plan_versions"].c.version.desc())
            .limit(1)
        ).scalar_one_or_none()
        if active_arc is None:
            raise HTTPException(409, "当前项目没有 active arc。")
        chapter_number = max(0, int(req.boundary_chapter or 0))
        band = session.execute(
            select(BandExperiencePlan)
            .where(
                BandExperiencePlan.project_id == project_id,
                BandExperiencePlan.arc_id == active_arc,
                BandExperiencePlan.chapter_start <= max(1, chapter_number or 1),
                BandExperiencePlan.chapter_end >= max(1, chapter_number or 1),
            )
            .order_by(BandExperiencePlan.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if band is None and boundary_kind == "band_end":
            band = session.execute(
                select(BandExperiencePlan)
                .where(
                    BandExperiencePlan.project_id == project_id,
                    BandExperiencePlan.arc_id == active_arc,
                    BandExperiencePlan.chapter_end == chapter_number,
                )
                .order_by(BandExperiencePlan.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()
        if band is None:
            raise HTTPException(400, "未找到对应 band，manual checkpoint 只能落在章边界或 band 边界。")
        if boundary_kind == "band_end":
            chapter_number = int(band.chapter_end or 0)
        row = BandCheckpoint(
            project_id=project_id,
            arc_id=str(active_arc),
            band_id=band.band_id,
            chapter_start=int(band.chapter_start or 0),
            chapter_end=int(band.chapter_end or 0),
            trigger_source="manual_boundary",
            boundary_kind=boundary_kind,
            boundary_chapter=chapter_number,
            status="pending",
            summary="人工 checkpoint 已创建，等待边界命中或人工处理。",
            reason=reason,
            issues_json="[]",
        )
        session.add(row)
        session.flush()
        log_decision_event(
            session,
            project_id=project_id,
            band_id=band.band_id,
            chapter_number=chapter_number,
            event_family="audit_action",
            event_type=DecisionEventType.MANUAL_CHECKPOINT_CREATED,
            actor_type="manual_ui",
            scope="band",
            summary="已插入人工 checkpoint。",
            reason=reason,
            related_object_type="band_checkpoint",
            related_object_id=row.id,
        )
        session.commit()
        session.refresh(row)
        return serialize_band_checkpoint(row, session=session)
    finally:
        session.close()


def get_band_checkpoint(
    project_id: str,
    band_id: str,
    *,
    get_session,
    latest_band_checkpoint_row,
    serialize_band_checkpoint,
) -> BandCheckpointDetail:
    session = get_session()
    try:
        row = latest_band_checkpoint_row(session, project_id=project_id, band_id=band_id)
        if row is None:
            raise HTTPException(404, "band checkpoint 不存在")
        return serialize_band_checkpoint(row, session=session)
    finally:
        session.close()


def approve_band_checkpoint(
    project_id: str,
    band_id: str,
    req: BandCheckpointApproveRequest,
    *,
    get_session,
    latest_band_checkpoint_row,
    latest_related_decision_event,
    require_reason,
    log_decision_event,
    serialize_band_checkpoint,
) -> BandCheckpointDetail:
    session = get_session()
    try:
        row = latest_band_checkpoint_row(session, project_id=project_id, band_id=band_id)
        if row is None:
            raise HTTPException(404, "band checkpoint 不存在")
        parent = latest_related_decision_event(
            session,
            project_id=project_id,
            related_object_type="band_checkpoint",
            related_object_id=row.id,
        )
        next_status = str(req.status or "overridden").strip() or "overridden"
        reason = require_reason(
            req.reason,
            action="pass checkpoint" if next_status == "pass" else "override checkpoint",
        )
        row.status = next_status
        row.reason = reason
        session.add(row)
        session.flush()
        log_decision_event(
            session,
            project_id=project_id,
            band_id=band_id,
            chapter_number=int(row.boundary_chapter or 0),
            event_family="audit_action",
            event_type=DecisionEventType.BAND_CHECKPOINT_APPROVED if next_status == "pass" else DecisionEventType.BAND_CHECKPOINT_OVERRIDDEN,
            actor_type="manual_ui",
            scope="band",
            summary="band checkpoint 已人工放行。",
            reason=reason,
            related_object_type="band_checkpoint",
            related_object_id=row.id,
            parent_event_id=str(parent.id if parent is not None else ""),
            causal_root_id=str(parent.causal_root_id if parent is not None else ""),
        )
        session.commit()
        session.refresh(row)
        return serialize_band_checkpoint(row, session=session)
    finally:
        session.close()


def get_chapter_task_contract(
    project_id: str,
    chapter_number: int,
    *,
    get_session,
) -> TaskContractResponse:
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
        return TaskContractResponse(
            project_id=project_id,
            scope="chapter",
            chapter_number=chapter_number,
            items=load_plan_task_contract(getattr(plan, "task_contract_json", "[]")),
            message="已读取 chapter task contract。",
        )
    finally:
        session.close()


def update_chapter_task_contract(
    project_id: str,
    chapter_number: int,
    req: TaskContractUpdateRequest,
    *,
    get_session,
    require_reason,
    log_decision_event,
) -> TaskContractResponse:
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
        reason = require_reason(req.reason, action="更新 chapter task contract")
        plan.task_contract_json = plan_task_contract_to_json(req.items)
        session.add(plan)
        session.flush()
        log_decision_event(
            session,
            project_id=project_id,
            chapter_number=chapter_number,
            event_family="audit_action",
            event_type=DecisionEventType.PLAN_TASK_CONTRACT_UPDATED,
            actor_type="manual_ui",
            scope="chapter",
            summary=f"第{chapter_number}章 task contract 已更新。",
            reason=reason,
            payload={"scope": "chapter", "item_count": len(req.items)},
            related_object_type="chapter_plan",
            related_object_id=plan.id,
        )
        session.commit()
        return TaskContractResponse(
            project_id=project_id,
            scope="chapter",
            chapter_number=chapter_number,
            items=list(req.items),
            message="chapter task contract 已保存。",
        )
    finally:
        session.close()


def get_band_task_contract(
    project_id: str,
    band_id: str,
    *,
    get_session,
) -> TaskContractResponse:
    session = get_session()
    try:
        row = session.execute(
            select(BandExperiencePlan)
            .where(
                BandExperiencePlan.project_id == project_id,
                BandExperiencePlan.band_id == band_id,
            )
            .order_by(BandExperiencePlan.created_at.desc(), BandExperiencePlan.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, "band 不存在")
        return TaskContractResponse(
            project_id=project_id,
            scope="band",
            band_id=band_id,
            items=load_plan_task_contract(getattr(row, "task_contract_json", "[]")),
            message="已读取 band task contract。",
        )
    finally:
        session.close()


def update_band_task_contract(
    project_id: str,
    band_id: str,
    req: TaskContractUpdateRequest,
    *,
    get_session,
    require_reason,
    log_decision_event,
) -> TaskContractResponse:
    session = get_session()
    try:
        row = session.execute(
            select(BandExperiencePlan)
            .where(
                BandExperiencePlan.project_id == project_id,
                BandExperiencePlan.band_id == band_id,
            )
            .order_by(BandExperiencePlan.created_at.desc(), BandExperiencePlan.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, "band 不存在")
        reason = require_reason(req.reason, action="更新 band task contract")
        row.task_contract_json = plan_task_contract_to_json(req.items)
        session.add(row)
        session.flush()
        log_decision_event(
            session,
            project_id=project_id,
            band_id=band_id,
            event_family="audit_action",
            event_type=DecisionEventType.PLAN_TASK_CONTRACT_UPDATED,
            actor_type="manual_ui",
            scope="band",
            summary=f"{band_id} task contract 已更新。",
            reason=reason,
            payload={
                "scope": "band",
                "item_count": len(req.items),
                "chapter_start": int(row.chapter_start or 0),
                "chapter_end": int(row.chapter_end or 0),
            },
            related_object_type="band_experience_plan",
            related_object_id=row.id,
        )
        session.commit()
        return TaskContractResponse(
            project_id=project_id,
            scope="band",
            band_id=band_id,
            items=list(req.items),
            message="band task contract 已保存。",
        )
    finally:
        session.close()


def list_project_constraints(
    project_id: str,
    *,
    get_session,
    serialize_constraint,
) -> NarrativeConstraintsResponse:
    session = get_session()
    try:
        rows = session.execute(
            select(NarrativeConstraint)
            .where(NarrativeConstraint.project_id == project_id)
            .order_by(NarrativeConstraint.created_at.desc(), NarrativeConstraint.id.desc())
        ).scalars().all()
        return NarrativeConstraintsResponse(items=[serialize_constraint(row) for row in rows])
    finally:
        session.close()


def create_project_constraint(
    project_id: str,
    req: NarrativeConstraintCreateRequest,
    *,
    get_session,
    require_reason,
    validate_constraint_payload,
    log_decision_event,
    serialize_constraint,
) -> Any:
    session = get_session()
    try:
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        reason = require_reason(req.reason, action="创建 narrative constraint")
        constraint_type, level, status = validate_constraint_payload(
            constraint_type=req.constraint_type,
            level=req.level,
            status=req.status,
        )
        row = NarrativeConstraint(
            project_id=project_id,
            arc_id=str(req.arc_id or "").strip(),
            band_id=str(req.band_id or "").strip(),
            constraint_type=constraint_type,
            level=level,
            subject_name=str(req.subject_name or "").strip(),
            description=str(req.description or "").strip(),
            payload_json=json.dumps(req.payload or {}, ensure_ascii=False),
            effective_from_chapter=max(1, int(req.effective_from_chapter or 1)),
            protect_until_chapter=max(0, int(req.protect_until_chapter or 0)),
            status=status,
        )
        session.add(row)
        session.flush()
        log_decision_event(
            session,
            project_id=project_id,
            band_id=row.band_id,
            event_family="audit_action",
            event_type=DecisionEventType.CONSTRAINT_CREATED,
            actor_type="manual_ui",
            scope="project",
            summary="已新增 narrative constraint。",
            reason=reason,
            related_object_type="narrative_constraint",
            related_object_id=row.id,
        )
        session.commit()
        session.refresh(row)
        return serialize_constraint(row)
    finally:
        session.close()


def update_project_constraint(
    project_id: str,
    constraint_id: str,
    req: NarrativeConstraintUpdateRequest,
    *,
    get_session,
    require_reason,
    validate_constraint_payload,
    log_decision_event,
    serialize_constraint,
    json_load_object,
) -> Any:
    session = get_session()
    try:
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        row = session.get(NarrativeConstraint, constraint_id)
        if row is None or row.project_id != project_id:
            raise HTTPException(404, "narrative constraint 不存在")
        reason = require_reason(req.reason, action="更新 narrative constraint")
        old_status = str(row.status or "")
        changes: dict[str, Any] = {}
        next_constraint_type, next_level, next_status = validate_constraint_payload(
            constraint_type=req.constraint_type if req.constraint_type is not None else row.constraint_type,
            level=req.level if req.level is not None else row.level,
            status=req.status if req.status is not None else row.status,
        )

        def _set_if_present(attr: str, value: Any, transform=lambda item: item):
            if value is None:
                return
            next_value = transform(value)
            if getattr(row, attr) != next_value:
                changes[attr] = {"from": getattr(row, attr), "to": next_value}
                setattr(row, attr, next_value)

        _set_if_present("constraint_type", req.constraint_type, lambda _item: next_constraint_type)
        _set_if_present("level", req.level, lambda _item: next_level)
        _set_if_present("subject_name", req.subject_name, lambda item: str(item or "").strip())
        _set_if_present("description", req.description, lambda item: str(item or "").strip())
        if req.payload is not None:
            payload_json = json.dumps(req.payload or {}, ensure_ascii=False)
            if row.payload_json != payload_json:
                changes["payload"] = {"from": json_load_object(row.payload_json), "to": req.payload or {}}
                row.payload_json = payload_json
        _set_if_present("arc_id", req.arc_id, lambda item: str(item or "").strip())
        _set_if_present("band_id", req.band_id, lambda item: str(item or "").strip())
        _set_if_present("effective_from_chapter", req.effective_from_chapter, lambda item: max(1, int(item or 1)))
        _set_if_present("protect_until_chapter", req.protect_until_chapter, lambda item: max(0, int(item or 0)))
        _set_if_present("status", req.status, lambda _item: next_status)
        event_type = (
            DecisionEventType.CONSTRAINT_ARCHIVED
            if old_status == "active" and str(row.status or "") in {"inactive", "archived"}
            else DecisionEventType.CONSTRAINT_UPDATED
        )
        session.add(row)
        session.flush()
        log_decision_event(
            session,
            project_id=project_id,
            band_id=row.band_id,
            event_family="audit_action",
            event_type=event_type,
            actor_type="manual_ui",
            scope="project",
            summary="已停用 narrative constraint。" if event_type == DecisionEventType.CONSTRAINT_ARCHIVED else "已更新 narrative constraint。",
            reason=reason,
            payload={"changes": changes},
            related_object_type="narrative_constraint",
            related_object_id=row.id,
        )
        session.commit()
        session.refresh(row)
        return serialize_constraint(row)
    finally:
        session.close()


def list_project_decision_events(
    project_id: str,
    *,
    get_session,
    list_decision_event_rows,
    serialize_decision_event,
    scope: str = "",
    band_id: str = "",
    chapter_number: int = 0,
    task_id: str = "",
    event_family: str = "",
    related_object_type: str = "",
    related_object_id: str = "",
    causal_root_id: str = "",
) -> DecisionEventsResponse:
    session = get_session()
    try:
        rows = list_decision_event_rows(
            session,
            project_id=project_id,
            scope=scope,
            band_id=band_id,
            chapter_number=chapter_number,
            task_id=task_id,
            event_family=event_family,
            related_object_type=related_object_type,
            related_object_id=related_object_id,
            causal_root_id=causal_root_id,
            limit=200,
            ascending=False,
        )
        return DecisionEventsResponse(items=[serialize_decision_event(row) for row in rows])
    finally:
        session.close()


def get_project_causal_replay(
    project_id: str,
    *,
    get_session,
    build_causal_replay,
    scope: str = "project",
    arc_id: str = "",
    band_id: str = "",
    chapter_number: int = 0,
    task_id: str = "",
) -> CausalReplayResponse:
    session = get_session()
    try:
        return build_causal_replay(
            session,
            project_id=project_id,
            scope=str(scope or "project").strip() or "project",
            arc_id=str(arc_id or "").strip(),
            band_id=band_id,
            chapter_number=chapter_number,
            task_id=task_id,
        )
    finally:
        session.close()


def get_project_governance_insights(
    project_id: str,
    *,
    get_session,
    build_governance_insights,
) -> GovernanceInsightsResponse:
    session = get_session()
    try:
        return build_governance_insights(session, project_id=project_id)
    finally:
        session.close()


def get_latest_provisional_band(
    project_id: str,
    *,
    get_session,
    display_datetime,
) -> ProvisionalBandDetail:
    session = get_session()
    try:
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")

        latest = latest_provisional_band_execution(session, project_id)
        if latest is None:
            raise HTTPException(404, "项目暂无 provisional 预演记录")
        return build_provisional_band_detail(
            session=session,
            project_id=project_id,
            latest=latest,
            display_datetime=display_datetime,
        )
    finally:
        session.close()


def get_latest_scenario_rehearsal(
    project_id: str,
    *,
    get_session,
    display_datetime,
) -> ScenarioRehearsalDetail:
    session = get_session()
    try:
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")

        latest = latest_scenario_rehearsal_run(session, project_id)
        if latest is None:
            raise HTTPException(404, "项目暂无 scenario rehearsal 记录")
        return build_scenario_rehearsal_detail(
            project_id=project_id,
            latest=latest,
            display_datetime=display_datetime,
        )
    finally:
        session.close()


def rerun_scenario_rehearsal(
    project_id: str,
    run_id: str,
    *,
    get_session,
    display_datetime,
) -> ScenarioRehearsalDetail:
    session = get_session()
    try:
        run = session.get(ScenarioRehearsalRunRow, run_id)
        if run is None or run.project_id != project_id:
            raise HTTPException(404, "scenario rehearsal run 不存在")
        try:
            chapter_numbers = json.loads(run.chapter_numbers_json or "[]") or []
        except (json.JSONDecodeError, TypeError):
            chapter_numbers = []
        chapter_numbers = [
            int(item)
            for item in chapter_numbers
            if str(item).strip().lstrip("-").isdigit()
        ]
        outcome = ScenarioRehearsalCoordinator(session).run_for_band(
            project_id=project_id,
            arc_id=str(run.arc_id or ""),
            band_id=str(run.band_id or ""),
            chapter_numbers=chapter_numbers,
        )
        session.commit()
        report = outcome.report
        return ScenarioRehearsalDetail(
            project_id=project_id,
            arc_id=report.arc_id,
            band_id=report.band_id,
            rehearsal_scope=report.rehearsal_scope,
            chapter_numbers=list(report.chapter_numbers),
            trigger_reasons=list(report.trigger_reasons),
            recommendation=report.recommendation.value,
            risk_count=len(report.risk_findings),
            blocker_count=sum(1 for item in report.risk_findings if item.severity == "fail"),
            required_patch_count=len(report.required_plan_patches),
            resolution_status=report.resolution_status,
            patch_attempt_count=report.patch_attempt_count,
            checkpoint_id=report.checkpoint_id,
            replan_event_id=report.replan_event_id,
            report=report.model_dump(mode="json"),
            created_at=display_datetime(None),
        )
    finally:
        session.close()


def approve_scenario_plan_patch(
    project_id: str,
    patch_id: str,
    *,
    reason: str,
    get_session,
    display_datetime,
) -> ScenarioRehearsalDetail:
    session = get_session()
    try:
        patch_row = session.get(ScenarioPlanPatchRow, patch_id)
        if patch_row is None or patch_row.project_id != project_id:
            raise HTTPException(404, "scenario plan patch 不存在")
        run = session.get(ScenarioRehearsalRunRow, patch_row.run_id)
        if run is None:
            raise HTTPException(404, "scenario rehearsal run 不存在")
        try:
            report = ScenarioRehearsalReport.model_validate(json.loads(run.report_json or "{}"))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise HTTPException(409, "scenario rehearsal report 无法解析") from exc
        try:
            patch = ScenarioPlanPatch.model_validate(json.loads(patch_row.patch_json or "{}"))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise HTTPException(409, "scenario plan patch 无法解析") from exc
        coordinator = ScenarioRehearsalCoordinator(session)
        applied = coordinator._apply_known_patches(  # noqa: SLF001 - API shares the patch executor.
            report.model_copy(update={"required_plan_patches": [patch]})
        )
        status = applied[0].status if applied else "failed"
        patch_row.status = "applied" if status == "applied" else status
        patch_row.approval_reason = str(reason or "")
        patch_row.applied_at = datetime.now(timezone.utc) if patch_row.status == "applied" else None
        session.add(patch_row)
        session.commit()
        detail = build_scenario_rehearsal_detail(
            project_id=project_id,
            latest=run,
            display_datetime=display_datetime,
        )
        updated_report = dict(detail.report)
        updated_report.update(
            {
                "applied_patch_id": patch_id,
                "patch_status": patch_row.status,
                "approval_reason": patch_row.approval_reason,
            }
        )
        return detail.model_copy(update={"report": updated_report})
    finally:
        session.close()


def get_trope_templates(
    *,
    category: str = "",
    q: str = "",
    limit: int = 0,
) -> list[TropeTemplateInfo]:
    normalized_category = str(category or "").strip()
    normalized_query = str(q or "").strip().lower()
    templates = list(TROPE_TEMPLATE_LIBRARY)
    if normalized_category:
        templates = [
            template
            for template in templates
            if str(template.category) == normalized_category
        ]
    if normalized_query:
        templates = [
            template
            for template in templates
            if normalized_query
            in " ".join(
                [
                    template.template_id,
                    template.display_name,
                    template.setup_requirement,
                    template.payoff_shape,
                    " ".join(template.risk_flags),
                    " ".join(template.recommended_hook_types),
                ]
            ).lower()
        ]
    if limit > 0:
        templates = templates[:limit]
    return [
        TropeTemplateInfo.model_validate(template.model_dump(mode="json"))
        for template in templates
    ]


def get_trope_template_summary() -> TropeRegistrySummaryResponse:
    return TropeRegistrySummaryResponse.model_validate(
        trope_registry_summary().model_dump(mode="json")
    )


def validate_trope_templates(req: TropeTemplateValidationRequest) -> TropeTemplateValidationResponse:
    templates, errors = validate_trope_template_payload(
        req.templates,
        require_full=bool(req.require_full),
    )
    category_counts: dict[str, int] = {}
    for template in templates:
        category_counts[str(template.category)] = category_counts.get(str(template.category), 0) + 1
    return TropeTemplateValidationResponse(
        ok=not errors,
        total_count=len(templates),
        category_counts=category_counts,
        errors=errors,
    )


def override_band_experience(
    project_id: str,
    band_id: str,
    req: BandExperienceOverrideRequest,
    *,
    get_session,
    orchestrator,
) -> BandExperienceOverrideResponse:
    session = get_session()
    try:
        repo = StateRepository(session)
        active_arc = repo.get_active_arc_plan(project_id)
        if active_arc is None:
            raise HTTPException(404, "当前项目没有 active arc")
        band_row = session.execute(
            select(BandExperiencePlan)
            .where(
                BandExperiencePlan.project_id == project_id,
                BandExperiencePlan.arc_id == active_arc.id,
                BandExperiencePlan.band_id == band_id,
            )
            .order_by(BandExperiencePlan.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if band_row is None:
            raise HTTPException(404, f"band 不存在: {band_id}")

        current_payload = json.loads(band_row.schedule_json or "{}") if band_row.schedule_json else {}
        if not isinstance(current_payload, dict):
            current_payload = {}
        if req.scheduled_rewards:
            current_payload["scheduled_rewards"] = req.scheduled_rewards
        if req.curiosity_beats:
            current_payload["curiosity_beats"] = req.curiosity_beats
        if req.immersion_anchor_scene_goal.strip():
            current_payload["immersion_anchor_scene_goal"] = req.immersion_anchor_scene_goal.strip()
        current_payload.setdefault("band_id", band_row.band_id)
        current_payload.setdefault("chapter_start", band_row.chapter_start)
        current_payload.setdefault("chapter_end", band_row.chapter_end)
        current_payload.setdefault("stall_guard_max_gap", band_row.stall_guard_max_gap)

        schedule = BandDelightSchedule.model_validate(current_payload)
        band_row.schedule_json = json.dumps(schedule.model_dump(mode="json"), ensure_ascii=False)
        band_row.stall_guard_max_gap = schedule.stall_guard_max_gap
        session.add(band_row)

        if orchestrator is not None:
            arc_structure = repo.get_latest_arc_structure_draft(project_id)
            structure_data = orchestrator._structure_data_from_row(arc_structure)
            for chapter_number in range(schedule.chapter_start, schedule.chapter_end + 1):
                chapter_plan = repo.get_chapter_plan(project_id, chapter_number)
                if chapter_plan is None:
                    continue
                experience_plan = orchestrator.arc_envelope_manager._derive_chapter_experience_plan(
                    chapter_number=chapter_number,
                    structure=structure_data,
                    schedule=schedule,
                    chapter_plan=chapter_plan,
                )
                chapter_plan.experience_plan_json = json.dumps(
                    experience_plan.model_dump(mode="json"),
                    ensure_ascii=False,
                )
                session.add(chapter_plan)
        session.commit()
        return BandExperienceOverrideResponse(
            ok=True,
            project_id=project_id,
            band_id=band_id,
            chapter_start=schedule.chapter_start,
            chapter_end=schedule.chapter_end,
            message="band experience overlay 已更新并重生成 chapter experience plans",
        )
    finally:
        session.close()
