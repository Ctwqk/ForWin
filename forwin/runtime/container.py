from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Callable

from forwin.book_genesis import BookGenesisService
from forwin.config import Config
from forwin.context.assembler import ChapterContextAssembler
from forwin.context.gates import RecencyTruncateGate
from forwin.director import ArcDirector
from forwin.experience.service import ExperiencePlanningService
from forwin.llm.factory import maybe_wrap_with_codex_router
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.orchestrator.phase24 import ArcEnvelopeManager
from forwin.orchestrator.phase3 import PacingStrategist, ReplanGovernor, StageAnalyzer
from forwin.orchestrator.phase4 import NPCIntentGenerator, WorldSimulator
from forwin.observability.service import ObservabilityService
from forwin.planning.band_plan_service import BandPlanService
from forwin.planning.world_contract_service import WorldContractPlanningService
from forwin.publisher_runtime.codex_intervention import build_codex_intervention_handler
from forwin.publisher_runtime.service import PublisherRuntimeService
from forwin.retrieval import RetrievalBroker, create_memory_index
from forwin.reviewer import HistoricalReviewHub
from forwin.reviser import RepairVerifier
from forwin.runtime.factories import ProductionSchedulerFactory, build_provisional_writer, build_writer
from forwin.runtime.services import RuntimeServices, SkillRuntimeBundle
from forwin.skills import build_skill_runtime_components
from forwin.storage import ArtifactStore
from forwin.subworld_manager import SubWorldManager
from forwin.writer.llm_client import LLMClient


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RuntimeContainer:
    config: Config
    _services: RuntimeServices | None = None

    @classmethod
    def from_config(cls, config: Config) -> "RuntimeContainer":
        return cls(config=config)

    def services(self) -> RuntimeServices:
        if self._services is None:
            self._services = self._build_services()
        return self._services

    def build_writing_orchestrator(
        self,
        *,
        progress_callback: Callable[[str, dict], None] | None = None,
        should_abort: Callable[[], bool] | None = None,
        should_pause: Callable[[], bool] | None = None,
    ):
        from forwin.orchestrator.loop import WritingOrchestrator

        return WritingOrchestrator(
            services=self.services(),
            progress_callback=progress_callback,
            should_abort=should_abort,
            should_pause=should_pause,
        )

    def build_genesis_workspace_service(self):
        return self.services().genesis_workspace_service

    def build_genesis_handoff_service(self):
        return self.services().genesis_handoff_service

    def build_book_genesis_service(self):
        config = self.config
        llm_client = self._build_llm_client(config)
        skill_runtime = self._build_skill_runtime(config)
        artifact_store = self._build_artifact_store(config)
        return self._build_book_genesis_service(
            config=config,
            llm_client=llm_client,
            skill_runtime=skill_runtime,
            artifact_store=artifact_store,
        )

    def build_publisher_runtime(self):
        return self.services().publisher_runtime

    def build_production_scheduler(self, **callbacks):
        return self.services().production_scheduler.build(**callbacks)

    def _build_services(self) -> RuntimeServices:
        config = self.config
        engine = get_engine(config.database_url)
        init_db(engine)
        session_factory = get_session_factory(engine)
        self._run_retention_cleanup(session_factory, config)

        llm_client = self._build_llm_client(config)
        skill_runtime = self._build_skill_runtime(config)
        artifact_store = self._build_artifact_store(config)
        observability = ObservabilityService(
            session_factory=session_factory,
            artifact_store=artifact_store,
            config=config,
        )
        if bool(getattr(config, "observability_record_db_spans", False)):
            from forwin.observability.sqlalchemy_probe import install_sqlalchemy_query_probe

            install_sqlalchemy_query_probe(engine)
        book_genesis = self._build_book_genesis_service(
            config=config,
            llm_client=llm_client,
            skill_runtime=skill_runtime,
            artifact_store=artifact_store,
        )
        book_genesis.observability = observability

        arc_director = ArcDirector(
            llm_client=llm_client,
            max_tokens=config.max_tokens,
        )
        subworld_manager = SubWorldManager(director=arc_director)
        retrieval_broker = RetrievalBroker(
            context_budget_chars=config.context_budget_chars,
            max_entities=config.retrieval_max_entities,
            max_threads=config.retrieval_max_threads,
            max_summaries=config.retrieval_max_summaries,
            database_url=config.database_url,
            retrieval_backend=config.retrieval_backend,
            qdrant_url=config.qdrant_url,
            qdrant_collection=config.qdrant_collection,
            llm_kb_qdrant_url=config.qdrant_url,
            llm_kb_qdrant_collection=config.llm_kb_qdrant_collection,
            memory_index=create_memory_index(
                backend=config.retrieval_backend,
                root_dir=config.retrieval_root,
                database_url=config.database_url,
                qdrant_url=config.qdrant_url,
                qdrant_collection=config.qdrant_collection,
                embedding_backend=config.embedding_backend,
                embedding_base_url=config.embedding_base_url,
                embedding_api_key=config.embedding_api_key,
                embedding_model=config.embedding_model,
                embedding_dims=config.embedding_dims,
            ),
        )

        writer = build_writer(config, llm_client, observability)
        provisional_writer = build_provisional_writer(config, llm_client, observability)
        stage_analyzer = StageAnalyzer()
        pacing_strategist = PacingStrategist(
            window_size=config.pacing_window_size,
            stale_thread_window=config.stale_thread_window,
            min_avg_chars=config.pacing_min_avg_chars,
            max_avg_chars=config.pacing_max_avg_chars,
            active_thread_limit=config.phase_active_thread_limit,
        )
        replan_governor = ReplanGovernor(
            cooldown_chapters=config.replan_cooldown_chapters,
            director=arc_director,
            subworld_manager=subworld_manager,
        )
        llm_available = bool(config.minimax_api_key) or bool(getattr(config, "codex_enabled", False))
        phase4_llm = llm_client if config.phase4_use_llm and llm_available else None
        npc_intent_generator = NPCIntentGenerator(
            llm_client=phase4_llm,
            active_thread_limit=config.phase_active_thread_limit,
        )
        world_simulator = WorldSimulator(
            llm_client=phase4_llm,
            active_thread_limit=config.phase_active_thread_limit,
        )
        world_contract_service = WorldContractPlanningService()
        experience_planning_service = ExperiencePlanningService()
        band_plan_service = BandPlanService(
            subworld_manager=subworld_manager,
            world_contract_service=world_contract_service,
            experience_service=experience_planning_service,
            trope_cost_ceiling=2 if config.quality_profile == "pulp" else 3,
        )
        arc_envelope_manager = ArcEnvelopeManager(
            director=arc_director,
            subworld_manager=subworld_manager,
            legacy_preview_enabled=config.legacy_provisional_blocking,
        )
        arc_envelope_manager.services.band_plan = band_plan_service
        arc_envelope_manager.services.world_contracts = world_contract_service
        arc_envelope_manager.services.experience = experience_planning_service

        hub_llm_enabled = (
            llm_available
            and str(config.reviewer_quality_mode or "").strip().lower() != "deterministic"
        )
        review_hub = HistoricalReviewHub(
            experience_review_enabled=config.experience_review_enabled,
            lint_review_enabled=config.lint_review_enabled,
            map_movement_review_enabled=config.map_movement_review_enabled,
            personality_review_enabled=config.personality_review_enabled,
            canon_quality_review_in_hub_enabled=config.canon_quality_review_in_hub_enabled,
            llm_client=llm_client if hub_llm_enabled else None,
            llm_enabled=hub_llm_enabled,
            observability=observability,
            chapter_review_form_mode=config.chapter_review_form_mode,
        )
        publisher_runtime = PublisherRuntimeService(
            session_factory=session_factory,
            extension_api_key=config.publisher_extension_api_key,
            heartbeat_stale_seconds=90,
            preferred_client_id=config.publisher_preferred_client_id,
            publisher_session_secret=config.publisher_session_secret,
            publisher_session_encryption_required=config.publisher_session_encryption_required,
            strict_preferred_client=config.publisher_strict_preferred_client,
            observability=observability,
            codex_intervention_handler=build_codex_intervention_handler(config),
        )
        return RuntimeServices(
            config=config,
            engine=engine,
            session_factory=session_factory,
            llm_client=llm_client,
            skill_runtime=skill_runtime,
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
            experience_planning_service=experience_planning_service,
            band_plan_service=band_plan_service,
            world_contract_service=world_contract_service,
            genesis_workspace_service=book_genesis.workspace,
            genesis_handoff_service=book_genesis.handoff,
            production_scheduler=ProductionSchedulerFactory(
                session_factory=session_factory,
                config=config,
                observability=observability,
            ),
            publisher_runtime=publisher_runtime,
            context_assembler=ChapterContextAssembler(
                gates=[
                    *ChapterContextAssembler._default_gates(),
                    RecencyTruncateGate(
                        window_chapters=config.context_recency_window_chapters,
                        max_entities=config.retrieval_max_entities,
                    ),
                ],
                observability=observability,
            ),
            review_hub=review_hub,
            writer=writer,
            provisional_writer=provisional_writer,
            repair_verifier=RepairVerifier(
                llm_client=llm_client if llm_available else None,
                llm_enabled=llm_available,
            ),
        )

    def _run_retention_cleanup(self, session_factory, config: Config) -> None:  # noqa: ANN001
        if not bool(getattr(config, "retention_cleanup_on_startup", True)):
            return
        try:
            from forwin.maintenance.retention import RetentionPolicy, run_retention_cleanup

            with session_factory.begin() as session:
                result = run_retention_cleanup(session, RetentionPolicy.from_config(config))
            logger.info(
                "retention_cleanup_completed performance_spans=%s prompt_traces=%s candidate_drafts=%s",
                result.performance_spans_deleted,
                result.prompt_traces_deleted,
                result.candidate_drafts_deleted,
            )
        except Exception:
            logger.warning("retention_cleanup_failed", exc_info=True)

    @staticmethod
    def _build_llm_client(config: Config):
        llm_client = LLMClient(
            api_key=config.minimax_api_key,
            base_url=config.minimax_base_url,
            model=config.minimax_model,
            timeout_seconds=config.llm_timeout_seconds,
            retry_attempts=config.llm_retry_attempts,
            retry_initial_delay_seconds=config.llm_retry_initial_delay_seconds,
            retry_max_delay_seconds=config.llm_retry_max_delay_seconds,
            fallback_profiles=config.llm_fallback_profiles,
        )
        return maybe_wrap_with_codex_router(llm_client, config)

    @staticmethod
    def _build_skill_runtime(config: Config) -> SkillRuntimeBundle:
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
        config: Config,
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
    def _build_artifact_store(config: Config) -> ArtifactStore:
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
