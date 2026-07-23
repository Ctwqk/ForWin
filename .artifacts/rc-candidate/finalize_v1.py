#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


EXPECTED_RUNTIME_SERVICES = frozenset(
    {
        "forwin",
        "generation-worker",
        "outbox-worker",
        "forwin-mcp",
        "publisher-worker",
    }
)
EXPECTED_BROWSER_SERVICES = frozenset({"publisher-browser"})
EXPECTED_DEPENDENCY_SERVICES = frozenset({"postgres", "qdrant", "minio"})
EXPECTED_SERVICES = (
    EXPECTED_RUNTIME_SERVICES
    | EXPECTED_BROWSER_SERVICES
    | EXPECTED_DEPENDENCY_SERVICES
)
EXPECTED_HEALTHCHECK_SERVICES = frozenset(
    {"postgres", "qdrant", "forwin", "forwin-mcp", "publisher-browser"}
)
MIGRATION_STEPS = (
    "upgrade_head_initial",
    "alembic_check",
    "downgrade_base",
    "upgrade_head_final",
)
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / ".artifacts/v1-release-gate/manifest.json"
CONTROLLER_PATH = Path(__file__).with_name("recovery_stack.py").resolve()
EXPECTED_HARNESS_PATHS = {
    "controller": CONTROLLER_PATH,
    "compose_file": (ROOT / "docker-compose.yml").resolve(),
    "compose_override": Path(__file__).with_name(
        "docker-compose.recovery.yml"
    ).resolve(),
    "finalizer": Path(__file__).resolve(),
}


class V1EvidenceError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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


def load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise V1EvidenceError(f"required JSON is missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise V1EvidenceError(f"invalid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise V1EvidenceError(f"expected an object in {path}")
    return payload


def load_verified_events(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise V1EvidenceError(f"V1 event log is missing: {path}") from exc
    events: list[dict[str, Any]] = []
    previous = "0" * 64
    for line_number, raw in enumerate(lines, start=1):
        try:
            event = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise V1EvidenceError(
                f"invalid V1 event JSON at line {line_number}"
            ) from exc
        if not isinstance(event, dict):
            raise V1EvidenceError(f"V1 event {line_number} is not an object")
        if event.get("previous_event_sha256") != previous:
            raise V1EvidenceError(
                f"V1 event chain mismatch at line {line_number}"
            )
        actual_hash = str(event.get("event_sha256") or "")
        if not actual_hash or event_hash(event) != actual_hash:
            raise V1EvidenceError(f"V1 event hash mismatch at line {line_number}")
        previous = actual_hash
        events.append(event)
    if not events:
        raise V1EvidenceError("V1 event log is empty")
    return events


def normalized_time(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value or ""))
    return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)


def is_private_lan_url(value: object) -> bool:
    parsed = urlsplit(str(value or ""))
    try:
        address = ipaddress.ip_address(parsed.hostname or "")
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and address.is_private
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_unspecified
    )


def _completed_event(events: list[dict[str, Any]]) -> dict[str, Any]:
    completed = [
        event for event in events if event.get("action") == "v1_preflight_completed"
    ]
    if len(completed) != 1:
        return {}
    return completed[0]


