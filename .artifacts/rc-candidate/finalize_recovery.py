#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class RecoveryEvidenceError(RuntimeError):
    pass


EVALUATOR_PATH = Path(__file__).with_name("recovery_evidence.py").resolve()
_EVALUATOR_SPEC = importlib.util.spec_from_file_location(
    "forwin_recovery_evidence", EVALUATOR_PATH
)
if _EVALUATOR_SPEC is None or _EVALUATOR_SPEC.loader is None:
    raise RecoveryEvidenceError(f"cannot load semantic evaluator: {EVALUATOR_PATH}")
evaluator = importlib.util.module_from_spec(_EVALUATOR_SPEC)
_EVALUATOR_SPEC.loader.exec_module(evaluator)

# Compatibility alias for readers that enumerate fault kinds only.
FAULT_CONTRACTS = {kind: {} for kind in evaluator.FAULT_CONTRACTS}

SERVICE_FAULTS: dict[str, dict[str, str]] = {
    "generation_worker_precommit_crash": {
        "service": "generation-worker",
        "fault_action": "fault_service_killed",
        "fault_time_field": "crash_time",
    },
    "generation_worker_postcommit_crash": {
        "service": "generation-worker",
        "fault_action": "fault_service_killed",
        "fault_time_field": "crash_time",
    },
    "qdrant_unavailable": {
        "service": "qdrant",
        "fault_action": "fault_service_stopped",
        "fault_time_field": "fault_time",
    },
    "projection_consumer_unavailable": {
        "service": "outbox-worker",
        "fault_action": "fault_service_stopped",
        "fault_time_field": "fault_time",
    },
    "minio_pre_canon_unavailable": {
        "service": "minio",
        "fault_action": "fault_service_stopped",
        "fault_time_field": "fault_time",
    },
    "minio_post_canon_unavailable": {
        "service": "minio",
        "fault_action": "fault_service_stopped",
        "fault_time_field": "fault_time",
    },
    "publisher_backend_unavailable": {
        "service": "publisher-worker",
        "fault_action": "fault_service_killed",
        "fault_time_field": "crash_time",
    },
    "publisher_browser_unavailable": {
        "service": "publisher-browser",
        "fault_action": "fault_service_stopped",
        "fault_time_field": "fault_time",
    },
}

ROOT = Path(__file__).resolve().parents[2]
GATE_HELPER_PATH = Path(__file__).with_name("run_rc_gates.py")
RECOVERY_CONTROLLER_PATH = Path(__file__).with_name("recovery_stack.py")
DESTROY_SERVICES = frozenset(
    {
        "forwin",
        "forwin-mcp",
        "generation-worker",
        "minio",
        "outbox-worker",
        "postgres",
        "publisher-browser",
        "publisher-worker",
        "qdrant",
    }
)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_write(path: Path, body: str) -> None:
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


def write_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RecoveryEvidenceError(f"required JSON is missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RecoveryEvidenceError(f"invalid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise RecoveryEvidenceError(f"expected an object in {path}")
    return payload


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RecoveryEvidenceError(f"cannot load release helper: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def load_verified_events(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise RecoveryEvidenceError(f"recovery event log is missing: {path}") from exc
    events: list[dict[str, Any]] = []
    previous = "0" * 64
    for line_number, raw in enumerate(lines, start=1):
        try:
            event = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RecoveryEvidenceError(
                f"invalid recovery event JSON at line {line_number}"
            ) from exc
        if not isinstance(event, dict):
            raise RecoveryEvidenceError(
                f"recovery event {line_number} is not an object"
            )
        if event.get("previous_event_sha256") != previous:
            raise RecoveryEvidenceError(
                f"recovery event chain mismatch at line {line_number}"
            )
        actual = str(event.get("event_sha256") or "")
        if not actual or event_hash(event) != actual:
            raise RecoveryEvidenceError(
                f"recovery event hash mismatch at line {line_number}"
            )
        previous = actual
        events.append(event)
    if not events:
        raise RecoveryEvidenceError("recovery event log is empty")
    return events


def normalized_time(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value or ""))
    return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)


