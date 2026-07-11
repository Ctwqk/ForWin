from __future__ import annotations

from typing import Any
from forwin.models.genesis import (
    BookGenesisRevision,
    PromptTrace,
)
from forwin.audit.events import DecisionEventType
from forwin.genesis.handoff import GenesisHandoffService
from forwin.genesis.workspace.trace_service import GenesisTraceService
from forwin.genesis.workspace import GenesisWorkspaceService
from forwin.model_adapter import ModelAdapter
from forwin.observability.ports import NullObservability
from forwin.models.project import Project
from sqlalchemy.orm import Session
from forwin.skills import (
    SkillPromptLayerBuilder,
    SkillRouter,
)
from forwin.state.updater import StateUpdater

from forwin.genesis.messages import (
    _build_stage_generation_messages,
    _build_stage_refine_messages,
)
from forwin.genesis.materialize import (
    materialize_book_arcs,
    materialize_arc_chapter_plans,
    _ensure_arc_map_expansion,
    promote_next_arc_if_needed,
)
from forwin.genesis.llm import (
    _generate_stage_payload,
    _refine_stage_payload,
    _call_json_with_trace,
    _call_json_with_trace_impl,
    _call_llm_chat,
    _resolve_skill_layers,
    _trace_payload,
    _prepare_trace_payload_for_save,
    _record_llm_events_for_trace,
    _record_trace_performance_spans,
)
from forwin.genesis.normalize import (
    _normalize_world_payload,
    _normalize_world_root_payload,
    _normalize_scope_profile,
    _normalize_blueprint_payload,
    _normalize_map_payload,
    _normalize_story_engine_payload,
)
from forwin.genesis.planning import _refine_support_context, _plan_arc_chapters


class BookGenesisService:
    def __init__(
        self,
        *,
        llm_client: ModelAdapter,
        max_tokens: int = 1600,
        skill_router: SkillRouter | None = None,
        skill_prompt_layer_builder: SkillPromptLayerBuilder | None = None,
        artifact_store: object | None = None,
    ) -> None:
        self.llm_client = llm_client
        self.max_tokens = max_tokens
        self.skill_router = skill_router
        self.skill_prompt_layer_builder = skill_prompt_layer_builder
        self.artifact_store = artifact_store
        self.observability = NullObservability()
        self.trace_service = GenesisTraceService(self)
        self.workspace = GenesisWorkspaceService(self)
        self.handoff = GenesisHandoffService(self)

    def create_initial_revision(
        self,
        *,
        session: Session,
        updater: StateUpdater,
        project: Project,
        brief_seed: dict[str, Any] | None = None,
    ) -> BookGenesisRevision:
        return self.workspace.create_initial_revision(
            session=session,
            updater=updater,
            project=project,
            brief_seed=brief_seed,
        )

    def active_revision(
        self,
        session: Session,
        project: Project,
    ) -> BookGenesisRevision | None:
        return self.workspace.active_revision(session, project)

    def load_pack(self, revision: BookGenesisRevision | None) -> dict[str, Any]:
        return self.workspace.load_pack(revision)

    def patch_pack(
        self,
        *,
        session: Session,
        updater: StateUpdater,
        project: Project,
        revision: BookGenesisRevision,
        patch: dict[str, Any],
        reason: str = "",
    ) -> BookGenesisRevision:
        return self.workspace.patch_pack(
            session=session,
            updater=updater,
            project=project,
            revision=revision,
            patch=patch,
            reason=reason,
        )

    def generate_stage(
        self,
        *,
        session: Session,
        updater: StateUpdater,
        project: Project,
        revision: BookGenesisRevision,
        stage_key: str,
        event_type: str = DecisionEventType.GENESIS_STAGE_GENERATED,
    ) -> tuple[BookGenesisRevision, PromptTrace]:
        return self.workspace.generate_stage(
            session=session,
            updater=updater,
            project=project,
            revision=revision,
            stage_key=stage_key,
            event_type=event_type,
        )

    def refine_stage(
        self,
        *,
        session: Session,
        updater: StateUpdater,
        project: Project,
        revision: BookGenesisRevision,
        stage_key: str,
        instruction: str,
        target_path: str = "",
        reason: str = "",
    ) -> tuple[BookGenesisRevision, PromptTrace]:
        return self.workspace.refine_stage(
            session=session,
            updater=updater,
            project=project,
            revision=revision,
            stage_key=stage_key,
            instruction=instruction,
            target_path=target_path,
            reason=reason,
        )

    def lock_stage(
        self,
        *,
        session: Session,
        updater: StateUpdater,
        project: Project,
        revision: BookGenesisRevision,
        stage_key: str,
    ) -> BookGenesisRevision:
        return self.workspace.lock_stage(
            session=session,
            updater=updater,
            project=project,
            revision=revision,
            stage_key=stage_key,
        )

    def build_detail(
        self,
        *,
        session: Session,
        project: Project,
    ) -> dict[str, Any]:
        return self.workspace.build_detail(session=session, project=project)

    def generate_name_suggestions(
        self,
        *,
        project: Project,
        revision: BookGenesisRevision,
        stage_key: str,
        target_path: str,
        field_path: str,
        kind: str = "",
        count: int = 1,
        nonce: str = "",
        stage_payload_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.workspace.generate_name_suggestions(
            project=project,
            revision=revision,
            stage_key=stage_key,
            target_path=target_path,
            field_path=field_path,
            kind=kind,
            count=count,
            nonce=nonce,
            stage_payload_override=stage_payload_override,
        )

    _build_stage_generation_messages = _build_stage_generation_messages
    _build_stage_refine_messages = _build_stage_refine_messages
    materialize_book_arcs = materialize_book_arcs
    materialize_arc_chapter_plans = materialize_arc_chapter_plans
    _ensure_arc_map_expansion = _ensure_arc_map_expansion
    promote_next_arc_if_needed = promote_next_arc_if_needed
    _generate_stage_payload = _generate_stage_payload
    _refine_stage_payload = _refine_stage_payload
    _call_json_with_trace = _call_json_with_trace
    _call_json_with_trace_impl = _call_json_with_trace_impl
    _call_llm_chat = _call_llm_chat
    _resolve_skill_layers = _resolve_skill_layers
    _trace_payload = _trace_payload
    _prepare_trace_payload_for_save = _prepare_trace_payload_for_save
    _record_llm_events_for_trace = _record_llm_events_for_trace
    _record_trace_performance_spans = _record_trace_performance_spans
    _normalize_world_payload = _normalize_world_payload
    _normalize_world_root_payload = _normalize_world_root_payload
    _normalize_scope_profile = _normalize_scope_profile
    _normalize_blueprint_payload = _normalize_blueprint_payload
    _normalize_map_payload = _normalize_map_payload
    _normalize_story_engine_payload = _normalize_story_engine_payload
    _refine_support_context = _refine_support_context
    _plan_arc_chapters = _plan_arc_chapters


__all__ = ["BookGenesisService"]
