from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from forwin.arc_sizing import ArcPolicyTier, policy_for_total_chapters
from forwin.audience_metrics import derive_audience_trends
from forwin.director.arc_director import ArcDirector
from forwin.models import (
    ArcEnvelope,
    ArcEnvelopeAnalysis,
    ArcPlanVersion,
    ArcStructureDraft,
    BandExperiencePlan,
    ChapterPlan,
    Project,
    ProvisionalBandExecution,
    ProvisionalPromotionRecord,
    SignalWindowAggregate,
    new_id,
)
from forwin.orchestrator.goals import load_goals_json
from forwin.planning.world_contracts import (
    ArcWorldContract,
    BandWorldContract,
    ChapterWorldDeltaIntent,
    ReaderCognitionTransition,
    RevealLadderStep,
    WorldContractRepository,
)
from forwin.planning.scenario_rehearsal_resolution import ScenarioRehearsalCoordinator
from forwin.protocol.experience import (
    AmbiguityPayoff,
    ArcPayoffMap,
    BandDelightSchedule,
    BandRewardItem,
    ChapterExperiencePlan,
    CuriosityBeat,
    ReaderPromise,
)
from forwin.protocol.scenario_rehearsal import ScenarioRehearsalReport, ScenarioRehearsalRecommendation
from forwin.protocol.trope_library import TROPE_TEMPLATE_LIBRARY, trope_templates_by_category
from forwin.state.updater import StateUpdater
from forwin.subworld_manager import SubWorldManager

logger = logging.getLogger(__name__)


def _clamp_int(value: float | int, lower: int, upper: int) -> int:
    return max(lower, min(int(round(value)), upper))


def _load_long_window_audience_trends(
    session: Session,
    project_id: str,
    *,
    limit: int = 3,
) -> list[str]:
    rows = session.execute(
        select(SignalWindowAggregate)
        .where(
            SignalWindowAggregate.project_id == project_id,
            SignalWindowAggregate.window_type == "long",
            SignalWindowAggregate.signal_level.in_(("confirmed", "watchlist")),
        )
        .order_by(
            SignalWindowAggregate.window_chapter_end.desc(),
            SignalWindowAggregate.unique_user_count.desc(),
            SignalWindowAggregate.max_severity.desc(),
        )
        .limit(limit)
    ).scalars().all()
    if not rows:
        return []
    trend_views = derive_audience_trends(rows, window_type="long", limit=limit)
    if trend_views:
        return [
            f"{row.target_name or '整体'}:{row.signal_type}:{row.current_level}"
            for row in trend_views
        ]
    return [
        f"{row.target_name or '整体'}:{row.signal_type}:{row.signal_level}"
        for row in rows
    ]


@dataclass(slots=True)
class AudienceCalibrationProfile:
    boost_reward_density: bool = False
    clarify_rule_legibility: bool = False
    protect_character_heat: bool = False
    hold_managed_ambiguity: bool = False


def _load_long_window_audience_trend_views(
    session: Session,
    project_id: str,
    *,
    limit: int = 6,
):
    rows = session.execute(
        select(SignalWindowAggregate)
        .where(
            SignalWindowAggregate.project_id == project_id,
            SignalWindowAggregate.window_type == "long",
            SignalWindowAggregate.signal_level.in_(("confirmed", "watchlist", "candidate")),
        )
        .order_by(
            SignalWindowAggregate.window_chapter_end.desc(),
            SignalWindowAggregate.unique_user_count.desc(),
            SignalWindowAggregate.max_severity.desc(),
        )
    ).scalars().all()
    return derive_audience_trends(rows, window_type="long", limit=limit)


@dataclass(slots=True)
class ArcStructureDraftData:
    phase_layout: list[str]
    key_beats: list[str]
    thread_priorities: list[dict[str, object]]
    hotspot_candidates: list[str]
    compression_candidates: list[str]
    reader_promise: ReaderPromise
    arc_payoff_map: ArcPayoffMap


@dataclass(slots=True)
class ArcEnvelopeResolution:
    base_target_size: int
    base_soft_min: int
    base_soft_max: int
    resolved_target_size: int
    resolved_soft_min: int
    resolved_soft_max: int
    detailed_band_size: int
    frozen_zone_size: int
    recommendation: str
    evidence: list[str]
    expansion_signals: list[str]
    compression_signals: list[str]
    confidence: float
    based_on_band_id: str
    source_policy_tier: str


@dataclass(slots=True)
class ProvisionalBandPreview:
    band_id: str
    artifact_path: str
    aggregate_verdict: str
    preview_chapter_count: int
    total_char_count: int
    issue_count: int
    failure_count: int
    chapter_numbers: list[int]
    summary_lines: list[str]


