from __future__ import annotations

from dataclasses import dataclass, field
from functools import partial
import logging
import threading
from typing import TYPE_CHECKING, Callable, Literal, cast

from forwin.application.generation import GenerationApplicationService
from forwin.canon import CanonAdmissionService, CanonPreparationService
from forwin.config import InfrastructureConfig, is_configured
from forwin.generation.gate_delegation import GateDelegationService, LLMGateDelegate
from forwin.genesis import BookGenesisService
from forwin.models.base import get_engine, get_session_factory, require_v5_schema
from forwin.observability.service import ObservabilityService
from forwin.planning.arc_progression import ArcProgressionService
from forwin.publisher_runtime.service import PublisherRuntimeService
from forwin.retrieval import RetrievalBroker, create_memory_index
from forwin.review.draft_service import DraftReviewService
from forwin.review.repair import RepairService, RepairVerifier
from forwin.runtime.factories import ProductionSchedulerFactory, build_writer
from forwin.runtime.policy import RuntimePolicy
from forwin.runtime.services import (
    CoreRuntimeServices,
    GenerationRuntimeServices,
    PublisherRuntimeServices,
    SkillRuntimeBundle,
)
from forwin.audience.comment_analysis import CommentAnalyzer
from forwin.skills import build_skill_runtime_components
from forwin.storage import ArtifactStore
from forwin.writer.llm import LLMClient


logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from forwin.maintenance.post_canon import PostCanonMaintenanceService
    from forwin.publisher_runtime.canon_jobs import CanonPublisherJobService

RuntimeRole = Literal[
    "api",
    "generation_worker",
    "publisher_worker",
    "outbox_worker",
]
_RUNTIME_ROLES: frozenset[str] = frozenset(
    {"api", "generation_worker", "publisher_worker", "outbox_worker"}
)


