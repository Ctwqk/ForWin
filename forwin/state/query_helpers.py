from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from forwin.models.draft import ChapterDraft
from forwin.models.entity import EntityState
from forwin.models.phase import (
    ArcEnvelope,
    ArcEnvelopeAnalysis,
    ProjectReplanEvent,
    ProjectStageAnalysis,
    ProvisionalBandExecution,
)
from forwin.models.phase4 import WorldSimulationTurn
from forwin.models.project import ArcPlanVersion
from forwin.models.thread import PlotThreadBeat


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
    rows = session.execute(
        select(model)
        .join(ranked, model.id == ranked.c.row_id)
        .where(ranked.c.rn == 1)
    ).scalars().all()
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


def load_latest_entity_states(
    session: Session,
    entity_ids: Iterable[str],
) -> dict[str, EntityState]:
    ids = _as_list(entity_ids)
    if not ids:
        return {}

    ranked = (
        select(
            EntityState.id.label("row_id"),
            EntityState.entity_id.label("entity_id"),
            func.row_number()
            .over(
                partition_by=EntityState.entity_id,
                order_by=(
                    EntityState.as_of_chapter.desc(),
                    EntityState.updated_at.desc(),
                    EntityState.id.desc(),
                ),
            )
            .label("rn"),
        )
        .where(EntityState.entity_id.in_(ids))
        .subquery()
    )

    rows = session.execute(
        select(EntityState)
        .join(ranked, EntityState.id == ranked.c.row_id)
        .where(ranked.c.rn == 1)
    ).scalars().all()
    return {row.entity_id: row for row in rows}


def load_recent_thread_beats(
    session: Session,
    thread_ids: Iterable[str],
    *,
    limit_per_thread: int,
) -> dict[str, list[PlotThreadBeat]]:
    ids = _as_list(thread_ids)
    if not ids:
        return {}

    ranked = (
        select(
            PlotThreadBeat.id.label("row_id"),
            PlotThreadBeat.thread_id.label("thread_id"),
            func.row_number()
            .over(
                partition_by=PlotThreadBeat.thread_id,
                order_by=(
                    PlotThreadBeat.chapter_number.desc(),
                    PlotThreadBeat.id.desc(),
                ),
            )
            .label("rn"),
        )
        .where(PlotThreadBeat.thread_id.in_(ids))
        .subquery()
    )

    rows = session.execute(
        select(PlotThreadBeat, ranked.c.rn)
        .join(ranked, PlotThreadBeat.id == ranked.c.row_id)
        .where(ranked.c.rn <= limit_per_thread)
        .order_by(ranked.c.thread_id.asc(), ranked.c.rn.asc())
    ).all()

    grouped: dict[str, list[PlotThreadBeat]] = defaultdict(list)
    for beat, _rn in rows:
        grouped[beat.thread_id].append(beat)
    return dict(grouped)


def load_latest_thread_beats(
    session: Session,
    thread_ids: Iterable[str],
) -> dict[str, PlotThreadBeat]:
    grouped = load_recent_thread_beats(session, thread_ids, limit_per_thread=1)
    return {thread_id: beats[0] for thread_id, beats in grouped.items() if beats}


def load_latest_drafts_by_plan_id(
    session: Session,
    chapter_plan_ids: Iterable[str],
) -> dict[str, ChapterDraft]:
    return _load_latest_partitioned_rows(
        session,
        ChapterDraft,
        ChapterDraft.chapter_plan_id,
        chapter_plan_ids,
        order_by=(
            ChapterDraft.version.desc(),
            ChapterDraft.created_at.desc(),
            ChapterDraft.id.desc(),
        ),
    )


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
                order_by=(ArcEnvelope.updated_at.desc(), ArcEnvelope.created_at.desc(), ArcEnvelope.id.desc()),
            )
            .label("rn"),
        )
        .join(active_arcs, ArcEnvelope.arc_id == active_arcs.c.arc_id)
        .where(active_arcs.c.arc_rn == 1)
        .subquery()
    )
    rows = session.execute(
        select(ArcEnvelope)
        .join(ranked, ArcEnvelope.id == ranked.c.row_id)
        .where(ranked.c.rn == 1)
    ).scalars().all()
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
    rows = session.execute(
        select(ArcEnvelopeAnalysis)
        .join(ranked, ArcEnvelopeAnalysis.id == ranked.c.row_id)
        .where(ranked.c.rn == 1)
    ).scalars().all()
    return {row.project_id: row for row in rows}


def load_latest_provisional_band_execution_by_project(
    session: Session,
    project_ids: Iterable[str],
) -> dict[str, ProvisionalBandExecution]:
    return _load_latest_partitioned_rows(
        session,
        ProvisionalBandExecution,
        ProvisionalBandExecution.project_id,
        project_ids,
        order_by=(
            ProvisionalBandExecution.created_at.desc(),
            ProvisionalBandExecution.id.desc(),
        ),
    )


def chunked(values: Sequence[str], size: int = 200) -> list[list[str]]:
    if size <= 0:
        return [list(values)]
    return [list(values[index:index + size]) for index in range(0, len(values), size)]
