#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import ipaddress
import importlib.util
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
ARTIFACT_DIR = Path(__file__).resolve().parent
CONTROLLER_PATH = ARTIFACT_DIR / "recovery_stack.py"
EVALUATOR_PATH = ARTIFACT_DIR / "recovery_evidence.py"
FINALIZER_PATH = ARTIFACT_DIR / "finalize_recovery.py"
EVENT_LOG_NAME = "stack-events.jsonl"
REPORT_NAME = "fault-report.json"
GENESIS_STAGES = (
    "brief",
    "world",
    "map",
    "story_engine",
    "book_blueprint",
    "bootstrap",
)
FAULT_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,128}")
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")


class RunnerError(RuntimeError):
    pass


class SetupBlocked(RunnerError):
    pass


def load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RunnerError(f"cannot load recovery helper: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise RunnerError(f"value is not JSON-compatible: {exc}") from exc


def stable_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def atomic_write_new(path: Path, body: str) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.tmp-",
        dir=path.parent,
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary_path, path)
        except FileExistsError as exc:
            raise RunnerError(f"evidence artifact already exists: {path}") from exc
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def atomic_write_json_new(path: Path, payload: Mapping[str, Any]) -> None:
    atomic_write_new(
        path,
        json.dumps(
            dict(payload),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n",
    )


def validate_fault_id(fault_id: str) -> str:
    value = str(fault_id or "")
    if FAULT_ID_PATTERN.fullmatch(value) is None:
        raise RunnerError(
            "fault identity must be 1-128 ASCII letters, digits, dot, "
            "underscore, or hyphen"
        )
    return value


def required_url(value: str, label: str) -> str:
    normalized = str(value or "").strip().rstrip("/")
    parsed = urllib.parse.urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RunnerError(f"{label} must be an absolute HTTP(S) URL")
    return normalized


def require_client_endpoint(
    client: Any,
    *,
    attribute: str,
    expected_url: str,
    label: str,
) -> None:
    observed = getattr(client, attribute, None)
    if not isinstance(observed, str) or not observed.strip():
        raise SetupBlocked(f"{label} client endpoint identity is missing")
    if required_url(observed, label) != required_url(expected_url, label):
        raise SetupBlocked(
            f"{label} client endpoint does not match the bound configuration"
        )


def _loopback_http_url(
    value: str,
    *,
    label: str,
    expected_path: str,
) -> dict[str, Any]:
    normalized = str(value or "").strip().rstrip("/")
    parsed = urllib.parse.urlsplit(normalized)
    try:
        address = ipaddress.ip_address(parsed.hostname or "")
        port = int(parsed.port or 0)
    except (ValueError, TypeError) as exc:
        raise SetupBlocked(f"{label} endpoint is invalid") from exc
    if (
        parsed.scheme != "http"
        or not address.is_loopback
        or not 1 <= port <= 65535
        or parsed.path != expected_path
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise SetupBlocked(
            f"{label} endpoint must be exact, credential-free, and loopback"
        )
    return {
        "scheme": parsed.scheme,
        "host": address.compressed,
        "port": port,
        "endpoint_path": expected_path,
    }


def _loopback_database_url(value: str) -> dict[str, Any]:
    parsed = urllib.parse.urlsplit(normalize_database_url(value))
    try:
        address = ipaddress.ip_address(parsed.hostname or "")
        port = int(parsed.port or 0)
    except (ValueError, TypeError) as exc:
        raise SetupBlocked("database endpoint is invalid") from exc
    database = parsed.path.removeprefix("/")
    if (
        parsed.scheme not in {"postgresql", "postgres"}
        or not address.is_loopback
        or not 1 <= port <= 65535
        or database != "forwin"
        or parsed.fragment
    ):
        raise SetupBlocked(
            "database endpoint must be the exact loopback recovery database"
        )
    return {
        "scheme": "postgresql",
        "host": address.compressed,
        "port": port,
        "database": database,
    }


_ENDPOINT_SERVICE_KEYS = {
    "scheme",
    "host",
    "port",
    "endpoint_path",
    "health_path",
    "health_status",
    "service",
    "container_port",
    "container_id",
    "image_id",
}
_ENDPOINT_DATABASE_KEYS = {
    "scheme",
    "host",
    "port",
    "database",
    "service",
    "container_port",
    "container_id",
    "image_id",
}
_ENDPOINT_SENTINEL_KEYS = {
    "table",
    "sentinel_id",
    "run_id",
    "fault_id",
    "source_sha",
}
_ENDPOINT_IDENTITY_CORE_KEYS = {
    "schema_version",
    "fault_id",
    "run_id",
    "source_sha",
    "source_tree",
    "project_name",
    "candidate_manifest_sha256",
    "candidate_identity_sha256",
    "sentinel",
    "api",
    "mcp",
    "database",
    "identity_sha256",
}
_ENDPOINT_OPTIONAL_DEFINITIONS = {
    "qdrant": {
        "health_path": "/readyz",
        "service": "qdrant",
        "container_port": 6333,
    },
    "minio": {
        "health_path": "/minio/health/ready",
        "service": "minio",
        "container_port": 9000,
    },
}


def _validated_endpoint_identity(
    value: Any,
    *,
    fault_id: str,
    source_sha: str,
    api: Mapping[str, Any],
    mcp: Mapping[str, Any],
    database: Mapping[str, Any],
    optional_endpoints: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    expected_record_keys = _ENDPOINT_IDENTITY_CORE_KEYS | set(
        optional_endpoints
    )
    if not isinstance(value, Mapping) or set(value) != expected_record_keys:
        raise SetupBlocked("endpoint identity has an invalid field set")
    record = dict(value)
    if (
        record.get("schema_version") != 1
        or record.get("fault_id") != fault_id
        or record.get("source_sha") != source_sha
        or re.fullmatch(r"[0-9a-f]{32}", str(record.get("run_id") or ""))
        is None
        or record.get("project_name")
        != f"forwin-v5-recovery-{record.get('run_id')}"
        or re.fullmatch(
            r"[0-9a-f]{40}",
            str(record.get("source_tree") or ""),
        )
        is None
    ):
        raise SetupBlocked("endpoint identity run or source identity drifted")
    for field in (
        "candidate_manifest_sha256",
        "candidate_identity_sha256",
        "identity_sha256",
    ):
        canonical_digest(record.get(field), f"endpoint identity {field}")
    sentinel = record.get("sentinel")
    if (
        not isinstance(sentinel, Mapping)
        or set(sentinel) != _ENDPOINT_SENTINEL_KEYS
        or sentinel.get("table") != "forwin_recovery_run_sentinel"
        or sentinel.get("run_id") != record["run_id"]
        or sentinel.get("fault_id") != fault_id
        or sentinel.get("source_sha") != source_sha
    ):
        raise SetupBlocked("endpoint identity sentinel drifted")
    canonical_digest(
        sentinel.get("sentinel_id"),
        "endpoint identity sentinel_id",
    )
    expected_endpoints = {
        "api": {
            **api,
            "health_path": "/health",
            "service": "forwin",
            "container_port": 8899,
        },
        "mcp": {
            **mcp,
            "health_path": "/health",
            "service": "forwin-mcp",
            "container_port": 8896,
        },
        "database": {
            **database,
            "service": "postgres",
            "container_port": 5432,
        },
    }
    for name, endpoint in optional_endpoints.items():
        expected_endpoints[name] = {
            **endpoint,
            **_ENDPOINT_OPTIONAL_DEFINITIONS[name],
        }
    for name in ("api", "mcp", "database", *optional_endpoints):
        endpoint = record.get(name)
        expected_keys = (
            _ENDPOINT_DATABASE_KEYS
            if name == "database"
            else _ENDPOINT_SERVICE_KEYS
        )
        if not isinstance(endpoint, Mapping) or set(endpoint) != expected_keys:
            raise SetupBlocked(f"{name} endpoint identity field set drifted")
        if any(
            endpoint.get(field) != expected
            for field, expected in expected_endpoints[name].items()
        ):
            raise SetupBlocked(f"{name} endpoint does not match the active run")
        if (
            not str(endpoint.get("container_id") or "")
            or not str(endpoint.get("image_id") or "").startswith("sha256:")
        ):
            raise SetupBlocked(f"{name} container identity is incomplete")
        if name != "database" and endpoint.get("health_status") != 200:
            raise SetupBlocked(f"{name} health probe identity drifted")
    unsigned = {
        key: nested
        for key, nested in record.items()
        if key != "identity_sha256"
    }
    if record["identity_sha256"] != stable_hash(unsigned):
        raise SetupBlocked("endpoint identity hash drifted")
    return record


def bind_recovery_endpoints(
    *,
    controller: Any,
    fault_id: str,
    source_sha: str,
    api_url: str,
    mcp_url: str,
    database_url: str,
    sentinel_reader: Callable[[], Mapping[str, Any]],
    qdrant_url: str | None = None,
    minio_url: str | None = None,
) -> dict[str, Any]:
    validated_fault = validate_fault_id(fault_id)
    if SHA_PATTERN.fullmatch(str(source_sha or "")) is None:
        raise SetupBlocked("endpoint source SHA is not canonical")
    api = _loopback_http_url(
        api_url,
        label="API",
        expected_path="",
    )
    mcp = _loopback_http_url(
        mcp_url,
        label="MCP",
        expected_path="/mcp",
    )
    database = _loopback_database_url(database_url)
    optional_urls = {
        name: value
        for name, value in {
            "qdrant": qdrant_url,
            "minio": minio_url,
        }.items()
        if value is not None
    }
    optional_endpoints = {
        name: _loopback_http_url(
            value,
            label=name.capitalize(),
            expected_path="",
        )
        for name, value in optional_urls.items()
    }
    record = _validated_endpoint_identity(
        controller.bind_endpoints(
            fault_id=validated_fault,
            api_url=api_url,
            mcp_url=mcp_url,
            database_host=str(database["host"]),
            database_port=int(database["port"]),
            database_name=str(database["database"]),
            **{
                f"{name}_url": value
                for name, value in optional_urls.items()
            },
        ),
        fault_id=validated_fault,
        source_sha=source_sha,
        api=api,
        mcp=mcp,
        database=database,
        optional_endpoints=optional_endpoints,
    )
    observed_sentinel = sentinel_reader()
    if (
        not isinstance(observed_sentinel, Mapping)
        or dict(observed_sentinel) != record["sentinel"]
    ):
        raise SetupBlocked(
            "database endpoint did not return the active-run sentinel"
        )
    return record


def required_text(value: Any, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise SetupBlocked(f"{field} is empty")
    return normalized


def canonical_digest(value: Any, field: str) -> str:
    normalized = required_text(value, field)
    if re.fullmatch(r"[0-9a-f]{64}", normalized) is None:
        raise SetupBlocked(f"{field} is not a canonical SHA-256 digest")
    return normalized


def normalize_database_url(database_url: str) -> str:
    value = str(database_url or "").strip()
    if value.startswith("postgresql+psycopg://"):
        return "postgresql://" + value.partition("://")[2]
    if value.startswith("postgres+psycopg://"):
        return "postgres://" + value.partition("://")[2]
    return value


def psycopg_connect(database_url: str) -> Any:
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(
        normalize_database_url(database_url),
        autocommit=True,
        row_factory=dict_row,
    )


@dataclass(frozen=True, slots=True)
class CandidateIdentity:
    manifest_path: Path
    source_sha: str
    evaluator_path: Path
    evaluator_sha256: str


def candidate_identity(candidate_manifest: Path) -> CandidateIdentity:
    path = candidate_manifest.expanduser().resolve()
    if not path.is_file():
        raise RunnerError(f"candidate manifest is missing: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RunnerError(f"candidate manifest is invalid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise RunnerError("candidate manifest must contain an object")
    source_sha = str((payload.get("source") or {}).get("sha") or "")
    if SHA_PATTERN.fullmatch(source_sha) is None:
        raise RunnerError("candidate manifest source SHA is not canonical")
    evaluator_path = EVALUATOR_PATH.resolve()
    return CandidateIdentity(
        manifest_path=path,
        source_sha=source_sha,
        evaluator_path=evaluator_path,
        evaluator_sha256=sha256_file(evaluator_path),
    )


@dataclass(frozen=True, slots=True)
class ProjectFixture:
    project_id: str


@dataclass(frozen=True, slots=True)
class TaskFixture:
    project_id: str
    task_id: str


def decode_mcp_result(result: Any) -> dict[str, Any]:
    if isinstance(result, Mapping):
        return dict(result)
    payload = getattr(result, "structured_content", None)
    if isinstance(payload, Mapping):
        return dict(payload)
    raise SetupBlocked("MCP tool returned no structured JSON object")


class OneChapterLifecycle:
    def __init__(
        self,
        *,
        mcp_url: str,
        fault_id: str,
        call_tool: Callable[[str, dict[str, Any]], Any] | None = None,
    ) -> None:
        self.mcp_url = required_url(mcp_url, "MCP URL")
        self.fault_id = validate_fault_id(fault_id)
        self.call_tool = call_tool

    async def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if self.call_tool is not None:
            result = self.call_tool(name, arguments)
            if hasattr(result, "__await__"):
                result = await result
            return decode_mcp_result(result)
        from fastmcp import Client

        async with Client(self.mcp_url) as client:
            return decode_mcp_result(await client.call_tool(name, arguments))

    async def create_genesis_project(self) -> ProjectFixture:
        created = await self._call(
            "project_create",
            {
                "title": f"Recovery Evidence Fixture {self.fault_id}",
                "premise": (
                    "A compact one-chapter systems fixture about a team "
                    "preserving a public archive through an ordinary handoff."
                ),
                "genre": "systems fiction",
                "setting_summary": (
                    "A generic archive workspace with no production book, "
                    "model, or incident-specific content."
                ),
                "target_total_chapters": 1,
            },
        )
        project_payload = created.get("project")
        if not isinstance(project_payload, Mapping):
            raise SetupBlocked("project_create returned no project object")
        project_id = str(project_payload.get("id") or "")
        if not project_id:
            raise SetupBlocked("project_create returned no project identity")
        await self._call("project_get", {"project_id": project_id})
        for stage in GENESIS_STAGES:
            await self._call("genesis_get", {"project_id": project_id})
            await self._require_ok(
                "genesis_stage_generate",
                {"project_id": project_id, "stage_key": stage},
            )
            await self._call("genesis_get", {"project_id": project_id})
            await self._require_ok(
                "genesis_stage_lock",
                {"project_id": project_id, "stage_key": stage},
            )
        genesis = await self._call("genesis_get", {"project_id": project_id})
        project = await self._call("project_get", {"project_id": project_id})
        if not bool(
            genesis.get("can_start_writing")
            or project.get("can_start_writing")
        ):
            raise SetupBlocked("six locked Genesis stages are not writing-ready")
        return ProjectFixture(project_id=project_id)

    async def start_writing(self, project_id: str) -> TaskFixture:
        await self._call("project_get", {"project_id": project_id})
        active = await self._call(
            "task_active_generation_check",
            {"project_id": project_id},
        )
        if bool(active.get("has_active_generation_task")) or int(
            active.get("active_count") or 0
        ):
            raise SetupBlocked("project has an active generation task")
        started = await self._require_ok(
            "project_start_writing",
            {
                "project_id": project_id,
                "auto_continue": False,
                "max_chapters": 1,
            },
        )
        task = (
            started.get("task")
            if isinstance(started.get("task"), Mapping)
            else {}
        )
        task_id = str(task.get("task_id") or "")
        if not task_id:
            raise SetupBlocked("project_start_writing returned no task identity")
        return TaskFixture(project_id=project_id, task_id=task_id)

    async def _require_ok(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        payload = await self._call(name, arguments)
        if payload.get("ok") is False:
            raise SetupBlocked(f"{name} failed: {payload.get('message') or payload}")
        return payload


def http_json(
    method: str,
    url: str,
    *,
    query: Mapping[str, Any] | None = None,
    json_body: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    target = url
    if query:
        encoded = urllib.parse.urlencode(
            {
                key: (
                    str(value).lower() if isinstance(value, bool) else value
                )
                for key, value in query.items()
            }
        )
        target = f"{target}?{encoded}"
    body = (
        json.dumps(dict(json_body), ensure_ascii=False).encode("utf-8")
        if json_body is not None
        else None
    )
    request = urllib.request.Request(
        target,
        data=body,
        method=method,
        headers={
            "Content-Type": "application/json",
            **{
                str(key): str(value)
                for key, value in (headers or {}).items()
            },
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
        raise RunnerError(f"HTTP request failed: {method} {target}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RunnerError(f"HTTP response is not an object: {method} {target}")
    return payload


class RecoveryController:
    def __init__(
        self,
        *,
        candidate_manifest: Path,
        evidence_dir: Path,
        execute: Callable[..., Any] = subprocess.run,
        python_executable: str = sys.executable,
    ) -> None:
        self.candidate_manifest = candidate_manifest.resolve()
        self.evidence_dir = evidence_dir.resolve()
        self.execute = execute
        self.python_executable = python_executable

    @property
    def event_log_path(self) -> Path:
        return self.evidence_dir / EVENT_LOG_NAME

    def _command(self, *arguments: str) -> tuple[Any, list[str]]:
        environment = dict(os.environ)
        environment.update(
            {
                "FORWIN_RECOVERY_CANDIDATE_MANIFEST": str(
                    self.candidate_manifest
                ),
                "FORWIN_RECOVERY_EVIDENCE_DIR": str(self.evidence_dir),
            }
        )
        command = [
            self.python_executable,
            str(CONTROLLER_PATH),
            *arguments,
        ]
        completed = self.execute(
            command,
            cwd=ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        return completed, command

    def _run(self, *arguments: str) -> dict[str, Any]:
        completed, command = self._command(*arguments)
        if int(completed.returncode or 0):
            detail = str(completed.stderr or completed.stdout or "").strip()
            raise RunnerError(
                f"recovery controller failed ({' '.join(arguments)}): {detail}"
            )
        output = str(completed.stdout or "").strip()
        if not output:
            return {}
        try:
            payload = json.loads(output)
        except json.JSONDecodeError:
            return {"output": output}
        return payload if isinstance(payload, dict) else {"output": payload}

    def fresh_up(self, fault_id: str) -> dict[str, Any]:
        validated = validate_fault_id(fault_id)
        try:
            return self._run("fresh-up", "--fault-id", validated)
        except BaseException:
            try:
                self.interrupt_cleanup(validated)
            except BaseException:
                pass
            raise

    def kill(self, service: str, fault_id: str) -> dict[str, Any]:
        return self._run("kill", service, "--fault-id", validate_fault_id(fault_id))

    def stop(self, service: str, fault_id: str) -> dict[str, Any]:
        return self._run("stop", service, "--fault-id", validate_fault_id(fault_id))

    def start(self, service: str, fault_id: str) -> dict[str, Any]:
        return self._run("start", service, "--fault-id", validate_fault_id(fault_id))

    def setup_hold(
        self,
        service: str,
        fault_id: str,
        hold_id: str,
        *,
        fault_kind: str,
        purpose: str = "auxiliary",
    ) -> dict[str, Any]:
        return self._run(
            "setup-hold",
            service,
            "--fault-id",
            validate_fault_id(fault_id),
            "--hold-id",
            validate_fault_id(hold_id),
            "--fault-kind",
            str(fault_kind),
            "--purpose",
            str(purpose),
        )

    def setup_release(
        self,
        service: str,
        fault_id: str,
        hold_id: str,
    ) -> dict[str, Any]:
        return self._run(
            "setup-release",
            service,
            "--fault-id",
            validate_fault_id(fault_id),
            "--hold-id",
            validate_fault_id(hold_id),
        )

    def setup_discard(
        self,
        service: str,
        fault_id: str,
        hold_id: str,
    ) -> dict[str, Any]:
        return self._run(
            "setup-discard",
            service,
            "--fault-id",
            validate_fault_id(fault_id),
            "--hold-id",
            validate_fault_id(hold_id),
        )

    def bind_endpoints(
        self,
        *,
        fault_id: str,
        api_url: str,
        mcp_url: str,
        database_host: str,
        database_port: int,
        database_name: str,
        qdrant_url: str | None = None,
        minio_url: str | None = None,
    ) -> dict[str, Any]:
        arguments = [
            "bind-endpoints",
            "--fault-id",
            validate_fault_id(fault_id),
            "--api-url",
            str(api_url),
            "--mcp-url",
            str(mcp_url),
            "--database-host",
            str(database_host),
            "--database-port",
            str(database_port),
            "--database-name",
            str(database_name),
        ]
        for option, value in (
            ("--qdrant-url", qdrant_url),
            ("--minio-url", minio_url),
        ):
            if value is not None:
                arguments.extend((option, str(value)))
        return self._run(*arguments)

    def mark(
        self,
        fault_kind: str,
        phase: str,
        fault_id: str,
    ) -> dict[str, Any]:
        return self._run(
            "mark",
            str(fault_kind),
            str(phase),
            "--fault-id",
            validate_fault_id(fault_id),
        )

    def file_inventory(
        self,
        service: str,
        fault_id: str,
        root: str,
    ) -> dict[str, Any]:
        return self._run(
            "file-inventory",
            str(service),
            "--fault-id",
            validate_fault_id(fault_id),
            "--root",
            str(root),
        )

    def abort(self, fault_id: str, stage: str, reason: str) -> dict[str, Any]:
        completed, _command = self._command(
            "abort",
            "--fault-id",
            validate_fault_id(fault_id),
            "--stage",
            validate_fault_id(stage),
            "--reason",
            str(reason),
        )
        if int(completed.returncode or 0) == 0:
            raise RunnerError("recovery controller abort unexpectedly succeeded")
        detail = str(completed.stderr or completed.stdout or "").strip()
        if "recovery abort:" not in detail:
            raise RunnerError(f"recovery controller abort failed: {detail}")
        return {"output": detail}

    def interrupt_cleanup(self, fault_id: str) -> dict[str, Any]:
        return self._run(
            "interrupt-cleanup",
            "--fault-id",
            validate_fault_id(fault_id),
        )

    def snapshot(self, label: str) -> dict[str, Any]:
        return self._run("snapshot", "--label", str(label))

    def destroy(self) -> dict[str, Any]:
        return self._run("destroy")


class EvidenceWriter:
    def __init__(
        self,
        *,
        evidence_dir: Path,
        evaluator: Any | None = None,
        report_validator: Callable[..., list[str]] | None = None,
        finalizer: Any | None = None,
    ) -> None:
        self.evidence_dir = evidence_dir.resolve()
        self.evaluator = evaluator or load_module(
            f"recovery_runner_evaluator_{secrets.token_hex(4)}",
            EVALUATOR_PATH,
        )
        self.finalizer = finalizer or load_module(
            f"recovery_runner_finalizer_{secrets.token_hex(4)}",
            FINALIZER_PATH,
        )
        self.report_validator = (
            report_validator or self.finalizer.fault_report_violations
        )

    def write_pass_report(
        self,
        *,
        fault_kind: str,
        fault_id: str,
        source_sha: str,
        snapshots: Mapping[str, dict[str, Any]],
        event_log_path: Path,
        supplemental_artifacts: Mapping[
            str, Mapping[str, Any]
        ] | None = None,
    ) -> Path:
        in_memory = {stage: snapshots[stage] for stage in self.evaluator.STAGES}
        self._validate_snapshots(fault_kind, in_memory)
        assertions = self.evaluator.derive_assertions(fault_kind, in_memory)
        assertion_violations = self.evaluator.assertion_violations(
            fault_kind,
            assertions,
        )
        if assertion_violations:
            raise RunnerError(
                "in-memory recovery assertions failed: "
                + "; ".join(assertion_violations)
            )

        artifacts: list[dict[str, str]] = []
        for stage in self.evaluator.STAGES:
            path = self.evidence_dir / f"{stage}.json"
            atomic_write_json_new(path, in_memory[stage])
            artifacts.append(
                {
                    "stage": stage,
                    "path": str(path),
                    "sha256": sha256_file(path),
                }
            )

        reopened: dict[str, dict[str, Any]] = {}
        for artifact in artifacts:
            path = Path(artifact["path"])
            if sha256_file(path) != artifact["sha256"]:
                raise RunnerError(f"snapshot changed before reopen: {path}")
            reopened[artifact["stage"]] = self.evaluator.load_snapshot(path)
            if sha256_file(path) != artifact["sha256"]:
                raise RunnerError(f"snapshot changed while reopening: {path}")
        self._validate_snapshots(fault_kind, reopened)
        reopened_assertions = self.evaluator.derive_assertions(
            fault_kind,
            reopened,
        )
        if self.evaluator.stable_hash(
            reopened_assertions
        ) != self.evaluator.stable_hash(assertions):
            raise RunnerError("reopened evaluator mismatch")
        reopened_violations = self.evaluator.assertion_violations(
            fault_kind,
            reopened_assertions,
        )
        if reopened_violations:
            raise RunnerError(
                "reopened recovery assertions failed: "
                + "; ".join(reopened_violations)
            )

        event_path = event_log_path.resolve()
        if event_path.parent != self.evidence_dir:
            raise RunnerError("event log is not a direct child of evidence directory")
        events = self._load_events(event_path)
        fault_time, recovery_time = self._fault_times(
            fault_kind,
            fault_id,
            events,
        )
        supplemental_refs: list[dict[str, str]] = []
        for name, payload in sorted((supplemental_artifacts or {}).items()):
            relative_path = Path(name)
            if (
                relative_path.name != name
                or relative_path.suffix != ".json"
                or name
                in {
                    REPORT_NAME,
                    EVENT_LOG_NAME,
                    "before.json",
                    "during.json",
                    "after.json",
                }
            ):
                raise RunnerError(f"invalid supplemental evidence name: {name}")
            path = (self.evidence_dir / name).resolve()
            if path.parent != self.evidence_dir:
                raise RunnerError(
                    "supplemental evidence is not a direct child of "
                    "evidence directory"
                )
            atomic_write_json_new(path, payload)
            artifact_hash = sha256_file(path)
            reopened_payload = self._load_json(path)
            if sha256_file(path) != artifact_hash:
                raise RunnerError(
                    f"supplemental evidence changed while reopening: {path}"
                )
            if self.evaluator.stable_hash(
                reopened_payload
            ) != self.evaluator.stable_hash(dict(payload)):
                raise RunnerError(
                    f"reopened supplemental evidence mismatch: {path}"
                )
            supplemental_refs.append(
                {
                    "name": name,
                    "path": str(path),
                    "sha256": artifact_hash,
                }
            )
        evaluator_path = Path(self.evaluator.__file__).resolve()
        report = {
            "schema_version": 2,
            "fault_kind": fault_kind,
            "fault_id": fault_id,
            "source_sha": source_sha,
            "result": "pass",
            "fault_time": fault_time,
            "recovery_time": recovery_time,
            "expected": ["fault-local durable state follows the Task 1 contract"],
            "actual": ["read-only observations satisfy the Task 1 contract"],
            "replay_result": "pass",
            "assertions": reopened_assertions,
            "artifacts": artifacts,
            "event_log": {
                "path": str(event_path),
                "sha256": sha256_file(event_path),
                "event_count": len(events),
                "chain_head": str(events[-1]["event_sha256"]),
            },
            "evaluator": {
                "path": str(evaluator_path),
                "sha256": sha256_file(evaluator_path),
            },
            "supplemental_artifacts": supplemental_refs,
        }
        prewrite_violations = self.report_validator(
            report,
            source_sha=source_sha,
        )
        if prewrite_violations:
            raise RunnerError(
                "fault report failed prewrite validation: "
                + "; ".join(prewrite_violations)
            )
        report_path = self.evidence_dir / REPORT_NAME
        atomic_write_json_new(report_path, report)
        report_hash = sha256_file(report_path)
        reopened_report = self._load_json(report_path)
        if sha256_file(report_path) != report_hash:
            raise RunnerError("fault report changed while reopening")
        if self.evaluator.stable_hash(
            reopened_report
        ) != self.evaluator.stable_hash(report):
            raise RunnerError("reopened fault report mismatch")
        postwrite_violations = self.report_validator(
            reopened_report,
            source_sha=source_sha,
        )
        if postwrite_violations:
            raise RunnerError(
                "reopened fault report failed validation: "
                + "; ".join(postwrite_violations)
            )
        return report_path

    def write_setup_blocked(
        self,
        *,
        fault_kind: str,
        fault_id: str,
        source_sha: str,
        failure_stage: str,
        failure_reason: str,
        cleanup_errors: Sequence[str],
    ) -> Path:
        evaluator_path = Path(self.evaluator.__file__).resolve()
        payload = {
            "schema_version": 2,
            "fault_kind": fault_kind,
            "fault_id": fault_id,
            "source_sha": source_sha,
            "result": "setup_blocked",
            "failure_stage": str(failure_stage or "setup"),
            "failure_reason": str(failure_reason or "setup blocked"),
            "cleanup_errors": [str(item) for item in cleanup_errors],
            "evaluator": {
                "path": str(evaluator_path),
                "sha256": sha256_file(evaluator_path),
            },
        }
        path = self.evidence_dir / REPORT_NAME
        atomic_write_json_new(path, payload)
        report_hash = sha256_file(path)
        reopened = self._load_json(path)
        if sha256_file(path) != report_hash:
            raise RunnerError("setup_blocked report changed while reopening")
        if reopened != payload:
            raise RunnerError("reopened setup_blocked report mismatch")
        return path

    def _validate_snapshots(
        self,
        fault_kind: str,
        snapshots: Mapping[str, dict[str, Any]],
    ) -> None:
        violations = self.evaluator.snapshot_violations(
            fault_kind,
            snapshots,
        )
        if violations:
            raise RunnerError(
                "snapshot contract failed: " + "; ".join(violations)
            )

    @staticmethod
    def _load_json(path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RunnerError(f"invalid JSON evidence: {path}") from exc
        if not isinstance(payload, dict):
            raise RunnerError(f"expected JSON object: {path}")
        return payload

    def _load_events(self, path: Path) -> list[dict[str, Any]]:
        try:
            return self.finalizer.load_verified_events(path)
        except Exception as exc:
            raise RunnerError(f"invalid controller event log: {exc}") from exc

    def _fault_times(
        self,
        fault_kind: str,
        fault_id: str,
        events: Sequence[Mapping[str, Any]],
    ) -> tuple[str, str]:
        contract = self.finalizer.SERVICE_FAULTS.get(fault_kind)
        service_fault = isinstance(contract, Mapping)
        action = (
            str(contract["fault_action"])
            if service_fault
            else "fault_marked"
        )
        recovery_action = (
            "fault_service_recovered"
            if service_fault
            else "recovery_marked"
        )
        time_field = (
            str(contract["fault_time_field"])
            if service_fault
            else "fault_time"
        )
        service = str(contract["service"]) if service_fault else ""
        faults = [
            event
            for event in events
            if event.get("fault_id") == fault_id
            and event.get("action") == action
            and (
                event.get("service") == service
                if service_fault
                else event.get("fault_kind") == fault_kind
            )
        ]
        recoveries = [
            event
            for event in events
            if event.get("fault_id") == fault_id
            and event.get("action") == recovery_action
            and (
                event.get("service") == service
                if service_fault
                else event.get("fault_kind") == fault_kind
            )
        ]
        if len(faults) != 1 or len(recoveries) != 1:
            raise RunnerError("controller event log has no unique fault/recovery pair")
        return (
            required_text(faults[0].get(time_field), time_field),
            required_text(recoveries[0].get("recovery_time"), "recovery_time"),
        )


__all__ = [
    "ARTIFACT_DIR",
    "CONTROLLER_PATH",
    "CandidateIdentity",
    "EVENT_LOG_NAME",
    "EVALUATOR_PATH",
    "EvidenceWriter",
    "FAULT_ID_PATTERN",
    "FINALIZER_PATH",
    "GENESIS_STAGES",
    "OneChapterLifecycle",
    "ProjectFixture",
    "REPORT_NAME",
    "ROOT",
    "RecoveryController",
    "RunnerError",
    "SHA_PATTERN",
    "SetupBlocked",
    "TaskFixture",
    "atomic_write_json_new",
    "atomic_write_new",
    "candidate_identity",
    "canonical_digest",
    "canonical_json",
    "decode_mcp_result",
    "http_json",
    "load_module",
    "normalize_database_url",
    "psycopg_connect",
    "required_text",
    "required_url",
    "sha256_file",
    "stable_hash",
    "validate_fault_id",
]
