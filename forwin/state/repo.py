from __future__ import annotations

import json
import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.models import (
    ArcEnvelope,
    ArcPlanVersion,
    ChapterDraft,
    ChapterPlan,
    Entity,
    EntityAlias,
    FeedbackActionRecord,
    NPCIntentSnapshot,
    PlotThread,
    Project,
    ReaderScaleSnapshot,
    RelationEdge,
    StoryTimePoint,
    WorldSimulationTurn,
)
from forwin.protocol import (
    EntitySnapshot,
    NPCIntentView,
    PlotThreadSnapshot,
    ReaderCommentView,
    ReaderFeedbackView,
    RelationSnapshot,
    SignalSummaryView,
    TimelineSnapshot,
    WorldPressureView,
)
from forwin.state.query_helpers import (
    load_latest_entity_states,
    load_recent_thread_beats,
)

logger = logging.getLogger(__name__)


class _AudienceHintData:
    __slots__ = ("pacing_hints", "clarity_hints", "character_heat_changes", "risk_flags")

    def __init__(
        self,
        pacing_hints: list[str],
        clarity_hints: list[str],
        character_heat_changes: list[str],
        risk_flags: list[str],
    ) -> None:
        self.pacing_hints = pacing_hints
        self.clarity_hints = clarity_hints
        self.character_heat_changes = character_heat_changes
        self.risk_flags = risk_flags


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

    def get_active_arc_envelope(self, project_id: str) -> ArcEnvelope | None:
        active_arc = self.get_active_arc_plan(project_id)
        if active_arc is None:
            return None
        stmt = (
            select(ArcEnvelope)
            .where(
                ArcEnvelope.project_id == project_id,
                ArcEnvelope.arc_id == active_arc.id,
            )
            .order_by(ArcEnvelope.updated_at.desc(), ArcEnvelope.created_at.desc())
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
        entity_ids = [entity.id for entity in entities]
        state_map = load_latest_entity_states(self.session, entity_ids)
        alias_rows = (
            self.session.execute(
                select(EntityAlias.entity_id, EntityAlias.alias)
                .where(EntityAlias.entity_id.in_(entity_ids))
                .order_by(EntityAlias.alias.asc())
            ).all()
            if entity_ids
            else []
        )
        alias_map: dict[str, list[str]] = {}
        for entity_id, alias in alias_rows:
            alias_map.setdefault(entity_id, []).append(alias)

        snapshots: list[EntitySnapshot] = []
        for entity in entities:
            entity_state = state_map.get(entity.id)
            current_state: dict = {}
            if entity_state is not None:
                try:
                    current_state = json.loads(entity_state.state_json) or {}
                except (json.JSONDecodeError, TypeError):
                    logger.warning(
                        "Failed to parse state_json for entity %s", entity.id
                    )

            aliases = alias_map.get(entity.id, [])
            if not aliases and entity.aliases_json:
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
        recent_beats_map = load_recent_thread_beats(
            self.session,
            [thread.id for thread in threads],
            limit_per_thread=3,
        )

        snapshots: list[PlotThreadSnapshot] = []
        for thread in threads:
            beats = recent_beats_map.get(thread.id, [])
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

    def get_recent_npc_intents(
        self,
        project_id: str,
        before_chapter: int,
        limit: int = 5,
    ) -> list[NPCIntentView]:
        rows = self.session.execute(
            select(NPCIntentSnapshot)
            .where(
                NPCIntentSnapshot.project_id == project_id,
                NPCIntentSnapshot.chapter_number < before_chapter,
            )
            .order_by(
                NPCIntentSnapshot.chapter_number.desc(),
                NPCIntentSnapshot.urgency.desc(),
                NPCIntentSnapshot.created_at.desc(),
            )
            .limit(limit)
        ).scalars().all()
        return [
            NPCIntentView(
                entity_name=row.entity_name,
                intent_kind=row.intent_kind,
                objective=row.objective,
                tactic=row.tactic,
                urgency=row.urgency,
                notes=row.notes,
            )
            for row in rows
        ]

    def get_latest_world_pressure(
        self,
        project_id: str,
        before_chapter: int,
    ) -> Optional[WorldPressureView]:
        row = self.session.execute(
            select(WorldSimulationTurn)
            .where(
                WorldSimulationTurn.project_id == project_id,
                WorldSimulationTurn.chapter_number < before_chapter,
            )
            .order_by(
                WorldSimulationTurn.chapter_number.desc(),
                WorldSimulationTurn.created_at.desc(),
            )
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            return None
        try:
            shifts = json.loads(row.notable_shifts_json or "[]") or []
        except (json.JSONDecodeError, TypeError):
            shifts = []
        return WorldPressureView(
            pressure_level=row.pressure_level,
            pressure_summary=row.pressure_summary,
            notable_shifts=[str(item) for item in shifts],
        )

    def get_recent_reader_feedback(
        self,
        project_id: str,
        before_chapter: int,
        *,
        limit: int = 6,
    ) -> Optional[ReaderFeedbackView]:
        project = self.get_project(project_id)
        if project is None:
            return None
        work_name = str(project.title or "").strip()
        if not work_name:
            return None
        from forwin.orchestrator.comment_analysis import build_reader_feedback_snapshot

        snapshot = build_reader_feedback_snapshot(
            self.session,
            work_name,
            project_id=project_id,
            before_chapter=before_chapter,
            limit=limit,
            analyze_missing=False,
        )
        if not snapshot["recent_comments"] and snapshot["comment_count"] <= 0:
            return None
        highlights = [
            ReaderCommentView(
                platform_id=row.platform_id,
                author_name=row.author_name,
                body_text=str(row.body_text or "")[:180],
                chapter_title=row.chapter_title,
                remote_created_at=row.remote_created_at,
            )
            for row in snapshot["recent_comments"]
        ]
        confirmed_signals = [
            SignalSummaryView(
                signal_key=str(item.get("signal_key") or ""),
                signal_type=str(item.get("signal_type") or ""),
                target_name=str(item.get("target_name") or ""),
                level=str(item.get("level") or "noise"),
                hit_count=int(item.get("hit_count") or 0),
                max_severity=int(item.get("max_severity") or 0),
            )
            for item in snapshot["confirmed_signals"]
        ]

        # ── Load reader tier from latest snapshot ──
        reader_tier = 0
        scale_row = self.session.execute(
            select(ReaderScaleSnapshot)
            .where(ReaderScaleSnapshot.project_id == project_id)
            .order_by(ReaderScaleSnapshot.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if scale_row is not None:
            reader_tier = scale_row.tier

        return ReaderFeedbackView(
            comment_count=int(snapshot["comment_count"]),
            dominant_sentiment=str(snapshot["dominant_sentiment"] or "neutral"),
            feedback_summary=str(snapshot["feedback_summary"] or ""),
            recent_highlights=highlights[:4],
            highlighted_topics=list(snapshot["highlighted_topics"]),
            confirmed_signals=confirmed_signals,
            reader_tier=reader_tier,
        )

    # ------------------------------------------------------------------
    # Audience hints (Phase C)
    # ------------------------------------------------------------------

    def get_audience_hints(
        self,
        project_id: str,
        before_chapter: int,
    ) -> Optional["_AudienceHintData"]:
        """Build audience hints from recent FeedbackActionRecords.

        Returns a lightweight data object with hint lists, or None if no actions exist.
        """
        records = self.session.execute(
            select(FeedbackActionRecord)
            .where(
                FeedbackActionRecord.project_id == project_id,
                FeedbackActionRecord.triggered_at_chapter < before_chapter,
                FeedbackActionRecord.cooldown_until_chapter >= before_chapter,
            )
            .order_by(FeedbackActionRecord.created_at.desc())
            .limit(12)
        ).scalars().all()
        if not records:
            return None

        pacing: list[str] = []
        clarity: list[str] = []
        heat: list[str] = []
        risk: list[str] = []
        for rec in records:
            note = rec.notes or rec.action_type
            if rec.signal_type == "pacing":
                pacing.append(note)
            elif rec.signal_type == "confusion":
                clarity.append(note)
            elif rec.signal_type == "character_heat":
                heat.append(note)
            elif rec.signal_type == "risk":
                risk.append(note)

        if not any((pacing, clarity, heat, risk)):
            return None

        return _AudienceHintData(
            pacing_hints=pacing[:3],
            clarity_hints=clarity[:3],
            character_heat_changes=heat[:3],
            risk_flags=risk[:3],
        )

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

    def get_entities_by_names(
        self,
        project_id: str,
        names: list[str],
    ) -> dict[str, Entity]:
        normalized = [name.strip() for name in names if str(name).strip()]
        if not normalized:
            return {}

        mapping: dict[str, Entity] = {}
        exact_rows = self.session.execute(
            select(Entity).where(
                Entity.project_id == project_id,
                Entity.name.in_(normalized),
            )
        ).scalars().all()
        for entity in exact_rows:
            mapping[entity.name] = entity

        unresolved = [name for name in normalized if name not in mapping]
        if not unresolved:
            return mapping

        alias_rows = self.session.execute(
            select(EntityAlias.alias, Entity)
            .join(Entity, EntityAlias.entity_id == Entity.id)
            .where(
                Entity.project_id == project_id,
                EntityAlias.project_id == project_id,
                EntityAlias.alias.in_(unresolved),
            )
        ).all()
        for alias, entity in alias_rows:
            mapping[str(alias)] = entity

        return mapping

    def get_thread_by_name(
        self, project_id: str, name: str
    ) -> Optional[PlotThread]:
        """Find a plot thread by exact name."""
        stmt = select(PlotThread).where(
            PlotThread.project_id == project_id,
            PlotThread.name == name,
        )
        return self.session.execute(stmt).scalar_one_or_none()
