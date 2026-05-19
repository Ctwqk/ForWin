from __future__ import annotations

from copy import deepcopy
import os
from typing import Literal

from pydantic import BaseModel

from forwin.writer.profile import WriterProfile

DEFAULT_MINIMAX_BASE_URL = "https://api.minimaxi.com/v1"
DEFAULT_MINIMAX_MODEL = "MiniMax-M2.7"
LEGACY_DATABASE_PATH_ENV = "FORWIN_" + "DB_PATH"
DEFAULT_DATABASE_URL = "postgresql+psycopg://forwin:forwin@localhost:5432/forwin"
DEFAULT_MOONSHOT_BASE_URL = "https://api.moonshot.cn/v1"
DEFAULT_MOONSHOT_MODEL = "kimi-k2.5"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_DEEPSEEK_MODEL = "deepseek-chat"
DEFAULT_QDRANT_URL = "http://127.0.0.1:6335"
DEFAULT_HTTP_BASIC_EXEMPT_PATHS = (
    "/health",
    "/api/extension/",
    "/api/publisher/extension/",
    "/api/publishers/extension/",
)


class LLMConfig(BaseModel):
    minimax_api_key: str = ""
    minimax_base_url: str = DEFAULT_MINIMAX_BASE_URL
    minimax_model: str = DEFAULT_MINIMAX_MODEL
    timeout_seconds: float = 90.0
    retry_attempts: int = 2
    max_tokens: int = 16384


class StorageConfig(BaseModel):
    database_url: str = DEFAULT_DATABASE_URL
    artifact_backend: str = "local"
    artifact_root: str = "data/artifacts"
    retrieval_backend: str = "qdrant"
    qdrant_url: str = DEFAULT_QDRANT_URL


class PublisherConfig(BaseModel):
    extension_api_key: str = ""
    session_secret: str = ""
    session_encryption_required: bool = False


class ObservabilityConfig(BaseModel):
    enabled: bool = True
    performance_enabled: bool = True
    span_sample_rate: float = 1.0


class GovernanceConfig(BaseModel):
    progression_mode: str = "serial_canon_band_guard"
    review_interval_chapters: int = 0
    future_constraints_enabled: bool = True


class FormBlockingPolicy(BaseModel):
    character_dead: Literal["error", "warning"] = "error"
    character_wounded: Literal["error", "warning"] = "warning"
    character_captured: Literal["error", "warning"] = "error"
    countdown_inconsistent: Literal["error", "warning"] = "error"
    countdown_reset: Literal["error", "warning"] = "warning"
    countdown_advanced: Literal["error", "warning"] = "warning"
    obligation_unaddressed: Literal["error", "warning"] = "error"
    obligation_partial: Literal["error", "warning"] = "warning"
    signal_persisting: Literal["error", "warning"] = "error"
    signal_worsened: Literal["error", "warning"] = "error"
    final_dangling: Literal["error", "warning"] = "error"
    final_denied: Literal["error", "warning"] = "error"


class CodexConfig(BaseModel):
    enabled: bool = False
    bridge_url: str = "http://host.docker.internal:8897"
    max_concurrent: int = 1

try:
    from pydantic_settings import BaseSettings as _ConfigBaseModel

    _USES_BASE_SETTINGS = True
except ImportError:
    from pydantic import BaseModel as _ConfigBaseModel

    _USES_BASE_SETTINGS = False


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


def _resolved_env() -> dict[str, str]:
    values = _read_env_file_values()
    values.update({key: value for key, value in os.environ.items()})
    return values


def _merged_env_values() -> dict[str, str]:
    return _resolved_env()


def _env_str(env: dict[str, str], key: str, default: str = "") -> str:
    value = env.get(key)
    if value is None:
        return default
    value = str(value).strip()
    if value == "":
        return default
    return value


def _env_int(env: dict[str, str], key: str, default: int) -> int:
    value = _env_str(env, key, "")
    if value == "":
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"Invalid integer for {key}: {value}") from exc


def _env_float(env: dict[str, str], key: str, default: float) -> float:
    value = _env_str(env, key, "")
    if value == "":
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"Invalid float for {key}: {value}") from exc


def _env_bool(env: dict[str, str], key: str, default: bool) -> bool:
    value = _env_str(env, key, "")
    if value == "":
        return default
    normalized = value.lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean for {key}: {value}")


def _env_csv(env: dict[str, str], key: str) -> list[str]:
    value = _env_str(env, key, "")
    return [
        item.strip()
        for item in value.split(",")
        if item.strip()
    ]


