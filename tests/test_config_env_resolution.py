from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig

from forwin.api_runtime import build_runtime_config, copy_config
from forwin.api_schemas import GenerateRequest
from forwin.config import Config, DEFAULT_DATABASE_URL, DEFAULT_MINIMAX_BASE_URL
from tests.postgres import postgres_test_url


CONFIG_ENV_KEYS = {
    "AUTO_BAND_CHECKPOINT",
    "BAND_WARN_ACTION",
    "BLACKBOX_WRITER_ATTENTION_RETRIES",
    "COMMENT_TO_READER_RATIO",
    "CONTEXT_BUDGET_CHARS",
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_BASE_URL",
    "DEEPSEEK_MODEL",
    "DEFAULT_SCENE_COUNT",
    "EXPERIENCE_REVIEW_ENABLED",
    "FEEDBACK_COOLDOWN_CHAPTERS",
    "FORWIN_ARTIFACT_BACKEND",
    "FORWIN_ARTIFACT_ROOT",
    "FORWIN_ALLOW_BIND_ALL_INTERFACES",
    "FORWIN_ALLOW_UNAUTHENTICATED_LAN",
    "FORWIN_BOOK_STATE_LAYERS",
    "FORWIN_CANON_QUALITY_REVIEW_IN_HUB_ENABLED",
    "FORWIN_CODEX_BRIDGE_TOKEN",
    "FORWIN_CODEX_BRIDGE_URL",
    "FORWIN_CODEX_ENABLED",
    "FORWIN_CODEX_JOB_TIMEOUT_SECONDS",
    "FORWIN_CODEX_MAX_CONCURRENT",
    "FORWIN_CODEX_SYNC_TIMEOUT_SECONDS",
    "FORWIN_CONTEXT_RECENCY_WINDOW_CHAPTERS",
    "FORWIN_DATABASE_URL",
    "FORWIN_DB_PATH",
    "FORWIN_DISABLED_SKILL_IDS",
    "FORWIN_ENABLE_COMPAT_DEBUG_API",
    "FORWIN_EMBEDDING_API_KEY",
    "FORWIN_EMBEDDING_BACKEND",
    "FORWIN_EMBEDDING_BASE_URL",
    "FORWIN_EMBEDDING_DIMS",
    "FORWIN_EMBEDDING_MODEL",
    "FORWIN_ENABLED_SKILL_GROUPS",
    "FORWIN_ENV_FILE",
    "FORWIN_ENABLE_PUBLISHER_LOGIN_DISCORD_WEBHOOK",
    "FORWIN_HARD_FLOOR_GATE_ENABLED",
    "FORWIN_HTTP_BASIC_EXEMPT_PATHS",
    "FORWIN_HTTP_BASIC_PASSWORD",
    "FORWIN_HTTP_BASIC_USER",
    "FORWIN_HTTP_BIND",
    "FORWIN_HTTP_PORT",
    "FORWIN_LEGACY_PROVISIONAL_BLOCKING",
    "FORWIN_LLM_KB_QDRANT_COLLECTION",
    "FORWIN_MAP_MOVEMENT_REVIEW_ENABLED",
    "FORWIN_MINIO_ACCESS_KEY",
    "FORWIN_MINIO_BUCKET",
    "FORWIN_MINIO_ENDPOINT",
    "FORWIN_MINIO_PREFIX",
    "FORWIN_MINIO_SECRET_KEY",
    "FORWIN_MINIO_SECURE",
    "FORWIN_PERSONALITY_REVIEW_ENABLED",
    "FORWIN_PROVISIONAL_PREVIEW_ENABLED",
    "FORWIN_PUBLISHER_EXTENSION_API_KEY",
    "FORWIN_PUBLISHER_LOGIN_DISCORD_WEBHOOK_FILE",
    "FORWIN_PUBLISHER_LOGIN_DISCORD_WEBHOOK_URL",
    "FORWIN_PUBLISHER_PREFERRED_CLIENT_ID",
    "FORWIN_PUBLISHER_SESSION_ENCRYPTION_REQUIRED",
    "FORWIN_PUBLISHER_SESSION_SECRET",
    "FORWIN_QUALITY_PROFILE",
    "FORWIN_QDRANT_COLLECTION",
    "FORWIN_QDRANT_URL",
    "FORWIN_RETRIEVAL_BACKEND",
    "FORWIN_RETRIEVAL_ROOT",
    "FORWIN_RUNTIME_SETTINGS_PATH",
    "FORWIN_SKILL_REGISTRY_PATH",
    "FORWIN_SKILL_RUNTIME_ENABLED",
    "FORWIN_SKILL_STRICTNESS",
    "FREEZE_FAILED_CANDIDATES",
    "FUTURE_CONSTRAINTS_ENABLED",
    "KIMI_API_KEY",
    "KIMI_BASE_URL",
    "KIMI_MODEL",
    "LINT_REVIEW_ENABLED",
    "LLM_RETRY_ATTEMPTS",
    "LLM_RETRY_INITIAL_DELAY_SECONDS",
    "LLM_RETRY_MAX_DELAY_SECONDS",
    "LLM_TIMEOUT_SECONDS",
    "MANUAL_CHECKPOINTS_ENABLED",
    "MAX_CHAPTER_CHARS",
    "MAX_SCENE_COUNT",
    "MAX_TOKENS",
    "MIN_CHAPTER_CHARS",
    "MINIMAX_API_KEY",
    "MINIMAX_BASE_URL",
    "MINIMAX_MODEL",
    "MOONSHOT_API_KEY",
    "MOONSHOT_BASE_URL",
    "MOONSHOT_MODEL",
    "OPERATION_MODE",
    "PACING_MAX_AVG_CHARS",
    "PACING_MIN_AVG_CHARS",
    "PACING_WINDOW_SIZE",
    "PHASE4_USE_LLM",
    "PHASE_ACTIVE_THREAD_LIMIT",
    "PROGRESSION_MODE",
    "PROMPT_BUDGET_CHARS",
    "PUBLISHER_EXTENSION_API_KEY",
    "REPLAN_COOLDOWN_CHAPTERS",
    "RETRIEVAL_MAX_ENTITIES",
    "RETRIEVAL_MAX_SUMMARIES",
    "RETRIEVAL_MAX_THREADS",
    "REVIEW_FAIL_MAX_REWRITES",
    "REVIEW_INTERVAL_CHAPTERS",
    "SCENE_CALL_TIMEOUT_SECONDS",
    "STALE_THREAD_WINDOW",
    "TARGET_CHAPTER_CHARS",
    "TEMPERATURE",
    "WRITER_MODE",
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


def test_env_file_populates_storage_runtime_generation_and_codex(
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
            "FORWIN_RUNTIME_SETTINGS_PATH=data/file-runtime-settings.json",
            "FORWIN_PUBLISHER_SESSION_SECRET=file-session-secret",
            "FORWIN_PUBLISHER_SESSION_ENCRYPTION_REQUIRED=true",
            "LLM_RETRY_ATTEMPTS=5",
            "MAX_SCENE_COUNT=7",
            "FORWIN_CODEX_ENABLED=on",
        ],
    )

    config = Config.from_env()

    assert config.qdrant_url == "http://file-qdrant:6333"
    assert config.database_url == "postgresql+psycopg://file-db/forwin"
    assert config.llm_kb_qdrant_collection == "file_llm_kb_vectors"
    assert config.minio_endpoint == "file-minio:9000"
    assert config.artifact_backend == "minio"
    assert config.runtime_settings_path == "data/file-runtime-settings.json"
    assert config.publisher_session_secret == "file-session-secret"
    assert config.publisher_session_encryption_required is True
    assert config.llm_retry_attempts == 5
    assert config.max_scene_count == 7
    assert config.codex_enabled is True


