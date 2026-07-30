#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import platform
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit


ROOT = Path(__file__).resolve().parents[2]
COLLECTOR_VERSION = 6
MANIFEST_SCHEMA_VERSION = 3
MATRIX_AUDIT_SCHEMA_VERSION = 4
DEFAULT_DEPENDENCY_IMAGES = {
    "postgres": "postgres:16-alpine",
    "qdrant": "qdrant/qdrant:v1.17.1",
    "minio": "minio/minio:RELEASE.2025-09-07T16-13-09Z",
}
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
RELEASE_HARNESS_PATHS = (
    (ROOT / "docker-compose.yml").resolve(),
    Path(__file__).with_name("candidate_mcp_call.py").resolve(),
    Path(__file__).resolve(),
    Path(__file__).with_name("docker-compose.recovery.yml").resolve(),
    Path(__file__).with_name("finalize_matrix.py").resolve(),
    Path(__file__).with_name("finalize_recovery.py").resolve(),
    Path(__file__).with_name("finalize_smoke.py").resolve(),
    Path(__file__).with_name("finalize_v1.py").resolve(),
    Path(__file__).with_name("generation_projection_recovery.py").resolve(),
    Path(__file__).with_name("l200_evidence.py").resolve(),
    Path(__file__).with_name("minio_recovery.py").resolve(),
    Path(__file__).with_name("publisher_recovery.py").resolve(),
    Path(__file__).with_name("recovery_evidence.py").resolve(),
    Path(__file__).with_name("release_http_auth.py").resolve(),
    Path(__file__).with_name("recovery_runner_common.py").resolve(),
    Path(__file__).with_name("recovery_stack.py").resolve(),
    Path(__file__).with_name("release-source-files.txt").resolve(),
    Path(__file__).with_name("run_rc_gates.py").resolve(),
    Path(__file__).with_name("smoke_lifecycle.py").resolve(),
)
RECOVERY_RUNNER_PATHS = {
    "generation_worker_postcommit_crash": Path(__file__).with_name(
        "generation_projection_recovery.py"
    ).resolve(),
    "generation_worker_precommit_crash": Path(__file__).with_name(
        "generation_projection_recovery.py"
    ).resolve(),
    "minio_post_canon_unavailable": Path(__file__).with_name(
        "minio_recovery.py"
    ).resolve(),
    "minio_pre_canon_unavailable": Path(__file__).with_name(
        "minio_recovery.py"
    ).resolve(),
    "projection_consumer_unavailable": Path(__file__).with_name(
        "generation_projection_recovery.py"
    ).resolve(),
    "publisher_account_risk": Path(__file__).with_name(
        "publisher_recovery.py"
    ).resolve(),
    "publisher_backend_unavailable": Path(__file__).with_name(
        "publisher_recovery.py"
    ).resolve(),
    "publisher_browser_unavailable": Path(__file__).with_name(
        "publisher_recovery.py"
    ).resolve(),
    "publisher_captcha": Path(__file__).with_name(
        "publisher_recovery.py"
    ).resolve(),
    "publisher_mfa": Path(__file__).with_name(
        "publisher_recovery.py"
    ).resolve(),
    "qdrant_unavailable": Path(__file__).with_name(
        "generation_projection_recovery.py"
    ).resolve(),
}
MATRIX_SUCCESSOR_ALLOWED_PREFIXES = (
    ".artifacts/rc-candidate/",
    "forwin/application/publisher/",
    "forwin/publisher_runtime/",
    "forwin/publishers/",
)
MATRIX_SUCCESSOR_ALLOWED_FILES = frozenset(
    {
        "docker-compose.yml",
        "forwin/api_schema/__init__.py",
        "forwin/api_schema/publisher.py",
        "forwin/cli.py",
        "forwin/http/adapters/api_publisher_routes.py",
        "forwin/http/automation.py",
        "forwin/http/routes.py",
        "forwin/models/base.py",
        "scripts/check_publisher_browser_heartbeat.py",
        "tests/test_docker_compose_profiles.py",
        "tests/test_http_app_factory.py",
        "tests/test_publisher_attempt_leases.py",
        "tests/test_publisher_browser_healthcheck.py",
        "tests/test_publisher_receipts.py",
        "tests/test_publisher_risk_pause.py",
        "tests/test_publisher_runtime_comment_sync.py",
        "tests/test_publisher_runtime_covers.py",
        "tests/test_publisher_worker_cli.py",
        "tests/test_runtime_worker_roles.py",
        "tests/test_v5_recovery_schema.py",
    }
)

MODEL_ENV_KEYS = (
    "MINIMAX_API_KEY",
    "MINIMAX_BASE_URL",
    "MINIMAX_MODEL",
    "KIMI_API_KEY",
    "KIMI_BASE_URL",
    "KIMI_MODEL",
    "MOONSHOT_API_KEY",
    "MOONSHOT_BASE_URL",
    "MOONSHOT_MODEL",
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_BASE_URL",
    "DEEPSEEK_MODEL",
    "FORWIN_CODEX_ENABLED",
    "FORWIN_CODEX_BRIDGE_URL",
    "FORWIN_CODEX_DEFAULT_MODEL",
    "FORWIN_CODEX_MODEL",
    "FORWIN_EMBEDDING_BACKEND",
    "FORWIN_EMBEDDING_BASE_URL",
    "FORWIN_EMBEDDING_API_KEY",
    "FORWIN_EMBEDDING_MODEL",
    "FORWIN_EMBEDDING_DIMS",
    "FORWIN_EMBEDDING_REQUIRED",
)

EXPECTED_RUNTIME_SERVICES = frozenset(
    {
        "forwin",
        "generation-worker",
        "outbox-worker",
        "forwin-mcp",
        "publisher-worker",
    }
)
MODEL_EXECUTION_SERVICES = frozenset(
    {"forwin", "generation-worker", "outbox-worker"}
)
MODEL_PASSIVE_SERVICES = EXPECTED_RUNTIME_SERVICES - MODEL_EXECUTION_SERVICES
MODEL_ROUTING_GROUPS = {
    "model_execution": MODEL_EXECUTION_SERVICES,
    "passive": MODEL_PASSIVE_SERVICES,
}

ROUTING_DEFAULTS = {
    "MINIMAX_BASE_URL": "https://api.minimaxi.com/v1",
    "MINIMAX_MODEL": "MiniMax-M2.7",
    "MOONSHOT_BASE_URL": "https://api.moonshot.cn/v1",
    "MOONSHOT_MODEL": "kimi-k2.5",
    "DEEPSEEK_BASE_URL": "https://api.deepseek.com/v1",
    "DEEPSEEK_MODEL": "deepseek-chat",
    "FORWIN_CODEX_DEFAULT_MODEL": "gpt-5.3-codex-spark",
    "FORWIN_CODEX_BRIDGE_URL": "http://host.docker.internal:8897",
    "FORWIN_EMBEDDING_BACKEND": "gateway",
    "FORWIN_EMBEDDING_BASE_URL": "http://10.0.0.150:8080",
    "FORWIN_EMBEDDING_MODEL": "all-MiniLM-L6-v2",
    "FORWIN_EMBEDDING_DIMS": "384",
    "FORWIN_EMBEDDING_REQUIRED": "false",
}

MODEL_DEFAULT_NAMES = (
    "DEFAULT_MINIMAX_MODEL",
    "DEFAULT_MOONSHOT_MODEL",
    "DEFAULT_DEEPSEEK_MODEL",
    "DEFAULT_CODEX_MODEL",
    "DEFAULT_EMBEDDING_MODEL",
)

REPORT_SCHEMA_CLASSES = (
    "GateLedgerReportView",
    "CostLedgerReportView",
    "RuleProvenanceReportView",
)

RELEASE_GATE_STEPS = (
    "v1-fresh-schema",
    "v2-canon-recovery",
    "v3-spark-boundary",
    "v5-projection-publisher-recovery",
    "full-suite",
    "ruff",
    "compileall",
    "diff-check",
)

PYTEST_RELEASE_GATE_STEPS = frozenset(RELEASE_GATE_STEPS[:5])

PACKAGE_NAMES = (
    "forwin",
    "alembic",
    "pydantic",
    "SQLAlchemy",
    "fastapi",
    "fastmcp",
    "qdrant-client",
    "minio",
    "playwright",
    "pytest",
    "ruff",
)


class ManifestError(RuntimeError):
    pass


