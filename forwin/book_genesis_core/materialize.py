from __future__ import annotations

from forwin.book_genesis_core.constants import *
from forwin.book_genesis_core.helpers import *
from forwin.book_genesis_core.fallbacks import *
from forwin.book_genesis_core.names_paths import *

def materialize_book_arcs(
    self,
    *,
    session: Session,
    updater: StateUpdater,
    project: Project,
    revision,
) -> list[ArcPlanVersion]:
    return self.handoff.arc_materializer.materialize_book_arcs(
        session=session,
        updater=updater,
        project=project,
        revision=revision,
    )
    pack = self.load_pack(revision)
    blueprint = pack.get("book_arc_blueprint") if isinstance(pack.get("book_arc_blueprint"), dict) else {}
    arc_items = [item for item in (blueprint.get("arcs") or []) if isinstance(item, dict)]
    if not arc_items:
        raise ValueError("Genesis blueprint 尚未生成 arcs。")
    existing_rows = session.execute(
        select(ArcPlanVersion)
        .where(ArcPlanVersion.project_id == project.id)
        .order_by(ArcPlanVersion.arc_number.asc(), ArcPlanVersion.created_at.asc())
    ).scalars().all()
    if existing_rows:
        return existing_rows
    created: list[ArcPlanVersion] = []
    for index, arc_payload in enumerate(arc_items, start=1):
        created.append(
            updater.create_arc_plan(
                project_id=project.id,
                arc_synopsis=str(arc_payload.get("arc_synopsis", "")).strip() or f"Arc {index}",
                version=index,
                status="active" if index == 1 else "planned",
                arc_number=int(arc_payload.get("arc_number", index) or index),
                chapter_start=int(arc_payload.get("chapter_start", 1) or 1),
                chapter_end=int(arc_payload.get("chapter_end", 0) or 0),
                planned_target_size=int(arc_payload.get("target_size", 0) or 0),
                planned_soft_min=int(arc_payload.get("soft_min", 0) or 0),
                planned_soft_max=int(arc_payload.get("soft_max", 0) or 0),
            )
        )
    session.flush()
    return created

