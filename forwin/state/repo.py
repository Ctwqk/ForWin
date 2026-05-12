from __future__ import annotations

import json
import logging
from typing import Optional

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from forwin.audience_metrics import derive_audience_trends
from forwin.governance import (
    DecisionEventInfo,
    NarrativeConstraintInfo,
    NextBandSummary,
    PlanTaskItem,
    load_plan_task_contract,
    normalize_project_governance,
)
from forwin.models import (
    ArcEnvelope,
    ArcPlanVersion,
    ArcStructureDraft,
    BandCheckpoint,
    BandExperiencePlan,
    BookGenesisRevision,
    CanonEvent,
    ChapterDraft,
    ChapterPlan,
    ChapterReview,
    ChapterRewriteAttempt,
    DecisionEvent,
    Entity,
    EntityAlias,
    EventEntityLink,
    FeedbackActionRecord,
    MapRegionRow,
    NarrativeConstraint,
    NPCIntentSnapshot,
    PlotThread,
    Project,
    PromptTrace,
    PublisherRawComment,
    ReaderScaleSnapshot,
    RelationEdge,
    SignalWindowAggregate,
    StoryTimePoint,
    SubWorld,
    SubWorldRosterItem,
    WorldSimulationTurn,
)
from forwin.protocol import (
    ArcPayoffMap,
    AudienceTrendView,
    BandDelightSchedule,
    CanonEventEvidence,
    ChapterExperiencePlan,
    EntitySnapshot,
    NPCIntentView,
    PlotThreadSnapshot,
    ReaderPromise,
    ReaderCommentView,
    ReaderFeedbackView,
    RelationSnapshot,
    ReviewNote,
    SignalSummaryView,
    SubWorldSummary,
    TimelineSnapshot,
    WorldPressureView,
)
from forwin.state.query_helpers import (
    load_latest_entity_states,
    load_recent_thread_beats,
)

logger = logging.getLogger(__name__)

_READER_FEEDBACK_LEVEL_ORDER = {
    "noise": 0,
    "candidate": 1,
    "watchlist": 2,
    "confirmed": 3,
}
_READER_FEEDBACK_SIGNAL_PRIORITY = {
    "risk": 3,
    "confusion": 2,
    "prediction": 2,
    "pacing": 1,
    "relationship_interest": 1,
    "character_heat": 0,
}
_READER_FEEDBACK_WINDOW_PRIORITY = {
    "short": 0,
    "medium": 1,
    "long": 2,
}
_POSITIVE_COMMENT_KEYWORDS = ("喜欢", "精彩", "好看", "期待", "爽", "牛", "神")
_NEGATIVE_COMMENT_KEYWORDS = ("水", "拖", "崩", "失望", "弃", "烂", "短", "乱")
_QUESTION_COMMENT_KEYWORDS = ("为什么", "怎么", "是不是", "会不会", "求", "能不能")


def _load_json_object(raw: str, default):
    try:
        return json.loads(raw or "")
    except (json.JSONDecodeError, TypeError):
        return default


def _reader_feedback_target_label(target_name: str) -> str:
    return str(target_name or "").strip() or "整体"


def _reader_feedback_sort_key(row: SignalWindowAggregate) -> tuple[int, int, int, int, str]:
    level = str(row.signal_level or "noise")
    signal_type = str(row.signal_type or "")
    boost = 2 if signal_type == "risk" and level in {"watchlist", "confirmed"} else 0
    return (
        _READER_FEEDBACK_LEVEL_ORDER.get(level, 0) + boost,
        _READER_FEEDBACK_SIGNAL_PRIORITY.get(signal_type, 0),
        int(row.max_severity or 0),
        int(row.hit_comment_count or 0),
        _reader_feedback_target_label(str(row.target_name or "")),
    )


def _keyword_dominant_sentiment(comments: list[PublisherRawComment]) -> str:
    positive = 0
    negative = 0
    curious = 0
    for comment in comments:
        text = str(comment.body_text or "")
        if any(keyword in text for keyword in _POSITIVE_COMMENT_KEYWORDS):
            positive += 1
        if any(keyword in text for keyword in _NEGATIVE_COMMENT_KEYWORDS):
            negative += 1
        if any(keyword in text for keyword in _QUESTION_COMMENT_KEYWORDS):
            curious += 1
    if negative > max(positive, curious):
        return "negative"
    if positive > max(negative, curious):
        return "positive"
    if curious:
        return "curious"
    return "neutral"


