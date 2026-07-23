#!/usr/bin/env python3
from __future__ import annotations

import argparse
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


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def harness_identity() -> dict[str, dict[str, str]]:
    paths = {
        "controller": Path(__file__).resolve(),
        "compose_file": COMPOSE_FILE.resolve(),
        "compose_override": COMPOSE_OVERRIDE.resolve(),
        "finalizer": Path(__file__).with_name("finalize_v1.py").resolve(),
    }
    return {
        key: {"path": str(path), "sha256": sha256_file(path)}
        for key, path in paths.items()
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


def compose_environment() -> dict[str, str]:
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


def compose(*args: str) -> str:
    environment = compose_environment()
    return command(
        "docker",
        "compose",
        "--env-file",
        environment["FORWIN_RECOVERY_ENV_FILE"],
        "--project-name",
        PROJECT,
        "--file",
        str(COMPOSE_FILE),
        "--file",
        str(COMPOSE_OVERRIDE),
        "--profile",
        "publisher",
        *args,
        env=environment,
    )


def compose_process(*args: str) -> subprocess.CompletedProcess[str]:
    environment = compose_environment()
    return subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            environment["FORWIN_RECOVERY_ENV_FILE"],
            "--project-name",
            PROJECT,
            "--file",
            str(COMPOSE_FILE),
            "--file",
            str(COMPOSE_OVERRIDE),
            "--profile",
            "publisher",
            *args,
        ],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def validate_isolated_compose_config(
    payload: dict[str, Any],
    *,
    identity: dict[str, Any],
) -> None:
    if payload.get("name") != PROJECT:
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


def assert_isolated_compose(identity: dict[str, Any]) -> None:
    completed = compose_process("config", "--format", "json")
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
    validate_isolated_compose_config(payload, identity=identity)


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


def assert_frozen() -> dict[str, Any]:
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
    local_harness = harness_identity()
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


def compose_container_id(service: str) -> str:
    return compose("ps", "--all", "--quiet", service)


