from __future__ import annotations

import copy
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import recovery_evidence as evidence


FAULT_KINDS = (
    "generation_worker_precommit_crash",
    "generation_worker_postcommit_crash",
    "qdrant_unavailable",
    "projection_consumer_unavailable",
    "minio_pre_canon_unavailable",
    "minio_post_canon_unavailable",
    "publisher_backend_unavailable",
    "publisher_browser_unavailable",
    "publisher_captcha",
    "publisher_mfa",
    "publisher_account_risk",
)
RISK_KINDS = (
    "publisher_captcha",
    "publisher_mfa",
    "publisher_account_risk",
)
PUBLISHER_KINDS = (
    "publisher_backend_unavailable",
    "publisher_browser_unavailable",
    *RISK_KINDS,
)
SOURCE_SHA = "0123456789abcdef0123456789abcdef01234567"


def token(kind: str, label: str) -> str:
    return f"fixture-{kind.replace('_', '-')}-{label}"


def digest(kind: str, label: str) -> str:
    return hashlib.sha256(f"{kind}:{label}".encode()).hexdigest()


def fixture_identity(kind: str) -> dict[str, str]:
    resource_type = "publisher_job" if kind in PUBLISHER_KINDS else "chapter"
    resource_id = (
        token(kind, "job-primary")
        if resource_type == "publisher_job"
        else token(kind, "chapter")
    )
    return {
        "fixture_id": token(kind, "fixture"),
        "fault_id": token(kind, "fault"),
        "resource_type": resource_type,
        "resource_id": resource_id,
    }


def empty_state(kind: str) -> dict[str, dict[str, Any]]:
    return {
        "target": {"fixture": fixture_identity(kind)},
        "mcp": {},
        "api": {},
        "database": {},
        "external": {},
        "barrier": {},
    }


def snapshots(kind: str) -> dict[str, dict[str, Any]]:
    return {
        stage: {
            "schema_version": 2,
            "source_sha": SOURCE_SHA,
            "fault_kind": kind,
            "fault_id": token(kind, "fault"),
            "stage": stage,
            "state": empty_state(kind),
        }
        for stage in evidence.STAGES
    }


def canon_record(kind: str, variant: str = "primary") -> dict[str, Any]:
    return {
        "canon_id": token(kind, f"canon-{variant}"),
        "natural_key": token(kind, f"canon-natural-{variant}"),
        "project_id": token(kind, "project"),
        "chapter_id": token(kind, "chapter"),
        "chapter_number": 1,
        "candidate_id": token(kind, "candidate"),
        "canon_version": 1 if variant == "primary" else 2,
        "content_sha256": digest(kind, f"canon-{variant}"),
    }


def accepted_bundle_record(kind: str, variant: str = "primary") -> dict[str, str]:
    return {
        "bundle_id": token(kind, f"bundle-{variant}"),
        "candidate_id": token(kind, "candidate"),
        "project_id": token(kind, "project"),
        "chapter_id": token(kind, "chapter"),
        "content_sha256": digest(kind, f"bundle-{variant}"),
    }


def candidate_record(kind: str, variant: str = "primary") -> dict[str, str]:
    return {
        "candidate_id": token(kind, f"candidate-{variant}"),
        "project_id": token(kind, "project"),
        "chapter_id": token(kind, "chapter"),
        "content_sha256": digest(kind, f"candidate-{variant}"),
    }


def authoritative_record(kind: str, variant: str = "primary") -> dict[str, str]:
    return {
        "entity_type": "canon",
        "record_id": token(kind, f"canon-{variant}"),
        "project_id": token(kind, "project"),
        "chapter_id": token(kind, "chapter"),
        "natural_key": token(kind, f"canon-natural-{variant}"),
    }


def task_record(kind: str, lease_epoch: int, variant: str = "primary") -> dict[str, Any]:
    return {"task_id": token(kind, f"task-{variant}"), "lease_epoch": lease_epoch}


def outbox_record(
    kind: str,
    attempt: int = 0,
    *,
    status: str = "pending",
    error_message: str = "",
) -> dict[str, Any]:
    payload = {
        "schema_version": 1,
        "canon_commit_id": token(kind, "canon-primary"),
        "canon_idempotency_key": token(kind, "canon-natural-primary"),
        "project_id": token(kind, "project"),
        "chapter_number": 1,
        "candidate_id": token(kind, "candidate"),
        "trigger": "canon_commit",
    }
    return {
        "event_id": (
            f"{payload['canon_idempotency_key']}:canon.projection.requested"
        ),
        "aggregate_type": "project",
        "aggregate_id": token(kind, "project"),
        "event_type": "canon.projection.requested",
        "payload": payload,
        "payload_sha256": evidence.stable_hash(payload),
        "status": status,
        "error_message": error_message,
        "attempt": attempt,
    }


def projection_observation(
    kind: str,
    status: str = "converged",
    *,
    collection: str = "fixture-vectors",
) -> dict[str, str]:
    identity_label = (
        "point-primary"
        if kind == "qdrant_unavailable"
        else "projection-primary"
    )
    record = {
        "projection_type": "vector",
        "identity_id": token(kind, identity_label),
        "canon_id": token(kind, "canon-primary"),
        "status": status,
    }
    if kind == "qdrant_unavailable":
        record["collection"] = collection
    return record


def point_record(
    kind: str,
    variant: str = "primary",
    *,
    collection: str = "fixture-vectors",
) -> dict[str, str]:
    return {
        "collection": collection,
        "projection_type": "vector",
        "point_id": token(kind, f"point-{variant}"),
        "canon_id": token(kind, "canon-primary"),
    }


def projection_identity_record(
    kind: str, variant: str = "primary"
) -> dict[str, str]:
    return {
        "projection_type": "vector",
        "projection_id": token(kind, f"projection-{variant}"),
        "canon_id": token(kind, "canon-primary"),
    }


def maintenance_record(kind: str, attempt: int, lease_epoch: int) -> dict[str, Any]:
    return {
        "natural_key": token(kind, "world-maintenance"),
        "project_id": token(kind, "project"),
        "canon_id": token(kind, "canon-primary"),
        "attempt": attempt,
        "lease_epoch": lease_epoch,
    }


def artifact_record(kind: str, variant: str = "primary") -> dict[str, Any]:
    return {
        "key": f"world/{token(kind, f'artifact-{variant}')}.json",
        "etag": digest(kind, f"etag-{variant}")[:32],
        "size": 4096,
        "content_type": "application/json",
        "content_sha256": digest(kind, f"artifact-{variant}"),
    }


def phase3_replay_observation(
    kind: str,
    *,
    status: str,
    attempts: int,
    lease_epoch: int,
    release_operation: bool = False,
) -> dict[str, Any]:
    payload = {
        "schema_version": 1,
        "canon_commit_id": token(kind, "canon-primary"),
        "canon_idempotency_key": token(kind, "canon-natural-primary"),
        "project_id": token(kind, "project"),
        "chapter_number": 1,
        "candidate_id": token(kind, "candidate"),
    }
    observation = {
        "row_id": token(kind, "phase3-row"),
        "event_id": (
            f"{payload['canon_idempotency_key']}:canon.phase3.requested"
        ),
        "aggregate_type": "project",
        "aggregate_id": payload["project_id"],
        "event_type": "canon.phase3.requested",
        "payload": payload,
        "payload_sha256": evidence.stable_hash(payload),
        "canon_idempotency_key": payload["canon_idempotency_key"],
        "status": status,
        "attempts": attempts,
        "lease_epoch": lease_epoch,
    }
    if release_operation:
        observation.update(
            {
                "conditional_rowcount": 1,
                "predicate_sha256": digest(kind, "phase3-predicate"),
                "before_row_sha256": digest(kind, "phase3-before-row"),
                "after_row_sha256": digest(kind, "phase3-after-row"),
            }
        )
    return observation


def backend_job_record(
    kind: str, owner: str, variant: str = "primary"
) -> dict[str, str]:
    return {
        "job_id": token(kind, f"job-{variant}"),
        "logical_key": token(kind, f"cover-logical-{variant}"),
        "status": "running",
        "owner_token": token(kind, owner),
        "artifact_key": f"covers/{token(kind, f'cover-{variant}')}.png",
    }


def browser_job_record(
    kind: str, status: str = "pending", variant: str = "primary"
) -> dict[str, str]:
    return {
        "job_id": token(kind, f"job-{variant}"),
        "logical_key": token(kind, f"browser-logical-{variant}"),
        "status": status,
    }


def risk_job_record(
    kind: str, status: str, fence: str = "pre-mutation", variant: str = "primary"
) -> dict[str, str]:
    return {
        "job_id": token(kind, f"job-{variant}"),
        "logical_key": token(kind, f"risk-logical-{variant}"),
        "status": status,
        "fence": fence,
    }