class ArcEnvelopeManager:
    def __init__(
        self,
        *,
        director: ArcDirector | None = None,
        provisional_executor: Any | None = None,
        subworld_manager: SubWorldManager | None = None,
        legacy_preview_enabled: bool = False,
        scenario_progress_callback: Any | None = None,
    ) -> None:
        self.director = director
        self.provisional_executor = provisional_executor
        self.subworld_manager = subworld_manager or SubWorldManager(director=director)
        self.legacy_preview_enabled = legacy_preview_enabled
        self.scenario_progress_callback = scenario_progress_callback

    def _emit_scenario_progress(
        self,
        *,
        stage: str,
        project_id: str,
        chapter_number: int = 0,
        message: str = "",
    ) -> None:
        if self.scenario_progress_callback is None:
            return
        self.scenario_progress_callback(
            stage=stage,
            project_id=project_id,
            current_chapter=chapter_number,
            message=message,
        )

    def ensure_active_arc_resolution(
        self,
        *,
        session: Session,
        project_id: str,
        activation_chapter: int = 1,
    ) -> ArcEnvelope | None:
        self._activate_arc_for_chapter(
            session=session,
            project_id=project_id,
            chapter_number=activation_chapter,
        )
        active_arc = session.execute(
            select(ArcPlanVersion)
            .where(
                ArcPlanVersion.project_id == project_id,
                ArcPlanVersion.status == "active",
            )
            .order_by(ArcPlanVersion.version.desc())
            .limit(1)
        ).scalar_one_or_none()
        if active_arc is None:
            return None

        self.subworld_manager.ensure_registry(session, project_id)
        self.subworld_manager.ensure_initial_registry_for_active_arc(
            session=session,
            project_id=project_id,
        )

        existing = session.execute(
            select(ArcEnvelope)
            .where(ArcEnvelope.arc_id == active_arc.id)
            .order_by(ArcEnvelope.updated_at.desc(), ArcEnvelope.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if existing is not None:
            existing.current_projected_size = max(
                existing.current_projected_size,
                min(existing.resolved_soft_max, max(activation_chapter, existing.resolved_target_size)),
            )
            chapter_plans = session.execute(
                select(ChapterPlan)
                .where(ChapterPlan.arc_plan_id == active_arc.id)
                .order_by(ChapterPlan.chapter_number.asc())
            ).scalars().all()
            latest_structure = session.execute(
                select(ArcStructureDraft)
                .where(
                    ArcStructureDraft.project_id == project_id,
                    ArcStructureDraft.arc_id == active_arc.id,
                )
                .order_by(ArcStructureDraft.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            if latest_structure is not None and chapter_plans:
                self._persist_experience_overlay(
                    session=session,
                    project_id=project_id,
                    arc_id=active_arc.id,
                    chapter_plans=chapter_plans,
                    activation_chapter=activation_chapter,
                    detailed_band_size=existing.detailed_band_size,
                    structure=ArcStructureDraftData(
                        phase_layout=json.loads(latest_structure.phase_layout_json or "[]") or [],
                        key_beats=json.loads(latest_structure.key_beats_json or "[]") or [],
                        thread_priorities=json.loads(latest_structure.thread_priorities_json or "[]") or [],
                        hotspot_candidates=json.loads(latest_structure.hotspot_candidates_json or "[]") or [],
                        compression_candidates=json.loads(latest_structure.compression_candidates_json or "[]") or [],
                        reader_promise=ReaderPromise.model_validate(
                            json.loads(latest_structure.reader_promise_json or "{}") or {}
                        ),
                        arc_payoff_map=ArcPayoffMap.model_validate(
                            json.loads(latest_structure.arc_payoff_map_json or "{}") or {}
                        ),
                    ),
                )
            return existing

        chapter_plans = session.execute(
            select(ChapterPlan)
            .where(ChapterPlan.arc_plan_id == active_arc.id)
            .order_by(ChapterPlan.chapter_number.asc())
        ).scalars().all()
        project = session.get(Project, project_id)
        if project is None:
            return None
        total_chapter_count = int(
            getattr(project, "target_total_chapters", 0)
            or session.execute(
                select(func.count(ChapterPlan.id)).where(ChapterPlan.project_id == project_id)
            ).scalar_one()
            or 0
        )

        policy = policy_for_total_chapters(total_chapter_count)
        persisted_target = max(0, int(getattr(active_arc, "planned_target_size", 0) or 0))
        persisted_soft_min = max(0, int(getattr(active_arc, "planned_soft_min", 0) or 0))
        persisted_soft_max = max(0, int(getattr(active_arc, "planned_soft_max", 0) or 0))
        if persisted_target > 0:
            base_target_size = persisted_target
            base_soft_min = persisted_soft_min or max(1, int(round(base_target_size * policy.soft_min_ratio)))
            base_soft_max = persisted_soft_max or max(base_target_size, int(round(base_target_size * policy.soft_max_ratio)))
        else:
            base_target_size = _clamp_int(
                total_chapter_count * policy.ratio,
                policy.min_size,
                policy.max_size,
            )
            base_soft_min = max(1, int(round(base_target_size * policy.soft_min_ratio)))
            base_soft_max = max(base_target_size, int(round(base_target_size * policy.soft_max_ratio)))
        provisional_target = _clamp_int(base_target_size * 0.40, 4, 12)
        provisional_band = chapter_plans[: provisional_target]
        band_id = f"band:1:{provisional_target}"

        structure = self._build_structure_draft(
            session=session,
            project=project,
            total_chapters=total_chapter_count,
            chapter_plans=chapter_plans,
            policy=policy,
            base_target_size=base_target_size,
        )
        structure_row = ArcStructureDraft(
            id=new_id(),
            project_id=project_id,
            arc_id=active_arc.id,
            phase_layout_json=json.dumps(structure.phase_layout, ensure_ascii=False),
            key_beats_json=json.dumps(structure.key_beats, ensure_ascii=False),
            thread_priorities_json=json.dumps(structure.thread_priorities, ensure_ascii=False),
            hotspot_candidates_json=json.dumps(structure.hotspot_candidates, ensure_ascii=False),
            compression_candidates_json=json.dumps(structure.compression_candidates, ensure_ascii=False),
            reader_promise_json=json.dumps(
                structure.reader_promise.model_dump(mode="json"),
                ensure_ascii=False,
            ),
            arc_payoff_map_json=json.dumps(
                structure.arc_payoff_map.model_dump(mode="json"),
                ensure_ascii=False,
            ),
        )
        session.add(structure_row)
        session.flush()

        self._persist_world_contracts(
            session=session,
            project_id=project_id,
            arc_id=active_arc.id,
            chapter_plans=chapter_plans,
            activation_chapter=activation_chapter,
            detailed_band_size=provisional_target,
        )
        self._emit_scenario_progress(
            stage="running_scenario_rehearsal",
            project_id=project_id,
            chapter_number=provisional_band[0].chapter_number if provisional_band else 0,
        )
        rehearsal_outcome = ScenarioRehearsalCoordinator(session, director=self.director).run_for_band(
            project_id=project_id,
            arc_id=active_arc.id,
            band_id=band_id,
            chapter_numbers=[plan.chapter_number for plan in provisional_band],
        )
        rehearsal = rehearsal_outcome.report
        if rehearsal.arc_id and rehearsal.arc_id != active_arc.id:
            replanned_arc = session.get(ArcPlanVersion, rehearsal.arc_id)
            if replanned_arc is not None:
                active_arc = replanned_arc
                chapter_plans = session.execute(
                    select(ChapterPlan)
                    .where(ChapterPlan.arc_plan_id == active_arc.id)
                    .order_by(ChapterPlan.chapter_number.asc())
                ).scalars().all()
                provisional_band = [
                    plan
                    for plan in chapter_plans
                    if int(plan.chapter_number or 0) in set(rehearsal.chapter_numbers)
                ]
        if rehearsal_outcome.status in {"manual_patch_required", "replan_required"}:
            self._emit_scenario_progress(
                stage="scenario_rehearsal_patch_required",
                project_id=project_id,
                chapter_number=provisional_band[0].chapter_number if provisional_band else 0,
                message="Scenario rehearsal 要求计划补丁或重排。",
            )
        elif rehearsal_outcome.status == "blocked":
            self._emit_scenario_progress(
                stage="scenario_rehearsal_blocked",
                project_id=project_id,
                chapter_number=provisional_band[0].chapter_number if provisional_band else 0,
                message="Scenario rehearsal 阻断当前计划。",
            )
        preview = (
            self._execute_provisional_band(
                session=session,
                project_id=project_id,
                arc_id=active_arc.id,
                band_id=band_id,
                chapter_plans=provisional_band,
            )
            if self.legacy_preview_enabled
            else None
        )
        resolution = self._resolve_envelope(
            chapter_plans=chapter_plans,
            total_chapters=total_chapter_count,
            policy=policy,
            base_target_size=base_target_size,
            base_soft_min=base_soft_min,
            base_soft_max=base_soft_max,
            structure=structure,
            provisional_band=provisional_band,
            band_id=band_id,
            preview=preview,
            rehearsal=rehearsal,
        )
        envelope = ArcEnvelope(
            id=new_id(),
            project_id=project_id,
            arc_id=active_arc.id,
            base_target_size=resolution.base_target_size,
            base_soft_min=resolution.base_soft_min,
            base_soft_max=resolution.base_soft_max,
            resolved_target_size=resolution.resolved_target_size,
            resolved_soft_min=resolution.resolved_soft_min,
            resolved_soft_max=resolution.resolved_soft_max,
            detailed_band_size=resolution.detailed_band_size,
            frozen_zone_size=resolution.frozen_zone_size,
            current_projected_size=max(
                resolution.resolved_target_size,
                min(resolution.resolved_soft_max, max(activation_chapter, resolution.detailed_band_size)),
            ),
            current_confidence=resolution.confidence,
            source_policy_tier=resolution.source_policy_tier,
        )
        session.add(envelope)
        analysis = ArcEnvelopeAnalysis(
            id=new_id(),
            project_id=project_id,
            arc_id=active_arc.id,
            based_on_band_id=resolution.based_on_band_id,
            recommendation=resolution.recommendation,
            evidence_json=json.dumps(resolution.evidence, ensure_ascii=False),
            expansion_signals_json=json.dumps(resolution.expansion_signals, ensure_ascii=False),
            compression_signals_json=json.dumps(resolution.compression_signals, ensure_ascii=False),
            suggested_target=resolution.resolved_target_size,
            suggested_soft_min=resolution.resolved_soft_min,
            suggested_soft_max=resolution.resolved_soft_max,
            confidence=resolution.confidence,
        )
        session.add(analysis)
        if preview is not None:
            session.add(
                ProvisionalBandExecution(
                    id=new_id(),
                    project_id=project_id,
                    arc_id=active_arc.id,
                    band_id=preview.band_id,
                    chapter_numbers_json=json.dumps(preview.chapter_numbers, ensure_ascii=False),
                    artifact_path=preview.artifact_path,
                    aggregate_verdict=preview.aggregate_verdict,
                    preview_char_count=preview.total_char_count,
                    issue_count=preview.issue_count,
                    failure_count=preview.failure_count,
                )
            )
        self._persist_experience_overlay(
            session=session,
            project_id=project_id,
            arc_id=active_arc.id,
            chapter_plans=chapter_plans,
            activation_chapter=activation_chapter,
            detailed_band_size=resolution.detailed_band_size,
            structure=structure,
        )
        session.flush()
        return envelope

    @staticmethod
    def _activate_arc_for_chapter(
        *,
        session: Session,
        project_id: str,
        chapter_number: int,
    ) -> ArcPlanVersion | None:
        if int(chapter_number or 0) <= 0:
            return None
        chapter_plan = session.execute(
            select(ChapterPlan)
            .where(
                ChapterPlan.project_id == project_id,
                ChapterPlan.chapter_number == int(chapter_number),
            )
            .limit(1)
        ).scalar_one_or_none()
        if chapter_plan is None:
            return None
        target_arc = session.get(ArcPlanVersion, chapter_plan.arc_plan_id)
        if target_arc is None or target_arc.status == "active":
            return target_arc
        active_rows = session.execute(
            select(ArcPlanVersion)
            .where(
                ArcPlanVersion.project_id == project_id,
                ArcPlanVersion.status == "active",
            )
        ).scalars().all()
        for row in active_rows:
            if row.id == target_arc.id:
                continue
            max_chapter = session.execute(
                select(func.max(ChapterPlan.chapter_number)).where(ChapterPlan.arc_plan_id == row.id)
            ).scalar_one()
            row.status = "completed" if int(max_chapter or 0) < int(chapter_number) else "planned"
            session.add(row)
        target_arc.status = "active"
        session.add(target_arc)
        session.flush()
        return target_arc

    def backfill_missing_resolutions(self, *, session: Session) -> int:
        project_ids = session.execute(
            select(ArcPlanVersion.project_id)
            .where(ArcPlanVersion.status == "active")
            .distinct()
        ).scalars().all()
        existing_project_ids = (
            {
                str(project_id or "").strip()
                for project_id in session.execute(
                    select(ArcPlanVersion.project_id)
                    .join(ArcEnvelope, ArcEnvelope.arc_id == ArcPlanVersion.id)
                    .where(
                        ArcPlanVersion.status == "active",
                        ArcPlanVersion.project_id.in_(project_ids),
                    )
                    .distinct()
                ).scalars().all()
                if str(project_id or "").strip()
            }
            if project_ids
            else set()
        )
        created = 0
        original_director = self.director
        original_executor = self.provisional_executor
        self.director = None
        self.provisional_executor = None
        try:
            for project_id in project_ids:
                if str(project_id or "").strip() in existing_project_ids:
                    continue
                row = self.ensure_active_arc_resolution(
                    session=session,
                    project_id=project_id,
                    activation_chapter=1,
                )
                if row is not None:
                    created += 1
        finally:
            self.director = original_director
            self.provisional_executor = original_executor
        return created

    def record_provisional_promotion(
        self,
        *,
        session: Session,
        project_id: str,
        chapter_number: int,
        reason: str = "accepted",
    ) -> ProvisionalPromotionRecord | None:
        chapter_plan = session.execute(
            select(ChapterPlan)
            .where(
                ChapterPlan.project_id == project_id,
                ChapterPlan.chapter_number == chapter_number,
            )
            .limit(1)
        ).scalar_one_or_none()
        if chapter_plan is None:
            return None
        envelope = session.execute(
            select(ArcEnvelope)
            .where(ArcEnvelope.arc_id == chapter_plan.arc_plan_id)
            .order_by(ArcEnvelope.updated_at.desc(), ArcEnvelope.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if envelope is None:
            return None
        band_index = max(1, ((chapter_number - 1) // max(1, envelope.detailed_band_size)) + 1)
        band_id = f"{chapter_plan.arc_plan_id}:band:{band_index}"
        analysis = session.execute(
            select(ArcEnvelopeAnalysis)
            .where(
                ArcEnvelopeAnalysis.arc_id == chapter_plan.arc_plan_id,
                ArcEnvelopeAnalysis.based_on_band_id == band_id,
            )
            .order_by(ArcEnvelopeAnalysis.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        existing = session.execute(
            select(ProvisionalPromotionRecord)
            .where(
                ProvisionalPromotionRecord.project_id == project_id,
                ProvisionalPromotionRecord.arc_id == chapter_plan.arc_plan_id,
                ProvisionalPromotionRecord.band_id == band_id,
            )
            .order_by(ProvisionalPromotionRecord.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if existing is None:
            existing = ProvisionalPromotionRecord(
                id=new_id(),
                project_id=project_id,
                arc_id=chapter_plan.arc_plan_id,
                band_id=band_id,
                promoted_chapter_ids_json="[]",
                promotion_reason=reason,
                based_on_analysis_id=analysis.id if analysis else "",
            )
            session.add(existing)
            session.flush()
        try:
            promoted_ids = json.loads(existing.promoted_chapter_ids_json or "[]") or []
        except (json.JSONDecodeError, TypeError):
            promoted_ids = []
        chapter_id = chapter_plan.id
        if chapter_id not in promoted_ids:
            promoted_ids.append(chapter_id)
        existing.promoted_chapter_ids_json = json.dumps(promoted_ids, ensure_ascii=False)
        existing.promotion_reason = reason
        if analysis is not None:
            existing.based_on_analysis_id = analysis.id
        return existing

    def _build_structure_draft(
        self,
        *,
        session: Session,
        project: Project,
        total_chapters: int,
        chapter_plans: list[ChapterPlan],
        policy: ArcPolicyTier,
        base_target_size: int,
    ) -> ArcStructureDraftData:
        chapter_seed = [
            {
                "chapter_number": plan.chapter_number,
                "title": plan.title,
                "one_line": plan.one_line,
                "goals": load_goals_json(plan.goals_json),
            }
            for plan in chapter_plans[: min(len(chapter_plans), 8)]
        ]
        audience_trends = _load_long_window_audience_trends(session, project.id)
        if self.director is not None:
            try:
                drafted = self.director.draft_arc_structure(
                    premise=project.premise,
                    genre=project.genre,
                    total_chapters=total_chapters,
                    policy_tier=policy.name,
                    base_target_size=base_target_size,
                    chapter_seed=chapter_seed,
                    audience_trends=audience_trends,
                )
            except Exception:
                drafted = {}
            if drafted:
                return ArcStructureDraftData(
                    phase_layout=[str(item) for item in drafted.get("phase_layout") or []],
                    key_beats=[str(item) for item in drafted.get("key_beats") or []],
                    thread_priorities=[
                        item for item in (drafted.get("thread_priorities") or []) if isinstance(item, dict)
                    ],
                    hotspot_candidates=[str(item) for item in drafted.get("hotspot_candidates") or []],
                    compression_candidates=[str(item) for item in drafted.get("compression_candidates") or []],
                    reader_promise=ReaderPromise.model_validate(drafted.get("reader_promise") or {}),
                    arc_payoff_map=ArcPayoffMap.model_validate(drafted.get("arc_payoff_map") or {}),
                )
        return ArcStructureDraftData(
            phase_layout=["setup", "pressure", "turn", "payoff"],
            key_beats=[
                plan.one_line or plan.title
                for plan in chapter_plans[: min(len(chapter_plans), 4)]
            ],
            thread_priorities=[
                {
                    "name": f"主线阶段-{index + 1}",
                    "priority": index + 1,
                    "reason": plan.one_line or plan.title,
                }
                for index, plan in enumerate(chapter_plans[:3])
            ],
            hotspot_candidates=[
                plan.title or plan.one_line
                for plan in chapter_plans[: min(len(chapter_plans), 3)]
            ],
            compression_candidates=[
                plan.title or plan.one_line
                for plan in chapter_plans[2:4]
                if plan.one_line
            ],
            reader_promise=ReaderPromise(
                genre_promise=f"{project.genre}网文",
                pleasure_promise=f"{project.genre}读者会稳定获得悬念和回报",
                core_pleasures=["稳定微回报", "阶段性翻盘", "真相逐层揭开"],
                acceptable_drag_level="low",
                acceptable_exposition_density="medium",
                cliffhanger_aggressiveness="high",
                ambiguity_mode="managed",
                world_legibility_target="关键冲突的规则与代价必须能被读者读懂。",
            ),
            arc_payoff_map=ArcPayoffMap(
                macro_payoffs=[],
                awe_kit=["反转", "线索揭面", "代价升级"],
                revelation_layers=[],
                ambiguity_constraints=["关键结果必须能回指既有线索与规则。"],
            ),
        )

    def _persist_experience_overlay(
        self,
        *,
        session: Session,
        project_id: str,
        arc_id: str,
        chapter_plans: list[ChapterPlan],
        activation_chapter: int,
        detailed_band_size: int,
        structure: ArcStructureDraftData,
    ) -> None:
        if not chapter_plans:
            return
        updater = StateUpdater(session)
        calibration = self._build_audience_calibration_profile(session=session, project_id=project_id)
        band_size = max(1, int(detailed_band_size or 1))
        current_index = max(0, activation_chapter - 1)
        band_start = (current_index // band_size) * band_size + 1
        band_end = min(len(chapter_plans), band_start + band_size - 1)
        band_id = f"band:{band_start}:{band_end}"
        active_band = [
            plan
            for plan in chapter_plans
            if band_start <= plan.chapter_number <= band_end
        ]
        schedule = self._derive_band_delight_schedule(
            band_id=band_id,
            chapter_start=band_start,
            chapter_end=band_end,
            structure=structure,
            active_band=active_band,
            calibration=calibration,
        )
        activation_plan = self.subworld_manager.plan_band_activation(
            session=session,
            updater=updater,
            project_id=project_id,
            chapter_start=band_start,
            chapter_end=band_end,
            active_band=active_band,
        )
        schedule = schedule.model_copy(
            update={
                "active_subworld_ids": activation_plan.active_subworld_ids,
                "chapter_entry_targets": activation_plan.chapter_entry_targets,
            }
        )
        session.query(BandExperiencePlan).filter(
            BandExperiencePlan.project_id == project_id,
            BandExperiencePlan.arc_id == arc_id,
            BandExperiencePlan.band_id == band_id,
        ).delete(synchronize_session=False)
        session.add(
            BandExperiencePlan(
                id=new_id(),
                project_id=project_id,
                arc_id=arc_id,
                band_id=schedule.band_id,
                chapter_start=schedule.chapter_start,
                chapter_end=schedule.chapter_end,
                stall_guard_max_gap=schedule.stall_guard_max_gap,
                schedule_json=json.dumps(schedule.model_dump(mode="json"), ensure_ascii=False),
            )
        )
        for plan in active_band:
            experience_plan = self._derive_chapter_experience_plan(
                chapter_number=plan.chapter_number,
                structure=structure,
                schedule=schedule,
                chapter_plan=plan,
                calibration=calibration,
            )
            chapter_targets = [
                item
                for item in schedule.chapter_entry_targets
                if item.chapter_hint == plan.chapter_number
            ]
            experience_plan = experience_plan.model_copy(
                update={
                    "active_subworld_ids": list(schedule.active_subworld_ids),
                    "chapter_entry_targets": chapter_targets,
                    "entity_admission_rule": "strict_named_character",
                }
            )
            plan.experience_plan_json = json.dumps(
                experience_plan.model_dump(mode="json"),
                ensure_ascii=False,
            )
            session.add(plan)
        self._persist_world_contracts(
            session=session,
            project_id=project_id,
            arc_id=arc_id,
            chapter_plans=chapter_plans,
            activation_chapter=activation_chapter,
            detailed_band_size=detailed_band_size,
        )

    def _persist_world_contracts(
        self,
        *,
        session: Session,
        project_id: str,
        arc_id: str,
        chapter_plans: list[ChapterPlan],
        activation_chapter: int,
        detailed_band_size: int,
    ) -> None:
        if not chapter_plans:
            return
        project = session.get(Project, project_id)
        arc = session.get(ArcPlanVersion, arc_id)
        if project is None or arc is None:
            return

        ordered_plans = sorted(chapter_plans, key=lambda plan: plan.chapter_number)
        all_text_parts = [
            project.title,
            project.premise,
            project.setting_summary,
            arc.arc_synopsis,
        ]
        for plan in ordered_plans:
            all_text_parts.extend([plan.title, plan.one_line])
            all_text_parts.extend(load_goals_json(plan.goals_json))
        all_text = "\n".join(str(part or "") for part in all_text_parts)

        has_homeworld_crisis = "母星" in all_text or "homeworld" in all_text.lower()
        has_colony_line = "殖民" in all_text or "colony" in all_text.lower()
        primary_line_ids = ["line_colony_defense"] if has_colony_line else ["primary_visible_line"]
        hidden_line_ids = ["line_homeworld_siege"] if has_homeworld_crisis else []
        major_gap_ids = ["gap_homeworld_siege"] if has_homeworld_crisis else []
        reveal_ladder = (
            [
                RevealLadderStep(
                    gap_id="gap_homeworld_siege",
                    chapter_hint=22,
                    from_state="hidden",
                    to_state="hinted",
                    method="通讯延迟",
                    fairness_evidence=["第22章必须出现通讯异常"],
                    must_not_reveal_before=25,
                ),
                RevealLadderStep(
                    gap_id="gap_homeworld_siege",
                    chapter_hint=25,
                    from_state="hinted",
                    to_state="partially_revealed",
                    method="残缺求援",
                    fairness_evidence=["第25章残缺求援只能部分揭示"],
                ),
                RevealLadderStep(
                    gap_id="gap_homeworld_siege",
                    chapter_hint=28,
                    from_state="partially_revealed",
                    to_state="closed",
                    method="返回母星确认",
                    fairness_evidence=["第28章确认此前线索成立"],
                ),
            ]
            if has_homeworld_crisis
            else []
        )
        reader_trajectory = (
            [
                ReaderCognitionTransition(
                    chapter_hint=22,
                    observer_id="reader",
                    from_state="hidden",
                    to_state="hinted",
                    intended_effect="不安与追问",
                    payoff_type="short_term_hint",
                ),
                ReaderCognitionTransition(
                    chapter_hint=25,
                    observer_id="reader",
                    from_state="hinted",
                    to_state="partially_revealed",
                    intended_effect="危机确认但真相未全开",
                    payoff_type="medium_reveal",
                ),
                ReaderCognitionTransition(
                    chapter_hint=28,
                    observer_id="reader",
                    from_state="partially_revealed",
                    to_state="closed",
                    intended_effect="长期悬念阶段兑现",
                    payoff_type="long_term_payoff",
                ),
            ]
            if has_homeworld_crisis
            else []
        )

        repo = WorldContractRepository(session)
        repo.save_arc_contract(
            ArcWorldContract(
                contract_id=f"arc_world_contract_{arc.id}",
                project_id=project_id,
                arc_id=arc_id,
                arc_number=arc.arc_number,
                title=arc.arc_synopsis,
                primary_world_line_ids=primary_line_ids,
                hidden_world_line_ids=hidden_line_ids,
                major_gap_ids=major_gap_ids,
                reveal_ladder=reveal_ladder,
                reader_cognition_trajectory=reader_trajectory,
                medium_term_payoff_promises=(
                    ["一个隐藏线从 hinted 走到 partially_revealed"]
                    if has_homeworld_crisis
                    else []
                ),
                long_term_payoff_promises=(
                    ["殖民地成为反攻母星基础"] if has_homeworld_crisis else []
                ),
                arc_exit_objective_state=(
                    "殖民地成为反攻母星基础" if has_homeworld_crisis else arc.arc_synopsis
                ),
                arc_exit_reader_state=(
                    "partially_revealed" if has_homeworld_crisis else "aware"
                ),
            )
        )

        min_chapter = min(plan.chapter_number for plan in ordered_plans)
        max_chapter = max(plan.chapter_number for plan in ordered_plans)
        band_size = max(1, int(detailed_band_size or 1))
        band_start = max(min_chapter, int(activation_chapter or min_chapter))
        band_end = min(max_chapter, band_start + band_size - 1)
        band_id = f"band:{band_start}:{band_end}"
        active_band = [
            plan for plan in ordered_plans if band_start <= plan.chapter_number <= band_end
        ]
        repo.save_band_contract(
            BandWorldContract(
                contract_id=f"band_world_contract_{arc.id}_{band_start}_{band_end}",
                project_id=project_id,
                arc_id=arc_id,
                band_id=band_id,
                chapter_start=band_start,
                chapter_end=band_end,
                foreground_world_line_ids=primary_line_ids,
                hidden_world_line_ids=hidden_line_ids,
                required_hints=(
                    ["乱码通讯", "父亲旧部呼号"] if has_homeworld_crisis else []
                ),
                gap_transitions=(
                    {"gap_homeworld_siege": "hidden -> hinted"}
                    if has_homeworld_crisis
                    else {}
                ),
                payoff_commitments=(
                    ["本 band 只给 mystery hint，不做 full reveal"]
                    if has_homeworld_crisis
                    else []
                ),
                band_exit_reader_state="hinted" if has_homeworld_crisis else "aware",
                band_exit_hidden_line_state=(
                    "母星通讯被进一步切断" if has_homeworld_crisis else ""
                ),
            )
        )

        for plan in active_band:
            is_homeworld_hint_chapter = has_homeworld_crisis and plan.chapter_number == 23
            repo.save_chapter_intent(
                ChapterWorldDeltaIntent(
                    intent_id=f"chapter_{plan.chapter_number}_world_intent",
                    project_id=project_id,
                    chapter_plan_id=plan.id,
                    chapter_number=plan.chapter_number,
                    visible_delta_intents=(
                        ["殖民地防线修复"]
                        if is_homeworld_hint_chapter
                        else load_goals_json(plan.goals_json)[:1]
                    ),
                    offscreen_delta_intents=(
                        ["敌方切断第三通讯阵列"] if is_homeworld_hint_chapter else []
                    ),
                    hint_delta_intents=(
                        ["乱码通讯", "父亲旧部呼号"] if is_homeworld_hint_chapter else []
                    ),
                    knowledge_delta_intents=(
                        ["主角进入 suspected 状态"] if is_homeworld_hint_chapter else []
                    ),
                    reader_experience_intents=(
                        ["mystery hint"] if is_homeworld_hint_chapter else []
                    ),
                    must_not_reveal=(
                        ["father_sieged"]
                        if has_homeworld_crisis and plan.chapter_number < 25
                        else []
                    ),
                    delta_sources=(
                        ["faction_action", "information_spread"]
                        if is_homeworld_hint_chapter
                        else []
                    ),
                    expected_observer_state_changes=(
                        {
                            "reader": "hidden -> hinted",
                            "protagonist": "unknown -> suspected",
                        }
                        if is_homeworld_hint_chapter
                        else {}
                    ),
                )
            )

    def _build_audience_calibration_profile(
        self,
        *,
        session: Session,
        project_id: str,
    ) -> AudienceCalibrationProfile:
        trends = _load_long_window_audience_trend_views(session, project_id)
        profile = AudienceCalibrationProfile()
        for trend in trends:
            strong_signal = trend.current_level in {"confirmed", "watchlist"} or trend.current_score >= 0.28
            if trend.signal_type == "pacing" and strong_signal and trend.trend_type != "falling":
                profile.boost_reward_density = True
            elif trend.signal_type in {"confusion", "risk"} and strong_signal:
                profile.clarify_rule_legibility = True
            elif trend.signal_type in {"character_heat", "relationship_interest"} and strong_signal and trend.trend_type != "falling":
                profile.protect_character_heat = True
            elif trend.signal_type == "prediction" and strong_signal:
                profile.hold_managed_ambiguity = True
        return profile

    def _derive_band_delight_schedule(
        self,
        *,
        band_id: str,
        chapter_start: int,
        chapter_end: int,
        structure: ArcStructureDraftData,
        active_band: list[ChapterPlan],
        calibration: AudienceCalibrationProfile | None = None,
    ) -> BandDelightSchedule:
        calibration = calibration or AudienceCalibrationProfile()
        band_length = max(1, chapter_end - chapter_start + 1)
        stall_guard_max_gap = max(1, min(2, band_length - 1 if band_length > 1 else 1))
        scheduled_rewards: list[BandRewardItem] = []
        curiosity_beats: list[CuriosityBeat] = []
        ambiguity_payoffs: list[AmbiguityPayoff] = []
        macro_by_category = {
            item.category: item
            for item in structure.arc_payoff_map.macro_payoffs
            if item.category not in {"emotion"}
        }

        def chapter_for(slot: str) -> int:
            if slot == "early":
                return chapter_start
            if slot == "late":
                return chapter_end
            if slot == "mid":
                return chapter_start + max(0, (band_length - 1) // 2)
            return chapter_start

        def template_for(category: str, fallback_index: int) -> str:
            macro = macro_by_category.get(category)
            if macro is not None and macro.template_id:
                return macro.template_id
            template_candidates = trope_templates_by_category(category)
            if template_candidates:
                return template_candidates[fallback_index % len(template_candidates)].template_id
            return TROPE_TEMPLATE_LIBRARY[fallback_index % len(TROPE_TEMPLATE_LIBRARY)].template_id

        blueprint: list[tuple[str, str, str]] = [
            ("power", "early", "micro_progress_power"),
            ("social", "mid" if band_length >= 2 else "early", "social_dominance"),
            ("mystery", "late", "mystery_clue_or_reveal"),
        ]
        if calibration.boost_reward_density and band_length >= 3:
            blueprint.insert(1, ("power", "mid" if band_length >= 4 else "late", "micro_progress_power"))
        pleasures_text = " ".join(structure.reader_promise.core_pleasures)
        if band_length >= 3:
            blueprint.insert(1, ("power", "mid", "micro_progress_power"))
        if any(item.category == "justice" for item in structure.arc_payoff_map.macro_payoffs):
            blueprint.append(("justice", "late", "justice_snap"))
        elif any(item.category == "emotion" for item in structure.arc_payoff_map.macro_payoffs) or any(
            token in pleasures_text for token in ("角色", "关系", "情感")
        ):
            blueprint.append(("emotion", "late", "emotion_knife"))
        elif calibration.protect_character_heat:
            blueprint.append(("emotion", "late", "emotion_knife"))

        for index, (category, slot, intent) in enumerate(blueprint):
            chapter_hint = chapter_for(slot)
            scheduled_rewards.append(
                BandRewardItem(
                    chapter_hint=chapter_hint,
                    category=category,
                    template_id=template_for(category, index),
                    intent=intent,
                )
            )

        reward_chapters = sorted(set(item.chapter_hint for item in scheduled_rewards))
        cursor = chapter_start
        while cursor <= chapter_end:
            if reward_chapters and any(abs(cursor - chapter) <= stall_guard_max_gap for chapter in reward_chapters):
                cursor += 1
                continue
            scheduled_rewards.append(
                BandRewardItem(
                    chapter_hint=cursor,
                    category="power" if cursor < chapter_end else "mystery",
                    template_id=template_for("power" if cursor < chapter_end else "mystery", cursor - chapter_start),
                    intent="stall_guard_cover",
                )
            )
            reward_chapters = sorted(set(item.chapter_hint for item in scheduled_rewards))
            cursor += 1

        first_question = (
            active_band[0].one_line
            if active_band and active_band[0].one_line
            else (structure.key_beats[0] if structure.key_beats else "当前局面真正的问题是什么")
        )
        opened_question = (
            structure.key_beats[1]
            if len(structure.key_beats) > 1
            else "当前危机背后还有谁在推动局势"
        )
        curiosity_beats.append(
            CuriosityBeat(
                chapter_hint=chapter_start,
                question_open=first_question,
                question_resolve=(
                    structure.key_beats[0]
                    if structure.key_beats
                    else "本 band 至少确认一条线索不是偶然"
                ),
                escalated_question=opened_question,
            )
        )
        if band_length >= 3:
            curiosity_beats.append(
                CuriosityBeat(
                    chapter_hint=chapter_for("late"),
                    question_open=opened_question,
                    question_resolve="确认一个阶段性真相或代价",
                    escalated_question=(
                        structure.key_beats[2]
                        if len(structure.key_beats) > 2
                        else "真正的规则限制究竟是什么"
                    ),
                )
            )
        if calibration.clarify_rule_legibility:
            curiosity_beats.append(
                CuriosityBeat(
                    chapter_hint=chapter_for("mid"),
                    question_open="这条规则到底限制了什么",
                    question_resolve="明确一条正在生效的代价、边界或因果限制",
                    escalated_question="如果继续逼近真相，会触发什么新的代价",
                )
            )

        ambiguity_constraints = [
            item for item in structure.arc_payoff_map.ambiguity_constraints if str(item).strip()
        ]
        ambiguity_mode = (structure.reader_promise.ambiguity_mode or "managed").strip()
        if calibration.hold_managed_ambiguity and ambiguity_mode == "stable":
            ambiguity_mode = "managed"
        ambiguity_payoffs.append(
            AmbiguityPayoff(
                chapter_hint=chapter_start,
                payoff_type="confirmation",
                summary="先确认一条小事实，证明叙事并非随机失真。",
                constraint_ref=ambiguity_constraints[0] if ambiguity_constraints else "",
            )
        )
        ambiguity_payoffs.append(
            AmbiguityPayoff(
                chapter_hint=chapter_for("mid"),
                payoff_type="constraint",
                summary="明确这一段不允许被打破的规则边界或代价。",
                constraint_ref=ambiguity_constraints[0] if ambiguity_constraints else "",
            )
        )
        ambiguity_payoffs.append(
            AmbiguityPayoff(
                chapter_hint=chapter_end,
                payoff_type="reversal",
                summary=(
                    "在不破坏规则边界的前提下给出一次认知反转。"
                    if ambiguity_mode == "high"
                    else "预置或兑现一次受控认知转向，但不能破坏已确认事实。"
                ),
                constraint_ref=(
                    ambiguity_constraints[1]
                    if len(ambiguity_constraints) > 1
                    else (ambiguity_constraints[0] if ambiguity_constraints else "")
                ),
            )
        )

        immersion_anchor_chapter = chapter_for("mid") if band_length > 1 else chapter_start

        return BandDelightSchedule(
            band_id=band_id,
            chapter_start=chapter_start,
            chapter_end=chapter_end,
            scheduled_rewards=sorted(
                scheduled_rewards,
                key=lambda item: (item.chapter_hint, item.category, item.template_id),
            ),
            immersion_anchor_scene_goal=(
                f"第{immersion_anchor_chapter}章必须给出一个可感知现场的沉浸 anchor scene："
                + (
                    structure.key_beats[0]
                    if structure.key_beats
                    else (active_band[0].one_line if active_band else "让读者进入现场")
                )
            ),
            stall_guard_max_gap=stall_guard_max_gap,
            curiosity_beats=curiosity_beats,
            ambiguity_payoffs=ambiguity_payoffs,
        )

    def _derive_chapter_experience_plan(
        self,
        *,
        chapter_number: int,
        structure: ArcStructureDraftData,
        schedule: BandDelightSchedule,
        chapter_plan: ChapterPlan,
        calibration: AudienceCalibrationProfile | None = None,
    ) -> ChapterExperiencePlan:
        calibration = calibration or AudienceCalibrationProfile()
        chapter_rewards = [
            item for item in schedule.scheduled_rewards if item.chapter_hint == chapter_number
        ]
        goals = load_goals_json(chapter_plan.goals_json)
        reward_tags = [item.category for item in chapter_rewards]
        hook_type = "cliffhanger_question"
        if "power" in reward_tags:
            hook_type = "advantage_reveal"
        elif "emotion" in reward_tags:
            hook_type = "emotional_knife"
        elif "justice" in reward_tags:
            hook_type = "retribution_snap"
        elif "social" in reward_tags:
            hook_type = "status_flip"
        immersion_anchors = [
            schedule.immersion_anchor_scene_goal if chapter_number == schedule.chapter_start + max(0, (schedule.chapter_end - schedule.chapter_start) // 2) else "",
            chapter_plan.one_line,
            *goals[:2],
        ]
        progress_markers = (
            goals[:3]
            or [chapter_plan.one_line or chapter_plan.title]
        )
        if any(item.intent == "micro_progress_power" for item in chapter_rewards):
            progress_markers = [*(progress_markers[:2]), "给主角一个可验证的微进展/实力兑现"]
        if any(item.intent == "social_dominance" for item in chapter_rewards):
            progress_markers = [*progress_markers[:2], "让社会地位或公开场面出现明确逆转"]
        if any(item.intent == "mystery_clue_or_reveal" for item in chapter_rewards):
            progress_markers = [*progress_markers[:2], "给出一条真实可追踪的新线索或半揭晓"]
        chapter_curiosity = next(
            (item for item in schedule.curiosity_beats if item.chapter_hint == chapter_number),
            None,
        )
        question_hook = (
            chapter_curiosity.question_open
            if chapter_curiosity is not None
            else (chapter_plan.one_line or chapter_plan.title)
        )
        question_resolution = (
            chapter_curiosity.question_resolve
            if chapter_curiosity is not None
            else (
                "至少解决一个小问题，并换来更大的问题"
                if "mystery" in reward_tags
                else "至少兑现一个可验证的局面变化"
            )
        )
        rule_anchors = [
            item.summary
            for item in structure.arc_payoff_map.revelation_layers[:2]
            if str(item.summary).strip()
        ]
        rule_anchors.extend(
            item
            for item in structure.arc_payoff_map.ambiguity_constraints[:2]
            if str(item).strip()
        )
        if structure.reader_promise.world_legibility_target:
            rule_anchors.append(structure.reader_promise.world_legibility_target)
        if calibration.clarify_rule_legibility:
            rule_anchors.append("把当前冲突涉及的规则、代价与因果关系讲清楚。")
            progress_markers.append("明确一条正在生效的规则边界或代价")
        minimum_progress_channels = ["event", "thread"]
        if "power" in reward_tags or "justice" in reward_tags:
            minimum_progress_channels.append("state")
        if "social" in reward_tags:
            minimum_progress_channels.append("status")
        if "emotion" in reward_tags:
            minimum_progress_channels.append("relationship")
        if "mystery" in reward_tags:
            minimum_progress_channels.append("rule")
        if calibration.clarify_rule_legibility and "rule" not in minimum_progress_channels:
            minimum_progress_channels.append("rule")
        if calibration.protect_character_heat and "relationship" not in minimum_progress_channels:
            minimum_progress_channels.append("relationship")
        relationship_or_status_shift = ""
        if "social" in reward_tags:
            relationship_or_status_shift = "本章至少要让公开地位、评价或权力排序发生一次明确变化。"
        elif "emotion" in reward_tags:
            relationship_or_status_shift = "本章至少要让角色关系或情感站位发生一次明确变化。"
        elif calibration.protect_character_heat:
            relationship_or_status_shift = "给当前高热角色一处可记忆的互动、态度变化或存在感强化。"
        return ChapterExperiencePlan(
            planned_reward_tags=reward_tags,
            selected_template_ids=[item.template_id for item in chapter_rewards],
            hook_type=hook_type,
            question_hook=question_hook,
            question_resolution=question_resolution,
            immersion_anchors=[str(item).strip() for item in immersion_anchors if str(item).strip()],
            progress_markers=[str(item).strip() for item in progress_markers if str(item).strip()],
            rule_anchors=[str(item).strip() for item in rule_anchors if str(item).strip()],
            relationship_or_status_shift=relationship_or_status_shift,
            minimum_progress_channels=list(dict.fromkeys(minimum_progress_channels)),
        )

    def _resolve_envelope(
        self,
        *,
        chapter_plans: list[ChapterPlan],
        total_chapters: int,
        policy: ArcPolicyTier,
        base_target_size: int,
        base_soft_min: int,
        base_soft_max: int,
        structure: ArcStructureDraftData,
        provisional_band: list[ChapterPlan],
        band_id: str,
        preview: ProvisionalBandPreview | None,
        rehearsal: ScenarioRehearsalReport | None = None,
    ) -> ArcEnvelopeResolution:
        evidence = [
            f"policy={policy.name}",
            f"base_target={base_target_size}",
            f"scenario_band={len(provisional_band)}",
        ]
        expansion_signals: list[str] = []
        compression_signals: list[str] = []
        if len(structure.hotspot_candidates) >= 3:
            expansion_signals.append("热点候选密度高")
        if len(structure.key_beats) >= 4 and total_chapters >= 150:
            expansion_signals.append("中层结构显示多段推进")
        if len(structure.compression_candidates) >= 2:
            compression_signals.append("中段存在可压缩片段")
        if len(provisional_band) <= max(4, base_target_size // 3):
            compression_signals.append("近端 rehearsal band 较短")
        if rehearsal is not None:
            evidence.extend(
                [
                    f"scenario_rehearsal={rehearsal.recommendation.value}",
                    f"scenario_risk_count={len(rehearsal.risk_findings)}",
                    f"scenario_patch_count={len(rehearsal.required_plan_patches)}",
                ]
            )
            if rehearsal.recommendation == ScenarioRehearsalRecommendation.PASS:
                expansion_signals.append("scenario rehearsal 通过")
            elif rehearsal.recommendation == ScenarioRehearsalRecommendation.PATCH:
                evidence.append("scenario rehearsal 要求 plan patch")
            elif rehearsal.recommendation in {
                ScenarioRehearsalRecommendation.REPLAN,
                ScenarioRehearsalRecommendation.BLOCK,
            }:
                compression_signals.append("scenario rehearsal 暴露高风险结构问题")
            for finding in rehearsal.risk_findings:
                if finding.severity == "fail":
                    compression_signals.append(f"scenario blocker: {finding.risk_type}")
        if preview is not None:
            evidence.extend(
                [
                    f"provisional_verdict={preview.aggregate_verdict}",
                    f"provisional_char_count={preview.total_char_count}",
                    f"provisional_issue_count={preview.issue_count}",
                ]
            )
            if preview.aggregate_verdict == "pass":
                expansion_signals.append("provisional band 运行顺滑")
            elif preview.aggregate_verdict == "warn":
                evidence.append("provisional band 有轻微审查警告")
            else:
                compression_signals.append("provisional band 暴露不稳定点")
            if preview.failure_count:
                compression_signals.append("provisional band 出现生成失败")

        recommendation = "keep"
        resolved_target_size = base_target_size
        if len(expansion_signals) > len(compression_signals):
            recommendation = "expand"
            resolved_target_size = min(
                base_soft_max,
                max(base_target_size + 2, int(round(base_target_size * 1.2))),
            )
        elif len(compression_signals) > len(expansion_signals):
            recommendation = "compress"
            resolved_target_size = max(
                base_soft_min,
                min(base_target_size - 2, int(round(base_target_size * 0.85))),
            )

        analysis_payload: dict[str, Any] | None = None
        if self.director is not None:
            try:
                analysis_payload = self.director.analyze_arc_envelope(
                    total_chapters=total_chapters,
                    policy_tier=policy.name,
                    base_target_size=base_target_size,
                    base_soft_min=base_soft_min,
                    base_soft_max=base_soft_max,
                    structure_draft={
                        "phase_layout": structure.phase_layout,
                        "key_beats": structure.key_beats,
                        "thread_priorities": structure.thread_priorities,
                        "hotspot_candidates": structure.hotspot_candidates,
                        "compression_candidates": structure.compression_candidates,
                    },
                    provisional_band=[
                        {
                            "chapter_number": plan.chapter_number,
                            "title": plan.title,
                            "one_line": plan.one_line,
                            "goals": load_goals_json(plan.goals_json),
                        }
                        for plan in provisional_band
                    ],
                )
            except Exception:
                analysis_payload = None
        if isinstance(analysis_payload, dict):
            suggestion = str(analysis_payload.get("recommendation") or recommendation).strip().lower()
            if suggestion in {"keep", "expand", "compress"}:
                recommendation = suggestion
            expansion_signals = [
                str(item) for item in (analysis_payload.get("expansion_signals") or expansion_signals)
            ]
            compression_signals = [
                str(item) for item in (analysis_payload.get("compression_signals") or compression_signals)
            ]
            evidence = [str(item) for item in (analysis_payload.get("evidence") or evidence)]
            resolved_target_size = _clamp_int(
                int(analysis_payload.get("suggested_target") or resolved_target_size),
                base_soft_min,
                base_soft_max,
            )
            confidence = max(0.2, min(0.95, float(analysis_payload.get("confidence") or 0.65)))
        else:
            confidence = 0.65 if recommendation == "keep" else 0.72

        detailed_band_size = _clamp_int(resolved_target_size * 0.40, 4, 12)
        frozen_zone_size = _clamp_int(detailed_band_size * 0.35, 2, 4)
        resolved_soft_min = max(1, _clamp_int(resolved_target_size * policy.soft_min_ratio, 1, base_soft_max))
        resolved_soft_max = max(
            resolved_target_size,
            _clamp_int(resolved_target_size * policy.soft_max_ratio, resolved_target_size, max(base_soft_max, resolved_target_size)),
        )
        return ArcEnvelopeResolution(
            base_target_size=base_target_size,
            base_soft_min=base_soft_min,
            base_soft_max=base_soft_max,
            resolved_target_size=resolved_target_size,
            resolved_soft_min=resolved_soft_min,
            resolved_soft_max=resolved_soft_max,
            detailed_band_size=detailed_band_size,
            frozen_zone_size=frozen_zone_size,
            recommendation=recommendation,
            evidence=evidence,
            expansion_signals=expansion_signals,
            compression_signals=compression_signals,
            confidence=confidence,
            based_on_band_id=band_id,
            source_policy_tier=policy.name,
        )

    def _execute_provisional_band(
        self,
        *,
        session: Session,
        project_id: str,
        arc_id: str,
        band_id: str,
        chapter_plans: list[ChapterPlan],
    ) -> ProvisionalBandPreview | None:
        if self.provisional_executor is None or not chapter_plans:
            return None
        try:
            preview = self.provisional_executor(
                session=session,
                project_id=project_id,
                arc_id=arc_id,
                band_id=band_id,
                chapter_plans=chapter_plans,
            )
        except Exception:  # noqa: BLE001
            logger.warning("Provisional band execution failed for %s/%s.", project_id, band_id, exc_info=True)
            return None
        if preview is None:
            return None
        if isinstance(preview, ProvisionalBandPreview):
            return preview
        if isinstance(preview, dict):
            return ProvisionalBandPreview(
                band_id=str(preview.get("band_id") or band_id),
                artifact_path=str(preview.get("artifact_path") or ""),
                aggregate_verdict=str(preview.get("aggregate_verdict") or "warn"),
                preview_chapter_count=int(preview.get("preview_chapter_count") or 0),
                total_char_count=int(preview.get("total_char_count") or 0),
                issue_count=int(preview.get("issue_count") or 0),
                failure_count=int(preview.get("failure_count") or 0),
                chapter_numbers=[int(item) for item in (preview.get("chapter_numbers") or [])],
                summary_lines=[str(item) for item in (preview.get("summary_lines") or [])],
            )
        return None