def _env_llm_profiles(env: dict[str, str] | None = None) -> list[dict[str, str]]:
    env = env or _resolved_env()
    profiles: list[dict[str, str]] = []
    kimi_api_key = (
        _env_str(env, "KIMI_API_KEY")
        or _env_str(env, "MOONSHOT_API_KEY")
    )
    if kimi_api_key:
        profiles.append(
            {
                "id": "env-kimi",
                "name": "Kimi (.env)",
                "api_key": kimi_api_key,
                "base_url": (
                    _env_str(env, "KIMI_BASE_URL")
                    or _env_str(env, "MOONSHOT_BASE_URL")
                    or DEFAULT_MOONSHOT_BASE_URL
                ),
                "model": (
                    _env_str(env, "KIMI_MODEL")
                    or _env_str(env, "MOONSHOT_MODEL")
                    or DEFAULT_MOONSHOT_MODEL
                ),
            }
        )

    deepseek_api_key = _env_str(env, "DEEPSEEK_API_KEY")
    if deepseek_api_key:
        profiles.append(
            {
                "id": "env-deepseek",
                "name": "DeepSeek (.env)",
                "api_key": deepseek_api_key,
                "base_url": _env_str(
                    env,
                    "DEEPSEEK_BASE_URL",
                    DEFAULT_DEEPSEEK_BASE_URL,
                ),
                "model": _env_str(
                    env,
                    "DEEPSEEK_MODEL",
                    DEFAULT_DEEPSEEK_MODEL,
                ),
            }
        )
    return profiles


