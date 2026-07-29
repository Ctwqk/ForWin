#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import functools
import hashlib
import ipaddress
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[2]
ARTIFACT_DIR = ROOT / ".artifacts/rc-candidate"
COMPOSE_FILE = ROOT / "docker-compose.yml"
COMPOSE_OVERRIDE = ARTIFACT_DIR / "docker-compose.recovery.yml"
PROJECT = "forwin-v5-recovery"
RECOVERY_PROJECT_PREFIX = "forwin-v5-recovery"
DATABASE_VOLUME_SUFFIX = "postgres-data"
CANDIDATE_MANIFEST_ENV = "FORWIN_RECOVERY_CANDIDATE_MANIFEST"
RUNTIME_ENV_FILE_ENV = "FORWIN_RECOVERY_ENV_FILE"
PROVIDER_ENV_FILE_ENV = "FORWIN_RECOVERY_PROVIDER_ENV_FILE"
EVIDENCE_DIR_ENV = "FORWIN_RECOVERY_EVIDENCE_DIR"
HOST_ENV_ALLOWLIST = frozenset(
    {
        "HOME",
        "LANG",
        "LC_ALL",
        "LOGNAME",
        "PATH",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TMPDIR",
        "USER",
    }
)
CONTROL_ENV_KEYS = frozenset(
    {
        "COMPOSE_DISABLE_ENV_FILE",
        "COMPOSE_ENV_FILES",
        "COMPOSE_FILE",
        "COMPOSE_PATH_SEPARATOR",
        "COMPOSE_PROFILES",
        "COMPOSE_PROJECT_NAME",
        "DOCKER_CERT_PATH",
        "DOCKER_CONFIG",
        "DOCKER_CONTEXT",
        "DOCKER_HOST",
        "DOCKER_TLS_VERIFY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_WORK_TREE",
    }
)

SERVICES = (
    "postgres",
    "qdrant",
    "minio",
    "forwin",
    "generation-worker",
    "outbox-worker",
    "forwin-mcp",
    "publisher-worker",
    "publisher-browser",
)
CONTAINER_NAME_SUFFIXES = {
    "forwin": "api",
    "generation-worker": "generation-worker",
    "outbox-worker": "outbox-worker",
    "postgres": "postgres",
    "qdrant": "qdrant",
    "forwin-mcp": "mcp",
    "publisher-worker": "publisher-worker",
    "publisher-browser": "publisher-browser",
    "minio": "minio",
}
APPLICATION_SERVICES = (
    "forwin",
    "generation-worker",
    "outbox-worker",
    "forwin-mcp",
    "publisher-worker",
    "publisher-browser",
)
DEPENDENCY_SERVICES = ("postgres", "qdrant", "minio")
HEALTHCHECK_SERVICES = frozenset(
    {"postgres", "qdrant", "forwin", "forwin-mcp", "publisher-browser"}
)

FAULT_SERVICES = {
    "generation-worker",
    "qdrant",
    "minio",
    "outbox-worker",
    "publisher-worker",
    "publisher-browser",
}

CRASH_SERVICES = {"generation-worker", "publisher-worker"}
TERMINAL_ACTIONS = frozenset(
    {"destroyed", "setup_blocked", "interrupted_cleanup"}
)
CLEANUP_TIMEOUT_SECONDS = 60
RISK_FAULT_KINDS = frozenset(
    {
        "publisher_captcha",
        "publisher_mfa",
        "publisher_account_risk",
    }
)
PRIMARY_FAULT_SERVICE = {
    "generation_worker_precommit_crash": "generation-worker",
    "generation_worker_postcommit_crash": "generation-worker",
    "qdrant_unavailable": "qdrant",
    "projection_consumer_unavailable": "outbox-worker",
    "minio_pre_canon_unavailable": "minio",
    "minio_post_canon_unavailable": "minio",
    "publisher_backend_unavailable": "publisher-worker",
    "publisher_browser_unavailable": "publisher-browser",
}
SUPPORTED_RECOVERY_FAULTS = frozenset(
    {*PRIMARY_FAULT_SERVICE, *RISK_FAULT_KINDS}
)
SETUP_HOLD_PURPOSES = frozenset({"auxiliary", "pre-fault-boundary"})
_FAULT_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_HOLD_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_ABORT_STAGE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_RUN_ID_PATTERN = re.compile(r"[a-f0-9]{32}")
PUBLISHER_COVER_ROOT = "/app/data/publisher_covers"
RECOVERY_SENTINEL_TABLE = "forwin_recovery_run_sentinel"

ISOLATED_DATABASE_URL = (
    "postgresql+psycopg://forwin:forwin@postgres:5432/forwin"
)
GENERATION_WORKER_DATABASE_URL = (
    f"{ISOLATED_DATABASE_URL}"
    "?application_name=forwin-recovery-generation-worker"
)
OUTBOX_WORKER_DATABASE_URL = (
    f"{ISOLATED_DATABASE_URL}"
    "?application_name=forwin-recovery-outbox-worker"
)
PUBLISHER_WORKER_DATABASE_URL = (
    f"{ISOLATED_DATABASE_URL}"
    "?application_name=forwin-recovery-publisher-worker"
)
SERVICE_DATABASE_URLS = {
    "generation-worker": GENERATION_WORKER_DATABASE_URL,
    "outbox-worker": OUTBOX_WORKER_DATABASE_URL,
    "publisher-worker": PUBLISHER_WORKER_DATABASE_URL,
}
ISOLATED_QDRANT_URL = "http://qdrant:6333"
ISOLATED_MINIO_ENDPOINT = "minio:9000"
ISOLATED_API_BASE_URL = "http://forwin:8899"
ISOLATED_MINIO_ACCESS_KEY = "forwin-recovery"
ISOLATED_MINIO_SECRET_KEY = "forwin-recovery-secret"

COMPOSE_ENV = {
    "FORWIN_DATABASE_URL": ISOLATED_DATABASE_URL,
    "FORWIN_QDRANT_URL": ISOLATED_QDRANT_URL,
    "FORWIN_ARTIFACT_BACKEND": "minio",
    "FORWIN_MINIO_ENDPOINT": ISOLATED_MINIO_ENDPOINT,
    "FORWIN_MINIO_ACCESS_KEY": ISOLATED_MINIO_ACCESS_KEY,
    "FORWIN_MINIO_SECRET_KEY": ISOLATED_MINIO_SECRET_KEY,
    "FORWIN_MINIO_BUCKET": "forwin-recovery-artifacts",
    "FORWIN_MINIO_PREFIX": "artifacts",
    "FORWIN_MINIO_SECURE": "false",
    "FORWIN_API_BASE_URL": ISOLATED_API_BASE_URL,
    "FORWIN_PUBLISHER_BROWSER_BACKEND_URL": ISOLATED_API_BASE_URL,
    "FORWIN_POSTGRES_USER": "forwin",
    "FORWIN_POSTGRES_PASSWORD": "forwin",
    "FORWIN_POSTGRES_DB": "forwin",
    "FORWIN_HTTP_BIND": "127.0.0.1",
    "FORWIN_HTTP_PORT": "19099",
    "FORWIN_MCP_DEBUG_BIND": "127.0.0.1:19096",
    "FORWIN_QDRANT_DEBUG_BIND": "127.0.0.1:16337",
    "FORWIN_EXTENSION_DEBUG_BIND": "127.0.0.1:19322",
    "FORWIN_RECOVERY_POSTGRES_BIND": "127.0.0.1:55434",
    "FORWIN_RECOVERY_MINIO_API_BIND": "127.0.0.1:19100",
    "FORWIN_RECOVERY_MINIO_CONSOLE_BIND": "127.0.0.1:19101",
    "FORWIN_GENERATION_WORKER_LEASE_SECONDS": "30",
    "FORWIN_GENERATION_WORKER_POLL_INTERVAL": "1",
    "FORWIN_OUTBOX_WORKER_POLL_INTERVAL": "1",
    "FORWIN_OUTBOX_WORKER_LEASE_SECONDS": "30",
    "FORWIN_OUTBOX_WORKER_HEARTBEAT_INTERVAL_SECONDS": "5",
    "FORWIN_OUTBOX_WORKER_BASE_DELAY_SECONDS": "2",
    "FORWIN_OUTBOX_WORKER_MAX_DELAY_SECONDS": "10",
    "FORWIN_PUBLISHER_WORKER_POLL_INTERVAL": "1",
}
V1_MIGRATION_STEPS = (
    "upgrade_head_initial",
    "alembic_check",
    "downgrade_base",
    "upgrade_head_final",
)
_V1_STALE_REVISION = "v1_stale_revision"
_V1_SCHEMA_INJECT_SCRIPT = f"""
import os
from sqlalchemy import create_engine, text

engine = create_engine(os.environ["FORWIN_DATABASE_URL"])
with engine.begin() as connection:
    connection.execute(
        text("UPDATE alembic_version SET version_num=:revision"),
        {{"revision": "{_V1_STALE_REVISION}"}},
    )
engine.dispose()
"""
_V1_SCHEMA_RESTORE_SCRIPT = """
import os
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from forwin.models.base import alembic_config

database_url = os.environ["FORWIN_DATABASE_URL"]
head = ScriptDirectory.from_config(alembic_config(database_url)).get_current_head()
if not head:
    raise RuntimeError("migration head is missing")
engine = create_engine(database_url)
with engine.begin() as connection:
    connection.execute(
        text("UPDATE alembic_version SET version_num=:revision"),
        {"revision": head},
    )
engine.dispose()
print(head)
"""
_V1_TASK_STATE_SCRIPT = """
import json
import os
from sqlalchemy import create_engine, text

engine = create_engine(os.environ["FORWIN_DATABASE_URL"])
with engine.connect() as connection:
    row = connection.execute(
        text(
            "SELECT count(*) AS total, "
            "count(*) FILTER (WHERE lease_owner<>'') AS leased "
            "FROM generation_tasks"
        )
    ).mappings().one()
engine.dispose()
print(json.dumps({"total": int(row["total"]), "leased": int(row["leased"])}))
"""
_V1_EMBEDDING_SCRIPT = """
import json
import httpx
import socket
from urllib.parse import urlsplit, urlunsplit
from forwin.config import InfrastructureConfig
from forwin.retrieval.memory_index import GatewayTextEmbedder

config = InfrastructureConfig.from_env()
base_url_parts = urlsplit(config.embedding_base_url)
safe_base_url = urlunsplit(
    (
        base_url_parts.scheme,
        base_url_parts.netloc.rsplit("@", 1)[-1],
        base_url_parts.path,
        "",
        "",
    )
)
peer_port = base_url_parts.port or (443 if base_url_parts.scheme == "https" else 80)
with socket.create_connection(
    (base_url_parts.hostname, peer_port),
    timeout=10.0,
) as peer_socket:
    peer_ip = peer_socket.getpeername()[0]
client = httpx.Client(
    timeout=httpx.Timeout(30.0, connect=10.0),
    trust_env=False,
    follow_redirects=False,
)
metadata_response = client.get(
    config.embedding_base_url.rstrip("/") + "/metadata",
)
metadata_response.raise_for_status()
metadata_dims = int(metadata_response.json().get("dimension") or 0)
embedder = GatewayTextEmbedder(
    base_url=config.embedding_base_url,
    dims=config.embedding_dims,
    required=config.embedding_required,
    client=client,
)
try:
    vectors = embedder.embed(["ForWin v1 release gate"])
finally:
    embedder.close()
print(
    json.dumps(
        {
            "backend": config.embedding_backend,
            "required": bool(config.embedding_required),
            "base_url": safe_base_url,
            "peer_ip": peer_ip,
            "model": config.embedding_model,
            "configured_dims": int(config.embedding_dims),
            "metadata_dims": metadata_dims,
            "vector_count": len(vectors),
            "vector_dims": [len(vector) for vector in vectors],
        },
        sort_keys=True,
    )
)
"""


class StackError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(UTC).isoformat()


def exception_text(error: BaseException) -> str:
    try:
        detail = str(error)
    except BaseException:
        detail = ""
    return detail or type(error).__name__


def safe_utc_now(
    *,
    fallback: str,
    errors: list[str],
    field: str,
) -> str:
    try:
        return normalized_utc_timestamp(now(), field=field)
    except BaseException as exc:
        errors.append(f"{field}: {exception_text(exc)}")
    try:
        return normalized_utc_timestamp(fallback, field=f"{field} fallback")
    except BaseException as exc:
        errors.append(f"{field} fallback: {exception_text(exc)}")
        return datetime.now(UTC).isoformat()


def stable_hash(value: Any) -> str:
    try:
        body = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise StackError(f"value is not JSON-compatible: {exc}") from exc
    return hashlib.sha256(body).hexdigest()


