from __future__ import annotations

import os

try:
    from pydantic_settings import BaseSettings as _ConfigBaseModel

    _USES_BASE_SETTINGS = True
except ImportError:
    from pydantic import BaseModel as _ConfigBaseModel

    _USES_BASE_SETTINGS = False


def _env_values() -> dict[str, object]:
    return {
        "db_path": os.environ.get("FORWIN_DB_PATH", "data/novel.db"),
        "artifact_root": os.environ.get("FORWIN_ARTIFACT_ROOT", "data/artifacts"),
        "artifact_backend": os.environ.get("FORWIN_ARTIFACT_BACKEND", "local"),
        "minio_endpoint": os.environ.get("FORWIN_MINIO_ENDPOINT", ""),
        "minio_access_key": os.environ.get("FORWIN_MINIO_ACCESS_KEY", ""),
        "minio_secret_key": os.environ.get("FORWIN_MINIO_SECRET_KEY", ""),
        "minio_bucket": os.environ.get("FORWIN_MINIO_BUCKET", "forwin-artifacts"),
        "minio_prefix": os.environ.get("FORWIN_MINIO_PREFIX", "artifacts"),
        "minio_secure": os.environ.get("FORWIN_MINIO_SECURE", "false").strip().lower() in {"1", "true", "yes"},
        "retrieval_backend": os.environ.get("FORWIN_RETRIEVAL_BACKEND", "local"),
        "retrieval_root": os.environ.get("FORWIN_RETRIEVAL_ROOT", "data/retrieval"),
        "qdrant_url": os.environ.get("FORWIN_QDRANT_URL", ""),
        "qdrant_collection": os.environ.get("FORWIN_QDRANT_COLLECTION", "chapter_memories"),
        "embedding_backend": os.environ.get("FORWIN_EMBEDDING_BACKEND", "hash"),
        "embedding_base_url": os.environ.get(
            "FORWIN_EMBEDDING_BASE_URL",
            os.environ.get("MINIMAX_BASE_URL", "https://api.minimaxi.com/v1"),
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
        "minimax_api_key": os.environ.get("MINIMAX_API_KEY", ""),
        "minimax_base_url": os.environ.get(
            "MINIMAX_BASE_URL", "https://api.minimaxi.com/v1"
        ),
        "minimax_model": os.environ.get("MINIMAX_MODEL", "MiniMax-M2.7"),
        "llm_timeout_seconds": float(os.environ.get("LLM_TIMEOUT_SECONDS", "90")),
        "scene_call_timeout_seconds": float(
            os.environ.get("SCENE_CALL_TIMEOUT_SECONDS", "45")
        ),
        "max_chapter_chars": int(os.environ.get("MAX_CHAPTER_CHARS", "2200")),
        "min_chapter_chars": int(os.environ.get("MIN_CHAPTER_CHARS", "1500")),
        "target_chapter_chars": int(
            os.environ.get("TARGET_CHAPTER_CHARS", "2000")
        ),
        "writer_mode": os.environ.get("WRITER_MODE", "scene"),
        "operation_mode": os.environ.get("OPERATION_MODE", "blackbox"),
        "freeze_failed_candidates": os.environ.get("FREEZE_FAILED_CANDIDATES", "true").strip().lower() not in {"0", "false", "no"},
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
        "phase4_use_llm": os.environ.get("PHASE4_USE_LLM", "true").strip().lower() not in {"0", "false", "no"},
        "temperature": float(os.environ.get("TEMPERATURE", "0.85")),
        "max_tokens": int(os.environ.get("MAX_TOKENS", "16384")),
    }


class _ConfigFields:
    db_path: str = "data/novel.db"
    artifact_root: str = "data/artifacts"
    artifact_backend: str = "local"
    minio_endpoint: str = ""
    minio_access_key: str = ""
    minio_secret_key: str = ""
    minio_bucket: str = "forwin-artifacts"
    minio_prefix: str = "artifacts"
    minio_secure: bool = False
    retrieval_backend: str = "local"
    retrieval_root: str = "data/retrieval"
    qdrant_url: str = ""
    qdrant_collection: str = "chapter_memories"
    embedding_backend: str = "hash"
    embedding_base_url: str = "https://api.minimaxi.com/v1"
    embedding_api_key: str = ""
    embedding_model: str = ""
    embedding_dims: int = 64
    runtime_settings_path: str = "data/runtime_settings.json"
    publisher_extension_api_key: str = ""
    minimax_api_key: str = ""
    minimax_base_url: str = "https://api.minimaxi.com/v1"
    minimax_model: str = "MiniMax-M2.7"
    llm_timeout_seconds: float = 90.0
    scene_call_timeout_seconds: float = 45.0
    max_chapter_chars: int = 2200
    min_chapter_chars: int = 1500
    target_chapter_chars: int = 2000
    writer_mode: str = "scene"
    operation_mode: str = "blackbox"
    freeze_failed_candidates: bool = True
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
    phase4_use_llm: bool = True
    temperature: float = 0.85
    max_tokens: int = 16384

    @classmethod
    def from_env(cls) -> "Config":
        return cls(**_env_values())


class Config(_ConfigFields, _ConfigBaseModel):  # type: ignore[misc]
    if _USES_BASE_SETTINGS:
        model_config = {"env_prefix": ""}
