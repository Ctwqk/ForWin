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


FAULT_CONTRACTS: dict[str, dict[str, Any]] = {
    "generation_worker_precommit_crash": {
        "same_task_reclaimed": True,
        "lease_epoch_increased": True,
        "canon_commits_during_fault": 0,
        "canon_commits_after_recovery": 1,
        "duplicate_authoritative_identities": 0,
    },
    "generation_worker_postcommit_crash": {
        "same_task_reclaimed": True,
        "lease_epoch_increased": True,
        "canon_identity_unchanged": True,
        "accepted_identity_unchanged": True,
        "duplicate_authoritative_identities": 0,
    },
    "qdrant_unavailable": {
        "canon_identity_unchanged": True,
        "outbox_retry_observed": True,
        "projection_converged": True,
        "duplicate_vector_identities": 0,
    },
    "projection_consumer_unavailable": {
        "canon_identity_unchanged": True,
        "durable_outbox_preserved": True,
        "projection_converged": True,
        "duplicate_projection_identities": 0,
    },
    "minio_pre_canon_unavailable": {
        "canon_commits_during_fault": 0,
        "same_candidate_retried": True,
        "canon_commits_after_recovery": 1,
        "duplicate_authoritative_identities": 0,
    },
    "minio_post_canon_unavailable": {
        "canon_identity_unchanged": True,
        "accepted_identity_unchanged": True,
        "phase3_retry_same_identity": True,
        "artifact_key_unchanged": True,
        "barrier_residue_count": 0,
        "duplicate_authoritative_identities": 0,
    },
    "publisher_backend_unavailable": {
        "canon_identity_unchanged": True,
        "same_job_reclaimed": True,
        "orphaned_running_jobs": 0,
        "duplicate_jobs": 0,
        "duplicate_attempts": 0,
        "duplicate_receipts": 0,
    },
    "publisher_browser_unavailable": {
        "canon_identity_unchanged": True,
        "same_job_identity": True,
        "uncertain_mutation_reconciled": True,
        "duplicate_jobs": 0,
        "duplicate_attempts": 0,
        "duplicate_receipts": 0,
    },
    "publisher_captcha": {
        "same_job_identity": True,
        "paused_safely": True,
        "operator_action_recorded": True,
        "bypass_attempted": False,
        "duplicate_receipts": 0,
    },
    "publisher_mfa": {
        "same_job_identity": True,
        "paused_safely": True,
        "operator_action_recorded": True,
        "bypass_attempted": False,
        "duplicate_receipts": 0,
    },
    "publisher_account_risk": {
        "same_job_identity": True,
        "paused_safely": True,
        "operator_action_recorded": True,
        "bypass_attempted": False,
        "duplicate_receipts": 0,
    },
}

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


def fault_report_violations(
    report: dict[str, Any],
    *,
    source_sha: str,
    candidate: dict[str, Any] | None = None,
) -> list[str]:
    violations: list[str] = []
    kind = str(report.get("fault_kind") or "")
    if int(report.get("schema_version") or 0) != 1:
        violations.append(f"{kind or 'unknown'}.schema_version is not 1")
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
    for field in ("expected", "actual"):
        value = report.get(field)
        if not isinstance(value, list) or not value or any(
            not str(item).strip() for item in value
        ):
            violations.append(f"{kind}.{field} must be a nonempty text list")
    try:
        fault_time = normalized_time(report.get("fault_time"))
        recovery_time = normalized_time(report.get("recovery_time"))
        if recovery_time <= fault_time:
            violations.append(f"{kind}.recovery_time is not after fault_time")
    except ValueError:
        violations.append(f"{kind}.fault/recovery timestamp is invalid")

    assertions = report.get("assertions")
    if not isinstance(assertions, dict):
        assertions = {}
        violations.append(f"{kind}.assertions are missing")
    for key, expected in FAULT_CONTRACTS[kind].items():
        actual = assertions.get(key)
        if actual != expected:
            violations.append(f"{kind}.{key}={actual}, expected={expected}")

    artifacts = report.get("artifacts")
    if not isinstance(artifacts, list):
        artifacts = []
    stages: set[str] = set()
    for index, item in enumerate(artifacts):
        if not isinstance(item, dict):
            violations.append(f"{kind}.artifacts[{index}] is not an object")
            continue
        stage = str(item.get("stage") or "")
        if not stage or stage in stages:
            violations.append(f"{kind}.artifact stage is missing or duplicated: {stage}")
        stages.add(stage)
        path = Path(str(item.get("path") or ""))
        expected_hash = str(item.get("sha256") or "")
        if not path.is_file() or not expected_hash:
            violations.append(f"{kind}.{stage or index} artifact identity is incomplete")
        elif sha256_file(path) != expected_hash:
            violations.append(f"{kind}.{stage or index} artifact hash mismatch")
        else:
            try:
                snapshot = load_json(path)
            except RecoveryEvidenceError as exc:
                violations.append(f"{kind}.{stage or index} artifact is invalid: {exc}")
            else:
                if (
                    int(snapshot.get("schema_version") or 0) != 1
                    or snapshot.get("source_sha") != source_sha
                    or snapshot.get("fault_kind") != kind
                    or snapshot.get("fault_id") != report.get("fault_id")
                    or snapshot.get("stage") != stage
                    or not isinstance(snapshot.get("state"), dict)
                ):
                    violations.append(
                        f"{kind}.{stage or index} artifact identity mismatch"
                    )
    missing_stages = {"before", "during", "after"} - stages
    if missing_stages:
        violations.append(f"{kind}.artifact stages missing: {sorted(missing_stages)}")
    if kind in SERVICE_FAULTS:
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
        event_fault_ids = {
            str(event.get("fault_id") or "")
            for event in events
            if str(event.get("fault_id") or "")
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
        contract = SERVICE_FAULTS[kind]
        fault_events = [
            event
            for event in events
            if event.get("fault_id") == report.get("fault_id")
            and event.get("service") == contract["service"]
            and event.get("action") == contract["fault_action"]
        ]
        recovery_events = [
            event
            for event in events
            if event.get("fault_id") == report.get("fault_id")
            and event.get("service") == contract["service"]
            and event.get("action") == "fault_service_recovered"
        ]
        if len(fault_events) != 1 or len(recovery_events) != 1:
            violations.append(f"{kind}.service fault/recovery event pair mismatch")
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
        int(manifest.get("schema_version") or 0) != 1
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

    refs = manifest.get("faults")
    if not isinstance(refs, dict) or set(refs) != set(FAULT_CONTRACTS):
        violations.append("recovery fault identities mismatch")
        refs = refs if isinstance(refs, dict) else {}
    reports: dict[str, dict[str, Any]] = {}
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

    fault_id_owners: dict[str, list[str]] = {}
    event_path_owners: dict[str, set[str]] = {}
    event_hash_owners: dict[str, set[str]] = {}
    for kind, report in reports.items():
        fault_id = str(report.get("fault_id") or "")
        if fault_id:
            fault_id_owners.setdefault(fault_id, []).append(kind)
        if kind not in SERVICE_FAULTS:
            continue
        event_identity = report.get("event_log") or {}
        if not isinstance(event_identity, dict):
            continue
        raw_event_path = str(event_identity.get("path") or "")
        event_sha256 = str(event_identity.get("sha256") or "")
        if raw_event_path:
            event_path = str(Path(raw_event_path).resolve())
            event_path_owners.setdefault(event_path, set()).add(kind)
        if event_sha256:
            event_hash_owners.setdefault(event_sha256, set()).add(kind)
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
        }
    manifest: dict[str, Any] = {
        "schema_version": 1,
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