def job_identity_record(kind: str, variant: str = "primary") -> dict[str, str]:
    return {
        "job_id": token(kind, f"job-{variant}"),
        "logical_key": token(kind, f"cover-logical-{variant}"),
    }


def attempt_record(kind: str, variant: str = "primary") -> dict[str, Any]:
    return {
        "attempt_id": token(kind, f"attempt-{variant}"),
        "job_id": token(kind, "job-primary"),
        "attempt_number": 1 if variant == "primary" else 2,
        "owner_token": token(kind, "owner-new"),
        "status": "completed",
    }


def receipt_record(kind: str, variant: str = "primary") -> dict[str, str]:
    return {
        "receipt_id": token(kind, f"receipt-{variant}"),
        "job_id": token(kind, "job-primary"),
        "attempt_id": token(kind, f"attempt-{variant}"),
        "remote_mutation_id": token(kind, f"remote-mutation-{variant}"),
    }


def stale_token_observation(kind: str) -> dict[str, str]:
    return {
        "observation_id": token(kind, "stale-token-observation"),
        "job_id": token(kind, "job-primary"),
        "stale_owner_token": token(kind, "owner-old"),
        "current_owner_token": token(kind, "owner-new"),
        "outcome": "rejected",
        "error_code": "stale_owner_token",
    }


def shared_path_observation(kind: str) -> dict[str, str]:
    return {
        "observation_id": token(kind, "shared-path-observation"),
        "job_id": token(kind, "job-primary"),
        "artifact_key": f"covers/{token(kind, 'cover-primary')}.png",
        "reader_owner_token": token(kind, "owner-new"),
        "outcome": "readable",
        "content_sha256": digest(kind, "shared-cover"),
    }


def heartbeat_observation(kind: str, stage: str, status: str) -> dict[str, str]:
    return {
        "observation_id": token(kind, f"heartbeat-{stage}"),
        "browser_id": token(kind, "browser"),
        "probe": "extension_heartbeat",
        "status": status,
    }


def resume_action(kind: str) -> dict[str, str]:
    return {
        "action_id": token(kind, "resume-action"),
        "job_id": token(kind, "job-primary"),
        "fence": "pre-mutation",
        "idempotency_key": token(kind, "resume-idempotency"),
        "actor_id": token(kind, "operator"),
        "auth_method": "operator_token",
        "authorization_scope": "publisher:risk:resume",
        "result": "accepted",
    }


def resume_replay(kind: str) -> dict[str, str]:
    return {
        "observation_id": token(kind, "resume-replay"),
        "action_id": token(kind, "resume-action"),
        "replayed_action_id": token(kind, "resume-action"),
        "idempotency_key": token(kind, "resume-idempotency"),
        "result": "idempotent_replay",
    }


def mutation_observation(kind: str) -> dict[str, Any]:
    return {
        "observation_id": token(kind, "mutation-guard"),
        "job_id": token(kind, "job-primary"),
        "fault_kind": kind,
        "fence": "pre-mutation",
        "bypass_attempt_count": 0,
        "external_mutation_count": 0,
    }


def valid_snapshots(kind: str) -> dict[str, dict[str, Any]]:
    values = snapshots(kind)
    before = values["before"]["state"]
    during = values["during"]["state"]
    after = values["after"]["state"]
    canon = [canon_record(kind)]

    if kind == "generation_worker_precommit_crash":
        before["database"]["task"] = task_record(kind, 4)
        during["database"].update(
            {"task": task_record(kind, 4), "canon_commits": []}
        )
        after["database"].update(
            {
                "task": task_record(kind, 5),
                "canon_commits": canon,
                "authoritative_identities": [authoritative_record(kind)],
            }
        )
    elif kind == "generation_worker_postcommit_crash":
        before["database"]["task"] = task_record(kind, 4)
        during["database"].update(
            {
                "task": task_record(kind, 4),
                "canon_commits": copy.deepcopy(canon),
                "accepted_bundles": [accepted_bundle_record(kind)],
            }
        )
        after["database"].update(
            {
                "task": task_record(kind, 5),
                "canon_commits": canon,
                "accepted_bundles": [accepted_bundle_record(kind)],
                "authoritative_identities": [authoritative_record(kind)],
            }
        )
    elif kind == "qdrant_unavailable":
        for snapshot in values.values():
            snapshot["state"]["database"]["canon_commits"] = copy.deepcopy(canon)
        before["database"]["outbox"] = outbox_record(kind, 0)
        during["database"]["outbox"] = outbox_record(
            kind,
            1,
            status="pending",
            error_message="Qdrant connection refused",
        )
        after["database"]["outbox"] = outbox_record(
            kind,
            2,
            status="processed",
        )
        baseline_projections = [projection_observation(kind)]
        baseline_points = [point_record(kind)]
        after["external"].update(
            {
                "replay_baseline_projections": copy.deepcopy(
                    baseline_projections
                ),
                "replay_baseline_point_identities": copy.deepcopy(
                    baseline_points
                ),
                "projections": baseline_projections,
                "point_identities": baseline_points,
            }
        )
    elif kind == "projection_consumer_unavailable":
        before["database"].update(
            {
                "canon_commits": copy.deepcopy(canon),
                "outbox": outbox_record(kind, 0),
            }
        )
        during["database"].update(
            {
                "canon_commits": copy.deepcopy(canon),
                "outbox": outbox_record(kind, 0),
            }
        )
        after["database"].update(
            {
                "canon_commits": copy.deepcopy(canon),
                "outbox": outbox_record(
                    kind,
                    1,
                    status="processed",
                ),
            }
        )
        baseline_projections = [projection_observation(kind)]
        baseline_identities = [projection_identity_record(kind)]
        after["external"].update(
            {
                "replay_baseline_projections": copy.deepcopy(
                    baseline_projections
                ),
                "replay_baseline_projection_identities": copy.deepcopy(
                    baseline_identities
                ),
                "projections": baseline_projections,
                "projection_identities": baseline_identities,
            }
        )
    elif kind == "minio_pre_canon_unavailable":
        for snapshot in values.values():
            snapshot["state"]["database"]["candidate"] = candidate_record(kind)
        during["database"]["canon_commits"] = []
        after["database"].update(
            {
                "canon_commits": canon,
                "authoritative_identities": [authoritative_record(kind)],
            }
        )
    elif kind == "minio_post_canon_unavailable":
        for snapshot in values.values():
            snapshot["state"]["database"].update(
                {
                    "canon_commits": copy.deepcopy(canon),
                    "accepted_bundles": [accepted_bundle_record(kind)],
                }
            )
        during["database"]["maintenance"] = maintenance_record(kind, 1, 8)
        after["database"].update(
            {
                "maintenance": maintenance_record(kind, 2, 9),
                "authoritative_identities": [authoritative_record(kind)],
                "phase3_replay_baseline": phase3_replay_observation(
                    kind,
                    status="processed",
                    attempts=3,
                    lease_epoch=4,
                ),
                "phase3_replay_release": phase3_replay_observation(
                    kind,
                    status="pending",
                    attempts=3,
                    lease_epoch=4,
                    release_operation=True,
                ),
                "phase3_replay_final": phase3_replay_observation(
                    kind,
                    status="processed",
                    attempts=4,
                    lease_epoch=5,
                ),
            }
        )
        after["external"]["replay_baseline_artifact"] = artifact_record(kind)
        after["external"]["artifact"] = artifact_record(kind)
        after["barrier"]["residue_count"] = 0
    elif kind == "publisher_backend_unavailable":
        for snapshot in values.values():
            snapshot["state"]["database"]["canon_commits"] = copy.deepcopy(canon)
        during["database"]["job"] = backend_job_record(kind, "owner-old")
        after["database"].update(
            {
                "job": backend_job_record(kind, "owner-new"),
                "jobs": [job_identity_record(kind)],
                "attempts": [attempt_record(kind)],
                "receipts": [receipt_record(kind)],
            }
        )
        after["api"]["stale_token_observation"] = stale_token_observation(kind)
        after["external"].update(
            {
                "shared_path_observation": shared_path_observation(kind),
                "orphan_residue_count": 0,
            }
        )
    elif kind == "publisher_browser_unavailable":
        for snapshot in values.values():
            snapshot["state"]["database"].update(
                {
                    "canon_commits": copy.deepcopy(canon),
                    "job": browser_job_record(kind),
                }
            )
        during["external"]["browser_heartbeat"] = heartbeat_observation(
            kind, "during", "stale"
        )
        after["external"]["browser_heartbeat"] = heartbeat_observation(
            kind, "after", "healthy"
        )
        after["database"].update({"attempts": [], "receipts": []})
    else:
        before["database"]["job"] = risk_job_record(kind, "claimed")
        during["database"]["job"] = risk_job_record(kind, "paused")
        after["database"].update(
            {"job": risk_job_record(kind, "pending"), "receipts": []}
        )
        after["api"].update(
            {
                "resume_actions": [resume_action(kind)],
                "resume_replay": resume_replay(kind),
                "mutation_observation": mutation_observation(kind),
            }
        )
    return values


