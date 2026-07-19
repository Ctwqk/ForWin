from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.api_schema import (
    ProjectSummary,
)
from forwin.models.draft import ChapterReview
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from forwin.runtime.policy_store import ProjectPolicyStore
from forwin.state.query_helpers import (
    load_latest_drafts_by_plan_id,
)
from .arc_snapshot import (
    _decision_timeline_by_project,
    _latest_band_checkpoint_by_project,
    project_arc_snapshot_payload,
)
from .generation import build_generation_control, effective_target_total_chapters
from .genesis import (
    _can_start_writing,
    _load_latest_genesis_revision_by_project,
    _stage_overview_from_revision,
)
from .runtime_maps import (
    load_project_runtime_maps,
    load_project_upload_stats,
    normalize_project_automation,
)


DisplayDatetime = Callable[[datetime | None], str]
_GENESIS_STAGE_ORDER = (
    "brief",
    "world",
    "map",
    "story_engine",
    "book_blueprint",
    "bootstrap",
)
_PROJECT_DETAIL_CHAPTER_PREVIEW_LIMIT = 60
_PROJECT_SUMMARY_CHAPTER_PREVIEW_LIMIT = 3


def build_project_summaries(
    *,
    session: Session,
    projects: list[Project],
    display_datetime: DisplayDatetime,
) -> list[ProjectSummary]:
    project_ids = [project.id for project in projects]
    plans = (
        session.execute(
            select(ChapterPlan)
            .where(ChapterPlan.project_id.in_(project_ids))
            .order_by(ChapterPlan.project_id, ChapterPlan.chapter_number)
        )
        .scalars()
        .all()
        if project_ids
        else []
    )
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
    runtime_maps = load_project_runtime_maps(session, project_ids)
    upload_stats = load_project_upload_stats(session, project_ids)
    genesis_revision_map = _load_latest_genesis_revision_by_project(
        session, project_ids
    )
    planned_future_projects = (
        {
            str(project_id or "").strip()
            for project_id in session.execute(
                select(ArcPlanVersion.project_id)
                .where(
                    ArcPlanVersion.project_id.in_(project_ids),
                    ArcPlanVersion.status == "planned",
                )
                .distinct()
            )
            .scalars()
            .all()
            if str(project_id or "").strip()
        }
        if project_ids
        else set()
    )
    latest_checkpoint_map = _latest_band_checkpoint_by_project(session, project_ids)
    decision_timeline_map = _decision_timeline_by_project(
        session, project_ids, limit=20
    )
    chapters_by_project: dict[str, list[dict[str, object]]] = {}
    plans_by_project: dict[str, list[ChapterPlan]] = defaultdict(list)
    chapter_stats_by_project: dict[str, dict[str, int]] = {
        project_id: {
            "chapter_count": 0,
            "generated_chapter_count": 0,
            "accepted_chapter_count": 0,
            "needs_review_chapter_count": 0,
        }
        for project_id in project_ids
    }
    for plan in plans:
        plans_by_project[plan.project_id].append(plan)
        draft = draft_map.get(plan.id)
        stats = chapter_stats_by_project.setdefault(
            plan.project_id,
            {
                "chapter_count": 0,
                "generated_chapter_count": 0,
                "accepted_chapter_count": 0,
                "needs_review_chapter_count": 0,
            },
        )
        stats["chapter_count"] += 1
        if draft is not None:
            stats["generated_chapter_count"] += 1
        if plan.status == "accepted":
            stats["accepted_chapter_count"] += 1
        if plan.status == "needs_review":
            stats["needs_review_chapter_count"] += 1
        chapters_by_project.setdefault(plan.project_id, []).append(
            {
                "chapter_number": plan.chapter_number,
                "title": plan.title,
                "status": plan.status,
                "char_count": draft.char_count if draft else 0,
                "summary": draft.summary if draft else "",
                "has_draft": draft is not None,
                "has_review": bool(draft and draft.id in review_draft_ids),
            }
        )

    payload: list[ProjectSummary] = []
    for project in projects:
        latest_stage = runtime_maps["latest_stage_map"].get(project.id)
        last_replan = runtime_maps["last_replan_map"].get(project.id)
        latest_world = runtime_maps["latest_world_map"].get(project.id)
        latest_arc_envelope = runtime_maps["latest_arc_envelope_map"].get(project.id)
        latest_arc_analysis = runtime_maps["latest_arc_analysis_map"].get(project.id)
        latest_arc_structure = runtime_maps["latest_arc_structure_map"].get(project.id)
        latest_band_experience = runtime_maps["latest_band_experience_map"].get(
            project.id
        )
        latest_checkpoint = latest_checkpoint_map.get(project.id)
        chapter_stats = chapter_stats_by_project.get(project.id, {})
        project_upload_stats = upload_stats.get(project.id, {})
        policy_record = ProjectPolicyStore(session).load(project)
        genesis_revision = genesis_revision_map.get(project.id)
        generation_control = build_generation_control(
            plans=plans_by_project.get(project.id, []),
            latest_replan=last_replan,
            review_interval_chapters=policy_record.policy.pause.review_interval_chapters,
            latest_band_checkpoint=latest_checkpoint,
            decision_events=decision_timeline_map.get(project.id, []),
            future_constraints_enabled=policy_record.policy.planning.future_constraints,
        )
        if (
            str(getattr(project, "creation_status", "") or "").strip() == "writing"
            and project.id in planned_future_projects
            and not generation_control.pending_review_chapters
            and not generation_control.drafted_chapters
        ):
            generation_control.can_resume = True
            if generation_control.plan_state == "completed":
                generation_control.plan_state = "in_progress"
            if not generation_control.next_gate:
                generation_control.next_gate = "next_arc_ready"
        payload.append(
            ProjectSummary(
                id=project.id,
                title=project.title,
                genre=project.genre,
                premise=project.premise[:100] + "..."
                if len(project.premise) > 100
                else project.premise,
                created_at=display_datetime(project.created_at),
                target_total_chapters=effective_target_total_chapters(
                    project,
                    int(chapter_stats.get("chapter_count", 0) or 0),
                ),
                creation_status=str(
                    getattr(project, "creation_status", "") or "creating"
                ),
                active_genesis_revision_id=str(
                    getattr(project, "active_genesis_revision_id", "") or ""
                ),
                genesis_stage_overview=_stage_overview_from_revision(genesis_revision),
                can_start_writing=_can_start_writing(project, genesis_revision),
                chapter_count=int(chapter_stats.get("chapter_count", 0) or 0),
                generated_chapter_count=int(
                    chapter_stats.get("generated_chapter_count", 0) or 0
                ),
                accepted_chapter_count=int(
                    chapter_stats.get("accepted_chapter_count", 0) or 0
                ),
                needs_review_chapter_count=int(
                    chapter_stats.get("needs_review_chapter_count", 0) or 0
                ),
                upload_task_count=int(
                    project_upload_stats.get("upload_task_count", 0) or 0
                ),
                uploaded_chapter_count=int(
                    project_upload_stats.get("uploaded_chapter_count", 0) or 0
                ),
                automation=normalize_project_automation(project.automation_json),
                runtime_policy=policy_record.policy,
                runtime_policy_version=policy_record.version,
                latest_stage=latest_stage.stage_label if latest_stage else "",
                pacing_verdict=latest_stage.pacing_verdict if latest_stage else "",
                pacing_summary=latest_stage.pacing_summary if latest_stage else "",
                last_replan_status=last_replan.status if last_replan else "",
                last_replan_strategy=last_replan.strategy if last_replan else "",
                last_replan_reason=last_replan.reason if last_replan else "",
                current_time_label=latest_stage.timeline_label if latest_stage else "",
                world_pressure_level=latest_world.pressure_level
                if latest_world
                else "",
                world_pressure_summary=latest_world.pressure_summary
                if latest_world
                else "",
                generation_control=generation_control,
                chapters=chapters_by_project.get(project.id, [])[
                    -_PROJECT_SUMMARY_CHAPTER_PREVIEW_LIMIT:
                ],
                latest_band_checkpoint=generation_control.latest_band_checkpoint,
                blocking_reason=generation_control.blocking_reason,
                next_gate=generation_control.next_gate,
                **project_arc_snapshot_payload(
                    latest_arc_envelope,
                    latest_arc_analysis,
                    latest_arc_structure,
                    latest_band_experience,
                ),
            )
        )
    return payload


__all__ = [
    "build_project_summaries",
]