def run_resource_violations(
    kind: str,
    *,
    fault_id: str,
    events: list[dict[str, Any]],
    event_path: Path,
    artifact_paths: dict[str, Path],
) -> tuple[list[str], dict[str, str]]:
    violations: list[str] = []
    expected_run_keys = {
        "run_id",
        "evidence_directory",
        "database_volume_name",
    }
    run_identities = [event.get("run_identity") for event in events]
    run_identity = (
        run_identities[0] if run_identities and isinstance(run_identities[0], dict)
        else {}
    )
    if (
        not run_identity
        or set(run_identity) != expected_run_keys
        or any(identity != run_identity for identity in run_identities)
        or not str(run_identity.get("run_id") or "")
        or not str(run_identity.get("database_volume_name") or "")
    ):
        violations.append(f"{kind}.run identity mismatch")
        return violations, {}

    raw_directory = Path(str(run_identity["evidence_directory"]))
    canonical_directory = raw_directory.resolve()
    if (
        not raw_directory.is_absolute()
        or str(raw_directory) != str(canonical_directory)
    ):
        violations.append(f"{kind}.evidence directory is not canonical")
    if event_path.resolve().parent != canonical_directory:
        violations.append(f"{kind}.event log directory mismatch")
    for stage, path in artifact_paths.items():
        if path.resolve().parent != canonical_directory:
            violations.append(f"{kind}.{stage} artifact directory mismatch")

    volume_name = str(run_identity["database_volume_name"])

    def absent(value: object) -> bool:
        return (
            isinstance(value, dict)
            and set(value) == {"name", "exists"}
            and value.get("name") == volume_name
            and value.get("exists") is False
        )

    def present(value: object) -> bool:
        if (
            not isinstance(value, dict)
            or set(value)
            != {"name", "exists", "created_at", "fingerprint"}
            or value.get("name") != volume_name
            or value.get("exists") is not True
            or not isinstance(value.get("created_at"), str)
            or not value.get("created_at")
            or not isinstance(value.get("fingerprint"), str)
            or not value.get("fingerprint")
        ):
            return False
        try:
            normalized_time(value["created_at"])
        except ValueError:
            return False
        expected_fingerprint = evaluator.stable_hash(
            {
                "created_at": value["created_at"],
                "name": value["name"],
            }
        )
        return value["fingerprint"] == expected_fingerprint

    fresh_starts = [
        event for event in events if event.get("action") == "fresh_up_started"
    ]
    fresh_completions = [
        event for event in events if event.get("action") == "fresh_up_completed"
    ]
    destroyed_events = [
        event for event in events if event.get("action") == "destroyed"
    ]
    service_contract = SERVICE_FAULTS.get(kind)
    fault_action = (
        service_contract["fault_action"]
        if service_contract is not None
        else "fault_marked"
    )
    recovery_action = (
        "fault_service_recovered"
        if service_contract is not None
        else "recovery_marked"
    )
    fault_events = [
        event
        for event in events
        if event.get("fault_id") == fault_id
        and event.get("action") == fault_action
    ]
    recovery_events = [
        event
        for event in events
        if event.get("fault_id") == fault_id
        and event.get("action") == recovery_action
    ]
    lifecycle_groups = (
        fresh_starts,
        fresh_completions,
        fault_events,
        recovery_events,
        destroyed_events,
    )
    volume_valid = all(len(group) == 1 for group in lifecycle_groups)
    present_volume: dict[str, Any] = {}
    if volume_valid:
        present_volume = fresh_completions[0].get("database_volume") or {}
        volume_valid = (
            absent(fresh_starts[0].get("database_volume"))
            and present(present_volume)
            and all(
                event.get("database_volume") == present_volume
                for event in events
                if event is not fresh_starts[0]
                and event is not destroyed_events[0]
            )
            and destroyed_events[0].get("database_volume_before")
            == present_volume
            and absent(destroyed_events[0].get("database_volume"))
        )
    if not volume_valid:
        violations.append(f"{kind}.database volume lifecycle mismatch")
        return violations, {}

    return violations, {
        "run_id": str(run_identity["run_id"]),
        "evidence_directory": str(canonical_directory),
        "volume_name": volume_name,
        "volume_fingerprint": str(present_volume["fingerprint"]),
    }


