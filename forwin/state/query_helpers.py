from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from forwin.models.canon import CanonCommitRecord
from forwin.models.draft import CandidateDraftRecord, ChapterDraft
from forwin.models.phase import (
    ArcEnvelope,
    ArcEnvelopeAnalysis,
    ChapterRewriteAttempt,
    ProjectReplanEvent,
    ProjectStageAnalysis,
)
from forwin.models.phase4 import WorldSimulationTurn
from forwin.models.project import ArcPlanVersion, ChapterPlan


def _as_list(values: Iterable[str]) -> list[str]:
    return [value for value in values if value]


def _latest_ranked_rows(
    model,
    partition_column,
    order_by: tuple[Any, ...],
    id_filter_values: list[str],
    *,
    filter_column=None,
):
    column = filter_column or partition_column
    return (
        select(
            model.id.label("row_id"),
            partition_column.label("partition_key"),
            func.row_number()
            .over(partition_by=partition_column, order_by=order_by)
            .label("rn"),
        )
        .where(column.in_(id_filter_values))
        .subquery()
    )


def _load_latest_partitioned_rows(
    session: Session,
    model,
    partition_column,
    id_filter_values: Iterable[str],
    *,
    order_by: tuple[Any, ...],
    filter_column=None,
) -> dict[str, Any]:
    ids = _as_list(id_filter_values)
    if not ids:
        return {}

    ranked = _latest_ranked_rows(
        model,
        partition_column,
        order_by,
        ids,
        filter_column=filter_column,
    )
    rows = (
        session.execute(
            select(model)
            .join(ranked, model.id == ranked.c.row_id)
            .where(ranked.c.rn == 1)
        )
        .scalars()
        .all()
    )
    return {getattr(row, partition_column.key): row for row in rows}


def _active_arc_rows_for_projects(project_ids: list[str]):
    return (
        select(
            ArcPlanVersion.id.label("arc_id"),
            ArcPlanVersion.project_id.label("project_id"),
            func.row_number()
            .over(
                partition_by=ArcPlanVersion.project_id,
                order_by=(ArcPlanVersion.version.desc(), ArcPlanVersion.id.desc()),
            )
            .label("arc_rn"),
        )
        .where(
            ArcPlanVersion.project_id.in_(project_ids),
            ArcPlanVersion.status == "active",
        )
        .subquery()
    )


def load_latest_drafts_by_plan_id(
    session: Session,
    chapter_plan_ids: Iterable[str],
) -> dict[str, ChapterDraft]:
    """Return active accepted text, or the newest draft for an unaccepted plan."""
    ids = _as_list(chapter_plan_ids)
    active_ids = set(
        session.scalars(
            select(ChapterPlan.id).where(
                ChapterPlan.id.in_(ids), ChapterPlan.active_commit_id.is_not(None)
            )
        )
    )
    drafts = _load_latest_partitioned_rows(
        session,
        ChapterDraft,
        ChapterDraft.chapter_plan_id,
        [plan_id for plan_id in ids if plan_id not in active_ids],
        order_by=(
            ChapterDraft.version.desc(),
            ChapterDraft.created_at.desc(),
            ChapterDraft.id.desc(),
        ),
    )
    active_drafts = session.scalars(
        select(ChapterDraft)
        .join(
            CandidateDraftRecord,
            CandidateDraftRecord.candidate_draft_id == ChapterDraft.id,
        )
        .join(
            CanonCommitRecord, CanonCommitRecord.candidate_id == CandidateDraftRecord.id
        )
        .join(ChapterPlan, ChapterPlan.active_commit_id == CanonCommitRecord.id)
        .where(
            ChapterPlan.id.in_(active_ids),
            ChapterDraft.chapter_plan_id == ChapterPlan.id,
        )
    )
    drafts.update({draft.chapter_plan_id: draft for draft in active_drafts})
    return drafts


def load_latest_rewrite_attempts_by_chapter(
    session: Session,
    project_id: str,
    chapter_numbers: Iterable[int] | None = None,
) -> dict[int, ChapterRewriteAttempt]:
    normalized_project_id = str(project_id or "").strip()
    if not normalized_project_id:
        return {}
    normalized_chapters = sorted(
        {int(value or 0) for value in chapter_numbers or [] if int(value or 0) > 0}
    )
    if chapter_numbers is not None and not normalized_chapters:
        return {}

    stmt = select(
        ChapterRewriteAttempt.id.label("row_id"),
        ChapterRewriteAttempt.chapter_number.label("chapter_number"),
        func.row_number()
        .over(
            partition_by=ChapterRewriteAttempt.chapter_number,
            order_by=(
                ChapterRewriteAttempt.attempt_no.desc(),
                ChapterRewriteAttempt.created_at.desc(),
                ChapterRewriteAttempt.id.desc(),
            ),
        )
        .label("rn"),
    ).where(ChapterRewriteAttempt.project_id == normalized_project_id)
    if chapter_numbers is not None:
        stmt = stmt.where(ChapterRewriteAttempt.chapter_number.in_(normalized_chapters))
    ranked = stmt.subquery()
    rows = (
        session.execute(
            select(ChapterRewriteAttempt)
            .join(ranked, ChapterRewriteAttempt.id == ranked.c.row_id)
            .where(ranked.c.rn == 1)
        )
        .scalars()
        .all()
    )
    return {
        chapter_number: row
        for row in rows
        if (chapter_number := int(row.chapter_number or 0)) > 0
    }