def path_value(root: Any, dotted: str) -> Any:
    current = root
    for part in dotted.split("."):
        current = current[int(part)] if part.isdigit() else current[part]
    return current


def set_path(root: Any, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    parent = root
    for part in parts[:-1]:
        parent = parent[int(part)] if part.isdigit() else parent[part]
    final = parts[-1]
    if final.isdigit():
        parent[int(final)] = value
    else:
        parent[final] = value


@dataclass(frozen=True)
class RecordCase:
    name: str
    kind: str
    stage: str
    container_path: str
    record: dict[str, Any]
    field: str
    wrong_value: Any
    is_list: bool = False


RECORD_CASES = (
    RecordCase(
        "fixture",
        "generation_worker_precommit_crash",
        "before",
        "target.fixture",
        fixture_identity("generation_worker_precommit_crash"),
        "fixture_id",
        7,
    ),
    RecordCase(
        "task",
        "generation_worker_precommit_crash",
        "before",
        "database.task",
        task_record("generation_worker_precommit_crash", 4),
        "task_id",
        7,
    ),
    RecordCase(
        "canon",
        "generation_worker_postcommit_crash",
        "during",
        "database.canon_commits",
        canon_record("generation_worker_postcommit_crash"),
        "canon_id",
        7,
        True,
    ),
    RecordCase(
        "accepted bundle",
        "generation_worker_postcommit_crash",
        "during",
        "database.accepted_bundles",
        accepted_bundle_record("generation_worker_postcommit_crash"),
        "bundle_id",
        7,
        True,
    ),
    RecordCase(
        "candidate",
        "minio_pre_canon_unavailable",
        "before",
        "database.candidate",
        candidate_record("minio_pre_canon_unavailable"),
        "candidate_id",
        7,
    ),
    RecordCase(
        "authoritative identity",
        "generation_worker_precommit_crash",
        "after",
        "database.authoritative_identities",
        authoritative_record("generation_worker_precommit_crash"),
        "natural_key",
        7,
        True,
    ),
    RecordCase(
        "outbox",
        "projection_consumer_unavailable",
        "before",
        "database.outbox",
        outbox_record("projection_consumer_unavailable"),
        "event_id",
        7,
    ),
    RecordCase(
        "projection observation",
        "qdrant_unavailable",
        "after",
        "external.projections",
        projection_observation("qdrant_unavailable"),
        "projection_type",
        7,
        True,
    ),
    RecordCase(
        "point identity",
        "qdrant_unavailable",
        "after",
        "external.point_identities",
        point_record("qdrant_unavailable"),
        "point_id",
        7,
        True,
    ),
    RecordCase(
        "projection identity",
        "projection_consumer_unavailable",
        "after",
        "external.projection_identities",
        projection_identity_record("projection_consumer_unavailable"),
        "projection_id",
        7,
        True,
    ),
    RecordCase(
        "maintenance",
        "minio_post_canon_unavailable",
        "during",
        "database.maintenance",
        maintenance_record("minio_post_canon_unavailable", 1, 8),
        "natural_key",
        7,
    ),
    RecordCase(
        "replay baseline artifact",
        "minio_post_canon_unavailable",
        "after",
        "external.replay_baseline_artifact",
        artifact_record("minio_post_canon_unavailable"),
        "key",
        7,
    ),
    RecordCase(
        "phase3 replay baseline",
        "minio_post_canon_unavailable",
        "after",
        "database.phase3_replay_baseline",
        phase3_replay_observation(
            "minio_post_canon_unavailable",
            status="processed",
            attempts=3,
            lease_epoch=4,
        ),
        "row_id",
        7,
    ),
    RecordCase(
        "phase3 replay release",
        "minio_post_canon_unavailable",
        "after",
        "database.phase3_replay_release",
        phase3_replay_observation(
            "minio_post_canon_unavailable",
            status="pending",
            attempts=3,
            lease_epoch=4,
            release_operation=True,
        ),
        "conditional_rowcount",
        "one",
    ),
    RecordCase(
        "phase3 replay final",
        "minio_post_canon_unavailable",
        "after",
        "database.phase3_replay_final",
        phase3_replay_observation(
            "minio_post_canon_unavailable",
            status="processed",
            attempts=4,
            lease_epoch=5,
        ),
        "status",
        7,
    ),
    RecordCase(
        "backend job",
        "publisher_backend_unavailable",
        "during",
        "database.job",
        backend_job_record("publisher_backend_unavailable", "owner-old"),
        "job_id",
        7,
    ),
    RecordCase(
        "job identity",
        "publisher_backend_unavailable",
        "after",
        "database.jobs",
        job_identity_record("publisher_backend_unavailable"),
        "logical_key",
        7,
        True,
    ),
    RecordCase(
        "attempt",
        "publisher_backend_unavailable",
        "after",
        "database.attempts",
        attempt_record("publisher_backend_unavailable"),
        "attempt_id",
        7,
        True,
    ),
    RecordCase(
        "receipt",
        "publisher_backend_unavailable",
        "after",
        "database.receipts",
        receipt_record("publisher_backend_unavailable"),
        "receipt_id",
        7,
        True,
    ),
    RecordCase(
        "stale token observation",
        "publisher_backend_unavailable",
        "after",
        "api.stale_token_observation",
        stale_token_observation("publisher_backend_unavailable"),
        "observation_id",
        7,
    ),
    RecordCase(
        "shared path observation",
        "publisher_backend_unavailable",
        "after",
        "external.shared_path_observation",
        shared_path_observation("publisher_backend_unavailable"),
        "observation_id",
        7,
    ),
    RecordCase(
        "browser job",
        "publisher_browser_unavailable",
        "before",
        "database.job",
        browser_job_record("publisher_browser_unavailable"),
        "job_id",
        7,
    ),
    RecordCase(
        "heartbeat observation",
        "publisher_browser_unavailable",
        "during",
        "external.browser_heartbeat",
        heartbeat_observation("publisher_browser_unavailable", "during", "stale"),
        "observation_id",
        7,
    ),
    RecordCase(
        "risk job",
        "publisher_captcha",
        "during",
        "database.job",
        risk_job_record("publisher_captcha", "paused"),
        "job_id",
        7,
    ),
    RecordCase(
        "resume action",
        "publisher_captcha",
        "after",
        "api.resume_actions",
        resume_action("publisher_captcha"),
        "action_id",
        7,
        True,
    ),
    RecordCase(
        "resume replay",
        "publisher_captcha",
        "after",
        "api.resume_replay",
        resume_replay("publisher_captcha"),
        "observation_id",
        7,
    ),
    RecordCase(
        "mutation observation",
        "publisher_captcha",
        "after",
        "api.mutation_observation",
        mutation_observation("publisher_captcha"),
        "observation_id",
        7,
    ),
)


def install_record(values: dict[str, dict[str, Any]], case: RecordCase) -> dict[str, Any]:
    state = values[case.stage]["state"]
    if case.is_list:
        set_path(state, case.container_path, [copy.deepcopy(case.record)])
        return path_value(state, f"{case.container_path}.0")
    set_path(state, case.container_path, copy.deepcopy(case.record))
    return path_value(state, case.container_path)


@pytest.mark.parametrize("kind", FAULT_KINDS)
def test_empty_state_reports_required_paths_for_every_fault(kind: str) -> None:
    values = snapshots(kind)
    for snapshot in values.values():
        snapshot["state"] = {}

    violations = evidence.snapshot_violations(kind, values)

    assert any(".state.target is missing" in item for item in violations)
    assert any(".state.database is missing" in item for item in violations)


@pytest.mark.parametrize("kind", FAULT_KINDS)
def test_valid_snapshot_contract_derives_only_passing_assertions(kind: str) -> None:
    values = valid_snapshots(kind)

    assert evidence.snapshot_violations(kind, values) == []
    assertions = evidence.derive_assertions(kind, values)
    assert evidence.assertion_violations(kind, assertions) == []


def test_qdrant_retry_requires_durable_failure_while_service_is_stopped() -> None:
    kind = "qdrant_unavailable"
    values = valid_snapshots(kind)
    values["during"]["state"]["database"]["outbox"] = outbox_record(
        kind,
        0,
    )
    values["after"]["state"]["database"]["outbox"] = outbox_record(
        kind,
        1,
        status="processed",
    )

    assert evidence.snapshot_violations(kind, values) == []
    assertions = evidence.derive_assertions(kind, values)
    assert assertions["outbox_retry_observed"] is False
    assert evidence.assertion_violations(kind, assertions)


@pytest.mark.parametrize(
    ("path", "value", "fragment"),
    (
        (
            "database.outbox.aggregate_type",
            "canon",
            "outbox aggregate type mismatch",
        ),
        (
            "database.outbox.aggregate_id",
            "wrong-project",
            "outbox aggregate identity mismatch",
        ),
        (
            "database.outbox.payload.canon_commit_id",
            "wrong-canon",
            "outbox Canon identity mismatch",
        ),
        (
            "database.outbox.payload.chapter_number",
            2,
            "outbox chapter identity mismatch",
        ),
        (
            "database.outbox.payload.candidate_id",
            "wrong-candidate",
            "outbox candidate identity mismatch",
        ),
    ),
)
def test_projection_outbox_preserves_production_identity_relations(
    path: str,
    value: Any,
    fragment: str,
) -> None:
    kind = "qdrant_unavailable"
    values = valid_snapshots(kind)
    set_path(values["during"]["state"], path, value)

    assert any(
        fragment in item
        for item in evidence.snapshot_violations(kind, values)
    )


def test_qdrant_collection_is_configured_identity_not_hardcoded() -> None:
    kind = "qdrant_unavailable"
    values = valid_snapshots(kind)

    assert evidence.snapshot_violations(kind, values) == []

    values["after"]["state"]["external"]["point_identities"][0][
        "collection"
    ] = "other-vectors"
    assert any(
        "external.point_identities coverage mismatch" in item
        for item in evidence.snapshot_violations(kind, values)
    )


def test_projection_replay_must_preserve_converged_identities() -> None:
    kind = "projection_consumer_unavailable"
    values = valid_snapshots(kind)
    changed = token(kind, "projection-after-refresh")
    values["after"]["state"]["external"]["projections"][0][
        "identity_id"
    ] = changed
    values["after"]["state"]["external"]["projection_identities"][0][
        "projection_id"
    ] = changed

    assert evidence.snapshot_violations(kind, values) == []
    assertions = evidence.derive_assertions(kind, values)
    assert assertions["replay_identity_unchanged"] is False
    assert evidence.assertion_violations(kind, assertions)


@pytest.mark.parametrize("case", RECORD_CASES, ids=lambda case: case.name)
@pytest.mark.parametrize("fault", ("missing", "unknown", "wrong_type", "fabricated"))
def test_every_normalized_record_has_a_strict_schema(
    case: RecordCase, fault: str
) -> None:
    values = valid_snapshots(case.kind)
    record = install_record(values, case)
    if fault == "missing":
        del record[case.field]
    elif fault == "unknown":
        record["operator_note"] = "same"
    elif fault == "wrong_type":
        record[case.field] = case.wrong_value
    else:
        if case.is_list:
            set_path(
                values[case.stage]["state"],
                case.container_path,
                [{"note": "same"}],
            )
        else:
            set_path(
                values[case.stage]["state"],
                case.container_path,
                {"note": "same"},
            )

    violations = evidence.snapshot_violations(case.kind, values)

    assert violations, f"{case.name} accepted {fault}"
    assert any(case.container_path in item for item in violations)


@pytest.mark.parametrize(
    ("kind", "path"),
    (
        (
            "generation_worker_precommit_crash",
            "database.authoritative_identities",
        ),
        (
            "generation_worker_postcommit_crash",
            "database.authoritative_identities",
        ),
        ("minio_pre_canon_unavailable", "database.authoritative_identities"),
        ("minio_post_canon_unavailable", "database.authoritative_identities"),
        ("qdrant_unavailable", "external.point_identities"),
        (
            "projection_consumer_unavailable",
            "external.projection_identities",
        ),
        ("publisher_backend_unavailable", "database.jobs"),
        ("publisher_backend_unavailable", "database.attempts"),
        ("publisher_backend_unavailable", "database.receipts"),
    ),
)
def test_required_identity_inventories_reject_empty_coverage(
    kind: str, path: str
) -> None:
    values = valid_snapshots(kind)
    set_path(values["after"]["state"], path, [])

    assert any(
        f"{path} coverage mismatch" in item
        for item in evidence.snapshot_violations(kind, values)
    )


@pytest.mark.parametrize(
    ("kind", "path"),
    (
        (
            "generation_worker_precommit_crash",
            "database.authoritative_identities",
        ),
        ("qdrant_unavailable", "external.point_identities"),
        (
            "projection_consumer_unavailable",
            "external.projection_identities",
        ),
    ),
)
def test_identity_inventory_rejects_extra_coverage(kind: str, path: str) -> None:
    values = valid_snapshots(kind)
    rows = path_value(values["after"]["state"], path)
    rows.append(copy.deepcopy(rows[0]))

    assert any(
        f"{path} coverage mismatch" in item
        for item in evidence.snapshot_violations(kind, values)
    )


@pytest.mark.parametrize(
    ("kind", "path", "field"),
    (
        (
            "generation_worker_precommit_crash",
            "database.authoritative_identities",
            "record_id",
        ),
        (
            "generation_worker_precommit_crash",
            "database.authoritative_identities",
            "natural_key",
        ),
        ("qdrant_unavailable", "external.point_identities", "canon_id"),
        ("qdrant_unavailable", "external.point_identities", "point_id"),
        (
            "projection_consumer_unavailable",
            "external.projection_identities",
            "canon_id",
        ),
        (
            "projection_consumer_unavailable",
            "external.projection_identities",
            "projection_id",
        ),
    ),
)
def test_identity_inventory_rejects_wrong_binding(
    kind: str, path: str, field: str
) -> None:
    values = valid_snapshots(kind)
    rows = path_value(values["after"]["state"], path)
    rows[0][field] = token(kind, "wrong-binding")

    assert any(
        f"{path} coverage mismatch" in item
        for item in evidence.snapshot_violations(kind, values)
    )


@pytest.mark.parametrize(
    ("kind", "stage", "path", "violation_path"),
    (
        (
            "generation_worker_postcommit_crash",
            "during",
            "database.canon_commits.0.content_sha256",
            "database.canon_commits[0].content_sha256",
        ),
        (
            "generation_worker_postcommit_crash",
            "during",
            "database.accepted_bundles.0.content_sha256",
            "database.accepted_bundles[0].content_sha256",
        ),
        (
            "minio_pre_canon_unavailable",
            "before",
            "database.candidate.content_sha256",
            "database.candidate.content_sha256",
        ),
        (
            "projection_consumer_unavailable",
            "before",
            "database.outbox.payload_sha256",
            "database.outbox.payload_sha256",
        ),
        (
            "minio_post_canon_unavailable",
            "after",
            "external.replay_baseline_artifact.content_sha256",
            "external.replay_baseline_artifact.content_sha256",
        ),
        (
            "publisher_backend_unavailable",
            "after",
            "external.shared_path_observation.content_sha256",
            "external.shared_path_observation.content_sha256",
        ),
    ),
)
def test_sha256_evidence_requires_canonical_digest(
    kind: str, stage: str, path: str, violation_path: str
) -> None:
    values = valid_snapshots(kind)
    set_path(values[stage]["state"], path, "not-a-sha256")

    assert any(
        f"{violation_path} is not a canonical SHA-256 digest" in item
        for item in evidence.snapshot_violations(kind, values)
    )


@pytest.mark.parametrize(
    ("kind", "stage", "path", "violation_path"),
    (
        (
            "generation_worker_precommit_crash",
            "before",
            "database.task.lease_epoch",
            "database.task.lease_epoch",
        ),
        (
            "generation_worker_postcommit_crash",
            "during",
            "database.canon_commits.0.canon_version",
            "database.canon_commits[0].canon_version",
        ),
        (
            "qdrant_unavailable",
            "before",
            "database.outbox.attempt",
            "database.outbox.attempt",
        ),
        (
            "minio_post_canon_unavailable",
            "during",
            "database.maintenance.attempt",
            "database.maintenance.attempt",
        ),
        (
            "minio_post_canon_unavailable",
            "during",
            "database.maintenance.lease_epoch",
            "database.maintenance.lease_epoch",
        ),
        (
            "minio_post_canon_unavailable",
            "after",
            "external.replay_baseline_artifact.size",
            "external.replay_baseline_artifact.size",
        ),
        (
            "publisher_backend_unavailable",
            "after",
            "database.attempts.0.attempt_number",
            "database.attempts[0].attempt_number",
        ),
        (
            "publisher_captcha",
            "after",
            "api.mutation_observation.bypass_attempt_count",
            "api.mutation_observation.bypass_attempt_count",
        ),
        (
            "publisher_captcha",
            "after",
            "api.mutation_observation.external_mutation_count",
            "api.mutation_observation.external_mutation_count",
        ),
        (
            "minio_post_canon_unavailable",
            "after",
            "barrier.residue_count",
            "barrier.residue_count",
        ),
        (
            "publisher_backend_unavailable",
            "after",
            "external.orphan_residue_count",
            "external.orphan_residue_count",
        ),
    ),
)
def test_integer_evidence_cannot_be_negative(
    kind: str, stage: str, path: str, violation_path: str
) -> None:
    values = valid_snapshots(kind)
    set_path(values[stage]["state"], path, -1)

    assert any(
        f"{violation_path} is negative" in item
        for item in evidence.snapshot_violations(kind, values)
    )


@pytest.mark.parametrize(
    "bad_sha",
    (
        "a" * 39,
        "A" * 40,
        "g" * 40,
        "not-a-commit",
    ),
)
def test_source_sha_requires_canonical_lowercase_commit_shape(bad_sha: str) -> None:
    kind = "generation_worker_precommit_crash"
    values = valid_snapshots(kind)
    for snapshot in values.values():
        snapshot["source_sha"] = bad_sha

    assert any(
        "source_sha is not a canonical 40-character lowercase hex SHA" in item
        for item in evidence.snapshot_violations(kind, values)
    )


def test_fixture_identity_must_be_stable_across_all_stages() -> None:
    kind = "minio_post_canon_unavailable"
    values = valid_snapshots(kind)
    values["after"]["state"]["target"]["fixture"]["fixture_id"] = token(
        kind, "other-fixture"
    )

    assert (
        "after.state.target.fixture mismatch"
        in evidence.snapshot_violations(kind, values)
    )


def test_fixture_identity_must_match_each_snapshot_fault_id() -> None:
    kind = "qdrant_unavailable"
    values = valid_snapshots(kind)
    for snapshot in values.values():
        snapshot["fault_id"] = token(kind, "replacement-fault")

    violations = evidence.snapshot_violations(kind, values)

    for stage in evidence.STAGES:
        assert (
            f"{stage}.state.target.fixture.fault_id mismatch"
            in violations
        )


def test_fault_local_fixtures_are_distinct_and_publisher_jobs_are_projectless() -> None:
    fixtures = [fixture_identity(kind) for kind in FAULT_KINDS]

    assert len({item["fixture_id"] for item in fixtures}) == len(FAULT_KINDS)
    for kind in PUBLISHER_KINDS:
        values = valid_snapshots(kind)
        assert "project_id" not in values["before"]["state"]["target"]["fixture"]
        for snapshot in values.values():
            job = snapshot["state"]["database"].get("job")
            if job is not None:
                assert "project_id" not in job


@pytest.mark.parametrize(
    "kind",
    (
        "publisher_browser_unavailable",
        "publisher_captcha",
        "publisher_mfa",
        "publisher_account_risk",
    ),
)
def test_no_mutation_faults_allow_empty_attempt_and_receipt_inventories(
    kind: str,
) -> None:
    values = valid_snapshots(kind)

    assert values["after"]["state"]["database"].get("attempts", []) == []
    assert values["after"]["state"]["database"]["receipts"] == []
    assert evidence.snapshot_violations(kind, values) == []


def test_unknown_snapshot_and_state_keys_are_rejected() -> None:
    kind = "generation_worker_precommit_crash"
    values = valid_snapshots(kind)
    values["before"]["operator_note"] = "not evidence"
    values["after"]["state"]["summary"] = {}

    violations = evidence.snapshot_violations(kind, values)

    assert "before has unknown keys: ['operator_note']" in violations
    assert "after.state has unknown keys: ['summary']" in violations


def test_stable_identity_rejects_mutable_timestamp_substitution() -> None:
    kind = "generation_worker_postcommit_crash"
    values = valid_snapshots(kind)
    values["after"]["state"]["database"]["accepted_bundles"][0][
        "updated_at"
    ] = "2026-07-26T00:00:00Z"

    assert any(
        "accepted_bundles[0]" in item and "updated_at" in item
        for item in evidence.snapshot_violations(kind, values)
    )


def test_minio_post_inventory_is_after_only_and_rejects_timestamps() -> None:
    kind = "minio_post_canon_unavailable"
    values = valid_snapshots(kind)
    assert values["during"]["state"]["external"] == {}
    values["during"]["state"]["external"]["artifact"] = artifact_record(kind)
    values["after"]["state"]["external"]["replay_baseline_artifact"][
        "last_modified"
    ] = "2026-07-26T00:00:00Z"

    violations = evidence.snapshot_violations(kind, values)

    assert (
        "during.state.external has unknown keys: ['artifact']" in violations
    )
    assert any(
        "replay_baseline_artifact" in item and "last_modified" in item
        for item in violations
    )


def test_minio_post_requires_immutable_replay_observations() -> None:
    kind = "minio_post_canon_unavailable"
    values = valid_snapshots(kind)

    assertions = evidence.derive_assertions(kind, values)

    assert assertions["phase3_replay_identity_unchanged"] is True
    assert assertions["phase3_release_rowcount"] == 1
    assert assertions["phase3_release_preserved_claim"] is True
    assert assertions["phase3_worker_claim_advanced"] is True
    assert assertions["phase3_replay_final_processed"] is True


@pytest.mark.parametrize(
    "path",
    (
        "phase3_replay_baseline",
        "phase3_replay_release",
        "phase3_replay_final",
    ),
)
def test_minio_post_rejects_missing_replay_observation(path: str) -> None:
    kind = "minio_post_canon_unavailable"
    values = valid_snapshots(kind)
    del values["after"]["state"]["database"][path]

    assert any(
        f"after.state.database.{path} is missing" in item
        for item in evidence.snapshot_violations(kind, values)
    )


def test_minio_post_rejects_copied_replay_boolean() -> None:
    kind = "minio_post_canon_unavailable"
    values = valid_snapshots(kind)
    values["after"]["state"]["database"]["replay_observed"] = True

    assert (
        "after.state.database has unknown keys: ['replay_observed']"
        in evidence.snapshot_violations(kind, values)
    )


@pytest.mark.parametrize(
    ("path", "value", "assertion"),
    (
        (
            "database.phase3_replay_final.row_id",
            "other-row",
            "phase3_replay_identity_unchanged",
        ),
        (
            "database.phase3_replay_release.conditional_rowcount",
            0,
            "phase3_release_rowcount",
        ),
        (
            "database.phase3_replay_release.attempts",
            4,
            "phase3_release_preserved_claim",
        ),
        (
            "database.phase3_replay_release.lease_epoch",
            5,
            "phase3_release_preserved_claim",
        ),
        (
            "database.phase3_replay_final.attempts",
            5,
            "phase3_worker_claim_advanced",
        ),
        (
            "database.phase3_replay_final.lease_epoch",
            6,
            "phase3_worker_claim_advanced",
        ),
        (
            "database.phase3_replay_final.status",
            "pending",
            "phase3_replay_final_processed",
        ),
    ),
)
def test_minio_post_replay_derived_negatives(
    path: str,
    value: Any,
    assertion: str,
) -> None:
    kind = "minio_post_canon_unavailable"
    values = valid_snapshots(kind)
    set_path(values["after"]["state"], path, value)

    derived = evidence.derive_assertions(kind, values)

    assert derived[assertion] != evidence.FAULT_CONTRACTS[kind][assertion]
    assert evidence.assertion_violations(kind, derived)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("key", "other/world.json"),
        ("etag", "different-etag"),
        ("size", 4097),
        ("content_type", "application/octet-stream"),
        ("content_sha256", "a" * 64),
    ),
)
def test_minio_post_compares_complete_artifact_identity(
    field: str,
    value: Any,
) -> None:
    kind = "minio_post_canon_unavailable"
    values = valid_snapshots(kind)
    values["after"]["state"]["external"]["artifact"][field] = value

    derived = evidence.derive_assertions(kind, values)

    assert derived["artifact_identity_unchanged"] is False
    assert evidence.assertion_violations(kind, derived)


