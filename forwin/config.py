from __future__ import annotations

import os
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from forwin.writer.profile import WriterProfile

if TYPE_CHECKING:
    from forwin.runtime.policy import RuntimePolicy


DEFAULT_MINIMAX_BASE_URL = "https://api.minimaxi.com/v1"
DEFAULT_MINIMAX_MODEL = "MiniMax-M2.7"
DEFAULT_DATABASE_URL = "postgresql+psycopg://forwin:forwin@localhost:5432/forwin"
DEFAULT_MOONSHOT_BASE_URL = "https://api.moonshot.cn/v1"
DEFAULT_MOONSHOT_MODEL = "kimi-k2.5"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_DEEPSEEK_MODEL = "deepseek-chat"
DEFAULT_CODEX_MODEL = "gpt-5.3-codex-spark"
DEFAULT_QDRANT_URL = "http://127.0.0.1:6335"
DEFAULT_EMBEDDING_GATEWAY_URL = "http://10.0.0.150:8080"
DEFAULT_EMBEDDING_MODEL = "all-MiniLM-L6-v2"
DEFAULT_EMBEDDING_DIMS = 384
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
    login_discord_webhook_url: str = ""


class ObservabilityConfig(BaseModel):
    enabled: bool = True
    performance_enabled: bool = True
    span_sample_rate: float = 1.0


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
    default_model: str = DEFAULT_CODEX_MODEL
    max_concurrent: int = 1


class ModelProfileConfig(BaseModel):
    model_config = {"frozen": True, "extra": "forbid"}

    id: str
    name: str
    api_key: str
    base_url: str
    model: str


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


def _env_str(env: dict[str, str], key: str, default: str = "") -> str:
    value = env.get(key)
    if value is None:
        return default
    normalized = str(value).strip()
    return normalized or default


def _env_int(env: dict[str, str], key: str, default: int) -> int:
    value = _env_str(env, key)
    if not value:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"Invalid integer for {key}: {value}") from exc


def _env_float(env: dict[str, str], key: str, default: float) -> float:
    value = _env_str(env, key)
    if not value:
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"Invalid float for {key}: {value}") from exc


def _env_bool(env: dict[str, str], key: str, default: bool) -> bool:
    value = _env_str(env, key)
    if not value:
        return default
    normalized = value.lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean for {key}: {value}")


def _env_csv(env: dict[str, str], key: str) -> list[str]:
    return [item.strip() for item in _env_str(env, key).split(",") if item.strip()]


def _env_llm_profiles(env: dict[str, str]) -> list[dict[str, str]]:
    profiles: list[dict[str, str]] = []
    kimi_api_key = _env_str(env, "KIMI_API_KEY") or _env_str(env, "MOONSHOT_API_KEY")
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
                    env, "DEEPSEEK_BASE_URL", DEFAULT_DEEPSEEK_BASE_URL
                ),
                "model": _env_str(env, "DEEPSEEK_MODEL", DEFAULT_DEEPSEEK_MODEL),
            }
        )
    return profiles