def fault_report_violations(
    report: dict[str, Any],
    *,
    source_sha: str,
    candidate: dict[str, Any] | None = None,
) -> list[str]:
    violations: list[str] = []
    kind = str(report.get("fault_kind") or "")
    if int(report.get("schema_version") or 0) != 2:
        violations.append(f"{kind or 'unknown'}.schema_version is not 2")
    if kind not in FAULT_CONTRACTS:
        violations.append(f"unknown fault kind: {kind or '<missing>'}")
        return violations
    if report.get("source_sha") != source_sha:
        violations.append(f"{kind}.source_sha mismatch")
    if not str(report.get("fault_id") or ""):
        violations.append(f"{kind}.fault_id is missing")
    if report.get("result") != "pass":
        violations.append(f"{kind}.result={report.get('result')}, expected=pass")
    if report.get("replay_result") != "pass":
        violations.append(
            f"{kind}.replay_result={report.get('replay_result')}, expected=pass"
        )
    try:
        fault_time = normalized_time(report.get("fault_time"))
        recovery_time = normalized_time(report.get("recovery_time"))
        if recovery_time <= fault_time:
            violations.append(f"{kind}.recovery_time is not after fault_time")
    except ValueError:
        violations.append(f"{kind}.fault/recovery timestamp is invalid")

    evaluator_identity = report.get("evaluator") or {}
    if (
        Path(str(evaluator_identity.get("path") or "")).resolve()
        != EVALUATOR_PATH
        or evaluator_identity.get("sha256") != sha256_file(EVALUATOR_PATH)
    ):
        violations.append(f"{kind}.semantic evaluator identity mismatch")
    if candidate is not None:
        release_files = {
            str(item.get("path") or ""): str(item.get("sha256") or "")
            for item in (candidate.get("release_harness") or {}).get("files") or []
            if isinstance(item, dict)
        }
        evaluator_source_path = EVALUATOR_PATH.relative_to(ROOT).as_posix()
        if release_files.get(evaluator_source_path) != sha256_file(EVALUATOR_PATH):
            violations.append(
                f"{kind}.semantic evaluator is not bound by candidate source"
            )

    artifacts = report.get("artifacts")
    if not isinstance(artifacts, list):
        artifacts = []
    stages: set[str] = set()
    snapshots: dict[str, dict[str, Any]] = {}
    artifact_paths: dict[str, Path] = {}
    for index, item in enumerate(artifacts):
        if not isinstance(item, dict):
            violations.append(f"{kind}.artifacts[{index}] is not an object")
            continue
        stage = str(item.get("stage") or "")
        if not stage or stage in stages:
            violations.append(f"{kind}.artifact stage is missing or duplicated: {stage}")
        stages.add(stage)
        path = Path(str(item.get("path") or ""))
        if stage:
            artifact_paths[stage] = path
        expected_hash = str(item.get("sha256") or "")
        if not path.is_file() or not expected_hash:
            violations.append(f"{kind}.{stage or index} artifact identity is incomplete")
        elif sha256_file(path) != expected_hash:
            violations.append(f"{kind}.{stage or index} artifact hash mismatch")
        else:
            try:
                snapshot = evaluator.load_snapshot(path)
            except evaluator.EvidenceContractError as exc:
                violations.append(f"{kind}.{stage or index} artifact is invalid: {exc}")
            else:
                if sha256_file(path) != expected_hash:
                    violations.append(
                        f"{kind}.{stage or index} artifact changed while loading"
                    )
                else:
                    snapshots[stage] = snapshot
    missing_stages = {"before", "during", "after"} - stages
    if missing_stages:
        violations.append(f"{kind}.artifact stages missing: {sorted(missing_stages)}")
    snapshot_contract = evaluator.snapshot_violations(kind, snapshots)
    if snapshot_contract:
        violations.extend(
            f"{kind}.snapshot contract: {item}" for item in snapshot_contract
        )
    else:
        if any(
            snapshot.get("source_sha") != source_sha
            or snapshot.get("fault_id") != report.get("fault_id")
            for snapshot in snapshots.values()
        ):
            violations.append(f"{kind}.snapshot identity mismatch")
        try:
            derived = evaluator.derive_assertions(kind, snapshots)
        except evaluator.EvidenceContractError as exc:
            violations.append(f"{kind}.snapshot contract: {exc}")
        else:
            try:
                report_assertion_hash = evaluator.stable_hash(
                    report.get("assertions")
                )
            except evaluator.EvidenceContractError:
                report_assertion_hash = ""
            if report_assertion_hash != evaluator.stable_hash(derived):
                violations.append(
                    f"{kind}.report assertions do not match derived assertions"
                )
            violations.extend(evaluator.assertion_violations(kind, derived))

    if kind in FAULT_CONTRACTS:
        event_identity = report.get("event_log")
        if not isinstance(event_identity, dict):
            violations.append(f"{kind}.independent event log is missing")
            return violations
        event_path = Path(str(event_identity.get("path") or ""))
        if (
            not event_path.is_file()
            or not event_identity.get("sha256")
            or sha256_file(event_path) != event_identity.get("sha256")
        ):
            violations.append(f"{kind}.independent event log artifact hash mismatch")
            return violations
        try:
            events = load_verified_events(event_path)
        except RecoveryEvidenceError as exc:
            violations.append(f"{kind}.{exc}")
            return violations
        if (
            int(event_identity.get("event_count") or 0) != len(events)
            or event_identity.get("chain_head") != events[-1].get("event_sha256")
        ):
            violations.append(f"{kind}.independent event log summary mismatch")
        if any(event.get("schema_version") != 2 for event in events):
            violations.append(f"{kind}.event schema version mismatch")
        run_violations, _ = run_resource_violations(
            kind,
            fault_id=str(report.get("fault_id") or ""),
            events=events,
            event_path=event_path,
            artifact_paths=artifact_paths,
        )
        violations.extend(run_violations)
        event_fault_ids = {
            str(event.get("fault_id") or "")
            for event in events
        }
        if event_fault_ids != {str(report.get("fault_id") or "")}:
            violations.append(
                f"{kind}.independent event log fault identities mismatch"
            )
        fresh_starts = [
            event for event in events if event.get("action") == "fresh_up_started"
        ]
        fresh_completions = [
            event
            for event in events
            if event.get("action") == "fresh_up_completed"
        ]
        fresh_lifecycle_valid = (
            len(fresh_starts) == 1
            and len(fresh_completions) == 1
            and events[:2] == [fresh_starts[0], fresh_completions[0]]
        )
        if not fresh_lifecycle_valid:
            violations.append(f"{kind}.fresh stack lifecycle mismatch")
        service_contract = SERVICE_FAULTS.get(kind)
        contract = service_contract or {
            "service": "",
            "fault_action": "fault_marked",
            "recovery_action": "recovery_marked",
            "fault_time_field": "fault_time",
        }
        recovery_action = (
            "fault_service_recovered"
            if service_contract is not None
            else contract["recovery_action"]
        )
        fault_events = [
            event
            for event in events
            if event.get("fault_id") == report.get("fault_id")
            and event.get("action") == contract["fault_action"]
            and (
                (
                    event.get("service") == contract["service"]
                    if service_contract is not None
                    else event.get("fault_kind") == kind
                )
            )
        ]
        recovery_events = [
            event
            for event in events
            if event.get("fault_id") == report.get("fault_id")
            and event.get("action") == recovery_action
            and (
                (
                    event.get("service") == contract["service"]
                    if service_contract is not None
                    else event.get("fault_kind") == kind
                )
            )
        ]
        destroyed_events = [
            event for event in events if event.get("action") == "destroyed"
        ]
        if len(fault_events) != 1 or len(recovery_events) != 1:
            violations.append(f"{kind}.service fault/recovery event pair mismatch")
            identity_events: list[dict[str, Any]] = []
        else:
            if (
                events.index(fault_events[0]) >= events.index(recovery_events[0])
                or (
                    fresh_lifecycle_valid
                    and events.index(fresh_completions[0])
                    >= events.index(fault_events[0])
                )
            ):
                violations.append(f"{kind}.service event order mismatch")
            if (
                fault_events[0].get(contract["fault_time_field"])
                != report.get("fault_time")
                or recovery_events[0].get("recovery_time")
                != report.get("recovery_time")
            ):
                violations.append(f"{kind}.service event timestamp mismatch")
            identity_events = [fault_events[0], recovery_events[0]]
            if fresh_lifecycle_valid:
                identity_events = [
                    fresh_starts[0],
                    fresh_completions[0],
                    *identity_events,
                ]
        terminal_destroy_valid = (
            len(destroyed_events) == 1
            and events[-1] is destroyed_events[0]
            and len(recovery_events) == 1
            and events.index(recovery_events[0]) < events.index(destroyed_events[0])
        )
        if not terminal_destroy_valid:
            violations.append(f"{kind}.terminal destroy lifecycle mismatch")
        else:
            services = (
                (destroyed_events[0].get("after") or {}).get("services")
                or {}
            )
            if (
                not isinstance(services, dict)
                or set(services) != DESTROY_SERVICES
                or any(
                    not isinstance(state, dict)
                    or set(state) != {"exists", "running"}
                    or state.get("exists") is not False
                    or state.get("running") is not False
                    for state in services.values()
                )
            ):
                violations.append(f"{kind}.destroyed service inventory mismatch")
            identity_events.append(destroyed_events[0])
        if identity_events:
            event_identities = [
                event.get("identity") or {} for event in identity_events
            ]
            if any(
                identity != event_identities[0]
                for identity in event_identities[1:]
            ):
                violations.append(f"{kind}.service event identities mismatch")
            for event in identity_events:
                event_identity = event.get("identity") or {}
                if event_identity.get("source_sha") != source_sha:
                    violations.append(f"{kind}.service event source SHA mismatch")
                if candidate is not None:
                    candidate_source = candidate.get("source") or {}
                    candidate_images = candidate.get("images") or {}
                    if event_identity.get("source_tree") != candidate_source.get(
                        "tree"
                    ):
                        violations.append(
                            f"{kind}.service event source tree mismatch"
                        )
                    candidate_artifact = (
                        event_identity.get("candidate_manifest") or {}
                    )
                    candidate_path = Path(
                        str(candidate_artifact.get("path") or "")
                    )
                    try:
                        event_candidate = load_json(candidate_path)
                    except RecoveryEvidenceError:
                        event_candidate = {}
                    if (
                        not candidate_path.is_file()
                        or sha256_file(candidate_path)
                        != candidate_artifact.get("sha256")
                        or event_candidate != candidate
                    ):
                        violations.append(
                            f"{kind}.service event candidate identity mismatch"
                        )
                    for image_key, event_key in (
                        ("runtime", "runtime_image"),
                        ("publisher_browser", "browser_image"),
                    ):
                        expected = candidate_images.get(image_key) or {}
                        actual = event_identity.get(event_key) or {}
                        if any(
                            actual.get(field) != expected.get(field)
                            for field in ("tag", "image_id", "revision")
                        ):
                            violations.append(
                                f"{kind}.service event image mismatch: "
                                f"{image_key}"
                            )
                    dependency_images = (
                        event_identity.get("dependency_images") or {}
                    )
                    for image_key in ("postgres", "qdrant", "minio"):
                        expected = candidate_images.get(image_key) or {}
                        actual = dependency_images.get(image_key) or {}
                        if any(
                            actual.get(field) != expected.get(field)
                            for field in ("tag", "image_id")
                        ):
                            violations.append(
                                f"{kind}.service event dependency image "
                                f"mismatch: {image_key}"
                            )
                    release_files = {
                        str(item.get("path") or ""): str(
                            item.get("sha256") or ""
                        )
                        for item in (
                            candidate.get("release_harness") or {}
                        ).get("files") or []
                        if isinstance(item, dict)
                    }
                    event_harness = event_identity.get("harness") or {}
                    if not isinstance(event_harness, dict) or not event_harness:
                        violations.append(
                            f"{kind}.service event harness identity missing"
                        )
                    for harness_key, artifact in event_harness.items():
                        if not isinstance(artifact, dict):
                            violations.append(
                                f"{kind}.service event harness malformed: "
                                f"{harness_key}"
                            )
                            continue
                        artifact_path = Path(
                            str(artifact.get("path") or "")
                        ).resolve()
                        try:
                            source_path = artifact_path.relative_to(
                                ROOT
                            ).as_posix()
                        except ValueError:
                            source_path = ""
                        if (
                            not source_path
                            or not artifact_path.is_file()
                            or sha256_file(artifact_path)
                            != artifact.get("sha256")
                            or release_files.get(source_path)
                            != artifact.get("sha256")
                        ):
                            violations.append(
                                f"{kind}.service event harness mismatch: "
                                f"{harness_key}"
                            )
                    docker = event_identity.get("docker") or {}
                    if (
                        not str(docker.get("context") or "")
                        or not str(docker.get("endpoint") or "").startswith(
                            "unix://"
                        )
                        or not str(docker.get("daemon_id") or "")
                    ):
                        violations.append(
                            f"{kind}.service event Docker identity mismatch"
                        )
    return violations