def test_copied_publisher_booleans_are_not_snapshot_schema_fields() -> None:
    backend = valid_snapshots("publisher_backend_unavailable")
    risk = valid_snapshots("publisher_captcha")
    backend["after"]["state"]["api"]["stale_token_rejected"] = True
    backend["after"]["state"]["external"]["shared_path_readable"] = True
    risk["after"]["state"]["api"]["bypass_attempted"] = False

    assert any(
        "after.state.api has unknown keys: ['stale_token_rejected']" in item
        for item in evidence.snapshot_violations(
            "publisher_backend_unavailable", backend
        )
    )
    assert any(
        "after.state.external has unknown keys: ['shared_path_readable']" in item
        for item in evidence.snapshot_violations(
            "publisher_backend_unavailable", backend
        )
    )
    assert any(
        "after.state.api has unknown keys: ['bypass_attempted']" in item
        for item in evidence.snapshot_violations("publisher_captcha", risk)
    )


@pytest.mark.parametrize(
    ("path", "value", "assertion"),
    (
        (
            "api.stale_token_observation.job_id",
            token("publisher_backend_unavailable", "job-other"),
            "stale_token_rejected",
        ),
        (
            "api.stale_token_observation.stale_owner_token",
            token("publisher_backend_unavailable", "owner-other"),
            "stale_token_rejected",
        ),
        (
            "api.stale_token_observation.current_owner_token",
            token("publisher_backend_unavailable", "owner-other"),
            "stale_token_rejected",
        ),
        (
            "api.stale_token_observation.error_code",
            "unexpected_error",
            "stale_token_rejected",
        ),
        (
            "external.shared_path_observation.job_id",
            token("publisher_backend_unavailable", "job-other"),
            "shared_path_readable",
        ),
        (
            "external.shared_path_observation.artifact_key",
            "covers/other.png",
            "shared_path_readable",
        ),
        (
            "external.shared_path_observation.reader_owner_token",
            token("publisher_backend_unavailable", "owner-other"),
            "shared_path_readable",
        ),
    ),
)
def test_backend_observations_are_bound_to_job_token_and_path_identity(
    path: str, value: str, assertion: str
) -> None:
    kind = "publisher_backend_unavailable"
    values = valid_snapshots(kind)
    set_path(values["after"]["state"], path, value)

    assert evidence.snapshot_violations(kind, values) == []
    assertions = evidence.derive_assertions(kind, values)
    assert assertions[assertion] is False
    assert evidence.assertion_violations(kind, assertions)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("job_id", "wrong-job"),
        ("fault_kind", "publisher_mfa"),
        ("fence", "post-mutation"),
    ),
)
def test_risk_mutation_observation_must_match_fault_job_and_fence(
    field: str, value: str
) -> None:
    kind = "publisher_captcha"
    values = valid_snapshots(kind)
    values["after"]["state"]["api"]["mutation_observation"][field] = value

    assert any(
        "api.mutation_observation identity mismatch" in item
        for item in evidence.snapshot_violations(kind, values)
    )


