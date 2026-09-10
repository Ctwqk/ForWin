from __future__ import annotations

import json
import logging
from typing import Optional

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from forwin.audience.feedback import (
    keyword_dominant_sentiment,
    keyword_feedback_summary,
)
from forwin.models import (
    ArcEnvelope,
    ArcPlanVersion,
    ArcStructureDraft,
    BandCheckpoint,
    BandExperiencePlan,
    BookGenesisRevision,
    ChapterPlan,
    ChapterRewriteAttempt,
    FeedbackActionRecord,
    MapRegionRow,
    NarrativeConstraint,
    Project,
    PublisherRawComment,
    ReaderScaleSnapshot,
    SignalWindowAggregate,
    SubWorld,
    SubWorldRosterItem,
    WorldSimulationTurn,
)
from forwin.planning.checkpoints import NextBandSummary
from forwin.planning.constraints import NarrativeConstraintInfo
from forwin.planning.contracts import (
    PlanTaskItem,
    load_plan_task_contract,
)
from forwin.protocol import (
    ArcPayoffMap,
    BandDelightSchedule,
    ChapterExperiencePlan,
    ReaderCommentView,
    ReaderFeedbackView,
    ReaderPromise,
    SignalSummaryView,
    SubWorldSummary,
    WorldPressureView,
)
from forwin.runtime.policy_store import ProjectPolicyStore

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

def _load_json_object(raw: str, default):
    try:
        return json.loads(raw or "")
    except (json.JSONDecodeError, TypeError):
        return default


def _reader_feedback_target_label(target_name: str) -> str:
    return str(target_name or "").strip() or "整体"


