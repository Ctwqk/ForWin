from __future__ import annotations

from dataclasses import dataclass
import json
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
    ChapterPlan,
    Project,
    ProvisionalPromotionRecord,
    SignalWindowAggregate,
    new_id,
)
from forwin.experience.arc_experience_planner import ArcExperiencePlanningService
from forwin.experience.band_scheduler import BandExperienceScheduler
from forwin.experience.chapter_planner import ChapterExperiencePlanner
from forwin.experience.persistence import ExperiencePersistence
from forwin.experience.service import (
    AudienceCalibrationProfile,
    ExperiencePlanningService,
    load_long_window_audience_trends,
)
from forwin.experience.types import ArcExperienceBundle
from forwin.planning.arc_activation_service import ArcActivationService
from forwin.planning.arc_envelope_resolver import (
    ArcEnvelopeResolver,
    BaseEnvelopeContext,
)
from forwin.planning.arc_structure_service import (
    ArcStructureDraftData as CoreArcStructureDraftData,
    ArcStructurePlanningResult,
    ArcStructurePlanningService,
)
from forwin.planning.band_plan_service import BandPlanningRequest, BandPlanService
from forwin.planning.provisional_preview_service import ProvisionalPreviewService
from forwin.planning.scenario_rehearsal_service import ScenarioRehearsalService
from forwin.planning.world_contract_service import WorldContractPlanningService
from forwin.protocol.experience import (
    ArcPayoffMap,
    BandDelightSchedule,
    ChapterExperiencePlan,
    ReaderPromise,
)
from forwin.protocol.scenario_rehearsal import ScenarioRehearsalReport
from forwin.subworld_manager import SubWorldManager


def _clamp_int(value: float | int, lower: int, upper: int) -> int:
    return max(lower, min(int(round(value)), upper))


def _coerce_unit_float(value: object, *, default: float) -> float:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"high", "strong", "certain", "confident"}:
            return 0.85
        if normalized in {"medium", "moderate", "managed"}:
            return 0.65
        if normalized in {"low", "weak", "uncertain"}:
            return 0.35
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


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


def _to_core_structure(structure: ArcStructureDraftData | CoreArcStructureDraftData) -> CoreArcStructureDraftData:
    if isinstance(structure, CoreArcStructureDraftData):
        return structure
    return CoreArcStructureDraftData(
        phase_layout=list(structure.phase_layout),
        key_beats=list(structure.key_beats),
        thread_priorities=list(structure.thread_priorities),
        hotspot_candidates=list(structure.hotspot_candidates),
        compression_candidates=list(structure.compression_candidates),
    )


def _experience_from_compat_structure(structure: ArcStructureDraftData) -> ArcExperienceBundle:
    return ArcExperienceBundle(
        reader_promise=structure.reader_promise,
        arc_payoff_map=structure.arc_payoff_map,
    )


def _compat_structure(
    structure: CoreArcStructureDraftData,
    arc_experience: ArcExperienceBundle,
) -> ArcStructureDraftData:
    return ArcStructureDraftData(
        phase_layout=list(structure.phase_layout),
        key_beats=list(structure.key_beats),
        thread_priorities=list(structure.thread_priorities),
        hotspot_candidates=list(structure.hotspot_candidates),
        compression_candidates=list(structure.compression_candidates),
        reader_promise=arc_experience.reader_promise,
        arc_payoff_map=arc_experience.arc_payoff_map,
    )