def preflight_violations(
    candidate: dict[str, Any],
    events: list[dict[str, Any]],
) -> list[str]:
    violations: list[str] = []
    source_sha = str((candidate.get("source") or {}).get("sha") or "")
    images = candidate.get("images") or {}
    runtime_image = images.get("runtime") or {}
    browser_image = images.get("publisher_browser") or {}
    dependency_images = {
        service: images.get(service) or {}
        for service in EXPECTED_DEPENDENCY_SERVICES
    }
    if not source_sha:
        violations.append("candidate source SHA is missing")
    for name, image in (
        ("runtime", runtime_image),
        ("publisher_browser", browser_image),
    ):
        if (
            not str(image.get("image_id") or "")
            or image.get("revision") != source_sha
        ):
            violations.append(f"candidate {name} image identity is incomplete")
    for name, image in dependency_images.items():
        if not str(image.get("tag") or "") or not str(
            image.get("image_id") or ""
        ):
            violations.append(
                f"candidate dependency image identity is incomplete: {name}"
            )

    started = [
        event for event in events if event.get("action") == "v1_fresh_up_started"
    ]
    completed = [
        event for event in events if event.get("action") == "v1_preflight_completed"
    ]
    destroyed = [
        event for event in events if event.get("action") == "destroyed"
    ]
    if [str(event.get("action") or "") for event in events] != [
        "v1_fresh_up_started",
        "v1_preflight_completed",
        "destroyed",
    ]:
        violations.append("V1 event sequence is not start/completion/destroyed")
    if len(started) != 1:
        violations.append(f"V1 start event count={len(started)}, expected=1")
    if len(completed) != 1:
        violations.append(f"V1 completion event count={len(completed)}, expected=1")
        return violations
    if len(destroyed) != 1:
        violations.append(f"V1 destroy event count={len(destroyed)}, expected=1")
    completion = completed[0]
    run_ids = {
        str(event.get("run_id") or "")
        for event in (*started, *completed, *destroyed)
    }
    if len(run_ids) != 1 or "" in run_ids:
        violations.append("V1 run identity mismatch")
    if started:
        if started[0].get("identity") != completion.get("identity"):
            violations.append("V1 start/completion identity mismatch")
        try:
            candidate_time = normalized_time(candidate.get("collected_at"))
            if normalized_time(started[0].get("recorded_at")) <= candidate_time:
                violations.append("V1 run did not start after candidate collection")
            if normalized_time(completion.get("recorded_at")) <= normalized_time(
                started[0].get("recorded_at")
            ):
                violations.append("V1 completion timestamp is not after start")
        except ValueError:
            violations.append("V1 event timestamp is invalid")
    if destroyed:
        if destroyed[0].get("identity") != completion.get("identity"):
            violations.append("V1 completion/destroy identity mismatch")
        try:
            if normalized_time(destroyed[0].get("recorded_at")) <= normalized_time(
                completion.get("recorded_at")
            ):
                violations.append("V1 destroy timestamp is not after completion")
        except ValueError:
            violations.append("V1 destroy timestamp is invalid")
        destroyed_services = ((destroyed[0].get("after") or {}).get("services") or {})
        if set(destroyed_services) != set(EXPECTED_SERVICES):
            violations.append("V1 destroyed service set mismatch")
        for service in EXPECTED_SERVICES & set(destroyed_services):
            state = destroyed_services.get(service) or {}
            if state.get("exists") is not False or state.get("running") is True:
                violations.append(
                    f"V1 destroyed service still exists or runs: {service}"
                )

    identity = completion.get("identity") or {}
    if identity.get("source_sha") != source_sha:
        violations.append("V1 completion source SHA mismatch")
    if identity.get("source_tree") != (candidate.get("source") or {}).get(
        "tree"
    ):
        violations.append("V1 completion source tree mismatch")
    docker = identity.get("docker") or {}
    if (
        not str(docker.get("context") or "")
        or not str(docker.get("endpoint") or "").startswith("unix://")
        or not str(docker.get("daemon_id") or "")
    ):
        violations.append("V1 Docker execution identity is incomplete")
    candidate_artifact = identity.get("candidate_manifest") or {}
    candidate_path = Path(str(candidate_artifact.get("path") or ""))
    if not candidate_path.is_file():
        violations.append("V1 candidate manifest artifact is missing")
    elif sha256_file(candidate_path) != candidate_artifact.get("sha256"):
        violations.append("V1 candidate manifest artifact hash mismatch")
    release_files = {
        str(item.get("path") or ""): str(item.get("sha256") or "")
        for item in (candidate.get("release_harness") or {}).get("files") or []
        if isinstance(item, dict)
    }
    harness = identity.get("harness")
    if not isinstance(harness, dict) or set(harness) != set(
        EXPECTED_HARNESS_PATHS
    ):
        violations.append("V1 harness identity set mismatch")
        harness = harness if isinstance(harness, dict) else {}
    for key, expected_path in EXPECTED_HARNESS_PATHS.items():
        artifact = harness.get(key) or {}
        actual_path = Path(str(artifact.get("path") or "")).resolve()
        if actual_path != expected_path:
            violations.append(f"V1 harness path mismatch: {key}")
        if (
            not actual_path.is_file()
            or sha256_file(actual_path) != artifact.get("sha256")
        ):
            violations.append(f"V1 harness hash mismatch: {key}")
        try:
            source_path = actual_path.relative_to(ROOT).as_posix()
        except ValueError:
            violations.append(f"V1 harness is outside source tree: {key}")
        else:
            if release_files.get(source_path) != artifact.get("sha256"):
                violations.append(
                    f"V1 harness is not bound by candidate source: {key}"
                )

    migration = completion.get("migration_cycle") or {}
    steps = migration.get("steps")
    if not isinstance(steps, list):
        steps = []
    names = [str(item.get("name") or "") for item in steps if isinstance(item, dict)]
    if names != list(MIGRATION_STEPS):
        violations.append(f"migration step sequence mismatch: {names}")
    if any(
        int(item.get("exit_code", -1)) != 0
        for item in steps
        if isinstance(item, dict)
    ):
        violations.append("migration cycle contains a failed step")
    final_revision = str(migration.get("final_revision") or "")
    if not final_revision:
        violations.append("migration final revision is missing")

    stale = completion.get("stale_schema_failfast") or {}
    if stale.get("role") != "generation-worker":
        violations.append("stale schema fail-fast did not exercise generation-worker")
    if stale.get("injected_revision") != "v1_stale_revision":
        violations.append("stale schema revision marker mismatch")
    if int(stale.get("startup_exit_code") or 0) == 0:
        violations.append("stale schema role startup unexpectedly succeeded")
    if stale.get("error_code") != "FORWIN_SCHEMA_REVISION_MISMATCH":
        violations.append("stale schema error classification mismatch")
    if stale.get("expected_error_observed") is not True:
        violations.append("stale schema startup did not report schema mismatch")
    startup_log = stale.get("startup_log") or {}
    startup_log_path = Path(str(startup_log.get("path") or ""))
    if (
        not startup_log_path.is_file()
        or sha256_file(startup_log_path) != startup_log.get("sha256")
    ):
        violations.append("stale schema startup log artifact mismatch")
    else:
        expected_pattern = re.compile(
            r"\[FORWIN_SCHEMA_REVISION_MISMATCH\] "
            r"ForWin database schema is v1_stale_revision, expected "
            + re.escape(final_revision)
            + r"\."
        )
        if expected_pattern.search(
            startup_log_path.read_text(encoding="utf-8")
        ) is None:
            violations.append("stale schema startup log classification mismatch")
    task_state_before = stale.get("task_state_before")
    task_state_after = stale.get("task_state_after")
    if (
        task_state_before != {"total": 0, "leased": 0}
        or task_state_after != task_state_before
    ):
        violations.append("stale schema probe changed generation task state")
    if str(stale.get("restored_revision") or "") != final_revision:
        violations.append("schema revision was not restored to migration head")
    if int(stale.get("post_restore_exit_code", -1)) != 0:
        violations.append("generation-worker did not start after schema restore")

    after = completion.get("after") or {}
    services = after.get("services")
    if not isinstance(services, dict):
        services = {}
    if set(services) != set(EXPECTED_SERVICES):
        violations.append(
            "service set mismatch: "
            f"{sorted(services)}, expected={sorted(EXPECTED_SERVICES)}"
        )
    for service in sorted(EXPECTED_SERVICES & set(services)):
        item = services.get(service) or {}
        if item.get("exists") is not True or item.get("running") is not True:
            violations.append(f"service {service} is not running")
        health = str(item.get("health") or "")
        if (
            service in EXPECTED_HEALTHCHECK_SERVICES
            and health != "healthy"
        ):
            violations.append(f"service {service} health={item.get('health')}")
        if (
            service not in EXPECTED_HEALTHCHECK_SERVICES
            and health not in {"", "healthy"}
        ):
            violations.append(f"service {service} health={item.get('health')}")
        probe = item.get("probe") or {}
        if probe.get("passed") is not True or int(
            probe.get("exit_code", -1)
        ) != 0:
            violations.append(f"service {service} functional probe did not pass")
        if (
            service in EXPECTED_RUNTIME_SERVICES
            and item.get("image_id") != runtime_image.get("image_id")
        ):
            violations.append(f"runtime service {service} image mismatch")
        if (
            service in EXPECTED_BROWSER_SERVICES
            and item.get("image_id") != browser_image.get("image_id")
        ):
            violations.append(f"publisher browser image mismatch: {service}")
        if (
            service in EXPECTED_DEPENDENCY_SERVICES
            and item.get("image_id")
            != (dependency_images.get(service) or {}).get("image_id")
        ):
            violations.append(f"dependency service {service} image mismatch")

    embedding = completion.get("embedding") or {}
    configured_dims = int(embedding.get("configured_dims") or 0)
    metadata_dims = int(embedding.get("metadata_dims") or 0)
    vector_dims = embedding.get("vector_dims")
    if embedding.get("backend") != "gateway":
        violations.append("embedding backend is not gateway")
    if embedding.get("required") is not True:
        violations.append("embedding gateway is not required")
    if not is_private_lan_url(embedding.get("base_url")):
        violations.append("embedding gateway is not a private LAN address")
    try:
        gateway_address = ipaddress.ip_address(
            urlsplit(str(embedding.get("base_url") or "")).hostname or ""
        )
        peer_address = ipaddress.ip_address(
            str(embedding.get("peer_ip") or "")
        )
        if gateway_address != peer_address:
            violations.append("embedding TCP peer differs from gateway address")
    except ValueError:
        violations.append("embedding TCP peer is invalid")
    if not str(embedding.get("model") or ""):
        violations.append("embedding model is missing")
    if configured_dims <= 0 or metadata_dims != configured_dims:
        violations.append("embedding metadata dimension mismatch")
    if int(embedding.get("vector_count") or 0) != 1:
        violations.append("embedding smoke did not return exactly one vector")
    if (
        not isinstance(vector_dims, list)
        or vector_dims != [configured_dims]
    ):
        violations.append("embedding vector dimensions do not match configuration")
    return violations