def _env_values() -> tuple[dict[str, object], set[str]]:
    env = _resolved_env()
    explicit_keys: set[str] = set()

    def mark(field: str, *env_keys: str) -> None:
        if any(_env_str(env, key, "") != "" for key in env_keys):
            explicit_keys.add(field)

    def tracked_str(field: str, key: str, default: str = "") -> str:
        mark(field, key)
        return _env_str(env, key, default)

    def tracked_bool(field: str, key: str, default: bool = False) -> bool:
        mark(field, key)
        return _env_bool(env, key, default)

    def tracked_int(field: str, key: str, default: int = 0) -> int:
        mark(field, key)
        return _env_int(env, key, default)

    def tracked_csv(field: str, key: str) -> list[str]:
        mark(field, key)
        return _env_csv(env, key)

    database_url = (
        _env_str(env, "FORWIN_DATABASE_URL")
        or _env_str(env, LEGACY_DATABASE_PATH_ENV)
        or DEFAULT_DATABASE_URL
    )
    values: dict[str, object] = {
        "database_url": database_url,
        "db_path": database_url,
        "artifact_root": _env_str(env, "FORWIN_ARTIFACT_ROOT", "data/artifacts"),
        "artifact_backend": _env_str(env, "FORWIN_ARTIFACT_BACKEND", "local"),
        "minio_endpoint": _env_str(env, "FORWIN_MINIO_ENDPOINT"),
        "minio_access_key": _env_str(env, "FORWIN_MINIO_ACCESS_KEY"),
        "minio_secret_key": _env_str(env, "FORWIN_MINIO_SECRET_KEY"),
        "minio_bucket": _env_str(env, "FORWIN_MINIO_BUCKET", "forwin-artifacts"),
        "minio_prefix": _env_str(env, "FORWIN_MINIO_PREFIX", "artifacts"),
        "minio_secure": _env_bool(env, "FORWIN_MINIO_SECURE", False),
        "retrieval_backend": _env_str(env, "FORWIN_RETRIEVAL_BACKEND", "qdrant"),
        "retrieval_root": _env_str(env, "FORWIN_RETRIEVAL_ROOT", "data/retrieval"),
        "qdrant_url": _env_str(env, "FORWIN_QDRANT_URL", DEFAULT_QDRANT_URL),
        "qdrant_collection": _env_str(
            env, "FORWIN_QDRANT_COLLECTION", "chapter_memories"
        ),
        "llm_kb_qdrant_collection": _env_str(
            env, "FORWIN_LLM_KB_QDRANT_COLLECTION", "llm_kb_vectors"
        ),
        "embedding_backend": _env_str(env, "FORWIN_EMBEDDING_BACKEND", "hash"),
        "embedding_base_url": _env_str(
            env,
            "FORWIN_EMBEDDING_BASE_URL",
            _env_str(env, "MINIMAX_BASE_URL", DEFAULT_MINIMAX_BASE_URL),
        ),
        "embedding_api_key": _env_str(
            env,
            "FORWIN_EMBEDDING_API_KEY",
            _env_str(env, "MINIMAX_API_KEY"),
        ),
        "embedding_model": _env_str(env, "FORWIN_EMBEDDING_MODEL"),
        "embedding_dims": _env_int(env, "FORWIN_EMBEDDING_DIMS", 64),
        "runtime_settings_path": _env_str(
            env,
            "FORWIN_RUNTIME_SETTINGS_PATH", "data/runtime_settings.json"
        ),
        "observability_enabled": _env_bool(env, "FORWIN_OBSERVABILITY_ENABLED", True),
        "observability_performance_enabled": _env_bool(
            env, "FORWIN_OBSERVABILITY_PERFORMANCE_ENABLED", True
        ),
        "observability_span_sample_rate": _env_float(
            env, "FORWIN_OBSERVABILITY_SPAN_SAMPLE_RATE", 1.0
        ),
        "observability_slow_span_threshold_ms": _env_int(
            env, "FORWIN_OBSERVABILITY_SLOW_SPAN_THRESHOLD_MS", 1000
        ),
        "observability_record_db_spans": _env_bool(
            env, "FORWIN_OBSERVABILITY_RECORD_DB_SPANS", True
        ),
        "observability_record_payload_sizes": _env_bool(
            env, "FORWIN_OBSERVABILITY_RECORD_PAYLOAD_SIZES", True
        ),
        "retention_cleanup_on_startup": _env_bool(
            env, "FORWIN_RETENTION_CLEANUP_ON_STARTUP", True
        ),
        "performance_span_retention_days": _env_int(
            env, "FORWIN_PERFORMANCE_SPAN_RETENTION_DAYS", 30
        ),
        "prompt_trace_retention_days": _env_int(
            env, "FORWIN_PROMPT_TRACE_RETENTION_DAYS", 30
        ),
        "candidate_draft_keep_per_chapter": _env_int(
            env, "FORWIN_CANDIDATE_DRAFT_KEEP_PER_CHAPTER", 5
        ),
        "publisher_extension_api_key": _env_str(
            env,
            "FORWIN_PUBLISHER_EXTENSION_API_KEY",
            _env_str(env, "PUBLISHER_EXTENSION_API_KEY"),
        ),
        "publisher_session_secret": _env_str(
            env, "FORWIN_PUBLISHER_SESSION_SECRET"
        ),
        "publisher_session_encryption_required": _env_bool(
            env, "FORWIN_PUBLISHER_SESSION_ENCRYPTION_REQUIRED", False
        ),
        "publisher_preferred_client_id": _env_str(
            env,
            "FORWIN_PUBLISHER_PREFERRED_CLIENT_ID", ""
        ),
        "publisher_strict_preferred_client": _env_bool(
            env, "FORWIN_PUBLISHER_STRICT_PREFERRED_CLIENT", False
        ),
        "http_bind": _env_str(env, "FORWIN_HTTP_BIND", "127.0.0.1"),
        "http_port": _env_int(env, "FORWIN_HTTP_PORT", 8899),
        "http_basic_user": _env_str(env, "FORWIN_HTTP_BASIC_USER"),
        "http_basic_password": _env_str(env, "FORWIN_HTTP_BASIC_PASSWORD"),
        "http_basic_exempt_paths": tuple(
            _env_csv(env, "FORWIN_HTTP_BASIC_EXEMPT_PATHS")
        )
        or DEFAULT_HTTP_BASIC_EXEMPT_PATHS,
        "allow_unauthenticated_lan": _env_bool(
            env, "FORWIN_ALLOW_UNAUTHENTICATED_LAN", False
        ),
        "allow_bind_all_interfaces": _env_bool(
            env, "FORWIN_ALLOW_BIND_ALL_INTERFACES", False
        ),
        "minimax_api_key": _env_str(env, "MINIMAX_API_KEY"),
        "minimax_base_url": _env_str(
            env,
            "MINIMAX_BASE_URL", DEFAULT_MINIMAX_BASE_URL
        ),
        "minimax_model": _env_str(env, "MINIMAX_MODEL", DEFAULT_MINIMAX_MODEL),
        "llm_env_profiles": _env_llm_profiles(env),
        "llm_timeout_seconds": _env_float(env, "LLM_TIMEOUT_SECONDS", 90.0),
        "llm_retry_attempts": _env_int(env, "LLM_RETRY_ATTEMPTS", 2),
        "llm_retry_initial_delay_seconds": _env_float(
            env, "LLM_RETRY_INITIAL_DELAY_SECONDS", 2.0
        ),
        "llm_retry_max_delay_seconds": _env_float(
            env, "LLM_RETRY_MAX_DELAY_SECONDS", 15.0
        ),
        "scene_call_timeout_seconds": _env_float(
            env, "SCENE_CALL_TIMEOUT_SECONDS", 90.0
        ),
        "quality_profile": tracked_str(
            "quality_profile", "FORWIN_QUALITY_PROFILE", "standard"
        ),
        "max_chapter_chars": tracked_int(
            "max_chapter_chars", "MAX_CHAPTER_CHARS", 3200
        ),
        "min_chapter_chars": tracked_int(
            "min_chapter_chars", "MIN_CHAPTER_CHARS", 2500
        ),
        "target_chapter_chars": tracked_int(
            "target_chapter_chars", "TARGET_CHAPTER_CHARS", 2800
        ),
        "prompt_budget_chars": _env_int(env, "PROMPT_BUDGET_CHARS", 12000),
        "writer_mode": tracked_str("writer_mode", "WRITER_MODE", "scene"),
        "operation_mode": tracked_str("operation_mode", "OPERATION_MODE", "blackbox"),
        "book_state_layers": tracked_csv("book_state_layers", "FORWIN_BOOK_STATE_LAYERS")
        or ["world", "map", "cognition", "narrative"],
        "hard_floor_gate_enabled": tracked_bool(
            "hard_floor_gate_enabled", "FORWIN_HARD_FLOOR_GATE_ENABLED", False
        ),
        "context_recency_window_chapters": tracked_int(
            "context_recency_window_chapters",
            "FORWIN_CONTEXT_RECENCY_WINDOW_CHAPTERS",
            0,
        ),
        "map_movement_review_enabled": tracked_bool(
            "map_movement_review_enabled", "FORWIN_MAP_MOVEMENT_REVIEW_ENABLED", True
        ),
        "personality_review_enabled": tracked_bool(
            "personality_review_enabled", "FORWIN_PERSONALITY_REVIEW_ENABLED", True
        ),
        "canon_quality_review_in_hub_enabled": tracked_bool(
            "canon_quality_review_in_hub_enabled",
            "FORWIN_CANON_QUALITY_REVIEW_IN_HUB_ENABLED",
            True,
        ),
        "freeze_failed_candidates": tracked_bool(
            "freeze_failed_candidates", "FREEZE_FAILED_CANDIDATES", True
        ),
        "review_interval_chapters": tracked_int(
            "review_interval_chapters", "REVIEW_INTERVAL_CHAPTERS", 0
        ),
        "progression_mode": _env_str(
            env, "PROGRESSION_MODE", "serial_canon_band_guard"
        ),
        "auto_band_checkpoint": tracked_bool(
            "auto_band_checkpoint", "AUTO_BAND_CHECKPOINT", True
        ),
        "band_warn_action": _env_str(env, "BAND_WARN_ACTION", "pause"),
        "manual_checkpoints_enabled": tracked_bool(
            "manual_checkpoints_enabled", "MANUAL_CHECKPOINTS_ENABLED", True
        ),
        "future_constraints_enabled": tracked_bool(
            "future_constraints_enabled", "FUTURE_CONSTRAINTS_ENABLED", True
        ),
        "generation_audit_interval_chapters": tracked_int(
            "generation_audit_interval_chapters",
            "GENERATION_AUDIT_INTERVAL_CHAPTERS",
            0,
        ),
        "generation_audit_pause_enabled": tracked_bool(
            "generation_audit_pause_enabled", "GENERATION_AUDIT_PAUSE_ENABLED", False
        ),
        "legacy_provisional_blocking": _env_bool(
            env, "FORWIN_LEGACY_PROVISIONAL_BLOCKING", False
        ),
        "world_v4_compat_write_enabled": tracked_bool(
            "world_v4_compat_write_enabled", "FORWIN_WORLD_V4_COMPAT_WRITE", False
        ),
        "enable_world_v4_debug_api": _env_bool(
            env, "FORWIN_ENABLE_COMPAT_DEBUG_API", False
        ),
        "skill_runtime_enabled": _env_bool(
            env, "FORWIN_SKILL_RUNTIME_ENABLED", True
        ),
        "skill_registry_path": _env_str(
            env, "FORWIN_SKILL_REGISTRY_PATH", "forwin_skills"
        ),
        "skill_strictness": _env_str(env, "FORWIN_SKILL_STRICTNESS", "normal"),
        "enabled_skill_groups": _env_csv(env, "FORWIN_ENABLED_SKILL_GROUPS"),
        "disabled_skill_ids": _env_csv(env, "FORWIN_DISABLED_SKILL_IDS"),
        "default_scene_count": _env_int(env, "DEFAULT_SCENE_COUNT", 3),
        "max_scene_count": _env_int(env, "MAX_SCENE_COUNT", 4),
        "context_budget_chars": _env_int(env, "CONTEXT_BUDGET_CHARS", 6000),
        "retrieval_max_entities": _env_int(env, "RETRIEVAL_MAX_ENTITIES", 8),
        "retrieval_max_threads": _env_int(env, "RETRIEVAL_MAX_THREADS", 4),
        "retrieval_max_summaries": _env_int(env, "RETRIEVAL_MAX_SUMMARIES", 3),
        "pacing_window_size": _env_int(env, "PACING_WINDOW_SIZE", 3),
        "stale_thread_window": _env_int(env, "STALE_THREAD_WINDOW", 3),
        "pacing_min_avg_chars": _env_int(env, "PACING_MIN_AVG_CHARS", 1600),
        "pacing_max_avg_chars": _env_int(env, "PACING_MAX_AVG_CHARS", 3800),
        "phase_active_thread_limit": _env_int(env, "PHASE_ACTIVE_THREAD_LIMIT", 20),
        "replan_cooldown_chapters": _env_int(env, "REPLAN_COOLDOWN_CHAPTERS", 3),
        "blackbox_writer_attention_retries": _env_int(
            env, "BLACKBOX_WRITER_ATTENTION_RETRIES", 3
        ),
        "experience_review_enabled": tracked_bool(
            "experience_review_enabled", "EXPERIENCE_REVIEW_ENABLED", True
        ),
        "lint_review_enabled": tracked_bool(
            "lint_review_enabled", "LINT_REVIEW_ENABLED", True
        ),
        "review_fail_max_rewrites": tracked_int(
            "review_fail_max_rewrites", "REVIEW_FAIL_MAX_REWRITES", 3
        ),
        "repair_model_sequence": _env_csv(
            env,
            "FORWIN_REPAIR_MODEL_SEQUENCE",
        )
        or [
            "deepseek-reasoner",
            "deepseek-reasoner",
            "gpt-5.3-codex-spark",
        ],
        "canon_quality_gate": tracked_str(
            "canon_quality_gate", "FORWIN_CANON_QUALITY_GATE", "strict"
        ),
        "chapter_review_form_mode": _env_str(env, "FORWIN_CHAPTER_REVIEW_FORM_MODE", "primary"),
        "chapter_review_form_min_blocking_confidence": _env_float(
            env, "FORWIN_CHAPTER_REVIEW_FORM_MIN_BLOCKING_CONFIDENCE", 0.8
        ),
        "chapter_review_form_max_llm_retries": _env_int(env, "FORWIN_CHAPTER_REVIEW_FORM_MAX_LLM_RETRIES", 1),
        "chapter_review_form_token_budget_chars": _env_int(env, "FORWIN_CHAPTER_REVIEW_FORM_TOKEN_BUDGET_CHARS", 8000),
        "form_blocking_character_dead": _env_str(env, "FORWIN_FORM_BLOCKING_CHARACTER_DEAD", "error"),
        "form_blocking_character_wounded": _env_str(env, "FORWIN_FORM_BLOCKING_CHARACTER_WOUNDED", "warning"),
        "form_blocking_character_captured": _env_str(env, "FORWIN_FORM_BLOCKING_CHARACTER_CAPTURED", "error"),
        "form_blocking_countdown_inconsistent": _env_str(env, "FORWIN_FORM_BLOCKING_COUNTDOWN_INCONSISTENT", "error"),
        "form_blocking_countdown_reset": _env_str(env, "FORWIN_FORM_BLOCKING_COUNTDOWN_RESET", "warning"),
        "form_blocking_countdown_advanced": _env_str(env, "FORWIN_FORM_BLOCKING_COUNTDOWN_ADVANCED", "warning"),
        "form_blocking_obligation_unaddressed": _env_str(env, "FORWIN_FORM_BLOCKING_OBLIGATION_UNADDRESSED", "error"),
        "form_blocking_obligation_partial": _env_str(env, "FORWIN_FORM_BLOCKING_OBLIGATION_PARTIAL", "warning"),
        "form_blocking_signal_persisting": _env_str(env, "FORWIN_FORM_BLOCKING_SIGNAL_PERSISTING", "error"),
        "form_blocking_signal_worsened": _env_str(env, "FORWIN_FORM_BLOCKING_SIGNAL_WORSENED", "error"),
        "form_blocking_final_dangling": _env_str(env, "FORWIN_FORM_BLOCKING_FINAL_DANGLING", "error"),
        "form_blocking_final_denied": _env_str(env, "FORWIN_FORM_BLOCKING_FINAL_DENIED", "error"),
        "reviewer_quality_mode": tracked_str(
            "reviewer_quality_mode", "FORWIN_REVIEWER_QUALITY_MODE", "hybrid"
        ),
        "planning_audit_mode": tracked_str(
            "planning_audit_mode", "FORWIN_PLANNING_AUDIT_MODE", "hybrid"
        ),
        "plan_patch_validation_mode": tracked_str(
            "plan_patch_validation_mode", "FORWIN_PLAN_PATCH_VALIDATION_MODE", "hybrid"
        ),
        "final_gate_mode": tracked_str(
            "final_gate_mode", "FORWIN_FINAL_GATE_MODE", "hybrid"
        ),
        "band_checkpoint_mode": tracked_str(
            "band_checkpoint_mode", "FORWIN_BAND_CHECKPOINT_MODE", "hybrid"
        ),
        "final_completion_gate": _env_str(env, "FORWIN_FINAL_COMPLETION_GATE", "strict"),
        "style_telemetry_mode": _env_str(env, "FORWIN_STYLE_TELEMETRY_MODE", "warn"),
        "phase4_use_llm": tracked_bool("phase4_use_llm", "PHASE4_USE_LLM", True),
        "codex_enabled": _env_bool(env, "FORWIN_CODEX_ENABLED", False),
        "codex_bridge_url": _env_str(
            env, "FORWIN_CODEX_BRIDGE_URL", "http://host.docker.internal:8897"
        ),
        "codex_bridge_token": _env_str(env, "FORWIN_CODEX_BRIDGE_TOKEN"),
        "codex_max_concurrent": _env_int(env, "FORWIN_CODEX_MAX_CONCURRENT", 1),
        "codex_sync_timeout_seconds": _env_float(
            env, "FORWIN_CODEX_SYNC_TIMEOUT_SECONDS", 90.0
        ),
        "codex_job_timeout_seconds": _env_float(
            env, "FORWIN_CODEX_JOB_TIMEOUT_SECONDS", 900.0
        ),
        "feedback_cooldown_chapters": _env_int(
            env, "FEEDBACK_COOLDOWN_CHAPTERS", 3
        ),
        "comment_to_reader_ratio": _env_int(env, "COMMENT_TO_READER_RATIO", 80),
        "temperature": _env_float(env, "TEMPERATURE", 0.85),
        "max_tokens": _env_int(env, "MAX_TOKENS", 16384),
    }
    return values, explicit_keys


