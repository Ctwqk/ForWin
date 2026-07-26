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
from datetime import UTC, datetime
from pathlib import Path
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
TERMINAL_ACTIONS = frozenset({"destroyed", "setup_blocked"})
CLEANUP_TIMEOUT_SECONDS = 60
RISK_FAULT_KINDS = frozenset(
    {
        "publisher_captcha",
        "publisher_mfa",
        "publisher_account_risk",
    }
)
_FAULT_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_RUN_ID_PATTERN = re.compile(r"[a-f0-9]{32}")

ISOLATED_DATABASE_URL = (
    "postgresql+psycopg://forwin:forwin@postgres:5432/forwin"
)
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
) -> str:
    if args and args[0] in {"docker", "git"}:
        assert_no_control_environment()
        if env is None:
            env = host_command_environment()
    completed = subprocess.run(
        args,
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
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
    )


def compose_process(
    *args: str,
    run_identity: dict[str, Any] | None = None,
    timeout_seconds: float | None = None,
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
        "FORWIN_BACKEND_URL": ISOLATED_API_BASE_URL,
    }
    runtime_tag = str((identity.get("runtime_image") or {}).get("tag") or "")
    browser_tag = str((identity.get("browser_image") or {}).get("tag") or "")
    for service in APPLICATION_SERVICES:
        item = services.get(service) or {}
        environment = item.get("environment") or {}
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
        or expected_postgres_mount not in postgres_mounts
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
) -> str:
    return compose(
        "ps",
        "--all",
        "--quiet",
        service,
        run_identity=run_identity,
    )


def inspect_service(
    service: str,
    *,
    run_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    container_id = compose_container_id(service, run_identity=run_identity)
    if not container_id:
        return {"service": service, "exists": False}
    try:
        payload = json.loads(command("docker", "container", "inspect", container_id))
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


def append_event(action: str, **payload: Any) -> dict[str, Any]:
    path = events_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = load_verified_events()
    if existing and existing[-1].get("action") in TERMINAL_ACTIONS:
        raise StackError(
            "recovery event log is terminal after "
            f"{existing[-1].get('action')}"
        )
    event = {
        "schema_version": 2,
        "recorded_at": now(),
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
        ).splitlines()
    )
    if volume_name not in volume_names:
        return {"name": volume_name, "exists": False}
    try:
        payload = json.loads(
            command("docker", "volume", "inspect", volume_name)
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
    if created_at < request_time:
        raise StackError("database volume creation time predates fresh-up request")
    return observation


def require_active_recovery_run(
    fault_id: str,
) -> dict[str, Any]:
    validated_fault = validated_fault_id(fault_id)
    events = load_verified_events()
    if not events:
        raise StackError("recovery run has no completed fresh-up")
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
    if created_time < request_time:
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


def destroyed_service_inventory(
    run_identity: dict[str, Any],
) -> dict[str, dict[str, bool]]:
    inventory: dict[str, dict[str, bool]] = {}
    for service in SERVICES:
        state = inspect_service(service, run_identity=run_identity)
        if state.get("exists") or state.get("running"):
            raise StackError(
                f"service {service} still exists after recovery destroy"
            )
        inventory[service] = {"exists": False, "running": False}
    return inventory


def cleanup_recovery_run(
    run_identity: dict[str, Any],
) -> dict[str, Any]:
    cleanup_requested_at = now()
    cleanup_errors: list[str] = []
    try:
        completed = compose_process(
            "down",
            "--volumes",
            "--remove-orphans",
            run_identity=run_identity,
            timeout_seconds=CLEANUP_TIMEOUT_SECONDS,
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
        services = destroyed_service_inventory(run_identity)
    except Exception as exc:
        services = {}
        cleanup_errors.append(f"service absence: {exc}")

    try:
        database_volume = database_volume_observation(run_identity)
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
        now() if services_absent and volume_absent else None
    )
    return {
        "cleanup_requested_at": cleanup_requested_at,
        "cleanup_confirmed_at": cleanup_confirmed_at,
        "cleanup_error": (
            "; ".join(cleanup_errors) if cleanup_errors else None
        ),
        "database_volume": database_volume,
        "after": {
            "observed_at": cleanup_confirmed_at or now(),
            "services": services,
        },
    }


def append_setup_blocked(
    *,
    fault_id: str,
    requested_at: str,
    identity: dict[str, Any],
    run_identity: dict[str, Any],
    failure_stage: str,
    failure: BaseException,
) -> dict[str, Any]:
    cleanup = cleanup_recovery_run(run_identity)
    return append_event(
        "setup_blocked",
        fault_id=fault_id,
        requested_at=requested_at,
        identity=identity,
        run_identity=run_identity,
        failure_stage=failure_stage,
        failure_reason=str(failure) or type(failure).__name__,
        cleanup_requested_at=cleanup["cleanup_requested_at"],
        cleanup_confirmed_at=cleanup["cleanup_confirmed_at"],
        cleanup_error=cleanup["cleanup_error"],
        database_volume=cleanup["database_volume"],
        after=cleanup["after"],
    )


def reject_terminal_evidence_directory() -> None:
    events = load_verified_events()
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
            after=after,
        )
    except BaseException as failure:
        blocked = append_setup_blocked(
            fault_id=validated_fault,
            requested_at=requested_at,
            identity=identity,
            run_identity=run_identity,
            failure_stage=failure_stage,
            failure=failure,
        )
        detail = (
            f"fresh-up setup_blocked at {failure_stage}: "
            f"{blocked['failure_reason']}"
        )
        if blocked.get("cleanup_error"):
            detail += f"; cleanup error: {blocked['cleanup_error']}"
        raise StackError(detail) from failure
    print(json.dumps(after, ensure_ascii=False, indent=2))


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
    else:
        start_fault_service(args.service, args.fault_id)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except StackError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