def load_latest_stage_analysis_by_project(
    session: Session,
    project_ids: Iterable[str],
) -> dict[str, ProjectStageAnalysis]:
    return _load_latest_partitioned_rows(
        session,
        ProjectStageAnalysis,
        ProjectStageAnalysis.project_id,
        project_ids,
        order_by=(
            ProjectStageAnalysis.chapter_number.desc(),
            ProjectStageAnalysis.created_at.desc(),
            ProjectStageAnalysis.id.desc(),
        ),
    )


def load_latest_world_turn_by_project(
    session: Session,
    project_ids: Iterable[str],
) -> dict[str, WorldSimulationTurn]:
    return _load_latest_partitioned_rows(
        session,
        WorldSimulationTurn,
        WorldSimulationTurn.project_id,
        project_ids,
        order_by=(
            WorldSimulationTurn.chapter_number.desc(),
            WorldSimulationTurn.created_at.desc(),
            WorldSimulationTurn.id.desc(),
        ),
    )


def load_latest_replan_event_by_project(
    session: Session,
    project_ids: Iterable[str],
) -> dict[str, ProjectReplanEvent]:
    return _load_latest_partitioned_rows(
        session,
        ProjectReplanEvent,
        ProjectReplanEvent.project_id,
        project_ids,
        order_by=(
            ProjectReplanEvent.trigger_chapter.desc(),
            ProjectReplanEvent.created_at.desc(),
            ProjectReplanEvent.id.desc(),
        ),
    )


def load_latest_active_arc_envelope_by_project(
    session: Session,
    project_ids: Iterable[str],
) -> dict[str, ArcEnvelope]:
    ids = _as_list(project_ids)
    if not ids:
        return {}

    active_arcs = _active_arc_rows_for_projects(ids)
    ranked = (
        select(
            ArcEnvelope.id.label("row_id"),
            ArcEnvelope.project_id.label("project_id"),
            func.row_number()
            .over(
                partition_by=ArcEnvelope.project_id,
                order_by=(
                    ArcEnvelope.updated_at.desc(),
                    ArcEnvelope.created_at.desc(),
                    ArcEnvelope.id.desc(),
                ),
            )
            .label("rn"),
        )
        .join(active_arcs, ArcEnvelope.arc_id == active_arcs.c.arc_id)
        .where(active_arcs.c.arc_rn == 1)
        .subquery()
    )
    rows = (
        session.execute(
            select(ArcEnvelope)
            .join(ranked, ArcEnvelope.id == ranked.c.row_id)
            .where(ranked.c.rn == 1)
        )
        .scalars()
        .all()
    )
    return {row.project_id: row for row in rows}


def load_latest_arc_envelope_analysis_by_project(
    session: Session,
    project_ids: Iterable[str],
) -> dict[str, ArcEnvelopeAnalysis]:
    ids = _as_list(project_ids)
    if not ids:
        return {}

    active_arcs = _active_arc_rows_for_projects(ids)
    ranked = (
        select(
            ArcEnvelopeAnalysis.id.label("row_id"),
            ArcEnvelopeAnalysis.project_id.label("project_id"),
            func.row_number()
            .over(
                partition_by=ArcEnvelopeAnalysis.project_id,
                order_by=(
                    ArcEnvelopeAnalysis.created_at.desc(),
                    ArcEnvelopeAnalysis.id.desc(),
                ),
            )
            .label("rn"),
        )
        .join(active_arcs, ArcEnvelopeAnalysis.arc_id == active_arcs.c.arc_id)
        .where(active_arcs.c.arc_rn == 1)
        .subquery()
    )
    rows = (
        session.execute(
            select(ArcEnvelopeAnalysis)
            .join(ranked, ArcEnvelopeAnalysis.id == ranked.c.row_id)
            .where(ranked.c.rn == 1)
        )
        .scalars()
        .all()
    )
    return {row.project_id: row for row in rows}