@pytest.mark.parametrize(
    "field", ("bypass_attempt_count", "external_mutation_count")
)
def test_risk_observation_counts_cannot_be_negative(field: str) -> None:
    kind = "publisher_captcha"
    values = valid_snapshots(kind)
    values["after"]["state"]["api"]["mutation_observation"][field] = -1

    assert any(
        f"api.mutation_observation.{field} is negative" in item
        for item in evidence.snapshot_violations(kind, values)
    )


@pytest.mark.parametrize(
    ("path", "value", "assertion"),
    (
        ("api.resume_actions.0.job_id", "wrong-job", "operator_action_recorded"),
        ("api.resume_actions.0.fence", "post-mutation", "operator_action_recorded"),
        ("api.resume_actions.0.authorization_scope", "read", "operator_action_recorded"),
        ("api.resume_actions.0.result", "rejected", "operator_action_recorded"),
        ("api.resume_replay.action_id", "wrong-action", "resume_replay_idempotent"),
        (
            "api.resume_replay.replayed_action_id",
            "wrong-action",
            "resume_replay_idempotent",
        ),
        (
            "api.resume_replay.idempotency_key",
            "wrong-key",
            "resume_replay_idempotent",
        ),
    ),
)
def test_risk_action_and_replay_assertions_are_identity_derived(
    path: str, value: str, assertion: str
) -> None:
    kind = "publisher_captcha"
    values = valid_snapshots(kind)
    set_path(values["after"]["state"], path, value)

    assert evidence.snapshot_violations(kind, values) == []
    assertions = evidence.derive_assertions(kind, values)
    assert assertions[assertion] is False
    assert evidence.assertion_violations(kind, assertions)


