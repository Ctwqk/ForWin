from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from forwin.application.errors import PermanentConfigurationError
from forwin.config import InfrastructureConfig, ModelProfileConfig
from forwin.runtime.policy import RuntimePolicy


ExecutionMode = Literal["initial", "continue"]


class GenerationTaskExecutionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: ExecutionMode
    premise: str = ""
    genre: str = ""
    num_chapters: int = 0
    auto_continue: bool = True
    root_event_id: str = ""
    run_until_chapter: int = 0
    max_chapters: int = 0
    policy_version: int = Field(ge=1)
    policy_snapshot: RuntimePolicy


@dataclass(frozen=True, slots=True)
class GenerationExecutionContext:
    infrastructure: InfrastructureConfig
    policy: RuntimePolicy
    model_profile: ModelProfileConfig
    task_id: str
    root_event_id: str


def execution_payload(
    *,
    mode: ExecutionMode,
    policy: RuntimePolicy,
    policy_version: int,
    root_event_id: str = "",
    premise: str = "",
    genre: str = "",
    num_chapters: int = 0,
    auto_continue: bool = True,
    run_until_chapter: int | None = None,
    max_chapters: int | None = None,
) -> GenerationTaskExecutionPayload:
    return GenerationTaskExecutionPayload(
        mode=mode,
        premise=str(premise or ""),
        genre=str(genre or ""),
        num_chapters=int(num_chapters or 0),
        auto_continue=bool(auto_continue),
        root_event_id=str(root_event_id or ""),
        run_until_chapter=int(run_until_chapter or 0),
        max_chapters=int(max_chapters or 0),
        policy_version=int(policy_version),
        policy_snapshot=policy,
    )


def payload_to_json(payload: GenerationTaskExecutionPayload) -> str:
    return payload.model_dump_json()


def payload_from_json(raw: str | None) -> GenerationTaskExecutionPayload:
    try:
        return GenerationTaskExecutionPayload.model_validate_json(str(raw or ""))
    except (ValueError, TypeError) as exc:
        raise PermanentConfigurationError(
            "generation task has no valid v5 policy snapshot"
        ) from exc


def build_execution_context(
    infrastructure: InfrastructureConfig,
    payload: GenerationTaskExecutionPayload,
    *,
    task_id: str,
) -> GenerationExecutionContext:
    policy = payload.policy_snapshot
    return GenerationExecutionContext(
        infrastructure=infrastructure,
        policy=policy,
        model_profile=infrastructure.resolve_model_profile(policy.model_profile_id),
        task_id=str(task_id or ""),
        root_event_id=str(payload.root_event_id or ""),
    )
