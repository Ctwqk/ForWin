#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
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
        "stale_token_rejected": True,
        "shared_path_readable": True,
        "orphan_residue_count": 0,
        "duplicate_jobs": 0,
        "duplicate_attempts": 0,
        "duplicate_receipts": 0,
    },
    "publisher_browser_unavailable": {
        "canon_identity_unchanged": True,
        "same_job_identity": True,
        "pending_job_preserved": True,
        "heartbeat_recovered": True,
        "attempt_count": 0,
        "receipt_count": 0,
    },
    "publisher_captcha": {
        "same_job_identity": True,
        "paused_safely": True,
        "operator_action_recorded": True,
        "resume_replay_idempotent": True,
        "bypass_attempted": False,
        "receipt_count": 0,
    },
    "publisher_mfa": {
        "same_job_identity": True,
        "paused_safely": True,
        "operator_action_recorded": True,
        "resume_replay_idempotent": True,
        "bypass_attempted": False,
        "receipt_count": 0,
    },
    "publisher_account_risk": {
        "same_job_identity": True,
        "paused_safely": True,
        "operator_action_recorded": True,
        "resume_replay_idempotent": True,
        "bypass_attempted": False,
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
            "state.external.artifact",
        ),
        "after": (
            "state.database.canon_commits",
            "state.database.accepted_bundles",
            "state.database.maintenance",
            "state.database.authoritative_identities",
            "state.external.artifact",
            "state.barrier.residue_count",
        ),
    },
    "publisher_backend_unavailable": {
        "before": ("state.database.canon_commits",),
        "during": ("state.database.canon_commits", "state.database.job"),
        "after": (
            "state.database.canon_commits",
            "state.database.job",
            "state.database.jobs",
            "state.database.attempts",
            "state.database.receipts",
            "state.api.stale_token_rejected",
            "state.external.shared_path_readable",
            "state.external.orphan_residue_count",
        ),
    },
    "publisher_browser_unavailable": {
        "before": ("state.database.canon_commits", "state.database.job"),
        "during": (
            "state.database.canon_commits",
            "state.database.job",
            "state.external.browser_heartbeat",
        ),
        "after": (
            "state.database.canon_commits",
            "state.database.job",
            "state.database.attempts",
            "state.database.receipts",
            "state.external.browser_heartbeat",
        ),
    },
    "publisher_captcha": {
        "before": ("state.database.job",),
        "during": ("state.database.job",),
        "after": (
            "state.database.job",
            "state.database.receipts",
            "state.api.resume_actions",
            "state.api.resume_replay",
            "state.api.bypass_attempted",
        ),
    },
    "publisher_mfa": {
        "before": ("state.database.job",),
        "during": ("state.database.job",),
        "after": (
            "state.database.job",
            "state.database.receipts",
            "state.api.resume_actions",
            "state.api.resume_replay",
            "state.api.bypass_attempted",
        ),
    },
    "publisher_account_risk": {
        "before": ("state.database.job",),
        "during": ("state.database.job",),
        "after": (
            "state.database.job",
            "state.database.receipts",
            "state.api.resume_actions",
            "state.api.resume_replay",
            "state.api.bypass_attempted",
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
        elif "state" in snapshot:
            violations.append(f"{stage}.state is not an object")

        if snapshot.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
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
        if not isinstance(source_sha, str) or not source_sha:
            violations.append(f"{stage}.source_sha is empty")
        if not isinstance(fault_id, str) or not fault_id:
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
        "same_task_reclaimed": len({task["id"] for task in tasks}) == 1,
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
        "same_task_reclaimed": len({task["id"] for task in tasks}) == 1,
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
    projections = _path(snapshots, "after", "external.projections")
    points = _path(snapshots, "after", "external.point_identities")
    return {
        "canon_identity_unchanged": _all_stable_equal(canon),
        "outbox_retry_observed": len({row["id"] for row in outbox}) == 1
        and outbox[-1]["attempt"] > outbox[0]["attempt"],
        "projection_converged": bool(projections)
        and all(row["converged"] for row in projections),
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
    outbox_identities = [
        {"id": row["id"], "payload": row["payload"]} for row in outbox
    ]
    return {
        "canon_identity_unchanged": _all_stable_equal(canon),
        "durable_outbox_preserved": _all_stable_equal(outbox_identities),
        "projection_converged": bool(projections)
        and all(row["converged"] for row in projections),
        "duplicate_projection_identities": duplicate_excess(
            identities, ("projection", "identity")
        ),
    }


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
    artifact_during = _path(snapshots, "during", "external.artifact")
    artifact_after = _path(snapshots, "after", "external.artifact")
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
        "artifact_key_unchanged": artifact_during["key"] == artifact_after["key"],
        "barrier_residue_count": _path(
            snapshots, "after", "barrier.residue_count"
        ),
        "duplicate_authoritative_identities": duplicate_excess(
            identities, ("entity_type", "natural_key")
        ),
    }


def _publisher_backend(
    snapshots: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    canon = [_path(snapshots, stage, "database.canon_commits") for stage in STAGES]
    job_during = _path(snapshots, "during", "database.job")
    job_after = _path(snapshots, "after", "database.job")
    jobs = _path(snapshots, "after", "database.jobs")
    attempts = _path(snapshots, "after", "database.attempts")
    receipts = _path(snapshots, "after", "database.receipts")
    return {
        "canon_identity_unchanged": _all_stable_equal(canon),
        "same_job_reclaimed": job_during["id"] == job_after["id"]
        and job_during["owner_token"] != job_after["owner_token"],
        "stale_token_rejected": _path(
            snapshots, "after", "api.stale_token_rejected"
        ),
        "shared_path_readable": _path(
            snapshots, "after", "external.shared_path_readable"
        ),
        "orphan_residue_count": _path(
            snapshots, "after", "external.orphan_residue_count"
        ),
        "duplicate_jobs": duplicate_excess(jobs, ("logical_key",)),
        "duplicate_attempts": duplicate_excess(
            attempts, ("job_id", "attempt")
        ),
        "duplicate_receipts": duplicate_excess(
            receipts, ("job_id", "receipt_id")
        ),
    }


def _publisher_browser(
    snapshots: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    canon = [_path(snapshots, stage, "database.canon_commits") for stage in STAGES]
    jobs = [_path(snapshots, stage, "database.job") for stage in STAGES]
    heartbeat_during = _path(
        snapshots, "during", "external.browser_heartbeat"
    )
    heartbeat_after = _path(snapshots, "after", "external.browser_heartbeat")
    return {
        "canon_identity_unchanged": _all_stable_equal(canon),
        "same_job_identity": len({job["id"] for job in jobs}) == 1,
        "pending_job_preserved": all(job["status"] == "pending" for job in jobs),
        "heartbeat_recovered": not heartbeat_during["healthy"]
        and heartbeat_after["healthy"],
        "attempt_count": len(_path(snapshots, "after", "database.attempts")),
        "receipt_count": len(_path(snapshots, "after", "database.receipts")),
    }


def _publisher_risk(
    snapshots: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    jobs = [_path(snapshots, stage, "database.job") for stage in STAGES]
    paused_job = jobs[1]
    actions = _path(snapshots, "after", "api.resume_actions")
    replay = _path(snapshots, "after", "api.resume_replay")
    action = actions[0] if len(actions) == 1 else {}
    operator_action_recorded = (
        len(actions) == 1
        and action.get("authenticated") is True
        and action.get("job_id") == paused_job["id"]
        and action.get("fence") == paused_job.get("fence")
    )
    return {
        "same_job_identity": len({job["id"] for job in jobs}) == 1,
        "paused_safely": paused_job["status"] == "paused"
        and bool(paused_job.get("fence")),
        "operator_action_recorded": operator_action_recorded,
        "resume_replay_idempotent": operator_action_recorded
        and replay["action_id"] == action["action_id"]
        and replay["idempotency_key"] == action["idempotency_key"]
        and replay["created_new_action"] is False,
        "bypass_attempted": _path(
            snapshots, "after", "api.bypass_attempted"
        ),
        "receipt_count": len(_path(snapshots, "after", "database.receipts")),
    }


def _publisher_captcha(
    snapshots: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    return _publisher_risk(snapshots)


def _publisher_mfa(
    snapshots: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    return _publisher_risk(snapshots)


def _publisher_account_risk(
    snapshots: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    return _publisher_risk(snapshots)


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

    def mapping(
        stage: str, dotted: str, keys: set[str] | None = None
    ) -> Mapping[str, Any] | None:
        value = _path(snapshots, stage, dotted)
        path = f"{stage}.state.{dotted}"
        if not isinstance(value, Mapping):
            violations.append(f"{path} is not an object")
            return None
        if keys is not None:
            violations.extend(_unknown_key_violations(path, value, keys))
            missing = sorted(keys - set(value))
            if missing:
                violations.append(f"{path} keys missing: {missing}")
        return value

    def sequence(
        stage: str,
        dotted: str,
        row_keys: set[str] | None = None,
    ) -> Sequence[Any] | None:
        value = _path(snapshots, stage, dotted)
        path = f"{stage}.state.{dotted}"
        if not isinstance(value, Sequence) or isinstance(
            value, (str, bytes, bytearray)
        ):
            violations.append(f"{path} is not an array")
            return None
        if row_keys is not None:
            for index, row in enumerate(value):
                row_path = f"{path}[{index}]"
                if not isinstance(row, Mapping):
                    violations.append(f"{row_path} is not an object")
                    continue
                violations.extend(_unknown_key_violations(row_path, row, row_keys))
                missing = sorted(row_keys - set(row))
                if missing:
                    violations.append(f"{row_path} keys missing: {missing}")
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

    def sequence_scalars(
        stage: str,
        dotted: str,
        fields: Mapping[str, type[Any]],
    ) -> None:
        try:
            rows = _path(snapshots, stage, dotted)
        except EvidenceContractError:
            return
        if not isinstance(rows, Sequence) or isinstance(
            rows, (str, bytes, bytearray)
        ):
            return
        for index, row in enumerate(rows):
            if not isinstance(row, Mapping):
                continue
            for field, expected_type in fields.items():
                if field not in row:
                    continue
                value = row[field]
                if type(value) is not expected_type or (
                    expected_type is str and not value
                ):
                    description = (
                        "a boolean"
                        if expected_type is bool
                        else "an integer"
                        if expected_type is int
                        else "a nonempty string"
                    )
                    violations.append(
                        f"{stage}.state.{dotted}[{index}].{field} "
                        f"is not {description}"
                    )

    if kind.startswith("generation_worker_"):
        for stage in STAGES:
            mapping(stage, "database.task", {"id", "lease_epoch"})
            scalar(stage, "database.task.id", str, "a nonempty string")
            scalar(stage, "database.task.lease_epoch", int, "an integer")
        sequence("during", "database.canon_commits")
        sequence("after", "database.canon_commits")
        sequence(
            "after",
            "database.authoritative_identities",
            {"entity_type", "natural_key"},
        )
        if kind == "generation_worker_postcommit_crash":
            sequence("during", "database.accepted_bundles")
            sequence("after", "database.accepted_bundles")
    elif kind == "qdrant_unavailable":
        for stage in STAGES:
            sequence(stage, "database.canon_commits")
            mapping(stage, "database.outbox", {"id", "attempt"})
            scalar(stage, "database.outbox.id", str, "a nonempty string")
            scalar(stage, "database.outbox.attempt", int, "an integer")
        sequence(
            "after",
            "external.projections",
            {"name", "identity", "converged"},
        )
        sequence(
            "after",
            "external.point_identities",
            {"collection", "point_id"},
        )
        sequence_scalars(
            "after",
            "external.projections",
            {"name": str, "identity": str, "converged": bool},
        )
        sequence_scalars(
            "after",
            "external.point_identities",
            {"collection": str, "point_id": str},
        )
    elif kind == "projection_consumer_unavailable":
        for stage in STAGES:
            sequence(stage, "database.canon_commits")
            mapping(stage, "database.outbox", {"id", "payload"})
            scalar(stage, "database.outbox.id", str, "a nonempty string")
        sequence(
            "after",
            "external.projections",
            {"name", "identity", "converged"},
        )
        sequence(
            "after",
            "external.projection_identities",
            {"projection", "identity"},
        )
        sequence_scalars(
            "after",
            "external.projections",
            {"name": str, "identity": str, "converged": bool},
        )
        sequence_scalars(
            "after",
            "external.projection_identities",
            {"projection": str, "identity": str},
        )
    elif kind == "minio_pre_canon_unavailable":
        for stage in STAGES:
            mapping(stage, "database.candidate")
        sequence("during", "database.canon_commits")
        sequence("after", "database.canon_commits")
        sequence(
            "after",
            "database.authoritative_identities",
            {"entity_type", "natural_key"},
        )
    elif kind == "minio_post_canon_unavailable":
        for stage in STAGES:
            sequence(stage, "database.canon_commits")
            sequence(stage, "database.accepted_bundles")
        for stage in ("during", "after"):
            mapping(
                stage,
                "database.maintenance",
                {"natural_key", "attempt", "lease_epoch"},
            )
            mapping(stage, "external.artifact", {"key"})
            scalar(
                stage,
                "database.maintenance.natural_key",
                str,
                "a nonempty string",
            )
            scalar(
                stage, "database.maintenance.attempt", int, "an integer"
            )
            scalar(
                stage, "database.maintenance.lease_epoch", int, "an integer"
            )
            scalar(stage, "external.artifact.key", str, "a nonempty string")
        sequence(
            "after",
            "database.authoritative_identities",
            {"entity_type", "natural_key"},
        )
        scalar("after", "barrier.residue_count", int, "an integer")
    elif kind == "publisher_backend_unavailable":
        for stage in STAGES:
            sequence(stage, "database.canon_commits")
        for stage in ("during", "after"):
            mapping(stage, "database.job", {"id", "owner_token"})
            scalar(stage, "database.job.id", str, "a nonempty string")
            scalar(
                stage, "database.job.owner_token", str, "a nonempty string"
            )
        sequence("after", "database.jobs", {"logical_key"})
        sequence("after", "database.attempts", {"job_id", "attempt"})
        sequence("after", "database.receipts", {"job_id", "receipt_id"})
        sequence_scalars(
            "after", "database.jobs", {"logical_key": str}
        )
        sequence_scalars(
            "after", "database.attempts", {"job_id": str, "attempt": int}
        )
        sequence_scalars(
            "after",
            "database.receipts",
            {"job_id": str, "receipt_id": str},
        )
        scalar(
            "after", "api.stale_token_rejected", bool, "a boolean"
        )
        scalar(
            "after", "external.shared_path_readable", bool, "a boolean"
        )
        scalar(
            "after",
            "external.orphan_residue_count",
            int,
            "an integer",
        )
    elif kind == "publisher_browser_unavailable":
        for stage in STAGES:
            sequence(stage, "database.canon_commits")
            mapping(stage, "database.job", {"id", "status"})
            scalar(stage, "database.job.id", str, "a nonempty string")
            scalar(stage, "database.job.status", str, "a nonempty string")
        for stage in ("during", "after"):
            mapping(stage, "external.browser_heartbeat", {"healthy"})
            scalar(
                stage,
                "external.browser_heartbeat.healthy",
                bool,
                "a boolean",
            )
        sequence("after", "database.attempts")
        sequence("after", "database.receipts")
    else:
        mapping("before", "database.job", {"id", "status"})
        mapping("during", "database.job", {"id", "status", "fence"})
        mapping("after", "database.job", {"id", "status"})
        for stage in STAGES:
            scalar(stage, "database.job.id", str, "a nonempty string")
            scalar(stage, "database.job.status", str, "a nonempty string")
        scalar("during", "database.job.fence", str, "a nonempty string")
        sequence("after", "database.receipts")
        sequence(
            "after",
            "api.resume_actions",
            {
                "action_id",
                "job_id",
                "fence",
                "authenticated",
                "idempotency_key",
            },
        )
        mapping(
            "after",
            "api.resume_replay",
            {"action_id", "idempotency_key", "created_new_action"},
        )
        sequence_scalars(
            "after",
            "api.resume_actions",
            {
                "action_id": str,
                "job_id": str,
                "fence": str,
                "authenticated": bool,
                "idempotency_key": str,
            },
        )
        scalar(
            "after", "api.resume_replay.action_id", str, "a nonempty string"
        )
        scalar(
            "after",
            "api.resume_replay.idempotency_key",
            str,
            "a nonempty string",
        )
        scalar(
            "after",
            "api.resume_replay.created_new_action",
            bool,
            "a boolean",
        )
        scalar("after", "api.bypass_attempted", bool, "a boolean")
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
    elif kind == "qdrant_unavailable":
        paths.extend((stage, "database.canon_commits") for stage in STAGES)
    elif kind == "projection_consumer_unavailable":
        paths.extend(
            (stage, dotted)
            for stage in STAGES
            for dotted in (
                "database.canon_commits",
                "database.outbox.id",
                "database.outbox.payload",
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
            (stage, "external.artifact.key") for stage in ("during", "after")
        )
        paths.append(("after", "database.authoritative_identities"))
    elif kind in {
        "publisher_backend_unavailable",
        "publisher_browser_unavailable",
    }:
        paths.extend((stage, "database.canon_commits") for stage in STAGES)

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
