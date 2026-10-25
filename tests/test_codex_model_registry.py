from __future__ import annotations

from unittest.mock import patch

import pytest

from forwin.config import InfrastructureConfig
from forwin.llm.factory import maybe_wrap_with_codex_router
from forwin.llm.router import LLMCallRouter, RoutedModelAdapter
from forwin.writer.llm import LLMClient


class OrdinaryAdapter:
    model = "ordinary-model"

    def chat(self, messages, **kwargs):
        return "ordinary"


class FakeCodexClient:
    def __init__(self):
        self.calls = []
        self.last_call_trace = {}

    def chat(self, messages, *, intent, **kwargs):
        self.calls.append(kwargs)
        self.last_call_trace = {"actual_model": kwargs["model"]}
        return "codex"


def test_declared_model_routes_to_codex_bridge():
    codex = FakeCodexClient()
    adapter = RoutedModelAdapter(
        LLMCallRouter(
            ordinary_adapter=OrdinaryAdapter(), codex_client=codex, codex_enabled=True,
        ),
        codex_models=("gpt-6-astra",),
    )

    assert adapter.chat([], task_family="review", preferred_model="gpt-6-astra") == "codex"
    assert codex.calls[0]["model"] == "gpt-6-astra"
    assert adapter.last_call_result.backend == "codex_bridge"
    assert adapter.last_call_result.trace["actual_model"] == "gpt-6-astra"


@pytest.mark.parametrize("model", ["custom-codex-like", "gpt-5.3-custom", "gpt-5.3-codex-spark"])
def test_undeclared_model_is_not_codex_even_with_codex_substring(model):
    codex = FakeCodexClient()
    adapter = RoutedModelAdapter(
        LLMCallRouter(
            ordinary_adapter=OrdinaryAdapter(), codex_client=codex, codex_enabled=True,
        ),
        codex_models=("gpt-6-astra",),
    )

    assert adapter.chat([], task_family="review", preferred_model=model) == "ordinary"
    assert codex.calls == []


def test_profile_kind_spark_comes_from_registry():
    profile = {"model": " GPT-6-ASTRA ", "base_url": "https://openai.example/v1"}
    assert LLMClient._profile_kind(profile, codex_models=(" gpt-6-astra ",)) == "spark"
    assert LLMClient._profile_kind(profile, codex_models=()) == "openai"
    assert LLMClient._profile_kind(
        {"id": "codex-spark", "name": "gpt-5.3-codex-spark", "model": "kimi-k2.5"},
        codex_models=("gpt-6-astra",),
    ) == "kimi"


def test_env_csv_parses_codex_models(monkeypatch, tmp_path):
    monkeypatch.setenv("FORWIN_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.setenv("FORWIN_CODEX_MODELS", " gpt-6-astra , gpt-5.3-codex-spark ")
    assert InfrastructureConfig.from_env().codex_models == ("gpt-6-astra", "gpt-5.3-codex-spark")


@pytest.mark.parametrize(
    ("model", "registry", "expected"),
    [
        (" GPT-6-ASTRA ", (" gpt-6-astra ",), True),
        ("gpt-6-astra-preview", ("gpt-6-astra",), False),
        ("astra", ("gpt-6-astra",), False),
        ("custom-codex-like", ("gpt-6-astra",), False),
        ("gpt-6-astra", (), False),
    ],
)
def test_registry_matches_exact_normalized_models(model, registry, expected):
    from forwin.llm.codex_models import is_codex_model

    assert is_codex_model(model, iter(registry)) is expected


@pytest.mark.parametrize("provider_kind", ["codex", "codex_bridge", "spark"])
def test_explicit_provider_kind_still_routes_undeclared_model(provider_kind):
    codex = FakeCodexClient()
    adapter = RoutedModelAdapter(
        LLMCallRouter(
            ordinary_adapter=OrdinaryAdapter(), codex_client=codex, codex_enabled=True,
        ),
        codex_models=(),
    )
    assert adapter.chat(
        [], task_family="review", preferred_provider_kind=provider_kind, preferred_model="custom-model",
    ) == "codex"
    assert codex.calls[0]["model"] == "custom-model"


@pytest.mark.parametrize(
    ("enabled", "family", "allowed"),
    [(False, "review", True), (True, "world_engine", True), (True, "review", False)],
)
def test_registry_does_not_bypass_codex_policy(enabled, family, allowed):
    codex = FakeCodexClient()
    adapter = RoutedModelAdapter(
        LLMCallRouter(
            ordinary_adapter=OrdinaryAdapter(), codex_client=codex, codex_enabled=enabled,
        ),
        codex_models=("gpt-6-astra",),
    )
    assert adapter.chat(
        [], task_family=family, preferred_model="gpt-6-astra", codex_allowed=allowed,
    ) == "ordinary"
    assert codex.calls == []


def test_factory_passes_configured_registry():
    codex = FakeCodexClient()
    config = InfrastructureConfig(codex_enabled=True, codex_models=("custom-review",))
    with patch("forwin.llm.factory.CodexBridgeClient", return_value=codex):
        adapter = maybe_wrap_with_codex_router(OrdinaryAdapter(), config)
    assert adapter.chat([], task_family="review", preferred_model="custom-review") == "codex"
    assert codex.calls[0]["model"] == "custom-review"
    assert adapter.chat([], task_family="review", preferred_model="gpt-6-astra") == "ordinary"


def test_default_registry_supports_astra_and_spark():
    config = InfrastructureConfig()
    assert config.codex_models == ("gpt-5.3-codex-spark", "gpt-6-luna")
    for model in config.codex_models:
        assert LLMClient._profile_kind({"model": model}) == "spark"
