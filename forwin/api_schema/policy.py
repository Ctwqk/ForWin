from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from forwin.config import InfrastructureConfig, ModelProfileConfig
from forwin.runtime.policy import (
    BandCheckpointAction,
    GateDelegate,
    QualityProfile,
    RuntimePolicy,
)

from .llm import ModelProfile


class RuntimePolicyUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    quality_profile: QualityProfile
    model_profile_id: str = ""
    min_chapter_chars: int = Field(ge=500, le=20000)
    target_chapter_chars: int = Field(ge=500, le=20000)
    max_chapter_chars: int = Field(ge=500, le=20000)
    review_interval_chapters: int = Field(ge=0, le=500)
    manual_checkpoints: bool
    band_checkpoint_action: BandCheckpointAction
    gate_delegate: GateDelegate
    reason: str = Field(min_length=1)


class RuntimePolicyResponse(BaseModel):
    ok: bool = True
    project_id: str
    version: int
    policy: RuntimePolicy
    message: str = ""


class RuntimeCatalogResponse(BaseModel):
    model_profiles: list[ModelProfile]
    default_model_profile_id: str
    bootstrap_policy: RuntimePolicy


def runtime_catalog(config: InfrastructureConfig) -> RuntimeCatalogResponse:
    resolved_profiles = [
        config.resolve_model_profile(""),
        *(
            ModelProfileConfig.model_validate(item)
            for item in config.llm_env_profiles
        ),
    ]
    unique_profiles: dict[str, ModelProfileConfig] = {}
    for profile in resolved_profiles:
        unique_profiles.setdefault(profile.id, profile)
    profiles = [
        ModelProfile(
            id=profile.id,
            name=profile.name,
            has_api_key=bool(profile.api_key),
            base_url=profile.base_url,
            model=profile.model,
        )
        for profile in unique_profiles.values()
    ]
    return RuntimeCatalogResponse(
        model_profiles=profiles,
        default_model_profile_id="env-minimax",
        bootstrap_policy=RuntimePolicy.for_profile("standard"),
    )


__all__ = [
    "RuntimeCatalogResponse",
    "RuntimePolicyResponse",
    "RuntimePolicyUpdateRequest",
    "runtime_catalog",
]
