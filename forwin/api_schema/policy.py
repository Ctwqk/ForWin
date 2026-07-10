from __future__ import annotations

from pydantic import BaseModel, Field

from forwin.runtime.policy import (
    BandCheckpointAction,
    GateDelegate,
    QualityProfile,
    RuntimePolicy,
)


class RuntimePolicyUpdateRequest(BaseModel):
    expected_version: int = Field(ge=1)
    quality_profile: QualityProfile
    model_profile_id: str = ""
    min_chapter_chars: int = Field(ge=500, le=20000)
    target_chapter_chars: int = Field(ge=500, le=20000)
    max_chapter_chars: int = Field(ge=500, le=20000)
    review_interval_chapters: int = Field(ge=0, le=500)
    manual_checkpoints: bool
    band_checkpoint_action: BandCheckpointAction
    generation_audit_interval: int = Field(ge=0, le=500)
    generation_audit_pauses: bool
    gate_delegate: GateDelegate
    reason: str = Field(min_length=1)


class RuntimePolicyResponse(BaseModel):
    ok: bool = True
    project_id: str
    version: int
    policy: RuntimePolicy
    message: str = ""


__all__ = ["RuntimePolicyResponse", "RuntimePolicyUpdateRequest"]
