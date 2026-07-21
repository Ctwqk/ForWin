from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from forwin.canon import (
    CanonAdmissionService,
    CanonPreparationContext,
    CanonPreparationService,
)
from forwin.director import ArcDirector
from forwin.generation.gate_delegation import GateDelegationService
from forwin.generation.pipeline_core.acceptance import AcceptanceStage
from forwin.generation.pipeline_core.finalization import FinalizationStage
from forwin.generation.pipeline_core.gate_delegation import GateDelegationStage
from forwin.generation.pipeline_core.audit_control import AuditControlStage
from forwin.generation.pipeline_core.project_chapters import ChapterExecutionStage
from forwin.generation.pipeline_core.quality_gates import QualityDiagnosticsStage
from forwin.generation.pipeline_core.repair_patches import RepairPlanningStage
from forwin.generation.pipeline_core.review_autofix import ReviewWorkflowStage
from forwin.generation.pipeline_core.run_control import RunControlStage
from forwin.generation.pipeline_core.runtime_helpers import RuntimeSupportStage
from forwin.generation.pipeline_core.world_projection import PostCanonStage
from forwin.generation.pipeline_core.writer_attention import WriterExecutionStage
from forwin.genesis import BookGenesisService
from forwin.model_adapter import ModelAdapter
from forwin.maintenance.post_canon import PostCanonMaintenanceService
from forwin.observability.ports import SpanHandle
from forwin.observability.service import ObservabilityService
from forwin.planning.arc_envelope import ArcEnvelopeManager
from forwin.planning.stage_analysis import (
    PacingStrategist,
    ReplanGovernor,
    StageAnalyzer,
)
from forwin.retrieval import RetrievalBroker
from forwin.review.draft_service import DraftReviewService
from forwin.review.repair import RepairExecution, RepairService, RepairVerifier
from forwin.runtime.policy import RuntimePolicy
from forwin.simulation.world import WorldSimulator
from forwin.skills import SkillPromptLayerBuilder, SkillRouter
from forwin.state.updater import StateUpdater
from forwin.storage import ArtifactStore
from forwin.subworld_manager import SubWorldManager
from forwin.writer.chapter_writer import ChapterWriter


