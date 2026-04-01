from __future__ import annotations

import json
import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.models import (
    ArcPlanVersion,
    ChapterDraft,
    ChapterPlan,
    Entity,
    EntityAlias,
    EntityState,
    PlotThread,
    PlotThreadBeat,
    Project,
    RelationEdge,
    StoryTimePoint,
)
from forwin.protocol import (
    EntitySnapshot,
    PlotThreadSnapshot,
    RelationSnapshot,
    TimelineSnapshot,
)

logger = logging.getLogger(__name__)


class StateRepository:
    """Read-only queries against the state database."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # Project / Arc
    # ------------------------------------------------------------------

    def get_project(self, project_id: str) -> Optional[Project]:
        """Return the Project row, or None if not found."""
        stmt = select(Project).where(Project.id == project_id)
        return self.session.execute(stmt).scalar_one_or_none()

    def get_active_arc_plan(self, project_id: str) -> Optional[ArcPlanVersion]:
        """Get the currently active arc plan version."""
        stmt = (
            select(ArcPlanVersion)
            .where(
                ArcPlanVersion.project_id == project_id,
                ArcPlanVersion.status == "active",
            )
            .order_by(ArcPlanVersion.version.desc())
            .limit(1)
        )
        return self.session.execute(stmt).scalar_one_or_none()

    # ------------------------------------------------------------------
    # Chapter Plan
    # ------------------------------------------------------------------

    def get_chapter_plan(
        self, project_id: str, chapter_number: int
    ) -> Optional[ChapterPlan]:
        """Return the ChapterPlan for the given project and chapter number."""
        stmt = select(ChapterPlan).where(
            ChapterPlan.project_id == project_id,
            ChapterPlan.chapter_number == chapter_number,
        )
        return self.session.execute(stmt).scalar_one_or_none()

    # ------------------------------------------------------------------
    # Entities
    # ------------------------------------------------------------------

    def get_active_entities(self, project_id: str) -> list[EntitySnapshot]:
        """Get all active entities with their latest state."""
        stmt = select(Entity).where(
            Entity.project_id == project_id,
            Entity.is_active == True,  # noqa: E712
        )
        entities = self.session.execute(stmt).scalars().all()

        snapshots: list[EntitySnapshot] = []
        for entity in entities:
            # Find the EntityState with the highest as_of_chapter for this entity.
            state_stmt = (
                select(EntityState)
                .where(EntityState.entity_id == entity.id)
                .order_by(EntityState.as_of_chapter.desc())
                .limit(1)
            )
            entity_state = self.session.execute(state_stmt).scalar_one_or_none()

            current_state: dict = {}
            if entity_state is not None:
                try:
                    current_state = json.loads(entity_state.state_json) or {}
                except (json.JSONDecodeError, TypeError):
                    logger.warning(
                        "Failed to parse state_json for entity %s", entity.id
                    )

            aliases: list[str] = []
            try:
                aliases = json.loads(entity.aliases_json) or []
            except (json.JSONDecodeError, TypeError):
                logger.warning(
                    "Failed to parse aliases_json for entity %s", entity.id
                )

            snapshots.append(
                EntitySnapshot(
                    entity_id=entity.id,
                    kind=entity.kind,
                    name=entity.name,
                    importance=entity.importance,
                    aliases=aliases,
                    description=entity.description,
                    current_state=current_state,
                )
            )

        return snapshots

    # ------------------------------------------------------------------
    # Relations
    # ------------------------------------------------------------------

    def get_active_relations(self, project_id: str) -> list[RelationSnapshot]:
        """Get all active relation edges, resolved to entity names."""
        stmt = select(RelationEdge).where(
            RelationEdge.project_id == project_id,
            RelationEdge.is_active == True,  # noqa: E712
        )
        edges = self.session.execute(stmt).scalars().all()

        # Build a name cache to avoid N+1 queries.
        entity_ids = set()
        for edge in edges:
            entity_ids.add(edge.source_entity_id)
            entity_ids.add(edge.target_entity_id)

        name_map: dict[str, str] = {}
        if entity_ids:
            name_stmt = select(Entity).where(Entity.id.in_(entity_ids))
            for ent in self.session.execute(name_stmt).scalars().all():
                name_map[ent.id] = ent.name

        snapshots: list[RelationSnapshot] = []
        for edge in edges:
            source_name = name_map.get(edge.source_entity_id, edge.source_entity_id)
            target_name = name_map.get(edge.target_entity_id, edge.target_entity_id)
            snapshots.append(
                RelationSnapshot(
                    source_name=source_name,
                    target_name=target_name,
                    relation_type=edge.relation_type,
                    description=edge.description,
                )
            )

        return snapshots

    # ------------------------------------------------------------------
    # Plot Threads
    # ------------------------------------------------------------------

    def get_active_threads(self, project_id: str) -> list[PlotThreadSnapshot]:
        """Get active plot threads with their last 3 beats."""
        stmt = select(PlotThread).where(
            PlotThread.project_id == project_id,
            PlotThread.status == "active",
        )
        threads = self.session.execute(stmt).scalars().all()

        snapshots: list[PlotThreadSnapshot] = []
        for thread in threads:
            beats_stmt = (
                select(PlotThreadBeat)
                .where(PlotThreadBeat.thread_id == thread.id)
                .order_by(PlotThreadBeat.chapter_number.desc())
                .limit(3)
            )
            beats = self.session.execute(beats_stmt).scalars().all()
            # Return beats in chronological order.
            recent_beats = [b.description for b in reversed(beats)]

            snapshots.append(
                PlotThreadSnapshot(
                    thread_id=thread.id,
                    name=thread.name,
                    description=thread.description,
                    status=thread.status,
                    priority=thread.priority,
                    recent_beats=recent_beats,
                )
            )

        return snapshots

    # ------------------------------------------------------------------
    # Chapter summaries
    # ------------------------------------------------------------------

    def get_chapter_summaries(
        self,
        project_id: str,
        up_to_chapter: int,
        limit: int = 3,
    ) -> list[str]:
        """Return recent chapter summaries from drafts, up to *limit* entries.

        Ordered newest-first (highest chapter_number first), then we reverse
        so the caller gets them in chronological order.
        """
        stmt = (
            select(ChapterDraft.summary, ChapterPlan.chapter_number)
            .join(ChapterPlan, ChapterDraft.chapter_plan_id == ChapterPlan.id)
            .where(
                ChapterPlan.project_id == project_id,
                ChapterPlan.chapter_number < up_to_chapter,
            )
            .order_by(ChapterPlan.chapter_number.desc())
            .limit(limit)
        )
        rows = self.session.execute(stmt).all()
        # rows are (summary, chapter_number) newest-first; reverse for chron order.
        summaries = [row[0] for row in reversed(rows) if row[0]]
        return summaries

    # ------------------------------------------------------------------
    # Timeline
    # ------------------------------------------------------------------

    def get_current_timeline(self, project_id: str) -> Optional[TimelineSnapshot]:
        """Get the latest story time point (highest ordinal)."""
        stmt = (
            select(StoryTimePoint)
            .where(StoryTimePoint.project_id == project_id)
            .order_by(StoryTimePoint.ordinal.desc())
            .limit(1)
        )
        stp = self.session.execute(stmt).scalar_one_or_none()
        if stp is None:
            return None
        return TimelineSnapshot(current_time_label=stp.label, ordinal=stp.ordinal)

    # ------------------------------------------------------------------
    # Name-based lookups
    # ------------------------------------------------------------------

    def get_entity_by_name(
        self, project_id: str, name: str
    ) -> Optional[Entity]:
        """Find entity by exact name match, then by alias match."""
        # 1. Exact name match.
        stmt = select(Entity).where(
            Entity.project_id == project_id,
            Entity.name == name,
        )
        entity = self.session.execute(stmt).scalar_one_or_none()
        if entity is not None:
            return entity

        alias_stmt = (
            select(Entity)
            .join(EntityAlias, EntityAlias.entity_id == Entity.id)
            .where(
                Entity.project_id == project_id,
                EntityAlias.project_id == project_id,
                EntityAlias.alias == name,
            )
        )
        entity = self.session.execute(alias_stmt).scalar_one_or_none()
        if entity is not None:
            return entity

        return None

    def get_thread_by_name(
        self, project_id: str, name: str
    ) -> Optional[PlotThread]:
        """Find a plot thread by exact name."""
        stmt = select(PlotThread).where(
            PlotThread.project_id == project_id,
            PlotThread.name == name,
        )
        return self.session.execute(stmt).scalar_one_or_none()
