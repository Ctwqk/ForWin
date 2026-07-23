#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / ".artifacts/v5-rc-gates"
RUFF_VERSION = "0.15.22"
HOST_ENV_ALLOWLIST = frozenset(
    {
        "FORWIN_TEST_ADMIN_DATABASE",
        "FORWIN_TEST_DATABASE_URL",
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
        "COVERAGE_PROCESS_START",
        "COVERAGE_RCFILE",
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
        "PYTHONHOME",
        "PYTHONINSPECT",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "PYTEST_ADDOPTS",
        "PYTEST_PLUGINS",
        "UV_CACHE_DIR",
        "UV_CONFIG_FILE",
        "UV_NO_CONFIG",
        "UV_OFFLINE",
        "UV_PROJECT",
        "UV_PROJECT_ENVIRONMENT",
        "UV_PYTHON",
        "UV_PYTHON_PREFERENCE",
        "UV_TOOL_BIN_DIR",
        "UV_TOOL_DIR",
        "UV_WORKING_DIR",
    }
)


V1_TESTS = (
    "tests/test_v5_live_migration.py",
    "tests/test_v5_recovery_schema.py",
    "tests/test_runtime_container_roles.py",
    "tests/test_runtime_worker_roles.py",
    "tests/test_api_system_routes.py::test_health_reports_current_embedding_backend_status",
    "tests/test_mcp_server.py::ForWinMCPIntegrationTests::test_health_endpoint_reports_upstream_ok",
    "tests/test_mcp_server.py::ForWinMCPIntegrationTests::test_project_create_and_genesis_get_via_mcp",
    "tests/test_mcp_server.py::ForWinMCPIntegrationTests::test_start_writing_continue_conflict_and_pause_via_mcp",
    "tests/test_genesis_handoff_service.py",
    "tests/test_project_policy_api.py",
    "tests/test_candidate_draft_records.py",
    "tests/test_book_state_final.py",
    "tests/test_projection_outbox.py",
)

V2_TESTS = (
    "tests/test_canon_atomic_transaction.py",
    "tests/test_generation_worker_canon_recovery.py",
    "tests/test_outbox_leases.py",
)

V3_TESTS = (
    "tests/test_gate_delegation.py",
    "tests/test_gate_delegation_chapter.py",
    "tests/test_generation_task_payload.py",
)

V5_TESTS = (
    "tests/test_lazy_external_indexes.py",
    "tests/test_projection_outbox.py",
    "tests/test_projection_checkpoints.py",
    "tests/test_qdrant_projection_convergence.py",
    "tests/test_post_canon_maintenance.py",
    "tests/test_post_canon_maintenance_api.py",
    "tests/test_canon_publisher_jobs.py",
    "tests/test_publisher_attempt_leases.py",
    "tests/test_publisher_risk_pause.py",
    "tests/test_publisher_receipts.py",
    "tests/test_publisher_idempotency.py",
    "tests/test_publisher_reconciliation_api.py",
    "tests/test_publisher_runtime_covers.py",
    "tests/test_publisher_worker_cli.py",
    "tests/test_runtime_worker_roles.py",
)


class GateError(RuntimeError):
    pass