def build_manifest(
    *,
    candidate: dict[str, Any],
    candidate_path: Path,
    events_path: Path,
) -> dict[str, Any]:
    events = load_verified_events(events_path)
    violations = preflight_violations(candidate, events)
    completion = _completed_event(events)
    return {
        "schema_version": 1,
        "source_sha": str((candidate.get("source") or {}).get("sha") or ""),
        "result": "pass" if not violations else "fail",
        "violations": violations,
        "identity": {
            "candidate_manifest": {
                "path": str(candidate_path.resolve()),
                "sha256": sha256_file(candidate_path),
            },
            "stack_events": {
                "path": str(events_path.resolve()),
                "sha256": sha256_file(events_path),
            },
        },
        "migration_cycle": completion.get("migration_cycle") or {},
        "stale_schema_failfast": completion.get("stale_schema_failfast") or {},
        "embedding": completion.get("embedding") or {},
        "services": ((completion.get("after") or {}).get("services") or {}),
        "harness": ((completion.get("identity") or {}).get("harness") or {}),
        "auditor": {
            "path": str(Path(__file__).resolve()),
            "sha256": sha256_file(Path(__file__).resolve()),
            "controller": {
                "path": str(CONTROLLER_PATH),
                "sha256": sha256_file(CONTROLLER_PATH),
            },
        },
    }


