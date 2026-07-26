#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Callable


SNAPSHOT_SCHEMA_VERSION = 2
STAGES = ("before", "during", "after")

_ENVELOPE_KEYS = {
    "schema_version",
    "source_sha",
    "fault_kind",
    "fault_id",
    "stage",
    "state",
}
_STATE_KEYS = {"target", "mcp", "api", "database", "external", "barrier"}
_MUTABLE_IDENTITY_KEYS = {
    "attempt",
    "attempt_count",
    "attempts",
    "created",
    "created_at",
    "heartbeat_at",
    "lease_epoch",
    "owner_token",
    "retry_count",
    "status",
    "timestamp",
    "timestamps",
    "updated",
    "updated_at",
}
_SOURCE_SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")

_FIXTURE_SCHEMA = {
    "fixture_id": str,
    "fault_id": str,
    "resource_type": str,
    "resource_id": str,
}
_PUBLISHER_FIXTURE_SCHEMA = {
    **_FIXTURE_SCHEMA,
    "logical_key": str,
}
_TASK_SCHEMA = {"task_id": str, "lease_epoch": int}
_CANON_SCHEMA = {
    "canon_id": str,
    "natural_key": str,
    "project_id": str,
    "chapter_id": str,
    "chapter_number": int,
    "candidate_id": str,
    "canon_version": int,
    "content_sha256": str,
}
_ACCEPTED_BUNDLE_SCHEMA = {
    "bundle_id": str,
    "candidate_id": str,
    "project_id": str,
    "chapter_id": str,
    "content_sha256": str,
}
_CANDIDATE_SCHEMA = {
    "candidate_id": str,
    "project_id": str,
    "chapter_id": str,
    "content_sha256": str,
}
_AUTHORITATIVE_SCHEMA = {
    "entity_type": str,
    "record_id": str,
    "project_id": str,
    "chapter_id": str,
    "natural_key": str,
}
_OUTBOX_SCHEMA = {
    "event_id": str,
    "aggregate_type": str,
    "aggregate_id": str,
    "event_type": str,
    "payload": dict,
    "payload_sha256": str,
    "status": str,
    "error_message": str,
    "attempt": int,
}
_OUTBOX_PAYLOAD_SCHEMA = {
    "schema_version": int,
    "canon_commit_id": str,
    "canon_idempotency_key": str,
    "project_id": str,
    "chapter_number": int,
    "candidate_id": str,
    "trigger": str,
}
_PROJECTION_OBSERVATION_SCHEMA = {
    "projection_type": str,
    "identity_id": str,
    "canon_id": str,
    "status": str,
}
_QDRANT_PROJECTION_OBSERVATION_SCHEMA = {
    **_PROJECTION_OBSERVATION_SCHEMA,
    "collection": str,
}
_POINT_SCHEMA = {
    "collection": str,
    "projection_type": str,
    "point_id": str,
    "canon_id": str,
}
_PROJECTION_IDENTITY_SCHEMA = {
    "projection_type": str,
    "projection_id": str,
    "canon_id": str,
}
_MAINTENANCE_SCHEMA = {
    "natural_key": str,
    "project_id": str,
    "canon_id": str,
    "attempt": int,
    "lease_epoch": int,
}
_ARTIFACT_SCHEMA = {
    "key": str,
    "etag": str,
    "size": int,
    "content_type": str,
    "content_sha256": str,
}
_PHASE3_PAYLOAD_SCHEMA = {
    "schema_version": int,
    "canon_commit_id": str,
    "canon_idempotency_key": str,
    "project_id": str,
    "chapter_number": int,
    "candidate_id": str,
}
_PHASE3_REPLAY_SCHEMA = {
    "row_id": str,
    "event_id": str,
    "aggregate_type": str,
    "aggregate_id": str,
    "event_type": str,
    "payload": dict,
    "payload_sha256": str,
    "canon_idempotency_key": str,
    "status": str,
    "attempts": int,
    "lease_epoch": int,
}
_PHASE3_RELEASE_SCHEMA = {
    **_PHASE3_REPLAY_SCHEMA,
    "conditional_rowcount": int,
    "predicate_sha256": str,
    "before_row_sha256": str,
    "after_row_sha256": str,
}
_PUBLISHER_JOB_SCHEMA = {
    "job_id": str,
    "logical_key": str,
    "task_kind": str,
    "project_id": str,
    "platform_id": str,
    "status": str,
    "publish": bool,
    "book_name": str,
    "chapter_title": str,
    "body_sha256": str,
    "unsafe_payload_paths": list,
}
_BACKEND_JOB_SCHEMA = {
    **_PUBLISHER_JOB_SCHEMA,
    "owner_token": str,
    "artifact_id": str,
}
_BROWSER_JOB_SCHEMA = dict(_PUBLISHER_JOB_SCHEMA)
_RISK_JOB_SCHEMA = {
    **_PUBLISHER_JOB_SCHEMA,
    "pause_reason": str,
    "pause_token": str,
    "risk_boundary": str,
}
_JOB_IDENTITY_SCHEMA = {
    "job_id": str,
    "logical_key": str,
    "task_kind": str,
}
_ATTEMPT_SCHEMA = {
    "attempt_id": str,
    "job_id": str,
    "attempt_number": int,
    "attempt_kind": str,
    "owner_token": str,
    "lease_epoch": int,
    "status": str,
    "phase": str,
    "content_sha256": str,
    "error_code": str,
}
_RECEIPT_SCHEMA = {
    "receipt_id": str,
    "job_id": str,
    "attempt_id": str,
    "natural_key": str,
}
_COVER_ASSET_SCHEMA = {
    "asset_id": str,
    "job_id": str,
    "file_path": str,
    "file_size": int,
    "mime_type": str,
}
_STALE_TOKEN_OBSERVATION_SCHEMA = {
    "observation_id": str,
    "job_id": str,
    "stale_owner_token": str,
    "current_owner_token": str,
    "response": dict,
}
_STALE_TOKEN_RESPONSE_SCHEMA = {
    "ok": bool,
    "stale_claim": bool,
}
_COVER_FILE_SCHEMA = {
    "path": str,
    "size": int,
    "content_sha256": str,
}
_TERMINAL_WRITE_OBSERVATION_SCHEMA = {
    "observation_id": str,
    "job_id": str,
    "owner_token": str,
    "holder_pid": int,
    "waiter_pid": int,
    "waiter_application_name": str,
    "waiter_role": str,
    "blocking_pids": list,
    "trigger_name": str,
    "function_name": str,
    "scope_table": str,
    "advisory_key": int,
}
_BARRIER_RESIDUE_SCHEMA = {
    "trigger_count": int,
    "function_count": int,
    "scope_table_count": int,
    "advisory_lock_count": int,
}
_HEARTBEAT_SCHEMA = {
    "observation_id": str,
    "browser_id": str,
    "probe": str,
    "status": str,
}
_RESUME_ACTION_SCHEMA = {
    "action_id": str,
    "job_id": str,
    "natural_key": str,
    "action": str,
    "pause_token": str,
    "actor_id": str,
    "auth_method": str,
    "reason_sha256": str,
    "old_status": str,
    "new_status": str,
}
_RESUME_REPLAY_SCHEMA = {
    "observation_id": str,
    "job_id": str,
    "action_id": str,
    "pause_token": str,
    "pause_reason": str,
    "request_sha256": str,
    "replay_request_sha256": str,
    "first_transition_sha256": str,
    "replay_transition_sha256": str,
    "first_disposition": str,
    "replay_disposition": str,
}
_EMPTY_STRING_FIELDS = {
    "artifact_id",
    "chapter_title",
    "error_code",
    "pause_reason",
    "pause_token",
    "project_id",
    "risk_boundary",
}


class EvidenceContractError(RuntimeError):
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
        "replay_identity_unchanged": True,
        "duplicate_vector_identities": 0,
    },
    "projection_consumer_unavailable": {
        "canon_identity_unchanged": True,
        "durable_outbox_preserved": True,
        "projection_converged": True,
        "replay_identity_unchanged": True,
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
        "phase3_replay_identity_unchanged": True,
        "phase3_release_rowcount": 1,
        "phase3_release_preserved_claim": True,
        "phase3_worker_claim_advanced": True,
        "phase3_replay_final_processed": True,
        "artifact_identity_unchanged": True,
        "barrier_residue_count": 0,
        "duplicate_authoritative_identities": 0,
    },
    "publisher_backend_unavailable": {
        "fixture_safe": True,
        "same_job_reclaimed": True,
        "terminal_write_boundary_observed": True,
        "stale_token_rejected": True,
        "shared_path_readable": True,
        "orphan_cleanup_observed": True,
        "orphan_residue_count": 0,
        "barrier_residue_count": 0,
        "duplicate_jobs": 0,
        "duplicate_attempts": 0,
        "duplicate_receipts": 0,
        "attempt_count": 0,
        "receipt_count": 0,
    },
    "publisher_browser_unavailable": {
        "fixture_safe": True,
        "same_job_identity": True,
        "pending_job_preserved": True,
        "heartbeat_recovered": True,
        "attempt_count": 0,
        "mutation_count": 0,
        "receipt_count": 0,
    },
    "publisher_captcha": {
        "fixture_safe": True,
        "same_job_identity": True,
        "paused_safely": True,
        "operator_action_recorded": True,
        "resume_replay_idempotent": True,
        "bypass_attempted": False,
        "attempt_count": 1,
        "receipt_count": 0,
    },
    "publisher_mfa": {
        "fixture_safe": True,
        "same_job_identity": True,
        "paused_safely": True,
        "operator_action_recorded": True,
        "resume_replay_idempotent": True,
        "bypass_attempted": False,
        "attempt_count": 1,
        "receipt_count": 0,
    },
    "publisher_account_risk": {
        "fixture_safe": True,
        "same_job_identity": True,
        "paused_safely": True,
        "operator_action_recorded": True,
        "resume_replay_idempotent": True,
        "bypass_attempted": False,
        "attempt_count": 1,
        "receipt_count": 0,
    },
}