def materialize_arc_chapter_plans(
    self,
    *,
    session: Session,
    updater: StateUpdater,
    project: Project,
    revision,
    arc_number: int,
    decision_event_id: str = "",
    ensure_arc_map: bool = True,
) -> ArcPlanVersion:
    return self.handoff.chapter_materializer.materialize_arc_chapter_plans(
        session=session,
        updater=updater,
        project=project,
        revision=revision,
        arc_number=arc_number,
        decision_event_id=decision_event_id,
        ensure_arc_map=ensure_arc_map,
    )
    pack = self.load_pack(revision)
    blueprint = pack.get("book_arc_blueprint") if isinstance(pack.get("book_arc_blueprint"), dict) else {}
    arc_payload = next(
        (
            item
            for item in (blueprint.get("arcs") or [])
            if isinstance(item, dict) and int(item.get("arc_number", 0) or 0) == int(arc_number or 0)
        ),
        None,
    )
    if arc_payload is None:
        raise ValueError(f"Genesis blueprint 不存在 arc {arc_number}")
    arc_row = session.execute(
        select(ArcPlanVersion)
        .where(
            ArcPlanVersion.project_id == project.id,
            ArcPlanVersion.arc_number == int(arc_number or 0),
        )
        .limit(1)
    ).scalar_one_or_none()
    if arc_row is None:
        raise ValueError(f"Arc {arc_number} skeleton 不存在")
    existing = session.execute(
        select(func.count(ChapterPlan.id)).where(ChapterPlan.arc_plan_id == arc_row.id)
    ).scalar_one()
    if int(existing or 0) > 0:
        if ensure_arc_map:
            self._ensure_arc_map_expansion(
                session=session,
                updater=updater,
                project=project,
                revision=revision,
                pack=pack,
                arc_row=arc_row,
                parent_event_id=decision_event_id,
            )
        return arc_row
    chapter_start = int(arc_payload.get("chapter_start", 1) or 1)
    chapter_end = int(arc_payload.get("chapter_end", chapter_start) or chapter_start)
    chapter_count = max(1, int(arc_payload.get("chapter_count", chapter_end - chapter_start + 1) or 1))
    planned, trace_payload = self._plan_arc_chapters(
        project=project,
        pack=pack,
        arc_payload=arc_payload,
        chapter_count=chapter_count,
    )
    if str(decision_event_id or "").strip():
        trace_payload = self._prepare_trace_payload_for_save(trace_payload, project_id=project.id)
        trace = updater.save_prompt_trace(
            project_id=project.id,
            genesis_revision_id=str(getattr(revision, "id", "") or ""),
            decision_event_id=str(decision_event_id or "").strip(),
            trace_scope="start_writing",
            stage_key=f"launch_arc_{arc_row.arc_number}",
            template_id=f"launch_arc_plan:{arc_row.arc_number}",
            template_version="v1",
            effective_system_prompt=str(trace_payload.get("effective_system_prompt", "")),
            prompt_layers_json=_json_dump(trace_payload.get("prompt_layers", [])),
            input_snapshot_json=_json_dump(trace_payload.get("input_snapshot", {})),
            model_profile_json=_json_dump(trace_payload.get("model_profile", {})),
            attempts_json=_json_dump(trace_payload.get("attempts", [])),
            output_summary_json=_json_dump(trace_payload.get("output_summary", {})),
            backend=str(trace_payload.get("backend", "") or ""),
            codex_job_id=str(trace_payload.get("codex_job_id", "") or ""),
            permission_profile=str(trace_payload.get("permission_profile", "") or ""),
            fallback_used=bool(trace_payload.get("fallback_used", False)),
        )
        self._record_llm_events_for_trace(
            updater=updater,
            project_id=project.id,
            trace_id=trace.id,
            trace_payload=trace_payload,
            decision_event_id=str(decision_event_id or "").strip(),
        )
    for index in range(chapter_count):
        number = chapter_start + index
        item = planned[index] if index < len(planned) else {}
        updater.create_chapter_plan(
            project_id=project.id,
            arc_plan_id=arc_row.id,
            chapter_number=number,
            title=str(item.get("title", "")).strip() or f"第{number}章",
            one_line=str(item.get("one_line", "")).strip() or f"推进 arc {arc_number} 冲突。",
            goals=[
                str(goal).strip()
                for goal in (item.get("goals") or [])
                if str(goal).strip()
            ][:3]
            or ["推进主线冲突", "兑现当前阶段承诺"],
        )
    session.flush()
    if ensure_arc_map:
        self._ensure_arc_map_expansion(
            session=session,
            updater=updater,
            project=project,
            revision=revision,
            pack=pack,
            arc_row=arc_row,
            parent_event_id=decision_event_id,
        )
    return arc_row