def command_environment(
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    values = os.environ if source is None else source
    control = sorted(
        key for key, value in values.items() if value and key in CONTROL_ENV_KEYS
    )
    if control:
        raise ManifestError(
            "collector control environment is not allowed: "
            + ", ".join(control)
        )
    return {
        key: value
        for key, value in values.items()
        if key in HOST_ENV_ALLOWLIST
    }


def run(*args: str) -> str:
    completed = subprocess.run(
        args,
        cwd=ROOT,
        env=command_environment(),
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise ManifestError(f"command failed ({' '.join(args)}): {detail}")
    return completed.stdout.strip()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_hash(value: Any) -> str:
    return sha256_bytes(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def sha256_file(path: Path) -> str:
    if not path.is_file():
        raise ManifestError(f"required file is missing: {path.relative_to(ROOT)}")
    return sha256_bytes(path.read_bytes())


def relative(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def source_path_matches_execution_copy(
    recorded_source: object,
    *,
    actual_path: Path,
    expected_path: Path,
) -> bool:
    raw_source = str(recorded_source or "")
    source_path = PurePosixPath(raw_source)
    source_parts = source_path.parts
    actual_parts = PurePosixPath(actual_path.as_posix()).parts
    return (
        bool(raw_source)
        and bool(source_parts)
        and not source_path.is_absolute()
        and ".." not in source_parts
        and source_path.name == expected_path.name
        and len(actual_parts) >= len(source_parts)
        and actual_parts[-len(source_parts) :] == source_parts
    )


def iter_files(paths: Iterable[Path]) -> list[Path]:
    found: set[Path] = set()
    for path in paths:
        if not path.exists():
            raise ManifestError(f"required path is missing: {relative(path)}")
        if path.is_file():
            found.add(path)
            continue
        for child in path.rglob("*"):
            if not child.is_file():
                continue
            if "__pycache__" in child.parts or child.suffix in {".pyc", ".pyo"}:
                continue
            found.add(child)
    return sorted(found, key=relative)


def tree_revision(*paths: Path) -> dict[str, Any]:
    files = iter_files(paths)
    digest = hashlib.sha256()
    entries: list[dict[str, str]] = []
    for path in files:
        name = relative(path)
        content_hash = sha256_file(path)
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content_hash.encode("ascii"))
        digest.update(b"\n")
        entries.append({"path": name, "sha256": content_hash})
    return {
        "sha256": digest.hexdigest(),
        "file_count": len(entries),
        "files": entries,
    }


def tracked_source_revision(
    source_sha: str,
    paths: Iterable[Path],
) -> dict[str, Any]:
    normalized = sorted((path.resolve() for path in paths), key=relative)
    for path in normalized:
        name = relative(path)
        if name == str(path):
            raise ManifestError(f"release harness is outside the source tree: {path}")
        run("git", "ls-files", "--error-unmatch", name)
        expected_blob = run("git", "rev-parse", f"{source_sha}:{name}")
        actual_blob = run("git", "hash-object", str(path))
        if actual_blob != expected_blob:
            raise ManifestError(
                f"release harness differs from candidate source: {name}"
            )
    return tree_revision(*normalized)


def matrix_successor_delta(
    matrix_source_sha: str,
    current_source_sha: str,
) -> dict[str, Any]:
    if matrix_source_sha == current_source_sha:
        return {
            "mode": "exact",
            "base_source_sha": matrix_source_sha,
            "current_source_sha": current_source_sha,
            "changes": [],
            "sha256": canonical_hash([]),
        }
    run(
        "git",
        "merge-base",
        "--is-ancestor",
        matrix_source_sha,
        current_source_sha,
    )
    raw = run(
        "git",
        "diff",
        "--name-status",
        "--no-renames",
        f"{matrix_source_sha}..{current_source_sha}",
    )
    changes: list[dict[str, str]] = []
    for line in raw.splitlines():
        status, separator, path = line.partition("\t")
        if not separator or status not in {"A", "M"}:
            raise ManifestError(
                f"matrix successor contains unsupported change: {line}"
            )
        if (
            path not in MATRIX_SUCCESSOR_ALLOWED_FILES
            and not path.startswith(MATRIX_SUCCESSOR_ALLOWED_PREFIXES)
        ):
            raise ManifestError(
                f"matrix successor changes an unapproved path: {path}"
            )
        changes.append(
            {
                "status": status,
                "path": path,
                "blob": run(
                    "git",
                    "rev-parse",
                    f"{current_source_sha}:{path}",
                ),
            }
        )
    if not changes:
        raise ManifestError("matrix predecessor SHA differs without a source delta")
    return {
        "mode": "bounded_successor",
        "base_source_sha": matrix_source_sha,
        "current_source_sha": current_source_sha,
        "changes": changes,
        "sha256": canonical_hash(changes),
    }


def module_ast(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=relative(path))


def literal_assignments(path: Path) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for node in module_ast(path).body:
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            target = node.target
            value = node.value
        if not isinstance(target, ast.Name) or value is None:
            continue
        try:
            values[target.id] = ast.literal_eval(value)
        except (TypeError, ValueError):
            continue
    return values


def class_literal(path: Path, class_name: str, field_name: str) -> Any:
    for node in module_ast(path).body:
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for child in node.body:
            if not isinstance(child, ast.AnnAssign):
                continue
            if not isinstance(child.target, ast.Name) or child.target.id != field_name:
                continue
            try:
                return ast.literal_eval(child.value)
            except (TypeError, ValueError) as exc:
                raise ManifestError(
                    f"{class_name}.{field_name} is not a literal in {relative(path)}"
                ) from exc
    raise ManifestError(f"{class_name}.{field_name} not found in {relative(path)}")


def assert_routing_defaults(config_path: Path) -> dict[str, str]:
    assignments = literal_assignments(config_path)
    try:
        actual = {
            "MINIMAX_BASE_URL": str(assignments["DEFAULT_MINIMAX_BASE_URL"]),
            "MINIMAX_MODEL": str(assignments["DEFAULT_MINIMAX_MODEL"]),
            "MOONSHOT_BASE_URL": str(assignments["DEFAULT_MOONSHOT_BASE_URL"]),
            "MOONSHOT_MODEL": str(assignments["DEFAULT_MOONSHOT_MODEL"]),
            "DEEPSEEK_BASE_URL": str(assignments["DEFAULT_DEEPSEEK_BASE_URL"]),
            "DEEPSEEK_MODEL": str(assignments["DEFAULT_DEEPSEEK_MODEL"]),
            "FORWIN_CODEX_DEFAULT_MODEL": str(
                assignments["DEFAULT_CODEX_MODEL"]
            ),
            "FORWIN_CODEX_BRIDGE_URL": str(
                class_literal(config_path, "CodexConfig", "bridge_url")
            ),
            "FORWIN_EMBEDDING_BACKEND": str(
                class_literal(
                    config_path,
                    "_InfrastructureFields",
                    "embedding_backend",
                )
            ),
            "FORWIN_EMBEDDING_BASE_URL": str(
                assignments["DEFAULT_EMBEDDING_GATEWAY_URL"]
            ),
            "FORWIN_EMBEDDING_MODEL": str(
                assignments["DEFAULT_EMBEDDING_MODEL"]
            ),
            "FORWIN_EMBEDDING_DIMS": str(
                assignments["DEFAULT_EMBEDDING_DIMS"]
            ),
            "FORWIN_EMBEDDING_REQUIRED": str(
                class_literal(
                    config_path,
                    "_InfrastructureFields",
                    "embedding_required",
                )
            ).lower(),
        }
    except KeyError as exc:
        raise ManifestError(
            f"model routing source default is missing: {exc.args[0]}"
        ) from exc
    mismatches = [
        key
        for key, value in actual.items()
        if str(ROUTING_DEFAULTS.get(key) or "") != value
    ]
    if set(actual) != set(ROUTING_DEFAULTS):
        mismatches.extend(
            sorted(set(actual).symmetric_difference(ROUTING_DEFAULTS))
        )
    if mismatches:
        raise ManifestError(
            f"model routing defaults drift from source: {sorted(set(mismatches))}"
        )
    return {key: actual[key] for key in sorted(actual)}


def inspect_image(tag: str, source_sha: str) -> dict[str, Any]:
    try:
        payload = json.loads(run("docker", "image", "inspect", tag))
    except json.JSONDecodeError as exc:
        raise ManifestError(f"docker returned invalid JSON for image {tag}") from exc
    if len(payload) != 1:
        raise ManifestError(f"expected one image for {tag}, got {len(payload)}")
    image = payload[0]
    labels = (image.get("Config") or {}).get("Labels") or {}
    revision = str(labels.get("org.opencontainers.image.revision") or "")
    if revision != source_sha:
        raise ManifestError(
            f"image {tag} revision {revision or '<missing>'} does not match {source_sha}"
        )
    return {
        "tag": tag,
        "image_id": str(image.get("Id") or ""),
        "repo_digests": sorted(str(item) for item in image.get("RepoDigests") or []),
        "created": str(image.get("Created") or ""),
        "os": str(image.get("Os") or ""),
        "architecture": str(image.get("Architecture") or ""),
        "revision": revision,
    }


def inspect_dependency_image(tag: str) -> dict[str, Any]:
    try:
        payload = json.loads(run("docker", "image", "inspect", tag))
    except json.JSONDecodeError as exc:
        raise ManifestError(
            f"docker returned invalid JSON for dependency image {tag}"
        ) from exc
    if len(payload) != 1:
        raise ManifestError(
            f"expected one dependency image for {tag}, got {len(payload)}"
        )
    image = payload[0]
    image_id = str(image.get("Id") or "")
    if not image_id:
        raise ManifestError(f"dependency image has no image ID: {tag}")
    return {
        "tag": tag,
        "image_id": image_id,
        "repo_digests": sorted(str(item) for item in image.get("RepoDigests") or []),
        "created": str(image.get("Created") or ""),
        "os": str(image.get("Os") or ""),
        "architecture": str(image.get("Architecture") or ""),
    }


def _env_value(env: dict[str, str], key: str, default: str = "") -> str:
    return str(env.get(key) or default).strip()


def _env_bool(env: dict[str, str], key: str, default: bool = False) -> bool:
    raw = _env_value(env, key)
    if not raw:
        return default
    normalized = raw.lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ManifestError(f"invalid boolean model-routing value for {key}")


def _env_int(env: dict[str, str], key: str, default: int) -> int:
    raw = _env_value(env, key)
    try:
        return int(raw) if raw else default
    except ValueError as exc:
        raise ManifestError(
            f"invalid integer model-routing value for {key}"
        ) from exc


def _endpoint_target_sha256(value: str) -> str:
    parsed = urlsplit(str(value or "").strip())
    if not parsed.scheme or not parsed.hostname:
        raise ManifestError("model-routing endpoint is invalid")
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    authority = (
        f"{host}:{parsed.port}"
        if parsed.port is not None
        else host
    )
    target = urlunsplit(
        (
            parsed.scheme.lower(),
            authority.lower(),
            parsed.path.rstrip("/"),
            "",
            "",
        )
    )
    return sha256_bytes(target.encode("utf-8"))


def resolve_model_routing(
    env: dict[str, str],
    *,
    model_profile_id: str,
) -> dict[str, Any]:
    minimax = {
        "id": "env-minimax",
        "model": _env_value(
            env, "MINIMAX_MODEL", ROUTING_DEFAULTS["MINIMAX_MODEL"]
        ),
        "base_url_target_sha256": _endpoint_target_sha256(
            _env_value(
                env,
                "MINIMAX_BASE_URL",
                ROUTING_DEFAULTS["MINIMAX_BASE_URL"],
            )
        ),
        "api_key_configured": bool(_env_value(env, "MINIMAX_API_KEY")),
    }
    kimi_api_key = _env_value(env, "KIMI_API_KEY") or _env_value(
        env, "MOONSHOT_API_KEY"
    )
    kimi = {
        "id": "env-kimi",
        "model": (
            _env_value(env, "KIMI_MODEL")
            or _env_value(env, "MOONSHOT_MODEL")
            or ROUTING_DEFAULTS["MOONSHOT_MODEL"]
        ),
        "base_url_target_sha256": _endpoint_target_sha256(
            _env_value(env, "KIMI_BASE_URL")
            or _env_value(env, "MOONSHOT_BASE_URL")
            or ROUTING_DEFAULTS["MOONSHOT_BASE_URL"]
        ),
        "api_key_configured": bool(kimi_api_key),
    }
    deepseek = {
        "id": "env-deepseek",
        "model": _env_value(
            env,
            "DEEPSEEK_MODEL",
            ROUTING_DEFAULTS["DEEPSEEK_MODEL"],
        ),
        "base_url_target_sha256": _endpoint_target_sha256(
            _env_value(
                env,
                "DEEPSEEK_BASE_URL",
                ROUTING_DEFAULTS["DEEPSEEK_BASE_URL"],
            )
        ),
        "api_key_configured": bool(_env_value(env, "DEEPSEEK_API_KEY")),
    }
    profiles = {
        item["id"]: item for item in (minimax, kimi, deepseek)
    }
    selected_id = str(model_profile_id or "").strip() or "env-minimax"
    if selected_id not in profiles:
        raise ManifestError(f"unknown model profile: {selected_id}")
    codex_model = (
        _env_value(env, "FORWIN_CODEX_DEFAULT_MODEL")
        or _env_value(env, "FORWIN_CODEX_MODEL")
        or ROUTING_DEFAULTS["FORWIN_CODEX_DEFAULT_MODEL"]
    )
    return {
        "selected_profile": profiles[selected_id],
        "fallback_profiles": [
            profile
            for profile in (kimi, deepseek)
            if profile["api_key_configured"]
        ],
        "codex": {
            "enabled": _env_bool(env, "FORWIN_CODEX_ENABLED", False),
            "default_model": codex_model,
            "bridge_target_sha256": _endpoint_target_sha256(
                _env_value(
                    env,
                    "FORWIN_CODEX_BRIDGE_URL",
                    ROUTING_DEFAULTS["FORWIN_CODEX_BRIDGE_URL"],
                )
            ),
        },
        "embedding": {
            "backend": _env_value(
                env,
                "FORWIN_EMBEDDING_BACKEND",
                ROUTING_DEFAULTS["FORWIN_EMBEDDING_BACKEND"],
            ),
            "api_key_configured": bool(
                _env_value(env, "FORWIN_EMBEDDING_API_KEY")
            ),
            "base_url_target_sha256": _endpoint_target_sha256(
                _env_value(
                    env,
                    "FORWIN_EMBEDDING_BASE_URL",
                    ROUTING_DEFAULTS["FORWIN_EMBEDDING_BASE_URL"],
                )
            ),
            "model": _env_value(
                env,
                "FORWIN_EMBEDDING_MODEL",
                ROUTING_DEFAULTS["FORWIN_EMBEDDING_MODEL"],
            ),
            "dims": _env_int(
                env,
                "FORWIN_EMBEDDING_DIMS",
                int(ROUTING_DEFAULTS["FORWIN_EMBEDDING_DIMS"]),
            ),
            "required": _env_bool(
                env,
                "FORWIN_EMBEDDING_REQUIRED",
                ROUTING_DEFAULTS["FORWIN_EMBEDDING_REQUIRED"] == "true",
            ),
        },
    }


def inspect_model_environments(
    containers: Iterable[str],
    runtime_image_id: str,
    *,
    model_profile_id: str,
) -> dict[str, Any]:
    services: dict[str, dict[str, Any]] = {}
    compose_projects: set[str] = set()
    for container in containers:
        try:
            payload = json.loads(
                run("docker", "container", "inspect", container)
            )
        except json.JSONDecodeError as exc:
            raise ManifestError(
                f"docker returned invalid JSON for container {container}"
            ) from exc
        if len(payload) != 1:
            raise ManifestError(
                f"expected one container for {container}, got {len(payload)}"
            )
        item = payload[0]
        if str(item.get("Image") or "") != runtime_image_id:
            raise ManifestError(
                f"container {container} does not use the frozen runtime image"
            )
        config = item.get("Config") or {}
        labels = config.get("Labels") or {}
        state = item.get("State") or {}
        health = (state.get("Health") or {}).get("Status") or ""
        if not state.get("Running") or health not in {"", "healthy"}:
            raise ManifestError(
                f"runtime container is not healthy: {container}"
            )
        service = str(
            labels.get("com.docker.compose.service") or ""
        )
        project = str(
            labels.get("com.docker.compose.project") or ""
        )
        if not service or not project or service in services:
            raise ManifestError(
                "runtime container Compose identity is incomplete or duplicate"
            )
        env: dict[str, str] = {}
        for raw in config.get("Env") or []:
            key, separator, value = str(raw).partition("=")
            if separator and key in MODEL_ENV_KEYS:
                env[key] = value
        routing = resolve_model_routing(
            env,
            model_profile_id=model_profile_id,
        )
        services[service] = {
            "container": str(container),
            "container_id": str(item.get("Id") or ""),
            "runtime_image_id": runtime_image_id,
            "routing": routing,
            "routing_sha256": canonical_hash(routing),
        }
        compose_projects.add(project)
    if set(services) != set(EXPECTED_RUNTIME_SERVICES):
        raise ManifestError(
            "runtime model-routing service set is "
            f"{sorted(services)}, expected "
            f"{sorted(EXPECTED_RUNTIME_SERVICES)}"
        )
    if len(compose_projects) != 1:
        raise ManifestError(
            "runtime model-routing containers do not share one Compose project"
        )
    for service in MODEL_PASSIVE_SERVICES:
        routing = services[service]["routing"]
        if (
            (routing.get("selected_profile") or {}).get(
                "api_key_configured"
            )
            or routing.get("fallback_profiles")
            or (routing.get("codex") or {}).get("enabled")
            or (routing.get("embedding") or {}).get(
                "api_key_configured"
            )
        ):
            raise ManifestError(
                f"passive runtime service has model credentials: {service}"
            )
    routing_group_hashes: dict[str, str] = {}
    for group, expected_services in MODEL_ROUTING_GROUPS.items():
        hashes = {
            services[service]["routing_sha256"]
            for service in expected_services
        }
        if len(hashes) != 1:
            raise ManifestError(
                f"effective model routing differs within {group} roles"
            )
        routing_group_hashes[group] = next(iter(hashes))
    routing_hash = canonical_hash(
        {
            service: services[service]["routing_sha256"]
            for service in sorted(services)
        }
    )
    return {
        "compose_project": next(iter(compose_projects)),
        "services": {
            key: services[key] for key in sorted(services)
        },
        "routing_group_hashes": routing_group_hashes,
        "routing_sha256": routing_hash,
    }


def load_matrix_manifest(
    path: Path,
    source_sha: str,
    *,
    require_final_audit: bool = False,
) -> dict[str, Any]:
    if not path.is_file():
        raise ManifestError(f"matrix manifest is missing: {relative(path)}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ManifestError(f"matrix manifest is invalid JSON: {relative(path)}") from exc
    if require_final_audit:
        if int(payload.get("schema_version") or 0) != MATRIX_AUDIT_SCHEMA_VERSION:
            raise ManifestError(
                "matrix final audit schema version mismatch: "
                f"{payload.get('schema_version')}"
            )
        if payload.get("result") != "pass":
            raise ManifestError("matrix final audit does not report pass")
        identity = payload.get("identity") or {}
        matrix_source_sha = str(identity.get("source_sha") or "")
        if int(identity.get("code_changes_during_run") or 0) != 0:
            raise ManifestError("matrix final audit reports code changes")
        expected_cells = {"L30", "L60S", "L60P", "L100"}
        cells = payload.get("cells")
        if not isinstance(cells, dict) or set(cells) != expected_cells:
            raise ManifestError("matrix final audit cell identities mismatch")

        expected_auditor = Path(__file__).with_name("finalize_matrix.py").resolve()
        expected_helper = Path(__file__).with_name("l200_evidence.py").resolve()
        auditor = payload.get("auditor") or {}
        for path_key, source_key, hash_key, expected_path in (
            ("path", "source_path", "sha256", expected_auditor),
            (
                "database_helper_path",
                "database_helper_source_path",
                "database_helper_sha256",
                expected_helper,
            ),
        ):
            actual_path = Path(str(auditor.get(path_key) or "")).resolve()
            recorded_hash = str(auditor.get(hash_key) or "")
            if (
                not source_path_matches_execution_copy(
                    auditor.get(source_key),
                    actual_path=actual_path,
                    expected_path=expected_path,
                )
                or not actual_path.is_file()
                or sha256_file(actual_path) != recorded_hash
                or not expected_path.is_file()
                or sha256_file(expected_path) != recorded_hash
            ):
                raise ManifestError(
                    f"matrix final audit tool identity mismatch: {path_key}"
                )

        matrix_ref = identity.get("matrix_manifest") or {}
        matrix_path = Path(str(matrix_ref.get("path") or ""))
        if not matrix_path.is_absolute():
            matrix_path = ROOT / matrix_path
        if (
            not matrix_path.is_file()
            or sha256_file(matrix_path) != matrix_ref.get("sha256")
        ):
            raise ManifestError("matrix final audit source manifest hash mismatch")
        try:
            matrix_payload = json.loads(matrix_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ManifestError("matrix source manifest is invalid JSON") from exc
        if (
            not isinstance(matrix_payload, dict)
            or matrix_payload.get("source_sha") != matrix_source_sha
            or int(matrix_payload.get("code_changes_during_run") or 0) != 0
        ):
            raise ManifestError("matrix source manifest identity mismatch")
        recorded_harness = identity.get("harness")
        if (
            not isinstance(recorded_harness, dict)
            or not recorded_harness.get("path")
            or not recorded_harness.get("sha256")
        ):
            raise ManifestError(
                "matrix final audit harness identity is missing"
            )
        harness = matrix_harness_identity(
            matrix_path,
            matrix_payload,
            recorded=recorded_harness,
        )
        predecessor_delta = matrix_successor_delta(
            matrix_source_sha,
            source_sha,
        )

        spec = importlib.util.spec_from_file_location(
            "forwin_matrix_finalizer",
            expected_auditor,
        )
        if spec is None or spec.loader is None:
            raise ManifestError("matrix final audit tool import failed")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        report_results = {}
        for name in sorted(expected_cells):
            item = cells[name]
            if (
                not isinstance(item, dict)
                or item.get("violations") != []
                or int(item.get("target") or 0)
                != int(module.EXPECTED_CELLS[name]["target"])
            ):
                raise ManifestError(f"matrix final audit cell is not passing: {name}")
            evidence_path = Path(str(item.get("evidence_path") or ""))
            if not evidence_path.is_absolute():
                evidence_path = ROOT / evidence_path
            if (
                not evidence_path.is_file()
                or sha256_file(evidence_path) != item.get("evidence_sha256")
            ):
                raise ManifestError(
                    f"matrix final audit cell evidence hash mismatch: {name}"
                )
            try:
                evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ManifestError(
                    f"matrix final audit cell evidence is invalid: {name}"
                ) from exc
            if (
                not isinstance(evidence, dict)
                or module.validate_cell(name, evidence)
            ):
                raise ManifestError(
                    f"matrix final audit cell evidence fails revalidation: {name}"
                )
            evidence_project_id = str(
                (evidence.get("project") or {}).get("id") or ""
            )
            if str(item.get("project_id") or "") != evidence_project_id:
                raise ManifestError(
                    f"matrix final audit cell project mismatch: {name}"
                )
            candidate_spark_model = str(
                (
                    (identity.get("candidate_stack") or {}).get(
                        "spark_model"
                    )
                    or ""
                )
            )
            if (
                not candidate_spark_model
                or evidence.get("candidate_spark_model")
                != candidate_spark_model
            ):
                raise ManifestError(
                    f"matrix final audit Spark model mismatch: {name}"
                )
            report_results[name] = {
                "violations": [],
                "evidence": evidence,
            }
        matrix_violations = module.matrix_operational_violations(report_results)
        if matrix_violations:
            raise ManifestError(
                "matrix final audit lacks live Spark delegation evidence"
            )
        if payload.get("violations") != matrix_violations:
            raise ManifestError("matrix final audit violation ledger mismatch")
        final_report = payload.get("final_report") or {}
        report_path = Path(str(final_report.get("path") or ""))
        if not report_path.is_absolute():
            report_path = ROOT / report_path
        if (
            not report_path.is_file()
            or sha256_file(report_path) != final_report.get("sha256")
        ):
            raise ManifestError("matrix final audit report hash mismatch")
        expected_report = module.final_report(
            identity,
            report_results,
            matrix_violations,
        )
        if report_path.read_text(encoding="utf-8") != expected_report:
            raise ManifestError("matrix final audit report content mismatch")
        return {
            "path": relative(path),
            "sha256": sha256_file(path),
            "schema_version": payload.get("schema_version"),
            "result": "pass",
            "matrix": matrix_payload.get("matrix"),
            "source_sha": matrix_source_sha,
            "current_rc_source_sha": source_sha,
            "predecessor_delta": predecessor_delta,
            "code_changes_during_run": 0,
            "source_manifest": {
                "path": relative(matrix_path),
                "sha256": sha256_file(matrix_path),
            },
            "harness": harness,
            "cells": {
                name: {
                    "project_id": cells[name].get("project_id"),
                    "target": cells[name].get("target"),
                    "evidence_sha256": cells[name].get("evidence_sha256"),
                }
                for name in sorted(expected_cells)
            },
        }

    matrix_source_sha = str(payload.get("source_sha") or "")
    if not matrix_source_sha:
        raise ManifestError("matrix manifest has no source_sha")
    if payload.get("code_changes_during_run") != 0:
        raise ManifestError("matrix manifest reports code_changes_during_run != 0")
    harness = matrix_harness_identity(path, payload)
    return {
        "path": relative(path),
        "sha256": sha256_file(path),
        "schema_version": payload.get("schema_version"),
        "matrix": payload.get("matrix"),
        "source_sha": matrix_source_sha,
        "current_rc_source_sha": source_sha,
        "code_changes_during_run": payload.get("code_changes_during_run"),
        "result": "draft",
        "harness": harness,
    }


def matrix_harness_identity(
    matrix_path: Path,
    matrix_payload: Mapping[str, Any],
    *,
    recorded: object = None,
) -> dict[str, str]:
    expected_hash = str(matrix_payload.get("harness_sha256") or "")
    if not expected_hash:
        raise ManifestError("matrix manifest has no harness_sha256")

    recorded_identity = recorded if isinstance(recorded, dict) else {}
    raw_path = str(recorded_identity.get("path") or "")
    harness_path = (
        Path(raw_path) if raw_path else matrix_path.with_name("matrix_run.py")
    )
    if not harness_path.is_absolute():
        harness_path = ROOT / harness_path
    recorded_hash = str(recorded_identity.get("sha256") or expected_hash)
    if recorded_hash != expected_hash:
        raise ManifestError("matrix harness identity hash mismatch")
    if not harness_path.is_file() or sha256_file(harness_path) != expected_hash:
        raise ManifestError("matrix harness hash mismatch")
    return {
        "path": relative(harness_path),
        "sha256": expected_hash,
    }


def resolve_evidence_path(value: object) -> Path:
    path = Path(str(value or ""))
    return path if path.is_absolute() else ROOT / path


def verify_evidence_artifact(item: object, *, label: str) -> None:
    if not isinstance(item, dict):
        raise ManifestError(f"{label} artifact identity is missing")
    path = resolve_evidence_path(item.get("path"))
    expected_hash = str(item.get("sha256") or "")
    if not path.is_file() or not expected_hash:
        raise ManifestError(f"{label} artifact identity is incomplete")
    if sha256_file(path) != expected_hash:
        raise ManifestError(f"{label} artifact hash mismatch")


def image_identity_subset(value: object) -> dict[str, str]:
    image = value if isinstance(value, dict) else {}
    return {
        key: str(image.get(key) or "")
        for key in ("tag", "image_id", "revision")
    }


def validate_passing_junit(path: Path, *, step_name: str) -> None:
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise ManifestError(
            f"release gate JUnit is invalid XML: {step_name}"
        ) from exc
    nodes = [root, *root.iter()]
    suites = {
        id(node): node
        for node in nodes
        if str(node.tag).rsplit("}", 1)[-1] == "testsuite"
    }.values()
    testcases = [
        node
        for node in root.iter()
        if str(node.tag).rsplit("}", 1)[-1] == "testcase"
    ]
    failures = [
        node
        for node in root.iter()
        if str(node.tag).rsplit("}", 1)[-1] in {"failure", "error"}
    ]
    try:
        declared_tests = sum(int(node.get("tests") or 0) for node in suites)
        declared_failures = sum(
            int(node.get("failures") or 0) + int(node.get("errors") or 0)
            for node in suites
        )
    except ValueError as exc:
        raise ManifestError(
            f"release gate JUnit counters are invalid: {step_name}"
        ) from exc
    if (
        declared_tests <= 0
        or not testcases
        or declared_failures
        or failures
    ):
        raise ManifestError(f"release gate JUnit is not passing: {step_name}")


def validate_release_gate_evidence(
    payload: dict[str, Any],
    *,
    source_sha: str | None = None,
    expected_gate_identity: dict[str, Any] | None = None,
) -> None:
    identity = payload.get("identity") or {}
    evidence_source_sha = str(identity.get("source_sha") or "")
    if not evidence_source_sha or (
        source_sha is not None and evidence_source_sha != source_sha
    ):
        raise ManifestError("release gate source SHA mismatch")
    source_tree = str(identity.get("source_tree") or "")
    if not source_tree:
        raise ManifestError("release gate source tree is missing")
    runtime_image = image_identity_subset(identity.get("runtime_image"))
    browser_image = image_identity_subset(identity.get("browser_image"))
    for label, image in (
        ("runtime", runtime_image),
        ("browser", browser_image),
    ):
        if not all(image.values()) or image["revision"] != evidence_source_sha:
            raise ManifestError(f"release gate {label} image identity is incomplete")

    verify_evidence_artifact(
        identity.get("rc_manifest"),
        label="release gate candidate manifest",
    )
    candidate_path = resolve_evidence_path(
        (identity.get("rc_manifest") or {}).get("path")
    )
    try:
        candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ManifestError("release gate candidate manifest is invalid JSON") from exc
    candidate_source = candidate.get("source") or {}
    candidate_images = candidate.get("images") or {}
    if (
        candidate_source.get("sha") != evidence_source_sha
        or candidate_source.get("tree") != source_tree
    ):
        raise ManifestError("release gate candidate source identity mismatch")
    if image_identity_subset(candidate_images.get("runtime")) != runtime_image:
        raise ManifestError("release gate candidate runtime image mismatch")
    if (
        image_identity_subset(candidate_images.get("publisher_browser"))
        != browser_image
    ):
        raise ManifestError("release gate candidate browser image mismatch")

    if expected_gate_identity is not None:
        if str(expected_gate_identity.get("source_tree") or "") != source_tree:
            raise ManifestError("release gate source tree mismatch")
        if (
            image_identity_subset(expected_gate_identity.get("runtime_image"))
            != runtime_image
        ):
            raise ManifestError("release gate runtime image mismatch")
        if (
            image_identity_subset(expected_gate_identity.get("browser_image"))
            != browser_image
        ):
            raise ManifestError("release gate browser image mismatch")

    if payload.get("all_steps_completed") is not True:
        raise ManifestError("release gate evidence is not complete")
    expected_runner = Path(__file__).with_name("run_rc_gates.py").resolve()
    runner = payload.get("runner") or {}
    actual_runner = resolve_evidence_path(runner.get("path")).resolve()
    if actual_runner != expected_runner:
        raise ManifestError("release gate runner path mismatch")
    verify_evidence_artifact(
        runner,
        label="release gate runner",
    )
    release_files = {
        str(item.get("path") or ""): str(item.get("sha256") or "")
        for item in (candidate.get("release_harness") or {}).get("files") or []
        if isinstance(item, dict)
    }
    if release_files.get(relative(expected_runner)) != runner.get("sha256"):
        raise ManifestError(
            "release gate runner is not bound by candidate source"
        )
    spec = importlib.util.spec_from_file_location(
        "forwin_release_gate_runner",
        expected_runner,
    )
    if spec is None or spec.loader is None:
        raise ManifestError("release gate runner import failed")
    runner_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner_module)
    configured_steps = {
        str(step["name"]): step for step in runner_module.gate_steps()
    }
    required = payload.get("required_step_names")
    if (
        required != list(RELEASE_GATE_STEPS)
        or list(configured_steps) != list(RELEASE_GATE_STEPS)
    ):
        raise ManifestError("release gate required step contract mismatch")
    completed = payload.get("completed_step_names")
    if (
        not isinstance(completed, list)
        or len(completed) != len(set(completed))
        or set(completed) != set(RELEASE_GATE_STEPS)
    ):
        raise ManifestError("release gate completed step contract mismatch")
    steps = payload.get("steps")
    if not isinstance(steps, list):
        raise ManifestError("release gate step evidence is missing")
    by_name = {
        str(item.get("name") or ""): item
        for item in steps
        if isinstance(item, dict)
    }
    if len(by_name) != len(steps) or set(by_name) != set(RELEASE_GATE_STEPS):
        raise ManifestError("release gate step identities mismatch")
    for name in RELEASE_GATE_STEPS:
        step = by_name[name]
        configured = configured_steps[name]
        if step.get("kind") != configured.get("kind"):
            raise ManifestError(f"release gate step kind mismatch: {name}")
        expected_command = list(configured["command"])
        junit = step.get("junit")
        if name in PYTEST_RELEASE_GATE_STEPS:
            if not isinstance(junit, dict) or junit.get("exists") is not True:
                raise ManifestError(f"release gate JUnit is missing: {name}")
            junit_path = resolve_evidence_path(junit.get("path")).resolve()
            expected_command.append(f"--junitxml={junit_path}")
        if step.get("command") != expected_command:
            raise ManifestError(f"release gate step command mismatch: {name}")
        if step.get("passed") is not True or int(step.get("exit_code", -1)) != 0:
            raise ManifestError(f"release gate step is not passing: {name}")
        verify_evidence_artifact(step.get("log"), label=f"release gate {name} log")
        log_path = resolve_evidence_path((step.get("log") or {}).get("path"))
        expected_header = (
            f"started_at={step.get('started_at')}\n"
            + "command="
            + json.dumps(expected_command, ensure_ascii=False)
            + "\n"
        )
        if not log_path.read_text(encoding="utf-8").startswith(expected_header):
            raise ManifestError(f"release gate step log header mismatch: {name}")
        if name in PYTEST_RELEASE_GATE_STEPS:
            verify_evidence_artifact(
                junit,
                label=f"release gate {name} JUnit",
            )
            validate_passing_junit(junit_path, step_name=name)


def validate_smoke_evidence(
    payload: dict[str, Any],
    *,
    source_sha: str,
    expected_candidate_identity: dict[str, Any] | None = None,
) -> None:
    if (
        int(payload.get("schema_version") or 0) != 1
        or payload.get("result") != "pass"
        or int(payload.get("target") or 0) != 30
        or int(payload.get("accepted") or 0) != 30
        or int(payload.get("needs_review") or 0) != 0
        or payload.get("has_active_generation_task") is not False
        or int(payload.get("code_changes_during_run") or 0) != 0
        or payload.get("violations") != []
    ):
        raise ManifestError("post-decision smoke summary is not a complete pass")
    identity = payload.get("identity") or {}
    if identity.get("source_sha") != source_sha:
        raise ManifestError("post-decision smoke identity source SHA mismatch")
    verify_evidence_artifact(
        identity.get("candidate_manifest"),
        label="post-decision smoke candidate manifest",
    )
    verify_evidence_artifact(
        payload.get("evidence"),
        label="post-decision smoke state",
    )
    verify_evidence_artifact(
        payload.get("final_report"),
        label="post-decision smoke report",
    )
    verify_evidence_artifact(
        payload.get("operation_transcript"),
        label="post-decision smoke operation transcript",
    )

    expected_tools = {
        "path": Path(__file__).with_name("finalize_smoke.py").resolve(),
        "matrix_helper_path": Path(__file__).with_name("finalize_matrix.py").resolve(),
        "database_helper_path": Path(__file__).with_name("l200_evidence.py").resolve(),
        "lifecycle_runner_path": Path(__file__).with_name(
            "smoke_lifecycle.py"
        ).resolve(),
    }
    auditor = payload.get("auditor") or {}
    for path_key, expected_path in expected_tools.items():
        hash_key = "sha256" if path_key == "path" else path_key.replace("_path", "_sha256")
        actual_path = resolve_evidence_path(auditor.get(path_key)).resolve()
        if actual_path != expected_path:
            raise ManifestError(f"post-decision smoke auditor path mismatch: {path_key}")
        verify_evidence_artifact(
            {
                "path": actual_path,
                "sha256": auditor.get(hash_key),
            },
            label=f"post-decision smoke auditor {path_key}",
        )

    candidate_path = resolve_evidence_path(
        (identity.get("candidate_manifest") or {}).get("path")
    )
    state_path = resolve_evidence_path((payload.get("evidence") or {}).get("path"))
    transcript_path = resolve_evidence_path(
        (payload.get("operation_transcript") or {}).get("path")
    )
    try:
        candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
        evidence = json.loads(state_path.read_text(encoding="utf-8"))
        transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ManifestError("post-decision smoke nested evidence is invalid JSON") from exc
    if (
        not isinstance(candidate, dict)
        or not isinstance(evidence, dict)
        or not isinstance(transcript, dict)
    ):
        raise ManifestError("post-decision smoke nested evidence must contain objects")
    if (candidate.get("source") or {}).get("sha") != source_sha:
        raise ManifestError("post-decision smoke candidate source SHA mismatch")
    release_files = {
        str(item.get("path") or ""): str(item.get("sha256") or "")
        for item in (candidate.get("release_harness") or {}).get("files") or []
        if isinstance(item, dict)
    }
    for path_key, expected_path in expected_tools.items():
        source_path = relative(expected_path)
        hash_key = (
            "sha256"
            if path_key == "path"
            else path_key.replace("_path", "_sha256")
        )
        if release_files.get(source_path) != (payload.get("auditor") or {}).get(
            hash_key
        ):
            raise ManifestError(
                f"post-decision smoke auditor is not candidate-bound: {path_key}"
            )

    smoke_path = expected_tools["path"]
    spec = importlib.util.spec_from_file_location(
        "forwin_release_smoke_finalizer",
        smoke_path,
    )
    if spec is None or spec.loader is None:
        raise ManifestError("post-decision smoke auditor import failed")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    identity_errors = module.identity_violations(
        candidate,
        identity,
    )
    if identity_errors:
        raise ManifestError(
            "post-decision smoke identity failed: "
            + "; ".join(identity_errors)
        )
    if expected_candidate_identity is not None:
        if (
            str((candidate.get("source") or {}).get("tree") or "")
            != str(expected_candidate_identity.get("source_tree") or "")
        ):
            raise ManifestError(
                "post-decision smoke candidate source tree mismatch"
            )
        candidate_images = candidate.get("images") or {}
        expected_images = {
            "runtime": expected_candidate_identity.get("runtime_image"),
            "publisher_browser": expected_candidate_identity.get("browser_image"),
            **(
                expected_candidate_identity.get("dependency_images")
                if isinstance(
                    expected_candidate_identity.get("dependency_images"),
                    dict,
                )
                else {}
            ),
        }
        for key in (
            "runtime",
            "publisher_browser",
            "postgres",
            "qdrant",
            "minio",
        ):
            expected = expected_images.get(key) or {}
            actual = candidate_images.get(key) or {}
            if any(
                str(actual.get(field) or "")
                != str(expected.get(field) or "")
                for field in ("tag", "image_id")
            ):
                raise ManifestError(
                    f"post-decision smoke candidate image mismatch: {key}"
                )
    violations = module.smoke_violations(
        candidate,
        evidence,
        payload.get("fresh_project") or {},
        profile=str(payload.get("quality_profile") or ""),
        delegate=str(payload.get("gate_delegate") or ""),
    )
    if violations:
        raise ManifestError(
            "post-decision smoke nested state failed: " + "; ".join(violations)
        )
    transcript_violations = module.operation_transcript_violations(
        candidate,
        identity,
        transcript,
        evidence,
    )
    if transcript_violations:
        raise ManifestError(
            "post-decision smoke lifecycle failed: "
            + "; ".join(transcript_violations)
        )
    project = evidence.get("project") or {}
    if (
        str(project.get("id") or "") != str(payload.get("project_id") or "")
        or int(project.get("accepted_chapter_count") or 0) != 30
        or int(project.get("needs_review_chapter_count") or 0) != 0
    ):
        raise ManifestError("post-decision smoke project summary mismatch")
    report_path = resolve_evidence_path(
        (payload.get("final_report") or {}).get("path")
    )
    if report_path.read_text(encoding="utf-8") != module.report(
        identity,
        evidence,
        [],
    ):
        raise ManifestError("post-decision smoke report content mismatch")


def validate_recovery_runner_identity(
    identity: object,
    *,
    expected_runner: Path,
    release_files: Mapping[str, str],
    fault_kind: str,
) -> None:
    runner = identity if isinstance(identity, dict) else {}
    actual_path = resolve_evidence_path(runner.get("path")).resolve()
    if actual_path != expected_runner:
        raise ManifestError(f"{fault_kind}.runner path mismatch")
    runner_hash = str(runner.get("sha256") or "")
    if (
        not expected_runner.is_file()
        or not runner_hash
        or sha256_file(expected_runner) != runner_hash
    ):
        raise ManifestError(f"{fault_kind}.runner artifact hash mismatch")
    if release_files.get(relative(expected_runner)) != runner_hash:
        raise ManifestError(
            f"{fault_kind}.runner is not bound by candidate source"
        )


def validate_recovery_candidate_bindings(
    payload: dict[str, Any],
    *,
    finalizer: Any,
    expected_candidate_identity: dict[str, Any] | None,
) -> None:
    identity = payload.get("identity") or {}
    candidate_artifact = identity.get("rc_manifest") or {}
    candidate_path = resolve_evidence_path(candidate_artifact.get("path"))
    try:
        candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError("recovery candidate manifest is unreadable") from exc
    if not isinstance(candidate, dict):
        raise ManifestError("recovery candidate manifest is not an object")

    if expected_candidate_identity is not None:
        candidate_source = candidate.get("source") or {}
        if candidate_source.get("tree") != expected_candidate_identity.get(
            "source_tree"
        ):
            raise ManifestError("recovery candidate source tree mismatch")
        candidate_images = candidate.get("images") or {}
        expected_images = {
            "runtime": expected_candidate_identity.get("runtime_image"),
            "publisher_browser": expected_candidate_identity.get(
                "browser_image"
            ),
            **(
                expected_candidate_identity.get("dependency_images")
                if isinstance(
                    expected_candidate_identity.get("dependency_images"),
                    dict,
                )
                else {}
            ),
        }
        for key in (
            "runtime",
            "publisher_browser",
            "postgres",
            "qdrant",
            "minio",
        ):
            if image_identity_subset(
                candidate_images.get(key)
            ) != image_identity_subset(expected_images.get(key)):
                raise ManifestError(
                    f"recovery candidate image mismatch: {key}"
                )

    release_files = {
        str(item.get("path") or ""): str(item.get("sha256") or "")
        for item in (candidate.get("release_harness") or {}).get("files") or []
        if isinstance(item, dict)
    }
    docker_identity: dict[str, Any] | None = None
    faults = payload.get("faults") or {}
    for kind, expected_runner in RECOVERY_RUNNER_PATHS.items():
        item = faults.get(kind) or {}
        report_path = resolve_evidence_path(item.get("path"))
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ManifestError(
                f"{kind}.report is unreadable during candidate binding"
            ) from exc
        if not isinstance(report, dict):
            raise ManifestError(
                f"{kind}.report is not an object during candidate binding"
            )
        validate_recovery_runner_identity(
            report.get("runner"),
            expected_runner=expected_runner,
            release_files=release_files,
            fault_kind=kind,
        )
        event_identity = report.get("event_log") or {}
        event_path = resolve_evidence_path(event_identity.get("path"))
        try:
            events = finalizer.load_verified_events(event_path)
        except finalizer.RecoveryEvidenceError as exc:
            raise ManifestError(
                f"{kind}.event chain changed during candidate binding"
            ) from exc
        chain_docker_identities = {
            canonical_hash((event.get("identity") or {}).get("docker") or {})
            for event in events
        }
        if len(chain_docker_identities) != 1:
            raise ManifestError(
                f"{kind}.event-chain Docker identity mismatch"
            )
        current_docker = (events[0].get("identity") or {}).get("docker")
        if not isinstance(current_docker, dict) or not current_docker:
            raise ManifestError(
                f"{kind}.event-chain Docker identity mismatch"
            )
        if docker_identity is None:
            docker_identity = current_docker
        elif current_docker != docker_identity:
            raise ManifestError(
                f"{kind}.event-chain Docker identity mismatch"
            )


def validate_recovery_evidence(
    payload: dict[str, Any],
    *,
    source_sha: str,
    expected_candidate_identity: dict[str, Any] | None = None,
) -> None:
    finalizer_path = Path(__file__).with_name("finalize_recovery.py").resolve()
    if not finalizer_path.is_file():
        raise ManifestError("recovery evidence finalizer is missing")
    spec = importlib.util.spec_from_file_location(
        "forwin_recovery_finalizer",
        finalizer_path,
    )
    if spec is None or spec.loader is None:
        raise ManifestError("recovery evidence finalizer import failed")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    violations = module.recovery_manifest_violations(
        payload,
        source_sha=source_sha,
    )
    if violations:
        raise ManifestError("recovery evidence invalid: " + "; ".join(violations))
    validate_recovery_candidate_bindings(
        payload,
        finalizer=module,
        expected_candidate_identity=expected_candidate_identity,
    )


def validate_v1_evidence(
    payload: dict[str, Any],
    *,
    source_sha: str,
) -> None:
    finalizer_path = Path(__file__).with_name("finalize_v1.py").resolve()
    if not finalizer_path.is_file():
        raise ManifestError("V1 evidence finalizer is missing")
    spec = importlib.util.spec_from_file_location(
        "forwin_v1_finalizer",
        finalizer_path,
    )
    if spec is None or spec.loader is None:
        raise ManifestError("V1 evidence finalizer import failed")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    violations = module.v1_manifest_violations(
        payload,
        source_sha=source_sha,
    )
    if violations:
        raise ManifestError("V1 evidence invalid: " + "; ".join(violations))


def load_release_evidence(
    path: Path,
    *,
    source_sha: str,
    kind: str,
    expected_gate_identity: dict[str, Any] | None = None,
    expected_candidate_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not path.is_file():
        raise ManifestError(f"{kind} manifest is missing: {relative(path)}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ManifestError(f"{kind} manifest is invalid JSON: {relative(path)}") from exc
    if not isinstance(payload, dict):
        raise ManifestError(f"{kind} manifest must contain an object")
    evidence_source = str(
        payload.get("source_sha")
        or (payload.get("identity") or {}).get("source_sha")
        or ""
    )
    if evidence_source != source_sha:
        raise ManifestError(
            f"{kind} source_sha {evidence_source or '<missing>'} does not match {source_sha}"
        )
    if kind == "release_gates":
        passed = payload.get("release_gate_passed") is True
    else:
        passed = payload.get("result") == "pass"
    if not passed:
        raise ManifestError(f"{kind} manifest does not report a complete pass")
    if kind == "release_gates":
        validate_release_gate_evidence(
            payload,
            source_sha=source_sha,
            expected_gate_identity=expected_gate_identity,
        )
    elif kind == "v1_preflight":
        validate_v1_evidence(payload, source_sha=source_sha)
    elif kind == "post_decision_smoke":
        validate_smoke_evidence(
            payload,
            source_sha=source_sha,
            expected_candidate_identity=expected_candidate_identity,
        )
    elif kind == "live_recovery":
        validate_recovery_evidence(
            payload,
            source_sha=source_sha,
            expected_candidate_identity=expected_candidate_identity,
        )
    identity = payload.get("identity") or {}
    candidate_artifact = (
        identity.get("rc_manifest")
        if kind in {"release_gates", "live_recovery"}
        else identity.get("candidate_manifest")
    )
    candidate_sha256 = str((candidate_artifact or {}).get("sha256") or "")
    if not candidate_sha256:
        raise ManifestError(f"{kind} candidate manifest identity is missing")
    return {
        "path": relative(path),
        "sha256": sha256_file(path),
        "source_sha": evidence_source,
        "result": "pass",
        "schema_version": payload.get("schema_version"),
        "candidate_manifest_sha256": candidate_sha256,
    }


def collect_release_candidate(
    args: argparse.Namespace,
    source_sha: str,
    *,
    expected_gate_identity: dict[str, Any] | None = None,
    expected_candidate_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if args.draft:
        return {
            "status": "draft",
            "source_sha": source_sha,
            "annotated_tag": "",
            "tag_object_sha": "",
            "evidence": {},
        }

    required = {
        "--rc-tag": args.rc_tag,
        "--v1-manifest": args.v1_manifest,
        "--gate-manifest": args.gate_manifest,
        "--recovery-manifest": args.recovery_manifest,
        "--smoke-manifest": args.smoke_manifest,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise ManifestError(
            "final RC manifest requires: " + ", ".join(sorted(missing))
        )

    tag = str(args.rc_tag)
    tag_ref = f"refs/tags/{tag}"
    if run("git", "cat-file", "-t", tag_ref) != "tag":
        raise ManifestError(f"RC tag is not annotated: {tag}")
    tagged_source = run("git", "rev-parse", f"{tag_ref}^{{}}")
    if tagged_source != source_sha:
        raise ManifestError(
            f"RC tag {tag} points to {tagged_source}, expected {source_sha}"
        )

    evidence = {
        "v1_preflight": load_release_evidence(
            args.v1_manifest,
            source_sha=source_sha,
            kind="v1_preflight",
        ),
        "release_gates": load_release_evidence(
            args.gate_manifest,
            source_sha=source_sha,
            kind="release_gates",
            expected_gate_identity=expected_gate_identity,
        ),
        "live_recovery": load_release_evidence(
            args.recovery_manifest,
            source_sha=source_sha,
            kind="live_recovery",
            expected_candidate_identity=expected_candidate_identity,
        ),
        "post_decision_smoke": load_release_evidence(
            args.smoke_manifest,
            source_sha=source_sha,
            kind="post_decision_smoke",
            expected_candidate_identity=expected_candidate_identity,
        ),
    }
    candidate_hashes = {
        str(item.get("candidate_manifest_sha256") or "")
        for item in evidence.values()
    }
    if len(candidate_hashes) != 1 or "" in candidate_hashes:
        raise ManifestError(
            "release evidence does not share one candidate manifest identity"
        )
    return {
        "status": "frozen",
        "source_sha": source_sha,
        "annotated_tag": tag,
        "tag_object_sha": run("git", "rev-parse", tag_ref),
        "evidence": evidence,
    }


def package_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    for name in PACKAGE_NAMES:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "unavailable"
    return versions


def report_schema_versions(path: Path) -> dict[str, Any]:
    return {
        class_name: class_literal(path, class_name, "schema_version")
        for class_name in REPORT_SCHEMA_CLASSES
    }


def read_optional_evidence(paths: list[Path]) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for path in paths:
        if path.is_file():
            revision = tree_revision(path)
        elif path.is_dir():
            revision = tree_revision(path)
        else:
            raise ManifestError(f"evidence path is missing: {relative(path)}")
        evidence.append({"path": relative(path), **revision})
    return evidence


def atomic_write(path: Path, payload: dict[str, Any]) -> None:
    if path.exists():
        raise ManifestError(f"RC manifest already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise ManifestError(f"RC manifest already exists: {path}") from exc
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect a secret-free, reproducible ForWin v5 RC manifest."
    )
    parser.add_argument("--runtime-image", required=True)
    parser.add_argument("--browser-image", required=True)
    parser.add_argument(
        "--postgres-image",
        default=DEFAULT_DEPENDENCY_IMAGES["postgres"],
    )
    parser.add_argument(
        "--qdrant-image",
        default=DEFAULT_DEPENDENCY_IMAGES["qdrant"],
    )
    parser.add_argument(
        "--minio-image",
        default=DEFAULT_DEPENDENCY_IMAGES["minio"],
    )
    parser.add_argument(
        "--runtime-container",
        action="append",
        required=True,
    )
    parser.add_argument(
        "--matrix-manifest",
        type=Path,
        default=ROOT / ".artifacts/v4-matrix-candidate/manifest.json",
    )
    parser.add_argument(
        "--matrix-audit-manifest",
        type=Path,
        default=ROOT / ".artifacts/v4-matrix-candidate/final-audit/manifest.json",
    )
    parser.add_argument("--evidence", type=Path, action="append", default=[])
    parser.add_argument("--quality-profile", default="standard")
    parser.add_argument("--gate-delegate", default="human")
    parser.add_argument("--model-profile-id", default="")
    parser.add_argument("--draft", action="store_true")
    parser.add_argument("--rc-tag", default="")
    parser.add_argument("--v1-manifest", type=Path)
    parser.add_argument("--gate-manifest", type=Path)
    parser.add_argument("--recovery-manifest", type=Path)
    parser.add_argument("--smoke-manifest", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / ".artifacts/rc-candidate/manifest.json",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output = args.output.resolve()
    if output.exists():
        raise ManifestError(f"RC manifest already exists: {output}")
    source_sha = run("git", "rev-parse", "HEAD")
    source_tree = run("git", "rev-parse", "HEAD^{tree}")
    tracked_status = run("git", "status", "--porcelain=v1", "--untracked-files=no")
    if tracked_status:
        raise ManifestError("tracked worktree is dirty; refusing to collect RC manifest")

    config_path = ROOT / "forwin/config.py"
    policy_path = ROOT / "forwin/runtime/policy.py"
    telemetry_path = ROOT / "forwin/writer/llm/telemetry.py"
    mcp_models_path = ROOT / "forwin/mcp/models.py"
    baseline_path = ROOT / "forwin/migrations/versions/0001_v5_baseline.py"
    config_values = literal_assignments(config_path)
    assert_routing_defaults(config_path)
    missing_defaults = [name for name in MODEL_DEFAULT_NAMES if name not in config_values]
    if missing_defaults:
        raise ManifestError(f"model defaults missing from config: {missing_defaults}")
    telemetry_values = literal_assignments(telemetry_path)
    route_policy_version = telemetry_values.get("_LLM_ROUTE_POLICY_VERSION")
    if route_policy_version is None:
        raise ManifestError("_LLM_ROUTE_POLICY_VERSION is missing")

    runtime_image = inspect_image(args.runtime_image, source_sha)
    browser_image = inspect_image(args.browser_image, source_sha)
    dependency_images = {
        "postgres": inspect_dependency_image(args.postgres_image),
        "qdrant": inspect_dependency_image(args.qdrant_image),
        "minio": inspect_dependency_image(args.minio_image),
    }
    model_environment = inspect_model_environments(
        args.runtime_container,
        runtime_image["image_id"],
        model_profile_id=args.model_profile_id,
    )
    matrix_evidence = load_matrix_manifest(
        args.matrix_manifest if args.draft else args.matrix_audit_manifest,
        source_sha,
        require_final_audit=not args.draft,
    )

    component_revisions = {
        "model_routing": tree_revision(
            config_path,
            policy_path,
            ROOT / "forwin/llm",
            ROOT / "forwin/writer/llm",
        ),
        "prompt_runtime": tree_revision(
            ROOT / "forwin/genesis/workspace/prompts.py",
            ROOT / "forwin/writer/prompt_core",
            ROOT / "forwin/skills/prompt_layer.py",
            ROOT / "forwin_skills",
        ),
        "skill_registry": tree_revision(
            ROOT / "forwin_skills",
            ROOT / "forwin/skills",
            ROOT / ".agents/skills/forwin-operator",
        ),
        "report_tools": tree_revision(
            ROOT / "forwin/audit",
            ROOT / "forwin/mcp",
        ),
    }
    release_harness = tracked_source_revision(
        source_sha,
        RELEASE_HARNESS_PATHS,
    )

    manifest: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "collector_version": COLLECTOR_VERSION,
        "collected_at": datetime.now(UTC).isoformat(),
        "source": {
            "sha": source_sha,
            "tree": source_tree,
            "commit_time": run("git", "show", "-s", "--format=%cI", "HEAD"),
            "branch": run("git", "branch", "--show-current"),
            "tags_at_head": run("git", "tag", "--points-at", "HEAD").splitlines(),
            "tracked_worktree_clean": True,
        },
        "images": {
            "runtime": runtime_image,
            "publisher_browser": browser_image,
            **dependency_images,
        },
        "baseline_schema": {
            "path": relative(baseline_path),
            "sha256": sha256_file(baseline_path),
        },
        "runtime_policy": {
            "schema_version": class_literal(
                policy_path, "RuntimePolicy", "schema_version"
            ),
            "source_sha256": sha256_file(policy_path),
            "quality_profile": args.quality_profile,
            "gate_delegate": args.gate_delegate,
            "model_profile_id": args.model_profile_id,
        },
        "model_profiles": {
            "defaults": {
                name: config_values[name] for name in MODEL_DEFAULT_NAMES
            },
            "route_policy_version": route_policy_version,
            "effective_container_fields": model_environment,
            "revision": component_revisions["model_routing"],
        },
        "prompt_revision": component_revisions["prompt_runtime"],
        "skill_registry_revision": component_revisions["skill_registry"],
        "report_tools": {
            "schema_versions": report_schema_versions(mcp_models_path),
            "revision": component_revisions["report_tools"],
            "matrix_harness_sha256": matrix_evidence["harness"]["sha256"],
            "packages": package_versions(),
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "git": run("git", "--version"),
            "docker": run("docker", "--version"),
        },
        "release_harness": release_harness,
        "matrix_evidence": matrix_evidence,
        "additional_evidence": read_optional_evidence(args.evidence),
        "release_candidate": collect_release_candidate(
            args,
            source_sha,
            expected_gate_identity={
                "source_tree": source_tree,
                "runtime_image": runtime_image,
                "browser_image": browser_image,
            },
            expected_candidate_identity={
                "source_tree": source_tree,
                "runtime_image": runtime_image,
                "browser_image": browser_image,
                "dependency_images": dependency_images,
            },
        ),
        "freeze_contract": {
            "code_changes_during_run": 0,
            "prompt_changes_during_run": 0,
            "model_routing_changes_during_run": 0,
            "config_changes_during_run": 0,
            "schema_changes_during_run": 0,
            "rule_promotions_during_run": 0,
            "threshold_changes_during_run": 0,
        },
        "collector": {
            "path": relative(Path(__file__).resolve()),
            "sha256": sha256_file(Path(__file__).resolve()),
        },
    }
    atomic_write(output, manifest)
    print(str(output))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ManifestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
