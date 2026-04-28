from __future__ import annotations

import os

DEFAULT_MINIMAX_BASE_URL = "https://api.minimaxi.com/v1"
DEFAULT_MINIMAX_MODEL = "MiniMax-M2.7"
LEGACY_DATABASE_PATH_ENV = "FORWIN_" + "DB_PATH"
DEFAULT_MOONSHOT_BASE_URL = "https://api.moonshot.cn/v1"
DEFAULT_MOONSHOT_MODEL = "kimi-k2.5"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_DEEPSEEK_MODEL = "deepseek-chat"

try:
    from pydantic_settings import BaseSettings as _ConfigBaseModel

    _USES_BASE_SETTINGS = True
except ImportError:
    from pydantic import BaseModel as _ConfigBaseModel

    _USES_BASE_SETTINGS = False


def _csv_list(value: str | None) -> list[str]:
    return [
        item.strip()
        for item in str(value or "").split(",")
        if item.strip()
    ]


def _read_env_file_values() -> dict[str, str]:
    path = os.environ.get("FORWIN_ENV_FILE", ".env").strip() or ".env"
    values: dict[str, str] = {}
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except OSError:
        return values
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def _merged_env_values() -> dict[str, str]:
    values = _read_env_file_values()
    values.update({key: value for key, value in os.environ.items()})
    return values


def _env_llm_profiles(env: dict[str, str] | None = None) -> list[dict[str, str]]:
    env = env or _merged_env_values()
    profiles: list[dict[str, str]] = []
    kimi_api_key = (
        env.get("KIMI_API_KEY")
        or env.get("MOONSHOT_API_KEY")
        or ""
    ).strip()
    if kimi_api_key:
        profiles.append(
            {
                "id": "env-kimi",
                "name": "Kimi (.env)",
                "api_key": kimi_api_key,
                "base_url": (
                    env.get("KIMI_BASE_URL")
                    or env.get("MOONSHOT_BASE_URL")
                    or DEFAULT_MOONSHOT_BASE_URL
                ).strip(),
                "model": (
                    env.get("KIMI_MODEL")
                    or env.get("MOONSHOT_MODEL")
                    or DEFAULT_MOONSHOT_MODEL
                ).strip(),
            }
        )

    deepseek_api_key = env.get("DEEPSEEK_API_KEY", "").strip()
    if deepseek_api_key:
        profiles.append(
            {
                "id": "env-deepseek",
                "name": "DeepSeek (.env)",
                "api_key": deepseek_api_key,
                "base_url": env.get(
                    "DEEPSEEK_BASE_URL",
                    DEFAULT_DEEPSEEK_BASE_URL,
                ).strip(),
                "model": env.get(
                    "DEEPSEEK_MODEL",
                    DEFAULT_DEEPSEEK_MODEL,
                ).strip(),
            }
        )
    return profiles


