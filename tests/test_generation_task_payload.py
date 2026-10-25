from __future__ import annotations

import json

import pytest

from forwin.application.errors import PermanentConfigurationError
from forwin.config import InfrastructureConfig
from forwin.generation.task_payload import (
    GenerationTaskExecutionPayload,
    build_execution_context,
    execution_payload,
    payload_from_json,
)
from forwin.runtime.policy import DEFAULT_LLM_CHAINS, RuntimePolicy


ENV_PROFILE = {
    "id": "env-kimi",
    "name": "Kimi (.env)",
    "api_key": "secret",
    "base_url": "https://api.moonshot.cn/v1",
    "model": "kimi-k2.5",
}


def test_execution_payload_serializes_exact_non_secret_policy() -> None:
    policy = RuntimePolicy.for_profile(
        "standard", model_profile_id="env-kimi"
    ).with_user_settings(gate_delegate="spark")

    payload = execution_payload(
        mode="continue",
        policy=policy,
        policy_version=3,
        root_event_id="root-1",
        max_chapters=5,
    )
    raw = payload.model_dump(mode="json")

    assert raw["policy_version"] == 3
    assert raw["policy_snapshot"]["pause"]["gate_delegate"] == "spark"
    assert "runtime_overrides" not in raw
    assert "api_key" not in json.dumps(raw)


@pytest.mark.parametrize("profile, actor_turns", [("standard", 6), ("pulp", 3)])
def test_legacy_policy_snapshot_loads_world_and_models_defaults(profile, actor_turns) -> None:
    payload = execution_payload(
        mode="continue",
        policy=RuntimePolicy.for_profile(profile),
        policy_version=1,
        root_event_id="root-legacy",
    ).model_dump(mode="json")
    payload["policy_snapshot"].pop("world", None)
    payload["policy_snapshot"].pop("models", None)

    restored = payload_from_json(json.dumps(payload))

    assert restored.policy_snapshot.world.conflict_delegate == "codex"
    assert restored.policy_snapshot.world.max_actor_turns_per_tick == actor_turns
    assert restored.policy_snapshot.models.chains == DEFAULT_LLM_CHAINS


def test_worker_context_resolves_credentials_without_mutating_policy() -> None:
    infrastructure = InfrastructureConfig(llm_env_profiles=[ENV_PROFILE])
    payload = GenerationTaskExecutionPayload(
        mode="continue",
        policy_version=2,
        policy_snapshot=RuntimePolicy.for_profile(
            "pulp", model_profile_id="env-kimi"
        ),
        root_event_id="root-1",
    )

    context = build_execution_context(infrastructure, payload, task_id="task-1")

    assert context.model_profile.api_key == "secret"
    assert context.policy == payload.policy_snapshot
    assert context.task_id == "task-1"


@pytest.mark.parametrize("raw", [None, "", "{}", '{"mode":"continue"}'])
def test_payload_without_v5_policy_snapshot_fails_closed(raw: str | None) -> None:
    with pytest.raises(
        PermanentConfigurationError,
        match="generation task has no valid v5 policy snapshot",
    ):
        payload_from_json(raw)
