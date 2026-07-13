from __future__ import annotations


from fastapi import HTTPException
from sqlalchemy import func, select

from forwin.application.read_models import build_project_detail, normalize_project_automation
from forwin.api_schema import (
    ProjectAutomationUpdateRequest,
    ProjectAutomationUpdateResponse,
    ProjectContinueGenerationRequest,
    ProjectDetail,
    ProjectExtendGenerationRequest,
    TaskResponse,
)
from forwin.generation.continue_workset import (
    build_continue_generation_workset,
)
from forwin.generation.run_target import resolve_generation_run_target
from forwin.audit.events import DecisionEventType
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from forwin.state.query_helpers import load_latest_drafts_by_plan_id
from forwin.state.updater import StateUpdater
from .common import (
    _continue_workset_http_error,
    _extension_arc_synopsis,
    _extension_chapter_blueprint,
    _extension_continuity_guard,
)


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

def _reset_orphan_needs_review_plans(
    session,
    plans: list[ChapterPlan],
) -> tuple[list[int], bool]:
    needs_review_plans = [
        plan
        for plan in plans
        if str(getattr(plan, "status", "") or "") == "needs_review"
    ]
    if not needs_review_plans:
        return [], False

    latest_drafts = load_latest_drafts_by_plan_id(
        session,
        [str(plan.id or "") for plan in needs_review_plans],
    )
    waiting_review: list[int] = []
    reset_any = False
    for plan in needs_review_plans:
        plan_id = str(plan.id or "")
        chapter_number = int(getattr(plan, "chapter_number", 0) or 0)
        if plan_id and plan_id in latest_drafts:
            waiting_review.append(chapter_number)
            continue
        plan.status = "planned"
        session.add(plan)
        reset_any = True
    return waiting_review, reset_any


def continue_project_generation(
    project_id: str,
    req: ProjectContinueGenerationRequest | None = None,
    *,
    get_session,
    config,
    display_datetime,
    active_generation_task_error_cls,
    project_has_active_generation_task,
    generation_task_conflict_message,
    log_decision_event,
    create_continue_generation_task,
    serialize_task,
    get_generation_task_or_404,
) -> TaskResponse:
    if not config:
        raise HTTPException(503, "服务尚未初始化")
    session = get_session()
    try:
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        if str(project.creation_status or "") in {"creating", "genesis_ready"}:
            raise HTTPException(
                409, "该项目仍在 Genesis 阶段，请先完成创世并点击“启动写作”。"
            )
        if project_has_active_generation_task(project_id, session=session):
            raise HTTPException(409, generation_task_conflict_message(project_id))
        plans = (
            session.execute(
                select(ChapterPlan)
                .where(ChapterPlan.project_id == project_id)
                .order_by(ChapterPlan.chapter_number.asc())
            )
            .scalars()
            .all()
        )
        waiting_review, reset_orphan_review = _reset_orphan_needs_review_plans(
            session, plans
        )
        if reset_orphan_review:
            session.commit()
            plans = (
                session.execute(
                    select(ChapterPlan)
                    .where(ChapterPlan.project_id == project_id)
                    .order_by(ChapterPlan.chapter_number.asc())
                )
                .scalars()
                .all()
            )
        if waiting_review:
            raise HTTPException(
                409,
                f"仍有章节等待 review：{', '.join(str(item) for item in waiting_review)}",
            )
        waiting_acceptance = [
            plan.chapter_number for plan in plans if plan.status == "drafted"
        ]
        if waiting_acceptance:
            raise HTTPException(
                409,
                f"仍有章节等待接受：{', '.join(str(item) for item in waiting_acceptance)}",
            )
        project_detail = build_project_detail(
            session=session,
            project=project,
            display_datetime=display_datetime,
        )
        if project_detail.blocking_reason.code:
            log_decision_event(
                session,
                project_id=project_id,
                event_family="evaluation_verdict",
                event_type=DecisionEventType.HARD_GATE_HIT,
                actor_type="api",
                scope="project",
                summary=project_detail.blocking_reason.message
                or project_detail.blocking_reason.code,
                payload={"blocking_reason": project_detail.blocking_reason.code},
                band_id=project_detail.blocking_reason.band_id,
                chapter_number=int(project_detail.blocking_reason.chapter_number or 0),
                related_object_type="project",
                related_object_id=project_id,
            )
            session.commit()
            raise HTTPException(409, project_detail.blocking_reason.message)
        max_chapters = req.max_chapters if req is not None else None
        auto_continue = (
            True
            if req is None or req.auto_continue is None
            else bool(req.auto_continue)
        )
        run_until_chapter = req.run_until_chapter if req is not None else None
        workset = build_continue_generation_workset(
            session,
            project_id,
            max_chapters=max_chapters,
            source="direct_continue",
        )
        if workset.requested_chapters <= 0:
            raise _continue_workset_http_error(workset)
        task_max_chapters = max_chapters
        task_run_until_chapter = run_until_chapter
        if run_until_chapter is not None:
            first_chapter = int(workset.chapter_numbers[0])
            try:
                target = resolve_generation_run_target(
                    project,
                    next_chapter=first_chapter,
                    run_until_chapter=run_until_chapter,
                    max_chapters=max_chapters,
                )
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            task_max_chapters = target.effective_max_chapters
            task_run_until_chapter = target.run_until_chapter
            workset = build_continue_generation_workset(
                session,
                project_id,
                max_chapters=task_max_chapters,
                source="direct_continue",
            )
            if workset.requested_chapters <= 0:
                raise _continue_workset_http_error(workset)
        elif max_chapters is not None:
            first_chapter = int(workset.chapter_numbers[0])
            target_total = int(getattr(project, "target_total_chapters", 0) or 0)
            batch_end_chapter = first_chapter + int(max_chapters) - 1
            if target_total >= first_chapter:
                batch_end_chapter = min(batch_end_chapter, target_total)
            task_run_until_chapter = batch_end_chapter
        task_id = create_continue_generation_task(
            project_id=project_id,
            requested_chapters=workset.requested_chapters,
            max_chapters=task_max_chapters,
            auto_continue=auto_continue,
            run_until_chapter=task_run_until_chapter,
            title=project.title,
            subtitle=f"继续生成 · {project.genre}",
            message="准备继续生成剩余章节。",
        )
    except active_generation_task_error_cls as exc:
        raise HTTPException(409, str(exc)) from exc
    finally:
        session.close()
    return serialize_task(task_id, get_generation_task_or_404(task_id))