@dataclass(slots=True)
class PlanningServices:
    arc_activation: ArcActivationService
    arc_envelope_resolver: ArcEnvelopeResolver
    arc_structure: ArcStructurePlanningService
    arc_experience: ArcExperiencePlanningService
    experience: ExperiencePlanningService
    experience_persistence: ExperiencePersistence
    band_scheduler: BandExperienceScheduler
    chapter_planner: ChapterExperiencePlanner
    band_plan: BandPlanService
    world_contracts: WorldContractPlanningService
    scenario_rehearsal: ScenarioRehearsalService
    provisional_preview: ProvisionalPreviewService

    @classmethod
    def from_legacy_args(
        cls,
        *,
        director: ArcDirector | None = None,
        provisional_executor: Any | None = None,
        subworld_manager: SubWorldManager | None = None,
        legacy_preview_enabled: bool = False,
        scenario_progress_callback: Any | None = None,
    ) -> "PlanningServices":
        resolved_subworld_manager = subworld_manager or SubWorldManager(director=director)
        world_contracts = WorldContractPlanningService()
        experience = ExperiencePlanningService()
        persistence = ExperiencePersistence()
        band_scheduler = BandExperienceScheduler()
        chapter_planner = ChapterExperiencePlanner()
        return cls(
            arc_activation=ArcActivationService(),
            arc_envelope_resolver=ArcEnvelopeResolver(director=director),
            arc_structure=ArcStructurePlanningService(director=director),
            arc_experience=ArcExperiencePlanningService(),
            experience=experience,
            experience_persistence=persistence,
            band_scheduler=band_scheduler,
            chapter_planner=chapter_planner,
            band_plan=BandPlanService(
                subworld_manager=resolved_subworld_manager,
                world_contract_service=world_contracts,
                experience_service=experience,
                scheduler=band_scheduler,
                chapter_planner=chapter_planner,
                persistence=persistence,
            ),
            world_contracts=world_contracts,
            scenario_rehearsal=ScenarioRehearsalService(
                director=director,
                progress_callback=scenario_progress_callback,
            ),
            provisional_preview=ProvisionalPreviewService(
                provisional_executor=provisional_executor,
                legacy_preview_enabled=legacy_preview_enabled,
            ),
        )


@dataclass(slots=True)
class ArcResolutionPlanningState:
    active_arc: ArcPlanVersion
    chapter_plans: list[ChapterPlan]
    base_context: BaseEnvelopeContext
    structure_result: ArcStructurePlanningResult
    arc_experience: ArcExperienceBundle
    audience_trends: list[str]


