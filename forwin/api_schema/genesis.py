from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from forwin.config import DEFAULT_MINIMAX_BASE_URL, DEFAULT_MINIMAX_MODEL
from forwin.governance import (
    BandCheckpointDetail,
    BlockingReasonInfo,
    DecisionEventInfo,
    NarrativeConstraintInfo,
    PlanTaskItem,
    ProjectGovernanceSettings,
)
from forwin.protocol.subworld import SubWorldSummary


class BookGenesisStageState(BaseModel):
    stage_key: str
    status: str = "todo"
    locked: bool = False
    updated_at: str = ""
    last_trace_id: str = ""


class BookGenesisPack(BaseModel):
    book_brief: dict[str, Any] = Field(default_factory=dict)
    world: dict[str, Any] = Field(default_factory=dict)
    book_arc_blueprint: dict[str, Any] = Field(default_factory=dict)
    subworld_policy: dict[str, Any] = Field(default_factory=dict)
    execution_bootstrap: dict[str, Any] = Field(default_factory=dict)
    stage_states: dict[str, BookGenesisStageState] = Field(default_factory=dict)


class PromptTraceInfo(BaseModel):
    id: str
    trace_scope: str = "genesis"
    stage_key: str = ""
    template_id: str = ""
    template_version: str = "v1"
    effective_system_prompt: str = ""
    prompt_layers: list[dict[str, Any]] = Field(default_factory=list)
    input_snapshot: dict[str, Any] = Field(default_factory=dict)
    model_profile: dict[str, Any] = Field(default_factory=dict)
    attempts: list[dict[str, Any]] = Field(default_factory=list)
    output_summary: dict[str, Any] = Field(default_factory=dict)
    decision_event_id: str = ""
    parent_trace_id: str = ""
    created_at: str = ""


class BookGenesisDetail(BaseModel):
    project_id: str
    creation_status: str = "creating"
    active_genesis_revision_id: str = ""
    revision: int = 1
    pack: BookGenesisPack = Field(default_factory=BookGenesisPack)
    prompt_traces: list[PromptTraceInfo] = Field(default_factory=list)
    can_start_writing: bool = False


class BookGenesisPatchRequest(BaseModel):
    book_brief: dict[str, Any] | None = None
    world: dict[str, Any] | None = None
    book_arc_blueprint: dict[str, Any] | None = None
    subworld_policy: dict[str, Any] | None = None
    execution_bootstrap: dict[str, Any] | None = None
    stage_states: dict[str, Any] | None = None
    reason: str = ""


class BookGenesisStageRunRequest(BaseModel):
    model_profile_id: str = ""


class BookGenesisRefineRequest(BaseModel):
    instruction: str = ""
    target_path: str = ""
    reason: str = ""
    model_profile_id: str = ""


class BookGenesisNameGenerateRequest(BaseModel):
    stage_key: str = ""
    target_path: str = ""
    field_path: str = ""
    kind: str = ""
    count: int = 1
    nonce: str = ""
    stage_payload_override: dict[str, Any] | None = None


class BookGenesisNameGenerateResponse(BaseModel):
    ok: bool = True
    stage_key: str = ""
    target_path: str = ""
    field_path: str = ""
    kind: str = ""
    suggestions: list[str] = Field(default_factory=list)
    applied_value: Any = None
    culture_profile_id: str = ""
    culture_profile_name: str = ""
    generator_civilization: str = ""
    message: str = ""


class StartWritingRequest(BaseModel):
    auto_continue: bool | None = None
    run_until_chapter: int | None = Field(default=None, ge=1)
    max_chapters: int | None = Field(default=None, ge=1)


class StartWritingResponse(BaseModel):
    ok: bool
    project_id: str
    creation_status: str = "writing"
    task_id: str = ""
    message: str = ""


__all__ = [
    'BookGenesisStageState',
    'BookGenesisPack',
    'PromptTraceInfo',
    'BookGenesisDetail',
    'BookGenesisPatchRequest',
    'BookGenesisStageRunRequest',
    'BookGenesisRefineRequest',
    'BookGenesisNameGenerateRequest',
    'BookGenesisNameGenerateResponse',
    'StartWritingRequest',
    'StartWritingResponse',
]
