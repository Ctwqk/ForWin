from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.models.project import ArcPlanVersion, ChapterPlan, Project


@dataclass(frozen=True)
class ContinueGenerationWorkset:
    project_id: str
    chapter_numbers: tuple[int, ...]
    requested_chapters: int
    materialized_plan_count: int
    active_arc_id: str
    active_arc_number: int
    source: str
    reason: str


def build_continue_generation_workset(
    session: Session,
    project_id: str,
    *,
    max_chapters: int | None = None,
    include_failed: bool = True,
    source: str = "direct_continue",
    preloaded_plans: list[ChapterPlan] | None = None,
) -> ContinueGenerationWorkset:
    normalized_project_id = str(project_id or "").strip()
    normalized_source = str(source or "direct_continue").strip() or "direct_continue"
    if max_chapters is not None and int(max_chapters) < 1:
        raise ValueError("max_chapters must be positive when provided")

    project = session.get(Project, normalized_project_id)
    if project is None:
        return _empty_workset(
            normalized_project_id,
            source=normalized_source,
            reason="project_not_found",
        )
    if str(getattr(project, "creation_status", "") or "").strip() == "completed":
        return _empty_workset(
            normalized_project_id,
            source=normalized_source,
            reason="project_completed",
        )

    plans = list(preloaded_plans) if preloaded_plans is not None else list(
        session.execute(
            select(ChapterPlan)
            .where(ChapterPlan.project_id == normalized_project_id)
            .order_by(ChapterPlan.chapter_number.asc(), ChapterPlan.id.asc())
        ).scalars()
    )
    if any(str(plan.status or "") == "needs_review" for plan in plans):
        return _empty_workset(
            normalized_project_id,
            source=normalized_source,
            reason="pending_review_blocker",
        )
    if any(str(plan.status or "") == "drafted" for plan in plans):
        return _empty_workset(
            normalized_project_id,
            source=normalized_source,
            reason="pending_acceptance_blocker",
        )

    active_arc = _active_arc(session, normalized_project_id)
    pending_statuses = {"planned", "failed"} if include_failed else {"planned"}
    if active_arc is not None:
        active_candidates = [
            int(plan.chapter_number or 0)
            for plan in plans
            if str(plan.arc_plan_id or "") == str(active_arc.id)
            and str(plan.status or "") in pending_statuses
            and int(plan.chapter_number or 0) > 0
        ]
        if active_candidates:
            selected = _apply_max(active_candidates, max_chapters)
            return ContinueGenerationWorkset(
                project_id=normalized_project_id,
                chapter_numbers=tuple(selected),
                requested_chapters=len(selected),
                materialized_plan_count=len(active_candidates),
                active_arc_id=str(active_arc.id or ""),
                active_arc_number=int(active_arc.arc_number or 0),
                source=normalized_source,
                reason="active_arc_pending",
            )

    materialized_candidates = [
        plan
        for plan in plans
        if str(plan.status or "") in pending_statuses
        and int(plan.chapter_number or 0) > 0
    ]
    if materialized_candidates:
        candidate_numbers = [int(plan.chapter_number or 0) for plan in materialized_candidates]
        selected = _apply_max(candidate_numbers, max_chapters)
        selected_arc_id = str(getattr(materialized_candidates[0], "arc_plan_id", "") or "")
        selected_arc = session.get(ArcPlanVersion, selected_arc_id) if selected_arc_id else None
        return ContinueGenerationWorkset(
            project_id=normalized_project_id,
            chapter_numbers=tuple(selected),
            requested_chapters=len(selected),
            materialized_plan_count=len(candidate_numbers),
            active_arc_id=str(getattr(selected_arc, "id", "") or selected_arc_id),
            active_arc_number=int(getattr(selected_arc, "arc_number", 0) or 0),
            source=normalized_source,
            reason="materialized_pending",
        )

    future_arc = _next_planned_arc(session, normalized_project_id)
    if future_arc is not None:
        predicted = _future_arc_chapter_numbers(future_arc)
        selected = _apply_max(predicted, max_chapters)
        return ContinueGenerationWorkset(
            project_id=normalized_project_id,
            chapter_numbers=tuple(selected),
            requested_chapters=len(selected),
            materialized_plan_count=0,
            active_arc_id=str(future_arc.id or ""),
            active_arc_number=int(future_arc.arc_number or 0),
            source=normalized_source,
            reason="future_arc_materialization_required" if selected else "no_remaining_chapters",
        )

    return _empty_workset(
        normalized_project_id,
        source=normalized_source,
        reason="no_remaining_chapters",
    )


def _empty_workset(project_id: str, *, source: str, reason: str) -> ContinueGenerationWorkset:
    return ContinueGenerationWorkset(
        project_id=project_id,
        chapter_numbers=(),
        requested_chapters=0,
        materialized_plan_count=0,
        active_arc_id="",
        active_arc_number=0,
        source=source,
        reason=reason,
    )


def _active_arc(session: Session, project_id: str) -> ArcPlanVersion | None:
    return session.execute(
        select(ArcPlanVersion)
        .where(
            ArcPlanVersion.project_id == project_id,
            ArcPlanVersion.status == "active",
        )
        .order_by(ArcPlanVersion.created_at.desc(), ArcPlanVersion.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def _next_planned_arc(session: Session, project_id: str) -> ArcPlanVersion | None:
    return session.execute(
        select(ArcPlanVersion)
        .where(
            ArcPlanVersion.project_id == project_id,
            ArcPlanVersion.status == "planned",
        )
        .order_by(ArcPlanVersion.arc_number.asc(), ArcPlanVersion.created_at.asc(), ArcPlanVersion.id.asc())
        .limit(1)
    ).scalar_one_or_none()


def _apply_max(chapter_numbers: list[int], max_chapters: int | None) -> list[int]:
    if max_chapters is None:
        return list(chapter_numbers)
    return list(chapter_numbers)[: int(max_chapters)]


def _future_arc_chapter_numbers(arc: ArcPlanVersion) -> list[int]:
    start = int(getattr(arc, "chapter_start", 0) or 0)
    end = int(getattr(arc, "chapter_end", 0) or 0)
    target_size = int(getattr(arc, "planned_target_size", 0) or 0)
    if start <= 0:
        return []
    if end >= start:
        return list(range(start, end + 1))
    if target_size > 0:
        return list(range(start, start + target_size))
    return [start]