def _keyword_feedback_summary(comment_count: int, dominant_sentiment: str) -> str:
    summary_parts = [f"最近 {comment_count} 条评论"]
    if dominant_sentiment == "negative":
        summary_parts.append("整体情绪偏担忧")
    elif dominant_sentiment == "positive":
        summary_parts.append("整体情绪偏积极")
    elif dominant_sentiment == "curious":
        summary_parts.append("读者对悬念追问较多")
    else:
        summary_parts.append("暂无明确结构化信号")
    return "，".join(summary_parts) + "。"


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

    def get_active_genesis_revision(self, project_id: str) -> BookGenesisRevision | None:
        project = self.get_project(project_id)
        if project is None:
            return None
        revision_id = str(getattr(project, "active_genesis_revision_id", "") or "").strip()
        if revision_id:
            row = self.session.get(BookGenesisRevision, revision_id)
            if row is not None:
                return row
        stmt = (
            select(BookGenesisRevision)
            .where(BookGenesisRevision.project_id == project_id)
            .order_by(BookGenesisRevision.revision.desc(), BookGenesisRevision.created_at.desc())
            .limit(1)
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def list_prompt_traces(
        self,
        project_id: str,
        *,
        stage_key: str = "",
        limit: int = 40,
    ) -> list[PromptTrace]:
        stmt = (
            select(PromptTrace)
            .where(PromptTrace.project_id == project_id)
            .order_by(PromptTrace.created_at.desc(), PromptTrace.id.desc())
            .limit(max(1, int(limit or 40)))
        )
        if str(stage_key or "").strip():
            stmt = stmt.where(PromptTrace.stage_key == str(stage_key or "").strip())
        return list(self.session.execute(stmt).scalars().all())

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

    def get_latest_arc_structure_draft(self, project_id: str) -> ArcStructureDraft | None:
        active_arc = self.get_active_arc_plan(project_id)
        if active_arc is None:
            return None
        stmt = (
            select(ArcStructureDraft)
            .where(
                ArcStructureDraft.project_id == project_id,
                ArcStructureDraft.arc_id == active_arc.id,
            )
            .order_by(ArcStructureDraft.created_at.desc())
            .limit(1)
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def get_reader_promise(self, project_id: str) -> ReaderPromise | None:
        structure = self.get_latest_arc_structure_draft(project_id)
        if structure is None:
            return None
        payload = _load_json_object(structure.reader_promise_json, {})
        if not isinstance(payload, dict) or not payload:
            return None
        return ReaderPromise.model_validate(payload)

    def get_arc_payoff_map(self, project_id: str) -> ArcPayoffMap | None:
        structure = self.get_latest_arc_structure_draft(project_id)
        if structure is None:
            return None
        payload = _load_json_object(structure.arc_payoff_map_json, {})
        if not isinstance(payload, dict) or not payload:
            return None
        return ArcPayoffMap.model_validate(payload)

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

    def get_chapter_experience_plan(
        self,
        project_id: str,
        chapter_number: int,
    ) -> ChapterExperiencePlan | None:
        plan = self.get_chapter_plan(project_id, chapter_number)
        if plan is None:
            return None
        payload = _load_json_object(plan.experience_plan_json, {})
        if not isinstance(payload, dict):
            return None
        return ChapterExperiencePlan.model_validate(payload)

    def get_chapter_task_contract(
        self,
        project_id: str,
        chapter_number: int,
    ) -> list[PlanTaskItem]:
        plan = self.get_chapter_plan(project_id, chapter_number)
        if plan is None:
            return []
        return load_plan_task_contract(getattr(plan, "task_contract_json", "[]"))

    def get_band_row_for_chapter(
        self,
        project_id: str,
        chapter_number: int,
    ) -> BandExperiencePlan | None:
        active_arc = self.get_active_arc_plan(project_id)
        if active_arc is None:
            return None
        return self.session.execute(
            select(BandExperiencePlan)
            .where(
                BandExperiencePlan.project_id == project_id,
                BandExperiencePlan.arc_id == active_arc.id,
                BandExperiencePlan.chapter_start <= chapter_number,
                BandExperiencePlan.chapter_end >= chapter_number,
            )
            .order_by(BandExperiencePlan.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()

    def get_band_experience_plan_for_chapter(
        self,
        project_id: str,
        chapter_number: int,
    ) -> BandDelightSchedule | None:
        row = self.get_band_row_for_chapter(project_id, chapter_number)
        if row is None:
            return None
        payload = _load_json_object(row.schedule_json, {})
        if not isinstance(payload, dict):
            return None
        return BandDelightSchedule.model_validate(payload)

    def get_band_task_contract_for_chapter(
        self,
        project_id: str,
        chapter_number: int,
    ) -> list[PlanTaskItem]:
        row = self.get_band_row_for_chapter(project_id, chapter_number)
        if row is None:
            return []
        return load_plan_task_contract(getattr(row, "task_contract_json", "[]"))

    def get_next_band_summary(
        self,
        project_id: str,
        chapter_number: int,
    ) -> NextBandSummary | None:
        active_arc = self.get_active_arc_plan(project_id)
        if active_arc is None:
            return None
        row = self.session.execute(
            select(BandExperiencePlan)
            .where(
                BandExperiencePlan.project_id == project_id,
                BandExperiencePlan.arc_id == active_arc.id,
                BandExperiencePlan.chapter_start > chapter_number,
            )
            .order_by(BandExperiencePlan.chapter_start.asc(), BandExperiencePlan.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            return None
        plans = self.session.execute(
            select(ChapterPlan)
            .where(
                ChapterPlan.project_id == project_id,
                ChapterPlan.chapter_number >= row.chapter_start,
                ChapterPlan.chapter_number <= row.chapter_end,
            )
            .order_by(ChapterPlan.chapter_number.asc())
        ).scalars().all()
        return NextBandSummary(
            band_id=row.band_id,
            chapter_start=row.chapter_start,
            chapter_end=row.chapter_end,
            chapter_titles=[str(plan.title or "") for plan in plans if str(plan.title or "").strip()],
            band_task_contract=load_plan_task_contract(getattr(row, "task_contract_json", "[]")),
        )

    def get_latest_band_checkpoint(
        self,
        project_id: str,
        *,
        band_id: str,
    ) -> BandCheckpoint | None:
        return self.session.execute(
            select(BandCheckpoint)
            .where(
                BandCheckpoint.project_id == project_id,
                BandCheckpoint.band_id == band_id,
            )
            .order_by(BandCheckpoint.created_at.desc(), BandCheckpoint.id.desc())
            .limit(1)
        ).scalar_one_or_none()

    def list_band_checkpoints(
        self,
        project_id: str,
        *,
        band_id: str = "",
        status: str = "",
    ) -> list[BandCheckpoint]:
        stmt = select(BandCheckpoint).where(BandCheckpoint.project_id == project_id)
        if band_id:
            stmt = stmt.where(BandCheckpoint.band_id == band_id)
        if status:
            stmt = stmt.where(BandCheckpoint.status == status)
        return list(
            self.session.execute(
                stmt.order_by(BandCheckpoint.created_at.desc(), BandCheckpoint.id.desc())
            ).scalars().all()
        )

    def list_chapter_rewrite_attempts(
        self,
        project_id: str,
        chapter_number: int,
    ) -> list[ChapterRewriteAttempt]:
        return self.session.execute(
            select(ChapterRewriteAttempt)
            .where(
                ChapterRewriteAttempt.project_id == project_id,
                ChapterRewriteAttempt.chapter_number == chapter_number,
            )
            .order_by(ChapterRewriteAttempt.attempt_no.asc(), ChapterRewriteAttempt.created_at.asc())
        ).scalars().all()

    def get_recent_canon_events(
        self,
        project_id: str,
        *,
        before_chapter: int,
        entity_names: list[str] | None = None,
        thread_names: list[str] | None = None,
        limit: int = 5,
    ) -> list[CanonEventEvidence]:
        rows = self.session.execute(
            select(CanonEvent)
            .where(
                CanonEvent.project_id == project_id,
                CanonEvent.chapter_number < before_chapter,
            )
            .order_by(CanonEvent.chapter_number.desc(), CanonEvent.created_at.desc())
            .limit(max(limit * 4, limit))
        ).scalars().all()
        if not rows:
            return []
        event_ids = [row.id for row in rows]
        link_rows = self.session.execute(
            select(EventEntityLink.event_id, Entity.name)
            .join(Entity, Entity.id == EventEntityLink.entity_id)
            .where(EventEntityLink.event_id.in_(event_ids))
        ).all()
        names_by_event: dict[str, list[str]] = {}
        for event_id, entity_name in link_rows:
            names_by_event.setdefault(event_id, []).append(entity_name)

        entity_set = {str(item).strip() for item in (entity_names or []) if str(item).strip()}
        thread_set = {str(item).strip() for item in (thread_names or []) if str(item).strip()}
        ranked: list[tuple[float, CanonEventEvidence]] = []
        for row in rows:
            involved_names = names_by_event.get(row.id, [])
            overlap_score = float(len(entity_set & set(involved_names))) * 3.0
            thread_score = float(
                sum(1 for thread_name in thread_set if thread_name in row.summary)
            ) * 2.0
            recency_score = max(0.0, 10.0 - float(before_chapter - row.chapter_number))
            ranked.append(
                (
                    overlap_score + thread_score + recency_score,
                    CanonEventEvidence(
                        event_id=row.id,
                        chapter_number=row.chapter_number,
                        summary=row.summary,
                        significance=row.significance,
                        involved_entity_names=involved_names,
                        evidence_id=f"canon_event:{row.id}",
                    ),
                )
            )
        ranked.sort(key=lambda item: (-item[0], -item[1].chapter_number, item[1].event_id))
        return [event for _, event in ranked[:limit]]

    def get_recent_review_notes(
        self,
        project_id: str,
        *,
        before_chapter: int,
        band_start: int | None = None,
        band_end: int | None = None,
        limit: int = 5,
    ) -> list[ReviewNote]:
        rows = self.session.execute(
            select(ChapterReview, ChapterDraft, ChapterPlan)
            .join(ChapterDraft, ChapterDraft.id == ChapterReview.draft_id)
            .join(ChapterPlan, ChapterPlan.id == ChapterDraft.chapter_plan_id)
            .where(
                ChapterPlan.project_id == project_id,
                ChapterPlan.chapter_number < before_chapter,
            )
            .order_by(ChapterPlan.chapter_number.desc(), ChapterReview.created_at.desc())
        ).all()
        notes: list[ReviewNote] = []
        seen_chapters: set[int] = set()
        for review, draft, plan in rows:
            chapter_number = int(plan.chapter_number)
            if chapter_number in seen_chapters:
                continue
            if band_start is not None and chapter_number < band_start:
                continue
            if band_end is not None and chapter_number > band_end:
                continue
            meta = _load_json_object(review.review_meta_json, {})
            if not isinstance(meta, dict):
                meta = {}
            notes.append(
                ReviewNote(
                    chapter_number=chapter_number,
                    verdict=str(review.verdict or ""),
                    summary=str(meta.get("review_summary") or draft.summary or ""),
                    issue_types=[
                        str(item.get("issue_type") or item.get("rule_name") or "")
                        for item in _load_json_object(review.issues_json, [])
                        if isinstance(item, dict)
                    ],
                    planned_reward_tags=[
                        str(item)
                        for item in (meta.get("planned_reward_tags") or [])
                        if str(item).strip()
                    ],
                    delivered_reward_tags=[
                        str(item)
                        for item in (meta.get("delivered_reward_tags") or [])
                        if str(item).strip()
                    ],
                    review_notes=[
                        str(item)
                        for item in (meta.get("review_notes") or [])
                        if str(item).strip()
                    ],
                    evidence_refs=[
                        str(item)
                        for item in (meta.get("evidence_refs") or [])
                        if str(item).strip()
                    ],
                )
            )
            seen_chapters.add(chapter_number)
            if len(notes) >= limit:
                break
        return notes

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

    def list_subworlds(self, project_id: str) -> list[SubWorld]:
        return list(
            self.session.execute(
                select(SubWorld)
                .where(SubWorld.project_id == project_id)
                .order_by(SubWorld.scope.asc(), SubWorld.created_at.asc(), SubWorld.id.asc())
            ).scalars().all()
        )

    def list_roster_items(
        self,
        project_id: str,
        subworld_ids: list[str] | None = None,
    ) -> list[SubWorldRosterItem]:
        stmt = select(SubWorldRosterItem).where(SubWorldRosterItem.project_id == project_id)
        normalized_ids = [str(item or "").strip() for item in (subworld_ids or []) if str(item or "").strip()]
        if normalized_ids:
            stmt = stmt.where(SubWorldRosterItem.subworld_id.in_(normalized_ids))
        return list(
            self.session.execute(
                stmt.order_by(
                    SubWorldRosterItem.subworld_id.asc(),
                    SubWorldRosterItem.is_core.desc(),
                    SubWorldRosterItem.created_at.asc(),
                    SubWorldRosterItem.id.asc(),
                )
            ).scalars().all()
        )

    def get_allowed_entity_names(
        self,
        project_id: str,
        chapter_number: int,
    ) -> set[str]:
        active_ids = self._active_subworld_ids_for_chapter(project_id, chapter_number)
        if not active_ids:
            active_ids = self._fallback_global_core_ids(project_id)
        roster_items = self.list_roster_items(project_id, active_ids)
        entity_ids = [
            str(item.entity_id or "").strip()
            for item in roster_items
            if item.entity_kind == "character" and str(item.entity_id or "").strip()
        ]
        names: set[str] = set()
        if entity_ids:
            entities = self.session.execute(
                select(Entity)
                .where(
                    Entity.project_id == project_id,
                    Entity.id.in_(entity_ids),
                )
            ).scalars().all()
            names.update(
                str(entity.name or "").strip()
                for entity in entities
                if str(entity.name or "").strip()
            )
            alias_rows = self.session.execute(
                select(EntityAlias.alias)
                .where(
                    EntityAlias.project_id == project_id,
                    EntityAlias.entity_id.in_(entity_ids),
                )
            ).all()
            names.update(
                str(alias or "").strip()
                for alias, in alias_rows
                if str(alias or "").strip()
            )
        chapter_experience = self.get_chapter_experience_plan(project_id, chapter_number)
        if chapter_experience is not None:
            names.update(
                str(item.entity_name or "").strip()
                for item in chapter_experience.chapter_entry_targets
                if str(item.entity_name or "").strip()
            )
        names.update(self._world_pressure_character_names(project_id, chapter_number))
        return names

    def get_allowed_entity_snapshots(
        self,
        project_id: str,
        chapter_number: int,
    ) -> list[EntitySnapshot]:
        active_subworld_ids = self._active_subworld_ids_for_chapter(project_id, chapter_number)
        if not active_subworld_ids:
            active_subworld_ids = self._fallback_global_core_ids(project_id)
        roster_items = self.list_roster_items(project_id, active_subworld_ids)
        allowed_character_ids = {
            str(item.entity_id or "").strip()
            for item in roster_items
            if item.entity_kind == "character" and str(item.entity_id or "").strip()
        }
        pressure_character_names = self._world_pressure_character_names(project_id, chapter_number)
        active_entities = self.get_active_entities(project_id)
        snapshots: list[EntitySnapshot] = []
        for entity in active_entities:
            if entity.kind != "character":
                snapshots.append(entity)
                continue
            entity_names = {
                str(entity.name or "").strip(),
                *[str(alias or "").strip() for alias in (entity.aliases or [])],
            }
            if entity.entity_id in allowed_character_ids or bool(entity_names & pressure_character_names):
                snapshots.append(entity)
        return snapshots

    def _world_pressure_character_names(
        self,
        project_id: str,
        chapter_number: int,
    ) -> set[str]:
        pressure = self.get_latest_world_pressure(project_id, before_chapter=chapter_number)
        if pressure is None:
            return set()
        pressure_text = "\n".join(
            [
                str(pressure.pressure_summary or ""),
                *[str(item or "") for item in (pressure.notable_shifts or [])],
            ]
        )
        if not pressure_text.strip():
            return set()

        names: set[str] = set()
        for entity in self.get_active_entities(project_id):
            if entity.kind != "character":
                continue
            candidates = [
                str(entity.name or "").strip(),
                *[str(alias or "").strip() for alias in (entity.aliases or [])],
            ]
            matched = [name for name in candidates if name and name in pressure_text]
            if matched:
                names.update(name for name in candidates if name)
        return names

    def get_active_subworld_summary(
        self,
        project_id: str,
        chapter_number: int,
    ) -> list[SubWorldSummary]:
        active_ids = self._active_subworld_ids_for_chapter(project_id, chapter_number)
        if not active_ids:
            active_ids = self._fallback_global_core_ids(project_id)
        active_set = set(active_ids)
        rows = self.list_subworlds(project_id)
        roster_by_subworld: dict[str, list[SubWorldRosterItem]] = {}
        for item in self.list_roster_items(project_id, [row.id for row in rows]):
            roster_by_subworld.setdefault(item.subworld_id, []).append(item)
        summaries: list[SubWorldSummary] = []
        for row in rows:
            if row.id not in active_set:
                continue
            roster = roster_by_subworld.get(row.id, [])
            summaries.append(
                SubWorldSummary(
                    id=row.id,
                    name=row.name,
                    purpose=row.purpose,
                    scope=row.scope,
                    status=row.status,
                    active_in_current_band=True,
                    core_cast=[
                        item.display_name
                        for item in roster
                        if item.is_core and str(item.display_name or "").strip()
                    ],
                    planned_slot_count=sum(1 for item in roster if item.status == "planned_slot"),
                )
            )
        return summaries

    def get_active_subworld_region_drafts(
        self,
        project_id: str,
        chapter_number: int,
    ) -> list[dict]:
        active_ids = self._active_subworld_ids_for_chapter(project_id, chapter_number)
        if not active_ids:
            active_ids = self._fallback_global_core_ids(project_id)
        active_set = set(active_ids)
        drafts: list[dict] = []
        map_region_rows = self.session.execute(
            select(MapRegionRow)
            .where(
                MapRegionRow.project_id == project_id,
                MapRegionRow.subworld_id.in_(active_set),
            )
            .order_by(MapRegionRow.created_at.asc(), MapRegionRow.id.asc())
        ).scalars().all()
        seen_names: set[tuple[str, str]] = set()
        subworld_names = {row.id: row.name for row in self.list_subworlds(project_id) if row.id in active_set}
        for region in map_region_rows:
            metadata = _load_json_object(region.metadata_json or "{}", {})
            payload = {
                "id": region.id,
                "name": region.name,
                "kind": region.region_type,
                "level": metadata.get("level", ""),
                "summary": region.description,
                "terrain": region.terrain,
                "culture_traits": region.culture_tag,
                "subworld_id": region.subworld_id,
                "subworld_name": subworld_names.get(region.subworld_id, ""),
                "region_source": metadata.get("legacy_source", "map_regions"),
                "region_promotion_state": "promoted",
            }
            drafts.append(payload)
            seen_names.add((region.subworld_id, region.name))
        for row in self.list_subworlds(project_id):
            if row.id not in active_set:
                continue
            metadata = _load_json_object(getattr(row, "metadata_json", "") or "{}", {})
            region_drafts = metadata.get("region_drafts") if isinstance(metadata, dict) else []
            if not isinstance(region_drafts, list):
                continue
            for draft in region_drafts:
                if not isinstance(draft, dict):
                    continue
                name = str(draft.get("name", "") or "").strip()
                if (row.id, name) in seen_names:
                    continue
                draft_payload = dict(draft)
                draft_payload.setdefault("subworld_id", row.id)
                draft_payload.setdefault("subworld_name", row.name)
                draft_payload.setdefault(
                    "region_source",
                    str(metadata.get("region_source", "") or "").strip(),
                )
                draft_payload.setdefault(
                    "region_promotion_state",
                    str(metadata.get("region_promotion_state", "") or "").strip(),
                )
                drafts.append(draft_payload)
        return drafts

    def get_active_rule_entities(self, project_id: str) -> list[EntitySnapshot]:
        return [
            entity
            for entity in self.get_active_entities(project_id)
            if entity.kind == "rule"
        ]

    # ------------------------------------------------------------------
    # Relations
    # ------------------------------------------------------------------

    def get_active_relations(
        self,
        project_id: str,
        entity_names: list[str] | None = None,
    ) -> list[RelationSnapshot]:
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
            if entity_names is not None:
                allowed = {str(name or "").strip() for name in entity_names if str(name or "").strip()}
                if allowed and source_name not in allowed and target_name not in allowed:
                    continue
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

    def list_active_narrative_constraints(
        self,
        project_id: str,
        *,
        chapter_number: int,
    ) -> list[NarrativeConstraintInfo]:
        rows = self.session.execute(
            select(NarrativeConstraint)
            .where(
                NarrativeConstraint.project_id == project_id,
                NarrativeConstraint.status == "active",
                NarrativeConstraint.effective_from_chapter <= chapter_number,
                or_(
                    NarrativeConstraint.protect_until_chapter == 0,
                    NarrativeConstraint.protect_until_chapter >= chapter_number,
                ),
            )
            .order_by(
                NarrativeConstraint.level.asc(),
                NarrativeConstraint.protect_until_chapter.desc(),
                NarrativeConstraint.created_at.desc(),
            )
        ).scalars().all()
        return [
            NarrativeConstraintInfo(
                id=row.id,
                project_id=row.project_id,
                arc_id=row.arc_id,
                band_id=row.band_id,
                constraint_type=row.constraint_type,
                level=row.level,
                subject_name=row.subject_name,
                description=row.description,
                payload=_load_json_object(row.payload_json, {}),
                effective_from_chapter=row.effective_from_chapter,
                protect_until_chapter=row.protect_until_chapter,
                status=row.status,
            )
            for row in rows
        ]

    def future_constraints_enabled(self, project_id: str) -> bool:
        project = self.session.get(Project, project_id)
        if project is None:
            return False
        return bool(normalize_project_governance(getattr(project, "governance_json", "") or "").future_constraints_enabled)

    def list_narrative_constraints(
        self,
        project_id: str,
    ) -> list[NarrativeConstraint]:
        return list(
            self.session.execute(
                select(NarrativeConstraint)
                .where(NarrativeConstraint.project_id == project_id)
                .order_by(NarrativeConstraint.created_at.desc(), NarrativeConstraint.id.desc())
            ).scalars().all()
        )

    def list_decision_events(
        self,
        project_id: str,
        *,
        scope: str = "",
        band_id: str = "",
        chapter_number: int = 0,
        task_id: str = "",
        event_family: str = "",
        related_object_type: str = "",
        related_object_id: str = "",
        causal_root_id: str = "",
        limit: int = 50,
    ) -> list[DecisionEventInfo]:
        stmt = select(DecisionEvent).where(DecisionEvent.project_id == project_id)
        if scope:
            stmt = stmt.where(DecisionEvent.scope == scope)
        if band_id:
            stmt = stmt.where(DecisionEvent.band_id == band_id)
        if chapter_number > 0:
            stmt = stmt.where(DecisionEvent.chapter_number == chapter_number)
        if task_id:
            stmt = stmt.where(DecisionEvent.task_id == task_id)
        if event_family:
            stmt = stmt.where(DecisionEvent.event_family == event_family)
        if related_object_type:
            stmt = stmt.where(DecisionEvent.related_object_type == related_object_type)
        if related_object_id:
            stmt = stmt.where(DecisionEvent.related_object_id == related_object_id)
        if causal_root_id:
            stmt = stmt.where(DecisionEvent.causal_root_id == causal_root_id)
        rows = self.session.execute(
            stmt.order_by(DecisionEvent.created_at.desc(), DecisionEvent.id.desc()).limit(max(1, limit))
        ).scalars().all()
        return [
            DecisionEventInfo(
                id=row.id,
                project_id=row.project_id,
                task_id=row.task_id,
                band_id=row.band_id,
                chapter_number=row.chapter_number,
                scope=row.scope,
                event_family=row.event_family,
                event_type=row.event_type,
                actor_type=row.actor_type,
                actor_id=row.actor_id,
                summary=row.summary,
                reason=row.reason,
                payload=_load_json_object(row.payload_json, {}),
                related_object_type=row.related_object_type,
                related_object_id=row.related_object_id,
                parent_event_id=str(getattr(row, "parent_event_id", "") or ""),
                causal_root_id=str(getattr(row, "causal_root_id", "") or ""),
                created_at=row.created_at.isoformat() if row.created_at else "",
            )
            for row in rows
        ]

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

        allowed_chapter_titles = self.session.execute(
            select(ChapterPlan.title)
            .where(
                ChapterPlan.project_id == project_id,
                ChapterPlan.chapter_number < before_chapter,
            )
            .order_by(ChapterPlan.chapter_number.desc())
        ).scalars().all()
        normalized_allowed_titles = {
            str(item).strip()
            for item in allowed_chapter_titles
            if str(item).strip()
        }
        has_project_scoped_comments = bool(
            self.session.execute(
                select(func.count(PublisherRawComment.id)).where(
                    PublisherRawComment.project_id == project_id
                )
            ).scalar_one()
        )

        comments_stmt = select(PublisherRawComment)
        if has_project_scoped_comments:
            comments_stmt = comments_stmt.where(PublisherRawComment.project_id == project_id)
        else:
            comments_stmt = comments_stmt.where(
                or_(
                    PublisherRawComment.project_id == project_id,
                    PublisherRawComment.project_id == "",
                ),
                PublisherRawComment.work_name == work_name,
            )
        if normalized_allowed_titles:
            comments_stmt = comments_stmt.where(
                or_(
                    PublisherRawComment.chapter_title.in_(sorted(normalized_allowed_titles)),
                    PublisherRawComment.chapter_title == "",
                )
            )
        recent_comments = self.session.execute(
            comments_stmt
            .order_by(PublisherRawComment.synced_at.desc(), PublisherRawComment.updated_at.desc())
            .limit(limit)
        ).scalars().all()

        aggregate_rows = self.session.execute(
            select(SignalWindowAggregate)
            .where(
                SignalWindowAggregate.project_id == project_id,
                SignalWindowAggregate.window_chapter_end < before_chapter,
            )
            .order_by(SignalWindowAggregate.window_chapter_end.desc())
        ).scalars().all()
        structured_snapshot: list[SignalWindowAggregate] = []
        if aggregate_rows:
            anchor_row = sorted(
                aggregate_rows,
                key=lambda row: (
                    int(row.window_chapter_end or 0),
                    -_READER_FEEDBACK_WINDOW_PRIORITY.get(str(row.window_type or ""), 99),
                ),
                reverse=True,
            )[0]
            structured_snapshot = [
                row
                for row in aggregate_rows
                if row.window_chapter_end == anchor_row.window_chapter_end
                and row.window_type == anchor_row.window_type
            ]

        structured_signals = sorted(
            [
                row
                for row in structured_snapshot
                if str(row.signal_level or "noise") != "noise"
            ],
            key=_reader_feedback_sort_key,
            reverse=True,
        )

        comment_count = (
            max(
                len(recent_comments),
                max((int(row.total_comment_count or 0) for row in structured_snapshot), default=0),
            )
            if structured_snapshot
            else len(recent_comments)
        )
        if not recent_comments and comment_count <= 0 and not structured_signals:
            return None
        highlights = [
            ReaderCommentView(
                platform_id=row.platform_id,
                author_name=row.author_name,
                body_text=str(row.body_text or "")[:180],
                chapter_title=row.chapter_title,
                remote_created_at=row.remote_created_at,
            )
            for row in recent_comments[:4]
        ]
        if structured_signals:
            dominant = structured_signals[0]
            dominant_sentiment = f"{dominant.signal_type}:{dominant.signal_level}"
            highlighted_topics = [
                f"{_reader_feedback_target_label(str(row.target_name or ''))}:{row.signal_type}:{row.signal_level}"
                for row in structured_signals[:3]
            ]
            confirmed_signals = [
                SignalSummaryView(
                    signal_key=str(row.signal_key or ""),
                    signal_type=str(row.signal_type or ""),
                    target_name=str(row.target_name or ""),
                    level=str(row.signal_level or "noise"),
                    hit_count=int(row.hit_comment_count or 0),
                    max_severity=int(row.max_severity or 0),
                )
                for row in structured_signals
                if str(row.signal_level or "noise") in {"confirmed", "watchlist"}
            ][:6]
            summary_parts = [
                f"最近 {comment_count} 条评论",
                "主导信号："
                f"{_reader_feedback_target_label(str(dominant.target_name or ''))}:"
                f"{dominant.signal_type}:{dominant.signal_level}",
            ]
            if len(structured_signals) > 1:
                summary_parts.append(
                    "关注点："
                    + "、".join(
                        f"{_reader_feedback_target_label(str(row.target_name or ''))}:"
                        f"{row.signal_type}:{row.signal_level}"
                        for row in structured_signals[:3]
                    )
                )
            feedback_summary = "，".join(summary_parts) + "。"
        else:
            dominant_sentiment = _keyword_dominant_sentiment(recent_comments)
            highlighted_topics = []
            confirmed_signals = []
            feedback_summary = _keyword_feedback_summary(comment_count, dominant_sentiment)

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
            comment_count=int(comment_count),
            dominant_sentiment=str(dominant_sentiment or "neutral"),
            feedback_summary=str(feedback_summary or ""),
            recent_highlights=highlights[:4],
            highlighted_topics=highlighted_topics,
            confirmed_signals=confirmed_signals,
            reader_tier=reader_tier,
        )

    def get_audience_trends(
        self,
        project_id: str,
        before_chapter: int,
        *,
        window_type: str = "long",
        limit: int = 6,
    ) -> list[AudienceTrendView]:
        rows = self.session.execute(
            select(SignalWindowAggregate)
            .where(
                SignalWindowAggregate.project_id == project_id,
                SignalWindowAggregate.window_chapter_end < before_chapter,
            )
            .order_by(
                SignalWindowAggregate.window_chapter_end.desc(),
                SignalWindowAggregate.created_at.desc(),
            )
        ).scalars().all()
        if not rows:
            return []
        return derive_audience_trends(
            rows,
            window_type=window_type,
            limit=limit,
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
            elif rec.signal_type in {"character_heat", "relationship_interest"}:
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

    def _active_subworld_ids_for_chapter(
        self,
        project_id: str,
        chapter_number: int,
    ) -> list[str]:
        chapter_experience = self.get_chapter_experience_plan(project_id, chapter_number)
        if chapter_experience is not None and chapter_experience.active_subworld_ids:
            return [
                str(item).strip()
                for item in chapter_experience.active_subworld_ids
                if str(item).strip()
            ]
        band_schedule = self.get_band_experience_plan_for_chapter(project_id, chapter_number)
        if band_schedule is not None and band_schedule.active_subworld_ids:
            return [
                str(item).strip()
                for item in band_schedule.active_subworld_ids
                if str(item).strip()
            ]
        return []

    def _fallback_global_core_ids(self, project_id: str) -> list[str]:
        rows = self.session.execute(
            select(SubWorld.id)
            .where(
                SubWorld.project_id == project_id,
                SubWorld.scope == "global_core",
                SubWorld.status == "active",
            )
            .order_by(SubWorld.created_at.asc(), SubWorld.id.asc())
        ).all()
        return [str(subworld_id) for subworld_id, in rows if str(subworld_id or "").strip()]

    def get_thread_by_name(
        self, project_id: str, name: str
    ) -> Optional[PlotThread]:
        """Find a plot thread by exact name."""
        stmt = select(PlotThread).where(
            PlotThread.project_id == project_id,
            PlotThread.name == name,
        )
        return self.session.execute(stmt).scalar_one_or_none()