def recovery_manifest_violations(
    manifest: dict[str, Any],
    *,
    source_sha: str,
    require_final_report: bool = True,
) -> list[str]:
    violations: list[str] = []
    if (
        int(manifest.get("schema_version") or 0) != 2
        or manifest.get("source_sha") != source_sha
        or manifest.get("result") != "pass"
        or manifest.get("violations") != []
    ):
        violations.append("recovery manifest summary is not a complete pass")

    identity = manifest.get("identity") or {}
    candidate: dict[str, Any] = {}
    if identity.get("source_sha") != source_sha:
        violations.append("recovery identity source SHA mismatch")
    for key in ("runtime_image", "browser_image"):
        if (identity.get(key) or {}).get("revision") != source_sha:
            violations.append(f"recovery {key} revision mismatch")
    candidate_identity = identity.get("rc_manifest") or {}
    candidate_path = Path(str(candidate_identity.get("path") or ""))
    if (
        not candidate_path.is_file()
        or not candidate_identity.get("sha256")
        or sha256_file(candidate_path) != candidate_identity.get("sha256")
    ):
        violations.append("recovery candidate manifest artifact hash mismatch")
    else:
        try:
            candidate = load_json(candidate_path)
        except RecoveryEvidenceError as exc:
            violations.append(str(exc))
        else:
            if (candidate.get("source") or {}).get("sha") != source_sha:
                violations.append("recovery candidate manifest source SHA mismatch")
            candidate_source = candidate.get("source") or {}
            if identity.get("source_tree") != candidate_source.get("tree"):
                violations.append("recovery candidate source tree mismatch")
            candidate_images = candidate.get("images") or {}
            for image_key, identity_key in (
                ("runtime", "runtime_image"),
                ("publisher_browser", "browser_image"),
            ):
                expected = candidate_images.get(image_key) or {}
                actual = identity.get(identity_key) or {}
                if any(
                    actual.get(field) != expected.get(field)
                    for field in ("tag", "image_id", "revision")
                ):
                    violations.append(
                        f"recovery candidate image mismatch: {image_key}"
                    )
            for image_key in ("postgres", "qdrant", "minio"):
                expected = candidate_images.get(image_key) or {}
                if not expected.get("tag") or not expected.get("image_id"):
                    violations.append(
                        f"recovery candidate dependency image incomplete: "
                        f"{image_key}"
                    )

    auditor = manifest.get("auditor") or {}
    release_files = {
        str(item.get("path") or ""): str(item.get("sha256") or "")
        for item in (candidate.get("release_harness") or {}).get("files") or []
        if isinstance(item, dict)
    }
    expected_auditors = {
        "path": Path(__file__).resolve(),
        "recovery_controller_path": RECOVERY_CONTROLLER_PATH.resolve(),
        "gate_helper_path": GATE_HELPER_PATH.resolve(),
    }
    for path_key, expected_path in expected_auditors.items():
        hash_key = "sha256" if path_key == "path" else path_key.replace("_path", "_sha256")
        actual_path = Path(str(auditor.get(path_key) or "")).resolve()
        if actual_path != expected_path:
            violations.append(f"recovery auditor path mismatch: {path_key}")
        if (
            not actual_path.is_file()
            or not auditor.get(hash_key)
            or sha256_file(actual_path) != auditor.get(hash_key)
        ):
            violations.append(f"recovery auditor artifact hash mismatch: {path_key}")
        try:
            source_path = actual_path.relative_to(ROOT).as_posix()
        except ValueError:
            source_path = ""
        if release_files.get(source_path) != auditor.get(hash_key):
            violations.append(
                f"recovery auditor is not bound by candidate source: {path_key}"
            )
    manifest_evaluator = manifest.get("evaluator") or {}
    if (
        Path(str(manifest_evaluator.get("path") or "")).resolve()
        != EVALUATOR_PATH
        or manifest_evaluator.get("sha256") != sha256_file(EVALUATOR_PATH)
        or release_files.get(EVALUATOR_PATH.relative_to(ROOT).as_posix())
        != sha256_file(EVALUATOR_PATH)
    ):
        violations.append("recovery semantic evaluator identity mismatch")

    refs = manifest.get("faults")
    if not isinstance(refs, dict) or set(refs) != set(FAULT_CONTRACTS):
        violations.append("recovery fault identities mismatch")
        refs = refs if isinstance(refs, dict) else {}
    reports: dict[str, dict[str, Any]] = {}
    report_paths: dict[str, Path] = {}
    for kind in FAULT_CONTRACTS:
        item = refs.get(kind)
        if not isinstance(item, dict):
            continue
        path = Path(str(item.get("path") or ""))
        expected_hash = str(item.get("sha256") or "")
        if not path.is_file() or not expected_hash or sha256_file(path) != expected_hash:
            violations.append(f"{kind}.report artifact hash mismatch")
            continue
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            violations.append(f"{kind}.report is invalid JSON")
            continue
        if not isinstance(report, dict):
            violations.append(f"{kind}.report is not an object")
            continue
        if (
            report.get("fault_kind") != kind
            or item.get("fault_id") != report.get("fault_id")
            or item.get("result") != report.get("result")
            or item.get("evaluator") != report.get("evaluator")
            or item.get("evaluator") != manifest_evaluator
        ):
            violations.append(f"{kind}.report identity mismatch")
        violations.extend(
            fault_report_violations(
                report,
                source_sha=source_sha,
                candidate=candidate or None,
            )
        )
        reports[kind] = report
        report_paths[kind] = path

    fault_id_owners: dict[str, list[str]] = {}
    event_path_owners: dict[str, set[str]] = {}
    event_hash_owners: dict[str, set[str]] = {}
    event_dir_owners: dict[str, set[str]] = {}
    evidence_dir_owners: dict[str, set[str]] = {}
    artifact_dir_owners: dict[str, set[str]] = {}
    report_dir_owners: dict[str, set[str]] = {}
    run_id_owners: dict[str, set[str]] = {}
    volume_name_owners: dict[str, set[str]] = {}
    volume_fingerprint_owners: dict[str, set[str]] = {}
    for kind, report in reports.items():
        fault_id = str(report.get("fault_id") or "")
        if fault_id:
            fault_id_owners.setdefault(fault_id, []).append(kind)
        event_identity = report.get("event_log") or {}
        if not isinstance(event_identity, dict):
            continue
        raw_event_path = str(event_identity.get("path") or "")
        event_sha256 = str(event_identity.get("sha256") or "")
        if raw_event_path:
            resolved_event_path = Path(raw_event_path).resolve()
            event_path = str(resolved_event_path)
            event_path_owners.setdefault(event_path, set()).add(kind)
            event_dir_owners.setdefault(
                str(resolved_event_path.parent), set()
            ).add(kind)
        if event_sha256:
            event_hash_owners.setdefault(event_sha256, set()).add(kind)
        artifact_paths = {
            str(item.get("stage") or ""): Path(str(item.get("path") or ""))
            for item in report.get("artifacts") or []
            if isinstance(item, dict) and str(item.get("stage") or "")
        }
        for artifact_path in artifact_paths.values():
            artifact_dir_owners.setdefault(
                str(artifact_path.resolve().parent), set()
            ).add(kind)
        report_path = report_paths.get(kind)
        if report_path is not None:
            report_dir_owners.setdefault(
                str(report_path.resolve().parent), set()
            ).add(kind)
        if not raw_event_path or not Path(raw_event_path).is_file():
            continue
        try:
            events = load_verified_events(Path(raw_event_path))
        except RecoveryEvidenceError:
            continue
        _, resources = run_resource_violations(
            kind,
            fault_id=fault_id,
            events=events,
            event_path=Path(raw_event_path),
            artifact_paths=artifact_paths,
        )
        if not resources:
            continue
        evidence_directory = resources["evidence_directory"]
        evidence_dir_owners.setdefault(evidence_directory, set()).add(kind)
        if (
            report_path is not None
            and str(report_path.resolve().parent) != evidence_directory
        ):
            violations.append(f"{kind}.report directory mismatch")
        run_id_owners.setdefault(resources["run_id"], set()).add(kind)
        volume_name_owners.setdefault(
            resources["volume_name"], set()
        ).add(kind)
        volume_fingerprint_owners.setdefault(
            resources["volume_fingerprint"], set()
        ).add(kind)
    for fault_id, owners in sorted(fault_id_owners.items()):
        if len(owners) > 1:
            violations.append(
                f"recovery fault_id is reused: {fault_id} "
                f"({', '.join(sorted(owners))})"
            )
    reused_event_logs = {
        tuple(sorted(owners))
        for owners in (*event_path_owners.values(), *event_hash_owners.values())
        if len(owners) > 1
    }
    for owners in sorted(reused_event_logs):
        violations.append(
            "recovery service event log is reused: " + ", ".join(owners)
        )
    ownership_checks = (
        ("evidence directory", evidence_dir_owners),
        ("event directory", event_dir_owners),
        ("artifact directory", artifact_dir_owners),
        ("report directory", report_dir_owners),
        ("run_id", run_id_owners),
        ("database volume name", volume_name_owners),
        ("database volume fingerprint", volume_fingerprint_owners),
    )
    for label, ownership in ownership_checks:
        reused = {
            tuple(sorted(owners))
            for owners in ownership.values()
            if len(owners) > 1
        }
        for owners in sorted(reused):
            violations.append(
                f"recovery {label} is reused: " + ", ".join(owners)
            )

    if require_final_report:
        final_report_artifact = manifest.get("final_report") or {}
        final_report_path = Path(str(final_report_artifact.get("path") or ""))
        if (
            not final_report_path.is_file()
            or sha256_file(final_report_path)
            != final_report_artifact.get("sha256")
        ):
            violations.append("recovery final report artifact hash mismatch")
        elif final_report_path.read_text(encoding="utf-8") != final_report(
            manifest
        ):
            violations.append("recovery final report content mismatch")
    return violations