@pytest.mark.parametrize(
    ("kind", "path", "value", "fragment"),
    (
        (
            "minio_pre_canon_unavailable",
            "database.canon_commits.0.project_id",
            "wrong-project",
            "candidate resource mismatch",
        ),
        (
            "publisher_backend_unavailable",
            "database.jobs.0.job_id",
            "wrong-job",
            "database.jobs[0] job identity mismatch",
        ),
        (
            "publisher_backend_unavailable",
            "database.attempts.0.job_id",
            "wrong-job",
            "database.attempts[0] job identity mismatch",
        ),
        (
            "publisher_backend_unavailable",
            "database.attempts.0.owner_token",
            "wrong-owner",
            "database.attempts coverage mismatch",
        ),
    ),
)
def test_related_inventory_records_cannot_drift_to_other_resources(
    kind: str, path: str, value: str, fragment: str
) -> None:
    values = valid_snapshots(kind)
    set_path(values["after"]["state"], path, value)

    assert any(
        fragment in item for item in evidence.snapshot_violations(kind, values)
    )


def test_receipt_identity_must_reference_the_reclaimed_job_and_attempt() -> None:
    kind = "publisher_backend_unavailable"
    values = valid_snapshots(kind)
    receipt = receipt_record(kind)
    receipt["attempt_id"] = "wrong-attempt"
    values["after"]["state"]["database"]["receipts"] = [receipt]

    assert any(
        "database.receipts[0] attempt identity mismatch" in item
        for item in evidence.snapshot_violations(kind, values)
    )


def test_backend_attempt_history_allows_failed_stale_attempt_without_receipt() -> None:
    kind = "publisher_backend_unavailable"
    values = valid_snapshots(kind)
    stale_attempt = attempt_record(kind)
    stale_attempt["owner_token"] = token(kind, "owner-old")
    stale_attempt["status"] = "failed"
    values["after"]["state"]["database"]["attempts"] = [
        stale_attempt,
        attempt_record(kind, "secondary"),
    ]
    values["after"]["state"]["database"]["receipts"] = [
        receipt_record(kind, "secondary")
    ]

    assert evidence.snapshot_violations(kind, values) == []
    assertions = evidence.derive_assertions(kind, values)
    assert evidence.assertion_violations(kind, assertions) == []


def set_mutation(stage: str, path: str, value: Any) -> Callable[[dict[str, Any]], None]:
    def mutate(values: dict[str, Any]) -> None:
        set_path(values[stage]["state"], path, copy.deepcopy(value))

    return mutate


def duplicate_mutation(
    stage: str, path: str, index: int = 0
) -> Callable[[dict[str, Any]], None]:
    def mutate(values: dict[str, Any]) -> None:
        rows = path_value(values[stage]["state"], path)
        rows.append(copy.deepcopy(rows[index]))

    return mutate


def multi_mutation(
    *mutations: Callable[[dict[str, Any]], None],
) -> Callable[[dict[str, Any]], None]:
    def mutate(values: dict[str, Any]) -> None:
        for operation in mutations:
            operation(values)

    return mutate


@dataclass(frozen=True)
class ContractCase:
    name: str
    kind: str
    assertion: str
    mutate: Callable[[dict[str, Any]], None]
    actual: Any


@dataclass(frozen=True)
class SchemaInvariantCase:
    name: str
    kind: str
    assertion: str
    mutate: Callable[[dict[str, Any]], None]
    violation_fragment: str