def _env_values() -> dict[str, object]:
    env = _merged_env_values()
    if LEGACY_DATABASE_PATH_ENV in os.environ or LEGACY_DATABASE_PATH_ENV in env:
        raise ValueError(
            f"{LEGACY_DATABASE_PATH_ENV} is no longer supported. Set FORWIN_DATABASE_URL "
            "to a postgresql+psycopg:// URL."
        )
    return {
        "database_url": os.environ.get(
            "FORWIN_DATABASE_URL",
            "postgresql+psycopg://forwin:forwin@localhost:5432/forwin",
        ),
        "artifact_root": os.environ.get("FORWIN_ARTIFACT_ROOT", "data/artifacts"),
        "artifact_backend": os.environ.get("FORWIN_ARTIFACT_BACKEND", "local"),
        "minio_endpoint": os.environ.get("FORWIN_MINIO_ENDPOINT", ""),
        "minio_access_key": os.environ.get("FORWIN_MINIO_ACCESS_KEY", ""),
        "minio_secret_key": os.environ.get("FORWIN_MINIO_SECRET_KEY", ""),
        "minio_bucket": os.environ.get("FORWIN_MINIO_BUCKET", "forwin-artifacts"),
        "minio_prefix": os.environ.get("FORWIN_MINIO_PREFIX", "artifacts"),
        "minio_secure": os.environ.get("FORWIN_MINIO_SECURE", "false").strip().lower() in {"1", "true", "yes"},
        "retrieval_backend": os.environ.get("FORWIN_RETRIEVAL_BACKEND", "qdrant"),
        "retrieval_root": os.environ.get("FORWIN_RETRIEVAL_ROOT", "data/retrieval"),
        "qdrant_url": os.environ.get("FORWIN_QDRANT_URL", "http://localhost:6333"),
        "qdrant_collection": os.environ.get("FORWIN_QDRANT_COLLECTION", "chapter_memories"),
        "llm_kb_qdrant_collection": os.environ.get("FORWIN_LLM_KB_QDRANT_COLLECTION", "llm_kb_vectors"),
        "embedding_backend": os.environ.get("FORWIN_EMBEDDING_BACKEND", "hash"),
        "embedding_base_url": os.environ.get(
            "FORWIN_EMBEDDING_BASE_URL",
            os.environ.get("MINIMAX_BASE_URL", DEFAULT_MINIMAX_BASE_URL),
        ),
        "embedding_api_key": os.environ.get(
            "FORWIN_EMBEDDING_API_KEY",
            os.environ.get("MINIMAX_API_KEY", ""),
        ),
        "embedding_model": os.environ.get("FORWIN_EMBEDDING_MODEL", ""),
        "embedding_dims": int(os.environ.get("FORWIN_EMBEDDING_DIMS", "64")),
        "runtime_settings_path": os.environ.get(
            "FORWIN_RUNTIME_SETTINGS_PATH", "data/runtime_settings.json"
        ),
        "publisher_extension_api_key": os.environ.get(
            "FORWIN_PUBLISHER_EXTENSION_API_KEY",
            os.environ.get("PUBLISHER_EXTENSION_API_KEY", ""),
        ),
        "publisher_preferred_client_id": os.environ.get(
            "FORWIN_PUBLISHER_PREFERRED_CLIENT_ID", ""
        ),
        "minimax_api_key": env.get("MINIMAX_API_KEY", ""),
        "minimax_base_url": env.get(
            "MINIMAX_BASE_URL", DEFAULT_MINIMAX_BASE_URL
        ),
        "minimax_model": env.get("MINIMAX_MODEL", DEFAULT_MINIMAX_MODEL),
        "llm_env_profiles": _env_llm_profiles(env),
        "llm_timeout_seconds": float(os.environ.get("LLM_TIMEOUT_SECONDS", "90")),
        "llm_retry_attempts": int(os.environ.get("LLM_RETRY_ATTEMPTS", "2")),
        "llm_retry_initial_delay_seconds": float(
            os.environ.get("LLM_RETRY_INITIAL_DELAY_SECONDS", "2")
        ),
        "llm_retry_max_delay_seconds": float(
            os.environ.get("LLM_RETRY_MAX_DELAY_SECONDS", "15")
        ),
        "scene_call_timeout_seconds": float(
            os.environ.get("SCENE_CALL_TIMEOUT_SECONDS", "45")
        ),
        "max_chapter_chars": int(os.environ.get("MAX_CHAPTER_CHARS", "3200")),
        "min_chapter_chars": int(os.environ.get("MIN_CHAPTER_CHARS", "2500")),
        "target_chapter_chars": int(
            os.environ.get("TARGET_CHAPTER_CHARS", "2800")
        ),
        "writer_mode": os.environ.get("WRITER_MODE", "scene"),
        "operation_mode": os.environ.get("OPERATION_MODE", "blackbox"),
        "freeze_failed_candidates": os.environ.get("FREEZE_FAILED_CANDIDATES", "true").strip().lower() not in {"0", "false", "no"},
        "review_interval_chapters": int(os.environ.get("REVIEW_INTERVAL_CHAPTERS", "0")),
        "progression_mode": os.environ.get("PROGRESSION_MODE", "serial_canon_band_guard"),
        "auto_band_checkpoint": os.environ.get("AUTO_BAND_CHECKPOINT", "true").strip().lower() in {"1", "true", "yes"},
        "band_warn_action": os.environ.get("BAND_WARN_ACTION", "pause"),
        "manual_checkpoints_enabled": os.environ.get("MANUAL_CHECKPOINTS_ENABLED", "true").strip().lower() in {"1", "true", "yes"},
        "future_constraints_enabled": os.environ.get("FUTURE_CONSTRAINTS_ENABLED", "true").strip().lower() in {"1", "true", "yes"},
        "legacy_provisional_blocking": os.environ.get("FORWIN_LEGACY_PROVISIONAL_BLOCKING", "false").strip().lower() in {"1", "true", "yes"},
        "skill_runtime_enabled": os.environ.get("FORWIN_SKILL_RUNTIME_ENABLED", "true").strip().lower() not in {"0", "false", "no"},
        "skill_registry_path": os.environ.get("FORWIN_SKILL_REGISTRY_PATH", "forwin_skills"),
        "skill_strictness": os.environ.get("FORWIN_SKILL_STRICTNESS", "normal"),
        "enabled_skill_groups": _csv_list(os.environ.get("FORWIN_ENABLED_SKILL_GROUPS", "")),
        "disabled_skill_ids": _csv_list(os.environ.get("FORWIN_DISABLED_SKILL_IDS", "")),
        "default_scene_count": int(os.environ.get("DEFAULT_SCENE_COUNT", "3")),
        "max_scene_count": int(os.environ.get("MAX_SCENE_COUNT", "4")),
        "context_budget_chars": int(os.environ.get("CONTEXT_BUDGET_CHARS", "6000")),
        "retrieval_max_entities": int(
            os.environ.get("RETRIEVAL_MAX_ENTITIES", "8")
        ),
        "retrieval_max_threads": int(
            os.environ.get("RETRIEVAL_MAX_THREADS", "4")
        ),
        "retrieval_max_summaries": int(
            os.environ.get("RETRIEVAL_MAX_SUMMARIES", "3")
        ),
        "pacing_window_size": int(os.environ.get("PACING_WINDOW_SIZE", "3")),
        "stale_thread_window": int(os.environ.get("STALE_THREAD_WINDOW", "3")),
        "pacing_min_avg_chars": int(os.environ.get("PACING_MIN_AVG_CHARS", "1600")),
        "pacing_max_avg_chars": int(os.environ.get("PACING_MAX_AVG_CHARS", "3800")),
        "phase_active_thread_limit": int(
            os.environ.get("PHASE_ACTIVE_THREAD_LIMIT", "20")
        ),
        "replan_cooldown_chapters": int(
            os.environ.get("REPLAN_COOLDOWN_CHAPTERS", "3")
        ),
        "blackbox_writer_attention_retries": int(
            os.environ.get("BLACKBOX_WRITER_ATTENTION_RETRIES", "3")
        ),
        "experience_review_enabled": os.environ.get(
            "EXPERIENCE_REVIEW_ENABLED", "true"
        ).strip().lower() not in {"0", "false", "no"},
        "lint_review_enabled": os.environ.get(
            "LINT_REVIEW_ENABLED", "true"
        ).strip().lower() not in {"0", "false", "no"},
        "review_fail_max_rewrites": int(
            os.environ.get("REVIEW_FAIL_MAX_REWRITES", "3")
        ),
        "phase4_use_llm": os.environ.get("PHASE4_USE_LLM", "true").strip().lower() not in {"0", "false", "no"},
        "codex_enabled": os.environ.get("FORWIN_CODEX_ENABLED", "false").strip().lower() in {"1", "true", "yes"},
        "codex_bridge_url": os.environ.get("FORWIN_CODEX_BRIDGE_URL", "http://host.docker.internal:8897"),
        "codex_bridge_token": os.environ.get("FORWIN_CODEX_BRIDGE_TOKEN", ""),
        "codex_max_concurrent": int(os.environ.get("FORWIN_CODEX_MAX_CONCURRENT", "1")),
        "codex_sync_timeout_seconds": float(os.environ.get("FORWIN_CODEX_SYNC_TIMEOUT_SECONDS", "90")),
        "codex_job_timeout_seconds": float(os.environ.get("FORWIN_CODEX_JOB_TIMEOUT_SECONDS", "900")),
        "feedback_cooldown_chapters": int(os.environ.get("FEEDBACK_COOLDOWN_CHAPTERS", "3")),
        "comment_to_reader_ratio": int(os.environ.get("COMMENT_TO_READER_RATIO", "80")),
        "temperature": float(os.environ.get("TEMPERATURE", "0.85")),
        "max_tokens": int(os.environ.get("MAX_TOKENS", "16384")),
    }