def _artifact_violations(
    artifact: object,
    *,
    label: str,
) -> list[str]:
    if not isinstance(artifact, dict):
        return [f"{label} artifact is missing"]
    path = Path(str(artifact.get("path") or ""))
    if not path.is_file():
        return [f"{label} artifact is missing"]
    if sha256_file(path) != artifact.get("sha256"):
        return [f"{label} hash mismatch"]
    return []


def v1_manifest_violations(
    manifest: dict[str, Any],
    *,
    source_sha: str,
) -> list[str]:
    violations: list[str] = []
    if int(manifest.get("schema_version") or 0) != 1:
        violations.append("V1 manifest schema_version is not 1")
    if manifest.get("source_sha") != source_sha:
        violations.append("V1 manifest source SHA mismatch")
    if manifest.get("result") != "pass":
        violations.append("V1 manifest result is not pass")
    if manifest.get("violations") != []:
        violations.append("V1 manifest contains violations")
    identity = manifest.get("identity") or {}
    candidate_artifact = identity.get("candidate_manifest")
    events_artifact = identity.get("stack_events")
    violations.extend(
        _artifact_violations(candidate_artifact, label="candidate manifest")
    )
    violations.extend(_artifact_violations(events_artifact, label="event log"))
    auditor = manifest.get("auditor") or {}
    violations.extend(
        _artifact_violations(auditor, label="V1 auditor")
    )
    if Path(str(auditor.get("path") or "")).resolve() != Path(__file__).resolve():
        violations.append("V1 auditor path mismatch")
    controller = auditor.get("controller")
    violations.extend(
        _artifact_violations(controller, label="V1 controller")
    )
    if (
        not isinstance(controller, dict)
        or Path(str(controller.get("path") or "")).resolve() != CONTROLLER_PATH
    ):
        violations.append("V1 controller path mismatch")
    report_artifact = manifest.get("report")
    violations.extend(_artifact_violations(report_artifact, label="V1 report"))
    if violations:
        return violations
    candidate_path = Path(str(candidate_artifact["path"]))
    events_path = Path(str(events_artifact["path"]))
    try:
        candidate = load_json(candidate_path)
        events = load_verified_events(events_path)
    except V1EvidenceError as exc:
        return [str(exc)]
    violations.extend(preflight_violations(candidate, events))
    completion = _completed_event(events)
    for key, nested_key in (
        ("migration_cycle", "migration_cycle"),
        ("stale_schema_failfast", "stale_schema_failfast"),
        ("embedding", "embedding"),
    ):
        if manifest.get(key) != completion.get(nested_key):
            violations.append(f"V1 manifest {key} differs from event evidence")
    services = ((completion.get("after") or {}).get("services") or {})
    if manifest.get("services") != services:
        violations.append("V1 manifest services differ from event evidence")
    harness = ((completion.get("identity") or {}).get("harness") or {})
    if manifest.get("harness") != harness:
        violations.append("V1 manifest harness differs from event evidence")
    report_path = Path(str((report_artifact or {}).get("path") or ""))
    if report_path.read_text(encoding="utf-8") != final_report(manifest):
        violations.append("V1 report content mismatch")
    return violations


