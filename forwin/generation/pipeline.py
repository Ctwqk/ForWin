from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from forwin.canon import (
    CanonAdmissionService,
    CanonPreparationService,
)
from forwin.director import ArcDirector
from forwin.generation.gate_delegation import GateDelegationService
from forwin.generation.pipeline_core.acceptance import AcceptanceStage
from forwin.generation.pipeline_core.audit_control import AuditControlStage
from forwin.generation.pipeline_core.finalization import FinalizationStage
from forwin.generation.pipeline_core.gate_delegation import GateDelegationStage
from forwin.generation.pipeline_core.project_chapters import ChapterExecutionStage
from forwin.generation.pipeline_core.quality_gates import QualityDiagnosticsStage
from forwin.generation.pipeline_core.run_control import RunControlStage
from forwin.generation.pipeline_core.runtime_helpers import RuntimeSupportStage
from forwin.generation.pipeline_core.world_projection import PostCanonStage
from forwin.genesis import BookGenesisService
from forwin.maintenance.post_canon import PostCanonMaintenanceService
from forwin.model_adapter import ModelAdapter
from forwin.observability.pipeline_progress import PipelineProgressRecorder
from forwin.observability.pipeline_trace import (
    PipelineAuditContext,
    PipelineTraceRecorder,
)
from forwin.observability.service import ObservabilityService
from forwin.planning.arc_envelope import ArcEnvelopeManager
from forwin.planning.stage_analysis import (
    PacingStrategist,
    ReplanGovernor,
    StageAnalyzer,
)
from forwin.retrieval import RetrievalBroker
from forwin.review.candidate import CandidateReviewService
from forwin.review.draft_service import DraftReviewService
from forwin.review.repair import RepairExecution, RepairService, RepairVerifier
from forwin.review.repair.control import RepairControl
from forwin.review.repair.plan_patch import RepairPlanPatchService
from forwin.review.telemetry import ReviewTelemetry
from forwin.runtime.policy import RuntimePolicy
from forwin.simulation.world import WorldSimulator
from forwin.skills import SkillPromptLayerBuilder, SkillRouter
from forwin.storage import ArtifactStore
from forwin.subworld_manager import SubWorldManager
from forwin.writer.chapter_writer import ChapterWriter
from forwin.writer.execution import WriterExecution
from forwin.writer.execution_telemetry import WriterExecutionTelemetry


class ChapterPipeline(
    RunControlStage,
    AcceptanceStage,
    AuditControlStage,
    RuntimeSupportStage,
    GateDelegationStage,
    ChapterExecutionStage,
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
        self.audit_context = PipelineAuditContext(
            str(task_id or "").strip(), str(root_event_id or "").strip()
        )
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
        self.trace_recorder = PipelineTraceRecorder(
            audit=self.audit_context,
            artifact_store=artifact_store,
            observability=observability,
        )
        self.writer_execution = WriterExecution(
            policy=policy,
            writer=writer,
            skill_router=skill_router,
            skill_prompt_layer_builder=skill_prompt_layer_builder,
            artifact_store=artifact_store,
            telemetry=WriterExecutionTelemetry(
                recorder=self.trace_recorder, writer=writer, model_client=llm_client
            ),
            should_abort=should_abort,
        )
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
        self.progress_recorder = PipelineProgressRecorder(
            trace_recorder=self.trace_recorder,
            observability=self.observability,
            progress_callback=progress_callback,
        )
        self.candidate_review = CandidateReviewService(
            draft_review=draft_review,
            repair_verifier=repair_verifier,
            model_client=llm_client,
            skill_router=skill_router,
            skill_prompt_layer_builder=skill_prompt_layer_builder,
            artifact_store=artifact_store,
            trace_recorder=self.trace_recorder,
        )
        self.repair_plan_patch = RepairPlanPatchService(
            retrieval_broker=retrieval_broker, arc_envelope_manager=arc_envelope_manager
        )
        self.repair_execution = RepairExecution(
            policy=policy,
            candidate_review=self.candidate_review,
            plan_patch=self.repair_plan_patch,
            writer_execution=self.writer_execution,
            telemetry=ReviewTelemetry(self.trace_recorder),
            control=RepairControl(
                progress=self.progress_recorder, should_pause=should_pause
            ),
        )

    @property
    def _audit_task_id(self) -> str:
        return self.audit_context.task_id

    @_audit_task_id.setter
    def _audit_task_id(self, value: str) -> None:
        if not hasattr(self, "audit_context"):
            self.audit_context = PipelineAuditContext()
        self.audit_context.task_id = value

    @property
    def _audit_root_event_id(self) -> str:
        return self.audit_context.root_event_id

    @_audit_root_event_id.setter
    def _audit_root_event_id(self, value: str) -> None:
        if not hasattr(self, "audit_context"):
            self.audit_context = PipelineAuditContext()
        self.audit_context.root_event_id = value

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