PULP_OVERRIDES: dict[str, object] = {
    "writer_mode": "single",
    "operation_mode": "blackbox",
    "review_interval_chapters": 0,
    "experience_review_enabled": False,
    "lint_review_enabled": True,
    "canon_quality_gate": "fatal_only",
    "freeze_failed_candidates": False,
    "review_fail_max_rewrites": 0,
    "auto_band_checkpoint": False,
    "manual_checkpoints_enabled": False,
    "future_constraints_enabled": False,
    "generation_audit_interval_chapters": 0,
    "generation_audit_pause_enabled": False,
    "world_v4_compat_write_enabled": False,
    "phase4_use_llm": False,
    "reviewer_quality_mode": "deterministic",
    "planning_audit_mode": "off",
    "plan_patch_validation_mode": "off",
    "final_gate_mode": "off",
    "band_checkpoint_mode": "off",
    "min_chapter_chars": 1800,
    "target_chapter_chars": 2400,
    "max_chapter_chars": 3000,
    "book_state_layers": ["world"],
    "hard_floor_gate_enabled": True,
    "context_recency_window_chapters": 50,
    "map_movement_review_enabled": False,
    "personality_review_enabled": False,
    "canon_quality_review_in_hub_enabled": False,
}

PREMIUM_OVERRIDES: dict[str, object] = {}