def extend_project_generation(
    project_id: str,
    req: ProjectExtendGenerationRequest,
    *,
    get_session,
    display_datetime,
    project_has_active_generation_task,
    generation_task_conflict_message,
) -> ProjectDetail:
    session = get_session()
    try:
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        if str(project.creation_status or "") in {"creating", "genesis_ready"}:
            raise HTTPException(
                409, "该项目仍在 Genesis 阶段，请先完成创世并点击“启动写作”。"
            )
        if project_has_active_generation_task(project_id, session=session):
            raise HTTPException(409, generation_task_conflict_message(project_id))

        plans = (
            session.execute(
                select(ChapterPlan)
                .where(ChapterPlan.project_id == project_id)
                .order_by(ChapterPlan.chapter_number.asc(), ChapterPlan.id.asc())
            )
            .scalars()
            .all()
        )
        waiting_review = [
            plan.chapter_number for plan in plans if plan.status == "needs_review"
        ]
        if waiting_review:
            raise HTTPException(
                409,
                f"仍有章节等待 review：{', '.join(str(item) for item in waiting_review)}",
            )
        waiting_acceptance = [
            plan.chapter_number for plan in plans if plan.status == "drafted"
        ]
        if waiting_acceptance:
            raise HTTPException(
                409,
                f"仍有章节等待接受：{', '.join(str(item) for item in waiting_acceptance)}",
            )
        failed_generation = [
            plan.chapter_number for plan in plans if str(plan.status or "") == "failed"
        ]
        if failed_generation:
            raise HTTPException(
                409,
                "已有失败章节待处理，请先使用 retry/continue-generation："
                + ", ".join(str(item) for item in failed_generation[:12]),
            )

        additional_chapters = int(req.additional_chapters or 0)
        last_chapter = max([int(plan.chapter_number or 0) for plan in plans] or [0])
        start_chapter = last_chapter + 1
        end_chapter = start_chapter + additional_chapters - 1
        if end_chapter <= last_chapter:
            raise HTTPException(400, "追加章节数必须大于 0")

        max_arc_number = session.execute(
            select(func.max(ArcPlanVersion.arc_number)).where(
                ArcPlanVersion.project_id == project_id
            )
        ).scalar_one()
        next_arc_number = int(max_arc_number or 0) + 1
        guard = _extension_continuity_guard(req)
        updater = StateUpdater(session)
        project.target_total_chapters = max(
            int(project.target_total_chapters or 0), end_chapter
        )
        session.add(project)
        arc = updater.create_arc_plan(
            project_id=project_id,
            arc_synopsis=_extension_arc_synopsis(
                req=req,
                start_chapter=start_chapter,
                end_chapter=end_chapter,
            ),
            version=1,
            status="planned",
            arc_number=next_arc_number,
            chapter_start=start_chapter,
            chapter_end=end_chapter,
            planned_target_size=additional_chapters,
            planned_soft_min=max(1, int(round(additional_chapters * 0.85))),
            planned_soft_max=max(
                additional_chapters, int(round(additional_chapters * 1.20))
            ),
        )
        for offset, chapter_number in enumerate(range(start_chapter, end_chapter + 1)):
            title, one_line, goals, experience_plan = _extension_chapter_blueprint(
                chapter_number=chapter_number,
                offset=offset,
                guard=guard,
                end_chapter=end_chapter,
            )
            updater.create_chapter_plan(
                project_id=project_id,
                arc_plan_id=arc.id,
                chapter_number=chapter_number,
                title=title,
                one_line=one_line,
                goals=goals,
                experience_plan=experience_plan,
            )
        session.commit()
        return build_project_detail(
            session=session,
            project=project,
            display_datetime=display_datetime,
        )
    finally:
        session.close()


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
                "daily_plan_quota": req.daily_plan_quota,
                "daily_write_quota": req.daily_write_quota,
                "daily_review_quota": req.daily_review_quota,
                "daily_publish_quota": req.daily_publish_quota,
                "stop_when_review_pending": bool(req.stop_when_review_pending),
                "auto_publish": bool(req.auto_publish),
            }
        )
        if req.publish is not None:
            payload["publish"] = req.publish.model_dump(mode="json")
        if req.publish_bindings is not None:
            payload["publish_bindings"] = [
                binding.model_dump(mode="json") for binding in req.publish_bindings
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


__all__ = [
    "continue_project_generation",
    "extend_project_generation",
    "update_project_automation",
]
