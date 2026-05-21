from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from forwin.api_runtime import copy_config
from forwin.config import Config


ExecutionMode = Literal["initial", "continue"]


SECRET_CONFIG_FIELDS = {
    "database_url",
    "minimax_api_key",
    "minio_access_key",
    "minio_secret_key",
    "publisher_extension_api_key",
    "publisher_session_secret",
    "http_basic_password",
    "codex_bridge_token",
}


class GenerationTaskExecutionPayload(BaseModel):
    mode: ExecutionMode
    premise: str = ""
    genre: str = ""
    num_chapters: int = 0
    auto_continue: bool = True
    root_event_id: str = ""
    model_profile_id: str = ""
    run_until_chapter: int = 0
    max_chapters: int = 0
    runtime_overrides: dict[str, Any] = Field(default_factory=dict)


def runtime_overrides_from_config(config: Config) -> dict[str, Any]:
    raw = config.model_dump(mode="json")
    return {
        key: value
        for key, value in raw.items()
        if key not in SECRET_CONFIG_FIELDS
    }


def execution_payload_from_config(
    *,
    mode: ExecutionMode,
    runtime_config: Config,
    root_event_id: str = "",
    premise: str = "",
    genre: str = "",
    num_chapters: int = 0,
    auto_continue: bool = True,
    run_until_chapter: int | None = None,
    max_chapters: int | None = None,
    model_profile_id: str = "",
) -> GenerationTaskExecutionPayload:
    return GenerationTaskExecutionPayload(
        mode=mode,
        premise=str(premise or ""),
        genre=str(genre or ""),
        num_chapters=int(num_chapters or 0),
        auto_continue=bool(auto_continue),
        root_event_id=str(root_event_id or ""),
        model_profile_id=str(model_profile_id or ""),
        run_until_chapter=int(run_until_chapter or 0),
        max_chapters=int(max_chapters or 0),
        runtime_overrides=runtime_overrides_from_config(runtime_config),
    )


def payload_to_json(payload: GenerationTaskExecutionPayload) -> str:
    return payload.model_dump_json()


def payload_from_json(raw: str | None) -> GenerationTaskExecutionPayload:
    if not raw:
        return GenerationTaskExecutionPayload(mode="continue")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return GenerationTaskExecutionPayload(mode="continue")
    if not isinstance(payload, dict):
        return GenerationTaskExecutionPayload(mode="continue")
    return GenerationTaskExecutionPayload.model_validate(payload)


def build_worker_config_from_payload(
    base_config: Config,
    payload: GenerationTaskExecutionPayload,
    *,
    task_id: str,
) -> Config:
    overrides = dict(payload.runtime_overrides)
    overrides["governance_task_id"] = str(task_id or "")
    overrides["governance_causal_root_id"] = str(payload.root_event_id or "")
    for key in SECRET_CONFIG_FIELDS:
        overrides.pop(key, None)
    return copy_config(base_config, **overrides)