def inspect_service(service: str) -> dict[str, Any]:
    container_id = compose_container_id(service)
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
    if actual_project != PROJECT or actual_service != service:
        raise StackError(
            f"refusing container {container_id}: labels identify "
            f"{actual_project}/{actual_service}, expected {PROJECT}/{service}"
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


def functional_probe(service: str) -> dict[str, Any]:
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
    completed = compose_process(*command_args)
    output = _completed_output(completed)
    if completed.returncode:
        raise StackError(f"functional probe failed for {service}: {output}")
    return {
        "passed": True,
        "exit_code": 0,
        "output_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
    }


def stack_snapshot(*, probe: bool = False) -> dict[str, Any]:
    services = {service: inspect_service(service) for service in SERVICES}
    if probe:
        for service in SERVICES:
            services[service]["probe"] = functional_probe(service)
    return {
        "observed_at": now(),
        "services": services,
    }


def append_event(action: str, **payload: Any) -> dict[str, Any]:
    path = events_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = load_verified_events()
    event = {
        "schema_version": 1,
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


def wait_service(service: str, timeout_seconds: int = 240) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last = inspect_service(service)
    while time.monotonic() < deadline:
        last = inspect_service(service)
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
    identity = assert_frozen()
    assert_isolated_compose(identity)
    print(json.dumps({"ok": True, **identity}, ensure_ascii=False, indent=2))


def fresh_up() -> None:
    identity = assert_frozen()
    assert_isolated_compose(identity)
    require_new_evidence_run()
    before = stack_snapshot()
    append_event("fresh_up_started", identity=identity, before=before)
    compose("down", "--volumes", "--remove-orphans")
    compose("up", "--detach", "postgres", "qdrant", "minio")
    wait_service("postgres")
    wait_service("qdrant")
    wait_service("minio")
    compose("run", "--rm", "--no-deps", "forwin", "alembic", "upgrade", "head")
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
    after = stack_snapshot(probe=True)
    append_event("fresh_up_completed", identity=identity, after=after)
    print(json.dumps(after, ensure_ascii=False, indent=2))


def v1_up() -> None:
    identity = assert_frozen()
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


def destroy() -> None:
    identity = assert_frozen()
    assert_isolated_compose(identity)
    before = stack_snapshot()
    compose("down", "--volumes", "--remove-orphans")
    after = stack_snapshot()
    existing = load_verified_events()
    v1_starts = [
        event
        for event in existing
        if event.get("action") == "v1_fresh_up_started"
    ]
    run_identity = (
        {"run_id": str(v1_starts[0].get("run_id") or "")}
        if len(v1_starts) == 1
        else {}
    )
    append_event(
        "destroyed",
        **run_identity,
        identity=identity,
        before=before,
        after=after,
    )
    print(json.dumps(after, ensure_ascii=False, indent=2))


def stop_fault_service(service: str, fault_id: str) -> None:
    if service not in FAULT_SERVICES:
        raise StackError(f"service is not an allowed fault boundary: {service}")
    identity = assert_frozen()
    assert_isolated_compose(identity)
    before = inspect_service(service)
    if not before.get("running"):
        raise StackError(f"service {service} is not running before fault injection")
    fault_time = now()
    compose("stop", "--timeout", "10", service)
    after = inspect_service(service)
    if after.get("running"):
        raise StackError(f"service {service} is still running after stop")
    event = append_event(
        "fault_service_stopped",
        fault_id=fault_id,
        service=service,
        fault_time=fault_time,
        identity=identity,
        before=before,
        after=after,
    )
    print(json.dumps(event, ensure_ascii=False, indent=2))


def start_fault_service(service: str, fault_id: str) -> None:
    if service not in FAULT_SERVICES:
        raise StackError(f"service is not an allowed recovery boundary: {service}")
    identity = assert_frozen()
    assert_isolated_compose(identity)
    before = inspect_service(service)
    if before.get("running"):
        raise StackError(f"service {service} is already running before recovery")
    recovery_time = now()
    if service in CRASH_SERVICES:
        command(
            "docker",
            "container",
            "update",
            "--restart=unless-stopped",
            str(before["container_id"]),
        )
    compose("start", service)
    ready = wait_service(service)
    ready["probe"] = functional_probe(service)
    event = append_event(
        "fault_service_recovered",
        fault_id=fault_id,
        service=service,
        recovery_time=recovery_time,
        identity=identity,
        before=before,
        after=ready,
    )
    print(json.dumps(event, ensure_ascii=False, indent=2))


def kill_fault_service(service: str, fault_id: str) -> None:
    if service not in CRASH_SERVICES:
        raise StackError(f"service is not an allowed crash boundary: {service}")
    identity = assert_frozen()
    assert_isolated_compose(identity)
    before = inspect_service(service)
    if not before.get("running"):
        raise StackError(f"service {service} is not running before crash injection")
    container_id = str(before["container_id"])
    crash_time = now()
    command("docker", "container", "update", "--restart=no", container_id)
    command("docker", "container", "kill", "--signal=KILL", container_id)
    after = inspect_service(service)
    if after.get("running"):
        raise StackError(f"service {service} is still running after SIGKILL")
    event = append_event(
        "fault_service_killed",
        fault_id=fault_id,
        service=service,
        crash_time=crash_time,
        signal="SIGKILL",
        identity=identity,
        before=before,
        after=after,
    )
    print(json.dumps(event, ensure_ascii=False, indent=2))


def snapshot(label: str) -> None:
    identity = assert_frozen()
    assert_isolated_compose(identity)
    state = stack_snapshot()
    event = append_event("snapshot", label=label, identity=identity, state=state)
    print(json.dumps(event, ensure_ascii=False, indent=2))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Control only the isolated ForWin v5 live-recovery stack."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("config")
    commands.add_parser("fresh-up")
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
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "config":
        validate_config()
    elif args.command == "fresh-up":
        fresh_up()
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
    else:
        start_fault_service(args.service, args.fault_id)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except StackError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
