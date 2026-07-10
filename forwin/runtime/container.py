from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Callable, Literal

from forwin.application.generation import GenerationApplicationService
from forwin.genesis import BookGenesisService
from forwin.canon import CanonAdmissionService, CanonPreparationService
from forwin.config import InfrastructureConfig
from forwin.context.assembler_core import ChapterContextAssembler
from forwin.context.gates import RecencyTruncateGate
from forwin.director import ArcDirector
from forwin.generation.gate_delegation import GateDelegationService, SparkGateDelegate
from forwin.llm.factory import maybe_wrap_with_codex_router
from forwin.models.base import get_engine, get_session_factory, require_v5_schema
from forwin.planning.arc_envelope import ArcEnvelopeManager
from forwin.planning.service import PlanningService
from forwin.planning.stage_analysis import (
    PacingStrategist,
    ReplanGovernor,
    StageAnalyzer,
)
from forwin.simulation.world import NPCIntentGenerator, WorldSimulator
from forwin.observability.service import ObservabilityService
from forwin.publisher_runtime.codex_intervention import build_codex_intervention_handler
from forwin.publisher_runtime.service import PublisherRuntimeService
from forwin.retrieval import RetrievalBroker, create_memory_index
from forwin.review import DraftReviewService
from forwin.review.repair import RepairService, RepairVerifier
from forwin.runtime.factories import (
    ProductionSchedulerFactory,
    build_provisional_writer,
    build_writer,
)
from forwin.runtime.policy import RuntimePolicy
from forwin.runtime.services import RuntimeServices, SkillRuntimeBundle
from forwin.skills import build_skill_runtime_components
from forwin.storage import ArtifactStore
from forwin.subworld_manager import SubWorldManager
from forwin.writer.llm import LLMClient


logger = logging.getLogger(__name__)

RuntimeRole = Literal[
    "full",
    "api",
    "generation_worker",
    "publisher_worker",
    "mcp",
    "maintenance",
]
_RUNTIME_ROLES: set[str] = {
    "full",
    "api",
    "generation_worker",
    "publisher_worker",
    "mcp",
    "maintenance",
}