def _reader_feedback_sort_key(
    row: SignalWindowAggregate,
) -> tuple[int, int, int, int, str]:
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

    def get_active_genesis_revision(
        self, project_id: str
    ) -> BookGenesisRevision | None:
        project = self.get_project(project_id)
        if project is None:
            return None
        revision_id = str(
            getattr(project, "active_genesis_revision_id", "") or ""
        ).strip()
        if revision_id:
            row = self.session.get(BookGenesisRevision, revision_id)
            if row is not None:
                return row
        stmt = (
            select(BookGenesisRevision)
            .where(BookGenesisRevision.project_id == project_id)
            .order_by(
                BookGenesisRevision.revision.desc(),
                BookGenesisRevision.created_at.desc(),
            )
            .limit(1)
        )
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

    def get_latest_arc_structure_draft(
        self, project_id: str
    ) -> ArcStructureDraft | None:
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
            .order_by(
                BandExperiencePlan.chapter_start.asc(),
                BandExperiencePlan.created_at.desc(),
            )
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            return None
        plans = (
            self.session.execute(
                select(ChapterPlan)
                .where(
                    ChapterPlan.project_id == project_id,
                    ChapterPlan.chapter_number >= row.chapter_start,
                    ChapterPlan.chapter_number <= row.chapter_end,
                )
                .order_by(ChapterPlan.chapter_number.asc())
            )
            .scalars()
            .all()
        )
        return NextBandSummary(
            band_id=row.band_id,
            chapter_start=row.chapter_start,
            chapter_end=row.chapter_end,
            chapter_titles=[
                str(plan.title or "") for plan in plans if str(plan.title or "").strip()
            ],
            band_task_contract=load_plan_task_contract(
                getattr(row, "task_contract_json", "[]")
            ),
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
                stmt.order_by(
                    BandCheckpoint.created_at.desc(), BandCheckpoint.id.desc()
                )
            )
            .scalars()
            .all()
        )

    def list_chapter_rewrite_attempts(
        self,
        project_id: str,
        chapter_number: int,
    ) -> list[ChapterRewriteAttempt]:
        return (
            self.session.execute(
                select(ChapterRewriteAttempt)
                .where(
                    ChapterRewriteAttempt.project_id == project_id,
                    ChapterRewriteAttempt.chapter_number == chapter_number,
                )
                .order_by(
                    ChapterRewriteAttempt.attempt_no.asc(),
                    ChapterRewriteAttempt.created_at.asc(),
                )
            )
            .scalars()
            .all()
        )

    def list_subworlds(self, project_id: str) -> list[SubWorld]:
        return list(
            self.session.execute(
                select(SubWorld)
                .where(SubWorld.project_id == project_id)
                .order_by(
                    SubWorld.scope.asc(), SubWorld.created_at.asc(), SubWorld.id.asc()
                )
            )
            .scalars()
            .all()
        )

    def list_roster_items(
        self,
        project_id: str,
        subworld_ids: list[str] | None = None,
    ) -> list[SubWorldRosterItem]:
        stmt = select(SubWorldRosterItem).where(
            SubWorldRosterItem.project_id == project_id
        )
        normalized_ids = [
            str(item or "").strip()
            for item in (subworld_ids or [])
            if str(item or "").strip()
        ]
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
            )
            .scalars()
            .all()
        )

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
                    planned_slot_count=sum(
                        1 for item in roster if item.status == "planned_slot"
                    ),
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
        map_region_rows = (
            self.session.execute(
                select(MapRegionRow)
                .where(
                    MapRegionRow.project_id == project_id,
                    MapRegionRow.subworld_id.in_(active_set),
                )
                .order_by(MapRegionRow.created_at.asc(), MapRegionRow.id.asc())
            )
            .scalars()
            .all()
        )
        seen_names: set[tuple[str, str]] = set()
        subworld_names = {
            row.id: row.name
            for row in self.list_subworlds(project_id)
            if row.id in active_set
        }
        for region in map_region_rows:
            metadata = _load_json_object(region.metadata_json or "{}", {})
            drafts.append(
                {
                    "id": region.id,
                    "name": region.name,
                    "kind": region.region_type,
                    "level": metadata.get("level", ""),
                    "summary": region.description,
                    "terrain": region.terrain,
                    "culture_traits": region.culture_tag,
                    "subworld_id": region.subworld_id,
                    "subworld_name": subworld_names.get(region.subworld_id, ""),
                    "region_source": metadata.get("region_source", "map_regions"),
                    "region_promotion_state": "promoted",
                }
            )
            seen_names.add((region.subworld_id, region.name))
        for row in self.list_subworlds(project_id):
            if row.id not in active_set:
                continue
            metadata = _load_json_object(getattr(row, "metadata_json", "") or "{}", {})
            region_drafts = (
                metadata.get("region_drafts") if isinstance(metadata, dict) else []
            )
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

    def list_active_narrative_constraints(
        self,
        project_id: str,
        *,
        chapter_number: int,
    ) -> list[NarrativeConstraintInfo]:
        rows = (
            self.session.execute(
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
            )
            .scalars()
            .all()
        )
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
        return (
            ProjectPolicyStore(self.session)
            .load(project)
            .policy.planning.future_constraints
        )

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

        allowed_chapter_titles = (
            self.session.execute(
                select(ChapterPlan.title)
                .where(
                    ChapterPlan.project_id == project_id,
                    ChapterPlan.chapter_number < before_chapter,
                )
                .order_by(ChapterPlan.chapter_number.desc())
            )
            .scalars()
            .all()
        )
        normalized_allowed_titles = {
            str(item).strip() for item in allowed_chapter_titles if str(item).strip()
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
            comments_stmt = comments_stmt.where(
                PublisherRawComment.project_id == project_id
            )
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
                    PublisherRawComment.chapter_title.in_(
                        sorted(normalized_allowed_titles)
                    ),
                    PublisherRawComment.chapter_title == "",
                )
            )
        recent_comments = (
            self.session.execute(
                comments_stmt.order_by(
                    PublisherRawComment.synced_at.desc(),
                    PublisherRawComment.updated_at.desc(),
                ).limit(limit)
            )
            .scalars()
            .all()
        )

        aggregate_rows = (
            self.session.execute(
                select(SignalWindowAggregate)
                .where(
                    SignalWindowAggregate.project_id == project_id,
                    SignalWindowAggregate.window_chapter_end < before_chapter,
                )
                .order_by(SignalWindowAggregate.window_chapter_end.desc())
            )
            .scalars()
            .all()
        )
        structured_snapshot: list[SignalWindowAggregate] = []
        if aggregate_rows:
            anchor_row = sorted(
                aggregate_rows,
                key=lambda row: (
                    int(row.window_chapter_end or 0),
                    -_READER_FEEDBACK_WINDOW_PRIORITY.get(
                        str(row.window_type or ""), 99
                    ),
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
                max(
                    (int(row.total_comment_count or 0) for row in structured_snapshot),
                    default=0,
                ),
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
            dominant_sentiment = keyword_dominant_sentiment(recent_comments)
            highlighted_topics = []
            confirmed_signals = []
            feedback_summary = keyword_feedback_summary(
                comment_count, dominant_sentiment
            )

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

    # ------------------------------------------------------------------
    # Audience hints (Phase C)
    # ------------------------------------------------------------------

    def get_audience_hints(self, project_id: str, before_chapter: int):
        """Read only qualified, selected hints inside their explicit validity window."""
        from forwin.audience.actions import action_hint, action_hint_available
        from forwin.protocol.context import AudienceHintView

        records = self.session.scalars(
            select(FeedbackActionRecord).where(
                FeedbackActionRecord.project_id == project_id,
                FeedbackActionRecord.status == "selected",
                FeedbackActionRecord.source_qualified.is_(True),
                FeedbackActionRecord.selected_at_chapter < before_chapter,
                FeedbackActionRecord.hint_valid_from_chapter <= before_chapter,
                FeedbackActionRecord.hint_expires_at_chapter >= before_chapter,
                FeedbackActionRecord.target_chapter_start <= before_chapter,
                FeedbackActionRecord.target_chapter_end >= before_chapter,
            ).order_by(FeedbackActionRecord.selected_at.desc(), FeedbackActionRecord.id)
        ).all()
        items = [action_hint(row) for row in records if action_hint_available(row, before_chapter)]
        return AudienceHintView(items=items).clipped() if items else None

    def _active_subworld_ids_for_chapter(
        self,
        project_id: str,
        chapter_number: int,
    ) -> list[str]:
        chapter_experience = self.get_chapter_experience_plan(
            project_id, chapter_number
        )
        if chapter_experience is not None and chapter_experience.active_subworld_ids:
            return [
                str(item).strip()
                for item in chapter_experience.active_subworld_ids
                if str(item).strip()
            ]
        band_schedule = self.get_band_experience_plan_for_chapter(
            project_id, chapter_number
        )
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
        return [
            str(subworld_id)
            for (subworld_id,) in rows
            if str(subworld_id or "").strip()
        ]