def final_report(manifest: dict[str, Any]) -> str:
    lines = [
        "# ForWin v5 Live Recovery Final Audit",
        "",
        f"- Result: {'PASS' if manifest['result'] == 'pass' else 'FAIL'}",
        f"- Source SHA: `{manifest['source_sha']}`",
        f"- Faults: `{len(manifest['faults'])}` / `{len(FAULT_CONTRACTS)}`",
        f"- Independent service logs: `{len(SERVICE_FAULTS)}`",
        "",
        "| Fault | Result |",
        "| --- | --- |",
    ]
    for kind in FAULT_CONTRACTS:
        item = manifest["faults"].get(kind) or {}
        lines.append(f"| `{kind}` | {str(item.get('result') or 'missing').upper()} |")
    lines.extend(["", "## Findings", ""])
    lines.extend(
        ["- All live recovery fault contracts passed."]
        if not manifest["violations"]
        else [f"- {item}" for item in manifest["violations"]]
    )
    lines.append("")
    return "\n".join(lines)


def finalize(args: argparse.Namespace) -> int:
    candidate_path = args.candidate_manifest.resolve()
    gates = load_module("forwin_recovery_gate_helper", GATE_HELPER_PATH)
    identity = gates.assert_frozen(candidate_path)
    source_sha = str(identity["source_sha"])
    refs: dict[str, dict[str, Any]] = {}
    for raw_path in args.fault_report:
        path = raw_path.resolve()
        report = load_json(path)
        kind = str(report.get("fault_kind") or "")
        if not kind or kind in refs:
            raise RecoveryEvidenceError(
                f"fault report kind is missing or duplicated: {kind or '<missing>'}"
            )
        refs[kind] = {
            "path": str(path),
            "sha256": sha256_file(path),
            "fault_id": str(report.get("fault_id") or ""),
            "result": str(report.get("result") or ""),
            "event_log": report.get("event_log"),
            "evaluator": {
                "path": str(EVALUATOR_PATH),
                "sha256": sha256_file(EVALUATOR_PATH),
            },
        }
    manifest: dict[str, Any] = {
        "schema_version": 2,
        "source_sha": source_sha,
        "result": "pass",
        "violations": [],
        "identity": identity,
        "faults": refs,
        "auditor": {
            "path": str(Path(__file__).resolve()),
            "sha256": sha256_file(Path(__file__).resolve()),
            "recovery_controller_path": str(RECOVERY_CONTROLLER_PATH),
            "recovery_controller_sha256": sha256_file(RECOVERY_CONTROLLER_PATH),
            "gate_helper_path": str(GATE_HELPER_PATH),
            "gate_helper_sha256": sha256_file(GATE_HELPER_PATH),
        },
        "evaluator": {
            "path": str(EVALUATOR_PATH),
            "sha256": sha256_file(EVALUATOR_PATH),
        },
    }
    violations = recovery_manifest_violations(
        manifest,
        source_sha=source_sha,
        require_final_report=False,
    )
    manifest["violations"] = violations
    manifest["result"] = "pass" if not violations else "fail"

    output = args.output.resolve()
    if output.exists():
        raise RecoveryEvidenceError(f"recovery manifest already exists: {output}")
    report_path = output.with_name("final-report.md")
    if report_path.exists():
        raise RecoveryEvidenceError(f"recovery report already exists: {report_path}")
    atomic_write(report_path, final_report(manifest))
    manifest["final_report"] = {
        "path": str(report_path),
        "sha256": sha256_file(report_path),
    }
    write_json(output, manifest)
    print(report_path)
    return 0 if not violations else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Finalize strict live-recovery evidence for the v5 RC."
    )
    parser.add_argument("--candidate-manifest", type=Path, required=True)
    parser.add_argument("--fault-report", type=Path, action="append", required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / ".artifacts/v5-recovery-live/manifest.json",
    )
    return parser.parse_args()


def main() -> int:
    try:
        return finalize(parse_args())
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