@dataclass(slots=True)
class RuntimeContainer:
    infrastructure: InfrastructureConfig
    policy: RuntimePolicy
    role: RuntimeRole
    _core_services: CoreRuntimeServices | None = None
    _generation_services: GenerationRuntimeServices | None = None
    _publisher_services: PublisherRuntimeServices | None = None
    _post_canon_maintenance: PostCanonMaintenanceService | None = None
    _outbox_publisher_jobs: CanonPublisherJobService | None = None
    _outbox_handlers: dict | None = None
    _outbox_resources: list[object] = field(default_factory=list)
    _project_planning_clients: dict[str, object] = field(default_factory=dict)
    _closed: bool = False
    _lock: threading.RLock = field(
        default_factory=threading.RLock,
        repr=False,
    )

    @classmethod
    def from_config(
        cls,
        infrastructure: InfrastructureConfig,
        *,
        policy: RuntimePolicy,
        role: RuntimeRole,
    ) -> "RuntimeContainer":
        return cls(
            infrastructure=infrastructure,
            policy=policy,
            role=_validate_runtime_role(role),
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

    @classmethod
    def for_outbox_worker(
        cls, infrastructure: InfrastructureConfig, *, policy: RuntimePolicy
    ) -> "RuntimeContainer":
        return cls.from_config(
            infrastructure,
            policy=policy,
            role="outbox_worker",
        )

    def core_services(self) -> CoreRuntimeServices:
        with self._lock:
            self._require_open()
            if self._core_services is None:
                services = self._build_core_services()
                self._core_services = services
            return self._core_services

    def generation_services(self) -> GenerationRuntimeServices:
        self._require_capability(
            "generation",
            allowed_roles={"api", "generation_worker"},
        )
        with self._lock:
            self._require_open()
            if self._generation_services is None:
                services = self._build_generation_services()
                self._generation_services = services
            return self._generation_services

    def publisher_services(self) -> PublisherRuntimeServices:
        self._require_capability(
            "publisher",
            allowed_roles={"api", "publisher_worker"},
        )
        with self._lock:
            self._require_open()
            if self._publisher_services is None:
                services = self._build_publisher_services()
                self._publisher_services = services
            return self._publisher_services

    def build_outbox_handlers(self) -> dict:
        self._require_capability("outbox", allowed_roles={"outbox_worker"})
        with self._lock:
            self._require_open()
            if self._outbox_handlers is None:
                handlers = self._build_outbox_handlers()
                self._outbox_handlers = handlers
            return self._outbox_handlers

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

        with self._lock:
            self._require_open()
            core = self.core_services()
            generation = self.generation_services()
            pipeline = ChapterPipeline(
                policy=core.policy,
                engine=core.engine,
                session_factory=core.session_factory,
                llm_client=generation.llm_client,
                skill_router=generation.skill_runtime.router,
                skill_prompt_layer_builder=(
                    generation.skill_runtime.prompt_layer_builder
                ),
                book_genesis=generation.book_genesis,
                retrieval_broker=generation.retrieval_broker,
                artifact_store=core.artifact_store,
                observability=core.observability,
                writer=generation.writer,
                comment_analyzer=generation.comment_analyzer,
                draft_review=generation.draft_review,
                repair=generation.repair,
                repair_verifier=generation.repair_verifier,
                canon_preparation=generation.canon_preparation,
                canon_admission=generation.canon_admission,
                gate_delegation=generation.gate_delegation,
                band_planner=self.build_band_planner(),
                arc_progression=ArcProgressionService(self.build_beat_sheet_service()),
                post_canon_maintenance=self._resolve_post_canon_maintenance(
                    core=core,
                    generation=generation,
                ),
                progress_callback=progress_callback,
                should_abort=should_abort,
                should_pause=should_pause,
                task_id=task_id,
                root_event_id=root_event_id,
            )
            pipeline._runtime_container = self
            return pipeline

    def build_generation_application_service(self) -> GenerationApplicationService:
        self._require_capability(
            "generation application",
            allowed_roles={"api", "generation_worker"},
        )
        return self.core_services().generation_application

    def build_project_plan_amender(self):
        """Resolve the project's policy at amendment time, after the route has loaded it from its store."""
        self._require_capability("generation", allowed_roles={"api", "generation_worker"})
        return _ProjectPlanAmender(self)

    def build_plan_amender(self, *, policy: RuntimePolicy | None = None):
        """Build an amender on the supplied policy, defaulting to this container's generation policy."""
        from forwin.planning.amend import PlanAmender

        return PlanAmender(self.build_band_planner(policy=policy), self.build_beat_sheet_service(policy=policy))

    def build_band_planner(self, *, policy: RuntimePolicy | None = None):
        """Use the supplied project policy, or the generation client's policy when omitted."""
        self._require_capability(
            "generation",
            allowed_roles={"api", "generation_worker"},
        )
        with self._lock:
            self._require_open()
            core = self.core_services()
            return self._band_planner(
                self._planning_llm_client(core) if policy is None else self._project_planning_llm_client(policy),
                core.artifact_store, policy if policy is not None else core.policy,
            )

    def build_beat_sheet_service(self, *, policy: RuntimePolicy | None = None):
        """Use the supplied project policy, or the generation client's policy when omitted."""
        self._require_capability(
            "generation",
            allowed_roles={"api", "generation_worker"},
        )
        with self._lock:
            self._require_open()
            core = self.core_services()
            return self._beat_sheet_service(
                self._planning_llm_client(core) if policy is None else self._project_planning_llm_client(policy),
                core.artifact_store,
            )

    def _project_planning_llm_client(self, policy: RuntimePolicy):
        """Planning clients are keyed by policy and owned by this process, including on the outbox worker."""
        with self._lock:
            self._require_open()
            if not _llm_available(self.infrastructure, policy):
                return None
            key = policy.model_dump_json()
            if key not in self._project_planning_clients:
                self._project_planning_clients[key] = self._build_llm_client(self.infrastructure, policy)
            return self._project_planning_clients[key]

    def _planning_llm_client(self, core: CoreRuntimeServices):
        if not _llm_available(core.infrastructure, core.policy):
            return None
        return self.generation_services().llm_client

    def build_book_genesis_service(self):
        self._require_capability(
            "generation",
            allowed_roles={"api", "generation_worker"},
        )
        with self._lock:
            self._require_open()
            infrastructure = self.infrastructure
            llm_client = self._build_llm_client(infrastructure, self.policy)
            try:
                skill_runtime = self._build_skill_runtime(infrastructure)
                artifact_store = self._build_artifact_store(infrastructure)
                return self._build_book_genesis_service(
                    config=infrastructure,
                    llm_client=llm_client,
                    skill_runtime=skill_runtime,
                    artifact_store=artifact_store,
                    beat_sheets=self._beat_sheet_service(
                        llm_client if _llm_available(infrastructure, self.policy) else None,
                        artifact_store,
                    ),
                )
            except Exception:
                llm_client.close()
                raise

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            generation = self._generation_services
            core = self._core_services
            outbox_resources = list(self._outbox_resources)
            self._outbox_resources.clear()
            if generation is not None:
                _close_resource(generation.retrieval_broker, "retrieval")
            for resource in outbox_resources:
                _close_resource(resource, "outbox")
            for resource in self._project_planning_clients.values():
                _close_resource(resource, "project planning LLM")
            self._project_planning_clients.clear()
            if generation is not None:
                _close_resource(generation.llm_client, "LLM")
            if core is not None:
                _close_resource(
                    getattr(core, "artifact_store", None),
                    "artifact",
                )
                try:
                    core.engine.dispose()
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "Ignoring runtime engine shutdown error.", exc_info=True
                    )

    def _build_core_services(self) -> CoreRuntimeServices:
        infrastructure = self.infrastructure
        engine = get_engine(infrastructure.database_url)
        try:
            require_v5_schema(engine)
            session_factory = get_session_factory(engine)
            self._run_retention_cleanup(session_factory, infrastructure)
            generation_application = GenerationApplicationService(
                session_factory=session_factory,
                infrastructure=infrastructure,
            )
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
            return CoreRuntimeServices(
                infrastructure=infrastructure,
                policy=self.policy,
                engine=engine,
                session_factory=session_factory,
                generation_application=generation_application,
                artifact_store=artifact_store,
                observability=observability,
            )
        except Exception:
            engine.dispose()
            raise

    def _build_generation_services(self) -> GenerationRuntimeServices:
        core = self.core_services()
        infrastructure = core.infrastructure
        policy = core.policy
        llm_client = self._build_llm_client(infrastructure, policy)
        try:
            skill_runtime = self._build_skill_runtime(infrastructure)
            # Built inline: build_beat_sheet_service resolves these generation services.
            book_genesis = self._build_book_genesis_service(
                config=infrastructure,
                llm_client=llm_client,
                skill_runtime=skill_runtime,
                artifact_store=core.artifact_store,
                beat_sheets=self._beat_sheet_service(
                    llm_client if _llm_available(infrastructure, policy) else None,
                    core.artifact_store,
                ),
            )
            book_genesis.observability = core.observability

            retrieval_broker = RetrievalBroker(
                context_budget_chars=infrastructure.context_budget_chars,
                max_entities=infrastructure.retrieval_max_entities,
                max_threads=infrastructure.retrieval_max_threads,
                max_summaries=infrastructure.retrieval_max_summaries,
                memory_index_provider=lambda: self._build_memory_index(infrastructure),
            )

            writer = build_writer(
                infrastructure,
                policy,
                llm_client,
                core.observability,
            )
            llm_available = llm_configured(infrastructure, policy)
            comment_analyzer = CommentAnalyzer(
                llm_client=llm_client
                if policy.quality_profile == "standard" and llm_available else None
            )
            draft_review = DraftReviewService(
                experience_review_enabled=policy.review.allows_signal("experience"),
                lint_review_enabled=policy.review.allows_signal("lint"),
                world_consistency_review_enabled=policy.review.allows_signal(
                    "world_consistency"
                ),
                personality_review_enabled=policy.review.allows_signal("personality"),
                canon_quality_review_in_hub_enabled=policy.review.allows_signal(
                    "canon_quality"
                ),
                publisher_compliance_review_enabled=policy.review.allows_signal(
                    "publisher"
                ),
                llm_client=llm_client if llm_available else None,
                llm_enabled=llm_available,
                observability=core.observability,
                travel_tolerance=policy.review.travel_tolerance,
            )
            return GenerationRuntimeServices(
                llm_client=llm_client,
                skill_runtime=skill_runtime,
                book_genesis=book_genesis,
                retrieval_broker=retrieval_broker,
                comment_analyzer=comment_analyzer,
                draft_review=draft_review,
                writer=writer,
                repair=RepairService(),
                repair_verifier=RepairVerifier(
                    llm_client=llm_client if llm_available else None,
                    llm_enabled=llm_available,
                ),
                canon_preparation=CanonPreparationService(),
                canon_admission=CanonAdmissionService(
                    session_factory=core.session_factory
                ),
                gate_delegation=GateDelegationService(
                    llm_delegate=LLMGateDelegate(
                        llm_client=llm_client,
                    )
                ),
            )
        except Exception:
            llm_client.close()
            raise

    def _build_publisher_services(self) -> PublisherRuntimeServices:
        core = self.core_services()
        publisher_runtime = self._build_publisher_runtime(core)
        return PublisherRuntimeServices(
            publisher_runtime=publisher_runtime,
            production_scheduler=ProductionSchedulerFactory(
                session_factory=core.session_factory,
                infrastructure=core.infrastructure,
                generation_application=core.generation_application,
                observability=core.observability,
            ),
        )

    def _build_publisher_runtime(
        self,
        core: CoreRuntimeServices,
    ) -> PublisherRuntimeService:
        infrastructure = core.infrastructure
        return PublisherRuntimeService(
            session_factory=core.session_factory,
            extension_api_key=infrastructure.publisher_extension_api_key,
            heartbeat_stale_seconds=90,
            preferred_client_id=infrastructure.publisher_preferred_client_id,
            publisher_session_secret=infrastructure.publisher_session_secret,
            publisher_session_encryption_required=(
                infrastructure.publisher_session_encryption_required
            ),
            strict_preferred_client=infrastructure.publisher_strict_preferred_client,
            observability=core.observability,
            minimax_api_key=infrastructure.minimax_api_key,
            minimax_base_url=infrastructure.minimax_base_url,
        )

    def _build_outbox_handlers(self) -> dict:
        from forwin.outbox.handlers import build_default_outbox_handlers

        core = self.core_services()
        return build_default_outbox_handlers(
            session_factory=core.session_factory,
            config=core.infrastructure,
            memory_index_provider=self._provide_outbox_memory_index,
            post_canon_service_provider=(self._provide_outbox_post_canon_maintenance),
            publisher_job_service_provider=self._provide_outbox_publisher_jobs,
            artifact_store_provider=lambda: core.artifact_store,
        )

    def _resolve_post_canon_maintenance(
        self,
        *,
        core: CoreRuntimeServices,
        generation: GenerationRuntimeServices,
    ) -> PostCanonMaintenanceService:
        from forwin.maintenance.post_canon import (
            PostCanonMaintenanceService,
            build_world_tick_service,
        )

        service = self._post_canon_maintenance
        if service is None:
            service = PostCanonMaintenanceService(
                session_factory=core.session_factory,
                comment_analyzer=generation.comment_analyzer,
                artifact_store=core.artifact_store,
                llm_client=generation.llm_client,
                plan_step=self._plan_step_service(
                    generation.llm_client if _llm_available(core.infrastructure, core.policy) else None,
                    core.artifact_store, core.policy,
                ),
                world_tick=build_world_tick_service(
                    llm_client=generation.llm_client,
                    session_factory=core.session_factory,
                    artifact_store=core.artifact_store,
                    llm_available=_llm_available(core.infrastructure, core.policy),
                    admission=generation.canon_admission,
                ),
            )
            self._post_canon_maintenance = service
        return service

    def _provide_outbox_post_canon_maintenance(
        self,
    ) -> PostCanonMaintenanceService:
        with self._lock:
            self._require_open()
            core = self.core_services()
            service = self._post_canon_maintenance
            if service is None:
                service = self._build_outbox_post_canon_maintenance(core)
                self._post_canon_maintenance = service
            return service

    def _provide_outbox_publisher_jobs(self) -> CanonPublisherJobService:
        with self._lock:
            self._require_open()
            service = self._outbox_publisher_jobs
            if service is None:
                service = self._build_publisher_runtime(self.core_services()).canon_jobs
                self._outbox_publisher_jobs = service
            return service

    def _build_outbox_post_canon_maintenance(
        self,
        core: CoreRuntimeServices,
    ) -> PostCanonMaintenanceService:
        from forwin.maintenance.post_canon import (
            PostCanonMaintenanceService,
            build_world_tick_service,
        )

        infrastructure = core.infrastructure
        policy = core.policy
        llm_client = self._build_llm_client(infrastructure, policy)
        try:
            llm_available = _llm_available(infrastructure, policy)
            comment_analyzer = CommentAnalyzer(
                llm_client=llm_client
                if policy.quality_profile == "standard" and llm_available else None
            )
            service = PostCanonMaintenanceService(
                session_factory=core.session_factory,
                comment_analyzer=comment_analyzer,
                artifact_store=core.artifact_store,
                llm_client=llm_client,
                plan_step=_ProjectPlanStep(self),
                world_tick=build_world_tick_service(
                    llm_client=llm_client,
                    session_factory=core.session_factory,
                    artifact_store=core.artifact_store,
                    llm_available=llm_available,
                ),
            )
        except Exception:
            llm_client.close()
            raise
        self._outbox_resources.append(llm_client)
        return service

    def _provide_outbox_memory_index(self):
        with self._lock:
            self._require_open()
            resource = self._build_memory_index(self.infrastructure)
            self._outbox_resources.append(resource)
        return resource

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("RuntimeContainer is closed")

    def _require_capability(
        self,
        capability: str,
        *,
        allowed_roles: set[str],
    ) -> None:
        self._require_open()
        if self.role not in allowed_roles:
            raise RuntimeError(
                f"Runtime role {self.role!r} cannot resolve {capability} services"
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
                "retention_cleanup_completed performance_spans=%s",
                result.performance_spans_deleted,
            )
        except Exception:
            logger.warning("retention_cleanup_failed", exc_info=True)

    @staticmethod
    def _build_llm_client(
        infrastructure: InfrastructureConfig,
        policy: RuntimePolicy,
    ):
        llm_client = LLMClient(
            profiles=infrastructure.llm_profiles,
            chains=policy.models.chains,
            pinned_profile_id=policy.model_profile_id,
            timeout_seconds=infrastructure.llm_timeout_seconds,
            retry_attempts=infrastructure.llm_retry_attempts,
            retry_initial_delay_seconds=(
                infrastructure.llm_retry_initial_delay_seconds
            ),
            retry_max_delay_seconds=infrastructure.llm_retry_max_delay_seconds,
            embedding_api_key=infrastructure.embedding_api_key,
            embedding_base_url=infrastructure.embedding_base_url,
            embedding_model=infrastructure.embedding_model,
        )
        return llm_client

    @staticmethod
    def _build_skill_runtime(config: InfrastructureConfig) -> SkillRuntimeBundle:
        _, router, prompt_layer_builder = build_skill_runtime_components(
            root=config.skill_registry_path,
            enabled=config.skill_runtime_enabled,
            strictness=config.skill_strictness,
            enabled_skill_groups=config.enabled_skill_groups,
            disabled_skill_ids=config.disabled_skill_ids,
        )
        return SkillRuntimeBundle(
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
        beat_sheets,
    ) -> BookGenesisService:
        service = BookGenesisService(
            llm_client=llm_client,
            max_tokens=min(config.max_tokens, 1600),
            world_seed_max_tokens=config.max_tokens,
            skill_router=skill_runtime.router,
            skill_prompt_layer_builder=skill_runtime.prompt_layer_builder,
            beat_sheets=beat_sheets,
        )
        setattr(service, "artifact_store", artifact_store)
        return service

    def _plan_step_service(self, llm_client, artifact_store, policy: RuntimePolicy):
        from forwin.planning.feasibility import FeasibilityAuditor
        from forwin.planning.plan_step import PlanStepService

        return PlanStepService(
            self._band_planner(llm_client, artifact_store, policy),
            FeasibilityAuditor(),
            ArcProgressionService(self._beat_sheet_service(llm_client, artifact_store)),
        )

    @staticmethod
    def _beat_sheet_service(llm_client, artifact_store):
        """``llm_client`` is None when no model is configured: every sheet is then the fallback."""
        from forwin.maintenance.post_canon import save_world_prompt_trace
        from forwin.planning.beat_sheet.service import ArcBeatSheetService

        return ArcBeatSheetService(
            llm_client,
            save_trace=partial(save_world_prompt_trace, artifact_store=artifact_store),
        )

    @staticmethod
    def _band_planner(llm_client, artifact_store, policy: RuntimePolicy):
        """``llm_client`` is None when no model is configured: no band is planned then."""
        from forwin.maintenance.post_canon import save_world_prompt_trace
        from forwin.planning.band_planner.service import BandPlanner
        from forwin.planning.feasibility import FeasibilityAuditor

        return BandPlanner(
            llm_client,
            feasibility=FeasibilityAuditor(),
            save_trace=partial(save_world_prompt_trace, artifact_store=artifact_store),
            trope_cost_ceiling=2 if policy.quality_profile == "pulp" else 3,
        )

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

    @staticmethod
    def _build_memory_index(config: InfrastructureConfig):
        return create_memory_index(
            backend=config.retrieval_backend,
            root_dir=config.retrieval_root,
            qdrant_url=config.qdrant_url,
            qdrant_collection=config.qdrant_collection,
            embedding_backend=config.embedding_backend,
            embedding_base_url=config.embedding_base_url,
            embedding_api_key=config.embedding_api_key,
            embedding_model=config.embedding_model,
            embedding_dims=config.embedding_dims,
            embedding_required=config.embedding_required,
        )