def _ensure_arc_map_expansion(
    self,
    *,
    session: Session,
    updater: StateUpdater,
    project: Project,
    revision,
    pack: dict[str, Any],
    arc_row: ArcPlanVersion,
    parent_event_id: str = "",
) -> None:
    world = pack.get("world") if isinstance(pack.get("world"), dict) else {}
    map_atlas = world.get("map_atlas") if isinstance(world.get("map_atlas"), dict) else {}
    if not map_atlas and isinstance(pack.get("map_atlas"), dict):
        map_atlas = pack["map_atlas"]
    updater.save_decision_event(
        DecisionEventInfo(
            project_id=project.id,
            scope="project",
            event_family="runtime_observation",
            event_type=DecisionEventType.MAP_EXPANSION_STARTED,
            actor_type="system",
            summary=f"开始为 Arc {arc_row.arc_number} 补齐 Genesis BookMap。",
            payload=audit_payload(
                stage="map_expansion",
                status="started",
                arc_number=arc_row.arc_number,
            ),
            related_object_type="arc_plan_version",
            related_object_id=arc_row.id,
            parent_event_id=parent_event_id,
        )
    )
    try:
        result = ensure_book_map_from_genesis_atlas(
            session,
            project_id=project.id,
            genesis_revision_id=str(getattr(revision, "id", "") or ""),
            map_atlas=map_atlas,
            commit=False,
        )
    except Exception as exc:
        updater.save_decision_event(
            DecisionEventInfo(
                project_id=project.id,
                scope="project",
                event_family="runtime_observation",
                event_type=DecisionEventType.MAP_EXPANSION_FAILED,
                actor_type="system",
                summary=f"Arc {arc_row.arc_number} BookMap expansion 失败。",
                reason=str(exc),
                payload=event_error_payload(
                    exc,
                    stage="map_expansion",
                    arc_number=arc_row.arc_number,
                ),
                related_object_type="arc_plan_version",
                related_object_id=arc_row.id,
                parent_event_id=parent_event_id,
            )
        )
        raise
    if not result.validation_report.valid:
        message = "；".join(result.validation_report.errors) or "BookMap expansion validation failed."
        updater.save_decision_event(
            DecisionEventInfo(
                project_id=project.id,
                scope="project",
                event_family="runtime_observation",
                event_type=DecisionEventType.MAP_EXPANSION_FAILED,
                actor_type="system",
                summary=f"Arc {arc_row.arc_number} BookMap expansion 校验失败。",
                reason=message,
                payload=audit_payload(
                    stage="map_expansion",
                    status="failed",
                    arc_number=arc_row.arc_number,
                    validation_report=result.validation_report.model_dump(mode="json"),
                ),
                related_object_type="arc_plan_version",
                related_object_id=arc_row.id,
                parent_event_id=parent_event_id,
            )
        )
        raise ValueError(message)
    updater.save_decision_event(
        DecisionEventInfo(
            project_id=project.id,
            scope="project",
            event_family="runtime_observation",
            event_type=DecisionEventType.MAP_EXPANSION_SUCCEEDED,
            actor_type="system",
            summary=f"Arc {arc_row.arc_number} BookMap expansion 已完成。",
            payload=audit_payload(
                stage="map_expansion",
                status="succeeded",
                arc_number=arc_row.arc_number,
                summary=dict(result.summary),
                validation_report=result.validation_report.model_dump(mode="json"),
            ),
            related_object_type="arc_plan_version",
            related_object_id=arc_row.id,
            parent_event_id=parent_event_id,
        )
    )

def promote_next_arc_if_needed(
    self,
    *,
    session: Session,
    updater: StateUpdater,
    project: Project,
    revision,
) -> bool:
    next_arc = session.execute(
        select(ArcPlanVersion)
        .where(
            ArcPlanVersion.project_id == project.id,
            ArcPlanVersion.status == "planned",
        )
        .order_by(ArcPlanVersion.arc_number.asc(), ArcPlanVersion.created_at.asc())
        .limit(1)
    ).scalar_one_or_none()
    if next_arc is None:
        return False
    active_rows = session.execute(
        select(ArcPlanVersion)
        .where(
            ArcPlanVersion.project_id == project.id,
            ArcPlanVersion.status == "active",
        )
    ).scalars().all()
    for row in active_rows:
        row.status = "completed"
        session.add(row)
    next_arc.status = "active"
    session.add(next_arc)
    self.materialize_arc_chapter_plans(
        session=session,
        updater=updater,
        project=project,
        revision=revision,
        arc_number=next_arc.arc_number,
    )
    session.flush()
    return True



__all__ = ['materialize_book_arcs', 'materialize_arc_chapter_plans', '_ensure_arc_map_expansion', 'promote_next_arc_if_needed']