class ChapterPipeline(
    RunControlStage,
    AcceptanceStage,
    AuditControlStage,
    RuntimeSupportStage,
    ReviewWorkflowStage,
    RepairPlanningStage,
    GateDelegationStage,
    ChapterExecutionStage,
    WriterExecutionStage,
    QualityDiagnosticsStage,
    PostCanonStage,
    FinalizationStage,
):
    def __init__(
        self,
        *,
        policy: RuntimePolicy,
        engine: Engine,
        session_factory: sessionmaker[Session],
        llm_client: ModelAdapter,
        skill_router: SkillRouter,
        skill_prompt_layer_builder: SkillPromptLayerBuilder,
        arc_director: ArcDirector,
        book_genesis: BookGenesisService,
        subworld_manager: SubWorldManager,
        retrieval_broker: RetrievalBroker,
        artifact_store: ArtifactStore,
        observability: ObservabilityService,
        writer: ChapterWriter,
        stage_analyzer: StageAnalyzer,
        pacing_strategist: PacingStrategist,
        replan_governor: ReplanGovernor,
        world_simulator: WorldSimulator,
        arc_envelope_manager: ArcEnvelopeManager,
        draft_review: DraftReviewService,
        repair: RepairService,
        repair_verifier: RepairVerifier,
        canon_preparation: CanonPreparationService,
        canon_admission: CanonAdmissionService,
        gate_delegation: GateDelegationService,
        post_canon_maintenance: PostCanonMaintenanceService | None = None,
        progress_callback: Callable[[str, dict[str, object]], None] | None = None,
        should_abort: Callable[[], bool] | None = None,
        should_pause: Callable[[], bool] | None = None,
        task_id: str = "",
        root_event_id: str = "",
    ) -> None:
        self.policy = policy
        self.progress_callback = progress_callback
        self.should_abort = should_abort
        self.should_pause = should_pause
        self._audit_task_id = str(task_id or "").strip()
        self._audit_root_event_id = str(root_event_id or "").strip()
        self._audit_project_id = ""
        self._audit_updater: StateUpdater | None = None
        self._audit_stage_name = ""
        self._audit_stage_started_at = 0.0
        self._audit_stage_chapter_number = 0
        self._audit_stage_span: SpanHandle | None = None
        self._runtime_container = None

        self.engine = engine
        self._SessionFactory = session_factory
        self.llm_client = llm_client
        self.skill_router = skill_router
        self.skill_prompt_layer_builder = skill_prompt_layer_builder
        self.arc_director = arc_director
        self.book_genesis = book_genesis
        self.subworld_manager = subworld_manager
        self.retrieval_broker = retrieval_broker
        self.artifact_store = artifact_store
        self.observability = observability
        self.writer = writer
        self.stage_analyzer = stage_analyzer
        self.pacing_strategist = pacing_strategist
        self.replan_governor = replan_governor
        self.world_simulator = world_simulator
        self.arc_envelope_manager = arc_envelope_manager
        self.draft_review = draft_review
        self.repair = repair
        self.repair_verifier = repair_verifier
        self.canon_preparation = canon_preparation
        self.canon_admission = canon_admission
        self.gate_delegation = gate_delegation
        self.post_canon_maintenance = (
            post_canon_maintenance
            or PostCanonMaintenanceService(
                session_factory=self._SessionFactory,
                stage_analyzer=self.stage_analyzer,
                pacing_strategist=self.pacing_strategist,
                replan_governor=self.replan_governor,
                arc_envelope_manager=self.arc_envelope_manager,
                world_simulator=self.world_simulator,
                artifact_store=self.artifact_store,
                llm_client=self.llm_client,
            )
        )
        self.canon_preparation_context = CanonPreparationContext(
            policy=self.policy,
            llm_client=self.llm_client,
            artifact_store=self.artifact_store,
            _record_decision_event=self._record_decision_event,
            _record_rule_decision_event=self._record_rule_decision_event,
            save_prompt_trace=self._save_prompt_trace_payload,
        )
        self.repair_execution = RepairExecution(
            policy=self.policy,
            retrieval_broker=self.retrieval_broker,
            _save_prompt_trace_payload=self._save_prompt_trace_payload,
            _plan_writer_output_entities=self._plan_writer_output_entities,
            _review_current_output=self._review_current_output,
            _apply_canon_name_drift_autofix=self._apply_canon_name_drift_autofix,
            _apply_placeholder_leakage_autofix=(
                self._apply_placeholder_leakage_autofix
            ),
            _persist_draft_and_review=self._persist_draft_and_review,
            _record_decision_event=self._record_decision_event,
            _review_event_payload=self._review_event_payload,
            _record_map_movement_review_issues=(
                self._record_map_movement_review_issues
            ),
            _pause_requested=self._pause_requested,
            _record_rule_decision_event=self._record_rule_decision_event,
            _chapter_plan_snapshot=self._chapter_plan_snapshot,
            _band_plan_snapshot=self._band_plan_snapshot,
            _emit_progress=self._emit_progress,
            _write_chapter_with_attention_fallback=(
                self._write_chapter_with_attention_fallback
            ),
            _review_with_repair_verification=self._review_with_repair_verification,
            _chapter_experience_patch_payload=self._chapter_experience_patch_payload,
            _replace_band_schedule=self._replace_band_schedule,
            _band_schedule_patch_payload=self._band_schedule_patch_payload,
        )

    def close(self) -> None:
        runtime_container = self._runtime_container
        if runtime_container is not None:
            runtime_container.close()
            return
        try:
            self.llm_client.close()
        finally:
            self.engine.dispose()


__all__ = ["ChapterPipeline"]