def test_real_environment_overrides_env_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_env_file(
        monkeypatch,
        tmp_path,
        ["FORWIN_QDRANT_URL=http://file-qdrant:6333"],
    )
    monkeypatch.setenv("FORWIN_QDRANT_URL", "http://real-qdrant:6333")

    config = Config.from_env()

    assert config.qdrant_url == "http://real-qdrant:6333"


def test_default_qdrant_url_uses_forwin_local_debug_port(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_env_file(monkeypatch, tmp_path, [])

    assert Config.from_env().qdrant_url == "http://127.0.0.1:6335"
    assert Config().qdrant_url == "http://127.0.0.1:6335"


def test_alembic_env_uses_forwin_database_url_when_ini_has_no_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database_url = postgres_test_url("alembic-env-forwin-url")
    _set_env_file(monkeypatch, tmp_path, [f"FORWIN_DATABASE_URL={database_url}"])

    command.current(AlembicConfig("alembic.ini"))


def test_default_minimax_base_url_uses_configured_cn_openai_endpoint() -> None:
    assert DEFAULT_MINIMAX_BASE_URL == "https://api.minimaxi.com/v1"
    assert Config().minimax_base_url == "https://api.minimaxi.com/v1"


def test_default_scene_call_timeout_matches_default_llm_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_env_file(monkeypatch, tmp_path, [])

    config = Config.from_env()

    assert config.scene_call_timeout_seconds == config.llm_timeout_seconds == 90.0
    assert Config().scene_call_timeout_seconds == Config().llm_timeout_seconds == 90.0


def test_removed_db_path_env_alias_is_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_env_file(monkeypatch, tmp_path, ["FORWIN_DB_PATH=postgresql+psycopg://old/forwin"])

    config = Config.from_env()

    assert config.database_url == DEFAULT_DATABASE_URL
    assert not hasattr(config, "db_path")


def test_current_provisional_preview_env_is_supported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_env_file(monkeypatch, tmp_path, ["FORWIN_PROVISIONAL_PREVIEW_ENABLED=true"])

    assert Config.from_env().provisional_preview_enabled is True


def test_removed_legacy_provisional_blocking_env_alias_is_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_env_file(monkeypatch, tmp_path, ["FORWIN_LEGACY_PROVISIONAL_BLOCKING=true"])

    config = Config.from_env()

    assert config.provisional_preview_enabled is False
    assert not hasattr(config, "legacy_provisional_blocking")


def test_removed_legacy_relaxed_progression_mode_normalizes_to_current_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_env_file(monkeypatch, tmp_path, ["PROGRESSION_MODE=legacy_relaxed"])

    assert Config.from_env().progression_mode == "serial_canon_band_guard"
    assert Config(progression_mode="legacy_relaxed").progression_mode == "serial_canon_band_guard"


def test_copy_config_normalizes_removed_legacy_relaxed_progression_mode() -> None:
    runtime_config = copy_config(Config(), progression_mode="legacy_relaxed")

    assert runtime_config.progression_mode == "serial_canon_band_guard"


def test_build_runtime_config_normalizes_removed_request_progression_mode() -> None:
    runtime_config = build_runtime_config(
        GenerateRequest(
            premise="测试 premise",
            genre="玄幻",
            num_chapters=1,
            progression_mode="legacy_relaxed",
        ),
        base_config=Config(),
        runtime_settings=None,
    )

    assert runtime_config.progression_mode == "serial_canon_band_guard"


def test_publisher_extension_legacy_alias_still_works(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_env_file(
        monkeypatch,
        tmp_path,
        [
            "PUBLISHER_EXTENSION_API_KEY=legacy-extension-key",
            "FORWIN_PUBLISHER_SESSION_SECRET=legacy-session-secret",
            "FORWIN_PUBLISHER_SESSION_ENCRYPTION_REQUIRED=true",
        ],
    )

    config = Config.from_env()

    assert config.publisher_extension_api_key == "legacy-extension-key"


def test_publisher_login_discord_webhook_env_is_disabled_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_env_file(
        monkeypatch,
        tmp_path,
        ["FORWIN_PUBLISHER_LOGIN_DISCORD_WEBHOOK_URL=https://discord.invalid/api/webhooks/test"],
    )

    config = Config.from_env()

    assert config.publisher_login_discord_webhook_enabled is False
    assert config.publisher_login_discord_webhook_url == ""
    assert config.publisher.login_discord_webhook_url == ""


def test_publisher_login_discord_webhook_env_is_loaded_when_enabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_env_file(
        monkeypatch,
        tmp_path,
        [
            "FORWIN_ENABLE_PUBLISHER_LOGIN_DISCORD_WEBHOOK=true",
            "FORWIN_PUBLISHER_LOGIN_DISCORD_WEBHOOK_URL=https://discord.invalid/api/webhooks/test",
        ],
    )

    config = Config.from_env()

    assert config.publisher_login_discord_webhook_enabled is True
    assert config.publisher_login_discord_webhook_url == "https://discord.invalid/api/webhooks/test"
    assert config.publisher.login_discord_webhook_url == "https://discord.invalid/api/webhooks/test"


def test_publisher_login_discord_webhook_file_is_loaded_when_enabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secret_path = tmp_path / "discord-webhook.secret"
    secret_path.write_text("https://discord.invalid/api/webhooks/from-file\n", encoding="utf-8")
    _set_env_file(
        monkeypatch,
        tmp_path,
        [
            "FORWIN_ENABLE_PUBLISHER_LOGIN_DISCORD_WEBHOOK=true",
            f"FORWIN_PUBLISHER_LOGIN_DISCORD_WEBHOOK_FILE={secret_path}",
        ],
    )

    config = Config.from_env()

    assert config.publisher_login_discord_webhook_url == "https://discord.invalid/api/webhooks/from-file"


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("MAX_SCENE_COUNT=abc", "Invalid integer for MAX_SCENE_COUNT: abc"),
        ("LLM_TIMEOUT_SECONDS=abc", "Invalid float for LLM_TIMEOUT_SECONDS: abc"),
        (
            "FORWIN_CODEX_ENABLED=maybe",
            "Invalid boolean for FORWIN_CODEX_ENABLED: maybe",
        ),
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
        Config.from_env()


def test_publisher_session_encryption_required_needs_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_env_file(
        monkeypatch,
        tmp_path,
        ["FORWIN_PUBLISHER_SESSION_ENCRYPTION_REQUIRED=true"],
    )

    with pytest.raises(ValueError, match="FORWIN_PUBLISHER_SESSION_SECRET"):
        Config.from_env()