@dataclass(frozen=True)
class _ProjectPlanAmender:
    container: RuntimeContainer

    def amend(self, session, *, conflict, decision, policy: RuntimePolicy):
        return self.container.build_plan_amender(policy=policy).amend(
            session, conflict=conflict, decision=decision, policy=policy,
        )

    def propose_chapter_revision(self, session, *, conflict, decision, policy: RuntimePolicy):
        return self.container.build_plan_amender(policy=policy).propose_chapter_revision(
            session, conflict=conflict, decision=decision, policy=policy,
        )

    def publish_chapter_revision(self, session, proposal):
        return self.container.build_plan_amender().publish_chapter_revision(session, proposal)

    def record_world_amendment_failure(self, session, *, conflict, issues, policy: RuntimePolicy):
        return self.container.build_plan_amender(policy=policy).record_world_amendment_failure(
            session, conflict=conflict, issues=issues, policy=policy,
        )

    @staticmethod
    def error_issue(exc: BaseException):
        from forwin.planning.amend import PlanAmender

        return PlanAmender.error_issue(exc)


@dataclass(frozen=True)
class _ProjectPlanStep:
    container: RuntimeContainer

    def run(self, session, commit, *, policy: RuntimePolicy):
        # PostCanonMaintenanceService loads this project's current policy through ProjectPolicyStore.
        core = self.container.core_services()
        step = self.container._plan_step_service(
            self.container._project_planning_llm_client(policy), core.artifact_store, policy,
        )
        return step.run(session, commit, policy=policy)


def _validate_runtime_role(role: str) -> RuntimeRole:
    normalized = str(role or "").strip()
    if normalized not in _RUNTIME_ROLES:
        raise ValueError(f"Unsupported runtime role: {role}")
    return cast(RuntimeRole, normalized)


def llm_configured(infrastructure: InfrastructureConfig, policy: RuntimePolicy) -> bool:
    profiles = infrastructure.llm_profiles
    profile_ids = {profile_id for chain in policy.models.chains.values() for profile_id in chain}
    if policy.model_profile_id:
        profile_ids.add(policy.model_profile_id)
    return any(is_configured(profiles.get(profile_id)) for profile_id in profile_ids)


def _llm_available(infrastructure: InfrastructureConfig, policy: RuntimePolicy) -> bool:
    return llm_configured(infrastructure, policy)


def _close_resource(resource: object, label: str) -> None:
    close = getattr(resource, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception:  # noqa: BLE001
        logger.debug("Ignoring runtime %s shutdown error.", label, exc_info=True)