class _ConfigFields:
    database_url: str = "postgresql+psycopg://forwin:forwin@localhost:5432/forwin"
    artifact_root: str = "data/artifacts"
    artifact_backend: str = "local"
    minio_endpoint: str = ""
    minio_access_key: str = ""
    minio_secret_key: str = ""
    minio_bucket: str = "forwin-artifacts"
    minio_prefix: str = "artifacts"
    minio_secure: bool = False
    retrieval_backend: str = "qdrant"
    retrieval_root: str = "data/retrieval"
    qdrant_url: str = "http://localhost:6333"
    qdrant_collection: str = "chapter_memories"
    llm_kb_qdrant_collection: str = "llm_kb_vectors"
    embedding_backend: str = "hash"
    embedding_base_url: str = DEFAULT_MINIMAX_BASE_URL
    embedding_api_key: str = ""
    embedding_model: str = ""
    embedding_dims: int = 64
    runtime_settings_path: str = "data/runtime_settings.json"
    publisher_extension_api_key: str = ""
    publisher_preferred_client_id: str = ""
    minimax_api_key: str = ""
    minimax_base_url: str = DEFAULT_MINIMAX_BASE_URL
    minimax_model: str = DEFAULT_MINIMAX_MODEL
    llm_env_profiles: list[dict[str, str]] = []
    llm_timeout_seconds: float = 90.0
    llm_retry_attempts: int = 2
    llm_retry_initial_delay_seconds: float = 2.0
    llm_retry_max_delay_seconds: float = 15.0
    scene_call_timeout_seconds: float = 45.0
    max_chapter_chars: int = 3200
    min_chapter_chars: int = 2500
    target_chapter_chars: int = 2800
    writer_mode: str = "scene"
    operation_mode: str = "blackbox"
    freeze_failed_candidates: bool = True
    review_interval_chapters: int = 0
    progression_mode: str = "serial_canon_band_guard"
    auto_band_checkpoint: bool = True
    band_warn_action: str = "pause"
    manual_checkpoints_enabled: bool = True
    future_constraints_enabled: bool = True
    legacy_provisional_blocking: bool = False
    skill_runtime_enabled: bool = True
    skill_registry_path: str = "forwin_skills"
    skill_strictness: str = "normal"
    enabled_skill_groups: list[str] = []
    disabled_skill_ids: list[str] = []
    governance_task_id: str = ""
    governance_causal_root_id: str = ""
    llm_fallback_profiles: list[dict[str, str]] = []
    default_scene_count: int = 3
    max_scene_count: int = 4
    context_budget_chars: int = 6000
    retrieval_max_entities: int = 8
    retrieval_max_threads: int = 4
    retrieval_max_summaries: int = 3
    pacing_window_size: int = 3
    stale_thread_window: int = 3
    pacing_min_avg_chars: int = 1600
    pacing_max_avg_chars: int = 3800
    phase_active_thread_limit: int = 20
    replan_cooldown_chapters: int = 3
    blackbox_writer_attention_retries: int = 3
    experience_review_enabled: bool = True
    lint_review_enabled: bool = True
    review_fail_max_rewrites: int = 3
    phase4_use_llm: bool = True
    codex_enabled: bool = False
    codex_bridge_url: str = "http://host.docker.internal:8897"
    codex_bridge_token: str = ""
    codex_max_concurrent: int = 1
    codex_sync_timeout_seconds: float = 90.0
    codex_job_timeout_seconds: float = 900.0
    feedback_cooldown_chapters: int = 3
    comment_to_reader_ratio: int = 80
    temperature: float = 0.85
    max_tokens: int = 16384

    @classmethod
    def from_env(cls) -> "Config":
        return cls(**_env_values())


class Config(_ConfigFields, _ConfigBaseModel):  # type: ignore[misc]
    if _USES_BASE_SETTINGS:
        model_config = {"env_prefix": "", "extra": "forbid"}
    else:
        model_config = {"extra": "forbid"}
