#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime
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
    "project_id": str,
    "canon_commit_id": str,
    "candidate_id": str,
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
    "raw_point_id": str,
    "payload_sha256": str,
    "vector_sha256": str,
    "vector_dimensions": int,
}
_POINT_SCHEMA = {
    "collection": str,
    "projection_type": str,
    "point_id": str,
    "raw_point_id": str,
    "canon_id": str,
    "payload_sha256": str,
    "vector_sha256": str,
    "vector_dimensions": int,
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
_CANON_PUBLISHER_JOB_SCHEMA = {
    **_PUBLISHER_JOB_SCHEMA,
    "canon_commit_id": str,
    "candidate_id": str,
    "chapter_number": int,
    "body_text": str,
    "upload_url": str,
    "abort_requested": bool,
    "owner_token": str,
    "extension_client_id": str,
    "current_attempt_id": str,
    "available_at": str,
    "reconcile_after": str,
    "claimed_at": str,
    "started_at": str,
    "finished_at": str,
    "deleted_at": str,
    "paused_at": str,
    "pause_reason": str,
    "pause_token": str,
    "risk_boundary": str,
    "current_url": str,
    "result_message": str,
    "error_message": str,
    "result_payload": dict,
    "created_at": str,
    "updated_at": str,
    "database_now": str,
}
_PUBLISHER_CANON_SOURCE_SCHEMA = {
    "project_id": str,
    "chapter_plan_id": str,
    "draft_id": str,
    "candidate_id": str,
    "canon_commit_id": str,
    "canon_idempotency_key": str,
    "canon_status": str,
    "candidate_status": str,
    "candidate_canon_status": str,
    "chapter_status": str,
    "chapter_number": int,
    "body_hash": str,
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
    "idempotency_key": str,
    "platform_id": str,
    "remote_book_id": str,
    "remote_chapter_id": str,
    "remote_url": str,
    "official_state": str,
    "content_sha256": str,
    "source": str,
}
_PUBLISHER_DETECTOR_SCHEMA = {
    "detector": str,
    "boundary": str,
    "selector": str,
    "matched_text": str,
    "message": str,
    "risk_reason": str,
    "observed_at": str,
    "attempt_id": str,
}
_PUBLISHER_BROWSER_SCHEMA = {
    "browser_id": str,
    "status": str,
    "probe": str,
}
_PUBLISHER_BROWSER_LIFECYCLE_SCHEMA = {
    "action": str,
    "service": str,
    "fault_id": str,
}
_PUBLISHER_TERMINAL_FAULT_SCHEMA = {
    "job_id": str,
    "mode": str,
    "installed_at": str,
    "observed_at": str,
    "request_url": str,
}
_PUBLISHER_JOURNAL_SCHEMA = {
    "job_id": str,
    "attempt_id": str,
    "journal_phase": str,
    "receipt_key": str,
    "content_sha256": str,
    "fault_observed_at": str,
    "fault_request_url": str,
}
_PUBLISHER_JOURNAL_REPLAY_SCHEMA = {"attempt_id": str}
_PUBLISHER_OPERATOR_RESUME_SCHEMA = {
    "pause_token": str,
    "pause_reason": str,
    "first_disposition": str,
    "replay_disposition": str,
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
_ENDPOINT_IDENTITY_SCHEMA = {
    "schema_version": int,
    "fault_id": str,
    "run_id": str,
    "source_sha": str,
    "source_tree": str,
    "project_name": str,
    "candidate_manifest_sha256": str,
    "candidate_identity_sha256": str,
    "sentinel": dict,
    "api": dict,
    "mcp": dict,
    "database": dict,
    "identity_sha256": str,
}
_OPTIONAL_ENDPOINTS_BY_KIND = {
    "qdrant_unavailable": ("qdrant",),
    "minio_pre_canon_unavailable": ("minio",),
    "minio_post_canon_unavailable": ("minio",),
}
_ENDPOINT_HTTP_KEYS = {
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
_EMPTY_STRING_FIELDS = {
    "artifact_id",
    "candidate_id",
    "canon_commit_id",
    "chapter_title",
    "claimed_at",
    "current_attempt_id",
    "current_url",
    "deleted_at",
    "error_code",
    "extension_client_id",
    "finished_at",
    "owner_token",
    "pause_reason",
    "pause_token",
    "paused_at",
    "project_id",
    "reconcile_after",
    "result_message",
    "risk_boundary",
    "started_at",
    "upload_url",
}


class EvidenceContractError(RuntimeError):
    pass


FAULT_CONTRACTS: dict[str, dict[str, Any]] = {
    "generation_worker_precommit_crash": {
        "isolated_endpoint_identity": True,
        "same_task_reclaimed": True,
        "lease_epoch_increased": True,
        "canon_commits_during_fault": 0,
        "canon_commits_after_recovery": 1,
        "duplicate_authoritative_identities": 0,
    },
    "generation_worker_postcommit_crash": {
        "isolated_endpoint_identity": True,
        "same_task_reclaimed": True,
        "lease_epoch_increased": True,
        "canon_identity_unchanged": True,
        "accepted_identity_unchanged": True,
        "duplicate_authoritative_identities": 0,
    },
    "qdrant_unavailable": {
        "isolated_endpoint_identity": True,
        "canon_identity_unchanged": True,
        "outbox_retry_observed": True,
        "projection_converged": True,
        "replay_identity_unchanged": True,
        "duplicate_vector_identities": 0,
    },
    "projection_consumer_unavailable": {
        "isolated_endpoint_identity": True,
        "canon_identity_unchanged": True,
        "durable_outbox_preserved": True,
        "projection_converged": True,
        "replay_identity_unchanged": True,
        "duplicate_projection_identities": 0,
    },
    "minio_pre_canon_unavailable": {
        "isolated_endpoint_identity": True,
        "canon_commits_during_fault": 0,
        "same_candidate_retried": True,
        "canon_commits_after_recovery": 1,
        "duplicate_authoritative_identities": 0,
    },
    "minio_post_canon_unavailable": {
        "isolated_endpoint_identity": True,
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
        "isolated_endpoint_identity": True,
        "canon_source_bound": True,
        "fixture_safe": True,
        "backend_fault_observed": True,
        "same_job_attempt_converged": True,
        "retryable_during_fault": True,
        "journal_replayed": True,
        "external_effect_at_most_once": True,
        "duplicate_jobs": 0,
        "duplicate_attempts": 0,
        "duplicate_receipts": 0,
        "attempt_count": 1,
        "receipt_count": 1,
    },
    "publisher_browser_unavailable": {
        "isolated_endpoint_identity": True,
        "canon_source_bound": True,
        "fixture_safe": True,
        "browser_restarted": True,
        "same_job_attempt_converged": True,
        "retryable_during_fault": True,
        "journal_replayed": True,
        "external_effect_at_most_once": True,
        "duplicate_jobs": 0,
        "duplicate_attempts": 0,
        "duplicate_receipts": 0,
        "attempt_count": 1,
        "receipt_count": 1,
    },
    "publisher_captcha": {
        "isolated_endpoint_identity": True,
        "canon_source_bound": True,
        "fixture_safe": True,
        "typed_pause_from_detector": True,
        "operator_action_recorded": True,
        "resume_replay_idempotent": True,
        "no_bypass": True,
        "external_effect_at_most_once": True,
        "duplicate_jobs": 0,
        "duplicate_attempts": 0,
        "duplicate_receipts": 0,
        "attempt_count": 2,
        "receipt_count": 1,
    },
    "publisher_mfa": {
        "isolated_endpoint_identity": True,
        "canon_source_bound": True,
        "fixture_safe": True,
        "typed_pause_from_detector": True,
        "operator_action_recorded": True,
        "resume_replay_idempotent": True,
        "no_bypass": True,
        "external_effect_at_most_once": True,
        "duplicate_jobs": 0,
        "duplicate_attempts": 0,
        "duplicate_receipts": 0,
        "attempt_count": 2,
        "receipt_count": 1,
    },
    "publisher_account_risk": {
        "isolated_endpoint_identity": True,
        "canon_source_bound": True,
        "fixture_safe": True,
        "typed_pause_from_detector": True,
        "operator_action_recorded": True,
        "resume_replay_idempotent": True,
        "no_bypass": True,
        "external_effect_at_most_once": True,
        "duplicate_jobs": 0,
        "duplicate_attempts": 0,
        "duplicate_receipts": 0,
        "attempt_count": 2,
        "receipt_count": 1,
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
            "state.target.endpoint_identity",
            "state.database.canon_source",
            "state.database.job",
            "state.database.job_identity_count",
            "state.database.status",
            "state.database.attempts",
            "state.database.receipts",
            "state.database.resume_actions",
            "state.database.detector_evidence",
            "state.external.browser",
            "state.external.fixture_id",
            "state.external.effect_key_sha256",
            "state.external.upload_effect_count",
        ),
        "during": (
            "state.target.endpoint_identity",
            "state.database.canon_source",
            "state.database.job",
            "state.database.job_identity_count",
            "state.database.status",
            "state.database.attempts",
            "state.database.receipts",
            "state.database.resume_actions",
            "state.database.detector_evidence",
            "state.external.terminal_fault",
            "state.external.journal",
            "state.external.fixture_id",
            "state.external.effect_key_sha256",
            "state.external.upload_effect_count",
        ),
        "after": (
            "state.target.endpoint_identity",
            "state.database.canon_source",
            "state.database.job",
            "state.database.job_identity_count",
            "state.database.status",
            "state.database.attempts",
            "state.database.receipts",
            "state.database.resume_actions",
            "state.database.detector_evidence",
            "state.external.journal_replay",
            "state.external.fixture_id",
            "state.external.effect_key_sha256",
            "state.external.upload_effect_count",
        ),
    },
    "publisher_browser_unavailable": {
        "before": (
            "state.target.endpoint_identity",
            "state.database.canon_source",
            "state.database.job",
            "state.database.job_identity_count",
            "state.database.status",
            "state.database.attempts",
            "state.database.receipts",
            "state.database.resume_actions",
            "state.database.detector_evidence",
            "state.external.browser",
            "state.external.fixture_id",
            "state.external.effect_key_sha256",
            "state.external.upload_effect_count",
        ),
        "during": (
            "state.target.endpoint_identity",
            "state.database.canon_source",
            "state.database.job",
            "state.database.job_identity_count",
            "state.database.status",
            "state.database.attempts",
            "state.database.receipts",
            "state.database.resume_actions",
            "state.database.detector_evidence",
            "state.external.terminal_fault",
            "state.external.journal",
            "state.external.browser_fault",
            "state.external.fixture_id",
            "state.external.effect_key_sha256",
            "state.external.upload_effect_count",
        ),
        "after": (
            "state.target.endpoint_identity",
            "state.database.canon_source",
            "state.database.job",
            "state.database.job_identity_count",
            "state.database.status",
            "state.database.attempts",
            "state.database.receipts",
            "state.database.resume_actions",
            "state.database.detector_evidence",
            "state.external.browser_recovery",
            "state.external.journal_replay",
            "state.external.fixture_id",
            "state.external.effect_key_sha256",
            "state.external.upload_effect_count",
        ),
    },
    "publisher_captcha": {
        "before": (
            "state.target.endpoint_identity",
            "state.database.canon_source",
            "state.database.job",
            "state.database.job_identity_count",
            "state.database.status",
            "state.database.attempts",
            "state.database.receipts",
            "state.database.resume_actions",
            "state.database.detector_evidence",
            "state.external.browser",
            "state.external.fixture_id",
            "state.external.effect_key_sha256",
            "state.external.upload_effect_count",
        ),
        "during": (
            "state.target.endpoint_identity",
            "state.database.canon_source",
            "state.database.job",
            "state.database.job_identity_count",
            "state.database.status",
            "state.database.attempts",
            "state.database.receipts",
            "state.database.resume_actions",
            "state.database.detector_evidence",
            "state.external.detector_evidence",
            "state.external.fixture_id",
            "state.external.effect_key_sha256",
            "state.external.upload_effect_count",
        ),
        "after": (
            "state.target.endpoint_identity",
            "state.database.canon_source",
            "state.database.job",
            "state.database.job_identity_count",
            "state.database.status",
            "state.database.attempts",
            "state.database.receipts",
            "state.database.resume_actions",
            "state.database.detector_evidence",
            "state.external.operator_resume",
            "state.external.fixture_id",
            "state.external.effect_key_sha256",
            "state.external.upload_effect_count",
        ),
    },
}
_REQUIRED_PATHS["publisher_mfa"] = _REQUIRED_PATHS["publisher_captcha"]
_REQUIRED_PATHS["publisher_account_risk"] = _REQUIRED_PATHS[
    "publisher_captcha"
]


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
            _capture_required(
                snapshot,
                "state.target.endpoint_identity",
                stage,
                violations,
            )
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
        if kind in _PUBLISHER_FAULTS:
            violations.extend(_publisher_snapshot_violations(kind, snapshots))
        else:
            violations.extend(_shape_violations(kind, snapshots))
    if not violations and kind not in _PUBLISHER_FAULTS:
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
        "isolated_endpoint_identity": _endpoint_identity(
            "generation_worker_precommit_crash", snapshots
        ),
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
        "isolated_endpoint_identity": _endpoint_identity(
            "generation_worker_postcommit_crash", snapshots
        ),
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
        "isolated_endpoint_identity": _endpoint_identity(
            "qdrant_unavailable", snapshots
        ),
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
        "isolated_endpoint_identity": _endpoint_identity(
            "projection_consumer_unavailable", snapshots
        ),
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
        "isolated_endpoint_identity": _endpoint_identity(
            "minio_pre_canon_unavailable", snapshots
        ),
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
        "isolated_endpoint_identity": _endpoint_identity(
            "minio_post_canon_unavailable", snapshots
        ),
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
    "canon_commit_id",
    "candidate_id",
    "chapter_number",
    "body_text",
    "upload_url",
)
_RISK_REASONS = {
    "publisher_captcha": "captcha",
    "publisher_mfa": "mfa",
    "publisher_account_risk": "account_risk",
}
_PUBLISHER_FAULTS = {
    "publisher_backend_unavailable",
    "publisher_browser_unavailable",
    *_RISK_REASONS,
}


def _publisher_fixture_safe(
    kind: str,
    snapshots: Mapping[str, dict[str, Any]],
) -> bool:
    fixtures = [_path(snapshots, stage, "target.fixture") for stage in STAGES]
    jobs = [_path(snapshots, stage, "database.job") for stage in STAGES]
    sources = [
        _path(snapshots, stage, "database.canon_source") for stage in STAGES
    ]
    return (
        _all_stable_equal(fixtures)
        and _all_stable_equal(sources)
        and all(
            fixture["resource_type"] == "publisher_job"
            and fixture["resource_id"] == job["job_id"]
            and fixture["logical_key"] == job["logical_key"]
            and fixture["project_id"] == job["project_id"]
            and fixture["canon_commit_id"] == job["canon_commit_id"]
            and fixture["candidate_id"] == job["candidate_id"]
            and fixture["fault_id"] == snapshots[stage]["fault_id"]
            for stage, fixture, job in zip(STAGES, fixtures, jobs)
        )
        and all(
            job["task_kind"] == "chapter_upload"
            and bool(job["project_id"])
            and job["platform_id"] == "qidian"
            and job["publish"] is True
            and job["book_name"].startswith(_PUBLISHER_BOOK_NAME)
            and job["chapter_title"].startswith(_PUBLISHER_CHAPTER_TITLE)
            and job["body_text"].startswith(
                "Generic publisher recovery fixture content."
            )
            and job["body_sha256"]
            == hashlib.sha256(job["body_text"].encode("utf-8")).hexdigest()
            and job["upload_url"].startswith("https://write.qq.com/")
            and job["unsafe_payload_paths"] == []
            for job in jobs
        )
        and all(
            source["project_id"] == job["project_id"]
            and source["canon_commit_id"] == job["canon_commit_id"]
            and source["candidate_id"] == job["candidate_id"]
            and source["chapter_number"] == job["chapter_number"]
            and source["body_hash"] == job["body_sha256"]
            and source["canon_status"] == "committed"
            and source["candidate_status"] == "accepted"
            and source["candidate_canon_status"] == "committed"
            and source["chapter_status"] == "accepted"
            for source, job in zip(sources, jobs)
        )
    )


def _endpoint_identity(
    kind: str,
    snapshots: Mapping[str, dict[str, Any]],
) -> bool:
    records = [
        _path(snapshots, stage, "target.endpoint_identity")
        for stage in STAGES
    ]
    if not _all_stable_equal(records):
        return False
    record = records[0]
    sentinel = record["sentinel"]
    api = record["api"]
    mcp = record["mcp"]
    database = record["database"]
    optional_names = _OPTIONAL_ENDPOINTS_BY_KIND.get(kind, ())
    optional = [record[name] for name in optional_names]
    try:
        addresses = [
            ipaddress.ip_address(endpoint["host"])
            for endpoint in (api, mcp, database, *optional)
        ]
    except ValueError:
        return False
    return (
        record["identity_sha256"]
        == stable_hash(
            {
                key: value
                for key, value in record.items()
                if key != "identity_sha256"
            }
        )
        and record["fault_id"] == snapshots["before"]["fault_id"]
        and record["source_sha"] == snapshots["before"]["source_sha"]
        and re.fullmatch(r"[0-9a-f]{32}", record["run_id"]) is not None
        and record["project_name"]
        == f"forwin-v5-recovery-{record['run_id']}"
        and sentinel
        == {
            "table": "forwin_recovery_run_sentinel",
            "sentinel_id": sentinel["sentinel_id"],
            "run_id": record["run_id"],
            "fault_id": record["fault_id"],
            "source_sha": record["source_sha"],
        }
        and _SHA256_PATTERN.fullmatch(sentinel["sentinel_id"]) is not None
        and all(address.is_loopback for address in addresses)
        and api["scheme"] == "http"
        and api["endpoint_path"] == ""
        and api["health_path"] == "/health"
        and api["health_status"] == 200
        and api["service"] == "forwin"
        and api["container_port"] == 8899
        and mcp["scheme"] == "http"
        and mcp["endpoint_path"] == "/mcp"
        and mcp["health_path"] == "/health"
        and mcp["health_status"] == 200
        and mcp["service"] == "forwin-mcp"
        and mcp["container_port"] == 8896
        and database["scheme"] == "postgresql"
        and database["database"] == "forwin"
        and database["service"] == "postgres"
        and database["container_port"] == 5432
        and all(
            endpoint["scheme"] == "http"
            and endpoint["endpoint_path"] == ""
            and endpoint["health_status"] == 200
            and endpoint["service"] == name
            and endpoint["container_port"]
            == {"qdrant": 6333, "minio": 9000}[name]
            and endpoint["health_path"]
            == {
                "qdrant": "/readyz",
                "minio": "/minio/health/ready",
            }[name]
            for name, endpoint in zip(optional_names, optional, strict=True)
        )
        and all(
            1 <= endpoint["port"] <= 65535
            and bool(endpoint["container_id"])
            and endpoint["image_id"].startswith("sha256:")
            for endpoint in (api, mcp, database, *optional)
        )
    )


def _publisher_snapshot_violations(
    kind: str,
    snapshots: Mapping[str, dict[str, Any]],
) -> list[str]:
    violations: list[str] = []

    def record(
        stage: str,
        dotted: str,
        schema: Mapping[str, type[Any]],
        *,
        allow_empty: bool = False,
    ) -> Mapping[str, Any] | None:
        value = _path(snapshots, stage, dotted)
        path = f"{stage}.state.{dotted}"
        if not isinstance(value, Mapping):
            violations.append(f"{path} is not an object")
            return None
        if allow_empty and not value:
            return value
        expected = set(schema)
        violations.extend(_unknown_key_violations(path, value, expected))
        missing = sorted(expected - set(value))
        if missing:
            violations.append(f"{path} keys missing: {missing}")
        for field, expected_type in schema.items():
            if field not in value:
                continue
            nested = value[field]
            permits_empty = field in _EMPTY_STRING_FIELDS or field in {
                "error_message",
                "message",
            }
            if type(nested) is not expected_type or (
                expected_type is str and not nested and not permits_empty
            ):
                violations.append(
                    f"{path}.{field} has invalid {expected_type.__name__} value"
                )
            elif (
                expected_type is str
                and field.endswith("_sha256")
                and _SHA256_PATTERN.fullmatch(nested) is None
            ):
                violations.append(f"{path}.{field} is not a canonical SHA-256 digest")
            elif expected_type is int and nested < 0:
                violations.append(f"{path}.{field} is negative")
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
            if not isinstance(row, Mapping):
                violations.append(f"{path}[{index}] is not an object")
                continue
            expected = set(schema)
            violations.extend(
                _unknown_key_violations(f"{path}[{index}]", row, expected)
            )
            missing = sorted(expected - set(row))
            if missing:
                violations.append(f"{path}[{index}] keys missing: {missing}")
            for field, expected_type in schema.items():
                if field not in row:
                    continue
                nested = row[field]
                permits_empty = field in _EMPTY_STRING_FIELDS
                if type(nested) is not expected_type or (
                    expected_type is str and not nested and not permits_empty
                ):
                    violations.append(
                        f"{path}[{index}].{field} has invalid "
                        f"{expected_type.__name__} value"
                    )
                elif (
                    expected_type is str
                    and field.endswith("_sha256")
                    and _SHA256_PATTERN.fullmatch(nested) is None
                ):
                    violations.append(
                        f"{path}[{index}].{field} is not a canonical SHA-256 digest"
                    )
                elif expected_type is int and nested < 0:
                    violations.append(f"{path}[{index}].{field} is negative")
        return value

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
        record(stage, "target.fixture", _PUBLISHER_FIXTURE_SCHEMA)
        endpoint = record(
            stage,
            "target.endpoint_identity",
            _ENDPOINT_IDENTITY_SCHEMA,
        )
        if isinstance(endpoint, Mapping):
            sentinel = endpoint.get("sentinel")
            http_endpoints = [endpoint.get(name) for name in ("api", "mcp")]
            database_endpoint = endpoint.get("database")
            if (
                not isinstance(sentinel, Mapping)
                or set(sentinel)
                != {
                    "table",
                    "sentinel_id",
                    "run_id",
                    "fault_id",
                    "source_sha",
                }
                or any(
                    not isinstance(item, Mapping)
                    or set(item) != _ENDPOINT_HTTP_KEYS
                    for item in http_endpoints
                )
                or not isinstance(database_endpoint, Mapping)
                or set(database_endpoint) != _ENDPOINT_DATABASE_KEYS
            ):
                violations.append(
                    f"{stage}.state.target.endpoint identity nested field set mismatch"
                )
        record(stage, "database.canon_source", _PUBLISHER_CANON_SOURCE_SCHEMA)
        job = record(stage, "database.job", _CANON_PUBLISHER_JOB_SCHEMA)
        records(stage, "database.attempts", _ATTEMPT_SCHEMA)
        records(stage, "database.receipts", _RECEIPT_SCHEMA)
        records(stage, "database.resume_actions", _RESUME_ACTION_SCHEMA)
        detector = record(
            stage,
            "database.detector_evidence",
            _PUBLISHER_DETECTOR_SCHEMA,
            allow_empty=True,
        )
        count = _path(snapshots, stage, "database.job_identity_count")
        status = _path(snapshots, stage, "database.status")
        if type(count) is not int:
            violations.append(
                f"{stage}.state.database.job_identity_count is not an integer"
            )
        elif count < 0:
            violations.append(
                f"{stage}.state.database.job_identity_count is negative"
            )
        if type(status) is not str or not status:
            violations.append(f"{stage}.state.database.status is not a nonempty string")
        elif isinstance(job, Mapping) and status != job.get("status"):
            violations.append(f"{stage}.state.database.status does not match job")
        if kind in _RISK_REASONS and stage in {"during", "after"} and not detector:
            violations.append(f"{stage}.state.database.detector_evidence is empty")

        external = state["external"]
        fixture_id = external.get("fixture_id")
        effect_hash = external.get("effect_key_sha256")
        effect_count = external.get("upload_effect_count")
        if type(fixture_id) is not str or not fixture_id:
            violations.append(f"{stage}.state.external.fixture_id is empty")
        if (
            type(effect_hash) is not str
            or _SHA256_PATTERN.fullmatch(effect_hash) is None
        ):
            violations.append(
                f"{stage}.state.external.effect_key_sha256 is not a canonical SHA-256 digest"
            )
        if type(effect_count) is not int or effect_count < 0:
            violations.append(
                f"{stage}.state.external.upload_effect_count is not a nonnegative integer"
            )

    record("before", "external.browser", _PUBLISHER_BROWSER_SCHEMA)
    if kind in {
        "publisher_backend_unavailable",
        "publisher_browser_unavailable",
    }:
        record("during", "external.terminal_fault", _PUBLISHER_TERMINAL_FAULT_SCHEMA)
        record("during", "external.journal", _PUBLISHER_JOURNAL_SCHEMA)
        record("after", "external.journal_replay", _PUBLISHER_JOURNAL_REPLAY_SCHEMA)
    if kind == "publisher_browser_unavailable":
        for stage, dotted, action in (
            ("during", "external.browser_fault", "fault_service_stopped"),
            ("after", "external.browser_recovery", "fault_service_recovered"),
        ):
            value = record(stage, dotted, _PUBLISHER_BROWSER_LIFECYCLE_SCHEMA)
            path = f"{stage}.state.{dotted}"
            if isinstance(value, Mapping) and (
                value.get("action") != action
                or value.get("service") != "publisher-browser"
                or value.get("fault_id") != snapshots[stage]["fault_id"]
            ):
                violations.append(f"{path} does not describe the real browser lifecycle")
    if kind in _RISK_REASONS:
        record("during", "external.detector_evidence", _PUBLISHER_DETECTOR_SCHEMA)
        record("after", "external.operator_resume", _PUBLISHER_OPERATOR_RESUME_SCHEMA)

    if not violations:
        violations.extend(_publisher_relation_violations(kind, snapshots))
    return _deduplicated(violations)


def _publisher_relation_violations(
    kind: str,
    snapshots: Mapping[str, dict[str, Any]],
) -> list[str]:
    violations: list[str] = []
    fixtures = [_path(snapshots, stage, "target.fixture") for stage in STAGES]
    jobs = [_path(snapshots, stage, "database.job") for stage in STAGES]
    sources = [_path(snapshots, stage, "database.canon_source") for stage in STAGES]
    if not _all_stable_equal(fixtures):
        violations.append("publisher target fixture is not stable")
    if not _all_stable_equal(sources):
        violations.append("publisher Canon source is not stable")
    identities = [
        {field: job[field] for field in _PUBLISHER_JOB_IDENTITY_FIELDS}
        for job in jobs
    ]
    if not _all_stable_equal(identities):
        violations.append("publisher Canon job identity is not stable")
    if not _publisher_fixture_safe(kind, snapshots):
        violations.append("publisher fixture is not bound to a safe Canon chapter upload")
    if not _endpoint_identity(kind, snapshots):
        violations.append(f"{kind}.endpoint identity is not stable or bound")

    for stage, fixture, job in zip(STAGES, fixtures, jobs, strict=True):
        external = _path(snapshots, stage, "external")
        attempts = _path(snapshots, stage, "database.attempts")
        receipts = _path(snapshots, stage, "database.receipts")
        actions = _path(snapshots, stage, "database.resume_actions")
        detector = _path(snapshots, stage, "database.detector_evidence")
        if external["fixture_id"] != fixture["fixture_id"]:
            violations.append(f"{stage}.state.external fixture identity mismatch")
        attempt_ids = set()
        for index, attempt in enumerate(attempts):
            if attempt["job_id"] != job["job_id"]:
                violations.append(
                    f"{stage}.state.database.attempts[{index}] job identity mismatch"
                )
            attempt_ids.add(attempt["attempt_id"])
        for index, receipt in enumerate(receipts):
            if (
                receipt["job_id"] != job["job_id"]
                or receipt["attempt_id"] not in attempt_ids
                or receipt["idempotency_key"] != job["logical_key"]
                or receipt["platform_id"] != job["platform_id"]
                or receipt["content_sha256"] != job["body_sha256"]
                or receipt["remote_url"] != job["upload_url"]
                or receipt["official_state"] != "published"
                or receipt["source"] != "extension"
            ):
                violations.append(
                    f"{stage}.state.database.receipts[{index}] publisher identity mismatch"
                )
        for index, action in enumerate(actions):
            if action["job_id"] != job["job_id"]:
                violations.append(
                    f"{stage}.state.database.resume_actions[{index}] job identity mismatch"
                )
        if detector and detector["attempt_id"] not in attempt_ids:
            violations.append(
                f"{stage}.state.database.detector_evidence attempt identity mismatch"
            )
    return violations


def _publisher_terminal_recovery(
    kind: str,
    snapshots: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    jobs = [_path(snapshots, stage, "database.job") for stage in STAGES]
    attempts = [_path(snapshots, stage, "database.attempts") for stage in STAGES]
    receipts = [_path(snapshots, stage, "database.receipts") for stage in STAGES]
    counts = [
        _path(snapshots, stage, "external.upload_effect_count") for stage in STAGES
    ]
    during_attempt = attempts[1][0] if len(attempts[1]) == 1 else {}
    after_attempt = attempts[2][0] if len(attempts[2]) == 1 else {}
    receipt = receipts[2][0] if len(receipts[2]) == 1 else {}
    journal = _path(snapshots, "during", "external.journal")
    replay = _path(snapshots, "after", "external.journal_replay")
    terminal = _path(snapshots, "during", "external.terminal_fault")
    attempt_identity_fields = (
        "attempt_id",
        "job_id",
        "attempt_number",
        "attempt_kind",
        "owner_token",
        "lease_epoch",
        "content_sha256",
    )
    converged = (
        attempts[0] == []
        and len(attempts[1]) == 1
        and len(attempts[2]) == 1
        and _same_fields(during_attempt, after_attempt, attempt_identity_fields)
        and jobs[0]["status"] == "pending"
        and jobs[1]["status"] == "running"
        and jobs[1]["current_attempt_id"] == during_attempt.get("attempt_id")
        and during_attempt.get("status") == "running"
        and during_attempt.get("phase") == "mutation_started"
        and jobs[2]["status"] == "succeeded"
        and jobs[2]["current_attempt_id"] == after_attempt.get("attempt_id")
        and after_attempt.get("status") == "succeeded"
        and after_attempt.get("phase") == "result_submitted"
        and len(receipts[2]) == 1
        and receipt.get("attempt_id") == after_attempt.get("attempt_id")
    )
    expected_mode = (
        "backend_unavailable"
        if kind == "publisher_backend_unavailable"
        else "browser_shutdown_barrier"
    )
    base = {
        "isolated_endpoint_identity": _endpoint_identity(kind, snapshots),
        "canon_source_bound": _all_stable_equal(
            [_path(snapshots, stage, "database.canon_source") for stage in STAGES]
        ),
        "fixture_safe": _publisher_fixture_safe(kind, snapshots),
        "same_job_attempt_converged": converged,
        "retryable_during_fault": (
            converged
            and receipts[1] == []
            and terminal["mode"] == expected_mode
            and terminal["job_id"] == jobs[1]["job_id"]
            and bool(terminal["observed_at"])
            and journal["job_id"] == jobs[1]["job_id"]
            and journal["attempt_id"] == during_attempt.get("attempt_id")
            and journal["journal_phase"] == "ack_pending"
            and journal["content_sha256"] == jobs[1]["body_sha256"]
            and journal["fault_observed_at"] == terminal["observed_at"]
        ),
        "journal_replayed": (
            converged
            and replay["attempt_id"] == journal["attempt_id"]
            and receipt.get("natural_key") == journal["receipt_key"]
        ),
        "external_effect_at_most_once": counts == [0, 1, 1],
        "duplicate_jobs": max(
            int(_path(snapshots, stage, "database.job_identity_count")) - 1
            for stage in STAGES
        ),
        "duplicate_attempts": duplicate_excess(
            attempts[2], ("job_id", "attempt_number")
        ),
        "duplicate_receipts": duplicate_excess(
            receipts[2], ("job_id", "natural_key")
        ),
        "attempt_count": len(attempts[2]),
        "receipt_count": len(receipts[2]),
    }
    if kind == "publisher_backend_unavailable":
        return {
            **{
                key: base[key]
                for key in (
                    "isolated_endpoint_identity",
                    "canon_source_bound",
                    "fixture_safe",
                )
            },
            "backend_fault_observed": (
                terminal["mode"] == "backend_unavailable"
                and terminal["request_url"] == journal["fault_request_url"]
            ),
            **{
                key: base[key]
                for key in (
                    "same_job_attempt_converged",
                    "retryable_during_fault",
                    "journal_replayed",
                    "external_effect_at_most_once",
                    "duplicate_jobs",
                    "duplicate_attempts",
                    "duplicate_receipts",
                    "attempt_count",
                    "receipt_count",
                )
            },
        }
    stopped = _path(snapshots, "during", "external.browser_fault")
    recovered = _path(snapshots, "after", "external.browser_recovery")
    return {
        **{
            key: base[key]
            for key in (
                "isolated_endpoint_identity",
                "canon_source_bound",
                "fixture_safe",
            )
        },
        "browser_restarted": (
            stopped["action"] == "fault_service_stopped"
            and recovered["action"] == "fault_service_recovered"
            and stopped["service"] == recovered["service"] == "publisher-browser"
            and stopped["fault_id"] == recovered["fault_id"]
            == snapshots["before"]["fault_id"]
        ),
        **{
            key: base[key]
            for key in (
                "same_job_attempt_converged",
                "retryable_during_fault",
                "journal_replayed",
                "external_effect_at_most_once",
                "duplicate_jobs",
                "duplicate_attempts",
                "duplicate_receipts",
                "attempt_count",
                "receipt_count",
            )
        },
    }


def _publisher_backend(
    snapshots: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    return _publisher_terminal_recovery(
        "publisher_backend_unavailable",
        snapshots,
    )


def _publisher_browser(
    snapshots: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    return _publisher_terminal_recovery(
        "publisher_browser_unavailable",
        snapshots,
    )


def _publisher_risk(
    kind: str,
    snapshots: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    jobs = [_path(snapshots, stage, "database.job") for stage in STAGES]
    attempts = [_path(snapshots, stage, "database.attempts") for stage in STAGES]
    receipts = [_path(snapshots, stage, "database.receipts") for stage in STAGES]
    actions = _path(snapshots, "after", "database.resume_actions")
    detector = _path(snapshots, "during", "database.detector_evidence")
    external_detector = _path(
        snapshots, "during", "external.detector_evidence"
    )
    resume = _path(snapshots, "after", "external.operator_resume")
    counts = [
        _path(snapshots, stage, "external.upload_effect_count") for stage in STAGES
    ]
    expected_reason = _RISK_REASONS[kind]
    paused = attempts[1][0] if len(attempts[1]) == 1 else {}
    final_paused = attempts[2][0] if len(attempts[2]) >= 1 else {}
    succeeded = attempts[2][1] if len(attempts[2]) == 2 else {}
    action = actions[0] if len(actions) == 1 else {}
    receipt = receipts[2][0] if len(receipts[2]) == 1 else {}
    stable_pause_fields = (
        "attempt_id",
        "job_id",
        "attempt_number",
        "attempt_kind",
        "owner_token",
        "lease_epoch",
        "status",
        "phase",
        "content_sha256",
        "error_code",
    )
    typed_pause = (
        attempts[0] == []
        and len(attempts[1]) == 1
        and jobs[0]["status"] == "pending"
        and jobs[1]["status"] == "paused"
        and jobs[1]["current_attempt_id"] == paused.get("attempt_id")
        and jobs[1]["pause_token"] == paused.get("attempt_id")
        and jobs[1]["pause_reason"] == expected_reason
        and jobs[1]["risk_boundary"] == "pre-mutation"
        and paused.get("status") == "paused"
        and paused.get("phase") == "claimed"
        and paused.get("error_code") == expected_reason
        and detector == external_detector
        and detector.get("detector") == "publisher-risk-v1"
        and detector.get("boundary") == "pre-mutation"
        and detector.get("risk_reason") == expected_reason
        and detector.get("attempt_id") == paused.get("attempt_id")
        and bool(detector.get("selector"))
        and bool(detector.get("matched_text"))
    )
    action_recorded = (
        len(actions) == 1
        and action.get("job_id") == jobs[2]["job_id"]
        and action.get("action") == "resume"
        and action.get("pause_token") == paused.get("attempt_id")
        and action.get("natural_key")
        == f"{jobs[2]['job_id']}:resume:{paused.get('attempt_id')}"
        and action.get("auth_method") in {"basic", "trusted_proxy"}
        and action.get("old_status") == "paused"
        and action.get("new_status") == "pending"
    )
    converged = (
        typed_pause
        and len(attempts[2]) == 2
        and _same_fields(paused, final_paused, stable_pause_fields)
        and succeeded.get("attempt_number") == paused.get("attempt_number", 0) + 1
        and succeeded.get("status") == "succeeded"
        and succeeded.get("phase") == "result_submitted"
        and succeeded.get("content_sha256") == jobs[2]["body_sha256"]
        and jobs[2]["status"] == "succeeded"
        and jobs[2]["current_attempt_id"] == succeeded.get("attempt_id")
        and len(receipts[2]) == 1
        and receipt.get("attempt_id") == succeeded.get("attempt_id")
    )
    return {
        "isolated_endpoint_identity": _endpoint_identity(kind, snapshots),
        "canon_source_bound": _all_stable_equal(
            [_path(snapshots, stage, "database.canon_source") for stage in STAGES]
        ),
        "fixture_safe": _publisher_fixture_safe(kind, snapshots),
        "typed_pause_from_detector": typed_pause,
        "operator_action_recorded": action_recorded,
        "resume_replay_idempotent": (
            action_recorded
            and resume["pause_token"] == paused.get("attempt_id")
            and resume["pause_reason"] == expected_reason
            and resume["first_disposition"] == "applied"
            and resume["replay_disposition"] == "idempotent"
        ),
        "no_bypass": (
            typed_pause
            and receipts[0] == receipts[1] == []
            and counts[0] == counts[1] == 0
            and converged
        ),
        "external_effect_at_most_once": counts == [0, 0, 1],
        "duplicate_jobs": max(
            int(_path(snapshots, stage, "database.job_identity_count")) - 1
            for stage in STAGES
        ),
        "duplicate_attempts": duplicate_excess(
            attempts[2], ("job_id", "attempt_number")
        ),
        "duplicate_receipts": duplicate_excess(
            receipts[2], ("job_id", "natural_key")
        ),
        "attempt_count": len(attempts[2]),
        "receipt_count": len(receipts[2]),
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
            elif field == "vector_dimensions" and nested == 0:
                violations.append(f"{path}.{field} is not positive")
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
        record(stage, "target.fixture", _FIXTURE_SCHEMA)
        optional_endpoint_names = _OPTIONAL_ENDPOINTS_BY_KIND.get(kind, ())
        endpoint = record(
            stage,
            "target.endpoint_identity",
            {
                **_ENDPOINT_IDENTITY_SCHEMA,
                **{name: dict for name in optional_endpoint_names},
            },
        )
        if isinstance(endpoint, Mapping):
            sentinel = endpoint.get("sentinel")
            http_endpoints = [
                endpoint.get(name)
                for name in ("api", "mcp", *optional_endpoint_names)
            ]
            database_endpoint = endpoint.get("database")
            if (
                not isinstance(sentinel, Mapping)
                or set(sentinel)
                != {
                    "table",
                    "sentinel_id",
                    "run_id",
                    "fault_id",
                    "source_sha",
                }
                or any(
                    not isinstance(item, Mapping)
                    or set(item) != _ENDPOINT_HTTP_KEYS
                    for item in http_endpoints
                )
                or not isinstance(database_endpoint, Mapping)
                or set(database_endpoint) != _ENDPOINT_DATABASE_KEYS
            ):
                violations.append(
                    f"{stage}.state.target.endpoint identity nested "
                    "field set mismatch"
                )

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
    if not violations:
        violations.extend(_relation_violations(kind, snapshots))
    return violations


def _allowed_state_fields(kind: str, stage: str) -> dict[str, set[str]]:
    allowed = {section: set() for section in _STATE_KEYS}
    allowed["target"].update(("fixture", "endpoint_identity"))
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
    if fixture["resource_type"] != "chapter":
        violations.append(
            "before.state.target.fixture.resource_type mismatch"
        )

    for stage, dotted in _chapter_record_paths(kind):
        value = _path(snapshots, stage, dotted)
        rows = value if isinstance(value, Sequence) else (value,)
        for index, row in enumerate(rows):
            if row["chapter_id"] != fixture["resource_id"]:
                suffix = f"[{index}]" if isinstance(value, Sequence) else ""
                violations.append(
                    f"{stage}.state.{dotted}{suffix} fixture resource mismatch"
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
                        row["raw_point_id"],
                        row["payload_sha256"],
                        row["vector_sha256"],
                        row["vector_dimensions"],
                    )
                    for row in projections
                ]
                actual_coverage = [
                    (
                        row["collection"],
                        row["projection_type"],
                        row["canon_id"],
                        row["point_id"],
                        row["raw_point_id"],
                        row["payload_sha256"],
                        row["vector_sha256"],
                        row["vector_dimensions"],
                    )
                    for row in identities
                ]
                evidence_rows = [
                    (row, "identity_id") for row in projections
                ] + [(row, "point_id") for row in identities]
                for row, identity_field in evidence_rows:
                    expected_identity = (
                        f"{row['raw_point_id']}"
                        f"#payload-sha256={row['payload_sha256']}"
                        f"#vector-sha256={row['vector_sha256']}"
                    )
                    if row[identity_field] != expected_identity:
                        violations.append(
                            f"after.state.{identity_path} Qdrant evidence "
                            "identity binding mismatch"
                        )
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
    if not _endpoint_identity(kind, snapshots):
        violations.append(f"{kind}.endpoint identity is not stable or bound")
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
