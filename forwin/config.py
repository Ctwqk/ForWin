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
        "max_chapter_chars": int(os.environ.get("MAX_CHAPTER_CHARS", "2200")),
        "min_chapter_chars": int(os.environ.get("MIN_CHAPTER_CHARS", "1500")),
        "target_chapter_chars": int(
            os.environ.get("TARGET_CHAPTER_CHARS", "2000")
        ),
        "writer_mode": os.environ.get("WRITER_MODE", "single"),
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
        "temperature": float(os.environ.get("TEMPERATURE", "0.85")),
        "max_tokens": int(os.environ.get("MAX_TOKENS", "16384")),
    }


class _ConfigFields:
    db_path: str = "data/novel.db"
    artifact_root: str = "data/artifacts"
    runtime_settings_path: str = "data/runtime_settings.json"
    publisher_extension_api_key: str = ""
    minimax_api_key: str = ""
    minimax_base_url: str = "https://api.minimaxi.com/v1"
    minimax_model: str = "MiniMax-M2.7"
    max_chapter_chars: int = 2200
    min_chapter_chars: int = 1500
    target_chapter_chars: int = 2000
    writer_mode: str = "single"
    default_scene_count: int = 3
    max_scene_count: int = 4
    context_budget_chars: int = 6000
    retrieval_max_entities: int = 8
    retrieval_max_threads: int = 4
    retrieval_max_summaries: int = 3
    temperature: float = 0.85
    max_tokens: int = 16384

    @classmethod
    def from_env(cls) -> "Config":
        return cls(**_env_values())


class Config(_ConfigFields, _ConfigBaseModel):  # type: ignore[misc]
    if _USES_BASE_SETTINGS:
        model_config = {"env_prefix": ""}