def contract_cases() -> list[ContractCase]:
    cases = [
        ContractCase(
            "precommit same task",
            "generation_worker_precommit_crash",
            "same_task_reclaimed",
            set_mutation(
                "after",
                "database.task.task_id",
                token("generation_worker_precommit_crash", "task-other"),
            ),
            False,
        ),
        ContractCase(
            "precommit lease epoch",
            "generation_worker_precommit_crash",
            "lease_epoch_increased",
            set_mutation("after", "database.task.lease_epoch", 4),
            False,
        ),
        ContractCase(
            "precommit zero canon during",
            "generation_worker_precommit_crash",
            "canon_commits_during_fault",
            set_mutation(
                "during",
                "database.canon_commits",
                [canon_record("generation_worker_precommit_crash")],
            ),
            1,
        ),
        ContractCase(
            "precommit one canon after",
            "generation_worker_precommit_crash",
            "canon_commits_after_recovery",
            multi_mutation(
                set_mutation(
                    "after",
                    "database.canon_commits",
                    [
                        canon_record("generation_worker_precommit_crash"),
                        canon_record(
                            "generation_worker_precommit_crash", "secondary"
                        ),
                    ],
                ),
                set_mutation(
                    "after",
                    "database.authoritative_identities",
                    [
                        authoritative_record(
                            "generation_worker_precommit_crash"
                        ),
                        authoritative_record(
                            "generation_worker_precommit_crash", "secondary"
                        ),
                    ],
                ),
            ),
            2,
        ),
        ContractCase(
            "postcommit same task",
            "generation_worker_postcommit_crash",
            "same_task_reclaimed",
            set_mutation(
                "after",
                "database.task.task_id",
                token("generation_worker_postcommit_crash", "task-other"),
            ),
            False,
        ),
        ContractCase(
            "postcommit lease epoch",
            "generation_worker_postcommit_crash",
            "lease_epoch_increased",
            set_mutation("after", "database.task.lease_epoch", 4),
            False,
        ),
        ContractCase(
            "postcommit canon identity",
            "generation_worker_postcommit_crash",
            "canon_identity_unchanged",
            set_mutation(
                "after",
                "database.canon_commits.0.content_sha256",
                digest("generation_worker_postcommit_crash", "changed-canon"),
            ),
            False,
        ),
        ContractCase(
            "postcommit accepted identity",
            "generation_worker_postcommit_crash",
            "accepted_identity_unchanged",
            set_mutation(
                "after",
                "database.accepted_bundles.0.content_sha256",
                digest("generation_worker_postcommit_crash", "changed-bundle"),
            ),
            False,
        ),
        ContractCase(
            "qdrant canon identity",
            "qdrant_unavailable",
            "canon_identity_unchanged",
            set_mutation(
                "after",
                "database.canon_commits.0.content_sha256",
                digest("qdrant_unavailable", "changed-canon"),
            ),
            False,
        ),
        ContractCase(
            "qdrant retry",
            "qdrant_unavailable",
            "outbox_retry_observed",
            set_mutation("after", "database.outbox.attempt", 0),
            False,
        ),
        ContractCase(
            "qdrant convergence",
            "qdrant_unavailable",
            "projection_converged",
            set_mutation("after", "external.projections.0.status", "pending"),
            False,
        ),
        ContractCase(
            "projection canon identity",
            "projection_consumer_unavailable",
            "canon_identity_unchanged",
            set_mutation(
                "after",
                "database.canon_commits.0.content_sha256",
                digest("projection_consumer_unavailable", "changed-canon"),
            ),
            False,
        ),
        ContractCase(
            "projection outbox processing",
            "projection_consumer_unavailable",
            "durable_outbox_preserved",
            set_mutation(
                "after",
                "database.outbox.status",
                "pending",
            ),
            False,
        ),
        ContractCase(
            "projection convergence",
            "projection_consumer_unavailable",
            "projection_converged",
            set_mutation("after", "external.projections.0.status", "pending"),
            False,
        ),
        ContractCase(
            "qdrant replay identity",
            "qdrant_unavailable",
            "replay_identity_unchanged",
            multi_mutation(
                set_mutation(
                    "after",
                    "external.projections.0.identity_id",
                    token("qdrant_unavailable", "point-after-refresh"),
                ),
                set_mutation(
                    "after",
                    "external.point_identities.0.point_id",
                    token("qdrant_unavailable", "point-after-refresh"),
                ),
            ),
            False,
        ),
        ContractCase(
            "projection replay identity",
            "projection_consumer_unavailable",
            "replay_identity_unchanged",
            multi_mutation(
                set_mutation(
                    "after",
                    "external.projections.0.identity_id",
                    token(
                        "projection_consumer_unavailable",
                        "projection-after-refresh",
                    ),
                ),
                set_mutation(
                    "after",
                    "external.projection_identities.0.projection_id",
                    token(
                        "projection_consumer_unavailable",
                        "projection-after-refresh",
                    ),
                ),
            ),
            False,
        ),
        ContractCase(
            "minio pre zero canon",
            "minio_pre_canon_unavailable",
            "canon_commits_during_fault",
            set_mutation(
                "during",
                "database.canon_commits",
                [canon_record("minio_pre_canon_unavailable")],
            ),
            1,
        ),
        ContractCase(
            "minio candidate identity",
            "minio_pre_canon_unavailable",
            "same_candidate_retried",
            set_mutation(
                "after",
                "database.candidate.content_sha256",
                digest("minio_pre_canon_unavailable", "changed-candidate"),
            ),
            False,
        ),
        ContractCase(
            "minio pre one canon",
            "minio_pre_canon_unavailable",
            "canon_commits_after_recovery",
            multi_mutation(
                set_mutation(
                    "after",
                    "database.canon_commits",
                    [
                        canon_record("minio_pre_canon_unavailable"),
                        canon_record(
                            "minio_pre_canon_unavailable", "secondary"
                        ),
                    ],
                ),
                set_mutation(
                    "after",
                    "database.authoritative_identities",
                    [
                        authoritative_record(
                            "minio_pre_canon_unavailable"
                        ),
                        authoritative_record(
                            "minio_pre_canon_unavailable", "secondary"
                        ),
                    ],
                ),
            ),
            2,
        ),
        ContractCase(
            "minio post canon identity",
            "minio_post_canon_unavailable",
            "canon_identity_unchanged",
            set_mutation(
                "after",
                "database.canon_commits.0.content_sha256",
                digest("minio_post_canon_unavailable", "changed-canon"),
            ),
            False,
        ),
        ContractCase(
            "minio post accepted identity",
            "minio_post_canon_unavailable",
            "accepted_identity_unchanged",
            set_mutation(
                "after",
                "database.accepted_bundles.0.content_sha256",
                digest("minio_post_canon_unavailable", "changed-bundle"),
            ),
            False,
        ),
        ContractCase(
            "minio maintenance retry",
            "minio_post_canon_unavailable",
            "phase3_retry_same_identity",
            set_mutation("after", "database.maintenance.attempt", 1),
            False,
        ),
        ContractCase(
            "minio phase3 replay identity",
            "minio_post_canon_unavailable",
            "phase3_replay_identity_unchanged",
            set_mutation(
                "after",
                "database.phase3_replay_final.row_id",
                token("minio_post_canon_unavailable", "other-phase3-row"),
            ),
            False,
        ),
        ContractCase(
            "minio phase3 release rowcount",
            "minio_post_canon_unavailable",
            "phase3_release_rowcount",
            set_mutation(
                "after",
                "database.phase3_replay_release.conditional_rowcount",
                0,
            ),
            0,
        ),
        ContractCase(
            "minio phase3 release preserves claim counters",
            "minio_post_canon_unavailable",
            "phase3_release_preserved_claim",
            set_mutation(
                "after",
                "database.phase3_replay_release.attempts",
                4,
            ),
            False,
        ),
        ContractCase(
            "minio phase3 worker claim advancement",
            "minio_post_canon_unavailable",
            "phase3_worker_claim_advanced",
            set_mutation(
                "after",
                "database.phase3_replay_final.lease_epoch",
                6,
            ),
            False,
        ),
        ContractCase(
            "minio phase3 final processed",
            "minio_post_canon_unavailable",
            "phase3_replay_final_processed",
            set_mutation(
                "after",
                "database.phase3_replay_final.status",
                "pending",
            ),
            False,
        ),
        ContractCase(
            "minio artifact identity",
            "minio_post_canon_unavailable",
            "artifact_identity_unchanged",
            set_mutation(
                "after",
                "external.artifact.content_sha256",
                digest("minio_post_canon_unavailable", "changed-artifact"),
            ),
            False,
        ),
        ContractCase(
            "minio barrier residue",
            "minio_post_canon_unavailable",
            "barrier_residue_count",
            set_mutation("after", "barrier.residue_count", 1),
            1,
        ),
        ContractCase(
            "backend canon identity",
            "publisher_backend_unavailable",
            "canon_identity_unchanged",
            set_mutation(
                "after",
                "database.canon_commits.0.content_sha256",
                digest("publisher_backend_unavailable", "changed-canon"),
            ),
            False,
        ),
        ContractCase(
            "backend reclaim token",
            "publisher_backend_unavailable",
            "same_job_reclaimed",
            multi_mutation(
                set_mutation(
                    "after",
                    "database.job.owner_token",
                    token("publisher_backend_unavailable", "owner-old"),
                ),
                set_mutation(
                    "after",
                    "database.attempts.0.owner_token",
                    token("publisher_backend_unavailable", "owner-old"),
                ),
            ),
            False,
        ),
        ContractCase(
            "backend stale token result",
            "publisher_backend_unavailable",
            "stale_token_rejected",
            set_mutation(
                "after", "api.stale_token_observation.outcome", "accepted"
            ),
            False,
        ),
        ContractCase(
            "backend shared path result",
            "publisher_backend_unavailable",
            "shared_path_readable",
            set_mutation(
                "after", "external.shared_path_observation.outcome", "missing"
            ),
            False,
        ),
        ContractCase(
            "backend orphan residue",
            "publisher_backend_unavailable",
            "orphan_residue_count",
            set_mutation("after", "external.orphan_residue_count", 1),
            1,
        ),
        ContractCase(
            "browser canon identity",
            "publisher_browser_unavailable",
            "canon_identity_unchanged",
            set_mutation(
                "after",
                "database.canon_commits.0.content_sha256",
                digest("publisher_browser_unavailable", "changed-canon"),
            ),
            False,
        ),
        ContractCase(
            "browser pending job",
            "publisher_browser_unavailable",
            "pending_job_preserved",
            set_mutation("during", "database.job.status", "running"),
            False,
        ),
        ContractCase(
            "browser heartbeat",
            "publisher_browser_unavailable",
            "heartbeat_recovered",
            set_mutation("after", "external.browser_heartbeat.status", "stale"),
            False,
        ),
        ContractCase(
            "browser attempts",
            "publisher_browser_unavailable",
            "attempt_count",
            set_mutation(
                "after",
                "database.attempts",
                [attempt_record("publisher_browser_unavailable")],
            ),
            1,
        ),
        ContractCase(
            "browser receipts",
            "publisher_browser_unavailable",
            "receipt_count",
            set_mutation(
                "after",
                "database.receipts",
                [receipt_record("publisher_browser_unavailable")],
            ),
            1,
        ),
    ]
    for kind in RISK_KINDS:
        cases.extend(
            (
                ContractCase(
                    f"{kind} paused fence",
                    kind,
                    "paused_safely",
                    multi_mutation(
                        set_mutation("during", "database.job.fence", "post-mutation"),
                        set_mutation(
                            "after", "api.resume_actions.0.fence", "post-mutation"
                        ),
                        set_mutation(
                            "after",
                            "api.mutation_observation.fence",
                            "post-mutation",
                        ),
                    ),
                    False,
                ),
                ContractCase(
                    f"{kind} authenticated resume",
                    kind,
                    "operator_action_recorded",
                    set_mutation(
                        "after", "api.resume_actions.0.auth_method", "anonymous"
                    ),
                    False,
                ),
                ContractCase(
                    f"{kind} replay",
                    kind,
                    "resume_replay_idempotent",
                    set_mutation(
                        "after", "api.resume_replay.result", "created_new_action"
                    ),
                    False,
                ),
                ContractCase(
                    f"{kind} bypass",
                    kind,
                    "bypass_attempted",
                    set_mutation(
                        "after",
                        "api.mutation_observation.bypass_attempt_count",
                        1,
                    ),
                    True,
                ),
                ContractCase(
                    f"{kind} external mutation",
                    kind,
                    "paused_safely",
                    set_mutation(
                        "after",
                        "api.mutation_observation.external_mutation_count",
                        1,
                    ),
                    False,
                ),
                ContractCase(
                    f"{kind} receipts",
                    kind,
                    "receipt_count",
                    set_mutation(
                        "after", "database.receipts", [receipt_record(kind)]
                    ),
                    1,
                ),
            )
        )
    return cases