_REQUIRED_PATHS: dict[str, dict[str, tuple[str, ...]]] = {
    "generation_worker_precommit_crash": {
        "before": ("state.database.task",),
        "during": ("state.database.task", "state.database.canon_commits"),
        "after": (
            "state.database.task",
            "state.database.canon_commits",
            "state.database.authoritative_identities",
        ),
    },
    "generation_worker_postcommit_crash": {
        "before": ("state.database.task",),
        "during": (
            "state.database.task",
            "state.database.canon_commits",
            "state.database.accepted_bundles",
        ),
        "after": (
            "state.database.task",
            "state.database.canon_commits",
            "state.database.accepted_bundles",
            "state.database.authoritative_identities",
        ),
    },
    "qdrant_unavailable": {
        "before": ("state.database.canon_commits", "state.database.outbox"),
        "during": ("state.database.canon_commits", "state.database.outbox"),
        "after": (
            "state.database.canon_commits",
            "state.database.outbox",
            "state.external.replay_baseline_projections",
            "state.external.replay_baseline_point_identities",
            "state.external.projections",
            "state.external.point_identities",
        ),
    },
    "projection_consumer_unavailable": {
        "before": ("state.database.canon_commits", "state.database.outbox"),
        "during": ("state.database.canon_commits", "state.database.outbox"),
        "after": (
            "state.database.canon_commits",
            "state.database.outbox",
            "state.external.replay_baseline_projections",
            "state.external.replay_baseline_projection_identities",
            "state.external.projections",
            "state.external.projection_identities",
        ),
    },
    "minio_pre_canon_unavailable": {
        "before": ("state.database.candidate",),
        "during": (
            "state.database.candidate",
            "state.database.canon_commits",
        ),
        "after": (
            "state.database.candidate",
            "state.database.canon_commits",
            "state.database.authoritative_identities",
        ),
    },
    "minio_post_canon_unavailable": {
        "before": (
            "state.database.canon_commits",
            "state.database.accepted_bundles",
        ),
        "during": (
            "state.database.canon_commits",
            "state.database.accepted_bundles",
            "state.database.maintenance",
        ),
        "after": (
            "state.database.canon_commits",
            "state.database.accepted_bundles",
            "state.database.maintenance",
            "state.database.authoritative_identities",
            "state.database.phase3_replay_baseline",
            "state.database.phase3_replay_release",
            "state.database.phase3_replay_final",
            "state.external.replay_baseline_artifact",
            "state.external.artifact",
            "state.barrier.residue_count",
        ),
    },
    "publisher_backend_unavailable": {
        "before": (
            "state.database.job",
            "state.database.jobs",
            "state.database.attempts",
            "state.database.receipts",
        ),
        "during": (
            "state.database.job",
            "state.database.jobs",
            "state.database.attempts",
            "state.database.receipts",
            "state.database.cover_assets",
            "state.external.cover_files",
        ),
        "after": (
            "state.database.job",
            "state.database.jobs",
            "state.database.attempts",
            "state.database.receipts",
            "state.database.cover_assets",
            "state.api.stale_token_observation",
            "state.external.cover_files",
            "state.barrier.terminal_writes",
            "state.barrier.residue",
        ),
    },
    "publisher_browser_unavailable": {
        "before": (
            "state.database.job",
            "state.database.attempts",
            "state.database.receipts",
            "state.external.browser_heartbeat",
        ),
        "during": (
            "state.database.job",
            "state.database.attempts",
            "state.database.receipts",
            "state.external.browser_heartbeat",
        ),
        "after": (
            "state.database.job",
            "state.database.attempts",
            "state.database.receipts",
            "state.external.browser_heartbeat",
        ),
    },
    "publisher_captcha": {
        "before": ("state.database.job", "state.database.attempts"),
        "during": ("state.database.job", "state.database.attempts"),
        "after": (
            "state.database.job",
            "state.database.attempts",
            "state.database.receipts",
            "state.database.resume_actions",
            "state.api.resume_replay",
        ),
    },
    "publisher_mfa": {
        "before": ("state.database.job", "state.database.attempts"),
        "during": ("state.database.job", "state.database.attempts"),
        "after": (
            "state.database.job",
            "state.database.attempts",
            "state.database.receipts",
            "state.database.resume_actions",
            "state.api.resume_replay",
        ),
    },
    "publisher_account_risk": {
        "before": ("state.database.job", "state.database.attempts"),
        "during": ("state.database.job", "state.database.attempts"),
        "after": (
            "state.database.job",
            "state.database.attempts",
            "state.database.receipts",
            "state.database.resume_actions",
            "state.api.resume_replay",
        ),
    },
}


def required_path(value: Mapping[str, Any], dotted: str) -> Any:
    current: Any = value
    traversed: list[str] = []
    for part in dotted.split("."):
        traversed.append(part)
        if not isinstance(current, Mapping) or part not in current:
            raise EvidenceContractError(f"{'.'.join(traversed)} is missing")
        current = current[part]
    return current


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
        raise EvidenceContractError(f"value is not JSON-compatible: {exc}") from exc
    return hashlib.sha256(body).hexdigest()


def duplicate_excess(
    rows: Sequence[Mapping[str, Any]], keys: tuple[str, ...]
) -> int:
    identities: list[str] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise EvidenceContractError(f"rows[{index}] is not an object")
        try:
            identity = [required_path(row, key) for key in keys]
        except EvidenceContractError as exc:
            raise EvidenceContractError(f"rows[{index}].{exc}") from exc
        identities.append(_canonical_json(identity))
    return sum(count - 1 for count in Counter(identities).values())


