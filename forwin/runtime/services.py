from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from forwin.config import InfrastructureConfig
from forwin.model_adapter import ModelAdapter
from forwin.observability.ports import ObservabilityPort
from forwin.runtime.policy import RuntimePolicy
from forwin.skills import SkillPromptLayerBuilder, SkillRouter

if TYPE_CHECKING:
    from forwin.application.generation import GenerationApplicationService
    from forwin.canon import CanonAdmissionService
    from forwin.generation.gate_delegation import GateDelegationService
    from forwin.review.draft_service import DraftReviewService
    from forwin.review.repair import RepairService, RepairVerifier


@dataclass(slots=True)
class SkillRuntimeBundle:
    router: SkillRouter
    prompt_layer_builder: SkillPromptLayerBuilder


@dataclass(slots=True)
class CoreRuntimeServices:
    infrastructure: InfrastructureConfig
    policy: RuntimePolicy
    engine: Engine
    session_factory: sessionmaker
    generation_application: GenerationApplicationService
    artifact_store: Any
    observability: ObservabilityPort


@dataclass(slots=True)
class GenerationRuntimeServices:
    llm_client: ModelAdapter
    skill_runtime: SkillRuntimeBundle
    arc_director: Any
    book_genesis: Any
    subworld_manager: Any
    retrieval_broker: Any
    stage_analyzer: Any
    pacing_strategist: Any
    replan_governor: Any
    world_simulator: Any
    arc_envelope_manager: Any
    draft_review: DraftReviewService
    writer: Any
    repair: RepairService
    repair_verifier: RepairVerifier
    canon_preparation: Any
    canon_admission: CanonAdmissionService
    gate_delegation: GateDelegationService


@dataclass(slots=True)
class PublisherRuntimeServices:
    publisher_runtime: Any
    production_scheduler: Any