class ArcEnvelopeManager:
    def __init__(
        self,
        *,
        director: ArcDirector | None = None,
        provisional_executor: Any | None = None,
        subworld_manager: SubWorldManager | None = None,
        legacy_preview_enabled: bool = False,
        scenario_progress_callback: Any | None = None,
        planning_services: PlanningServices | None = None,
    ) -> None:
        self.director = director
        self.provisional_executor = provisional_executor
        self.subworld_manager = subworld_manager or SubWorldManager(director=director)
        self.legacy_preview_enabled = legacy_preview_enabled
        self.scenario_progress_callback = scenario_progress_callback
        self.services = planning_services or PlanningServices.from_legacy_args(
            director=director,
            provisional_executor=provisional_executor,
            subworld_manager=self.subworld_manager,
            legacy_preview_enabled=legacy_preview_enabled,
            scenario_progress_callback=scenario_progress_callback,
        )

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

    def _load_active_arc(
        self,
        *,
        session: Session,
        project_id: str,
    ) -> ArcPlanVersion | None:
        return session.execute(
            select(ArcPlanVersion)
            .where(
                ArcPlanVersion.project_id == project_id,
                ArcPlanVersion.status == "active",
            )
            .order_by(ArcPlanVersion.version.desc())
            .limit(1)
        ).scalar_one_or_none()

    def _load_arc_chapter_plans(
        self,
        *,
        session: Session,
        arc_id: str,
    ) -> list[ChapterPlan]:
        return session.execute(
            select(ChapterPlan)
            .where(ChapterPlan.arc_plan_id == arc_id)
            .order_by(ChapterPlan.chapter_number.asc())
        ).scalars().all()

    def _build_arc_resolution_state(
        self,
        *,
        session: Session,
        project: Project,
        active_arc: ArcPlanVersion,
        chapter_plans: list[ChapterPlan],
        activation_chapter: int,
    ) -> ArcResolutionPlanningState:
        base_context = self.services.arc_envelope_resolver.build_base_context(
            session=session,
            project=project,
            active_arc=active_arc,
            chapter_plans=chapter_plans,
            activation_chapter=activation_chapter,
        )
        audience_trends = load_long_window_audience_trends(session, project.id)
        structure_result = self.services.arc_structure.ensure_structure(
            session=session,
            project=project,
            active_arc=active_arc,
            total_chapters=base_context.total_chapters,
            policy=base_context.policy,
            base_target_size=base_context.base_target_size,
            chapter_plans=chapter_plans,
            audience_trends=audience_trends,
        )
        arc_experience = self.services.arc_experience.plan_arc_experience(
            project=project,
            structure=structure_result.structure,
            chapter_plans=chapter_plans,
            audience_trends=audience_trends,
            drafted_payload=structure_result.experience_payload,
        )
        self.services.experience_persistence.persist_arc_experience(
            structure_row=structure_result.row,
            arc_experience=arc_experience,
        )
        session.add(structure_result.row)
        session.flush()
        return ArcResolutionPlanningState(
            active_arc=active_arc,
            chapter_plans=chapter_plans,
            base_context=base_context,
            structure_result=structure_result,
            arc_experience=arc_experience,
            audience_trends=audience_trends,
        )

    def _refresh_state_after_rehearsal_replan(
        self,
        *,
        session: Session,
        project: Project,
        state: ArcResolutionPlanningState,
        rehearsal: ScenarioRehearsalReport,
        activation_chapter: int,
    ) -> ArcResolutionPlanningState:
        if not rehearsal.arc_id or rehearsal.arc_id == state.active_arc.id:
            return state
        replanned_arc = session.get(ArcPlanVersion, rehearsal.arc_id)
        if replanned_arc is None:
            return state
        chapter_plans = self._load_arc_chapter_plans(session=session, arc_id=replanned_arc.id)
        return self._build_arc_resolution_state(
            session=session,
            project=project,
            active_arc=replanned_arc,
            chapter_plans=chapter_plans,
            activation_chapter=activation_chapter,
        )

    def _ensure_current_band_plan_for_state(
        self,
        *,
        session: Session,
        project_id: str,
        state: ArcResolutionPlanningState,
        activation_chapter: int,
        detailed_band_size: int,
    ) -> None:
        self.services.band_plan.ensure_current_band_plan(
            session=session,
            request=BandPlanningRequest(
                project_id=project_id,
                arc_id=state.active_arc.id,
                activation_chapter=activation_chapter,
                detailed_band_size=detailed_band_size,
                chapter_plans=state.chapter_plans,
                structure=state.structure_result.structure,
                arc_experience=state.arc_experience,
            ),
        )

    def _resolve_new_arc_envelope(
        self,
        *,
        session: Session,
        project: Project,
        project_id: str,
        state: ArcResolutionPlanningState,
        activation_chapter: int,
    ) -> ArcEnvelope:
        self.services.world_contracts.ensure_for_arc_band(
            session=session,
            project_id=project_id,
            arc_id=state.active_arc.id,
            chapter_plans=state.chapter_plans,
            activation_chapter=activation_chapter,
            detailed_band_size=state.base_context.provisional_band_size,
        )
        rehearsal = self.services.scenario_rehearsal.run_for_band(
            session=session,
            project_id=project_id,
            arc_id=state.active_arc.id,
            band_id=state.base_context.provisional_window.band_id,
            chapter_plans=state.base_context.provisional_window.active_band,
        ).report
        state = self._refresh_state_after_rehearsal_replan(
            session=session,
            project=project,
            state=state,
            rehearsal=rehearsal,
            activation_chapter=activation_chapter,
        )
        preview = self.services.provisional_preview.execute(
            session=session,
            project_id=project_id,
            arc_id=state.active_arc.id,
            band_id=state.base_context.provisional_window.band_id,
            chapter_plans=state.base_context.provisional_window.active_band,
        )
        envelope = self.services.arc_envelope_resolver.ensure_resolution(
            session=session,
            project=project,
            active_arc=state.active_arc,
            chapter_plans=state.chapter_plans,
            activation_chapter=activation_chapter,
            structure=state.structure_result.structure,
            rehearsal_report=rehearsal,
            preview=preview,
            base_context=state.base_context,
        )
        self.services.provisional_preview.persist_execution(
            session=session,
            project_id=project_id,
            arc_id=state.active_arc.id,
            preview=preview,
        )
        self._ensure_current_band_plan_for_state(
            session=session,
            project_id=project_id,
            state=state,
            activation_chapter=activation_chapter,
            detailed_band_size=envelope.detailed_band_size,
        )
        session.flush()
        return envelope

    def ensure_active_arc_resolution(
        self,
        *,
        session: Session,
        project_id: str,
        activation_chapter: int = 1,
    ) -> ArcEnvelope | None:
        self.services.arc_activation.activate_for_chapter(
            session=session,
            project_id=project_id,
            chapter_number=activation_chapter,
        )
        active_arc = self._load_active_arc(session=session, project_id=project_id)
        if active_arc is None:
            return None

        self.subworld_manager.ensure_registry(session, project_id)
        self.subworld_manager.ensure_initial_registry_for_active_arc(
            session=session,
            project_id=project_id,
        )

        chapter_plans = self._load_arc_chapter_plans(session=session, arc_id=active_arc.id)
        project = session.get(Project, project_id)
        if project is None:
            return None

        state = self._build_arc_resolution_state(
            session=session,
            project=project,
            active_arc=active_arc,
            chapter_plans=chapter_plans,
            activation_chapter=activation_chapter,
        )
        existing = self.services.arc_envelope_resolver.get_existing_envelope(
            session=session,
            active_arc=state.active_arc,
            activation_chapter=activation_chapter,
        )

        if existing is not None:
            self._ensure_current_band_plan_for_state(
                session=session,
                project_id=project_id,
                state=state,
                activation_chapter=activation_chapter,
                detailed_band_size=existing.detailed_band_size,
            )
            return existing

        return self._resolve_new_arc_envelope(
            session=session,
            project=project,
            project_id=project_id,
            state=state,
            activation_chapter=activation_chapter,
        )

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
        audience_trends = load_long_window_audience_trends(session, project.id)
        structure, drafted_payload = self.services.arc_structure.build_structure_draft(
            project=project,
            total_chapters=total_chapters,
            chapter_plans=chapter_plans,
            policy=policy,
            base_target_size=base_target_size,
            audience_trends=audience_trends,
        )
        arc_experience = self.services.arc_experience.plan_arc_experience(
            project=project,
            structure=structure,
            chapter_plans=chapter_plans,
            audience_trends=audience_trends,
            drafted_payload=drafted_payload,
        )
        return _compat_structure(structure, arc_experience)

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
        self.services.band_plan.ensure_current_band_plan(
            session=session,
            request=BandPlanningRequest(
                project_id=project_id,
                arc_id=arc_id,
                activation_chapter=activation_chapter,
                detailed_band_size=detailed_band_size,
                chapter_plans=chapter_plans,
                structure=_to_core_structure(structure),
                arc_experience=_experience_from_compat_structure(structure),
            ),
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
        self.services.world_contracts.ensure_for_arc_band(
            session=session,
            project_id=project_id,
            arc_id=arc_id,
            chapter_plans=chapter_plans,
            activation_chapter=activation_chapter,
            detailed_band_size=detailed_band_size,
        )

    def _build_audience_calibration_profile(
        self,
        *,
        session: Session,
        project_id: str,
    ) -> AudienceCalibrationProfile:
        return self.services.experience.build_audience_calibration_profile(
            session=session,
            project_id=project_id,
        )

    def _derive_band_delight_schedule(
        self,
        *,
        band_id: str,
        chapter_start: int,
        chapter_end: int,
        structure: ArcStructureDraftData,
        active_band: list[ChapterPlan],
        calibration: AudienceCalibrationProfile | None = None,
        cost_ceiling: int = 3,
    ) -> BandDelightSchedule:
        return self.services.band_scheduler.derive_band_delight_schedule(
            band_id=band_id,
            chapter_start=chapter_start,
            chapter_end=chapter_end,
            structure=_to_core_structure(structure),
            arc_experience=_experience_from_compat_structure(structure),
            active_band=active_band,
            calibration=calibration,
            cost_ceiling=cost_ceiling,
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
        return self.services.chapter_planner.derive_chapter_experience_plan(
            chapter_number=chapter_number,
            structure=_to_core_structure(structure),
            arc_experience=_experience_from_compat_structure(structure),
            schedule=schedule,
            chapter_plan=chapter_plan,
            calibration=calibration,
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
        return self.services.arc_envelope_resolver._resolve_envelope(
            chapter_plans=chapter_plans,
            total_chapters=total_chapters,
            policy=policy,
            base_target_size=base_target_size,
            base_soft_min=base_soft_min,
            base_soft_max=base_soft_max,
            structure=_to_core_structure(structure),
            provisional_band=provisional_band,
            band_id=band_id,
            preview=preview,
            rehearsal=rehearsal,
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
        return ProvisionalPreviewService(
            provisional_executor=self.provisional_executor,
            legacy_preview_enabled=True,
        ).execute(
            session=session,
            project_id=project_id,
            arc_id=arc_id,
            band_id=band_id,
            chapter_plans=chapter_plans,
        )
