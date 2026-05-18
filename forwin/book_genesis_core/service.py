from __future__ import annotations

from forwin.book_genesis_core.constants import *
from forwin.book_genesis_core.helpers import *
from forwin.book_genesis_core.fallbacks import *
from forwin.book_genesis_core.names_paths import *

from forwin.book_genesis_core.messages import _build_stage_generation_messages, _build_stage_refine_messages
from forwin.book_genesis_core.workflow import create_initial_revision, active_revision, load_pack, patch_pack, generate_stage, refine_stage, lock_stage, build_detail, generate_name_suggestions, _resolve_name_generation_profile
from forwin.book_genesis_core.materialize import materialize_book_arcs, materialize_arc_chapter_plans, _ensure_arc_map_expansion, promote_next_arc_if_needed
from forwin.book_genesis_core.llm import _generate_stage_payload, _refine_stage_payload, _call_json_with_trace, _call_json_with_trace_impl, _call_llm_chat, _resolve_skill_layers, _trace_payload, _prepare_trace_payload_for_save, _record_llm_events_for_trace, _record_trace_performance_spans
from forwin.book_genesis_core.normalize import _normalize_world_payload, _normalize_world_root_payload, _normalize_scope_profile, _normalize_blueprint_payload, _normalize_map_payload, _normalize_story_engine_payload
from forwin.book_genesis_core.planning import _refine_support_context, _plan_arc_chapters


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



BookGenesisService._build_stage_generation_messages = _build_stage_generation_messages
BookGenesisService._build_stage_refine_messages = _build_stage_refine_messages
BookGenesisService.create_initial_revision = create_initial_revision
BookGenesisService.active_revision = active_revision
BookGenesisService.load_pack = load_pack
BookGenesisService.patch_pack = patch_pack
BookGenesisService.generate_stage = generate_stage
BookGenesisService.refine_stage = refine_stage
BookGenesisService.lock_stage = lock_stage
BookGenesisService.build_detail = build_detail
BookGenesisService.generate_name_suggestions = generate_name_suggestions
BookGenesisService._resolve_name_generation_profile = _resolve_name_generation_profile
BookGenesisService.materialize_book_arcs = materialize_book_arcs
BookGenesisService.materialize_arc_chapter_plans = materialize_arc_chapter_plans
BookGenesisService._ensure_arc_map_expansion = _ensure_arc_map_expansion
BookGenesisService.promote_next_arc_if_needed = promote_next_arc_if_needed
BookGenesisService._generate_stage_payload = _generate_stage_payload
BookGenesisService._refine_stage_payload = _refine_stage_payload
BookGenesisService._call_json_with_trace = _call_json_with_trace
BookGenesisService._call_json_with_trace_impl = _call_json_with_trace_impl
BookGenesisService._call_llm_chat = _call_llm_chat
BookGenesisService._resolve_skill_layers = _resolve_skill_layers
BookGenesisService._trace_payload = _trace_payload
BookGenesisService._prepare_trace_payload_for_save = _prepare_trace_payload_for_save
BookGenesisService._record_llm_events_for_trace = _record_llm_events_for_trace
BookGenesisService._record_trace_performance_spans = _record_trace_performance_spans
BookGenesisService._normalize_world_payload = _normalize_world_payload
BookGenesisService._normalize_world_root_payload = _normalize_world_root_payload
BookGenesisService._normalize_scope_profile = _normalize_scope_profile
BookGenesisService._normalize_blueprint_payload = _normalize_blueprint_payload
BookGenesisService._normalize_map_payload = _normalize_map_payload
BookGenesisService._normalize_story_engine_payload = _normalize_story_engine_payload
BookGenesisService._refine_support_context = _refine_support_context
BookGenesisService._plan_arc_chapters = _plan_arc_chapters

__all__ = ["BookGenesisService"]
