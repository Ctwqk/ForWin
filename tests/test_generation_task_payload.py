from __future__ import annotations

import json

from forwin.config import Config
from forwin.generation.task_payload import (
    GenerationTaskExecutionPayload,
    build_worker_config_from_payload,
    execution_payload_from_config,
)


def test_execution_payload_serializes_non_secret_runtime_overrides() -> None:
    config = Config(
        minimax_api_key="sk-secret",
        minimax_base_url="https://llm.example.test/v1",
        minimax_model="model-a",
        quality_profile="pulp",
        operation_mode="blackbox",
        publisher_session_secret="publisher-secret",
        codex_bridge_token="codex-secret",
    )

    payload = execution_payload_from_config(
        mode="continue",
        runtime_config=config,
        root_event_id="event-root",
        auto_continue=True,
        run_until_chapter=50,
        max_chapters=5,
    )
    raw = payload.model_dump(mode="json")

    assert raw["mode"] == "continue"
    assert raw["root_event_id"] == "event-root"
    assert raw["auto_continue"] is True
    assert raw["run_until_chapter"] == 50
    assert raw["max_chapters"] == 5
    assert raw["runtime_overrides"]["minimax_base_url"] == "https://llm.example.test/v1"
    assert raw["runtime_overrides"]["minimax_model"] == "model-a"
    assert raw["runtime_overrides"]["quality_profile"] == "pulp"
    assert "minimax_api_key" not in json.dumps(raw)
    assert "publisher-secret" not in json.dumps(raw)
    assert "codex-secret" not in json.dumps(raw)


def test_worker_config_uses_worker_secret_and_payload_generation_settings() -> None:
    base = Config(
        minimax_api_key="sk-worker",
        minimax_model="worker-default",
        operation_mode="blackbox",
    )
    payload = GenerationTaskExecutionPayload(
        mode="continue",
        runtime_overrides={
            "minimax_model": "queued-model",
            "quality_profile": "pulp",
            "operation_mode": "blackbox",
        },
        root_event_id="root-1",
    )

    config = build_worker_config_from_payload(
        base,
        payload,
        task_id="task-1",
    )

    assert config.minimax_api_key == "sk-worker"
    assert config.minimax_model == "queued-model"
    assert config.quality_profile == "pulp"
    assert config.governance_task_id == "task-1"
    assert config.governance_causal_root_id == "root-1"