def gate_environment(
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    values = os.environ if source is None else source
    control = sorted(
        key for key, value in values.items() if value and key in CONTROL_ENV_KEYS
    )
    if control:
        raise GateError(
            "release gate control environment is not allowed: "
            + ", ".join(control)
        )
    environment = {
        key: value
        for key, value in values.items()
        if key in HOST_ENV_ALLOWLIST
    }
    environment.update(
        {
            "PYTHONNOUSERSITE": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "UV_OFFLINE": "1",
        }
    )
    return environment


def now() -> str:
    return datetime.now(UTC).isoformat()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def command_output(*args: str) -> str:
    completed = subprocess.run(
        args,
        cwd=ROOT,
        env=gate_environment(),
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise GateError(f"command failed ({' '.join(args)}): {detail}")
    return completed.stdout.strip()


def image_identity(
    tag: str,
    *,
    source_sha: str,
    expected_image_id: str,
) -> dict[str, str]:
    try:
        payload = json.loads(command_output("docker", "image", "inspect", tag))
    except json.JSONDecodeError as exc:
        raise GateError(f"docker returned invalid JSON for {tag}") from exc
    if len(payload) != 1:
        raise GateError(f"expected one image for {tag}, got {len(payload)}")
    item = payload[0]
    labels = (item.get("Config") or {}).get("Labels") or {}
    revision = str(labels.get("org.opencontainers.image.revision") or "")
    image_id = str(item.get("Id") or "")
    if revision != source_sha:
        raise GateError(
            f"image {tag} revision {revision or '<missing>'} does not match "
            f"{source_sha}"
        )
    if image_id != expected_image_id:
        raise GateError(
            f"image {tag} id {image_id or '<missing>'} does not match "
            f"{expected_image_id}"
        )
    return {
        "tag": tag,
        "image_id": image_id,
        "revision": revision,
    }


def assert_frozen(rc_manifest_path: Path) -> dict[str, Any]:
    if not rc_manifest_path.is_file():
        raise GateError(f"RC manifest is missing: {rc_manifest_path}")
    try:
        rc_manifest = json.loads(rc_manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise GateError(f"RC manifest is invalid JSON: {rc_manifest_path}") from exc
    if not isinstance(rc_manifest, dict):
        raise GateError("RC manifest must contain an object")
    expected_source_sha = str((rc_manifest.get("source") or {}).get("sha") or "")
    if not expected_source_sha:
        raise GateError("RC manifest has no source SHA")
    source_sha = command_output("git", "rev-parse", "HEAD")
    if source_sha != expected_source_sha:
        raise GateError("RC manifest source SHA does not match HEAD")
    if command_output("git", "status", "--porcelain=v1", "--untracked-files=no"):
        raise GateError("tracked worktree is dirty")
    images = rc_manifest.get("images") or {}

    def verified_image(key: str) -> dict[str, str]:
        expected = images.get(key) or {}
        tag = str(expected.get("tag") or "")
        image_id = str(expected.get("image_id") or "")
        revision = str(expected.get("revision") or "")
        if not tag or not image_id or revision != source_sha:
            raise GateError(f"RC manifest image identity is incomplete: {key}")
        return image_identity(
            tag,
            source_sha=source_sha,
            expected_image_id=image_id,
        )

    return {
        "source_sha": source_sha,
        "source_tree": command_output("git", "rev-parse", "HEAD^{tree}"),
        "runtime_image": verified_image("runtime"),
        "browser_image": verified_image("publisher_browser"),
        "rc_manifest": {
            "path": str(rc_manifest_path.resolve()),
            "sha256": sha256_file(rc_manifest_path),
        },
    }


def pytest_step(name: str, tests: tuple[str, ...]) -> dict[str, Any]:
    return {
        "name": name,
        "kind": "pytest",
        "command": [
            "uv",
            "run",
            "--offline",
            "python",
            "-m",
            "pytest",
            "-p",
            "pytest_asyncio.plugin",
            "-q",
            "--tb=short",
            *tests,
        ],
        "junit": f"{name}.xml",
    }


def gate_steps() -> list[dict[str, Any]]:
    return [
        pytest_step("v1-fresh-schema", V1_TESTS),
        pytest_step("v2-canon-recovery", V2_TESTS),
        pytest_step("v3-spark-boundary", V3_TESTS),
        pytest_step("v5-projection-publisher-recovery", V5_TESTS),
        pytest_step("full-suite", ("tests",)),
        {
            "name": "ruff",
            "kind": "command",
            "command": [
                "uvx",
                "--offline",
                f"ruff=={RUFF_VERSION}",
                "check",
                "forwin",
                "tests",
            ],
        },
        {
            "name": "compileall",
            "kind": "command",
            "command": [
                "uv",
                "run",
                "--offline",
                "python",
                "-m",
                "compileall",
                "-q",
                "forwin",
                "tests",
            ],
        },
        {
            "name": "diff-check",
            "kind": "command",
            "command": ["git", "diff", "--check"],
        },
    ]


def selected_steps(names: list[str]) -> list[dict[str, Any]]:
    steps = gate_steps()
    if not names:
        return steps
    known = {step["name"] for step in steps}
    unknown = sorted(set(names) - known)
    if unknown:
        raise GateError(f"unknown gate steps: {unknown}")
    selected = set(names)
    return [step for step in steps if step["name"] in selected]


def atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def run_step(step: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    name = str(step["name"])
    log_path = output_dir / f"{name}.log"
    command = [str(item) for item in step["command"]]
    junit_name = step.get("junit")
    if junit_name:
        junit_path = output_dir / str(junit_name)
        command.append(f"--junitxml={junit_path}")
    started_at = now()
    started = time.monotonic()
    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"started_at={started_at}\n")
        log.write("command=" + json.dumps(command, ensure_ascii=False) + "\n")
        log.flush()
        completed = subprocess.run(
            command,
            cwd=ROOT,
            env=gate_environment(),
            check=False,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
        )
    result: dict[str, Any] = {
        "name": name,
        "kind": step["kind"],
        "command": command,
        "started_at": started_at,
        "completed_at": now(),
        "duration_seconds": round(time.monotonic() - started, 3),
        "exit_code": completed.returncode,
        "passed": completed.returncode == 0,
        "log": {
            "path": str(log_path.resolve()),
            "sha256": sha256_file(log_path),
        },
    }
    if junit_name:
        junit_path = output_dir / str(junit_name)
        result["junit"] = {
            "path": str(junit_path.resolve()),
            "exists": junit_path.is_file(),
            "sha256": sha256_file(junit_path) if junit_path.is_file() else "",
        }
    return result


def run_gates(args: argparse.Namespace) -> int:
    rc_manifest = args.rc_manifest.resolve()
    identity = assert_frozen(rc_manifest)
    runner_identity = {
        "path": str(Path(__file__).resolve()),
        "sha256": sha256_file(Path(__file__).resolve()),
    }
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "manifest.json"
    if manifest_path.exists() and not args.resume:
        raise GateError(
            f"gate manifest already exists: {manifest_path}; pass --resume to append"
        )
    manifest: dict[str, Any]
    if args.resume and manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("release_gate_passed") is True:
            raise GateError(
                f"gate manifest already passed and is sealed: {manifest_path}"
            )
        if manifest.get("identity") != identity:
            raise GateError("existing gate manifest identity does not match current RC")
        if manifest.get("runner") != runner_identity:
            raise GateError("existing gate manifest runner identity has drifted")
    else:
        manifest = {
            "schema_version": 1,
            "started_at": now(),
            "identity": identity,
            "runner": runner_identity,
            "steps": [],
        }
    configured_steps = gate_steps()
    selected = selected_steps(args.step)
    completed_names = {str(item.get("name")) for item in manifest["steps"]}
    for step in selected:
        if step["name"] in completed_names:
            continue
        result = run_step(step, output_dir)
        manifest["steps"].append(result)
        atomic_write(manifest_path, manifest)
        print(
            json.dumps(
                {
                    "name": result["name"],
                    "passed": result["passed"],
                    "duration_seconds": result["duration_seconds"],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    manifest["completed_at"] = now()
    results_by_name = {
        str(item.get("name")): item for item in manifest["steps"]
    }
    required_names = [str(step["name"]) for step in configured_steps]
    selected_names = [str(step["name"]) for step in selected]
    manifest["required_step_names"] = required_names
    manifest["selected_step_names"] = selected_names
    manifest["completed_step_names"] = sorted(results_by_name)
    manifest["selection_passed"] = bool(selected_names) and all(
        bool(results_by_name.get(name, {}).get("passed")) for name in selected_names
    )
    manifest["all_steps_completed"] = set(required_names) <= set(results_by_name)
    manifest["release_gate_passed"] = bool(manifest["all_steps_completed"]) and all(
        bool(results_by_name[name].get("passed")) for name in required_names
    )
    atomic_write(manifest_path, manifest)
    return 0 if manifest["selection_passed"] else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run and retain the immutable ForWin v5 RC gates."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    run_parser = commands.add_parser("run")
    run_parser.add_argument("--rc-manifest", type=Path, required=True)
    run_parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    run_parser.add_argument("--step", action="append", default=[])
    run_parser.add_argument("--resume", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "list":
        print(json.dumps(gate_steps(), ensure_ascii=False, indent=2))
        return 0
    return run_gates(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except GateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