def _infrastructure_env_values() -> dict[str, object]:
    env = _resolved_env()
    return {
        "database_url": _env_str(env, "FORWIN_DATABASE_URL", DEFAULT_DATABASE_URL),
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
        "embedding_backend": _env_str(env, "FORWIN_EMBEDDING_BACKEND", "gateway"),
        "embedding_base_url": _env_str(
            env, "FORWIN_EMBEDDING_BASE_URL", DEFAULT_EMBEDDING_GATEWAY_URL
        ),
        "embedding_api_key": _env_str(env, "FORWIN_EMBEDDING_API_KEY"),
        "embedding_model": _env_str(
            env, "FORWIN_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL
        ),
        "embedding_dims": _env_int(
            env, "FORWIN_EMBEDDING_DIMS", DEFAULT_EMBEDDING_DIMS
        ),
        "embedding_required": _env_bool(env, "FORWIN_EMBEDDING_REQUIRED", False),
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
        "publisher_extension_api_key": _env_str(
            env,
            "FORWIN_PUBLISHER_EXTENSION_API_KEY",
            _env_str(env, "PUBLISHER_EXTENSION_API_KEY"),
        ),
        "publisher_session_secret": _env_str(env, "FORWIN_PUBLISHER_SESSION_SECRET"),
        "publisher_session_encryption_required": _env_bool(
            env, "FORWIN_PUBLISHER_SESSION_ENCRYPTION_REQUIRED", False
        ),
        "publisher_login_discord_webhook_enabled": False,
        "publisher_login_discord_webhook_url": "",
        "publisher_preferred_client_id": _env_str(
            env, "FORWIN_PUBLISHER_PREFERRED_CLIENT_ID"
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
            env, "MINIMAX_BASE_URL", DEFAULT_MINIMAX_BASE_URL
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
        "skill_runtime_enabled": _env_bool(env, "FORWIN_SKILL_RUNTIME_ENABLED", True),
        "skill_registry_path": _env_str(
            env, "FORWIN_SKILL_REGISTRY_PATH", "forwin_skills"
        ),
        "skill_strictness": _env_str(env, "FORWIN_SKILL_STRICTNESS", "normal"),
        "enabled_skill_groups": _env_csv(env, "FORWIN_ENABLED_SKILL_GROUPS"),
        "disabled_skill_ids": _env_csv(env, "FORWIN_DISABLED_SKILL_IDS"),
        "context_budget_chars": _env_int(env, "CONTEXT_BUDGET_CHARS", 6000),
        "retrieval_max_entities": _env_int(env, "RETRIEVAL_MAX_ENTITIES", 8),
        "retrieval_max_threads": _env_int(env, "RETRIEVAL_MAX_THREADS", 4),
        "retrieval_max_summaries": _env_int(env, "RETRIEVAL_MAX_SUMMARIES", 3),
        "phase_active_thread_limit": _env_int(env, "PHASE_ACTIVE_THREAD_LIMIT", 20),
        "chapter_review_form_max_llm_retries": _env_int(
            env, "FORWIN_CHAPTER_REVIEW_FORM_MAX_LLM_RETRIES", 1
        ),
        "chapter_review_form_token_budget_chars": _env_int(
            env, "FORWIN_CHAPTER_REVIEW_FORM_TOKEN_BUDGET_CHARS", 8000
        ),
        "codex_enabled": _env_bool(env, "FORWIN_CODEX_ENABLED", False),
        "codex_bridge_url": _env_str(
            env, "FORWIN_CODEX_BRIDGE_URL", "http://host.docker.internal:8897"
        ),
        "codex_bridge_token": _env_str(env, "FORWIN_CODEX_BRIDGE_TOKEN"),
        "codex_default_model": _env_str(
            env,
            "FORWIN_CODEX_DEFAULT_MODEL",
            _env_str(env, "FORWIN_CODEX_MODEL", DEFAULT_CODEX_MODEL),
        ),
        "codex_max_concurrent": _env_int(env, "FORWIN_CODEX_MAX_CONCURRENT", 1),
        "codex_sync_timeout_seconds": _env_float(
            env, "FORWIN_CODEX_SYNC_TIMEOUT_SECONDS", 90.0
        ),
        "codex_job_timeout_seconds": _env_float(
            env, "FORWIN_CODEX_JOB_TIMEOUT_SECONDS", 900.0
        ),
        "default_scene_count": _env_int(env, "DEFAULT_SCENE_COUNT", 3),
        "max_scene_count": _env_int(env, "MAX_SCENE_COUNT", 4),
        "prompt_budget_chars": _env_int(env, "PROMPT_BUDGET_CHARS", 12000),
        "temperature": _env_float(env, "TEMPERATURE", 0.85),
        "max_tokens": _env_int(env, "MAX_TOKENS", 16384),
    }


class _InfrastructureFields:
    database_url: str = DEFAULT_DATABASE_URL
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
    embedding_backend: str = "gateway"
    embedding_base_url: str = DEFAULT_EMBEDDING_GATEWAY_URL
    embedding_api_key: str = ""
    embedding_model: str = DEFAULT_EMBEDDING_MODEL
    embedding_dims: int = DEFAULT_EMBEDDING_DIMS
    embedding_required: bool = False
    observability_enabled: bool = True
    observability_performance_enabled: bool = True
    observability_span_sample_rate: float = 1.0
    observability_slow_span_threshold_ms: int = 1000
    observability_record_db_spans: bool = True
    observability_record_payload_sizes: bool = True
    retention_cleanup_on_startup: bool = True
    performance_span_retention_days: int = 30
    publisher_extension_api_key: str = ""
    publisher_session_secret: str = ""
    publisher_session_encryption_required: bool = False
    publisher_login_discord_webhook_enabled: bool = False
    publisher_login_discord_webhook_url: str = ""
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
    llm_env_profiles: list[dict[str, str]] = Field(default_factory=list)
    llm_timeout_seconds: float = 90.0
    llm_retry_attempts: int = 2
    llm_retry_initial_delay_seconds: float = 2.0
    llm_retry_max_delay_seconds: float = 15.0
    scene_call_timeout_seconds: float = 90.0
    skill_runtime_enabled: bool = True
    skill_registry_path: str = "forwin_skills"
    skill_strictness: str = "normal"
    enabled_skill_groups: list[str] = Field(default_factory=list)
    disabled_skill_ids: list[str] = Field(default_factory=list)
    context_budget_chars: int = 6000
    retrieval_max_entities: int = 8
    retrieval_max_threads: int = 4
    retrieval_max_summaries: int = 3
    phase_active_thread_limit: int = 20
    chapter_review_form_max_llm_retries: int = 1
    chapter_review_form_token_budget_chars: int = 8000
    codex_enabled: bool = False
    codex_bridge_url: str = "http://host.docker.internal:8897"
    codex_bridge_token: str = ""
    codex_default_model: str = DEFAULT_CODEX_MODEL
    codex_max_concurrent: int = 1
    codex_sync_timeout_seconds: float = 90.0
    codex_job_timeout_seconds: float = 900.0
    default_scene_count: int = 3
    max_scene_count: int = 4
    prompt_budget_chars: int = 12000
    temperature: float = 0.85
    max_tokens: int = 16384

    @classmethod
    def from_env(cls) -> "InfrastructureConfig":
        return cls(**_infrastructure_env_values())


class InfrastructureConfig(_InfrastructureFields, _ConfigBaseModel):  # type: ignore[misc]
    if _USES_BASE_SETTINGS:
        model_config = {"env_prefix": "", "extra": "forbid"}
    else:
        model_config = {"extra": "forbid"}

    def __init__(self, **data: object) -> None:
        super().__init__(**data)
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
        if binds_all and not self.allow_bind_all_interfaces:
            raise ValueError(
                "FORWIN_HTTP_BIND binds all interfaces. Set "
                "FORWIN_ALLOW_BIND_ALL_INTERFACES=true only if this is intentional."
            )
        if public_bind and not (user and password) and not self.allow_unauthenticated_lan:
            raise ValueError(
                "FORWIN_HTTP_BIND is not localhost, but Basic Auth is disabled. "
                "Set FORWIN_HTTP_BASIC_USER/PASSWORD or explicitly set "
                "FORWIN_ALLOW_UNAUTHENTICATED_LAN=true."
            )
        publisher_secret = str(self.publisher_session_secret or "").strip()
        if publisher_secret.lower().startswith(("change-me", "replace-with", "changeme")):
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
        if publisher_profile_enabled and not self.publisher_session_encryption_required:
            raise ValueError(
                "FORWIN_PUBLISHER_SESSION_ENCRYPTION_REQUIRED=true must be set when "
                "publisher extension profile is enabled."
            )
        if self.publisher_session_encryption_required and not publisher_secret:
            raise ValueError(
                "FORWIN_PUBLISHER_SESSION_SECRET must be set when "
                "FORWIN_PUBLISHER_SESSION_ENCRYPTION_REQUIRED=true"
            )

    def resolve_model_profile(self, profile_id: str) -> ModelProfileConfig:
        requested = str(profile_id or "").strip()
        default_profile = ModelProfileConfig(
            id="env-minimax",
            name="MiniMax (.env)",
            api_key=self.minimax_api_key,
            base_url=self.minimax_base_url,
            model=self.minimax_model,
        )
        profiles = [
            ModelProfileConfig.model_validate(item) for item in self.llm_env_profiles
        ]
        if not requested or requested == default_profile.id:
            return default_profile
        for profile in profiles:
            if profile.id == requested:
                return profile
        raise ValueError(f"Unknown model profile: {requested}")

    def writer_profile(self, policy: RuntimePolicy) -> WriterProfile:
        lengths = policy.chapter_length
        return WriterProfile.from_values(
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            default_scene_count=self.default_scene_count,
            max_scene_count=self.max_scene_count,
            min_chapter_chars=lengths.min_chars,
            target_chapter_chars=lengths.target_chars,
            max_chapter_chars=lengths.max_chars,
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
            login_discord_webhook_url=self.publisher_login_discord_webhook_url,
        )

    @property
    def observability(self) -> ObservabilityConfig:
        return ObservabilityConfig(
            enabled=self.observability_enabled,
            performance_enabled=self.observability_performance_enabled,
            span_sample_rate=self.observability_span_sample_rate,
        )

    @property
    def codex(self) -> CodexConfig:
        return CodexConfig(
            enabled=self.codex_enabled,
            bridge_url=self.codex_bridge_url,
            default_model=self.codex_default_model,
            max_concurrent=self.codex_max_concurrent,
        )