def schema_invariant_cases() -> list[SchemaInvariantCase]:
    cases = [
        SchemaInvariantCase(
            "precommit authoritative coverage",
            "generation_worker_precommit_crash",
            "duplicate_authoritative_identities",
            duplicate_mutation("after", "database.authoritative_identities"),
            "after.state.database.authoritative_identities coverage mismatch",
        ),
        SchemaInvariantCase(
            "postcommit authoritative coverage",
            "generation_worker_postcommit_crash",
            "duplicate_authoritative_identities",
            duplicate_mutation("after", "database.authoritative_identities"),
            "after.state.database.authoritative_identities coverage mismatch",
        ),
        SchemaInvariantCase(
            "qdrant point coverage",
            "qdrant_unavailable",
            "duplicate_vector_identities",
            duplicate_mutation("after", "external.point_identities"),
            "after.state.external.point_identities coverage mismatch",
        ),
        SchemaInvariantCase(
            "projection identity coverage",
            "projection_consumer_unavailable",
            "duplicate_projection_identities",
            duplicate_mutation("after", "external.projection_identities"),
            "after.state.external.projection_identities coverage mismatch",
        ),
        SchemaInvariantCase(
            "minio pre authoritative coverage",
            "minio_pre_canon_unavailable",
            "duplicate_authoritative_identities",
            duplicate_mutation("after", "database.authoritative_identities"),
            "after.state.database.authoritative_identities coverage mismatch",
        ),
        SchemaInvariantCase(
            "minio post authoritative coverage",
            "minio_post_canon_unavailable",
            "duplicate_authoritative_identities",
            duplicate_mutation("after", "database.authoritative_identities"),
            "after.state.database.authoritative_identities coverage mismatch",
        ),
        SchemaInvariantCase(
            "backend job coverage",
            "publisher_backend_unavailable",
            "duplicate_jobs",
            duplicate_mutation("after", "database.jobs"),
            "after.state.database.jobs coverage mismatch",
        ),
        SchemaInvariantCase(
            "backend attempt coverage",
            "publisher_backend_unavailable",
            "duplicate_attempts",
            duplicate_mutation("after", "database.attempts"),
            "after.state.database.attempts coverage mismatch",
        ),
        SchemaInvariantCase(
            "backend receipt coverage",
            "publisher_backend_unavailable",
            "duplicate_receipts",
            duplicate_mutation("after", "database.receipts"),
            "after.state.database.receipts coverage mismatch",
        ),
        SchemaInvariantCase(
            "browser same job",
            "publisher_browser_unavailable",
            "same_job_identity",
            set_mutation(
                "after",
                "database.job.job_id",
                token("publisher_browser_unavailable", "job-other"),
            ),
            "after.state.database.job fixture resource mismatch",
        ),
    ]
    for kind in RISK_KINDS:
        cases.append(
            SchemaInvariantCase(
                f"{kind} same job",
                kind,
                "same_job_identity",
                set_mutation(
                    "after",
                    "database.job.job_id",
                    token(kind, "job-other"),
                ),
                "after.state.database.job fixture resource mismatch",
            )
        )
    return cases


@pytest.mark.parametrize(
    "case", contract_cases(), ids=lambda case: case.name
)
def test_each_reachable_derived_term_has_a_causal_negative(
    case: ContractCase,
) -> None:
    values = valid_snapshots(case.kind)
    case.mutate(values)
    violations = evidence.snapshot_violations(case.kind, values)

    assert violations == []
    assertions = evidence.derive_assertions(case.kind, values)
    assert assertions[case.assertion] == case.actual
    assert any(
        item.startswith(f"{case.kind}.{case.assertion}=")
        for item in evidence.assertion_violations(case.kind, assertions)
    )


@pytest.mark.parametrize(
    "case", schema_invariant_cases(), ids=lambda case: case.name
)
def test_schema_enforced_contract_invariants(
    case: SchemaInvariantCase,
) -> None:
    values = valid_snapshots(case.kind)
    case.mutate(values)

    violations = evidence.snapshot_violations(case.kind, values)

    assert case.violation_fragment in violations
    assert case.assertion in evidence.FAULT_CONTRACTS[case.kind]


def test_each_report_assertion_has_causal_or_schema_invariant_coverage() -> None:
    covered = {
        (case.kind, case.assertion)
        for case in (*contract_cases(), *schema_invariant_cases())
    }
    expected = {
        (kind, assertion)
        for kind, contract in evidence.FAULT_CONTRACTS.items()
        for assertion in contract
    }

    assert covered == expected


def test_cross_fault_snapshot_identities_are_rejected() -> None:
    kind = "generation_worker_postcommit_crash"
    values = valid_snapshots(kind)
    values["after"]["fault_kind"] = "qdrant_unavailable"
    values["during"]["fault_id"] = token(kind, "other-fault")
    values["after"]["source_sha"] = "b" * 40
    values["after"]["stage"] = "during"

    violations = evidence.snapshot_violations(kind, values)

    assert "after.fault_kind mismatch" in violations
    assert "during.fault_id mismatch" in violations
    assert "after.source_sha mismatch" in violations
    assert "after.stage mismatch" in violations


def test_unknown_assertion_keys_and_bool_int_substitution_are_rejected() -> None:
    kind = "generation_worker_precommit_crash"
    assertions = evidence.derive_assertions(kind, valid_snapshots(kind))
    assertions["operator_override"] = True
    assertions["same_task_reclaimed"] = 1

    violations = evidence.assertion_violations(kind, assertions)

    assert f"{kind}.assertions has unknown keys: ['operator_override']" in violations
    assert f"{kind}.same_task_reclaimed=1, expected=True" in violations


def test_empty_stable_inventory_does_not_prove_identity_unchanged() -> None:
    kind = "qdrant_unavailable"
    values = valid_snapshots(kind)
    for snapshot in values.values():
        snapshot["state"]["database"]["canon_commits"] = []

    assertions = evidence.derive_assertions(kind, values)

    assert assertions["canon_identity_unchanged"] is False


def test_stable_hash_uses_compact_sorted_canonical_json() -> None:
    assert evidence.stable_hash({"b": [2, 1], "a": "value"}) == (
        "d3e10b43b5406520aab9974466cc105b2c74b388c08c9621"
        "4caa4e383174ee75"
    )


def test_load_snapshot_accepts_only_a_json_object(tmp_path: Path) -> None:
    valid_path = tmp_path / "valid.json"
    invalid_path = tmp_path / "invalid.json"
    valid_path.write_text(json.dumps({"schema_version": 2}), encoding="utf-8")
    invalid_path.write_text("[]", encoding="utf-8")

    assert evidence.load_snapshot(valid_path) == {"schema_version": 2}
    with pytest.raises(evidence.EvidenceContractError, match="expected JSON object"):
        evidence.load_snapshot(invalid_path)


def test_derive_assertions_rejects_invalid_snapshot_schema() -> None:
    kind = "publisher_browser_unavailable"
    values = valid_snapshots(kind)
    del values["after"]["state"]["external"]["browser_heartbeat"]

    with pytest.raises(
        evidence.EvidenceContractError,
        match="after.state.external.browser_heartbeat is missing",
    ):
        evidence.derive_assertions(kind, values)