@dataclass(slots=True)
class RuntimeContainer:
    infrastructure: InfrastructureConfig
    policy: RuntimePolicy
    role: RuntimeRole = "full"
    _services: RuntimeServices | None = None

    @classmethod
    def from_config(
        cls,
        infrastructure: InfrastructureConfig,
        *,
        policy: RuntimePolicy,
        role: RuntimeRole = "full",
    ) -> "RuntimeContainer":
        normalized_role = _validate_runtime_role(role)
        return cls(
            infrastructure=infrastructure,
            policy=policy,
            role=normalized_role,
        )

    @classmethod
    def for_api(
        cls, infrastructure: InfrastructureConfig, *, policy: RuntimePolicy
    ) -> "RuntimeContainer":
        return cls.from_config(infrastructure, policy=policy, role="api")

    @classmethod
    def for_generation_worker(
        cls, infrastructure: InfrastructureConfig, *, policy: RuntimePolicy
    ) -> "RuntimeContainer":
        return cls.from_config(
            infrastructure,
            policy=policy,
            role="generation_worker",
        )

    @classmethod
    def for_publisher_worker(
        cls, infrastructure: InfrastructureConfig, *, policy: RuntimePolicy
    ) -> "RuntimeContainer":
        return cls.from_config(
            infrastructure,
            policy=policy,
            role="publisher_worker",
        )

    def services(self) -> RuntimeServices:
        if self._services is None:
            self._services = self._build_services()
        return self._services

    def build_chapter_pipeline(
        self,
        *,
        progress_callback: Callable[[str, dict], None] | None = None,
        should_abort: Callable[[], bool] | None = None,
        should_pause: Callable[[], bool] | None = None,
        task_id: str = "",
        root_event_id: str = "",
    ):
        from forwin.generation.pipeline import ChapterPipeline

        services = self.services()
        return ChapterPipeline(
            policy=services.policy,
            engine=services.engine,
            session_factory=services.session_factory,
            llm_client=services.llm_client,
            skill_router=services.skill_runtime.router,
            skill_prompt_layer_builder=services.skill_runtime.prompt_layer_builder,
            arc_director=services.arc_director,
            book_genesis=services.book_genesis,
            subworld_manager=services.subworld_manager,
            retrieval_broker=services.retrieval_broker,
            artifact_store=services.artifact_store,
            observability=services.observability,
            writer=services.writer,
            provisional_writer=services.provisional_writer,
            stage_analyzer=services.stage_analyzer,
            pacing_strategist=services.pacing_strategist,
            replan_governor=services.replan_governor,
            npc_intent_generator=services.npc_intent_generator,
            world_simulator=services.world_simulator,
            arc_envelope_manager=services.arc_envelope_manager,
            draft_review=services.draft_review,
            repair=services.repair,
            repair_verifier=services.repair_verifier,
            canon_preparation=services.canon_preparation,
            canon_admission=services.canon_admission,
            gate_delegation=services.gate_delegation,
            progress_callback=progress_callback,
            should_abort=should_abort,
            should_pause=should_pause,
            task_id=task_id,
            root_event_id=root_event_id,
        )

    def build_generation_application_service(self) -> GenerationApplicationService:
        return self.services().generation_application

    def build_genesis_workspace_service(self):
        return self.services().genesis_workspace_service

    def build_genesis_handoff_service(self):
        return self.services().genesis_handoff_service

    def build_book_genesis_service(self):
        infrastructure = self.infrastructure
        llm_client = self._build_llm_client(infrastructure, self.policy)
        skill_runtime = self._build_skill_runtime(infrastructure)
        artifact_store = self._build_artifact_store(infrastructure)
        return self._build_book_genesis_service(
            config=infrastructure,
            llm_client=llm_client,
            skill_runtime=skill_runtime,
            artifact_store=artifact_store,
        )

    def build_publisher_runtime(self):
        return self.services().publisher_runtime

    def build_production_scheduler(self, **callbacks):
        return self.services().production_scheduler.build(**callbacks)

    def _build_services(self) -> RuntimeServices:
        infrastructure = self.infrastructure
        policy = self.policy
        engine = get_engine(infrastructure.database_url)
        require_v5_schema(engine)
        session_factory = get_session_factory(engine)
        self._run_retention_cleanup(session_factory, infrastructure)
        generation_application = GenerationApplicationService(
            session_factory=session_factory,
            infrastructure=infrastructure,
        )

        model_profile = infrastructure.resolve_model_profile(policy.model_profile_id)
        llm_client = self._build_llm_client(infrastructure, policy)
        skill_runtime = self._build_skill_runtime(infrastructure)
        artifact_store = self._build_artifact_store(infrastructure)
        observability = ObservabilityService(
            session_factory=session_factory,
            artifact_store=artifact_store,
            config=infrastructure,
        )
        if infrastructure.observability_record_db_spans:
            from forwin.observability.sqlalchemy_probe import (
                install_sqlalchemy_query_probe,
            )

            install_sqlalchemy_query_probe(engine)
        book_genesis = self._build_book_genesis_service(
            config=infrastructure,
            llm_client=llm_client,
            skill_runtime=skill_runtime,
            artifact_store=artifact_store,
        )
        book_genesis.observability = observability

        arc_director = ArcDirector(
            llm_client=llm_client,
            max_tokens=infrastructure.max_tokens,
        )
        subworld_manager = SubWorldManager(director=arc_director)
        retrieval_broker = RetrievalBroker(
            context_budget_chars=infrastructure.context_budget_chars,
            max_entities=infrastructure.retrieval_max_entities,
            max_threads=infrastructure.retrieval_max_threads,
            max_summaries=infrastructure.retrieval_max_summaries,
            database_url=infrastructure.database_url,
            retrieval_backend=infrastructure.retrieval_backend,
            qdrant_url=infrastructure.qdrant_url,
            qdrant_collection=infrastructure.qdrant_collection,
            llm_kb_qdrant_url=infrastructure.qdrant_url,
            llm_kb_qdrant_collection=infrastructure.llm_kb_qdrant_collection,
            memory_index=create_memory_index(
                backend=infrastructure.retrieval_backend,
                root_dir=infrastructure.retrieval_root,
                qdrant_url=infrastructure.qdrant_url,
                qdrant_collection=infrastructure.qdrant_collection,
                embedding_backend=infrastructure.embedding_backend,
                embedding_base_url=infrastructure.embedding_base_url,
                embedding_api_key=infrastructure.embedding_api_key,
                embedding_model=infrastructure.embedding_model,
                embedding_dims=infrastructure.embedding_dims,
                embedding_required=infrastructure.embedding_required,
            ),
        )

        writer = build_writer(infrastructure, policy, llm_client, observability)
        provisional_writer = build_provisional_writer(
            infrastructure, policy, llm_client, observability
        )
        stage_analyzer = StageAnalyzer()
        pacing_strategist = PacingStrategist(
            window_size=3,
            stale_thread_window=3,
            min_avg_chars=1600,
            max_avg_chars=3800,
            active_thread_limit=infrastructure.phase_active_thread_limit,
        )
        replan_governor = ReplanGovernor(
            cooldown_chapters=3,
            director=arc_director,
            subworld_manager=subworld_manager,
        )
        llm_available = bool(model_profile.api_key) or infrastructure.codex_enabled
        phase4_llm = (
            llm_client if policy.planning.use_llm_simulation and llm_available else None
        )
        npc_intent_generator = NPCIntentGenerator(
            llm_client=phase4_llm,
            active_thread_limit=infrastructure.phase_active_thread_limit,
        )
        world_simulator = WorldSimulator(
            llm_client=phase4_llm,
            active_thread_limit=infrastructure.phase_active_thread_limit,
        )
        planning_service = PlanningService.build_default(
            director=arc_director,
            subworld_manager=subworld_manager,
            provisional_preview_enabled=policy.planning.provisional_preview,
            trope_cost_ceiling=2 if policy.quality_profile == "pulp" else 3,
        )
        arc_envelope_manager = ArcEnvelopeManager(
            director=arc_director,
            subworld_manager=subworld_manager,
            provisional_preview_enabled=policy.planning.provisional_preview,
            planning_service=planning_service,
        )

        hub_llm_enabled = llm_available
        draft_review = DraftReviewService(
            experience_review_enabled=policy.review.allows_signal("experience"),
            lint_review_enabled=policy.review.allows_signal("lint"),
            map_movement_review_enabled=policy.review.allows_signal("map_movement"),
            personality_review_enabled=policy.review.allows_signal("personality"),
            canon_quality_review_in_hub_enabled=policy.review.allows_signal(
                "canon_quality"
            ),
            publisher_compliance_review_enabled=policy.review.allows_signal(
                "publisher"
            ),
            llm_client=llm_client if hub_llm_enabled else None,
            llm_enabled=hub_llm_enabled,
            observability=observability,
        )
        publisher_runtime = PublisherRuntimeService(
            session_factory=session_factory,
            extension_api_key=infrastructure.publisher_extension_api_key,
            heartbeat_stale_seconds=90,
            preferred_client_id=infrastructure.publisher_preferred_client_id,
            publisher_session_secret=infrastructure.publisher_session_secret,
            publisher_session_encryption_required=infrastructure.publisher_session_encryption_required,
            strict_preferred_client=infrastructure.publisher_strict_preferred_client,
            observability=observability,
            codex_intervention_handler=build_codex_intervention_handler(infrastructure),
            minimax_api_key=model_profile.api_key,
            minimax_base_url=model_profile.base_url,
        )
        return RuntimeServices(
            infrastructure=infrastructure,
            policy=policy,
            engine=engine,
            session_factory=session_factory,
            llm_client=llm_client,
            skill_runtime=skill_runtime,
            generation_application=generation_application,
            arc_director=arc_director,
            book_genesis=book_genesis,
            subworld_manager=subworld_manager,
            retrieval_broker=retrieval_broker,
            artifact_store=artifact_store,
            observability=observability,
            stage_analyzer=stage_analyzer,
            pacing_strategist=pacing_strategist,
            replan_governor=replan_governor,
            npc_intent_generator=npc_intent_generator,
            world_simulator=world_simulator,
            arc_envelope_manager=arc_envelope_manager,
            genesis_workspace_service=book_genesis.workspace,
            genesis_handoff_service=book_genesis.handoff,
            production_scheduler=ProductionSchedulerFactory(
                session_factory=session_factory,
                infrastructure=infrastructure,
                generation_application=generation_application,
                observability=observability,
            ),
            publisher_runtime=publisher_runtime,
            context_assembler=ChapterContextAssembler(
                gates=[
                    *ChapterContextAssembler._default_gates(),
                    RecencyTruncateGate(
                        window_chapters=policy.planning.context_recency_window,
                        max_entities=infrastructure.retrieval_max_entities,
                    ),
                ],
                observability=observability,
            ),
            draft_review=draft_review,
            writer=writer,
            provisional_writer=provisional_writer,
            repair=RepairService(),
            repair_verifier=RepairVerifier(
                llm_client=llm_client if llm_available else None,
                llm_enabled=llm_available,
            ),
            canon_preparation=CanonPreparationService(),
            canon_admission=CanonAdmissionService(session_factory=session_factory),
            gate_delegation=GateDelegationService(
                spark_delegate=SparkGateDelegate(llm_client=llm_client)
            ),
        )

    def _run_retention_cleanup(
        self, session_factory, config: InfrastructureConfig
    ) -> None:  # noqa: ANN001
        if not bool(getattr(config, "retention_cleanup_on_startup", True)):
            return
        try:
            from forwin.maintenance.retention import (
                RetentionPolicy,
                run_retention_cleanup,
            )

            with session_factory.begin() as session:
                result = run_retention_cleanup(
                    session, RetentionPolicy.from_config(config)
                )
            logger.info(
                "retention_cleanup_completed performance_spans=%s prompt_traces=%s candidate_drafts=%s",
                result.performance_spans_deleted,
                result.prompt_traces_deleted,
                result.candidate_drafts_deleted,
            )
        except Exception:
            logger.warning("retention_cleanup_failed", exc_info=True)

    @staticmethod
    def _build_llm_client(
        infrastructure: InfrastructureConfig,
        policy: RuntimePolicy,
    ):
        profile = infrastructure.resolve_model_profile(policy.model_profile_id)
        llm_client = LLMClient(
            api_key=profile.api_key,
            base_url=profile.base_url,
            model=profile.model,
            timeout_seconds=infrastructure.llm_timeout_seconds,
            retry_attempts=infrastructure.llm_retry_attempts,
            retry_initial_delay_seconds=infrastructure.llm_retry_initial_delay_seconds,
            retry_max_delay_seconds=infrastructure.llm_retry_max_delay_seconds,
            fallback_profiles=infrastructure.llm_fallback_profiles,
        )
        llm_client.profile_id = profile.id
        llm_client.profile_name = profile.name
        return maybe_wrap_with_codex_router(llm_client, infrastructure)

    @staticmethod
    def _build_skill_runtime(config: InfrastructureConfig) -> SkillRuntimeBundle:
        registry, router, prompt_layer_builder = build_skill_runtime_components(
            root=config.skill_registry_path,
            enabled=config.skill_runtime_enabled,
            strictness=config.skill_strictness,
            enabled_skill_groups=config.enabled_skill_groups,
            disabled_skill_ids=config.disabled_skill_ids,
        )
        return SkillRuntimeBundle(
            registry=registry,
            router=router,
            prompt_layer_builder=prompt_layer_builder,
        )

    @staticmethod
    def _build_book_genesis_service(
        *,
        config: InfrastructureConfig,
        llm_client,
        skill_runtime,
        artifact_store,
    ) -> BookGenesisService:
        service = BookGenesisService(
            llm_client=llm_client,
            max_tokens=min(config.max_tokens, 1600),
            skill_router=skill_runtime.router,
            skill_prompt_layer_builder=skill_runtime.prompt_layer_builder,
        )
        setattr(service, "artifact_store", artifact_store)
        return service

    @staticmethod
    def _build_artifact_store(config: InfrastructureConfig) -> ArtifactStore:
        return ArtifactStore(
            config.artifact_root,
            backend=config.artifact_backend,
            minio_endpoint=config.minio_endpoint,
            minio_access_key=config.minio_access_key,
            minio_secret_key=config.minio_secret_key,
            minio_bucket=config.minio_bucket,
            minio_prefix=config.minio_prefix,
            minio_secure=config.minio_secure,
        )


def _validate_runtime_role(role: str) -> RuntimeRole:
    normalized = str(role or "full").strip() or "full"
    if normalized not in _RUNTIME_ROLES:
        raise ValueError(f"Unsupported runtime role: {role}")
    return normalized  # type: ignore[return-value]
