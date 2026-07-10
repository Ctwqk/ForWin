from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from forwin.config import InfrastructureConfig
from forwin.model_adapter import ModelAdapter
from forwin.observability.ports import ObservabilityPort
from forwin.runtime.policy import RuntimePolicy
from forwin.skills import SkillPromptLayerBuilder, SkillRegistry, SkillRouter

if TYPE_CHECKING:
    from forwin.application.generation import GenerationApplicationService
    from forwin.canon import CanonAdmissionService
    from forwin.generation.gate_delegation import GateDelegationService
    from forwin.review import DraftReviewService
    from forwin.review.repair import RepairService, RepairVerifier


@dataclass(slots=True)
class SkillRuntimeBundle:
    registry: SkillRegistry
    router: SkillRouter
    prompt_layer_builder: SkillPromptLayerBuilder


@dataclass(slots=True)
class RuntimeServices:
    infrastructure: InfrastructureConfig
    policy: RuntimePolicy
    engine: Engine
    session_factory: sessionmaker
    llm_client: ModelAdapter
    skill_runtime: SkillRuntimeBundle
    generation_application: GenerationApplicationService

    arc_director: Any
    book_genesis: Any
    subworld_manager: Any
    retrieval_broker: Any
    artifact_store: Any
    observability: ObservabilityPort

    stage_analyzer: Any
    pacing_strategist: Any
    replan_governor: Any
    npc_intent_generator: Any
    world_simulator: Any

    arc_envelope_manager: Any
    genesis_workspace_service: Any
    genesis_handoff_service: Any
    production_scheduler: Any
    publisher_runtime: Any

    context_assembler: Any
    draft_review: DraftReviewService
    writer: Any
    provisional_writer: Any
    repair: RepairService
    repair_verifier: RepairVerifier
    canon_preparation: Any
    canon_admission: CanonAdmissionService
    gate_delegation: GateDelegationService