def apply_quality_profile(config: "Config", *, explicit_keys: set[str]) -> "Config":
    profile = (
        str(getattr(config, "quality_profile", "standard") or "standard")
        .strip()
        .lower()
    )
    if profile == "pulp":
        overrides = PULP_OVERRIDES
    elif profile == "premium":
        overrides = PREMIUM_OVERRIDES
    else:
        return config
    update = {
        key: deepcopy(value)
        for key, value in overrides.items()
        if key not in explicit_keys
    }
    return config.model_copy(update=update)


class _ConfigFields:
    database_url: str = DEFAULT_DATABASE_URL
    db_path: str = DEFAULT_DATABASE_URL
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
    qdrant_url: str = DEFAULT_QDRANT_URL
    qdrant_collection: str = "chapter_memories"
    llm_kb_qdrant_collection: str = "llm_kb_vectors"
    embedding_backend: str = "hash"
    embedding_base_url: str = DEFAULT_MINIMAX_BASE_URL
    embedding_api_key: str = ""
    embedding_model: str = ""
    embedding_dims: int = 64
    runtime_settings_path: str = "data/runtime_settings.json"
    observability_enabled: bool = True
    observability_performance_enabled: bool = True
    observability_span_sample_rate: float = 1.0
    observability_slow_span_threshold_ms: int = 1000
    observability_record_db_spans: bool = True
    observability_record_payload_sizes: bool = True
    retention_cleanup_on_startup: bool = True
    performance_span_retention_days: int = 30
    prompt_trace_retention_days: int = 30
    candidate_draft_keep_per_chapter: int = 5
    publisher_extension_api_key: str = ""
    publisher_session_secret: str = ""
    publisher_session_encryption_required: bool = False
    publisher_preferred_client_id: str = ""
    publisher_strict_preferred_client: bool = False
    http_bind: str = "127.0.0.1"
    http_port: int = 8899
    http_basic_user: str = ""
    http_basic_password: str = ""
    http_basic_exempt_paths: tuple[str, ...] = DEFAULT_HTTP_BASIC_EXEMPT_PATHS
    allow_unauthenticated_lan: bool = False
    allow_bind_all_interfaces: bool = False
    minimax_api_key: str = ""
    minimax_base_url: str = DEFAULT_MINIMAX_BASE_URL
    minimax_model: str = DEFAULT_MINIMAX_MODEL
    llm_env_profiles: list[dict[str, str]] = []
    llm_timeout_seconds: float = 90.0
    llm_retry_attempts: int = 2
    llm_retry_initial_delay_seconds: float = 2.0
    llm_retry_max_delay_seconds: float = 15.0
    scene_call_timeout_seconds: float = 90.0
    quality_profile: Literal["pulp", "standard", "premium"] = "standard"
    max_chapter_chars: int = 3200
    min_chapter_chars: int = 2500
    target_chapter_chars: int = 2800
    prompt_budget_chars: int = 12000
    writer_mode: str = "scene"
    operation_mode: str = "blackbox"
    book_state_layers: list[str] = ["world", "map", "cognition", "narrative"]
    hard_floor_gate_enabled: bool = False
    context_recency_window_chapters: int = 0
    map_movement_review_enabled: bool = True
    personality_review_enabled: bool = True
    canon_quality_review_in_hub_enabled: bool = True
    freeze_failed_candidates: bool = True
    review_interval_chapters: int = 0
    progression_mode: str = "serial_canon_band_guard"
    auto_band_checkpoint: bool = True
    band_warn_action: str = "pause"
    manual_checkpoints_enabled: bool = True
    future_constraints_enabled: bool = True
    generation_audit_interval_chapters: int = 0
    generation_audit_pause_enabled: bool = False
    legacy_provisional_blocking: bool = False
    world_v4_compat_write_enabled: bool = False
    enable_world_v4_debug_api: bool = False
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
    repair_model_sequence: list[str] = [
        "deepseek-reasoner",
        "deepseek-reasoner",
        "gpt-5.3-codex-spark",
    ]
    canon_quality_gate: str = "strict"
    chapter_review_form_mode: str = "primary"
    chapter_review_form_min_blocking_confidence: float = 0.8
    chapter_review_form_max_llm_retries: int = 1
    chapter_review_form_token_budget_chars: int = 8000
    form_blocking_character_dead: str = "error"
    form_blocking_character_wounded: str = "warning"
    form_blocking_character_captured: str = "error"
    form_blocking_countdown_inconsistent: str = "error"
    form_blocking_countdown_reset: str = "warning"
    form_blocking_countdown_advanced: str = "warning"
    form_blocking_obligation_unaddressed: str = "error"
    form_blocking_obligation_partial: str = "warning"
    form_blocking_signal_persisting: str = "error"
    form_blocking_signal_worsened: str = "error"
    form_blocking_final_dangling: str = "error"
    form_blocking_final_denied: str = "error"
    reviewer_quality_mode: str = "hybrid"
    planning_audit_mode: str = "hybrid"
    plan_patch_validation_mode: str = "hybrid"
    final_gate_mode: str = "hybrid"
    band_checkpoint_mode: str = "hybrid"
    final_completion_gate: str = "strict"
    style_telemetry_mode: str = "warn"
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
        values, explicit_keys = _env_values()
        config = cls(**values)
        return apply_quality_profile(config, explicit_keys=explicit_keys)


