from __future__ import annotations

import json
from pathlib import Path

from forwin.config import Config
from forwin.runtime_settings import RuntimeSettingsStore


def test_config_builds_kimi_and_deepseek_profiles_from_env(monkeypatch) -> None:
    monkeypatch.setenv("KIMI_API_KEY", "secret-kimi")
    monkeypatch.setenv("KIMI_MODEL", "kimi-k2.5")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret-deepseek")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-chat")

    config = Config.from_env()
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

    config = Config.from_env()
    profiles = {item["id"]: item for item in config.llm_env_profiles}

    assert profiles["env-kimi"]["api_key"] == "file-kimi"
    assert profiles["env-deepseek"]["api_key"] == "file-deepseek"


def test_runtime_settings_uses_env_profiles_without_persisting_env_keys(tmp_path: Path) -> None:
    settings_path = tmp_path / "runtime_settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "profiles": [
                    {
                        "id": "default",
                        "name": "MiniMax 默认",
                        "api_key": "secret-minimax",
                        "base_url": "https://api.minimaxi.com/v1",
                        "model": "MiniMax-M2.7",
                    },
                    {
                        "id": "old-kimi",
                        "name": "Old Kimi",
                        "api_key": "stale-kimi",
                        "base_url": "https://api.moonshot.cn/v1",
                        "model": "kimi-k2.5",
                    },
                ],
                "default_profile_id": "old-kimi",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    store = RuntimeSettingsStore(
        str(settings_path),
        env_llm_profiles=[
            {
                "id": "env-kimi",
                "name": "Kimi (.env)",
                "api_key": "secret-kimi-env",
                "base_url": "https://api.moonshot.cn/v1",
                "model": "kimi-k2.5",
            },
            {
                "id": "env-deepseek",
                "name": "DeepSeek (.env)",
                "api_key": "secret-deepseek-env",
                "base_url": "https://api.deepseek.com/v1",
                "model": "deepseek-chat",
            },
        ],
    )

    loaded = store.get()
    profile_ids = [item["id"] for item in loaded["profiles"]]

    assert "old-kimi" not in profile_ids
    assert "env-kimi" in profile_ids
    assert "env-deepseek" in profile_ids
    assert loaded["default_profile_id"] == "env-kimi"
    assert loaded["api_key"] == "secret-kimi-env"

    saved = store.save(operation_mode="blackbox")
    persisted_text = settings_path.read_text(encoding="utf-8")

    assert "env-kimi" in [item["id"] for item in saved["profiles"]]
    assert "env-deepseek" in [item["id"] for item in saved["profiles"]]
    assert "secret-kimi-env" not in persisted_text
    assert "secret-deepseek-env" not in persisted_text
    assert "old-kimi" not in persisted_text


def test_runtime_settings_preserves_env_default_profile_across_reload(tmp_path: Path) -> None:
    settings_path = tmp_path / "runtime_settings.json"
    env_profiles = [
        {
            "id": "env-kimi",
            "name": "Kimi (.env)",
            "api_key": "secret-kimi-env",
            "base_url": "https://api.moonshot.cn/v1",
            "model": "kimi-k2.5",
        },
        {
            "id": "env-deepseek",
            "name": "DeepSeek (.env)",
            "api_key": "secret-deepseek-env",
            "base_url": "https://api.deepseek.com/v1",
            "model": "deepseek-chat",
        },
    ]
    store = RuntimeSettingsStore(str(settings_path), env_llm_profiles=env_profiles)

    saved = store.set_default_profile("env-deepseek")
    reloaded = RuntimeSettingsStore(str(settings_path), env_llm_profiles=env_profiles).get()
    persisted_text = settings_path.read_text(encoding="utf-8")

    assert saved["default_profile_id"] == "env-deepseek"
    assert reloaded["default_profile_id"] == "env-deepseek"
    assert reloaded["api_key"] == "secret-deepseek-env"
    assert "secret-deepseek-env" not in persisted_text
