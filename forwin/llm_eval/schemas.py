from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


ExpectedOutputKind = Literal["json", "tagged_prose", "prose"]


class EvalProfile(BaseModel):
    id: str
    name: str = ""
    provider: str = "openai_compatible"
    base_url: str
    model: str
    api_key: str = Field(default="", repr=False)
    api_key_env: str = ""
    rate_limit_per_minute: int = 10
    concurrency: int = 1
    timeout_seconds: float = 90.0

    def display_name(self) -> str:
        return self.name or self.id or self.model


class EvalCase(BaseModel):
    case_id: str
    stage_key: str
    task_family: str
    expected_output_kind: ExpectedOutputKind = "json"
    schema_name: str = ""
    messages: list[dict[str, Any]] = Field(default_factory=list)
    input_snapshot: dict[str, Any] = Field(default_factory=dict)
    variant_seed: str = ""
    temperature: float = 0.3
    max_tokens: int = 1200
    response_format: dict[str, Any] | None = None


class EvalValidationResult(BaseModel):
    parse_ok: bool = False
    schema_ok: bool = False
    required_keys_missing: list[str] = Field(default_factory=list)
    normalized_output_hash: str = ""
    output_chars: int = 0
    error_message: str = ""


class EvalAttemptResult(BaseModel):
    run_id: str
    profile_id: str
    case_id: str
    stage_key: str
    task_family: str
    attempt_group_id: str = ""
    http_status: int = 0
    error_category: str = ""
    timeout_kind: str = ""
    duration_ms: int = 0
    retry_count: int = 0
    input_chars: int = 0
    output_chars: int = 0
    temperature: float | None = None
    requested_temperature: float | None = None
    max_tokens: int | None = None
    requested_max_tokens: int | None = None
    parse_ok: bool = False
    schema_ok: bool = False
    required_keys_missing: list[str] = Field(default_factory=list)
    output_hash: str = ""
    response_artifact_uri: str = ""
    provider_request_id: str = ""
    error_class: str = ""
    error_message: str = ""
    clean_success: bool = False
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class EvalRunConfig(BaseModel):
    run_id: str
    artifact_root: str = "data/artifacts"
    suite: str = "medium"
    live: bool = False
    include_mini_real_run: bool = True
    request_interval_seconds: float = 6.0
    burst_every_seconds: float = 120.0
    burst_size: int = 3
    warmup_rounds: int = 1
    rounds: int = 1
    debug_save_redacted_io: bool = False
    allow_production_data: bool = False
    base_url: str = ""