def final_report(manifest: dict[str, Any]) -> str:
    migration = manifest.get("migration_cycle") or {}
    stale = manifest.get("stale_schema_failfast") or {}
    embedding = manifest.get("embedding") or {}
    services = manifest.get("services") or {}
    lines = [
        "# ForWin v5 V1 Fresh-schema Release Gate",
        "",
        f"- Result: {'PASS' if manifest['result'] == 'pass' else 'FAIL'}",
        f"- Source SHA: `{manifest['source_sha']}`",
        f"- Final schema revision: `{migration.get('final_revision') or 'missing'}`",
        f"- Stale-schema role: `{stale.get('role') or 'missing'}`",
        f"- Services running: `{len(services)}` / `{len(EXPECTED_SERVICES)}`",
        f"- Embedding gateway: `{embedding.get('base_url') or 'missing'}`",
        f"- Embedding model: `{embedding.get('model') or 'missing'}`",
        (
            "- Embedding dimensions: "
            f"`{embedding.get('vector_dims') or []}` "
            f"(configured `{embedding.get('configured_dims') or 0}`)"
        ),
        "",
        "## Migration Cycle",
        "",
        "| Step | Exit code |",
        "| --- | ---: |",
    ]
    for step in migration.get("steps") or []:
        lines.append(
            f"| `{step.get('name') or 'missing'}` | "
            f"{int(step.get('exit_code', -1))} |"
        )
    lines.extend(["", "## Services", "", "| Role | State | Health |", "| --- | --- | --- |"])
    for service in sorted(EXPECTED_SERVICES):
        state = services.get(service) or {}
        lines.append(
            f"| `{service}` | "
            f"{'running' if state.get('running') is True else 'not running'} | "
            f"{state.get('health') or 'unreported'} |"
        )
    lines.extend(["", "## Findings", ""])
    lines.extend(
        ["- All V1 preflight contracts passed."]
        if not manifest["violations"]
        else [f"- {item}" for item in manifest["violations"]]
    )
    lines.append("")
    return "\n".join(lines)


def atomic_write_text(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def atomic_write(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Finalize live ForWin v5 V1 preflight evidence."
    )
    parser.add_argument("--candidate-manifest", type=Path, required=True)
    parser.add_argument("--stack-events", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    candidate_path = args.candidate_manifest.resolve()
    events_path = args.stack_events.resolve()
    candidate = load_json(candidate_path)
    manifest = build_manifest(
        candidate=candidate,
        candidate_path=candidate_path,
        events_path=events_path,
    )
    output = args.output.resolve()
    report_path = output.with_name("report.md")
    if output.exists():
        raise V1EvidenceError(f"V1 manifest already exists: {output}")
    if report_path.exists():
        raise V1EvidenceError(f"V1 report already exists: {report_path}")
    atomic_write_text(report_path, final_report(manifest))
    manifest["report"] = {
        "path": str(report_path),
        "sha256": sha256_file(report_path),
    }
    atomic_write(output, manifest)
    print(str(output))
    return 0 if manifest["result"] == "pass" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except V1EvidenceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