def load_snapshot(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise EvidenceContractError(f"snapshot is missing: {path}") from exc
    except OSError as exc:
        raise EvidenceContractError(f"snapshot cannot be read: {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise EvidenceContractError(
            f"snapshot is not valid JSON: {path}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise EvidenceContractError(f"expected JSON object in snapshot: {path}")
    return payload


def snapshot_violations(
    kind: str, snapshots: Mapping[str, dict[str, Any]]
) -> list[str]:
    if kind not in FAULT_CONTRACTS:
        return [f"unknown fault kind: {kind or '<missing>'}"]
    if not isinstance(snapshots, Mapping):
        return ["snapshots is not an object"]

    violations: list[str] = []
    snapshot_keys = set(snapshots)
    missing_stages = sorted(set(STAGES) - snapshot_keys)
    unknown_stages = sorted(snapshot_keys - set(STAGES))
    if missing_stages:
        violations.append(f"snapshot stages missing: {missing_stages}")
    if unknown_stages:
        violations.append(f"snapshot stages unknown: {unknown_stages}")

    baseline_source_sha: Any = None
    baseline_fault_id: Any = None
    for stage in STAGES:
        snapshot = snapshots.get(stage)
        if not isinstance(snapshot, Mapping):
            if stage in snapshots:
                violations.append(f"{stage} is not an object")
            continue
        violations.extend(_unknown_key_violations(stage, snapshot, _ENVELOPE_KEYS))

        for dotted in (
            "schema_version",
            "source_sha",
            "fault_kind",
            "fault_id",
            "stage",
            "state",
        ):
            _capture_required(snapshot, dotted, stage, violations)
        state_value = snapshot.get("state")
        if isinstance(state_value, Mapping):
            violations.extend(
                _unknown_key_violations(f"{stage}.state", state_value, _STATE_KEYS)
            )
            for section in sorted(_STATE_KEYS):
                value = _capture_required(
                    snapshot, f"state.{section}", stage, violations
                )
                if value is not None and not isinstance(value, Mapping):
                    violations.append(f"{stage}.state.{section} is not an object")
            _capture_required(snapshot, "state.target.fixture", stage, violations)
        elif "state" in snapshot:
            violations.append(f"{stage}.state is not an object")

        if (
            type(snapshot.get("schema_version")) is not int
            or snapshot.get("schema_version") != SNAPSHOT_SCHEMA_VERSION
        ):
            violations.append(
                f"{stage}.schema_version={snapshot.get('schema_version')}, "
                f"expected={SNAPSHOT_SCHEMA_VERSION}"
            )
        if snapshot.get("fault_kind") != kind:
            violations.append(f"{stage}.fault_kind mismatch")
        if snapshot.get("stage") != stage:
            violations.append(f"{stage}.stage mismatch")
        source_sha = snapshot.get("source_sha")
        fault_id = snapshot.get("fault_id")
        if (
            type(source_sha) is not str
            or _SOURCE_SHA_PATTERN.fullmatch(source_sha) is None
        ):
            violations.append(
                f"{stage}.source_sha is not a canonical "
                "40-character lowercase hex SHA"
            )
        if type(fault_id) is not str or not fault_id:
            violations.append(f"{stage}.fault_id is empty")
        if stage == "before":
            baseline_source_sha = source_sha
            baseline_fault_id = fault_id
        else:
            if source_sha != baseline_source_sha:
                violations.append(f"{stage}.source_sha mismatch")
            if fault_id != baseline_fault_id:
                violations.append(f"{stage}.fault_id mismatch")

        for dotted in _REQUIRED_PATHS[kind][stage]:
            _capture_required(snapshot, dotted, stage, violations)

    if not violations:
        violations.extend(_shape_violations(kind, snapshots))
    if not violations:
        violations.extend(_stable_identity_violations(kind, snapshots))
    return _deduplicated(violations)


def derive_assertions(
    kind: str, snapshots: Mapping[str, dict[str, Any]]
) -> dict[str, Any]:
    violations = snapshot_violations(kind, snapshots)
    if violations:
        raise EvidenceContractError("; ".join(violations))
    return _DERIVERS[kind](snapshots)


def assertion_violations(
    kind: str, assertions: Mapping[str, Any]
) -> list[str]:
    if kind not in FAULT_CONTRACTS:
        return [f"unknown fault kind: {kind or '<missing>'}"]
    if not isinstance(assertions, Mapping):
        return [f"{kind}.assertions is not an object"]
    expected = FAULT_CONTRACTS[kind]
    violations: list[str] = []
    unknown = sorted(set(assertions) - set(expected))
    if unknown:
        violations.append(f"{kind}.assertions has unknown keys: {unknown}")
    for key, expected_value in expected.items():
        if key not in assertions:
            violations.append(f"{kind}.{key} is missing")
        elif (
            type(assertions[key]) is not type(expected_value)
            or assertions[key] != expected_value
        ):
            violations.append(
                f"{kind}.{key}={assertions[key]}, expected={expected_value}"
            )
    return violations


def _generation_worker_precommit(
    snapshots: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    tasks = [_path(snapshots, stage, "database.task") for stage in STAGES]
    during_canon = _path(snapshots, "during", "database.canon_commits")
    after_canon = _path(snapshots, "after", "database.canon_commits")
    identities = _path(
        snapshots, "after", "database.authoritative_identities"
    )
    return {
        "same_task_reclaimed": len({task["task_id"] for task in tasks}) == 1,
        "lease_epoch_increased": tasks[-1]["lease_epoch"]
        > tasks[0]["lease_epoch"],
        "canon_commits_during_fault": len(during_canon),
        "canon_commits_after_recovery": len(after_canon),
        "duplicate_authoritative_identities": duplicate_excess(
            identities, ("entity_type", "natural_key")
        ),
    }


def _generation_worker_postcommit(
    snapshots: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    tasks = [_path(snapshots, stage, "database.task") for stage in STAGES]
    canon_during = _path(snapshots, "during", "database.canon_commits")
    canon_after = _path(snapshots, "after", "database.canon_commits")
    accepted_during = _path(snapshots, "during", "database.accepted_bundles")
    accepted_after = _path(snapshots, "after", "database.accepted_bundles")
    identities = _path(
        snapshots, "after", "database.authoritative_identities"
    )
    return {
        "same_task_reclaimed": len({task["task_id"] for task in tasks}) == 1,
        "lease_epoch_increased": tasks[-1]["lease_epoch"]
        > tasks[0]["lease_epoch"],
        "canon_identity_unchanged": _all_stable_equal(
            (canon_during, canon_after)
        ),
        "accepted_identity_unchanged": _all_stable_equal(
            (accepted_during, accepted_after)
        ),
        "duplicate_authoritative_identities": duplicate_excess(
            identities, ("entity_type", "natural_key")
        ),
    }


def _qdrant(snapshots: Mapping[str, dict[str, Any]]) -> dict[str, Any]:
    canon = [_path(snapshots, stage, "database.canon_commits") for stage in STAGES]
    outbox = [_path(snapshots, stage, "database.outbox") for stage in STAGES]
    outbox_identities = [_outbox_identity(row) for row in outbox]
    projections = _path(snapshots, "after", "external.projections")
    points = _path(snapshots, "after", "external.point_identities")
    baseline_projections = _path(
        snapshots,
        "after",
        "external.replay_baseline_projections",
    )
    baseline_points = _path(
        snapshots,
        "after",
        "external.replay_baseline_point_identities",
    )
    return {
        "canon_identity_unchanged": _all_stable_equal(canon),
        "outbox_retry_observed": (
            _all_stable_equal(outbox_identities)
            and outbox[1]["status"] == "pending"
            and bool(outbox[1]["error_message"])
            and outbox[1]["attempt"] > outbox[0]["attempt"]
            and outbox[2]["status"] == "processed"
            and outbox[2]["error_message"] == ""
            and outbox[2]["attempt"] > outbox[1]["attempt"]
        ),
        "projection_converged": bool(projections)
        and all(row["status"] == "converged" for row in projections),
        "replay_identity_unchanged": _all_stable_equal(
            (baseline_projections, projections)
        )
        and _all_stable_equal((baseline_points, points)),
        "duplicate_vector_identities": duplicate_excess(
            points, ("collection", "point_id")
        ),
    }


def _projection_consumer(
    snapshots: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    canon = [_path(snapshots, stage, "database.canon_commits") for stage in STAGES]
    outbox = [_path(snapshots, stage, "database.outbox") for stage in STAGES]
    projections = _path(snapshots, "after", "external.projections")
    identities = _path(
        snapshots, "after", "external.projection_identities"
    )
    baseline_projections = _path(
        snapshots,
        "after",
        "external.replay_baseline_projections",
    )
    baseline_identities = _path(
        snapshots,
        "after",
        "external.replay_baseline_projection_identities",
    )
    outbox_identities = [_outbox_identity(row) for row in outbox]
    return {
        "canon_identity_unchanged": _all_stable_equal(canon),
        "durable_outbox_preserved": (
            _all_stable_equal(outbox_identities)
            and outbox[0]["status"] == "pending"
            and outbox[1]["status"] == "pending"
            and outbox[2]["status"] == "processed"
            and outbox[0]["error_message"] == ""
            and outbox[1]["error_message"] == ""
            and outbox[2]["error_message"] == ""
            and outbox[1]["attempt"] == outbox[0]["attempt"]
            and outbox[2]["attempt"] > outbox[1]["attempt"]
        ),
        "projection_converged": bool(projections)
        and all(row["status"] == "converged" for row in projections),
        "replay_identity_unchanged": _all_stable_equal(
            (baseline_projections, projections)
        )
        and _all_stable_equal((baseline_identities, identities)),
        "duplicate_projection_identities": duplicate_excess(
            identities, ("projection_type", "projection_id")
        ),
    }


def _outbox_identity(row: Mapping[str, Any]) -> dict[str, Any]:
    return _without_fields(
        row,
        {"attempt", "error_message", "status"},
    )


def _minio_pre_canon(
    snapshots: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    candidates = [
        _path(snapshots, stage, "database.candidate") for stage in STAGES
    ]
    during_canon = _path(snapshots, "during", "database.canon_commits")
    after_canon = _path(snapshots, "after", "database.canon_commits")
    identities = _path(
        snapshots, "after", "database.authoritative_identities"
    )
    return {
        "canon_commits_during_fault": len(during_canon),
        "same_candidate_retried": _all_stable_equal(candidates),
        "canon_commits_after_recovery": len(after_canon),
        "duplicate_authoritative_identities": duplicate_excess(
            identities, ("entity_type", "natural_key")
        ),
    }


def _minio_post_canon(
    snapshots: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    canon = [_path(snapshots, stage, "database.canon_commits") for stage in STAGES]
    accepted = [
        _path(snapshots, stage, "database.accepted_bundles") for stage in STAGES
    ]
    maintenance_during = _path(snapshots, "during", "database.maintenance")
    maintenance_after = _path(snapshots, "after", "database.maintenance")
    artifact_baseline = _path(
        snapshots,
        "after",
        "external.replay_baseline_artifact",
    )
    artifact_after = _path(snapshots, "after", "external.artifact")
    replay_baseline = _path(
        snapshots,
        "after",
        "database.phase3_replay_baseline",
    )
    replay_release = _path(
        snapshots,
        "after",
        "database.phase3_replay_release",
    )
    replay_final = _path(
        snapshots,
        "after",
        "database.phase3_replay_final",
    )
    replay_identity_fields = (
        "row_id",
        "event_id",
        "aggregate_type",
        "aggregate_id",
        "event_type",
        "payload",
        "payload_sha256",
        "canon_idempotency_key",
    )
    identities = _path(
        snapshots, "after", "database.authoritative_identities"
    )
    return {
        "canon_identity_unchanged": _all_stable_equal(canon),
        "accepted_identity_unchanged": _all_stable_equal(accepted),
        "phase3_retry_same_identity": (
            maintenance_during["natural_key"]
            == maintenance_after["natural_key"]
            and maintenance_after["attempt"] > maintenance_during["attempt"]
            and maintenance_after["lease_epoch"]
            > maintenance_during["lease_epoch"]
        ),
        "phase3_replay_identity_unchanged": all(
            replay_baseline[field]
            == replay_release[field]
            == replay_final[field]
            for field in replay_identity_fields
        ),
        "phase3_release_rowcount": replay_release["conditional_rowcount"],
        "phase3_release_preserved_claim": (
            replay_baseline["status"] == "processed"
            and replay_release["status"] == "pending"
            and replay_release["attempts"] == replay_baseline["attempts"]
            and replay_release["lease_epoch"]
            == replay_baseline["lease_epoch"]
        ),
        "phase3_worker_claim_advanced": (
            replay_final["attempts"] == replay_release["attempts"] + 1
            and replay_final["lease_epoch"]
            == replay_release["lease_epoch"] + 1
        ),
        "phase3_replay_final_processed": replay_final["status"] == "processed",
        "artifact_identity_unchanged": artifact_baseline == artifact_after,
        "barrier_residue_count": _path(
            snapshots, "after", "barrier.residue_count"
        ),
        "duplicate_authoritative_identities": duplicate_excess(
            identities, ("entity_type", "natural_key")
        ),
    }


_PUBLISHER_BOOK_NAME = "Publisher Recovery Fixture"
_PUBLISHER_CHAPTER_TITLE = "Recovery Chapter"
_PUBLISHER_BODY_SHA256 = hashlib.sha256(
    b"Generic publisher recovery fixture content."
).hexdigest()
_EMPTY_BODY_SHA256 = hashlib.sha256(b"").hexdigest()
_PUBLISHER_JOB_IDENTITY_FIELDS = (
    "job_id",
    "logical_key",
    "task_kind",
    "project_id",
    "platform_id",
    "publish",
    "book_name",
    "chapter_title",
    "body_sha256",
    "unsafe_payload_paths",
)
_RISK_REASONS = {
    "publisher_captcha": "captcha",
    "publisher_mfa": "mfa",
    "publisher_account_risk": "account_risk",
}


def _publisher_fixture_safe(
    kind: str,
    snapshots: Mapping[str, dict[str, Any]],
) -> bool:
    fixtures = [_path(snapshots, stage, "target.fixture") for stage in STAGES]
    jobs = [_path(snapshots, stage, "database.job") for stage in STAGES]
    backend = kind == "publisher_backend_unavailable"
    expected_task_kind = "cover_generate" if backend else "chapter_upload"
    expected_title = "" if backend else _PUBLISHER_CHAPTER_TITLE
    expected_body_sha256 = _EMPTY_BODY_SHA256 if backend else _PUBLISHER_BODY_SHA256
    return (
        _all_stable_equal(fixtures)
        and all(
            fixture["resource_type"] == "publisher_job"
            and fixture["resource_id"] == job["job_id"]
            and fixture["logical_key"] == job["logical_key"]
            and fixture["logical_key"]
            == f"publisher-recovery:v1:{fixture['fault_id']}"
            and fixture["fault_id"] == snapshots[stage]["fault_id"]
            for stage, fixture, job in zip(STAGES, fixtures, jobs)
        )
        and all(
            job["task_kind"] == expected_task_kind
            and job["project_id"] == ""
            and job["platform_id"] == "qidian"
            and job["publish"] is False
            and job["book_name"] == _PUBLISHER_BOOK_NAME
            and job["chapter_title"] == expected_title
            and job["body_sha256"] == expected_body_sha256
            and job["unsafe_payload_paths"] == []
            for job in jobs
        )
    )


def _publisher_backend(
    snapshots: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    job_before = _path(snapshots, "before", "database.job")
    job_during = _path(snapshots, "during", "database.job")
    job_after = _path(snapshots, "after", "database.job")
    jobs = _path(snapshots, "after", "database.jobs")
    attempts = _path(snapshots, "after", "database.attempts")
    receipts = _path(snapshots, "after", "database.receipts")
    cover_assets_during = _path(
        snapshots, "during", "database.cover_assets"
    )
    cover_assets_after = _path(snapshots, "after", "database.cover_assets")
    files_during = _path(snapshots, "during", "external.cover_files")
    files_after = _path(snapshots, "after", "external.cover_files")
    stale_token = _path(snapshots, "after", "api.stale_token_observation")
    stale_response = stale_token["response"]
    terminal_writes = _path(snapshots, "after", "barrier.terminal_writes")
    residue = _path(snapshots, "after", "barrier.residue")
    asset = cover_assets_after[0] if len(cover_assets_after) == 1 else {}
    file_by_path = {row["path"]: row for row in files_after}
    shared_file = file_by_path.get(asset.get("file_path"), {})
    after_paths = set(file_by_path)
    during_paths = {row["path"] for row in files_during}
    referenced_paths = {row["file_path"] for row in cover_assets_after}
    orphan_after = after_paths - referenced_paths
    barrier_bound = (
        len(terminal_writes) == 2
        and all(
            observation["job_id"] == job_after["job_id"]
            and observation["waiter_application_name"]
            == "forwin-recovery-publisher-worker"
            and observation["waiter_role"] == "forwin"
            and observation["holder_pid"] != observation["waiter_pid"]
            and observation["blocking_pids"]
            == [observation["holder_pid"]]
            and observation["holder_pid"] > 0
            and observation["waiter_pid"] > 0
            for observation in terminal_writes
        )
        and terminal_writes[0]["owner_token"] == job_during["owner_token"]
        and terminal_writes[1]["owner_token"] == job_after["owner_token"]
        and terminal_writes[0]["owner_token"]
        != terminal_writes[1]["owner_token"]
        and all(
            terminal_writes[0][field] == terminal_writes[1][field]
            for field in (
                "trigger_name",
                "function_name",
                "scope_table",
                "advisory_key",
                "holder_pid",
            )
        )
    )
    return {
        "fixture_safe": _publisher_fixture_safe(
            "publisher_backend_unavailable", snapshots
        ),
        "same_job_reclaimed": _same_fields(
            job_during,
            job_after,
            _PUBLISHER_JOB_IDENTITY_FIELDS,
        )
        and _same_fields(
            job_before,
            job_during,
            (*_PUBLISHER_JOB_IDENTITY_FIELDS, "owner_token"),
        )
        and job_before["status"] == "running"
        and job_during["status"] == "running"
        and job_after["status"] == "succeeded"
        and job_during["owner_token"] != job_after["owner_token"],
        "terminal_write_boundary_observed": barrier_bound,
        "stale_token_rejected": (
            stale_token["job_id"] == job_after["job_id"]
            and stale_token["stale_owner_token"] == job_during["owner_token"]
            and stale_token["current_owner_token"] == job_after["owner_token"]
            and stale_response == {"ok": False, "stale_claim": True}
        ),
        "shared_path_readable": (
            len(cover_assets_after) == 1
            and asset.get("job_id") == job_after["job_id"]
            and asset.get("asset_id") == job_after["artifact_id"]
            and shared_file.get("path") == asset.get("file_path")
            and shared_file.get("size") == asset.get("file_size")
            and isinstance(shared_file.get("content_sha256"), str)
            and _SHA256_PATTERN.fullmatch(
                str(shared_file.get("content_sha256") or "")
            )
            is not None
            and int(shared_file.get("size") or 0) > 0
        ),
        "orphan_cleanup_observed": (
            cover_assets_during == []
            and bool(during_paths)
            and during_paths.isdisjoint(after_paths)
        ),
        "orphan_residue_count": len(orphan_after),
        "barrier_residue_count": sum(residue.values()),
        "duplicate_jobs": duplicate_excess(jobs, ("logical_key",)),
        "duplicate_attempts": duplicate_excess(
            attempts, ("job_id", "attempt_number")
        ),
        "duplicate_receipts": duplicate_excess(
            receipts, ("job_id", "natural_key")
        ),
        "attempt_count": len(attempts),
        "receipt_count": len(receipts),
    }


def _publisher_browser(
    snapshots: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    jobs = [_path(snapshots, stage, "database.job") for stage in STAGES]
    heartbeats = [
        _path(snapshots, stage, "external.browser_heartbeat")
        for stage in STAGES
    ]
    attempts = [
        _path(snapshots, stage, "database.attempts")
        for stage in STAGES
    ]
    receipts = [
        _path(snapshots, stage, "database.receipts")
        for stage in STAGES
    ]
    return {
        "fixture_safe": _publisher_fixture_safe(
            "publisher_browser_unavailable", snapshots
        ),
        "same_job_identity": _all_stable_equal(jobs),
        "pending_job_preserved": all(job["status"] == "pending" for job in jobs),
        "heartbeat_recovered": (
            len({heartbeat["browser_id"] for heartbeat in heartbeats}) == 1
            and all(
                heartbeat["probe"] == "extension_heartbeat_status"
                for heartbeat in heartbeats
            )
            and [heartbeat["status"] for heartbeat in heartbeats]
            == ["healthy", "stale", "healthy"]
        ),
        "attempt_count": max(len(rows) for rows in attempts),
        "mutation_count": sum(
            stable_hash(job) != stable_hash(jobs[0]) for job in jobs[1:]
        ),
        "receipt_count": max(len(rows) for rows in receipts),
    }


def _publisher_risk(
    kind: str,
    snapshots: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    jobs = [_path(snapshots, stage, "database.job") for stage in STAGES]
    paused_job = jobs[1]
    attempts_by_stage = [
        _path(snapshots, stage, "database.attempts")
        for stage in STAGES
    ]
    attempts_after = attempts_by_stage[2]
    receipts = _path(snapshots, "after", "database.receipts")
    actions = _path(snapshots, "after", "database.resume_actions")
    replay = _path(snapshots, "after", "api.resume_replay")
    action = actions[0] if len(actions) == 1 else {}
    attempt = attempts_after[0] if len(attempts_after) == 1 else {}
    expected_reason = _RISK_REASONS[kind]
    operator_action_recorded = (
        len(actions) == 1
        and action.get("job_id") == paused_job["job_id"]
        and action.get("natural_key")
        == (
            f"{paused_job['job_id']}:resume:"
            f"{paused_job['pause_token']}"
        )
        and action.get("action") == "resume"
        and action.get("pause_token") == paused_job["pause_token"]
        and action.get("auth_method") in {"basic", "trusted_proxy"}
        and bool(action.get("actor_id"))
        and action.get("old_status") == "paused"
        and action.get("new_status") == "pending"
    )
    stable_attempt_fields = (
        "attempt_id",
        "job_id",
        "attempt_number",
        "attempt_kind",
        "owner_token",
        "lease_epoch",
        "phase",
        "content_sha256",
    )
    stable_attempt = (
        all(len(rows) == 1 for rows in attempts_by_stage)
        and all(
            _same_fields(
                attempts_by_stage[0][0],
                rows[0],
                stable_attempt_fields,
            )
            for rows in attempts_by_stage[1:]
        )
    )
    pre_mutation = (
        stable_attempt
        and attempt.get("phase") == "claimed"
        and attempt.get("status") == "paused"
        and attempt.get("error_code") == expected_reason
        and paused_job["risk_boundary"] == "pre-mutation"
        and paused_job["pause_reason"] == expected_reason
        and paused_job["pause_token"] == attempt.get("attempt_id")
    )
    return {
        "fixture_safe": _publisher_fixture_safe(kind, snapshots),
        "same_job_identity": _all_stable_equal(
            [
                {field: job[field] for field in _PUBLISHER_JOB_IDENTITY_FIELDS}
                for job in jobs
            ]
        ),
        "paused_safely": (
            jobs[0]["status"] == "running"
            and paused_job["status"] == "paused"
            and jobs[2]["status"] == "pending"
            and pre_mutation
        ),
        "operator_action_recorded": operator_action_recorded,
        "resume_replay_idempotent": operator_action_recorded
        and replay["job_id"] == paused_job["job_id"]
        and replay["action_id"] == action["action_id"]
        and replay["pause_token"] == paused_job["pause_token"]
        and replay["pause_reason"] == expected_reason
        and replay["request_sha256"] == replay["replay_request_sha256"]
        and replay["first_transition_sha256"]
        == replay["replay_transition_sha256"]
        and replay["first_disposition"] == "applied"
        and replay["replay_disposition"] == "idempotent",
        "bypass_attempted": not (
            pre_mutation
            and receipts == []
        ),
        "attempt_count": len(attempts_after),
        "receipt_count": len(receipts),
    }


def _publisher_captcha(
    snapshots: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    return _publisher_risk("publisher_captcha", snapshots)


def _publisher_mfa(
    snapshots: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    return _publisher_risk("publisher_mfa", snapshots)


def _publisher_account_risk(
    snapshots: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    return _publisher_risk("publisher_account_risk", snapshots)


_DERIVERS: dict[
    str, Callable[[Mapping[str, dict[str, Any]]], dict[str, Any]]
] = {
    "generation_worker_precommit_crash": _generation_worker_precommit,
    "generation_worker_postcommit_crash": _generation_worker_postcommit,
    "qdrant_unavailable": _qdrant,
    "projection_consumer_unavailable": _projection_consumer,
    "minio_pre_canon_unavailable": _minio_pre_canon,
    "minio_post_canon_unavailable": _minio_post_canon,
    "publisher_backend_unavailable": _publisher_backend,
    "publisher_browser_unavailable": _publisher_browser,
    "publisher_captcha": _publisher_captcha,
    "publisher_mfa": _publisher_mfa,
    "publisher_account_risk": _publisher_account_risk,
}


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise EvidenceContractError(f"value is not JSON-compatible: {exc}") from exc


def _capture_required(
    value: Mapping[str, Any],
    dotted: str,
    prefix: str,
    violations: list[str],
) -> Any:
    try:
        return required_path(value, dotted)
    except EvidenceContractError as exc:
        violations.append(f"{prefix}.{exc}")
        return None


def _unknown_key_violations(
    path: str, value: Mapping[str, Any], expected: set[str]
) -> list[str]:
    unknown = sorted(set(value) - expected)
    return [f"{path} has unknown keys: {unknown}"] if unknown else []


def _deduplicated(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _path(
    snapshots: Mapping[str, dict[str, Any]], stage: str, dotted: str
) -> Any:
    return required_path(snapshots[stage]["state"], dotted)


def _all_stable_equal(values: Sequence[Any]) -> bool:
    return (
        bool(values)
        and all(_identity_is_present(value) for value in values)
        and len({stable_hash(value) for value in values}) == 1
    )


def _without_fields(
    value: Mapping[str, Any], fields: set[str]
) -> dict[str, Any]:
    return {key: nested for key, nested in value.items() if key not in fields}


def _same_fields(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    fields: tuple[str, ...],
) -> bool:
    return all(left[field] == right[field] for field in fields)


def _identity_is_present(value: Any) -> bool:
    if isinstance(value, (Mapping, Sequence)) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return bool(value)
    return value is not None and value != ""


def _shape_violations(
    kind: str, snapshots: Mapping[str, dict[str, Any]]
) -> list[str]:
    violations: list[str] = []

    def record(
        stage: str,
        dotted: str,
        schema: Mapping[str, type[Any]],
    ) -> Mapping[str, Any] | None:
        value = _path(snapshots, stage, dotted)
        path = f"{stage}.state.{dotted}"
        return record_value(path, value, schema)

    def record_value(
        path: str,
        value: Any,
        schema: Mapping[str, type[Any]],
    ) -> Mapping[str, Any] | None:
        if not isinstance(value, Mapping):
            violations.append(f"{path} is not an object")
            return None
        expected = set(schema)
        violations.extend(_unknown_key_violations(path, value, expected))
        missing = sorted(expected - set(value))
        if missing:
            violations.append(f"{path} keys missing: {missing}")
        for field, expected_type in schema.items():
            if field not in value:
                continue
            nested = value[field]
            permits_empty_string = (
                field == "error_message" or field in _EMPTY_STRING_FIELDS
            )
            if type(nested) is not expected_type or (
                expected_type is str
                and not nested
                and not permits_empty_string
            ):
                description = (
                    "an integer"
                    if expected_type is int
                    else (
                        "a string"
                        if permits_empty_string
                        else "a nonempty string"
                    )
                )
                violations.append(f"{path}.{field} is not {description}")
            elif (
                field.endswith("_sha256")
                and _SHA256_PATTERN.fullmatch(nested) is None
            ):
                violations.append(
                    f"{path}.{field} is not a canonical SHA-256 digest"
                )
            elif expected_type is int and nested < 0:
                violations.append(f"{path}.{field} is negative")
            elif expected_type is list:
                item_type = int if field == "blocking_pids" else str
                if any(type(item) is not item_type for item in nested):
                    violations.append(
                        f"{path}.{field} contains a non-"
                        f"{item_type.__name__} value"
                    )
                if field == "blocking_pids" and not nested:
                    violations.append(
                        f"{path}.{field} is empty"
                    )
        return value

    def records(
        stage: str,
        dotted: str,
        schema: Mapping[str, type[Any]],
    ) -> Sequence[Any] | None:
        value = _path(snapshots, stage, dotted)
        path = f"{stage}.state.{dotted}"
        if not isinstance(value, Sequence) or isinstance(
            value, (str, bytes, bytearray)
        ):
            violations.append(f"{path} is not an array")
            return None
        for index, row in enumerate(value):
            row_path = f"{stage}.state.{dotted}[{index}]"
            record_value(row_path, row, schema)
        return value

    def scalar(
        stage: str,
        dotted: str,
        expected_type: type[Any],
        description: str,
    ) -> None:
        try:
            value = _path(snapshots, stage, dotted)
        except EvidenceContractError:
            return
        path = f"{stage}.state.{dotted}"
        if type(value) is not expected_type or (
            expected_type is str and not value
        ):
            violations.append(f"{path} is not {description}")
        elif expected_type is int and value < 0:
            violations.append(f"{path} is negative")

    for stage in STAGES:
        allowed = _allowed_state_fields(kind, stage)
        state = snapshots[stage]["state"]
        for section in sorted(_STATE_KEYS):
            violations.extend(
                _unknown_key_violations(
                    f"{stage}.state.{section}",
                    state[section],
                    allowed[section],
                )
            )
        fixture_schema = (
            _PUBLISHER_FIXTURE_SCHEMA
            if kind.startswith("publisher_")
            else _FIXTURE_SCHEMA
        )
        record(stage, "target.fixture", fixture_schema)

    if kind.startswith("generation_worker_"):
        for stage in STAGES:
            record(stage, "database.task", _TASK_SCHEMA)
        records("during", "database.canon_commits", _CANON_SCHEMA)
        records("after", "database.canon_commits", _CANON_SCHEMA)
        records(
            "after",
            "database.authoritative_identities",
            _AUTHORITATIVE_SCHEMA,
        )
        if kind == "generation_worker_postcommit_crash":
            records(
                "during",
                "database.accepted_bundles",
                _ACCEPTED_BUNDLE_SCHEMA,
            )
            records(
                "after",
                "database.accepted_bundles",
                _ACCEPTED_BUNDLE_SCHEMA,
            )
    elif kind == "qdrant_unavailable":
        for stage in STAGES:
            records(stage, "database.canon_commits", _CANON_SCHEMA)
            outbox = record(stage, "database.outbox", _OUTBOX_SCHEMA)
            if (
                isinstance(outbox, Mapping)
                and isinstance(outbox.get("payload"), Mapping)
            ):
                record_value(
                    f"{stage}.state.database.outbox.payload",
                    outbox["payload"],
                    _OUTBOX_PAYLOAD_SCHEMA,
                )
        records(
            "after",
            "external.replay_baseline_projections",
            _QDRANT_PROJECTION_OBSERVATION_SCHEMA,
        )
        records(
            "after",
            "external.replay_baseline_point_identities",
            _POINT_SCHEMA,
        )
        records(
            "after",
            "external.projections",
            _QDRANT_PROJECTION_OBSERVATION_SCHEMA,
        )
        records(
            "after",
            "external.point_identities",
            _POINT_SCHEMA,
        )
    elif kind == "projection_consumer_unavailable":
        for stage in STAGES:
            records(stage, "database.canon_commits", _CANON_SCHEMA)
            outbox = record(stage, "database.outbox", _OUTBOX_SCHEMA)
            if (
                isinstance(outbox, Mapping)
                and isinstance(outbox.get("payload"), Mapping)
            ):
                record_value(
                    f"{stage}.state.database.outbox.payload",
                    outbox["payload"],
                    _OUTBOX_PAYLOAD_SCHEMA,
                )
        records(
            "after",
            "external.replay_baseline_projections",
            _PROJECTION_OBSERVATION_SCHEMA,
        )
        records(
            "after",
            "external.replay_baseline_projection_identities",
            _PROJECTION_IDENTITY_SCHEMA,
        )
        records(
            "after",
            "external.projections",
            _PROJECTION_OBSERVATION_SCHEMA,
        )
        records(
            "after",
            "external.projection_identities",
            _PROJECTION_IDENTITY_SCHEMA,
        )
    elif kind == "minio_pre_canon_unavailable":
        for stage in STAGES:
            record(stage, "database.candidate", _CANDIDATE_SCHEMA)
        records("during", "database.canon_commits", _CANON_SCHEMA)
        records("after", "database.canon_commits", _CANON_SCHEMA)
        records(
            "after",
            "database.authoritative_identities",
            _AUTHORITATIVE_SCHEMA,
        )
    elif kind == "minio_post_canon_unavailable":
        for stage in STAGES:
            records(stage, "database.canon_commits", _CANON_SCHEMA)
            records(
                stage,
                "database.accepted_bundles",
                _ACCEPTED_BUNDLE_SCHEMA,
            )
        for stage in ("during", "after"):
            record(stage, "database.maintenance", _MAINTENANCE_SCHEMA)
        record(
            "after",
            "external.replay_baseline_artifact",
            _ARTIFACT_SCHEMA,
        )
        record("after", "external.artifact", _ARTIFACT_SCHEMA)
        for dotted, schema in (
            ("database.phase3_replay_baseline", _PHASE3_REPLAY_SCHEMA),
            ("database.phase3_replay_release", _PHASE3_RELEASE_SCHEMA),
            ("database.phase3_replay_final", _PHASE3_REPLAY_SCHEMA),
        ):
            replay = record("after", dotted, schema)
            if (
                isinstance(replay, Mapping)
                and isinstance(replay.get("payload"), Mapping)
            ):
                record_value(
                    f"after.state.{dotted}.payload",
                    replay["payload"],
                    _PHASE3_PAYLOAD_SCHEMA,
                )
        records(
            "after",
            "database.authoritative_identities",
            _AUTHORITATIVE_SCHEMA,
        )
        scalar("after", "barrier.residue_count", int, "an integer")
    elif kind == "publisher_backend_unavailable":
        for stage in STAGES:
            record(stage, "database.job", _BACKEND_JOB_SCHEMA)
            records(stage, "database.jobs", _JOB_IDENTITY_SCHEMA)
            records(stage, "database.attempts", _ATTEMPT_SCHEMA)
            records(stage, "database.receipts", _RECEIPT_SCHEMA)
        for stage in ("during", "after"):
            records(stage, "database.cover_assets", _COVER_ASSET_SCHEMA)
            records(stage, "external.cover_files", _COVER_FILE_SCHEMA)
        stale = record(
            "after",
            "api.stale_token_observation",
            _STALE_TOKEN_OBSERVATION_SCHEMA,
        )
        if isinstance(stale, Mapping) and isinstance(
            stale.get("response"), Mapping
        ):
            record_value(
                "after.state.api.stale_token_observation.response",
                stale["response"],
                _STALE_TOKEN_RESPONSE_SCHEMA,
            )
        records(
            "after",
            "barrier.terminal_writes",
            _TERMINAL_WRITE_OBSERVATION_SCHEMA,
        )
        record("after", "barrier.residue", _BARRIER_RESIDUE_SCHEMA)
    elif kind == "publisher_browser_unavailable":
        for stage in STAGES:
            record(stage, "database.job", _BROWSER_JOB_SCHEMA)
            records(stage, "database.attempts", _ATTEMPT_SCHEMA)
            records(stage, "database.receipts", _RECEIPT_SCHEMA)
            record(stage, "external.browser_heartbeat", _HEARTBEAT_SCHEMA)
    else:
        for stage in STAGES:
            record(stage, "database.job", _RISK_JOB_SCHEMA)
            records(stage, "database.attempts", _ATTEMPT_SCHEMA)
        records("after", "database.receipts", _RECEIPT_SCHEMA)
        records("after", "database.resume_actions", _RESUME_ACTION_SCHEMA)
        record("after", "api.resume_replay", _RESUME_REPLAY_SCHEMA)
    if not violations:
        violations.extend(_relation_violations(kind, snapshots))
    return violations


def _allowed_state_fields(kind: str, stage: str) -> dict[str, set[str]]:
    allowed = {section: set() for section in _STATE_KEYS}
    allowed["target"].add("fixture")
    for dotted in _REQUIRED_PATHS[kind][stage]:
        _, section, field, *_ = dotted.split(".")
        allowed[section].add(field)
    return allowed


def _coverage_violations(
    stage: str,
    dotted: str,
    expected: Sequence[tuple[str, ...]],
    actual: Sequence[tuple[str, ...]],
) -> list[str]:
    expected_counts = Counter(expected)
    actual_counts = Counter(actual)
    complete = (
        bool(expected_counts)
        and expected_counts == actual_counts
        and all(count == 1 for count in expected_counts.values())
        and all(count == 1 for count in actual_counts.values())
    )
    if complete:
        return []
    return [f"{stage}.state.{dotted} coverage mismatch"]


def _relation_violations(
    kind: str, snapshots: Mapping[str, dict[str, Any]]
) -> list[str]:
    violations: list[str] = []
    fixtures = [_path(snapshots, stage, "target.fixture") for stage in STAGES]
    if not _all_stable_equal(fixtures):
        violations.append("after.state.target.fixture mismatch")
        return violations

    fixture = fixtures[0]
    for stage, stage_fixture in zip(STAGES, fixtures):
        if stage_fixture["fault_id"] != snapshots[stage]["fault_id"]:
            violations.append(
                f"{stage}.state.target.fixture.fault_id mismatch"
            )
    publisher = kind.startswith("publisher_")
    expected_resource_type = "publisher_job" if publisher else "chapter"
    if fixture["resource_type"] != expected_resource_type:
        violations.append(
            "before.state.target.fixture.resource_type mismatch"
        )

    if publisher:
        for stage in STAGES:
            job = _path(snapshots, stage, "database.job")
            if job["job_id"] != fixture["resource_id"]:
                violations.append(
                    f"{stage}.state.database.job fixture resource mismatch"
                )
            if job["logical_key"] != fixture["logical_key"]:
                violations.append(
                    f"{stage}.state.database.job fixture logical key mismatch"
                )
    else:
        for stage, dotted in _chapter_record_paths(kind):
            value = _path(snapshots, stage, dotted)
            rows = value if isinstance(value, Sequence) else (value,)
            for index, row in enumerate(rows):
                if row["chapter_id"] != fixture["resource_id"]:
                    suffix = f"[{index}]" if isinstance(value, Sequence) else ""
                    violations.append(
                        f"{stage}.state.{dotted}{suffix} "
                        "fixture resource mismatch"
                    )

    for stage, canon_path, related_path in _canon_relation_paths(kind):
        canon_rows = _path(snapshots, stage, canon_path)
        related_rows = _path(snapshots, stage, related_path)
        if related_path == "database.authoritative_identities":
            expected = [
                (
                    "canon",
                    row["canon_id"],
                    row["natural_key"],
                    row["project_id"],
                    row["chapter_id"],
                )
                for row in canon_rows
            ]
            actual = [
                (
                    row["entity_type"],
                    row["record_id"],
                    row["natural_key"],
                    row["project_id"],
                    row["chapter_id"],
                )
                for row in related_rows
            ]
            violations.extend(
                _coverage_violations(
                    stage,
                    related_path,
                    expected,
                    actual,
                )
            )
            continue
        if not canon_rows:
            continue
        canon = canon_rows[0]
        rows = (
            related_rows
            if isinstance(related_rows, Sequence)
            else (related_rows,)
        )
        for index, row in enumerate(rows):
            if (
                row["project_id"] != canon["project_id"]
                or row["chapter_id"] != canon["chapter_id"]
            ):
                suffix = (
                    f"[{index}]"
                    if isinstance(related_rows, Sequence)
                    else ""
                )
                violations.append(
                    f"{stage}.state.{related_path}{suffix} "
                    "canon resource mismatch"
                )
    violations.extend(_external_relation_violations(kind, snapshots))
    return violations


def _chapter_record_paths(kind: str) -> tuple[tuple[str, str], ...]:
    if kind.startswith("generation_worker_"):
        paths = (
            ("during", "database.canon_commits"),
            ("after", "database.canon_commits"),
            ("after", "database.authoritative_identities"),
        )
        if kind == "generation_worker_postcommit_crash":
            paths += (
                ("during", "database.accepted_bundles"),
                ("after", "database.accepted_bundles"),
            )
        return paths
    if kind in {"qdrant_unavailable", "projection_consumer_unavailable"}:
        return tuple(
            (stage, "database.canon_commits") for stage in STAGES
        )
    if kind == "minio_pre_canon_unavailable":
        return tuple(
            (stage, "database.candidate") for stage in STAGES
        ) + (
            ("during", "database.canon_commits"),
            ("after", "database.canon_commits"),
            ("after", "database.authoritative_identities"),
        )
    return tuple(
        (stage, dotted)
        for stage in STAGES
        for dotted in (
            "database.canon_commits",
            "database.accepted_bundles",
        )
    ) + (("after", "database.authoritative_identities"),)


def _canon_relation_paths(
    kind: str,
) -> tuple[tuple[str, str, str], ...]:
    if kind == "generation_worker_precommit_crash":
        return (
            (
                "after",
                "database.canon_commits",
                "database.authoritative_identities",
            ),
        )
    if kind == "generation_worker_postcommit_crash":
        return (
            (
                "during",
                "database.canon_commits",
                "database.accepted_bundles",
            ),
            (
                "after",
                "database.canon_commits",
                "database.accepted_bundles",
            ),
            (
                "after",
                "database.canon_commits",
                "database.authoritative_identities",
            ),
        )
    if kind == "minio_pre_canon_unavailable":
        return (
            (
                "after",
                "database.canon_commits",
                "database.authoritative_identities",
            ),
        )
    if kind == "minio_post_canon_unavailable":
        return tuple(
            (
                stage,
                "database.canon_commits",
                "database.accepted_bundles",
            )
            for stage in STAGES
        ) + (
            (
                "after",
                "database.canon_commits",
                "database.authoritative_identities",
            ),
        )
    return ()


def _external_relation_violations(
    kind: str, snapshots: Mapping[str, dict[str, Any]]
) -> list[str]:
    violations: list[str] = []
    if kind in {"qdrant_unavailable", "projection_consumer_unavailable"}:
        if kind == "qdrant_unavailable":
            projection_identity_paths = (
                (
                    "external.replay_baseline_projections",
                    "external.replay_baseline_point_identities",
                ),
                ("external.projections", "external.point_identities"),
            )
        else:
            projection_identity_paths = (
                (
                    "external.replay_baseline_projections",
                    "external.replay_baseline_projection_identities",
                ),
                (
                    "external.projections",
                    "external.projection_identities",
                ),
            )
        final_canon_rows = _path(
            snapshots,
            "after",
            "database.canon_commits",
        )
        if not final_canon_rows:
            return violations
        final_canon_id = final_canon_rows[0]["canon_id"]
        for projection_path, identity_path in projection_identity_paths:
            projections = _path(snapshots, "after", projection_path)
            identities = _path(snapshots, "after", identity_path)
            if kind == "qdrant_unavailable":
                expected_coverage = [
                    (
                        row["collection"],
                        row["projection_type"],
                        row["canon_id"],
                        row["identity_id"],
                    )
                    for row in projections
                ]
                actual_coverage = [
                    (
                        row["collection"],
                        row["projection_type"],
                        row["canon_id"],
                        row["point_id"],
                    )
                    for row in identities
                ]
            else:
                expected_coverage = [
                    (
                        row["projection_type"],
                        row["canon_id"],
                        row["identity_id"],
                    )
                    for row in projections
                ]
                actual_coverage = [
                    (
                        row["projection_type"],
                        row["canon_id"],
                        row["projection_id"],
                    )
                    for row in identities
                ]
            violations.extend(
                _coverage_violations(
                    "after",
                    identity_path,
                    expected_coverage,
                    actual_coverage,
                )
            )
            if any(
                row["canon_id"] != final_canon_id
                for row in (*projections, *identities)
            ):
                violations.append(
                    f"after.state.{identity_path} canon identity mismatch"
                )

        for stage in STAGES:
            canon_rows = _path(
                snapshots,
                stage,
                "database.canon_commits",
            )
            if len(canon_rows) != 1:
                continue
            canon = canon_rows[0]
            outbox = _path(snapshots, stage, "database.outbox")
            payload = outbox["payload"]
            if outbox["aggregate_type"] != "project":
                violations.append(
                    f"{stage}.state.database.outbox aggregate type mismatch"
                )
            if outbox["aggregate_id"] != canon["project_id"]:
                violations.append(
                    f"{stage}.state.database.outbox aggregate identity mismatch"
                )
            if (
                payload["canon_commit_id"] != canon["canon_id"]
                or payload["canon_idempotency_key"] != canon["natural_key"]
            ):
                violations.append(
                    f"{stage}.state.database.outbox Canon identity mismatch"
                )
            if payload["project_id"] != canon["project_id"]:
                violations.append(
                    f"{stage}.state.database.outbox project identity mismatch"
                )
            if payload["chapter_number"] != canon["chapter_number"]:
                violations.append(
                    f"{stage}.state.database.outbox chapter identity mismatch"
                )
            if payload["candidate_id"] != canon["candidate_id"]:
                violations.append(
                    f"{stage}.state.database.outbox candidate identity mismatch"
                )
            if (
                payload["schema_version"] != 1
                or payload["trigger"] != "canon_commit"
                or outbox["event_type"] != "canon.projection.requested"
            ):
                violations.append(
                    f"{stage}.state.database.outbox projection payload mismatch"
                )
            expected_event_id = (
                f"{canon['natural_key']}:canon.projection.requested"
            )
            if outbox["event_id"] != expected_event_id:
                violations.append(
                    f"{stage}.state.database.outbox event identity mismatch"
                )
            if outbox["payload_sha256"] != stable_hash(payload):
                violations.append(
                    f"{stage}.state.database.outbox payload hash mismatch"
                )
            error = outbox["error_message"]
            if (
                "\x00" in error
                or len(error) > 4000
                or error != " ".join(error.split())
            ):
                violations.append(
                    f"{stage}.state.database.outbox error is not sanitized"
                )
            if outbox["status"] not in {"pending", "running", "processed"}:
                violations.append(
                    f"{stage}.state.database.outbox status is invalid"
                )
    elif kind == "minio_pre_canon_unavailable":
        candidate = _path(snapshots, "after", "database.candidate")
        canon_rows = _path(
            snapshots, "after", "database.canon_commits"
        )
        if canon_rows and (
            canon_rows[0]["project_id"] != candidate["project_id"]
            or canon_rows[0]["chapter_id"] != candidate["chapter_id"]
        ):
            violations.append(
                "after.state.database.canon_commits[0] "
                "candidate resource mismatch"
            )
    elif kind == "minio_post_canon_unavailable":
        for stage in ("during", "after"):
            canon = _path(
                snapshots, stage, "database.canon_commits"
            )[0]
            maintenance = _path(
                snapshots, stage, "database.maintenance"
            )
            if (
                maintenance["project_id"] != canon["project_id"]
                or maintenance["canon_id"] != canon["canon_id"]
            ):
                violations.append(
                    f"{stage}.state.database.maintenance "
                    "canon identity mismatch"
                )
        canon_rows = _path(
            snapshots,
            "after",
            "database.canon_commits",
        )
        if len(canon_rows) == 1:
            canon = canon_rows[0]
            for dotted in (
                "database.phase3_replay_baseline",
                "database.phase3_replay_release",
                "database.phase3_replay_final",
            ):
                replay = _path(snapshots, "after", dotted)
                payload = replay["payload"]
                if (
                    replay["aggregate_type"] != "project"
                    or replay["aggregate_id"] != canon["project_id"]
                    or replay["event_type"] != "canon.phase3.requested"
                    or replay["event_id"]
                    != (
                        f"{canon['natural_key']}:"
                        "canon.phase3.requested"
                    )
                ):
                    violations.append(
                        f"after.state.{dotted} event identity mismatch"
                    )
                if (
                    payload["schema_version"] != 1
                    or payload["canon_commit_id"] != canon["canon_id"]
                    or payload["canon_idempotency_key"]
                    != canon["natural_key"]
                    or payload["project_id"] != canon["project_id"]
                    or payload["chapter_number"] != canon["chapter_number"]
                    or payload["candidate_id"] != canon["candidate_id"]
                    or replay["canon_idempotency_key"]
                    != payload["canon_idempotency_key"]
                ):
                    violations.append(
                        f"after.state.{dotted} Canon identity mismatch"
                    )
                if replay["payload_sha256"] != stable_hash(payload):
                    violations.append(
                        f"after.state.{dotted} payload hash mismatch"
                    )
                if replay["status"] not in {
                    "pending",
                    "running",
                    "processed",
                }:
                    violations.append(
                        f"after.state.{dotted} status is invalid"
                    )
    elif kind == "publisher_backend_unavailable":
        for stage in STAGES:
            job = _path(snapshots, stage, "database.job")
            jobs = _path(snapshots, stage, "database.jobs")
            attempts = _path(snapshots, stage, "database.attempts")
            receipts = _path(snapshots, stage, "database.receipts")
            violations.extend(
                _coverage_violations(
                    stage,
                    "database.jobs",
                    [(job["job_id"], job["logical_key"], job["task_kind"])],
                    [
                        (row["job_id"], row["logical_key"], row["task_kind"])
                        for row in jobs
                    ],
                )
            )
            known_attempt_ids = {
                row["attempt_id"]
                for row in attempts
                if row["job_id"] == job["job_id"]
            }
            for index, row in enumerate(attempts):
                if row["job_id"] != job["job_id"]:
                    violations.append(
                        f"{stage}.state.database.attempts[{index}] "
                        "job identity mismatch"
                    )
            for index, row in enumerate(receipts):
                if row["job_id"] != job["job_id"]:
                    violations.append(
                        f"{stage}.state.database.receipts[{index}] "
                        "job identity mismatch"
                    )
                if row["attempt_id"] not in known_attempt_ids:
                    violations.append(
                        f"{stage}.state.database.receipts[{index}] "
                        "attempt identity mismatch"
                    )
        after_job = _path(snapshots, "after", "database.job")
        cover_assets = _path(snapshots, "after", "database.cover_assets")
        if len(cover_assets) == 1 and (
            cover_assets[0]["job_id"] != after_job["job_id"]
            or cover_assets[0]["asset_id"] != after_job["artifact_id"]
        ):
            violations.append(
                "after.state.database.cover_assets job identity mismatch"
            )
    elif kind in {
        "publisher_captcha",
        "publisher_mfa",
        "publisher_account_risk",
    }:
        paused_job = _path(snapshots, "during", "database.job")
        for stage in STAGES:
            attempts = _path(snapshots, stage, "database.attempts")
            for index, row in enumerate(attempts):
                if row["job_id"] != paused_job["job_id"]:
                    violations.append(
                        f"{stage}.state.database.attempts[{index}] "
                        "job identity mismatch"
                    )
        for index, action in enumerate(
            _path(snapshots, "after", "database.resume_actions")
        ):
            if action["job_id"] != paused_job["job_id"]:
                violations.append(
                    f"after.state.database.resume_actions[{index}] "
                    "job identity mismatch"
                )
        replay = _path(snapshots, "after", "api.resume_replay")
        if replay["job_id"] != paused_job["job_id"]:
            violations.append(
                "after.state.api.resume_replay job identity mismatch"
            )
    return violations


def _stable_identity_violations(
    kind: str, snapshots: Mapping[str, dict[str, Any]]
) -> list[str]:
    paths: list[tuple[str, str]] = []
    if kind == "generation_worker_precommit_crash":
        paths.extend(
            (
                ("after", "database.canon_commits"),
                ("after", "database.authoritative_identities"),
            )
        )
    elif kind == "generation_worker_postcommit_crash":
        paths.extend(
            (stage, dotted)
            for stage in ("during", "after")
            for dotted in ("database.canon_commits", "database.accepted_bundles")
        )
        paths.append(("after", "database.authoritative_identities"))
    elif kind in {"qdrant_unavailable", "projection_consumer_unavailable"}:
        paths.extend(
            (stage, dotted)
            for stage in STAGES
            for dotted in (
                "database.canon_commits",
                "database.outbox.event_id",
                "database.outbox.aggregate_type",
                "database.outbox.aggregate_id",
                "database.outbox.event_type",
                "database.outbox.payload",
                "database.outbox.payload_sha256",
            )
        )
    elif kind == "minio_pre_canon_unavailable":
        paths.extend((stage, "database.candidate") for stage in STAGES)
        paths.extend(
            (
                ("after", "database.canon_commits"),
                ("after", "database.authoritative_identities"),
            )
        )
    elif kind == "minio_post_canon_unavailable":
        paths.extend(
            (stage, dotted)
            for stage in STAGES
            for dotted in ("database.canon_commits", "database.accepted_bundles")
        )
        paths.extend(
            (stage, "database.maintenance.natural_key")
            for stage in ("during", "after")
        )
        paths.extend(
            (
                ("after", "external.replay_baseline_artifact"),
                ("after", "external.artifact"),
            )
        )
        for dotted in (
            "database.phase3_replay_baseline",
            "database.phase3_replay_release",
            "database.phase3_replay_final",
        ):
            paths.extend(
                ("after", f"{dotted}.{field}")
                for field in (
                    "row_id",
                    "event_id",
                    "aggregate_type",
                    "aggregate_id",
                    "event_type",
                    "payload",
                    "payload_sha256",
                    "canon_idempotency_key",
                )
            )
        paths.append(("after", "database.authoritative_identities"))
    violations: list[str] = []
    for stage, dotted in paths:
        value = _path(snapshots, stage, dotted)
        mutable_keys = sorted(_mutable_identity_keys(value))
        for key in mutable_keys:
            violations.append(
                f"{stage}.state.{dotted} contains mutable identity key '{key}'"
            )
        try:
            stable_hash(value)
        except EvidenceContractError as exc:
            violations.append(f"{stage}.state.{dotted}: {exc}")
    return violations


def _mutable_identity_keys(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, Mapping):
        for key, nested in value.items():
            key_text = str(key)
            if (
                key_text in _MUTABLE_IDENTITY_KEYS
                or key_text.endswith("_timestamp")
                or key_text.endswith("_time")
                or key_text.endswith("_at")
            ):
                found.add(key_text)
            found.update(_mutable_identity_keys(nested))
    elif isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        for nested in value:
            found.update(_mutable_identity_keys(nested))
    return found