def normalized_utc_time(value: object, *, field: str) -> datetime:
    raw = str(value or "")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise StackError(f"{field} is not a valid timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise StackError(f"{field} must be timezone-aware")
    return parsed.astimezone(UTC)


def normalized_utc_timestamp(value: object, *, field: str) -> str:
    return normalized_utc_time(value, field=field).isoformat()


def volume_creation_predates_request(
    created_at: datetime,
    requested_at: datetime,
) -> bool:
    # Docker volume CreatedAt is second-granularity on supported daemons.
    request_floor = (
        requested_at.replace(microsecond=0)
        if created_at.microsecond == 0
        else requested_at
    )
    return created_at < request_floor


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def harness_identity(
    *,
    mode: str = "recovery",
) -> dict[str, dict[str, str]]:
    paths = {
        "controller": Path(__file__).resolve(),
        "compose_file": COMPOSE_FILE.resolve(),
        "compose_override": COMPOSE_OVERRIDE.resolve(),
    }
    if mode == "recovery":
        paths.update(
            {
                "v1_finalizer": Path(__file__).with_name(
                    "finalize_v1.py"
                ).resolve(),
                "recovery_finalizer": Path(__file__).with_name(
                    "finalize_recovery.py"
                ).resolve(),
            }
        )
    elif mode == "v1":
        paths["finalizer"] = Path(__file__).with_name("finalize_v1.py").resolve()
    else:
        raise StackError(f"unknown harness identity mode: {mode}")
    return {
        key: {"path": str(path), "sha256": sha256_file(path)}
        for key, path in paths.items()
    }


def validated_fault_id(fault_id: str) -> str:
    value = str(fault_id or "")
    if _FAULT_ID_PATTERN.fullmatch(value) is None:
        raise StackError(
            "fault identity must be 1-128 ASCII letters, digits, dot, "
            "underscore, or hyphen"
        )
    return value


def validated_hold_id(hold_id: str) -> str:
    value = str(hold_id or "")
    if _HOLD_ID_PATTERN.fullmatch(value) is None:
        raise StackError(
            "hold identity must be 1-128 ASCII letters, digits, dot, "
            "underscore, or hyphen"
        )
    return value


def validated_abort_stage(stage: str) -> str:
    value = str(stage or "")
    if _ABORT_STAGE_PATTERN.fullmatch(value) is None:
        raise StackError(
            "abort stage must be 1-128 ASCII letters, digits, dot, "
            "underscore, or hyphen"
        )
    return value


def sanitized_abort_reason(reason: str) -> str:
    printable = "".join(
        character if character.isprintable() else " "
        for character in str(reason or "")
    )
    value = " ".join(printable.split())
    if not value:
        raise StackError("abort reason must not be empty")
    if len(value) > 512:
        raise StackError("abort reason must not exceed 512 characters")
    return value


def new_recovery_run_identity(
    fault_id: str,
    *,
    run_id: str,
    directory: Path,
) -> dict[str, str]:
    validated_fault_id(fault_id)
    if _RUN_ID_PATTERN.fullmatch(str(run_id or "")) is None:
        raise StackError("run identity must be exactly 32 lowercase hex characters")
    canonical_directory = directory.resolve()
    if not directory.is_absolute() or directory != canonical_directory:
        raise StackError("run evidence directory must be absolute and canonical")
    volume_name = (
        f"{RECOVERY_PROJECT_PREFIX}-{run_id}-{DATABASE_VOLUME_SUFFIX}"
    )
    return {
        "run_id": run_id,
        "evidence_directory": str(canonical_directory),
        "database_volume_name": volume_name,
    }


def recovery_project_name(run_identity: dict[str, Any]) -> str:
    run_id = str(run_identity.get("run_id") or "")
    if _RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise StackError("recovery run identity has an invalid run_id")
    return f"{RECOVERY_PROJECT_PREFIX}-{run_id}"


def validate_run_identity(
    run_identity: object,
) -> dict[str, str]:
    expected_keys = {
        "run_id",
        "evidence_directory",
        "database_volume_name",
    }
    if not isinstance(run_identity, dict) or set(run_identity) != expected_keys:
        raise StackError("recovery run identity has an invalid field set")
    run_id = str(run_identity.get("run_id") or "")
    if _RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise StackError("recovery run identity has an invalid run_id")
    raw_directory = Path(str(run_identity.get("evidence_directory") or ""))
    if (
        not raw_directory.is_absolute()
        or raw_directory != raw_directory.resolve()
        or raw_directory != evidence_directory()
    ):
        raise StackError("recovery run identity evidence directory mismatch")
    expected_volume = (
        f"{RECOVERY_PROJECT_PREFIX}-{run_id}-{DATABASE_VOLUME_SUFFIX}"
    )
    if run_identity.get("database_volume_name") != expected_volume:
        raise StackError("recovery run identity database volume mismatch")
    return {
        "run_id": run_id,
        "evidence_directory": str(raw_directory),
        "database_volume_name": expected_volume,
    }


def required_environment_path(name: str) -> Path:
    value = str(os.environ.get(name) or "").strip()
    if not value:
        raise StackError(f"required environment variable is empty: {name}")
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise StackError(f"required file is missing for {name}: {path}")
    return path


def candidate_manifest() -> tuple[Path, dict[str, Any]]:
    path = required_environment_path(CANDIDATE_MANIFEST_ENV)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise StackError(f"candidate manifest is invalid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise StackError(f"candidate manifest must contain an object: {path}")
    return path, payload


def evidence_directory() -> Path:
    value = str(os.environ.get(EVIDENCE_DIR_ENV) or "").strip()
    if not value:
        raise StackError(f"required environment variable is empty: {EVIDENCE_DIR_ENV}")
    return Path(value).expanduser().resolve()


def events_path() -> Path:
    return evidence_directory() / "stack-events.jsonl"


def event_hash(event: dict[str, Any]) -> str:
    payload = {
        key: value for key, value in event.items() if key != "event_sha256"
    }
    body = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def load_verified_events() -> list[dict[str, Any]]:
    path = events_path()
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    previous = "0" * 64
    for line_number, raw in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        try:
            event = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise StackError(
                f"invalid recovery event JSON at line {line_number}"
            ) from exc
        if not isinstance(event, dict):
            raise StackError(f"recovery event {line_number} is not an object")
        if event.get("previous_event_sha256") != previous:
            raise StackError(f"recovery event chain mismatch at line {line_number}")
        actual_hash = str(event.get("event_sha256") or "")
        if not actual_hash or event_hash(event) != actual_hash:
            raise StackError(f"recovery event hash mismatch at line {line_number}")
        previous = actual_hash
        events.append(event)
    return events


def has_incomplete_fresh_up(events: list[dict[str, Any]]) -> bool:
    return (
        any(event.get("action") == "fresh_up_started" for event in events)
        and not any(
            event.get("action")
            in {
                "fresh_up_completed",
                "setup_blocked",
                "interrupted_cleanup",
                "destroyed",
            }
            for event in events
        )
    )


def has_incomplete_abort(events: list[dict[str, Any]]) -> bool:
    abort_indexes = [
        index
        for index, event in enumerate(events)
        if event.get("action") == "abort_started"
    ]
    if not abort_indexes:
        return False
    return not any(
        event.get("action") == "setup_blocked"
        for event in events[abort_indexes[-1] + 1 :]
    )


def setup_hold_state(
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    used_hold_ids: set[str] = set()
    active_by_id: dict[str, dict[str, str]] = {}
    active_by_service: dict[str, str] = {}
    for event in events:
        action = event.get("action")
        if action not in {
            "setup_service_held",
            "setup_service_released",
            "setup_service_discarded",
        }:
            continue
        hold_id = validated_hold_id(str(event.get("hold_id") or ""))
        service = str(event.get("service") or "")
        if service not in FAULT_SERVICES:
            raise StackError(
                f"setup hold uses a service outside the allowlist: {service}"
            )
        if action == "setup_service_held":
            if hold_id in used_hold_ids:
                raise StackError(
                    f"setup hold identity was already used: {hold_id}"
                )
            if service in active_by_service:
                raise StackError(
                    f"service {service} already has an active setup hold"
                )
            used_hold_ids.add(hold_id)
            after = event.get("after")
            if not isinstance(after, dict):
                raise StackError("setup hold has no service identity")
            active_by_id[hold_id] = {
                "hold_id": hold_id,
                "service": service,
                "fault_kind": str(event.get("fault_kind") or ""),
                "purpose": str(event.get("purpose") or ""),
                "container_id": str(after.get("container_id") or ""),
                "image_id": str(after.get("image_id") or ""),
            }
            active_by_service[service] = hold_id
            continue
        held = active_by_id.get(hold_id)
        if held is None:
            raise StackError(
                f"{action} has no matching setup hold: {hold_id}"
            )
        if held["service"] != service:
            raise StackError(
                "setup hold release service mismatch: "
                f"expected {held['service']}, got {service}"
            )
        del active_by_id[hold_id]
        del active_by_service[service]
    return {
        "used_hold_ids": used_hold_ids,
        "active_by_id": active_by_id,
        "active_by_service": active_by_service,
    }


def require_new_evidence_run() -> None:
    directory = evidence_directory()
    if directory.exists() and any(directory.iterdir()):
        raise StackError(
            f"recovery evidence directory is not empty; use a new directory: {directory}"
        )


def controller_lock_path(directory: Path | None = None) -> Path:
    canonical_directory = (directory or evidence_directory()).resolve()
    lock_name = (
        ".forwin-recovery-controller-"
        f"{stable_hash(str(canonical_directory))[:24]}.lock"
    )
    return canonical_directory.parent / lock_name


def mutating_controller_command(function: Any) -> Any:
    @functools.wraps(function)
    def locked(*args: Any, **kwargs: Any) -> Any:
        directory = evidence_directory()
        path = controller_lock_path(directory)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+", encoding="utf-8") as handle:
            try:
                fcntl.flock(
                    handle.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
            except BlockingIOError as exc:
                raise StackError(
                    "controller transaction already active for evidence directory"
                ) from exc
            try:
                return function(*args, **kwargs)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    return locked


def command(
    *args: str,
    check: bool = True,
    env: dict[str, str] | None = None,
    timeout_seconds: float | None = None,
    deadline: float | None = None,
    timeout_stage: str = "command",
) -> str:
    if args and args[0] in {"docker", "git"}:
        assert_no_control_environment()
        if env is None:
            env = host_command_environment()
    if deadline is not None:
        remaining = cleanup_remaining_timeout(
            deadline,
            stage=timeout_stage,
        )
        timeout_seconds = (
            remaining
            if timeout_seconds is None
            else min(timeout_seconds, remaining)
        )
    try:
        completed = subprocess.run(
            args,
            cwd=ROOT,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise StackError(
            "command timed out after "
            f"{timeout_seconds} seconds ({' '.join(args)})"
        ) from exc
    if check and completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise StackError(f"command failed ({' '.join(args)}): {detail}")
    return completed.stdout.strip()


def assert_no_control_environment() -> None:
    control = sorted(
        key
        for key, value in os.environ.items()
        if value and key in CONTROL_ENV_KEYS
    )
    if control:
        raise StackError(
            "host control environment is not allowed: " + ", ".join(control)
        )


def host_command_environment() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key in HOST_ENV_ALLOWLIST
    }


def compose_resource_identity(
    run_identity: dict[str, Any] | None,
) -> tuple[str, str]:
    if run_identity is None:
        return PROJECT, f"{PROJECT}_forwin-postgres"
    validated = validate_run_identity(run_identity)
    return (
        f"{RECOVERY_PROJECT_PREFIX}-{validated['run_id']}",
        validated["database_volume_name"],
    )


def compose_environment(
    *,
    run_identity: dict[str, Any] | None = None,
) -> dict[str, str]:
    assert_no_control_environment()
    _manifest_path, manifest = candidate_manifest()
    source_sha = str((manifest.get("source") or {}).get("sha") or "")
    images = manifest.get("images") or {}
    runtime_tag = str((images.get("runtime") or {}).get("tag") or "")
    browser_tag = str((images.get("publisher_browser") or {}).get("tag") or "")
    dependency_tags = {
        service: str((images.get(service) or {}).get("tag") or "")
        for service in DEPENDENCY_SERVICES
    }
    if (
        not source_sha
        or not runtime_tag
        or not browser_tag
        or not all(dependency_tags.values())
    ):
        raise StackError("candidate manifest image/source identity is incomplete")
    project_name, database_volume_name = compose_resource_identity(run_identity)
    return {
        **host_command_environment(),
        **COMPOSE_ENV,
        "FORWIN_ENV_FILE": str(required_environment_path(RUNTIME_ENV_FILE_ENV)),
        "FORWIN_RECOVERY_ENV_FILE": str(
            required_environment_path(RUNTIME_ENV_FILE_ENV)
        ),
        "FORWIN_RECOVERY_PROVIDER_ENV_FILE": str(
            required_environment_path(PROVIDER_ENV_FILE_ENV)
        ),
        "FORWIN_RECOVERY_RUNTIME_IMAGE": runtime_tag,
        "FORWIN_RECOVERY_BROWSER_IMAGE": browser_tag,
        "FORWIN_RECOVERY_SOURCE_SHA": source_sha,
        "FORWIN_RECOVERY_POSTGRES_IMAGE": dependency_tags["postgres"],
        "FORWIN_RECOVERY_QDRANT_IMAGE": dependency_tags["qdrant"],
        "FORWIN_RECOVERY_MINIO_IMAGE": dependency_tags["minio"],
        "FORWIN_RECOVERY_PROJECT_NAME": project_name,
        "FORWIN_RECOVERY_DATABASE_VOLUME_NAME": database_volume_name,
    }


def docker_execution_identity() -> dict[str, str]:
    context = command("docker", "context", "show")
    try:
        contexts = json.loads(command("docker", "context", "inspect", context))
    except json.JSONDecodeError as exc:
        raise StackError("Docker context inspection returned invalid JSON") from exc
    if len(contexts) != 1:
        raise StackError("Docker context inspection did not return one context")
    endpoint = str(
        (((contexts[0].get("Endpoints") or {}).get("docker") or {}).get("Host"))
        or ""
    )
    if not endpoint.startswith("unix://"):
        raise StackError(
            "recovery operations require a local Unix socket Docker endpoint"
        )
    try:
        info = json.loads(command("docker", "info", "--format", "{{json .}}"))
    except json.JSONDecodeError as exc:
        raise StackError("Docker daemon inspection returned invalid JSON") from exc
    daemon_id = str(info.get("ID") or "")
    if not daemon_id:
        raise StackError("Docker daemon identity is missing")
    return {
        "context": context,
        "endpoint": endpoint,
        "daemon_id": daemon_id,
        "daemon_name": str(info.get("Name") or ""),
        "server_version": str(info.get("ServerVersion") or ""),
        "operating_system": str(info.get("OperatingSystem") or ""),
        "architecture": str(info.get("Architecture") or ""),
    }


def compose(
    *args: str,
    run_identity: dict[str, Any] | None = None,
    timeout_seconds: float | None = None,
    deadline: float | None = None,
    timeout_stage: str = "Compose command",
) -> str:
    environment = compose_environment(run_identity=run_identity)
    project_name, _database_volume_name = compose_resource_identity(run_identity)
    return command(
        "docker",
        "compose",
        "--env-file",
        environment["FORWIN_RECOVERY_ENV_FILE"],
        "--project-name",
        project_name,
        "--file",
        str(COMPOSE_FILE),
        "--file",
        str(COMPOSE_OVERRIDE),
        "--profile",
        "publisher",
        *args,
        env=environment,
        timeout_seconds=timeout_seconds,
        deadline=deadline,
        timeout_stage=timeout_stage,
    )


def compose_process(
    *args: str,
    run_identity: dict[str, Any] | None = None,
    timeout_seconds: float | None = None,
    deadline: float | None = None,
    timeout_stage: str = "Compose command",
) -> subprocess.CompletedProcess[str]:
    environment = compose_environment(run_identity=run_identity)
    project_name, _database_volume_name = compose_resource_identity(run_identity)
    command_args = [
        "docker",
        "compose",
        "--env-file",
        environment["FORWIN_RECOVERY_ENV_FILE"],
        "--project-name",
        project_name,
        "--file",
        str(COMPOSE_FILE),
        "--file",
        str(COMPOSE_OVERRIDE),
        "--profile",
        "publisher",
        *args,
    ]
    if deadline is not None:
        remaining = cleanup_remaining_timeout(
            deadline,
            stage=timeout_stage,
        )
        timeout_seconds = (
            remaining
            if timeout_seconds is None
            else min(timeout_seconds, remaining)
        )
    try:
        return subprocess.run(
            command_args,
            cwd=ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise StackError(
            "Compose command timed out after "
            f"{timeout_seconds} seconds ({' '.join(args)})"
        ) from exc


def validate_isolated_compose_config(
    payload: dict[str, Any],
    *,
    identity: dict[str, Any],
    run_identity: dict[str, Any] | None = None,
) -> None:
    project_name, database_volume_name = compose_resource_identity(run_identity)
    if payload.get("name") != project_name:
        raise StackError("effective Compose project is not the recovery project")
    services = payload.get("services")
    if not isinstance(services, dict) or set(services) != set(SERVICES):
        raise StackError("effective Compose service set is not exactly isolated")
    expected_environment = {
        "FORWIN_QDRANT_URL": ISOLATED_QDRANT_URL,
        "FORWIN_ARTIFACT_BACKEND": "minio",
        "FORWIN_MINIO_ENDPOINT": ISOLATED_MINIO_ENDPOINT,
        "FORWIN_MINIO_ACCESS_KEY": ISOLATED_MINIO_ACCESS_KEY,
        "FORWIN_MINIO_SECRET_KEY": ISOLATED_MINIO_SECRET_KEY,
        "FORWIN_MINIO_BUCKET": "forwin-recovery-artifacts",
        "FORWIN_MINIO_PREFIX": "artifacts",
        "FORWIN_MINIO_SECURE": "false",
        "FORWIN_API_BASE_URL": ISOLATED_API_BASE_URL,
        "FORWIN_BACKEND_URL": ISOLATED_API_BASE_URL,
    }
    runtime_tag = str((identity.get("runtime_image") or {}).get("tag") or "")
    browser_tag = str((identity.get("browser_image") or {}).get("tag") or "")
    for service in APPLICATION_SERVICES:
        item = services.get(service) or {}
        environment = item.get("environment") or {}
        expected_database_url = SERVICE_DATABASE_URLS.get(
            service,
            ISOLATED_DATABASE_URL,
        )
        if environment.get("FORWIN_DATABASE_URL") != expected_database_url:
            raise StackError(
                f"{service} effective FORWIN_DATABASE_URL is not isolated"
            )
        for key, expected in expected_environment.items():
            if environment.get(key) != expected:
                raise StackError(
                    f"{service} effective {key} is not isolated"
                )
        expected_image = browser_tag if service == "publisher-browser" else runtime_tag
        if not expected_image or item.get("image") != expected_image:
            raise StackError(f"{service} effective image is not the candidate image")
    dependency_images = identity.get("dependency_images") or {}
    for service in DEPENDENCY_SERVICES:
        expected_image = str(
            ((dependency_images.get(service) or {}).get("tag") or "")
        )
        if (
            not expected_image
            or (services.get(service) or {}).get("image") != expected_image
        ):
            raise StackError(
                f"{service} effective image is not the candidate dependency image"
            )
    postgres_environment = (services.get("postgres") or {}).get("environment") or {}
    if postgres_environment != {
        "POSTGRES_DB": "forwin",
        "POSTGRES_PASSWORD": "forwin",
        "POSTGRES_USER": "forwin",
    }:
        raise StackError("effective PostgreSQL credentials are not isolated")
    minio_environment = (services.get("minio") or {}).get("environment") or {}
    if minio_environment != {
        "MINIO_ROOT_PASSWORD": ISOLATED_MINIO_SECRET_KEY,
        "MINIO_ROOT_USER": ISOLATED_MINIO_ACCESS_KEY,
    }:
        raise StackError("effective MinIO credentials are not isolated")
    for service, suffix in CONTAINER_NAME_SUFFIXES.items():
        expected_container_name = f"{project_name}-{suffix}"
        if (
            (services.get(service) or {}).get("container_name")
            != expected_container_name
        ):
            raise StackError(
                f"{service} effective container name is not run-isolated"
            )
    postgres_mounts = (services.get("postgres") or {}).get("volumes") or []
    expected_postgres_mount = {
        "type": "volume",
        "source": "forwin-postgres",
        "target": "/var/lib/postgresql/data",
    }
    if (
        not isinstance(postgres_mounts, list)
        or not any(
            isinstance(mount, dict)
            and {
                key: mount.get(key)
                for key in expected_postgres_mount
            }
            == expected_postgres_mount
            for mount in postgres_mounts
        )
    ):
        raise StackError("effective PostgreSQL volume mount is not isolated")
    volumes = payload.get("volumes") or {}
    postgres_volume = volumes.get("forwin-postgres") or {}
    if postgres_volume.get("name") != database_volume_name:
        raise StackError("effective PostgreSQL volume is not run-isolated")


def assert_isolated_compose(
    identity: dict[str, Any],
    *,
    run_identity: dict[str, Any] | None = None,
) -> None:
    completed = compose_process(
        "config",
        "--format",
        "json",
        run_identity=run_identity,
    )
    if completed.returncode:
        raise StackError(
            "effective recovery Compose config could not be rendered: "
            f"{_completed_output(completed)}"
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise StackError("effective recovery Compose config is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise StackError("effective recovery Compose config is not an object")
    validate_isolated_compose_config(
        payload,
        identity=identity,
        run_identity=run_identity,
    )


def _completed_output(completed: subprocess.CompletedProcess[str]) -> str:
    return "\n".join(
        value.strip()
        for value in (completed.stdout, completed.stderr)
        if value.strip()
    )


def _require_compose_step(
    name: str,
    *args: str,
) -> tuple[dict[str, Any], subprocess.CompletedProcess[str]]:
    completed = compose_process(*args)
    if completed.returncode:
        raise StackError(
            f"V1 step {name} failed: {_completed_output(completed)}"
        )
    output = _completed_output(completed)
    return (
        {
            "name": name,
            "exit_code": completed.returncode,
            "output_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
        },
        completed,
    )


def write_evidence_text(name: str, body: str) -> dict[str, str]:
    path = evidence_directory() / name
    if path.exists():
        raise StackError(f"evidence artifact already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return {"path": str(path), "sha256": sha256_file(path)}


def v1_generation_task_state() -> dict[str, int]:
    _step, completed = _require_compose_step(
        "generation_task_state",
        "run",
        "--rm",
        "--no-deps",
        "forwin",
        "python",
        "-c",
        _V1_TASK_STATE_SCRIPT,
    )
    for line in reversed(completed.stdout.splitlines()):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            state = {
                "total": int(payload.get("total") or 0),
                "leased": int(payload.get("leased") or 0),
            }
            if state["total"] < 0 or state["leased"] < 0:
                break
            return state
    raise StackError("V1 generation task state could not be parsed")


def parse_v1_alembic_revision(output: str) -> str:
    match = re.search(
        r"(?m)^([A-Za-z0-9_]+)(?:\s+\(head\))?\s*$",
        output,
    )
    if match is None:
        raise StackError("V1 migration current revision could not be parsed")
    return match.group(1)


def run_v1_migration_cycle() -> dict[str, Any]:
    commands = (
        (
            "upgrade_head_initial",
            ("run", "--rm", "--no-deps", "forwin", "alembic", "upgrade", "head"),
        ),
        (
            "alembic_check",
            ("run", "--rm", "--no-deps", "forwin", "alembic", "check"),
        ),
        (
            "downgrade_base",
            ("run", "--rm", "--no-deps", "forwin", "alembic", "downgrade", "base"),
        ),
        (
            "upgrade_head_final",
            ("run", "--rm", "--no-deps", "forwin", "alembic", "upgrade", "head"),
        ),
    )
    steps = [
        _require_compose_step(name, *command_args)[0]
        for name, command_args in commands
    ]
    _current_step, current = _require_compose_step(
        "alembic_current",
        "run",
        "--rm",
        "--no-deps",
        "forwin",
        "alembic",
        "current",
    )
    return {
        "steps": steps,
        "final_revision": parse_v1_alembic_revision(current.stdout),
    }


def verify_v1_stale_schema_failfast(final_revision: str) -> dict[str, Any]:
    task_state_before = v1_generation_task_state()
    _require_compose_step(
        "inject_stale_schema",
        "run",
        "--rm",
        "--no-deps",
        "forwin",
        "python",
        "-c",
        _V1_SCHEMA_INJECT_SCRIPT,
    )
    stale = compose_process(
        "run",
        "--rm",
        "--no-deps",
        "generation-worker",
        "python",
        "-m",
        "forwin.cli",
        "-v",
        "generation-worker",
        "--once",
        "--worker-id",
        "v1-stale-schema-probe",
    )
    stale_output = _completed_output(stale)
    expected_pattern = re.compile(
        r"\[FORWIN_SCHEMA_REVISION_MISMATCH\] ForWin database schema is "
        + re.escape(_V1_STALE_REVISION)
        + r", expected "
        + re.escape(final_revision)
        + r"\."
    )
    expected_error = expected_pattern.search(stale_output) is not None
    startup_log = write_evidence_text(
        "stale-schema-generation-worker.log",
        stale_output + ("\n" if stale_output else ""),
    )
    task_state_after = v1_generation_task_state()
    if stale.returncode == 0 or not expected_error:
        raise StackError(
            "generation-worker did not fail closed on the injected stale schema"
        )
    if (
        task_state_before != task_state_after
        or task_state_after != {"total": 0, "leased": 0}
    ):
        raise StackError(
            "generation-worker stale-schema probe changed generation task state"
        )
    _restore_step, restored = _require_compose_step(
        "restore_schema_head",
        "run",
        "--rm",
        "--no-deps",
        "forwin",
        "python",
        "-c",
        _V1_SCHEMA_RESTORE_SCRIPT,
    )
    restored_revision = next(
        (
            line.strip()
            for line in reversed(restored.stdout.splitlines())
            if line.strip()
        ),
        "",
    )
    if restored_revision != final_revision:
        raise StackError(
            f"restored schema revision {restored_revision or '<missing>'} "
            f"does not match {final_revision}"
        )
    _post_step, post_restore = _require_compose_step(
        "post_restore_generation_worker",
        "run",
        "--rm",
        "--no-deps",
        "generation-worker",
        "python",
        "-m",
        "forwin.cli",
        "-v",
        "generation-worker",
        "--once",
        "--worker-id",
        "v1-restored-schema-probe",
    )
    return {
        "role": "generation-worker",
        "injected_revision": _V1_STALE_REVISION,
        "startup_exit_code": stale.returncode,
        "error_code": "FORWIN_SCHEMA_REVISION_MISMATCH",
        "expected_error_observed": expected_error,
        "startup_log": startup_log,
        "task_state_before": task_state_before,
        "task_state_after": task_state_after,
        "restored_revision": restored_revision,
        "post_restore_exit_code": post_restore.returncode,
    }


def validate_v1_embedding_smoke(payload: dict[str, Any]) -> dict[str, Any]:
    configured_dims = int(payload.get("configured_dims") or 0)
    if payload.get("backend") != "gateway":
        raise StackError("V1 embedding backend is not gateway")
    if payload.get("required") is not True:
        raise StackError("V1 embedding gateway is not required")
    parsed_base_url = urlsplit(str(payload.get("base_url") or ""))
    try:
        address = ipaddress.ip_address(parsed_base_url.hostname or "")
    except ValueError as exc:
        raise StackError(
            "V1 embedding gateway is not a private LAN address"
        ) from exc
    if (
        parsed_base_url.scheme not in {"http", "https"}
        or not address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_unspecified
    ):
        raise StackError("V1 embedding gateway is not a private LAN address")
    try:
        peer = ipaddress.ip_address(str(payload.get("peer_ip") or ""))
    except ValueError as exc:
        raise StackError("V1 embedding TCP peer is invalid") from exc
    if peer != address:
        raise StackError("V1 embedding TCP peer differs from the gateway address")
    if (
        configured_dims <= 0
        or int(payload.get("metadata_dims") or 0) != configured_dims
    ):
        raise StackError("V1 embedding metadata dimensions do not match")
    if int(payload.get("vector_count") or 0) != 1:
        raise StackError("V1 embedding smoke did not return one vector")
    if payload.get("vector_dims") != [configured_dims]:
        raise StackError("V1 embedding vector dimensions do not match")
    return payload


def run_v1_embedding_smoke() -> dict[str, Any]:
    _step, completed = _require_compose_step(
        "embedding_gateway_smoke",
        "exec",
        "-T",
        "forwin",
        "python",
        "-c",
        _V1_EMBEDDING_SCRIPT,
    )
    for line in reversed(completed.stdout.splitlines()):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return validate_v1_embedding_smoke(payload)
    raise StackError("V1 embedding smoke did not emit a JSON result")


def image_identity(
    tag: str,
    *,
    source_sha: str,
    expected_image_id: str,
) -> dict[str, str]:
    try:
        payload = json.loads(command("docker", "image", "inspect", tag))
    except json.JSONDecodeError as exc:
        raise StackError(f"docker returned invalid JSON for {tag}") from exc
    if len(payload) != 1:
        raise StackError(f"expected one image for {tag}, got {len(payload)}")
    image = payload[0]
    labels = (image.get("Config") or {}).get("Labels") or {}
    revision = str(labels.get("org.opencontainers.image.revision") or "")
    image_id = str(image.get("Id") or "")
    if revision != source_sha:
        raise StackError(
            f"image {tag} revision {revision or '<missing>'} does not match "
            f"{source_sha}"
        )
    if image_id != expected_image_id:
        raise StackError(
            f"image {tag} id {image_id or '<missing>'} does not match "
            f"{expected_image_id}"
        )
    return {
        "tag": tag,
        "image_id": image_id,
        "revision": revision,
    }


def dependency_image_identity(
    tag: str,
    *,
    expected_image_id: str,
) -> dict[str, str]:
    try:
        payload = json.loads(command("docker", "image", "inspect", tag))
    except json.JSONDecodeError as exc:
        raise StackError(f"docker returned invalid JSON for {tag}") from exc
    if len(payload) != 1:
        raise StackError(f"expected one image for {tag}, got {len(payload)}")
    image_id = str(payload[0].get("Id") or "")
    if not image_id or image_id != expected_image_id:
        raise StackError(
            f"dependency image {tag} id {image_id or '<missing>'} does not "
            f"match {expected_image_id}"
        )
    return {"tag": tag, "image_id": image_id}


def assert_frozen(*, harness_mode: str = "recovery") -> dict[str, Any]:
    manifest_path, manifest = candidate_manifest()
    expected_source_sha = str((manifest.get("source") or {}).get("sha") or "")
    if not expected_source_sha:
        raise StackError("candidate manifest has no source SHA")
    source_sha = command("git", "rev-parse", "HEAD")
    if source_sha != expected_source_sha:
        raise StackError(
            f"source SHA changed: {source_sha}, expected {expected_source_sha}"
        )
    tracked = command(
        "git", "status", "--porcelain=v1", "--untracked-files=no"
    )
    if tracked:
        raise StackError("tracked worktree is dirty")
    required_environment_path(RUNTIME_ENV_FILE_ENV)
    required_environment_path(PROVIDER_ENV_FILE_ENV)
    images = manifest.get("images") or {}

    def verified_image(key: str) -> dict[str, str]:
        expected = images.get(key) or {}
        tag = str(expected.get("tag") or "")
        image_id = str(expected.get("image_id") or "")
        revision = str(expected.get("revision") or "")
        if not tag or not image_id or revision != source_sha:
            raise StackError(f"candidate manifest image identity is incomplete: {key}")
        return image_identity(
            tag,
            source_sha=source_sha,
            expected_image_id=image_id,
        )

    dependency_images = {}
    for key in DEPENDENCY_SERVICES:
        expected = images.get(key) or {}
        tag = str(expected.get("tag") or "")
        image_id = str(expected.get("image_id") or "")
        if not tag or not image_id:
            raise StackError(
                f"candidate dependency image identity is incomplete: {key}"
            )
        dependency_images[key] = dependency_image_identity(
            tag,
            expected_image_id=image_id,
        )
    local_harness = harness_identity(mode=harness_mode)
    release_harness = manifest.get("release_harness") or {}
    release_files = {
        str(item.get("path") or ""): str(item.get("sha256") or "")
        for item in release_harness.get("files") or []
        if isinstance(item, dict)
    }
    for artifact in local_harness.values():
        path = Path(artifact["path"])
        try:
            source_path = path.relative_to(ROOT).as_posix()
        except ValueError as exc:
            raise StackError(
                f"recovery harness is outside candidate source: {path}"
            ) from exc
        if release_files.get(source_path) != artifact["sha256"]:
            raise StackError(
                f"candidate release harness identity mismatch: {source_path}"
            )

    return {
        "source_sha": source_sha,
        "source_tree": command("git", "rev-parse", "HEAD^{tree}"),
        "docker": docker_execution_identity(),
        "runtime_image": verified_image("runtime"),
        "browser_image": verified_image("publisher_browser"),
        "dependency_images": dependency_images,
        "candidate_manifest": {
            "path": str(manifest_path),
            "sha256": sha256_file(manifest_path),
        },
        "harness": local_harness,
        "isolated_test_settings": {
            key: COMPOSE_ENV[key]
            for key in sorted(COMPOSE_ENV)
            if key.endswith(
                (
                    "_LEASE_SECONDS",
                    "_POLL_INTERVAL",
                    "_HEARTBEAT_INTERVAL_SECONDS",
                    "_BASE_DELAY_SECONDS",
                    "_MAX_DELAY_SECONDS",
                )
            )
        },
    }


def compose_container_id(
    service: str,
    *,
    run_identity: dict[str, Any] | None = None,
    deadline: float | None = None,
) -> str:
    return compose(
        "ps",
        "--all",
        "--quiet",
        service,
        run_identity=run_identity,
        deadline=deadline,
        timeout_stage=f"service {service} Compose inspection",
    )


def inspect_service(
    service: str,
    *,
    run_identity: dict[str, Any] | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    container_id = compose_container_id(
        service,
        run_identity=run_identity,
        deadline=deadline,
    )
    if not container_id:
        return {"service": service, "exists": False}
    try:
        payload = json.loads(
            command(
                "docker",
                "container",
                "inspect",
                container_id,
                deadline=deadline,
                timeout_stage=f"service {service} container inspection",
            )
        )
    except json.JSONDecodeError as exc:
        raise StackError(f"docker returned invalid JSON for service {service}") from exc
    if len(payload) != 1:
        raise StackError(f"expected one container for service {service}")
    item = payload[0]
    labels = (item.get("Config") or {}).get("Labels") or {}
    actual_project = str(labels.get("com.docker.compose.project") or "")
    actual_service = str(labels.get("com.docker.compose.service") or "")
    expected_project, _database_volume_name = compose_resource_identity(
        run_identity
    )
    if actual_project != expected_project or actual_service != service:
        raise StackError(
            f"refusing container {container_id}: labels identify "
            f"{actual_project}/{actual_service}, expected "
            f"{expected_project}/{service}"
        )
    state = item.get("State") or {}
    health = state.get("Health") or {}
    host_config = item.get("HostConfig") or {}
    restart_policy = host_config.get("RestartPolicy") or {}
    return {
        "service": service,
        "exists": True,
        "container_id": str(item.get("Id") or ""),
        "name": str(item.get("Name") or "").removeprefix("/"),
        "image_id": str(item.get("Image") or ""),
        "status": str(state.get("Status") or ""),
        "running": bool(state.get("Running")),
        "exit_code": int(state.get("ExitCode") or 0),
        "started_at": str(state.get("StartedAt") or ""),
        "finished_at": str(state.get("FinishedAt") or ""),
        "health": str(health.get("Status") or ""),
        "restart_count": int(item.get("RestartCount") or 0),
        "restart_policy": str(restart_policy.get("Name") or ""),
    }


def published_endpoint_identity(
    service: str,
    container_port: int,
    *,
    run_identity: dict[str, Any],
) -> dict[str, Any]:
    allowed_ports = {
        "forwin": {8899},
        "forwin-mcp": {8896},
        "postgres": {5432},
        "qdrant": {6333},
        "minio": {9000, 9001},
    }
    if service not in allowed_ports:
        raise StackError("endpoint service is not allowed")
    if (
        type(container_port) is not int
        or container_port not in allowed_ports[service]
    ):
        raise StackError("endpoint container port is invalid")
    container_id = compose_container_id(service, run_identity=run_identity)
    if not container_id:
        raise StackError(f"endpoint service does not exist: {service}")
    try:
        payload = json.loads(
            command("docker", "container", "inspect", container_id)
        )
    except json.JSONDecodeError as exc:
        raise StackError(
            f"docker returned invalid endpoint identity for {service}"
        ) from exc
    if len(payload) != 1:
        raise StackError(f"expected one endpoint container for {service}")
    item = payload[0]
    labels = (item.get("Config") or {}).get("Labels") or {}
    expected_project = recovery_project_name(run_identity)
    if (
        labels.get("com.docker.compose.project") != expected_project
        or labels.get("com.docker.compose.service") != service
    ):
        raise StackError(f"endpoint container identity drifted for {service}")
    state = item.get("State") or {}
    if state.get("Running") is not True:
        raise StackError(f"endpoint service is not running: {service}")
    ports = (item.get("NetworkSettings") or {}).get("Ports") or {}
    published = {
        key: value
        for key, value in ports.items()
        if isinstance(value, list) and value
    }
    expected_key = f"{container_port}/tcp"
    expected_published = {
        f"{port}/tcp" for port in allowed_ports[service]
    }
    if (
        set(published) != expected_published
        or any(len(value) != 1 for value in published.values())
    ):
        raise StackError(
            f"endpoint published mapping is not exact for {service}"
        )
    for values in published.values():
        published_host = str(values[0].get("HostIp") or "")
        try:
            published_address = ipaddress.ip_address(published_host)
        except ValueError as exc:
            raise StackError(
                f"endpoint published host is invalid for {service}"
            ) from exc
        if not published_address.is_loopback:
            raise StackError(
                f"endpoint published host is not loopback for {service}"
            )
    mapping = published[expected_key][0]
    host = str(mapping.get("HostIp") or "")
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise StackError(
            f"endpoint published host is invalid for {service}"
        ) from exc
    if not address.is_loopback:
        raise StackError(
            f"endpoint published host is not loopback for {service}"
        )
    try:
        host_port = int(mapping.get("HostPort") or 0)
    except (TypeError, ValueError) as exc:
        raise StackError(
            f"endpoint published port is invalid for {service}"
        ) from exc
    if not 1 <= host_port <= 65535:
        raise StackError(
            f"endpoint published port is invalid for {service}"
        )
    inspected_container = str(item.get("Id") or "")
    image_id = str(item.get("Image") or "")
    if inspected_container != container_id or not image_id.startswith("sha256:"):
        raise StackError(
            f"endpoint container identity is incomplete for {service}"
        )
    return {
        "service": service,
        "host": address.compressed,
        "host_port": host_port,
        "container_port": container_port,
        "container_id": inspected_container,
        "image_id": image_id,
    }


def _sentinel_script(*, create: bool) -> str:
    operation = """
existing = cursor.execute(
    "SELECT to_regclass('public.forwin_recovery_run_sentinel') AS name"
).fetchone()["name"]
if existing is not None:
    raise SystemExit("recovery sentinel table already exists")
cursor.execute(
    "CREATE TABLE forwin_recovery_run_sentinel ("
    "singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),"
    "sentinel_id text NOT NULL UNIQUE,"
    "run_id text NOT NULL UNIQUE,"
    "fault_id text NOT NULL,"
    "source_sha text NOT NULL,"
    "created_at timestamptz NOT NULL DEFAULT now())"
)
cursor.execute(
    "INSERT INTO forwin_recovery_run_sentinel "
    "(sentinel_id, run_id, fault_id, source_sha) VALUES (%s, %s, %s, %s)",
    tuple(sys.argv[1:5]),
)
""" if create else """
row = cursor.execute(
    "SELECT sentinel_id, run_id, fault_id, source_sha "
    "FROM forwin_recovery_run_sentinel WHERE singleton = true"
).fetchone()
if row is None:
    raise SystemExit("recovery sentinel row is missing")
print(json.dumps({"table": "forwin_recovery_run_sentinel", **dict(row)}, sort_keys=True))
"""
    return f"""
import json
import sys
import psycopg
from psycopg.rows import dict_row
with psycopg.connect(
    "postgresql://forwin:forwin@postgres:5432/forwin",
    row_factory=dict_row,
) as connection:
    with connection.cursor() as cursor:
{chr(10).join('        ' + line for line in operation.strip().splitlines())}
""".strip()


def initialize_recovery_sentinel(
    *,
    fault_id: str,
    run_identity: dict[str, Any],
    identity: dict[str, Any],
) -> dict[str, str]:
    sentinel = {
        "table": RECOVERY_SENTINEL_TABLE,
        "sentinel_id": secrets.token_hex(32),
        "run_id": str(run_identity["run_id"]),
        "fault_id": validated_fault_id(fault_id),
        "source_sha": str(identity.get("source_sha") or ""),
    }
    if not re.fullmatch(r"[0-9a-f]{40}", sentinel["source_sha"]):
        raise StackError("recovery sentinel source identity is invalid")
    compose(
        "run",
        "--rm",
        "--no-deps",
        "forwin",
        "python",
        "-c",
        _sentinel_script(create=True),
        sentinel["sentinel_id"],
        sentinel["run_id"],
        sentinel["fault_id"],
        sentinel["source_sha"],
        run_identity=run_identity,
    )
    return sentinel


def read_recovery_sentinel(
    *,
    run_identity: dict[str, Any],
) -> dict[str, str]:
    output = compose(
        "exec",
        "-T",
        "forwin",
        "python",
        "-c",
        _sentinel_script(create=False),
        run_identity=run_identity,
    )
    try:
        value = json.loads(output)
    except json.JSONDecodeError as exc:
        raise StackError("recovery sentinel query returned invalid JSON") from exc
    expected_keys = {
        "table",
        "sentinel_id",
        "run_id",
        "fault_id",
        "source_sha",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise StackError("recovery sentinel query returned an invalid record")
    return {key: str(value[key]) for key in expected_keys}


def _loopback_http_endpoint(
    value: str,
    *,
    label: str,
    path: str,
) -> tuple[str, str, int]:
    parsed = urlsplit(str(value or ""))
    try:
        address = ipaddress.ip_address(parsed.hostname or "")
        port = int(parsed.port or 0)
    except (ValueError, TypeError) as exc:
        raise StackError(f"{label} endpoint is invalid") from exc
    if (
        parsed.scheme != "http"
        or not address.is_loopback
        or not 1 <= port <= 65535
        or parsed.path != path
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise StackError(
            f"{label} endpoint must be exact, credential-free, and loopback"
        )
    return parsed.scheme, address.compressed, port


def probe_loopback_health(url: str) -> int:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, method="GET")
    try:
        with opener.open(request, timeout=10) as response:
            status = int(response.status)
    except (OSError, urllib.error.HTTPError) as exc:
        raise StackError(f"endpoint health probe failed: {url}") from exc
    if not 200 <= status < 300:
        raise StackError(f"endpoint health probe was not successful: {url}")
    return status


@mutating_controller_command
def bind_recovery_endpoints(
    fault_id: str,
    api_url: str,
    mcp_url: str,
    database_host: str,
    database_port: int,
    database_name: str,
    *,
    qdrant_url: str | None = None,
    minio_url: str | None = None,
) -> dict[str, Any]:
    context = require_active_recovery_run(fault_id)
    if any(
        event.get("action") == "endpoints_bound"
        for event in context["events"]
    ):
        raise StackError("recovery endpoints were already bound")
    run_identity = context["run_identity"]
    identity = assert_frozen()
    if identity != context["identity"]:
        raise StackError("recovery harness identity changed before endpoint binding")
    assert_isolated_compose(identity, run_identity=run_identity)
    api_scheme, api_host, api_port = _loopback_http_endpoint(
        api_url,
        label="API",
        path="",
    )
    mcp_scheme, mcp_host, mcp_port = _loopback_http_endpoint(
        mcp_url,
        label="MCP",
        path="/mcp",
    )
    optional_specs = {
        "qdrant": {
            "url": qdrant_url,
            "service": "qdrant",
            "container_port": 6333,
            "health_path": "/readyz",
        },
        "minio": {
            "url": minio_url,
            "service": "minio",
            "container_port": 9000,
            "health_path": "/minio/health/ready",
        },
    }
    optional_requested: dict[str, tuple[str, str, int]] = {}
    for name, spec in optional_specs.items():
        if spec["url"] is not None:
            optional_requested[name] = _loopback_http_endpoint(
                str(spec["url"]),
                label=name.capitalize(),
                path="",
            )
    try:
        database_address = ipaddress.ip_address(database_host)
    except ValueError as exc:
        raise StackError("database endpoint host is invalid") from exc
    if (
        not database_address.is_loopback
        or not 1 <= int(database_port) <= 65535
        or database_name != "forwin"
    ):
        raise StackError("database endpoint is not the exact isolated database")
    mappings = {
        "api": published_endpoint_identity(
            "forwin", 8899, run_identity=run_identity
        ),
        "mcp": published_endpoint_identity(
            "forwin-mcp", 8896, run_identity=run_identity
        ),
        "database": published_endpoint_identity(
            "postgres", 5432, run_identity=run_identity
        ),
    }
    for name in optional_requested:
        spec = optional_specs[name]
        mappings[name] = published_endpoint_identity(
            str(spec["service"]),
            int(spec["container_port"]),
            run_identity=run_identity,
        )
    requested = {
        "api": (api_host, api_port),
        "mcp": (mcp_host, mcp_port),
        "database": (database_address.compressed, int(database_port)),
    }
    requested.update(
        {
            name: (host, port)
            for name, (_scheme, host, port) in optional_requested.items()
        }
    )
    for name, (host, port) in requested.items():
        mapping = mappings[name]
        if (mapping["host"], mapping["host_port"]) != (host, port):
            raise StackError(f"{name} endpoint does not map to the active run")
    fresh_completions = [
        event
        for event in context["events"]
        if event.get("action") == "fresh_up_completed"
    ]
    sentinel = read_recovery_sentinel(run_identity=run_identity)
    if (
        len(fresh_completions) != 1
        or fresh_completions[0].get("sentinel") != sentinel
    ):
        raise StackError("active-run database sentinel identity drifted")
    candidate_identity = {
        key: identity.get(key)
        for key in (
            "source_sha",
            "source_tree",
            "runtime_image",
            "browser_image",
            "dependency_images",
            "candidate_manifest",
        )
    }
    record: dict[str, Any] = {
        "schema_version": 1,
        "fault_id": fault_id,
        "run_id": str(run_identity["run_id"]),
        "source_sha": str(identity.get("source_sha") or ""),
        "source_tree": str(identity.get("source_tree") or ""),
        "project_name": recovery_project_name(run_identity),
        "candidate_manifest_sha256": str(
            (identity.get("candidate_manifest") or {}).get("sha256") or ""
        ),
        "candidate_identity_sha256": stable_hash(candidate_identity),
        "sentinel": sentinel,
        "api": {
            "scheme": api_scheme,
            "host": api_host,
            "port": api_port,
            "endpoint_path": "",
            "health_path": "/health",
            "health_status": probe_loopback_health(
                f"{api_scheme}://{api_host}:{api_port}/health"
            ),
            **{
                key: value
                for key, value in mappings["api"].items()
                if key not in {"host", "host_port"}
            },
        },
        "mcp": {
            "scheme": mcp_scheme,
            "host": mcp_host,
            "port": mcp_port,
            "endpoint_path": "/mcp",
            "health_path": "/health",
            "health_status": probe_loopback_health(
                f"{mcp_scheme}://{mcp_host}:{mcp_port}/health"
            ),
            **{
                key: value
                for key, value in mappings["mcp"].items()
                if key not in {"host", "host_port"}
            },
        },
        "database": {
            "scheme": "postgresql",
            "host": database_address.compressed,
            "port": int(database_port),
            "database": database_name,
            **{
                key: value
                for key, value in mappings["database"].items()
                if key not in {"host", "host_port"}
            },
        },
    }
    for name, (scheme, host, port) in optional_requested.items():
        spec = optional_specs[name]
        health_path = str(spec["health_path"])
        record[name] = {
            "scheme": scheme,
            "host": host,
            "port": port,
            "endpoint_path": "",
            "health_path": health_path,
            "health_status": probe_loopback_health(
                f"{scheme}://{host}:{port}{health_path}"
            ),
            **{
                key: value
                for key, value in mappings[name].items()
                if key not in {"host", "host_port"}
            },
        }
    record["identity_sha256"] = stable_hash(record)
    database_volume = confirmed_database_volume(
        run_identity,
        context["database_volume"],
    )
    append_event(
        "endpoints_bound",
        fault_id=fault_id,
        identity=identity,
        run_identity=run_identity,
        database_volume=database_volume,
        endpoint_identity=record,
    )
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return record


def functional_probe(
    service: str,
    *,
    run_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    worker_markers = {
        "generation-worker": "generation-worker",
        "outbox-worker": "outbox-worker",
        "publisher-worker": "publisher-worker",
    }
    if service in worker_markers:
        marker = worker_markers[service]
        command_args = (
            "exec",
            "-T",
            service,
            "python",
            "-c",
            (
                "from pathlib import Path; "
                "cmd=Path('/proc/1/cmdline').read_bytes().replace(b'\\0',b' '); "
                f"raise SystemExit(0 if b'{marker}' in cmd else 1)"
            ),
        )
    elif service == "postgres":
        command_args = (
            "exec",
            "-T",
            "postgres",
            "pg_isready",
            "-U",
            "forwin",
            "-d",
            "forwin",
        )
    elif service == "qdrant":
        command_args = (
            "exec",
            "-T",
            "qdrant",
            "bash",
            "-ec",
            "</dev/tcp/127.0.0.1/6333",
        )
    elif service == "minio":
        command_args = (
            "exec",
            "-T",
            "forwin",
            "python",
            "-c",
            (
                "import httpx; "
                "c=httpx.Client(trust_env=False,timeout=10); "
                "r=c.get('http://minio:9000/minio/health/ready'); "
                "c.close(); raise SystemExit(0 if r.status_code==200 else 1)"
            ),
        )
    elif service == "forwin":
        command_args = (
            "exec",
            "-T",
            "forwin",
            "python",
            "-c",
            (
                "import httpx; "
                "c=httpx.Client(trust_env=False,timeout=10); "
                "r=c.get('http://127.0.0.1:8899/health'); "
                "c.close(); raise SystemExit(0 if r.status_code==200 else 1)"
            ),
        )
    elif service == "forwin-mcp":
        command_args = (
            "exec",
            "-T",
            "forwin-mcp",
            "python",
            "-c",
            (
                "import httpx; "
                "c=httpx.Client(trust_env=False,timeout=10); "
                "r=c.get('http://127.0.0.1:8896/health'); "
                "c.close(); raise SystemExit(0 if r.status_code==200 else 1)"
            ),
        )
    elif service == "publisher-browser":
        command_args = (
            "exec",
            "-T",
            "publisher-browser",
            "python",
            "scripts/check_publisher_browser_heartbeat.py",
            "--wait-seconds",
            "0",
        )
    else:
        raise StackError(f"no functional probe is defined for service {service}")
    completed = compose_process(*command_args, run_identity=run_identity)
    output = _completed_output(completed)
    if completed.returncode:
        raise StackError(f"functional probe failed for {service}: {output}")
    return {
        "passed": True,
        "exit_code": 0,
        "output_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
    }


def stack_snapshot(
    *,
    probe: bool = False,
    run_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    services = {
        service: inspect_service(service, run_identity=run_identity)
        for service in SERVICES
    }
    if probe:
        for service in SERVICES:
            services[service]["probe"] = functional_probe(
                service,
                run_identity=run_identity,
            )
    return {
        "observed_at": now(),
        "services": services,
    }


def append_event(
    action: str,
    *,
    _recorded_at: str | None = None,
    **payload: Any,
) -> dict[str, Any]:
    path = events_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = load_verified_events()
    if existing and existing[-1].get("action") in TERMINAL_ACTIONS:
        raise StackError(
            "recovery event log is terminal after "
            f"{existing[-1].get('action')}"
        )
    if (
        has_incomplete_fresh_up(existing)
        and action
        not in {
            "fresh_up_completed",
            "setup_blocked",
            "interrupted_cleanup",
        }
    ):
        raise StackError("recovery event log has an incomplete fresh-up")
    if has_incomplete_abort(existing) and action != "setup_blocked":
        raise StackError("recovery event log has an incomplete abort")
    event = {
        "schema_version": 2,
        "recorded_at": _recorded_at or now(),
        "action": action,
        "previous_event_sha256": (
            str(existing[-1]["event_sha256"]) if existing else "0" * 64
        ),
        **payload,
    }
    event["event_sha256"] = event_hash(event)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return event


def database_volume_observation(
    run_identity: dict[str, Any],
    *,
    deadline: float | None = None,
) -> dict[str, Any]:
    validated = validate_run_identity(run_identity)
    volume_name = validated["database_volume_name"]
    volume_names = set(
        command(
            "docker",
            "volume",
            "ls",
            "--format",
            "{{.Name}}",
            deadline=deadline,
            timeout_stage="database volume list",
        ).splitlines()
    )
    if volume_name not in volume_names:
        return {"name": volume_name, "exists": False}
    try:
        payload = json.loads(
            command(
                "docker",
                "volume",
                "inspect",
                volume_name,
                deadline=deadline,
                timeout_stage="database volume inspection",
            )
        )
    except json.JSONDecodeError as exc:
        raise StackError(
            f"docker returned invalid JSON for volume {volume_name}"
        ) from exc
    if (
        not isinstance(payload, list)
        or len(payload) != 1
        or not isinstance(payload[0], dict)
    ):
        raise StackError(f"expected one Docker volume for {volume_name}")
    item = payload[0]
    labels = item.get("Labels") or {}
    expected_project = recovery_project_name(validated)
    if (
        item.get("Name") != volume_name
        or labels.get("com.docker.compose.project") != expected_project
        or labels.get("com.docker.compose.volume") != "forwin-postgres"
    ):
        raise StackError(
            f"refusing database volume with mismatched identity: {volume_name}"
        )
    created_at = normalized_utc_timestamp(
        item.get("CreatedAt"),
        field=f"database volume creation time for {volume_name}",
    )
    return {
        "name": volume_name,
        "exists": True,
        "created_at": created_at,
        "fingerprint": stable_hash(
            {"created_at": created_at, "name": volume_name}
        ),
    }


def confirmed_database_volume(
    run_identity: dict[str, Any],
    expected: dict[str, Any],
) -> dict[str, Any]:
    actual = database_volume_observation(run_identity)
    if actual != expected or actual.get("exists") is not True:
        raise StackError("database volume identity changed during recovery run")
    return actual


def confirmed_fresh_database_volume(
    run_identity: dict[str, Any],
    *,
    requested_at: str,
) -> dict[str, Any]:
    observation = database_volume_observation(run_identity)
    if observation.get("exists") is not True:
        raise StackError("fresh-up did not create its database volume")
    created_at = normalized_utc_time(
        observation.get("created_at"),
        field="database volume creation time",
    )
    request_time = normalized_utc_time(
        requested_at,
        field="fresh-up requested_at",
    )
    if volume_creation_predates_request(created_at, request_time):
        raise StackError("database volume creation time predates fresh-up request")
    return observation


def require_active_recovery_run(
    fault_id: str,
) -> dict[str, Any]:
    validated_fault = validated_fault_id(fault_id)
    events = load_verified_events()
    if not events:
        raise StackError("recovery run has no completed fresh-up")
    if has_incomplete_fresh_up(events):
        raise StackError("recovery event log has an incomplete fresh-up")
    if has_incomplete_abort(events):
        raise StackError("recovery event log has an incomplete abort")
    if events[-1].get("action") in TERMINAL_ACTIONS:
        raise StackError(
            "recovery event log is terminal after "
            f"{events[-1].get('action')}"
        )
    if any(event.get("schema_version") != 2 for event in events):
        raise StackError("recovery event schema version mismatch")
    event_fault_ids = {str(event.get("fault_id") or "") for event in events}
    if event_fault_ids != {validated_fault}:
        raise StackError("recovery fault identity mismatch")
    first_run_identity = validate_run_identity(events[0].get("run_identity"))
    if any(
        event.get("run_identity") != first_run_identity for event in events
    ):
        raise StackError("recovery run identity changed within event log")
    starts = [
        event for event in events if event.get("action") == "fresh_up_started"
    ]
    completions = [
        event for event in events if event.get("action") == "fresh_up_completed"
    ]
    if (
        len(starts) != 1
        or len(completions) != 1
        or events[:2] != [starts[0], completions[0]]
    ):
        raise StackError("recovery run requires one completed fresh-up")
    volume_name = first_run_identity["database_volume_name"]
    if starts[0].get("database_volume") != {
        "name": volume_name,
        "exists": False,
    }:
        raise StackError("fresh-up did not begin with an absent database volume")
    database_volume = completions[0].get("database_volume")
    created_at = (
        database_volume.get("created_at")
        if isinstance(database_volume, dict)
        else None
    )
    created_time = normalized_utc_time(
        created_at,
        field="fresh-up database volume creation time",
    )
    requested_at = starts[0].get("requested_at")
    request_time = normalized_utc_time(
        requested_at,
        field="fresh-up requested_at",
    )
    if completions[0].get("requested_at") != requested_at:
        raise StackError("fresh-up requested_at changed within event log")
    if volume_creation_predates_request(created_time, request_time):
        raise StackError("database volume creation time predates fresh-up request")
    if (
        not isinstance(database_volume, dict)
        or set(database_volume)
        != {"name", "exists", "created_at", "fingerprint"}
        or database_volume.get("name") != volume_name
        or database_volume.get("exists") is not True
        or database_volume.get("fingerprint")
        != stable_hash(
            {
                "created_at": database_volume.get("created_at"),
                "name": volume_name,
            }
        )
    ):
        raise StackError("fresh-up database volume identity is invalid")
    if any(
        event.get("database_volume") != database_volume
        for event in events[2:]
    ):
        raise StackError("database volume identity changed within event log")
    event_identity = completions[0].get("identity")
    if any(event.get("identity") != event_identity for event in events):
        raise StackError("recovery harness identity changed within event log")
    return {
        "fault_id": validated_fault,
        "run_identity": first_run_identity,
        "database_volume": database_volume,
        "identity": completions[0].get("identity"),
        "events": events,
    }


def require_interrupt_cleanup_run(
    fault_id: str,
) -> dict[str, Any]:
    validated_fault = validated_fault_id(fault_id)
    events = load_verified_events()
    if not events:
        raise StackError("interrupt cleanup has no recovery run")
    if events[-1].get("action") in TERMINAL_ACTIONS:
        raise StackError(
            "recovery event log is terminal after "
            f"{events[-1].get('action')}"
        )
    if not has_incomplete_fresh_up(events):
        return {
            **require_active_recovery_run(validated_fault),
            "fresh_up_completed": True,
        }
    if (
        len(events) != 1
        or events[0].get("action") != "fresh_up_started"
        or events[0].get("schema_version") != 2
        or str(events[0].get("fault_id") or "") != validated_fault
    ):
        raise StackError(
            "interrupt cleanup requires one valid fresh-up prefix"
        )
    start = events[0]
    run_identity = validate_run_identity(start.get("run_identity"))
    volume_name = run_identity["database_volume_name"]
    if start.get("database_volume") != {
        "name": volume_name,
        "exists": False,
    }:
        raise StackError(
            "interrupt cleanup fresh-up did not begin with an absent volume"
        )
    normalized_utc_time(
        start.get("requested_at"),
        field="interrupt cleanup fresh-up requested_at",
    )
    return {
        "fault_id": validated_fault,
        "run_identity": run_identity,
        "database_volume": None,
        "identity": start.get("identity"),
        "events": events,
        "fresh_up_completed": False,
    }


def interrupt_cleanup_volume_observation(
    context: dict[str, Any],
) -> dict[str, Any]:
    run_identity = context["run_identity"]
    actual = database_volume_observation(run_identity)
    absent = {
        "name": run_identity["database_volume_name"],
        "exists": False,
    }
    if actual == absent:
        return actual
    expected = context.get("database_volume")
    if context.get("fresh_up_completed") is True:
        if actual != expected:
            raise StackError(
                "interrupt cleanup database volume identity drifted"
            )
        return actual
    created_at = actual.get("created_at")
    if (
        set(actual)
        != {"name", "exists", "created_at", "fingerprint"}
        or actual.get("name") != run_identity["database_volume_name"]
        or actual.get("exists") is not True
        or actual.get("fingerprint")
        != stable_hash(
            {
                "created_at": created_at,
                "name": run_identity["database_volume_name"],
            }
        )
    ):
        raise StackError(
            "interrupt cleanup startup volume identity drifted"
        )
    created_time = normalized_utc_time(
        created_at,
        field="interrupt cleanup startup volume creation time",
    )
    requested_time = normalized_utc_time(
        context["events"][0].get("requested_at"),
        field="interrupt cleanup fresh-up requested_at",
    )
    if created_time < requested_time:
        raise StackError(
            "interrupt cleanup startup volume predates fresh-up"
        )
    return actual


def interrupt_cleanup_container_names(
    run_identity: dict[str, Any],
) -> dict[str, str]:
    if set(CONTAINER_NAME_SUFFIXES) != set(SERVICES):
        raise StackError(
            "interrupt cleanup container name contract is incomplete"
        )
    project_name = recovery_project_name(run_identity)
    return {
        service: f"{project_name}-{CONTAINER_NAME_SUFFIXES[service]}"
        for service in SERVICES
    }


def interrupt_cleanup_container_ids(
    selector: str,
    *,
    deadline: float,
) -> set[str]:
    output = command(
        "docker",
        "container",
        "ls",
        "--all",
        "--no-trunc",
        "--filter",
        selector,
        "--format",
        "json",
        deadline=deadline,
        timeout_stage="interrupt cleanup container discovery",
    )
    container_ids: set[str] = set()
    for line_number, raw in enumerate(output.splitlines(), start=1):
        try:
            item = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise StackError(
                "interrupt cleanup container discovery returned invalid "
                f"JSON at line {line_number}"
            ) from exc
        container_id = item.get("ID") if isinstance(item, dict) else None
        if not isinstance(container_id, str) or not container_id:
            raise StackError(
                "interrupt cleanup container discovery returned an "
                "invalid container identity"
            )
        container_ids.add(container_id)
    return container_ids


def interrupt_cleanup_expected_container_identities(
    context: dict[str, Any],
    *,
    expected_names: dict[str, str],
) -> dict[str, dict[str, str]]:
    if context.get("fresh_up_completed") is not True:
        return {}
    completions = [
        event
        for event in context["events"]
        if event.get("action") == "fresh_up_completed"
    ]
    if len(completions) != 1:
        raise StackError(
            "interrupt cleanup completed service identity is unavailable"
        )
    after = completions[0].get("after")
    services = after.get("services") if isinstance(after, dict) else None
    if not isinstance(services, dict) or set(services) != set(SERVICES):
        raise StackError(
            "interrupt cleanup completed service inventory is invalid"
        )
    expected: dict[str, dict[str, str]] = {}
    for service in SERVICES:
        state = services.get(service)
        if not isinstance(state, dict):
            raise StackError(
                f"interrupt cleanup completed identity is invalid for {service}"
            )
        container_id = state.get("container_id")
        image_id = state.get("image_id")
        if (
            state.get("service") != service
            or state.get("exists") is not True
            or state.get("name") != expected_names[service]
            or not isinstance(container_id, str)
            or not container_id
            or not isinstance(image_id, str)
            or not image_id
        ):
            raise StackError(
                f"interrupt cleanup completed identity is invalid for {service}"
            )
        expected[service] = {
            "container_id": container_id,
            "image_id": image_id,
        }
    return expected


def require_interrupt_cleanup_namespace(
    context: dict[str, Any],
) -> dict[str, dict[str, str]]:
    run_identity = context["run_identity"]
    project_name = recovery_project_name(run_identity)
    expected_names = interrupt_cleanup_container_names(run_identity)
    expected_identities = interrupt_cleanup_expected_container_identities(
        context,
        expected_names=expected_names,
    )
    deadline = time.monotonic() + CLEANUP_TIMEOUT_SECONDS
    selectors = [
        f"label=com.docker.compose.project={project_name}",
        *[
            f"name=^{expected_names[service]}$"
            for service in SERVICES
        ],
    ]
    container_ids: set[str] = set()
    for selector in selectors:
        container_ids.update(
            interrupt_cleanup_container_ids(
                selector,
                deadline=deadline,
            )
        )
    if not container_ids:
        return {}
    try:
        payload = json.loads(
            command(
                "docker",
                "container",
                "inspect",
                *sorted(container_ids),
                deadline=deadline,
                timeout_stage="interrupt cleanup container inspection",
            )
        )
    except json.JSONDecodeError as exc:
        raise StackError(
            "interrupt cleanup container inspection returned invalid JSON"
        ) from exc
    if (
        not isinstance(payload, list)
        or len(payload) != len(container_ids)
        or not all(isinstance(item, dict) for item in payload)
    ):
        raise StackError(
            "interrupt cleanup container inspection returned an invalid "
            "inventory"
        )

    observed: dict[str, dict[str, str]] = {}
    inspected_ids: set[str] = set()
    for item in payload:
        container_id = item.get("Id")
        image_id = item.get("Image")
        if (
            not isinstance(container_id, str)
            or not container_id
            or container_id not in container_ids
            or container_id in inspected_ids
            or not isinstance(image_id, str)
            or not image_id
        ):
            raise StackError(
                "interrupt cleanup container inspection returned an "
                "invalid identity"
            )
        inspected_ids.add(container_id)
        config = item.get("Config")
        labels = config.get("Labels") if isinstance(config, dict) else None
        if not isinstance(labels, dict):
            raise StackError(
                "interrupt cleanup container service label is missing"
            )
        actual_project = labels.get("com.docker.compose.project")
        if actual_project != project_name:
            raise StackError(
                "interrupt cleanup container project label mismatch"
            )
        service = labels.get("com.docker.compose.service")
        if not isinstance(service, str) or not service:
            raise StackError(
                "interrupt cleanup container service label is missing"
            )
        if service not in SERVICES:
            raise StackError(
                f"interrupt cleanup container has unknown service: {service}"
            )
        if service in observed:
            raise StackError(
                f"interrupt cleanup has duplicate containers for {service}"
            )
        expected_name = expected_names[service]
        if item.get("Name") != f"/{expected_name}":
            raise StackError(
                f"interrupt cleanup container name mismatch for {service}"
            )
        durable_identity = expected_identities.get(service)
        if durable_identity is not None:
            if container_id != durable_identity["container_id"]:
                raise StackError(
                    f"interrupt cleanup container identity drift for {service}"
                )
            if image_id != durable_identity["image_id"]:
                raise StackError(
                    f"interrupt cleanup image identity drift for {service}"
                )
        observed[service] = {
            "container_id": container_id,
            "image_id": image_id,
            "name": expected_name,
        }
    if inspected_ids != container_ids:
        raise StackError(
            "interrupt cleanup container inspection was incomplete"
        )
    return observed


def destroyed_service_inventory(
    run_identity: dict[str, Any],
    *,
    deadline: float | None = None,
) -> dict[str, dict[str, bool]]:
    inventory: dict[str, dict[str, bool]] = {}
    for service in SERVICES:
        state = inspect_service(
            service,
            run_identity=run_identity,
            deadline=deadline,
        )
        if state.get("exists") or state.get("running"):
            raise StackError(
                f"service {service} still exists after recovery destroy"
            )
        inventory[service] = {"exists": False, "running": False}
    return inventory


def cleanup_remaining_timeout(
    deadline: float,
    *,
    stage: str,
) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise StackError(
            f"cleanup transaction timed out before {stage}"
        )
    return remaining


def cleanup_recovery_run(
    run_identity: dict[str, Any],
    *,
    fallback_timestamp: str,
) -> dict[str, Any]:
    deadline = time.monotonic() + CLEANUP_TIMEOUT_SECONDS
    cleanup_errors: list[str] = []
    cleanup_requested_at = safe_utc_now(
        fallback=fallback_timestamp,
        errors=cleanup_errors,
        field="cleanup_requested_at timestamp",
    )
    try:
        completed = compose_process(
            "down",
            "--volumes",
            "--remove-orphans",
            run_identity=run_identity,
            deadline=deadline,
            timeout_stage="teardown command",
        )
    except Exception as exc:
        cleanup_errors.append(f"teardown command: {exc}")
    else:
        if completed.returncode:
            cleanup_errors.append(
                "teardown command: "
                + (_completed_output(completed) or f"exit {completed.returncode}")
            )

    try:
        services = destroyed_service_inventory(
            run_identity,
            deadline=deadline,
        )
    except Exception as exc:
        services = {}
        cleanup_errors.append(f"service absence: {exc}")

    try:
        database_volume = database_volume_observation(
            run_identity,
            deadline=deadline,
        )
    except Exception as exc:
        database_volume = {
            "name": str(run_identity.get("database_volume_name") or ""),
            "exists": None,
        }
        cleanup_errors.append(f"database volume absence: {exc}")

    expected_volume = {
        "name": str(run_identity.get("database_volume_name") or ""),
        "exists": False,
    }
    services_absent = (
        set(services) == set(SERVICES)
        and all(
            state == {"exists": False, "running": False}
            for state in services.values()
        )
    )
    volume_absent = database_volume == expected_volume
    if not services_absent and not any(
        error.startswith("service absence:") for error in cleanup_errors
    ):
        cleanup_errors.append("service absence: inventory is not fully absent")
    if not volume_absent and not any(
        error.startswith("database volume absence:")
        for error in cleanup_errors
    ):
        cleanup_errors.append("database volume absence: volume still exists")
    cleanup_confirmed_at = (
        safe_utc_now(
            fallback=cleanup_requested_at,
            errors=cleanup_errors,
            field="cleanup_confirmed_at timestamp",
        )
        if services_absent and volume_absent
        else None
    )
    observed_at = cleanup_confirmed_at or safe_utc_now(
        fallback=cleanup_requested_at,
        errors=cleanup_errors,
        field="cleanup observation timestamp",
    )
    try:
        cleanup_remaining_timeout(
            deadline,
            stage="setup_blocked event preparation completion",
        )
    except Exception as exc:
        cleanup_errors.append(f"blocked event preparation: {exc}")
    return {
        "cleanup_requested_at": cleanup_requested_at,
        "cleanup_confirmed_at": cleanup_confirmed_at,
        "cleanup_error": (
            "; ".join(cleanup_errors) if cleanup_errors else None
        ),
        "database_volume": database_volume,
        "after": {
            "observed_at": observed_at,
            "services": services,
        },
    }


def failed_cleanup_result(
    run_identity: dict[str, Any],
    *,
    fallback_timestamp: str,
    cleanup_error: str,
) -> dict[str, Any]:
    timestamp_errors: list[str] = []
    timestamp = safe_utc_now(
        fallback=fallback_timestamp,
        errors=timestamp_errors,
        field="cleanup fallback timestamp",
    )
    errors = [cleanup_error, *timestamp_errors]
    return {
        "cleanup_requested_at": timestamp,
        "cleanup_confirmed_at": None,
        "cleanup_error": "; ".join(error for error in errors if error),
        "database_volume": {
            "name": str(run_identity.get("database_volume_name") or ""),
            "exists": None,
        },
        "after": {
            "observed_at": timestamp,
            "services": {},
        },
    }


def append_setup_blocked(
    *,
    fault_id: str,
    requested_at: str,
    identity: dict[str, Any],
    run_identity: dict[str, Any],
    failure_stage: str,
    setup_failure: str,
    cleanup: dict[str, Any],
    terminal_recording_error: str | None,
) -> dict[str, Any]:
    return append_event(
        "setup_blocked",
        _recorded_at=str(cleanup["after"]["observed_at"]),
        fault_id=fault_id,
        requested_at=requested_at,
        identity=identity,
        run_identity=run_identity,
        failure_stage=failure_stage,
        failure_reason=setup_failure.partition(": ")[2] or setup_failure,
        setup_failure=setup_failure,
        cleanup_requested_at=cleanup["cleanup_requested_at"],
        cleanup_confirmed_at=cleanup["cleanup_confirmed_at"],
        cleanup_error=cleanup["cleanup_error"],
        terminal_recording_error=terminal_recording_error,
        database_volume=cleanup["database_volume"],
        after=cleanup["after"],
    )


def record_setup_blocked(
    *,
    fault_id: str,
    requested_at: str,
    identity: dict[str, Any],
    run_identity: dict[str, Any],
    failure_stage: str,
    failure: BaseException,
) -> tuple[dict[str, Any] | None, str, str | None]:
    setup_failure = f"{failure_stage}: {exception_text(failure)}"
    try:
        cleanup = cleanup_recovery_run(
            run_identity,
            fallback_timestamp=requested_at,
        )
    except BaseException as exc:
        cleanup = failed_cleanup_result(
            run_identity,
            fallback_timestamp=requested_at,
            cleanup_error=f"cleanup helper: {exception_text(exc)}",
        )

    terminal_errors: list[str] = []
    try:
        blocked = append_setup_blocked(
            fault_id=fault_id,
            requested_at=requested_at,
            identity=identity,
            run_identity=run_identity,
            failure_stage=failure_stage,
            setup_failure=setup_failure,
            cleanup=cleanup,
            terminal_recording_error=None,
        )
    except BaseException as exc:
        terminal_errors.append(exception_text(exc))
        terminal_recording_error = "; ".join(terminal_errors)
        try:
            blocked = append_event(
                "setup_blocked",
                _recorded_at=str(cleanup["after"]["observed_at"]),
                fault_id=fault_id,
                requested_at=requested_at,
                identity=identity,
                run_identity=run_identity,
                failure_stage=failure_stage,
                failure_reason=exception_text(failure),
                setup_failure=setup_failure,
                cleanup_requested_at=cleanup["cleanup_requested_at"],
                cleanup_confirmed_at=cleanup["cleanup_confirmed_at"],
                cleanup_error=cleanup["cleanup_error"],
                terminal_recording_error=terminal_recording_error,
                database_volume=cleanup["database_volume"],
                after=cleanup["after"],
            )
        except BaseException as fallback_exc:
            terminal_errors.append(exception_text(fallback_exc))
            blocked = None
    return blocked, str(cleanup.get("cleanup_error") or ""), (
        "; ".join(terminal_errors) if terminal_errors else None
    )


def reject_terminal_evidence_directory() -> None:
    events = load_verified_events()
    if has_incomplete_fresh_up(events):
        raise StackError("recovery event log has an incomplete fresh-up")
    if has_incomplete_abort(events):
        raise StackError("recovery event log has an incomplete abort")
    if events and events[-1].get("action") in TERMINAL_ACTIONS:
        raise StackError(
            "recovery event log is terminal after "
            f"{events[-1].get('action')}"
        )


def wait_service(
    service: str,
    timeout_seconds: int = 240,
    *,
    run_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last = inspect_service(service, run_identity=run_identity)
    while time.monotonic() < deadline:
        last = inspect_service(service, run_identity=run_identity)
        health_ready = (
            last.get("health") == "healthy"
            if service in HEALTHCHECK_SERVICES
            else last.get("health") in {"", "healthy"}
        )
        if last.get("running") and health_ready:
            return last
        time.sleep(2)
    raise StackError(f"service {service} did not become ready: {last}")


def validate_config() -> None:
    reject_terminal_evidence_directory()
    identity = assert_frozen()
    run_identity = new_recovery_run_identity(
        "config-check",
        run_id="0" * 32,
        directory=evidence_directory(),
    )
    assert_isolated_compose(identity, run_identity=run_identity)
    print(
        json.dumps(
            {
                "ok": True,
                "run_identity": run_identity,
                "compose_project_name": recovery_project_name(run_identity),
                **identity,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@mutating_controller_command
def fresh_up(fault_id: str) -> None:
    reject_terminal_evidence_directory()
    validated_fault = validated_fault_id(fault_id)
    identity = assert_frozen()
    require_new_evidence_run()
    run_identity = new_recovery_run_identity(
        validated_fault,
        run_id=secrets.token_hex(16),
        directory=evidence_directory(),
    )
    assert_isolated_compose(identity, run_identity=run_identity)
    database_volume = database_volume_observation(run_identity)
    if database_volume != {
        "name": run_identity["database_volume_name"],
        "exists": False,
    }:
        raise StackError("fresh-up target database volume already exists")
    before = stack_snapshot(run_identity=run_identity)
    requested_at = now()
    append_event(
        "fresh_up_started",
        fault_id=validated_fault,
        requested_at=requested_at,
        identity=identity,
        run_identity=run_identity,
        database_volume=database_volume,
        before=before,
    )
    failure_stage = "initial_down"
    try:
        compose(
            "down",
            "--volumes",
            "--remove-orphans",
            run_identity=run_identity,
        )
        failure_stage = "dependency_up"
        compose(
            "up",
            "--detach",
            "postgres",
            "qdrant",
            "minio",
            run_identity=run_identity,
        )
        failure_stage = "dependency_readiness"
        wait_service("postgres", run_identity=run_identity)
        wait_service("qdrant", run_identity=run_identity)
        wait_service("minio", run_identity=run_identity)
        failure_stage = "migration"
        compose(
            "run",
            "--rm",
            "--no-deps",
            "forwin",
            "alembic",
            "upgrade",
            "head",
            run_identity=run_identity,
        )
        failure_stage = "database_sentinel"
        sentinel = initialize_recovery_sentinel(
            fault_id=validated_fault,
            run_identity=run_identity,
            identity=identity,
        )
        failure_stage = "application_up"
        compose(
            "up",
            "--detach",
            "forwin",
            "generation-worker",
            "outbox-worker",
            "forwin-mcp",
            "publisher-worker",
            "publisher-browser",
            run_identity=run_identity,
        )
        failure_stage = "application_readiness"
        for service in SERVICES:
            wait_service(service, run_identity=run_identity)
        failure_stage = "functional_probe"
        after = stack_snapshot(probe=True, run_identity=run_identity)
        failure_stage = "database_volume_postcondition"
        database_volume = confirmed_fresh_database_volume(
            run_identity,
            requested_at=requested_at,
        )
        failure_stage = "completion_event"
        append_event(
            "fresh_up_completed",
            fault_id=validated_fault,
            requested_at=requested_at,
            identity=identity,
            run_identity=run_identity,
            database_volume=database_volume,
            sentinel=sentinel,
            after=after,
        )
        failure_stage = "response_serialization"
        serialized = json.dumps(after, ensure_ascii=False, indent=2)
        failure_stage = "response_print"
        print(serialized)
        return
    except BaseException as failure:
        if not isinstance(failure, Exception):
            try:
                cleanup = cleanup_recovery_run(
                    run_identity,
                    fallback_timestamp=requested_at,
                )
                if cleanup_is_confirmed(cleanup, run_identity):
                    append_event(
                        "interrupted_cleanup",
                        _recorded_at=str(
                            cleanup["after"]["observed_at"]
                        ),
                        fault_id=validated_fault,
                        requested_at=requested_at,
                        identity=identity,
                        run_identity=run_identity,
                        interrupted_stage=failure_stage,
                        database_volume_before=database_volume,
                        database_volume=cleanup["database_volume"],
                        cleanup_requested_at=cleanup[
                            "cleanup_requested_at"
                        ],
                        cleanup_confirmed_at=cleanup[
                            "cleanup_confirmed_at"
                        ],
                        cleanup_confirmed=True,
                        cleanup_error=cleanup["cleanup_error"],
                        after=cleanup["after"],
                    )
            except BaseException:
                pass
            raise
        try:
            (
                _blocked,
                cleanup_error,
                terminal_recording_error,
            ) = record_setup_blocked(
                fault_id=validated_fault,
                requested_at=requested_at,
                identity=identity,
                run_identity=run_identity,
                failure_stage=failure_stage,
                failure=failure,
            )
        except BaseException as coordinator_error:
            cleanup_error = "cleanup status unavailable"
            terminal_recording_error = (
                "record_setup_blocked: "
                f"{exception_text(coordinator_error)}"
            )
        detail = (
            "fresh-up failed: "
            f"setup_failure={failure_stage}: {exception_text(failure)}; "
            f"cleanup_error={cleanup_error or '<none>'}; "
            "terminal_recording_error="
            f"{terminal_recording_error or '<none>'}"
        )
        raise StackError(detail) from failure


@mutating_controller_command
def v1_up() -> None:
    reject_terminal_evidence_directory()
    identity = assert_frozen(harness_mode="v1")
    assert_isolated_compose(identity)
    require_new_evidence_run()
    run_id = secrets.token_hex(16)
    before = stack_snapshot()
    append_event(
        "v1_fresh_up_started",
        run_id=run_id,
        identity=identity,
        before=before,
    )
    compose("down", "--volumes", "--remove-orphans")
    compose("up", "--detach", "postgres", "qdrant", "minio")
    for service in ("postgres", "qdrant", "minio"):
        wait_service(service)
    migration_cycle = run_v1_migration_cycle()
    stale_schema = verify_v1_stale_schema_failfast(
        str(migration_cycle["final_revision"])
    )
    compose(
        "up",
        "--detach",
        "forwin",
        "generation-worker",
        "outbox-worker",
        "forwin-mcp",
        "publisher-worker",
        "publisher-browser",
    )
    for service in SERVICES:
        wait_service(service)
    embedding = run_v1_embedding_smoke()
    after = stack_snapshot(probe=True)
    event = append_event(
        "v1_preflight_completed",
        run_id=run_id,
        identity=identity,
        migration_cycle=migration_cycle,
        stale_schema_failfast=stale_schema,
        embedding=embedding,
        after=after,
    )
    print(json.dumps(event, ensure_ascii=False, indent=2))


@mutating_controller_command
def destroy() -> None:
    reject_terminal_evidence_directory()
    existing = load_verified_events()
    v1_starts = [
        event
        for event in existing
        if event.get("action") == "v1_fresh_up_started"
    ]
    if v1_starts:
        if len(v1_starts) != 1 or any(
            event.get("action") == "fresh_up_started" for event in existing
        ):
            raise StackError("V1 destroy lifecycle is invalid")
        identity = assert_frozen(harness_mode="v1")
        assert_isolated_compose(identity)
        before = stack_snapshot()
        requested_at = now()
        compose("down", "--volumes", "--remove-orphans")
        after = stack_snapshot()
        if any(
            state.get("exists") or state.get("running")
            for state in (after.get("services") or {}).values()
        ):
            raise StackError("a V1 service still exists after destroy")
        append_event(
            "destroyed",
            run_id=str(v1_starts[0].get("run_id") or ""),
            requested_at=requested_at,
            identity=identity,
            before=before,
            after=after,
        )
        print(json.dumps(after, ensure_ascii=False, indent=2))
        return
    fault_id = str((existing[0] if existing else {}).get("fault_id") or "")
    context = require_active_recovery_run(fault_id)
    events = context["events"]
    hold_state = setup_hold_state(events)
    if hold_state["active_by_id"]:
        raise StackError("destroy cannot run with an active setup hold")
    fault_events = [
        event
        for event in events
        if event.get("action")
        in {"fault_service_stopped", "fault_service_killed", "fault_marked"}
    ]
    recovery_events = [
        event
        for event in events
        if event.get("action")
        in {"fault_service_recovered", "recovery_marked"}
    ]
    if (
        len(fault_events) != 1
        or len(recovery_events) != 1
        or events.index(fault_events[0]) >= events.index(recovery_events[0])
    ):
        raise StackError("destroy requires one completed fault recovery")
    fault_event = fault_events[0]
    recovery_event = recovery_events[0]
    if fault_event.get("action") == "fault_marked":
        pair_matches = (
            recovery_event.get("action") == "recovery_marked"
            and recovery_event.get("fault_kind")
            == fault_event.get("fault_kind")
        )
    else:
        pair_matches = (
            recovery_event.get("action") == "fault_service_recovered"
            and recovery_event.get("service") == fault_event.get("service")
        )
    if not pair_matches:
        raise StackError("destroy requires a matching fault recovery pair")
    run_identity = context["run_identity"]
    identity = assert_frozen()
    if identity != context["identity"]:
        raise StackError("recovery harness identity changed before destroy")
    assert_isolated_compose(identity, run_identity=run_identity)
    database_volume_before = confirmed_database_volume(
        run_identity,
        context["database_volume"],
    )
    before = stack_snapshot(run_identity=run_identity)
    requested_at = now()
    compose(
        "down",
        "--volumes",
        "--remove-orphans",
        run_identity=run_identity,
    )
    services = destroyed_service_inventory(run_identity)
    database_volume = database_volume_observation(run_identity)
    if database_volume != {
        "name": run_identity["database_volume_name"],
        "exists": False,
    }:
        raise StackError("database volume still exists after recovery destroy")
    after = {"observed_at": now(), "services": services}
    append_event(
        "destroyed",
        fault_id=fault_id,
        requested_at=requested_at,
        identity=identity,
        run_identity=run_identity,
        database_volume_before=database_volume_before,
        database_volume=database_volume,
        before=before,
        after=after,
    )
    print(json.dumps(after, ensure_ascii=False, indent=2))


@mutating_controller_command
def stop_fault_service(service: str, fault_id: str) -> None:
    if service not in FAULT_SERVICES:
        raise StackError(f"service is not an allowed fault boundary: {service}")
    context = require_active_recovery_run(fault_id)
    if any(
        event.get("action")
        in {"fault_service_stopped", "fault_service_killed", "fault_marked"}
        for event in context["events"]
    ):
        raise StackError("duplicate fault event is not allowed")
    run_identity = context["run_identity"]
    identity = assert_frozen()
    if identity != context["identity"]:
        raise StackError("recovery harness identity changed before fault")
    assert_isolated_compose(identity, run_identity=run_identity)
    before = inspect_service(service, run_identity=run_identity)
    if not before.get("running"):
        raise StackError(f"service {service} is not running before fault injection")
    requested_at = now()
    compose(
        "stop",
        "--timeout",
        "10",
        service,
        run_identity=run_identity,
    )
    after = inspect_service(service, run_identity=run_identity)
    if after.get("running"):
        raise StackError(f"service {service} is still running after stop")
    fault_time = now()
    database_volume = confirmed_database_volume(
        run_identity,
        context["database_volume"],
    )
    event = append_event(
        "fault_service_stopped",
        fault_id=fault_id,
        service=service,
        requested_at=requested_at,
        fault_time=fault_time,
        identity=identity,
        run_identity=run_identity,
        database_volume=database_volume,
        before=before,
        after=after,
    )
    print(json.dumps(event, ensure_ascii=False, indent=2))


@mutating_controller_command
def start_fault_service(service: str, fault_id: str) -> None:
    if service not in FAULT_SERVICES:
        raise StackError(f"service is not an allowed recovery boundary: {service}")
    context = require_active_recovery_run(fault_id)
    fault_events = [
        event
        for event in context["events"]
        if event.get("action")
        in {"fault_service_stopped", "fault_service_killed"}
    ]
    recovery_events = [
        event
        for event in context["events"]
        if event.get("action")
        in {"fault_service_recovered", "recovery_marked"}
    ]
    if (
        len(fault_events) != 1
        or fault_events[0].get("service") != service
    ):
        raise StackError(f"recovery for {service} has no matching fault event")
    if recovery_events:
        raise StackError("duplicate recovery event is not allowed")
    run_identity = context["run_identity"]
    identity = assert_frozen()
    if identity != context["identity"]:
        raise StackError("recovery harness identity changed before recovery")
    assert_isolated_compose(identity, run_identity=run_identity)
    before = inspect_service(service, run_identity=run_identity)
    if before.get("running"):
        raise StackError(f"service {service} is already running before recovery")
    requested_at = now()
    if service in CRASH_SERVICES:
        command(
            "docker",
            "container",
            "update",
            "--restart=unless-stopped",
            str(before["container_id"]),
        )
    compose("start", service, run_identity=run_identity)
    ready = wait_service(service, run_identity=run_identity)
    ready["probe"] = functional_probe(service, run_identity=run_identity)
    recovery_time = now()
    database_volume = confirmed_database_volume(
        run_identity,
        context["database_volume"],
    )
    event = append_event(
        "fault_service_recovered",
        fault_id=fault_id,
        service=service,
        requested_at=requested_at,
        recovery_time=recovery_time,
        identity=identity,
        run_identity=run_identity,
        database_volume=database_volume,
        before=before,
        after=ready,
    )
    print(json.dumps(event, ensure_ascii=False, indent=2))


@mutating_controller_command
def kill_fault_service(service: str, fault_id: str) -> None:
    if service not in CRASH_SERVICES:
        raise StackError(f"service is not an allowed crash boundary: {service}")
    context = require_active_recovery_run(fault_id)
    if any(
        event.get("action")
        in {"fault_service_stopped", "fault_service_killed", "fault_marked"}
        for event in context["events"]
    ):
        raise StackError("duplicate fault event is not allowed")
    run_identity = context["run_identity"]
    identity = assert_frozen()
    if identity != context["identity"]:
        raise StackError("recovery harness identity changed before crash")
    assert_isolated_compose(identity, run_identity=run_identity)
    before = inspect_service(service, run_identity=run_identity)
    if not before.get("running"):
        raise StackError(f"service {service} is not running before crash injection")
    container_id = str(before["container_id"])
    requested_at = now()
    command("docker", "container", "update", "--restart=no", container_id)
    command("docker", "container", "kill", "--signal=KILL", container_id)
    after = inspect_service(service, run_identity=run_identity)
    if after.get("running"):
        raise StackError(f"service {service} is still running after SIGKILL")
    crash_time = now()
    database_volume = confirmed_database_volume(
        run_identity,
        context["database_volume"],
    )
    event = append_event(
        "fault_service_killed",
        fault_id=fault_id,
        service=service,
        requested_at=requested_at,
        crash_time=crash_time,
        signal="SIGKILL",
        identity=identity,
        run_identity=run_identity,
        database_volume=database_volume,
        before=before,
        after=after,
    )
    print(json.dumps(event, ensure_ascii=False, indent=2))


@mutating_controller_command
def setup_hold_service(
    service: str,
    fault_id: str,
    hold_id: str,
    fault_kind: str,
    purpose: str,
) -> dict[str, Any]:
    if service not in FAULT_SERVICES:
        raise StackError(f"service is not an allowed setup hold: {service}")
    validated_hold = validated_hold_id(hold_id)
    if fault_kind not in SUPPORTED_RECOVERY_FAULTS:
        raise StackError("setup hold fault kind is unsupported")
    if purpose not in SETUP_HOLD_PURPOSES:
        raise StackError("setup hold purpose is unsupported")
    primary_service = PRIMARY_FAULT_SERVICE.get(fault_kind)
    primary_boundary = (
        fault_kind == "publisher_backend_unavailable"
        and service == "publisher-worker"
        and purpose == "pre-fault-boundary"
    )
    if purpose == "pre-fault-boundary" and not primary_boundary:
        raise StackError("setup hold pre-fault purpose is not allowed")
    if service == primary_service and not primary_boundary:
        raise StackError(f"setup hold targets primary fault service: {service}")
    context = require_active_recovery_run(fault_id)
    if any(
        event.get("service") == service
        and event.get("action")
        in {
            "fault_service_stopped",
            "fault_service_killed",
        }
        for event in context["events"]
    ):
        raise StackError(f"setup hold targets primary fault service: {service}")
    hold_state = setup_hold_state(context["events"])
    if validated_hold in hold_state["used_hold_ids"]:
        raise StackError(
            f"setup hold identity was already used: {validated_hold}"
        )
    if service in hold_state["active_by_service"]:
        raise StackError(f"service {service} already has an active setup hold")
    run_identity = context["run_identity"]
    identity = assert_frozen()
    if identity != context["identity"]:
        raise StackError("recovery harness identity changed before setup hold")
    assert_isolated_compose(identity, run_identity=run_identity)
    before = inspect_service(service, run_identity=run_identity)
    if not before.get("exists") or not before.get("running"):
        raise StackError(
            f"service {service} is not running before setup hold"
        )
    requested_at = now()
    compose(
        "stop",
        "--timeout",
        "10",
        service,
        run_identity=run_identity,
    )
    after = inspect_service(service, run_identity=run_identity)
    if (
        not after.get("exists")
        or after.get("running")
        or after.get("container_id") != before.get("container_id")
        or after.get("image_id") != before.get("image_id")
    ):
        raise StackError(
            f"service {service} identity changed during setup hold"
        )
    hold_time = now()
    database_volume = confirmed_database_volume(
        run_identity,
        context["database_volume"],
    )
    event = append_event(
        "setup_service_held",
        fault_id=fault_id,
        fault_kind=fault_kind,
        purpose=purpose,
        hold_id=validated_hold,
        service=service,
        requested_at=requested_at,
        hold_time=hold_time,
        identity=identity,
        run_identity=run_identity,
        database_volume=database_volume,
        before=before,
        after=after,
    )
    print(json.dumps(event, ensure_ascii=False, indent=2))
    return event


@mutating_controller_command
def setup_release_service(
    service: str,
    fault_id: str,
    hold_id: str,
) -> dict[str, Any]:
    if service not in FAULT_SERVICES:
        raise StackError(f"service is not an allowed setup release: {service}")
    validated_hold = validated_hold_id(hold_id)
    context = require_active_recovery_run(fault_id)
    if any(
        event.get("service") == service
        and event.get("action")
        in {
            "fault_service_stopped",
            "fault_service_killed",
        }
        for event in context["events"]
    ):
        raise StackError(
            f"setup release targets primary fault service: {service}"
        )
    hold_state = setup_hold_state(context["events"])
    held = hold_state["active_by_id"].get(validated_hold)
    if held is None:
        raise StackError(
            f"release has no matching setup hold: {validated_hold}"
        )
    if held["service"] != service:
        raise StackError(
            "setup hold release service mismatch: "
            f"expected {held['service']}, got {service}"
        )
    if not held["container_id"] or not held["image_id"]:
        raise StackError("setup hold service identity is incomplete")
    run_identity = context["run_identity"]
    identity = assert_frozen()
    if identity != context["identity"]:
        raise StackError("recovery harness identity changed before setup release")
    assert_isolated_compose(identity, run_identity=run_identity)
    before = inspect_service(service, run_identity=run_identity)
    if (
        not before.get("exists")
        or before.get("running")
        or before.get("container_id") != held["container_id"]
        or before.get("image_id") != held["image_id"]
    ):
        raise StackError(
            f"service {service} identity drifted before setup release"
        )
    requested_at = now()
    compose("start", service, run_identity=run_identity)
    after = wait_service(service, run_identity=run_identity)
    after["probe"] = functional_probe(service, run_identity=run_identity)
    if (
        after.get("container_id") != held["container_id"]
        or after.get("image_id") != held["image_id"]
    ):
        raise StackError(
            f"service {service} identity drifted during setup release"
        )
    release_time = now()
    database_volume = confirmed_database_volume(
        run_identity,
        context["database_volume"],
    )
    event = append_event(
        "setup_service_released",
        fault_id=fault_id,
        fault_kind=held["fault_kind"],
        purpose=held["purpose"],
        hold_id=validated_hold,
        service=service,
        requested_at=requested_at,
        release_time=release_time,
        identity=identity,
        run_identity=run_identity,
        database_volume=database_volume,
        before=before,
        after=after,
    )
    print(json.dumps(event, ensure_ascii=False, indent=2))
    return event


@mutating_controller_command
def setup_discard_service(
    service: str,
    fault_id: str,
    hold_id: str,
) -> dict[str, Any]:
    if service not in FAULT_SERVICES:
        raise StackError(f"service is not an allowed setup discard: {service}")
    validated_hold = validated_hold_id(hold_id)
    context = require_active_recovery_run(fault_id)
    if any(
        event.get("service") == service
        and event.get("action")
        in {
            "fault_service_stopped",
            "fault_service_killed",
        }
        for event in context["events"]
    ):
        raise StackError(
            f"setup discard targets primary fault service: {service}"
        )
    hold_state = setup_hold_state(context["events"])
    held = hold_state["active_by_id"].get(validated_hold)
    if held is None:
        raise StackError(
            f"discard has no matching setup hold: {validated_hold}"
        )
    if held["service"] != service:
        raise StackError(
            "setup hold discard service mismatch: "
            f"expected {held['service']}, got {service}"
        )
    if held["purpose"] != "auxiliary":
        raise StackError(
            "setup discard is allowed only for an auxiliary hold"
        )
    if not held["container_id"] or not held["image_id"]:
        raise StackError("setup hold service identity is incomplete")
    run_identity = context["run_identity"]
    identity = assert_frozen()
    if identity != context["identity"]:
        raise StackError("recovery harness identity changed before setup discard")
    assert_isolated_compose(identity, run_identity=run_identity)
    before = inspect_service(service, run_identity=run_identity)
    if not before.get("exists"):
        raise StackError(
            f"service {service} is missing before setup discard"
        )
    if before.get("running"):
        raise StackError(
            f"service {service} is running before setup discard"
        )
    if (
        before.get("container_id") != held["container_id"]
        or before.get("image_id") != held["image_id"]
    ):
        raise StackError(
            f"service {service} identity drifted before setup discard"
        )
    requested_at = now()
    after = inspect_service(service, run_identity=run_identity)
    if (
        not after.get("exists")
        or after.get("running")
        or after.get("container_id") != before.get("container_id")
        or after.get("image_id") != before.get("image_id")
    ):
        raise StackError(
            f"service {service} changed while discarding setup hold"
        )
    discard_time = now()
    database_volume = confirmed_database_volume(
        run_identity,
        context["database_volume"],
    )
    event = append_event(
        "setup_service_discarded",
        fault_id=fault_id,
        fault_kind=held["fault_kind"],
        purpose=held["purpose"],
        hold_id=validated_hold,
        service=service,
        requested_at=requested_at,
        discard_time=discard_time,
        identity=identity,
        run_identity=run_identity,
        database_volume=database_volume,
        before=before,
        after=after,
    )
    print(json.dumps(event, ensure_ascii=False, indent=2))
    return event


def active_recovery_state(events: list[dict[str, Any]]) -> dict[str, Any]:
    hold_state = setup_hold_state(events)
    primary_faults = [
        {
            "action": str(event.get("action") or ""),
            "service": str(event.get("service") or ""),
            "fault_kind": str(event.get("fault_kind") or ""),
        }
        for event in events
        if event.get("action")
        in {"fault_service_stopped", "fault_service_killed", "fault_marked"}
    ]
    primary_recoveries = [
        {
            "action": str(event.get("action") or ""),
            "service": str(event.get("service") or ""),
            "fault_kind": str(event.get("fault_kind") or ""),
        }
        for event in events
        if event.get("action")
        in {"fault_service_recovered", "recovery_marked"}
    ]
    return {
        "active_holds": sorted(
            hold_state["active_by_id"].values(),
            key=lambda item: (item["service"], item["hold_id"]),
        ),
        "primary": {
            "faults": primary_faults,
            "recoveries": primary_recoveries,
        },
    }


def cleanup_is_confirmed(
    cleanup: dict[str, Any],
    run_identity: dict[str, Any],
) -> bool:
    services = (cleanup.get("after") or {}).get("services")
    return (
        bool(cleanup.get("cleanup_confirmed_at"))
        and isinstance(services, dict)
        and set(services) == set(SERVICES)
        and all(
            state == {"exists": False, "running": False}
            for state in services.values()
        )
        and cleanup.get("database_volume")
        == {
            "name": run_identity["database_volume_name"],
            "exists": False,
        }
    )


@mutating_controller_command
def abort_recovery_run(
    fault_id: str,
    stage: str,
    reason: str,
) -> None:
    context = require_active_recovery_run(fault_id)
    validated_stage = validated_abort_stage(stage)
    sanitized_reason = sanitized_abort_reason(reason)
    run_identity = context["run_identity"]
    identity = assert_frozen()
    if identity != context["identity"]:
        raise StackError("recovery harness identity changed before abort")
    assert_isolated_compose(identity, run_identity=run_identity)
    database_volume_before = confirmed_database_volume(
        run_identity,
        context["database_volume"],
    )
    timestamp_errors: list[str] = []
    requested_at = safe_utc_now(
        fallback=str(context["events"][0].get("requested_at") or ""),
        errors=timestamp_errors,
        field="abort requested_at timestamp",
    )
    active_state = active_recovery_state(context["events"])
    setup_failure = f"{validated_stage}: {sanitized_reason}"
    terminal_errors: list[str] = []
    try:
        append_event(
            "abort_started",
            _recorded_at=requested_at,
            fault_id=fault_id,
            requested_at=requested_at,
            identity=identity,
            run_identity=run_identity,
            database_volume=database_volume_before,
            failure_stage=validated_stage,
            failure_reason=sanitized_reason,
            active_state=active_state,
        )
    except BaseException as exc:
        terminal_errors.append(
            f"abort_started: {exception_text(exc)}"
        )
    try:
        cleanup = cleanup_recovery_run(
            run_identity,
            fallback_timestamp=requested_at,
        )
    except BaseException as exc:
        cleanup = failed_cleanup_result(
            run_identity,
            fallback_timestamp=requested_at,
            cleanup_error=f"cleanup helper: {exception_text(exc)}",
        )
    if timestamp_errors:
        timestamp_error = "; ".join(timestamp_errors)
        existing_cleanup_error = str(cleanup.get("cleanup_error") or "")
        cleanup["cleanup_error"] = "; ".join(
            item for item in (existing_cleanup_error, timestamp_error) if item
        )
    cleanup_confirmed = cleanup_is_confirmed(cleanup, run_identity)
    try:
        append_event(
            "setup_blocked",
            _recorded_at=str(cleanup["after"]["observed_at"]),
            fault_id=fault_id,
            requested_at=requested_at,
            identity=identity,
            run_identity=run_identity,
            database_volume_before=database_volume_before,
            database_volume=cleanup["database_volume"],
            failure_stage=validated_stage,
            failure_reason=sanitized_reason,
            setup_failure=setup_failure,
            active_state=active_state,
            cleanup_requested_at=cleanup["cleanup_requested_at"],
            cleanup_confirmed_at=cleanup["cleanup_confirmed_at"],
            cleanup_confirmed=cleanup_confirmed,
            cleanup_error=cleanup["cleanup_error"],
            terminal_recording_error=(
                "; ".join(terminal_errors) if terminal_errors else None
            ),
            after=cleanup["after"],
        )
    except BaseException as exc:
        terminal_errors.append(
            f"setup_blocked: {exception_text(exc)}"
        )
    detail = (
        "recovery abort: "
        f"setup_failure={setup_failure}; "
        f"cleanup_error={cleanup.get('cleanup_error') or '<none>'}; "
        "terminal_recording_error="
        f"{'; '.join(terminal_errors) if terminal_errors else '<none>'}"
    )
    raise StackError(detail)


@mutating_controller_command
def interrupt_cleanup_recovery_run(fault_id: str) -> dict[str, Any]:
    context = require_interrupt_cleanup_run(fault_id)
    run_identity = context["run_identity"]
    identity = assert_frozen()
    if identity != context["identity"]:
        raise StackError(
            "recovery harness identity changed before interrupt cleanup"
        )
    assert_isolated_compose(identity, run_identity=run_identity)
    database_volume_before = interrupt_cleanup_volume_observation(context)
    require_interrupt_cleanup_namespace(context)
    requested_at = now()
    active_state = active_recovery_state(context["events"])
    cleanup = cleanup_recovery_run(
        run_identity,
        fallback_timestamp=requested_at,
    )
    confirmed = cleanup_is_confirmed(cleanup, run_identity)
    if not confirmed:
        raise StackError(
            "interrupt cleanup did not confirm terminal resource removal"
        )
    event = append_event(
        "interrupted_cleanup",
        _recorded_at=str(cleanup["after"]["observed_at"]),
        fault_id=fault_id,
        requested_at=requested_at,
        identity=identity,
        run_identity=run_identity,
        database_volume_before=database_volume_before,
        database_volume=cleanup["database_volume"],
        active_state=active_state,
        cleanup_requested_at=cleanup["cleanup_requested_at"],
        cleanup_confirmed_at=cleanup["cleanup_confirmed_at"],
        cleanup_confirmed=True,
        cleanup_error=cleanup["cleanup_error"],
        after=cleanup["after"],
    )
    print(json.dumps(event, ensure_ascii=False, indent=2))
    return event


@mutating_controller_command
def snapshot(label: str) -> None:
    existing = load_verified_events()
    fault_id = str((existing[0] if existing else {}).get("fault_id") or "")
    context = require_active_recovery_run(fault_id)
    run_identity = context["run_identity"]
    identity = assert_frozen()
    if identity != context["identity"]:
        raise StackError("recovery harness identity changed before snapshot")
    assert_isolated_compose(identity, run_identity=run_identity)
    state = stack_snapshot(run_identity=run_identity)
    database_volume = confirmed_database_volume(
        run_identity,
        context["database_volume"],
    )
    event = append_event(
        "snapshot",
        fault_id=fault_id,
        label=label,
        identity=identity,
        run_identity=run_identity,
        database_volume=database_volume,
        state=state,
    )
    print(json.dumps(event, ensure_ascii=False, indent=2))


def file_inventory_script() -> str:
    return """
import hashlib
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
expected = Path(sys.argv[2])
if str(root) != str(expected) or not root.is_absolute():
    raise SystemExit("inventory root does not match the exact expected root")
try:
    resolved = root.resolve(strict=True)
except FileNotFoundError as exc:
    raise SystemExit("inventory root is missing") from exc
if resolved != root or not root.is_dir() or root.is_symlink():
    raise SystemExit("inventory root is not a canonical real directory")
current = Path(root.anchor)
for part in root.parts[1:]:
    current /= part
    if current.is_symlink():
        raise SystemExit("inventory root has a symlink ancestor")
files = []
for path in sorted(root.rglob("*")):
    if path.is_symlink():
        raise SystemExit("inventory contains a symlink")
    try:
        resolved_path = path.resolve(strict=True)
        relative = resolved_path.relative_to(root)
    except (FileNotFoundError, ValueError) as exc:
        raise SystemExit("inventory path escapes the canonical root") from exc
    if path.is_file():
        files.append(
            {
                "path": relative.as_posix(),
                "size": path.stat().st_size,
                "content_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
print(
    json.dumps(
        {"root": str(root), "root_exists": True, "files": files},
        sort_keys=True,
    )
)
""".strip()


@mutating_controller_command
def file_inventory(service: str, fault_id: str, root: str) -> dict[str, Any]:
    if service != "publisher-browser":
        raise StackError("file inventory service must be publisher-browser")
    normalized_root = str(root or "")
    if normalized_root != PUBLISHER_COVER_ROOT:
        raise StackError(
            "file inventory root must be the exact publisher cover root"
        )
    context = require_active_recovery_run(fault_id)
    run_identity = context["run_identity"]
    identity = assert_frozen()
    if identity != context["identity"]:
        raise StackError("recovery harness identity changed before file inventory")
    assert_isolated_compose(identity, run_identity=run_identity)
    output = compose(
        "exec",
        "-T",
        service,
        "python",
        "-c",
        file_inventory_script(),
        normalized_root,
        PUBLISHER_COVER_ROOT,
        run_identity=run_identity,
    )
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise StackError("file inventory returned invalid JSON") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("root") != normalized_root
        or payload.get("root_exists") is not True
        or not isinstance(payload.get("files"), list)
    ):
        raise StackError("file inventory returned an invalid object")
    seen_paths: set[str] = set()
    for row in payload["files"]:
        raw_path = row.get("path") if isinstance(row, dict) else None
        relative = (
            PurePosixPath(raw_path)
            if isinstance(raw_path, str) and raw_path
            else None
        )
        if (
            not isinstance(row, dict)
            or set(row) != {"path", "size", "content_sha256"}
            or relative is None
            or "\\" in raw_path
            or relative.is_absolute()
            or relative.as_posix() != raw_path
            or any(part in {"", ".", ".."} for part in relative.parts)
            or type(row.get("size")) is not int
            or row["size"] < 0
            or re.fullmatch(
                r"[0-9a-f]{64}",
                str(row.get("content_sha256") or ""),
            )
            is None
        ):
            raise StackError("file inventory returned an unsafe file row")
        if raw_path in seen_paths:
            raise StackError("file inventory returned a duplicate file path")
        seen_paths.add(raw_path)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return payload


@mutating_controller_command
def mark_fault(fault_kind: str, phase: str, fault_id: str) -> dict[str, Any]:
    if fault_kind not in RISK_FAULT_KINDS:
        raise StackError(
            "fault kind is not a typed publisher risk fault"
        )
    if phase not in {"fault", "recovery"}:
        raise StackError("marker phase must be fault or recovery")
    context = require_active_recovery_run(fault_id)
    fault_events = [
        event
        for event in context["events"]
        if event.get("action")
        in {"fault_service_stopped", "fault_service_killed", "fault_marked"}
    ]
    marker_events = [
        event
        for event in context["events"]
        if event.get("action") in {"fault_marked", "recovery_marked"}
    ]
    if any(event.get("fault_kind") != fault_kind for event in marker_events):
        raise StackError("typed marker fault kind changed within recovery run")
    if phase == "fault":
        if fault_events:
            raise StackError("duplicate fault marker is not allowed")
        action = "fault_marked"
        time_field = "fault_time"
    else:
        fault_markers = [
            event for event in marker_events
            if event.get("action") == "fault_marked"
        ]
        recovery_markers = [
            event for event in marker_events
            if event.get("action") == "recovery_marked"
        ]
        if len(fault_events) != 1 or not fault_markers:
            raise StackError("recovery marker cannot be written before fault marker")
        if recovery_markers:
            raise StackError("duplicate recovery marker is not allowed")
        action = "recovery_marked"
        time_field = "recovery_time"
    run_identity = context["run_identity"]
    identity = assert_frozen()
    if identity != context["identity"]:
        raise StackError("recovery harness identity changed before marker")
    assert_isolated_compose(identity, run_identity=run_identity)
    requested_at = now()
    database_volume = confirmed_database_volume(
        run_identity,
        context["database_volume"],
    )
    confirmed_at = now()
    event = append_event(
        action,
        fault_id=fault_id,
        fault_kind=fault_kind,
        requested_at=requested_at,
        **{time_field: confirmed_at},
        identity=identity,
        run_identity=run_identity,
        database_volume=database_volume,
    )
    print(json.dumps(event, ensure_ascii=False, indent=2))
    return event


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Control only the isolated ForWin v5 live-recovery stack."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("config")
    fresh_parser = commands.add_parser("fresh-up")
    fresh_parser.add_argument("--fault-id", required=True)
    commands.add_parser("v1-up")
    commands.add_parser("destroy")
    snapshot_parser = commands.add_parser("snapshot")
    snapshot_parser.add_argument("--label", required=True)
    for name in ("stop", "start"):
        child = commands.add_parser(name)
        child.add_argument("service", choices=sorted(FAULT_SERVICES))
        child.add_argument("--fault-id", required=True)
    kill_parser = commands.add_parser("kill")
    kill_parser.add_argument("service", choices=sorted(CRASH_SERVICES))
    kill_parser.add_argument("--fault-id", required=True)
    mark_parser = commands.add_parser("mark")
    mark_parser.add_argument("fault_kind", choices=sorted(RISK_FAULT_KINDS))
    mark_parser.add_argument("phase", choices=("fault", "recovery"))
    mark_parser.add_argument("--fault-id", required=True)
    inventory_parser = commands.add_parser("file-inventory")
    inventory_parser.add_argument("service", choices=("publisher-browser",))
    inventory_parser.add_argument("--fault-id", required=True)
    inventory_parser.add_argument("--root", required=True)
    endpoint_parser = commands.add_parser("bind-endpoints")
    endpoint_parser.add_argument("--fault-id", required=True)
    endpoint_parser.add_argument("--api-url", required=True)
    endpoint_parser.add_argument("--mcp-url", required=True)
    endpoint_parser.add_argument("--database-host", required=True)
    endpoint_parser.add_argument("--database-port", type=int, required=True)
    endpoint_parser.add_argument("--database-name", required=True)
    endpoint_parser.add_argument("--qdrant-url")
    endpoint_parser.add_argument("--minio-url")
    for name in ("setup-hold", "setup-release", "setup-discard"):
        child = commands.add_parser(name)
        child.add_argument("service", choices=sorted(FAULT_SERVICES))
        child.add_argument("--fault-id", required=True)
        child.add_argument("--hold-id", required=True)
        if name == "setup-hold":
            child.add_argument(
                "--fault-kind",
                choices=sorted(SUPPORTED_RECOVERY_FAULTS),
                required=True,
            )
            child.add_argument(
                "--purpose",
                choices=sorted(SETUP_HOLD_PURPOSES),
                default="auxiliary",
            )
    abort_parser = commands.add_parser("abort")
    abort_parser.add_argument("--fault-id", required=True)
    abort_parser.add_argument("--stage", required=True)
    abort_parser.add_argument("--reason", required=True)
    interrupt_parser = commands.add_parser("interrupt-cleanup")
    interrupt_parser.add_argument("--fault-id", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "config":
        validate_config()
    elif args.command == "fresh-up":
        fresh_up(args.fault_id)
    elif args.command == "v1-up":
        v1_up()
    elif args.command == "destroy":
        destroy()
    elif args.command == "snapshot":
        snapshot(args.label)
    elif args.command == "stop":
        stop_fault_service(args.service, args.fault_id)
    elif args.command == "kill":
        kill_fault_service(args.service, args.fault_id)
    elif args.command == "mark":
        mark_fault(args.fault_kind, args.phase, args.fault_id)
    elif args.command == "file-inventory":
        file_inventory(args.service, args.fault_id, args.root)
    elif args.command == "bind-endpoints":
        bind_recovery_endpoints(
            args.fault_id,
            args.api_url,
            args.mcp_url,
            args.database_host,
            args.database_port,
            args.database_name,
            qdrant_url=args.qdrant_url,
            minio_url=args.minio_url,
        )
    elif args.command == "setup-hold":
        setup_hold_service(
            args.service,
            args.fault_id,
            args.hold_id,
            args.fault_kind,
            args.purpose,
        )
    elif args.command == "setup-release":
        setup_release_service(args.service, args.fault_id, args.hold_id)
    elif args.command == "setup-discard":
        setup_discard_service(args.service, args.fault_id, args.hold_id)
    elif args.command == "abort":
        abort_recovery_run(args.fault_id, args.stage, args.reason)
    elif args.command == "interrupt-cleanup":
        interrupt_cleanup_recovery_run(args.fault_id)
    else:
        start_fault_service(args.service, args.fault_id)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except StackError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