class Config(_ConfigFields, _ConfigBaseModel):  # type: ignore[misc]
    if _USES_BASE_SETTINGS:
        model_config = {"env_prefix": "", "extra": "forbid"}
    else:
        model_config = {"extra": "forbid"}

    def __init__(self, **data: object) -> None:
        super().__init__(**data)
        database_url = str(getattr(self, "database_url", "") or "").strip()
        legacy_db_path = str(getattr(self, "db_path", "") or "").strip()
        if (
            legacy_db_path
            and legacy_db_path != DEFAULT_DATABASE_URL
            and database_url == DEFAULT_DATABASE_URL
        ):
            object.__setattr__(self, "database_url", legacy_db_path)
        elif database_url:
            object.__setattr__(self, "db_path", database_url)
        user = str(self.http_basic_user or "")
        password = str(self.http_basic_password or "")
        if bool(user) != bool(password):
            raise ValueError(
                "FORWIN_HTTP_BASIC_USER and FORWIN_HTTP_BASIC_PASSWORD must be set together"
            )
        bind = str(self.http_bind or "").strip().lower()
        local_binds = {"", "127.0.0.1", "localhost", "::1"}
        binds_all = bind in {"0.0.0.0", "::"}
        public_bind = bind not in local_binds
        if binds_all and not bool(self.allow_bind_all_interfaces):
            raise ValueError(
                "FORWIN_HTTP_BIND binds all interfaces. Set "
                "FORWIN_ALLOW_BIND_ALL_INTERFACES=true only if this is intentional."
            )
        if public_bind and not (user and password) and not bool(self.allow_unauthenticated_lan):
            raise ValueError(
                "FORWIN_HTTP_BIND is not localhost, but Basic Auth is disabled. "
                "Set FORWIN_HTTP_BASIC_USER/PASSWORD or explicitly set "
                "FORWIN_ALLOW_UNAUTHENTICATED_LAN=true."
            )
        publisher_secret = str(self.publisher_session_secret or "").strip()
        placeholder_secret = publisher_secret.lower().startswith(
            ("change-me", "replace-with", "changeme")
        )
        if publisher_secret and placeholder_secret:
            raise ValueError(
                "FORWIN_PUBLISHER_SESSION_SECRET appears to be a placeholder; "
                "replace it with a real secret."
            )
        publisher_profile_enabled = bool(
            str(self.publisher_extension_api_key or "").strip()
            or str(self.publisher_preferred_client_id or "").strip()
        )
        if publisher_profile_enabled and not publisher_secret:
            raise ValueError(
                "FORWIN_PUBLISHER_SESSION_SECRET must be set when publisher extension "
                "profile is enabled."
            )
        if publisher_profile_enabled and not bool(self.publisher_session_encryption_required):
            raise ValueError(
                "FORWIN_PUBLISHER_SESSION_ENCRYPTION_REQUIRED=true must be set when "
                "publisher extension profile is enabled."
            )
        if self.publisher_session_encryption_required and not publisher_secret:
            raise ValueError(
                "FORWIN_PUBLISHER_SESSION_SECRET must be set when "
                "FORWIN_PUBLISHER_SESSION_ENCRYPTION_REQUIRED=true"
            )

    @property
    def writer(self) -> WriterProfile:
        return WriterProfile.from_values(
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            default_scene_count=self.default_scene_count,
            max_scene_count=self.max_scene_count,
            min_chapter_chars=self.min_chapter_chars,
            target_chapter_chars=self.target_chapter_chars,
            max_chapter_chars=self.max_chapter_chars,
            prompt_budget_chars=self.prompt_budget_chars,
        )

    @property
    def llm(self) -> LLMConfig:
        return LLMConfig(
            minimax_api_key=self.minimax_api_key,
            minimax_base_url=self.minimax_base_url,
            minimax_model=self.minimax_model,
            timeout_seconds=self.llm_timeout_seconds,
            retry_attempts=self.llm_retry_attempts,
            max_tokens=self.max_tokens,
        )

    @property
    def storage(self) -> StorageConfig:
        return StorageConfig(
            database_url=self.database_url,
            artifact_backend=self.artifact_backend,
            artifact_root=self.artifact_root,
            retrieval_backend=self.retrieval_backend,
            qdrant_url=self.qdrant_url,
        )

    @property
    def publisher(self) -> PublisherConfig:
        return PublisherConfig(
            extension_api_key=self.publisher_extension_api_key,
            session_secret=self.publisher_session_secret,
            session_encryption_required=self.publisher_session_encryption_required,
        )

    @property
    def observability(self) -> ObservabilityConfig:
        return ObservabilityConfig(
            enabled=self.observability_enabled,
            performance_enabled=self.observability_performance_enabled,
            span_sample_rate=self.observability_span_sample_rate,
        )

    @property
    def governance(self) -> GovernanceConfig:
        return GovernanceConfig(
            progression_mode=self.progression_mode,
            review_interval_chapters=self.review_interval_chapters,
            future_constraints_enabled=self.future_constraints_enabled,
        )

    @property
    def form_blocking_policy(self) -> FormBlockingPolicy:
        return FormBlockingPolicy(
            character_dead=self.form_blocking_character_dead,
            character_wounded=self.form_blocking_character_wounded,
            character_captured=self.form_blocking_character_captured,
            countdown_inconsistent=self.form_blocking_countdown_inconsistent,
            countdown_reset=self.form_blocking_countdown_reset,
            countdown_advanced=self.form_blocking_countdown_advanced,
            obligation_unaddressed=self.form_blocking_obligation_unaddressed,
            obligation_partial=self.form_blocking_obligation_partial,
            signal_persisting=self.form_blocking_signal_persisting,
            signal_worsened=self.form_blocking_signal_worsened,
            final_dangling=self.form_blocking_final_dangling,
            final_denied=self.form_blocking_final_denied,
        )

    @property
    def codex(self) -> CodexConfig:
        return CodexConfig(
            enabled=self.codex_enabled,
            bridge_url=self.codex_bridge_url,
            max_concurrent=self.codex_max_concurrent,
        )
