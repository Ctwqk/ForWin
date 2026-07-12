from __future__ import annotations

from pathlib import Path

from forwin.config import InfrastructureConfig
from tests.http_runtime_harness import HttpRuntimeHarness


def test_config_builds_kimi_and_deepseek_profiles_from_env(monkeypatch) -> None:
    monkeypatch.setenv("KIMI_API_KEY", "secret-kimi")
    monkeypatch.setenv("KIMI_MODEL", "kimi-k2.5")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret-deepseek")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-chat")

    config = InfrastructureConfig.from_env()
    profiles = {item["id"]: item for item in config.llm_env_profiles}

    assert profiles["env-kimi"]["api_key"] == "secret-kimi"
    assert profiles["env-kimi"]["base_url"] == "https://api.moonshot.cn/v1"
    assert profiles["env-kimi"]["model"] == "kimi-k2.5"
    assert profiles["env-deepseek"]["api_key"] == "secret-deepseek"
    assert profiles["env-deepseek"]["base_url"] == "https://api.deepseek.com/v1"
    assert profiles["env-deepseek"]["model"] == "deepseek-chat"


def test_config_builds_provider_profiles_from_env_file(monkeypatch, tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "KIMI_API_KEY=file-kimi",
                "KIMI_MODEL=kimi-k2.5",
                "DEEPSEEK_API_KEY=file-deepseek",
                "DEEPSEEK_MODEL=deepseek-chat",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("FORWIN_ENV_FILE", str(env_path))
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    config = InfrastructureConfig.from_env()
    profiles = {item["id"]: item for item in config.llm_env_profiles}

    assert profiles["env-kimi"]["api_key"] == "file-kimi"
    assert profiles["env-deepseek"]["api_key"] == "file-deepseek"


def test_runtime_catalog_is_read_only_and_secret_free() -> None:
    api = HttpRuntimeHarness(
        config=InfrastructureConfig(
            minimax_api_key="secret-minimax-env",
            llm_env_profiles=[
                {
                    "id": "env-kimi",
                    "name": "Kimi (.env)",
                    "api_key": "secret-kimi-env",
                    "base_url": "https://api.moonshot.cn/v1",
                    "model": "kimi-k2.5",
                }
            ],
        )
    )
    response = api.get_runtime_catalog()

    assert response.bootstrap_policy.quality_profile == "standard"
    profiles = {profile.id: profile for profile in response.model_profiles}
    assert response.default_model_profile_id == "env-minimax"
    assert set(profiles) == {"env-minimax", "env-kimi"}
    assert profiles["env-minimax"].has_api_key is True
    assert profiles["env-kimi"].has_api_key is True
    assert not hasattr(profiles["env-minimax"], "api_key")
    assert "secret-kimi-env" not in response.model_dump_json()
    assert "secret-minimax-env" not in response.model_dump_json()


def test_explicit_env_minimax_profile_resolves_to_environment_default() -> None:
    config = InfrastructureConfig(
        minimax_api_key="secret-minimax-env",
        minimax_model="MiniMax-M2.7",
    )

    profile = config.resolve_model_profile("env-minimax")

    assert profile.id == "env-minimax"
    assert profile.api_key == "secret-minimax-env"


def test_runtime_catalog_routes_are_read_only() -> None:
    api = HttpRuntimeHarness(config=InfrastructureConfig())
    routes = {
        (route.path, method)
        for route in api.app.routes
        for method in (route.methods or set())
    }

    assert ("/api/settings/llm", "GET") in routes
    assert ("/api/settings/llm", "POST") not in routes
    assert ("/api/settings/llm/preferences", "POST") not in routes
    assert all("/api/settings/llm/profiles" not in path for path, _method in routes)
    assert all("/api/settings/llm/default-profile" not in path for path, _method in routes)
