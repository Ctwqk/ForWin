from __future__ import annotations

from pathlib import Path

import pytest

from forwin.config import (
    DEFAULT_DATABASE_URL,
    DEFAULT_EMBEDDING_DIMS,
    DEFAULT_EMBEDDING_GATEWAY_URL,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_MINIMAX_BASE_URL,
    InfrastructureConfig,
)


CONFIG_ENV_KEYS = {
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_BASE_URL",
    "DEEPSEEK_MODEL",
    "FORWIN_ALLOW_BIND_ALL_INTERFACES",
    "FORWIN_ALLOW_UNAUTHENTICATED_LAN",
    "FORWIN_ARTIFACT_BACKEND",
    "FORWIN_ARTIFACT_ROOT",
    "FORWIN_DATABASE_URL",
    "FORWIN_DB_PATH",
    "FORWIN_EMBEDDING_API_KEY",
    "FORWIN_EMBEDDING_BACKEND",
    "FORWIN_EMBEDDING_BASE_URL",
    "FORWIN_EMBEDDING_DIMS",
    "FORWIN_EMBEDDING_MODEL",
    "FORWIN_EMBEDDING_REQUIRED",
    "FORWIN_ENV_FILE",
    "FORWIN_HTTP_BASIC_EXEMPT_PATHS",
    "FORWIN_HTTP_BASIC_PASSWORD",
    "FORWIN_HTTP_BASIC_USER",
    "FORWIN_HTTP_BIND",
    "FORWIN_HTTP_PORT",
    "FORWIN_LLM_KB_QDRANT_COLLECTION",
    "FORWIN_MINIO_ACCESS_KEY",
    "FORWIN_MINIO_BUCKET",
    "FORWIN_MINIO_ENDPOINT",
    "FORWIN_MINIO_PREFIX",
    "FORWIN_MINIO_SECRET_KEY",
    "FORWIN_MINIO_SECURE",
    "FORWIN_PUBLISHER_EXTENSION_API_KEY",
    "FORWIN_PUBLISHER_PREFERRED_CLIENT_ID",
    "FORWIN_PUBLISHER_SESSION_ENCRYPTION_REQUIRED",
    "FORWIN_PUBLISHER_SESSION_SECRET",
    "FORWIN_QDRANT_COLLECTION",
    "FORWIN_QDRANT_URL",
    "FORWIN_RETRIEVAL_BACKEND",
    "FORWIN_RETRIEVAL_ROOT",
    "KIMI_API_KEY",
    "KIMI_BASE_URL",
    "KIMI_MODEL",
    "LLM_RETRY_ATTEMPTS",
    "LLM_RETRY_INITIAL_DELAY_SECONDS",
    "LLM_RETRY_MAX_DELAY_SECONDS",
    "LLM_TIMEOUT_SECONDS",
    "MAX_TOKENS",
    "MINIMAX_API_KEY",
    "MINIMAX_BASE_URL",
    "MINIMAX_MODEL",
    "MOONSHOT_API_KEY",
    "MOONSHOT_BASE_URL",
    "MOONSHOT_MODEL",
    "SCENE_CALL_TIMEOUT_SECONDS",
}


def _set_env_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, lines: list[str]
) -> Path:
    for key in CONFIG_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("\n".join(lines), encoding="utf-8")
    monkeypatch.setenv("FORWIN_ENV_FILE", str(env_path))
    return env_path


def test_env_file_populates_infrastructure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_env_file(
        monkeypatch,
        tmp_path,
        [
            "FORWIN_QDRANT_URL=http://file-qdrant:6333",
            "FORWIN_DATABASE_URL=postgresql+psycopg://file-db/forwin",
            "FORWIN_LLM_KB_QDRANT_COLLECTION=file_llm_kb_vectors",
            "FORWIN_MINIO_ENDPOINT=file-minio:9000",
            "FORWIN_ARTIFACT_BACKEND=minio",
            "FORWIN_PUBLISHER_SESSION_SECRET=file-session-secret",
            "FORWIN_PUBLISHER_SESSION_ENCRYPTION_REQUIRED=true",
            "LLM_RETRY_ATTEMPTS=5",
        ],
    )

    config = InfrastructureConfig.from_env()

    assert config.qdrant_url == "http://file-qdrant:6333"
    assert config.database_url == "postgresql+psycopg://file-db/forwin"
    assert config.llm_kb_qdrant_collection == "file_llm_kb_vectors"
    assert config.minio_endpoint == "file-minio:9000"
    assert config.artifact_backend == "minio"
    assert config.publisher_session_secret == "file-session-secret"
    assert config.publisher_session_encryption_required is True
    assert config.llm_retry_attempts == 5


def test_real_environment_overrides_env_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_env_file(monkeypatch, tmp_path, ["FORWIN_QDRANT_URL=http://file-qdrant:6333"])
    monkeypatch.setenv("FORWIN_QDRANT_URL", "http://real-qdrant:6333")

    assert InfrastructureConfig.from_env().qdrant_url == "http://real-qdrant:6333"


def test_infrastructure_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _set_env_file(monkeypatch, tmp_path, [])

    config = InfrastructureConfig.from_env()

    assert config.database_url == DEFAULT_DATABASE_URL
    assert config.qdrant_url == "http://127.0.0.1:6335"
    assert config.minimax_base_url == DEFAULT_MINIMAX_BASE_URL
    assert config.embedding_backend == "gateway"
    assert config.embedding_base_url == DEFAULT_EMBEDDING_GATEWAY_URL
    assert config.embedding_model == DEFAULT_EMBEDDING_MODEL
    assert config.embedding_dims == DEFAULT_EMBEDDING_DIMS
    assert config.embedding_required is False
    assert config.scene_call_timeout_seconds == config.llm_timeout_seconds == 90.0


def test_removed_environment_policy_is_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_env_file(
        monkeypatch,
        tmp_path,
        [
            "FORWIN_QUALITY_PROFILE=pulp",
            "OPERATION_MODE=copilot",
            "PROGRESSION_MODE=legacy_relaxed",
        ],
    )

    config = InfrastructureConfig.from_env()

    assert not hasattr(config, "quality_profile")
    assert not hasattr(config, "operation_mode")
    assert not hasattr(config, "progression_mode")


def test_removed_db_path_env_alias_is_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_env_file(monkeypatch, tmp_path, ["FORWIN_DB_PATH=postgresql+psycopg://old/forwin"])

    config = InfrastructureConfig.from_env()

    assert config.database_url == DEFAULT_DATABASE_URL
    assert not hasattr(config, "db_path")


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("LLM_TIMEOUT_SECONDS=abc", "Invalid float for LLM_TIMEOUT_SECONDS: abc"),
        (
            "FORWIN_PUBLISHER_SESSION_ENCRYPTION_REQUIRED=maybe",
            "Invalid boolean for FORWIN_PUBLISHER_SESSION_ENCRYPTION_REQUIRED: maybe",
        ),
    ],
)
def test_invalid_typed_values_include_env_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, line: str, message: str
) -> None:
    _set_env_file(monkeypatch, tmp_path, [line])

    with pytest.raises(ValueError, match=message):
        InfrastructureConfig.from_env()


def test_publisher_session_encryption_required_needs_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_env_file(
        monkeypatch,
        tmp_path,
        ["FORWIN_PUBLISHER_SESSION_ENCRYPTION_REQUIRED=true"],
    )

    with pytest.raises(ValueError, match="FORWIN_PUBLISHER_SESSION_SECRET"):
        InfrastructureConfig.from_env()
