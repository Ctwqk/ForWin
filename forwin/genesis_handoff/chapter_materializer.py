from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from forwin.models.genesis import BookGenesisRevision
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from forwin.state.updater import StateUpdater


def _book_genesis():
    from forwin import book_genesis

    return book_genesis


class GenesisChapterMaterializer:
    def __init__(self, owner: Any) -> None:
        self.owner = owner

    def materialize_arc_chapter_plans(
        self,
        *,
        session: Session,
        updater: StateUpdater,
        project: Project,
        revision: BookGenesisRevision,
        arc_number: int,
        decision_event_id: str = "",
        ensure_arc_map: bool = True,
    ) -> ArcPlanVersion:
        book_genesis = _book_genesis()
        pack = self.owner.load_pack(revision)
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
                self.owner._ensure_arc_map_expansion(
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
        planned, trace_payload = self.owner._plan_arc_chapters(
            project=project,
            pack=pack,
            arc_payload=arc_payload,
            chapter_count=chapter_count,
        )
        if str(decision_event_id or "").strip():
            trace_payload = self.owner._prepare_trace_payload_for_save(trace_payload, project_id=project.id)
            trace = updater.save_prompt_trace(
                project_id=project.id,
                genesis_revision_id=str(getattr(revision, "id", "") or ""),
                decision_event_id=str(decision_event_id or "").strip(),
                trace_scope="start_writing",
                stage_key=f"launch_arc_{arc_row.arc_number}",
                template_id=f"launch_arc_plan:{arc_row.arc_number}",
                template_version="v1",
                effective_system_prompt=str(trace_payload.get("effective_system_prompt", "")),
                prompt_layers_json=book_genesis._json_dump(trace_payload.get("prompt_layers", [])),
                input_snapshot_json=book_genesis._json_dump(trace_payload.get("input_snapshot", {})),
                model_profile_json=book_genesis._json_dump(trace_payload.get("model_profile", {})),
                attempts_json=book_genesis._json_dump(trace_payload.get("attempts", [])),
                output_summary_json=book_genesis._json_dump(trace_payload.get("output_summary", {})),
                backend=str(trace_payload.get("backend", "") or ""),
                codex_job_id=str(trace_payload.get("codex_job_id", "") or ""),
                permission_profile=str(trace_payload.get("permission_profile", "") or ""),
                fallback_used=bool(trace_payload.get("fallback_used", False)),
            )
            self.owner._record_llm_events_for_trace(
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
            self.owner._ensure_arc_map_expansion(
                session=session,
                updater=updater,
                project=project,
                revision=revision,
                pack=pack,
                arc_row=arc_row,
                parent_event_id=decision_event_id,
            )
        return arc_row

