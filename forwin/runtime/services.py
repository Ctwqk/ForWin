from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from forwin.config import Config
from forwin.model_adapter import ModelAdapter
from forwin.observability.ports import ObservabilityPort
from forwin.skills import SkillPromptLayerBuilder, SkillRegistry, SkillRouter


@dataclass(slots=True)
class SkillRuntimeBundle:
    registry: SkillRegistry
    router: SkillRouter
    prompt_layer_builder: SkillPromptLayerBuilder


@dataclass(slots=True)
class RuntimeServices:
    config: Config
    engine: Engine
    session_factory: sessionmaker
    llm_client: ModelAdapter
    skill_runtime: SkillRuntimeBundle

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
    experience_planning_service: Any
    band_plan_service: Any
    world_contract_service: Any
    genesis_workspace_service: Any
    genesis_handoff_service: Any
    production_scheduler: Any
    publisher_runtime: Any

    context_assembler: Any
    review_hub: Any
    writer: Any
    provisional_writer: Any
    repair_verifier: Any
