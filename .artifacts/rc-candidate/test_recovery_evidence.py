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
    fixture = {
        "fixture_id": token(kind, "fixture"),
        "fault_id": token(kind, "fault"),
        "resource_type": resource_type,
        "resource_id": resource_id,
    }
    if resource_type == "publisher_job":
        fixture.update(
            logical_key=f"canon-publisher:{token(kind, 'canon-natural')}",
            project_id=token(kind, "project"),
            canon_commit_id=token(kind, "canon-commit"),
            candidate_id=token(kind, "candidate"),
        )
    return fixture


def empty_state(kind: str) -> dict[str, dict[str, Any]]:
    state = {
        "target": {"fixture": fixture_identity(kind)},
        "mcp": {},
        "api": {},
        "database": {},
        "external": {},
        "barrier": {},
    }
    state["target"]["endpoint_identity"] = endpoint_identity(kind)
    return state


def endpoint_identity(kind: str) -> dict[str, Any]:
    fault_id = token(kind, "fault")
    run_id = digest(kind, "run")[:32]
    sentinel = {
        "table": "forwin_recovery_run_sentinel",
        "sentinel_id": digest(kind, "sentinel"),
        "run_id": run_id,
        "fault_id": fault_id,
        "source_sha": SOURCE_SHA,
    }
    record = {
        "schema_version": 1,
        "fault_id": fault_id,
        "run_id": run_id,
        "source_sha": SOURCE_SHA,
        "source_tree": "1" * 40,
        "project_name": f"forwin-v5-recovery-{run_id}",
        "candidate_manifest_sha256": digest(kind, "manifest"),
        "candidate_identity_sha256": digest(kind, "candidate-identity"),
        "sentinel": sentinel,
        "api": {
            "scheme": "http",
            "host": "127.0.0.1",
            "port": 25111,
            "endpoint_path": "",
            "health_path": "/health",
            "health_status": 200,
            "service": "forwin",
            "container_port": 8899,
            "container_id": token(kind, "api-container"),
            "image_id": "sha256:" + digest(kind, "runtime-image"),
        },
        "mcp": {
            "scheme": "http",
            "host": "127.0.0.1",
            "port": 25112,
            "endpoint_path": "/mcp",
            "health_path": "/health",
            "health_status": 200,
            "service": "forwin-mcp",
            "container_port": 8896,
            "container_id": token(kind, "mcp-container"),
            "image_id": "sha256:" + digest(kind, "runtime-image"),
        },
        "database": {
            "scheme": "postgresql",
            "host": "127.0.0.1",
            "port": 25113,
            "database": "forwin",
            "service": "postgres",
            "container_port": 5432,
            "container_id": token(kind, "postgres-container"),
            "image_id": "sha256:" + digest(kind, "postgres-image"),
        },
    }
    if kind == "qdrant_unavailable":
        record["qdrant"] = {
            "scheme": "http",
            "host": "127.0.0.1",
            "port": 25114,
            "endpoint_path": "",
            "health_path": "/readyz",
            "health_status": 200,
            "service": "qdrant",
            "container_port": 6333,
            "container_id": token(kind, "qdrant-container"),
            "image_id": "sha256:" + digest(kind, "qdrant-image"),
        }
    if kind in {
        "minio_pre_canon_unavailable",
        "minio_post_canon_unavailable",
    }:
        record["minio"] = {
            "scheme": "http",
            "host": "127.0.0.1",
            "port": 25115,
            "endpoint_path": "",
            "health_path": "/minio/health/ready",
            "health_status": 200,
            "service": "minio",
            "container_port": 9000,
            "container_id": token(kind, "minio-container"),
            "image_id": "sha256:" + digest(kind, "minio-image"),
        }
    record["identity_sha256"] = evidence.stable_hash(record)
    return record


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
    variant: str = "primary",
    projection_type: str | None = None,
) -> dict[str, Any]:
    identity_label = (
        f"point-{variant}"
        if kind == "qdrant_unavailable"
        else f"projection-{variant}"
    )
    raw_identity = token(kind, identity_label)
    payload_sha256 = digest(kind, f"qdrant-payload-{variant}")
    vector_sha256 = digest(kind, f"qdrant-vector-{variant}")
    evidence_identity = (
        f"{raw_identity}#payload-sha256={payload_sha256}"
        f"#vector-sha256={vector_sha256}"
    )
    record = {
        "projection_type": (
            projection_type
            or ("chapter_memory" if kind == "qdrant_unavailable" else "vector")
        ),
        "identity_id": (
            evidence_identity
            if kind == "qdrant_unavailable"
            else raw_identity
        ),
        "canon_id": token(kind, "canon-primary"),
        "status": status,
    }
    if kind == "qdrant_unavailable":
        record.update(
            {
                "collection": collection,
                "raw_point_id": raw_identity,
                "payload_sha256": payload_sha256,
                "vector_sha256": vector_sha256,
                "vector_dimensions": 3,
            }
        )
    return record


def point_record(
    kind: str,
    variant: str = "primary",
    *,
    collection: str = "fixture-vectors",
    projection_type: str = "chapter_memory",
) -> dict[str, Any]:
    raw_point_id = token(kind, f"point-{variant}")
    payload_sha256 = digest(kind, f"qdrant-payload-{variant}")
    vector_sha256 = digest(kind, f"qdrant-vector-{variant}")
    return {
        "collection": collection,
        "projection_type": projection_type,
        "point_id": (
            f"{raw_point_id}#payload-sha256={payload_sha256}"
            f"#vector-sha256={vector_sha256}"
        ),
        "raw_point_id": raw_point_id,
        "canon_id": token(kind, "canon-primary"),
        "payload_sha256": payload_sha256,
        "vector_sha256": vector_sha256,
        "vector_dimensions": 3,
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


def publisher_job_record(
    kind: str,
    *,
    status: str,
    task_kind: str = "chapter_upload",
    variant: str = "primary",
) -> dict[str, Any]:
    del task_kind
    body = f"Generic publisher recovery fixture content. Identity {digest(kind, variant)[:16]}."
    identity_variant = "" if variant == "primary" else f"-{variant}"
    return {
        "job_id": token(kind, f"job-{variant}"),
        "logical_key": (
            "canon-publisher:"
            f"{token(kind, f'canon-natural{identity_variant}')}"
        ),
        "task_kind": "chapter_upload",
        "project_id": token(kind, f"project{identity_variant}"),
        "platform_id": "qidian",
        "status": status,
        "publish": True,
        "book_name": f"Publisher Recovery Fixture {digest(kind, variant)[:8]}",
        "chapter_title": f"Recovery Chapter {digest(kind, variant)[:8]}",
        "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "unsafe_payload_paths": [],
    }


def backend_job_record(
    kind: str,
    owner: str,
    *,
    status: str = "running",
    artifact_id: str = "",
    variant: str = "primary",
) -> dict[str, Any]:
    del artifact_id
    value = browser_job_record(kind, status=status, variant=variant)
    value.update(
        owner_token=token(kind, owner),
        extension_client_id=token(kind, owner),
    )
    return value


def browser_job_record(
    kind: str, status: str = "pending", variant: str = "primary"
) -> dict[str, Any]:
    identity_variant = "" if variant == "primary" else f"-{variant}"
    base = publisher_job_record(
        kind,
        status=status,
        task_kind="chapter_upload",
        variant=variant,
    )
    return {
        **base,
        "canon_commit_id": token(kind, f"canon-commit{identity_variant}"),
        "candidate_id": token(kind, f"candidate{identity_variant}"),
        "chapter_number": 7,
        "body_text": (
            "Generic publisher recovery fixture content. "
            f"Identity {digest(kind, variant)[:16]}."
        ),
        "upload_url": (
            "https://write.qq.com/booknovelsvip/chaptertmp/"
            f"CBID/{digest(kind, 'book')[:12]}#ccid={digest(kind, 'chapter')[:12]}"
        ),
        "abort_requested": False,
        "owner_token": "",
        "extension_client_id": "",
        "current_attempt_id": "",
        "available_at": "2026-07-22T13:00:00+00:00",
        "reconcile_after": "",
        "claimed_at": "",
        "started_at": "",
        "finished_at": "",
        "deleted_at": "",
        "paused_at": "",
        "pause_reason": "",
        "pause_token": "",
        "risk_boundary": "",
        "current_url": (
            "https://write.qq.com/booknovelsvip/chaptertmp/"
            f"CBID/{digest(kind, 'book')[:12]}#ccid={digest(kind, 'chapter')[:12]}"
        ),
        "result_message": "",
        "error_message": "",
        "result_payload": {},
        "created_at": "2026-07-22T12:00:00+00:00",
        "updated_at": "2026-07-22T12:00:00+00:00",
        "database_now": "2026-07-22T12:30:00+00:00",
    }


def risk_job_record(
    kind: str,
    status: str,
    *,
    pause_reason: str = "",
    pause_token: str = "",
    risk_boundary: str = "",
    variant: str = "primary",
) -> dict[str, Any]:
    value = browser_job_record(kind, status=status, variant=variant)
    value.update(
        pause_reason=pause_reason,
        pause_token=pause_token,
        risk_boundary=risk_boundary,
        current_attempt_id=pause_token,
        result_payload=(
            {
                "risk_pause": {
                    "pause_token": pause_token,
                    "evidence": {"boundary": risk_boundary},
                }
            }
            if pause_token
            else {}
        ),
    )
    return value


def attempt_record(
    kind: str,
    variant: str = "primary",
    *,
    status: str = "paused",
    error_code: str = "",
    phase: str = "claimed",
) -> dict[str, Any]:
    return {
        "attempt_id": token(kind, f"attempt-{variant}"),
        "job_id": token(kind, "job-primary"),
        "attempt_number": 1 if variant == "primary" else 2,
        "attempt_kind": "execute",
        "owner_token": token(kind, "extension-owner"),
        "lease_epoch": 1 if variant == "primary" else 2,
        "status": status,
        "phase": phase,
        "content_sha256": hashlib.sha256(
            (
                "Generic publisher recovery fixture content. "
                f"Identity {digest(kind, 'primary')[:16]}."
            ).encode()
        ).hexdigest(),
        "error_code": error_code,
    }


def receipt_record(kind: str, variant: str = "primary") -> dict[str, str]:
    job = browser_job_record(kind)
    return {
        "receipt_id": token(kind, f"receipt-{variant}"),
        "job_id": token(kind, "job-primary"),
        "attempt_id": token(kind, f"attempt-{variant}"),
        "natural_key": digest(kind, f"receipt-natural-{variant}"),
        "idempotency_key": job["logical_key"],
        "platform_id": "qidian",
        "remote_book_id": digest(kind, "book")[:12],
        "remote_chapter_id": digest(kind, "chapter")[:12],
        "remote_url": job["upload_url"],
        "official_state": "published",
        "content_sha256": job["body_sha256"],
        "source": "extension",
    }


def resume_action(kind: str) -> dict[str, str]:
    pause_token = token(kind, "attempt-primary")
    return {
        "action_id": token(kind, "resume-action"),
        "job_id": token(kind, "job-primary"),
        "natural_key": (
            f"{token(kind, 'job-primary')}:resume:{pause_token}"
        ),
        "action": "resume",
        "pause_token": pause_token,
        "actor_id": f"basic:{token(kind, 'operator')}",
        "auth_method": "basic",
        "reason_sha256": digest(kind, "operator-reason"),
        "old_status": "paused",
        "new_status": "pending",
    }


def resume_replay(kind: str) -> dict[str, str]:
    return {
        "pause_token": token(kind, "attempt-primary"),
        "pause_reason": {
            "publisher_captcha": "captcha",
            "publisher_mfa": "mfa",
            "publisher_account_risk": "account_risk",
        }[kind],
        "first_disposition": "applied",
        "replay_disposition": "idempotent",
    }


def publisher_canon_source(kind: str) -> dict[str, Any]:
    job = browser_job_record(kind)
    return {
        "project_id": job["project_id"],
        "chapter_plan_id": token(kind, "chapter-plan"),
        "draft_id": token(kind, "draft"),
        "candidate_id": job["candidate_id"],
        "canon_commit_id": job["canon_commit_id"],
        "canon_idempotency_key": token(kind, "canon-natural"),
        "canon_status": "committed",
        "candidate_status": "accepted",
        "candidate_canon_status": "committed",
        "chapter_status": "accepted",
        "chapter_number": job["chapter_number"],
        "body_hash": job["body_sha256"],
    }


def publisher_detector(kind: str) -> dict[str, str]:
    reason = {
        "publisher_captcha": "captcha",
        "publisher_mfa": "mfa",
        "publisher_account_risk": "account_risk",
    }[kind]
    return {
        "detector": "publisher-risk-v1",
        "boundary": "pre-mutation",
        "selector": f"#publisher-recovery-{reason}",
        "matched_text": f"{reason} fixture",
        "message": f"publisher {reason} detected",
        "risk_reason": reason,
        "observed_at": "2026-07-22T12:15:00+00:00",
        "attempt_id": token(kind, "attempt-primary"),
    }


def publisher_effect(kind: str, count: int) -> dict[str, Any]:
    return {
        "fixture_id": token(kind, "fixture"),
        "effect_key_sha256": digest(kind, "effect-key"),
        "upload_effect_count": count,
    }


def publisher_terminal_fault(kind: str, mode: str) -> dict[str, str]:
    return {
        "job_id": token(kind, "job-primary"),
        "mode": mode,
        "installed_at": "2026-07-22T12:14:00+00:00",
        "observed_at": "2026-07-22T12:16:00+00:00",
        "request_url": (
            "http://forwin.invalid/api/publishers/extension/upload-jobs/"
            f"{token(kind, 'job-primary')}/attempts/"
            f"{token(kind, 'attempt-primary')}/receipt"
        ),
    }


def publisher_journal(kind: str) -> dict[str, str]:
    fault = publisher_terminal_fault(kind, "unused")
    return {
        "job_id": token(kind, "job-primary"),
        "attempt_id": token(kind, "attempt-primary"),
        "journal_phase": "ack_pending",
        "receipt_key": digest(kind, "receipt-natural-primary"),
        "content_sha256": browser_job_record(kind)["body_sha256"],
        "fault_observed_at": fault["observed_at"],
        "fault_request_url": fault["request_url"],
    }


def publisher_browser_lifecycle(kind: str, action: str) -> dict[str, str]:
    return {
        "action": action,
        "service": "publisher-browser",
        "fault_id": token(kind, "fault"),
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
        baseline_projections = [
            projection_observation(
                kind,
                collection="fixture-memory-vectors",
            ),
            projection_observation(
                kind,
                collection="fixture-kb-vectors",
                variant="secondary",
                projection_type="llm_kb",
            ),
        ]
        baseline_points = [
            point_record(kind, collection="fixture-memory-vectors"),
            point_record(
                kind,
                "secondary",
                collection="fixture-kb-vectors",
                projection_type="llm_kb",
            ),
        ]
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
    elif kind in PUBLISHER_KINDS:
        for snapshot in values.values():
            snapshot["state"]["database"].update(
                {
                    "canon_source": publisher_canon_source(kind),
                    "job": browser_job_record(kind),
                    "job_identity_count": 1,
                    "status": "pending",
                    "attempts": [],
                    "receipts": [],
                    "resume_actions": [],
                    "detector_evidence": {},
                }
            )
        before["external"].update(
            {
                "browser": {
                    "browser_id": token(kind, "browser"),
                    "status": "healthy",
                    "probe": "extension_service_worker_cdp",
                },
                **publisher_effect(kind, 0),
            }
        )
        if kind in {
            "publisher_backend_unavailable",
            "publisher_browser_unavailable",
        }:
            mode = (
                "backend_unavailable"
                if kind == "publisher_backend_unavailable"
                else "browser_shutdown_barrier"
            )
            during_job = browser_job_record(kind, status="running")
            during_job.update(
                owner_token=token(kind, "extension-owner"),
                extension_client_id=token(kind, "extension-owner"),
                current_attempt_id=token(kind, "attempt-primary"),
                started_at="2026-07-22T12:15:00+00:00",
            )
            during["database"].update(
                job=during_job,
                status="running",
                attempts=[
                    attempt_record(
                        kind,
                        status="running",
                        phase="mutation_started",
                    )
                ],
            )
            during["external"].update(
                {
                    "terminal_fault": publisher_terminal_fault(kind, mode),
                    "journal": publisher_journal(kind),
                    **publisher_effect(kind, 1),
                }
            )
            after_job = copy.deepcopy(during_job)
            after_job.update(
                status="succeeded",
                finished_at="2026-07-22T12:17:00+00:00",
                result_message="published",
            )
            after["database"].update(
                job=after_job,
                status="succeeded",
                attempts=[
                    attempt_record(
                        kind,
                        status="succeeded",
                        phase="result_submitted",
                    )
                ],
                receipts=[receipt_record(kind)],
            )
            after["external"].update(
                {
                    "journal_replay": {
                        "attempt_id": token(kind, "attempt-primary")
                    },
                    **publisher_effect(kind, 1),
                }
            )
            if kind == "publisher_browser_unavailable":
                during["external"]["browser_fault"] = (
                    publisher_browser_lifecycle(
                        kind,
                        "fault_service_stopped",
                    )
                )
                after["external"]["browser_recovery"] = (
                    publisher_browser_lifecycle(
                        kind,
                        "fault_service_recovered",
                    )
                )
        else:
            risk_reason = {
                "publisher_captcha": "captcha",
                "publisher_mfa": "mfa",
                "publisher_account_risk": "account_risk",
            }[kind]
            detector = publisher_detector(kind)
            during_job = risk_job_record(
                kind,
                "paused",
                pause_reason=risk_reason,
                pause_token=token(kind, "attempt-primary"),
                risk_boundary="pre-mutation",
            )
            during["database"].update(
                job=during_job,
                status="paused",
                attempts=[
                    attempt_record(
                        kind,
                        status="paused",
                        error_code=risk_reason,
                    )
                ],
                detector_evidence=detector,
            )
            during["external"].update(
                {
                    "detector_evidence": copy.deepcopy(detector),
                    **publisher_effect(kind, 0),
                }
            )
            after_job = browser_job_record(kind, status="succeeded")
            after_job.update(
                owner_token=token(kind, "extension-owner"),
                extension_client_id=token(kind, "extension-owner"),
                current_attempt_id=token(kind, "attempt-secondary"),
                finished_at="2026-07-22T12:17:00+00:00",
                result_message="published",
            )
            after["database"].update(
                job=after_job,
                status="succeeded",
                attempts=[
                    attempt_record(
                        kind,
                        status="paused",
                        error_code=risk_reason,
                    ),
                    attempt_record(
                        kind,
                        "secondary",
                        status="succeeded",
                        phase="result_submitted",
                    ),
                ],
                receipts=[receipt_record(kind, "secondary")],
                resume_actions=[resume_action(kind)],
                detector_evidence=detector,
            )
            after["external"].update(
                {
                    "operator_resume": resume_replay(kind),
                    **publisher_effect(kind, 1),
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
        "publisher Canon source",
        "publisher_backend_unavailable",
        "after",
        "database.canon_source",
        publisher_canon_source("publisher_backend_unavailable"),
        "canon_commit_id",
        7,
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
        "publisher terminal fault",
        "publisher_backend_unavailable",
        "during",
        "external.terminal_fault",
        publisher_terminal_fault(
            "publisher_backend_unavailable", "backend_unavailable"
        ),
        "request_url",
        7,
    ),
    RecordCase(
        "publisher journal",
        "publisher_backend_unavailable",
        "during",
        "external.journal",
        publisher_journal("publisher_backend_unavailable"),
        "attempt_id",
        7,
    ),
    RecordCase(
        "publisher journal replay",
        "publisher_browser_unavailable",
        "after",
        "external.journal_replay",
        {"attempt_id": token("publisher_browser_unavailable", "attempt-primary")},
        "attempt_id",
        7,
    ),
    RecordCase(
        "publisher browser lifecycle",
        "publisher_browser_unavailable",
        "during",
        "external.browser_fault",
        publisher_browser_lifecycle(
            "publisher_browser_unavailable", "fault_service_stopped"
        ),
        "action",
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
        "publisher detector evidence",
        "publisher_captcha",
        "during",
        "database.detector_evidence",
        publisher_detector("publisher_captcha"),
        "detector",
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
        "database.resume_actions",
        resume_action("publisher_captcha"),
        "action_id",
        7,
        True,
    ),
    RecordCase(
        "operator resume replay",
        "publisher_captcha",
        "after",
        "external.operator_resume",
        resume_replay("publisher_captcha"),
        "pause_token",
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
        ("qdrant_unavailable", "external.point_identities", "raw_point_id"),
        (
            "qdrant_unavailable",
            "external.point_identities",
            "payload_sha256",
        ),
        (
            "qdrant_unavailable",
            "external.point_identities",
            "vector_sha256",
        ),
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
    rows[0][field] = (
        digest(kind, "wrong-binding")
        if field.endswith("_sha256")
        else token(kind, "wrong-binding")
    )

    assert any(
        f"{path} coverage mismatch" in item
        for item in evidence.snapshot_violations(kind, values)
    )


def test_qdrant_identity_inventory_rejects_zero_vector_dimensions() -> None:
    values = valid_snapshots("qdrant_unavailable")
    values["after"]["state"]["external"]["point_identities"][0][
        "vector_dimensions"
    ] = 0

    assert any(
        "vector_dimensions is not positive" in item
        for item in evidence.snapshot_violations(
            "qdrant_unavailable",
            values,
        )
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
                "database.receipts.0.content_sha256",
                "database.receipts[0].content_sha256",
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
                "database.job_identity_count",
                "database.job_identity_count",
        ),
        (
            "publisher_captcha",
            "after",
            "database.attempts.0.attempt_number",
            "database.attempts[0].attempt_number",
        ),
        (
            "minio_post_canon_unavailable",
            "after",
            "barrier.residue_count",
            "barrier.residue_count",
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


def test_fault_local_fixtures_are_distinct_and_publishers_bind_canon() -> None:
    fixtures = [fixture_identity(kind) for kind in FAULT_KINDS]

    assert len({item["fixture_id"] for item in fixtures}) == len(FAULT_KINDS)
    for kind in FAULT_KINDS:
        values = valid_snapshots(kind)
        fixture = values["before"]["state"]["target"]["fixture"]
        if kind in PUBLISHER_KINDS:
            job = values["before"]["state"]["database"]["job"]
            source = values["before"]["state"]["database"]["canon_source"]
            assert fixture["project_id"] == job["project_id"] == source["project_id"]
            assert fixture["canon_commit_id"] == job["canon_commit_id"]
            assert fixture["candidate_id"] == job["candidate_id"]
            assert job["task_kind"] == "chapter_upload"
            assert job["publish"] is True
            assert job["unsafe_payload_paths"] == []
        else:
            assert "project_id" not in fixture


@pytest.mark.parametrize(
    "kind",
    (
        "publisher_browser_unavailable",
        "publisher_captcha",
        "publisher_mfa",
        "publisher_account_risk",
    ),
)
def test_publisher_faults_require_exact_attempt_and_receipt_inventories(
    kind: str,
) -> None:
    values = valid_snapshots(kind)

    expected_attempts = 1 if kind == "publisher_browser_unavailable" else 2
    assert len(values["after"]["state"]["database"]["attempts"]) == expected_attempts
    assert len(values["after"]["state"]["database"]["receipts"]) == 1
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
    ("stage", "path", "value", "assertion"),
    (
        (
            "during",
            "external.terminal_fault.job_id",
            token("publisher_backend_unavailable", "job-other"),
            "retryable_during_fault",
        ),
        (
            "during",
            "external.terminal_fault.mode",
            "browser_shutdown_barrier",
            "retryable_during_fault",
        ),
        (
            "during",
            "external.journal.job_id",
            token("publisher_backend_unavailable", "job-other"),
            "retryable_during_fault",
        ),
        (
            "during",
            "external.journal.attempt_id",
            token("publisher_backend_unavailable", "attempt-other"),
            "retryable_during_fault",
        ),
        (
            "after",
            "external.journal_replay.attempt_id",
            token("publisher_backend_unavailable", "attempt-other"),
            "journal_replayed",
        ),
        (
            "after",
            "database.receipts.0.natural_key",
            digest("publisher_backend_unavailable", "receipt-other"),
            "journal_replayed",
        ),
    ),
)
def test_backend_terminal_journal_is_bound_to_job_attempt_and_receipt(
    stage: str, path: str, value: Any, assertion: str
) -> None:
    kind = "publisher_backend_unavailable"
    values = valid_snapshots(kind)
    set_path(values[stage]["state"], path, value)

    assert evidence.snapshot_violations(kind, values) == []
    assertions = evidence.derive_assertions(kind, values)
    assert assertions[assertion] is False
    assert evidence.assertion_violations(kind, assertions)


@pytest.mark.parametrize(
    ("path", "value"),
    (
        ("database.job.pause_reason", "mfa"),
        ("database.job.risk_boundary", "post-mutation"),
        ("database.attempts.0.phase", "mutation_started"),
    ),
)
def test_risk_type_and_fence_are_derived_from_paused_job_and_attempt(
    path: str, value: str
) -> None:
    kind = "publisher_captcha"
    values = valid_snapshots(kind)
    set_path(values["during"]["state"], path, value)

    assert evidence.snapshot_violations(kind, values) == []
    assertions = evidence.derive_assertions(kind, values)
    assert assertions["typed_pause_from_detector"] is False
    assert evidence.assertion_violations(kind, assertions)


@pytest.mark.parametrize(
    ("path", "value", "assertion"),
    (
        (
            "database.resume_actions.0.natural_key",
            "wrong-natural-key",
            "operator_action_recorded",
        ),
        (
            "database.resume_actions.0.auth_method",
            "anonymous",
            "operator_action_recorded",
        ),
        (
            "external.operator_resume.pause_token",
            "wrong-attempt",
            "resume_replay_idempotent",
        ),
        (
            "external.operator_resume.pause_reason",
            "mfa",
            "resume_replay_idempotent",
        ),
        (
            "external.operator_resume.replay_disposition",
            "applied",
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


def test_risk_rejects_duplicate_resume_actions_even_with_same_natural_key() -> None:
    kind = "publisher_mfa"
    values = valid_snapshots(kind)
    values["after"]["state"]["database"]["resume_actions"].append(
        copy.deepcopy(
            values["after"]["state"]["database"]["resume_actions"][0]
        )
    )

    assert evidence.snapshot_violations(kind, values) == []
    assertions = evidence.derive_assertions(kind, values)
    assert assertions["operator_action_recorded"] is False
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
                "database.canon_source.project_id",
                "wrong-project",
                "publisher Canon source is not stable",
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
        "database.receipts[0] publisher identity mismatch" in item
        for item in evidence.snapshot_violations(kind, values)
    )


def test_backend_rejects_unexpected_attempts_and_receipts() -> None:
    kind = "publisher_backend_unavailable"
    values = valid_snapshots(kind)
    values["after"]["state"]["database"]["attempts"] = [attempt_record(kind)]
    values["after"]["state"]["database"]["receipts"] = [receipt_record(kind)]

    assert evidence.snapshot_violations(kind, values) == []
    assertions = evidence.derive_assertions(kind, values)
    assert assertions["attempt_count"] == 1
    assert assertions["receipt_count"] == 1
    assert evidence.assertion_violations(kind, assertions)


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


def qdrant_identity_mutation(variant: str) -> Callable[[dict[str, Any]], None]:
    def mutate(values: dict[str, Any]) -> None:
        raw_point_id = token("qdrant_unavailable", f"point-{variant}")
        payload_sha256 = digest(
            "qdrant_unavailable",
            f"qdrant-payload-{variant}",
        )
        vector_sha256 = digest(
            "qdrant_unavailable",
            f"qdrant-vector-{variant}",
        )
        evidence_identity = (
            f"{raw_point_id}#payload-sha256={payload_sha256}"
            f"#vector-sha256={vector_sha256}"
        )
        projection = values["after"]["state"]["external"]["projections"][0]
        identity = values["after"]["state"]["external"][
            "point_identities"
        ][0]
        projection.update(
            {
                "identity_id": evidence_identity,
                "raw_point_id": raw_point_id,
                "payload_sha256": payload_sha256,
                "vector_sha256": vector_sha256,
            }
        )
        identity.update(
            {
                "point_id": evidence_identity,
                "raw_point_id": raw_point_id,
                "payload_sha256": payload_sha256,
                "vector_sha256": vector_sha256,
            }
        )

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
            qdrant_identity_mutation("after-refresh"),
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
    ]
    for kind in (
        "publisher_backend_unavailable",
        "publisher_browser_unavailable",
    ):
        cases.extend(
            (
                ContractCase(
                    f"{kind} same attempt convergence",
                    kind,
                    "same_job_attempt_converged",
                    set_mutation(
                        "after",
                        "database.attempts.0.status",
                        "running",
                    ),
                    False,
                ),
                ContractCase(
                    f"{kind} retryable boundary",
                    kind,
                    "retryable_during_fault",
                    set_mutation(
                        "during",
                        "external.journal.journal_phase",
                        "queued",
                    ),
                    False,
                ),
                ContractCase(
                    f"{kind} journal replay",
                    kind,
                    "journal_replayed",
                    set_mutation(
                        "after",
                        "external.journal_replay.attempt_id",
                        token(kind, "attempt-other"),
                    ),
                    False,
                ),
                ContractCase(
                    f"{kind} external effect",
                    kind,
                    "external_effect_at_most_once",
                    set_mutation(
                        "after",
                        "external.upload_effect_count",
                        2,
                    ),
                    False,
                ),
                ContractCase(
                    f"{kind} duplicate jobs",
                    kind,
                    "duplicate_jobs",
                    set_mutation(
                        "after",
                        "database.job_identity_count",
                        2,
                    ),
                    1,
                ),
                ContractCase(
                    f"{kind} duplicate attempts",
                    kind,
                    "duplicate_attempts",
                    duplicate_mutation("after", "database.attempts"),
                    1,
                ),
                ContractCase(
                    f"{kind} duplicate receipts",
                    kind,
                    "duplicate_receipts",
                    duplicate_mutation("after", "database.receipts"),
                    1,
                ),
                ContractCase(
                    f"{kind} attempt count",
                    kind,
                    "attempt_count",
                    set_mutation(
                        "after",
                        "database.attempts",
                        [
                            attempt_record(
                                kind,
                                status="succeeded",
                                phase="result_submitted",
                            ),
                            attempt_record(
                                kind,
                                "secondary",
                                status="succeeded",
                                phase="result_submitted",
                            ),
                        ],
                    ),
                    2,
                ),
                ContractCase(
                    f"{kind} receipt count",
                    kind,
                    "receipt_count",
                    set_mutation("after", "database.receipts", []),
                    0,
                ),
            )
        )
    cases.append(
        ContractCase(
            "publisher backend fault observation",
            "publisher_backend_unavailable",
            "backend_fault_observed",
            set_mutation(
                "during",
                "external.terminal_fault.request_url",
                "http://forwin.invalid/wrong-terminal",
            ),
            False,
        )
    )
    for kind in RISK_KINDS:
        reason = {
            "publisher_captcha": "captcha",
            "publisher_mfa": "mfa",
            "publisher_account_risk": "account_risk",
        }[kind]
        third_attempt = attempt_record(
            kind,
            "tertiary",
            status="succeeded",
            phase="result_submitted",
        )
        third_attempt.update(
            attempt_id=token(kind, "attempt-tertiary"),
            attempt_number=3,
            lease_epoch=3,
        )
        cases.extend(
            (
                ContractCase(
                    f"{kind} typed detector pause",
                    kind,
                    "typed_pause_from_detector",
                    multi_mutation(
                        set_mutation(
                            "during",
                            "database.detector_evidence.boundary",
                            "post-mutation",
                        ),
                        set_mutation(
                            "during",
                            "external.detector_evidence.boundary",
                            "post-mutation",
                        ),
                    ),
                    False,
                ),
                ContractCase(
                    f"{kind} operator action",
                    kind,
                    "operator_action_recorded",
                    set_mutation(
                        "after",
                        "database.resume_actions.0.auth_method",
                        "anonymous",
                    ),
                    False,
                ),
                ContractCase(
                    f"{kind} resume replay",
                    kind,
                    "resume_replay_idempotent",
                    set_mutation(
                        "after",
                        "external.operator_resume.replay_disposition",
                        "applied",
                    ),
                    False,
                ),
                ContractCase(
                    f"{kind} no bypass",
                    kind,
                    "no_bypass",
                    set_mutation(
                        "during",
                        "external.upload_effect_count",
                        1,
                    ),
                    False,
                ),
                ContractCase(
                    f"{kind} external effect",
                    kind,
                    "external_effect_at_most_once",
                    set_mutation(
                        "after",
                        "external.upload_effect_count",
                        2,
                    ),
                    False,
                ),
                ContractCase(
                    f"{kind} duplicate jobs",
                    kind,
                    "duplicate_jobs",
                    set_mutation(
                        "after",
                        "database.job_identity_count",
                        2,
                    ),
                    1,
                ),
                ContractCase(
                    f"{kind} duplicate attempts",
                    kind,
                    "duplicate_attempts",
                    duplicate_mutation("after", "database.attempts", 1),
                    1,
                ),
                ContractCase(
                    f"{kind} duplicate receipts",
                    kind,
                    "duplicate_receipts",
                    duplicate_mutation("after", "database.receipts"),
                    1,
                ),
                ContractCase(
                    f"{kind} attempt count",
                    kind,
                    "attempt_count",
                    set_mutation(
                        "after",
                        "database.attempts",
                        [
                            attempt_record(
                                kind,
                                status="paused",
                                error_code=reason,
                            ),
                            attempt_record(
                                kind,
                                "secondary",
                                status="succeeded",
                                phase="result_submitted",
                            ),
                            third_attempt,
                        ],
                    ),
                    3,
                ),
                ContractCase(
                    f"{kind} receipt count",
                    kind,
                    "receipt_count",
                    set_mutation("after", "database.receipts", []),
                    0,
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
    ]
    for kind in FAULT_KINDS:
        cases.append(
            SchemaInvariantCase(
                f"{kind} endpoint identity",
                kind,
                "isolated_endpoint_identity",
                set_mutation(
                    "during",
                    "target.endpoint_identity.run_id",
                    "f" * 32,
                ),
                f"{kind}.endpoint identity is not stable or bound",
            )
        )
    for kind in PUBLISHER_KINDS:
        cases.extend(
            (
                SchemaInvariantCase(
                    f"{kind} Canon fixture binding",
                    kind,
                    "fixture_safe",
                    set_mutation(
                        "after",
                        "database.job.project_id",
                        token(kind, "project-other"),
                    ),
                    "publisher Canon job identity is not stable",
                ),
                SchemaInvariantCase(
                    f"{kind} Canon source binding",
                    kind,
                    "canon_source_bound",
                    set_mutation(
                        "after",
                        "database.canon_source.body_hash",
                        digest(kind, "body-other"),
                    ),
                    "publisher Canon source is not stable",
                ),
            )
        )
    cases.append(
        SchemaInvariantCase(
            "publisher browser lifecycle",
            "publisher_browser_unavailable",
            "browser_restarted",
            set_mutation(
                "after",
                "external.browser_recovery.action",
                "recovery_marked",
            ),
            (
                "after.state.external.browser_recovery does not describe "
                "the real browser lifecycle"
            ),
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
    del values["after"]["state"]["external"]["journal_replay"]

    with pytest.raises(
        evidence.EvidenceContractError,
        match="after.state.external.journal_replay is missing",
    ):
        evidence.derive_assertions(kind, values)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("logical_key", "canon-publisher:wrong"),
        ("task_kind", "cover_generate"),
        ("project_id", "wrong-project"),
        ("platform_id", "other-platform"),
        ("publish", False),
        ("canon_commit_id", "wrong-canon"),
        ("candidate_id", "wrong-candidate"),
        ("chapter_number", 9),
        ("body_sha256", "0" * 64),
        ("upload_url", "https://remote.invalid/upload"),
    ),
)
def test_browser_canon_identity_rejects_each_drifted_field(
    field: str,
    value: Any,
) -> None:
    kind = "publisher_browser_unavailable"
    values = valid_snapshots(kind)
    values["during"]["state"]["database"]["job"][field] = value

    violations = evidence.snapshot_violations(kind, values)

    assert any(
        "publisher Canon job identity is not stable" in violation
        or "publisher fixture is not bound to a safe Canon chapter upload" in violation
        for violation in violations
    )


@pytest.mark.parametrize(
    ("path", "value"),
    (
        ("database.job.current_attempt_id", "attempt-drift-generalized"),
        ("database.attempts.0.owner_token", "owner-drift-generalized"),
        ("database.attempts.0.lease_epoch", 2),
    ),
)
def test_browser_recovery_requires_same_job_attempt_to_converge(
    path: str,
    value: Any,
) -> None:
    kind = "publisher_browser_unavailable"
    values = valid_snapshots(kind)
    set_path(values["after"]["state"], path, value)

    assertions = evidence.derive_assertions(kind, values)

    assert assertions["same_job_attempt_converged"] is False


@pytest.mark.parametrize(
    ("stage", "field", "value"),
    (
        ("during", "journal_phase", "queued"),
        ("during", "attempt_id", "wrong-attempt"),
        ("after", "attempt_id", "wrong-attempt"),
    ),
)
def test_browser_journal_must_remain_bound_across_recovery(
    stage: str,
    field: str,
    value: str,
) -> None:
    kind = "publisher_browser_unavailable"
    values = valid_snapshots(kind)
    key = "journal" if stage == "during" else "journal_replay"
    values[stage]["state"]["external"][key][field] = value

    assertions = evidence.derive_assertions(kind, values)

    assertion = "retryable_during_fault" if stage == "during" else "journal_replayed"
    assert assertions[assertion] is False


@pytest.mark.parametrize("kind", PUBLISHER_KINDS)
def test_publisher_endpoint_identity_must_be_stable_and_self_hashed(
    kind: str,
) -> None:
    values = valid_snapshots(kind)
    values["during"]["state"]["target"]["endpoint_identity"]["run_id"] = (
        "f" * 32
    )

    violations = evidence.snapshot_violations(kind, values)

    assert any("endpoint identity" in violation for violation in violations)


def test_publisher_endpoint_identity_rejects_cross_stack_project_prefix() -> None:
    kind = "publisher_browser_unavailable"
    values = valid_snapshots(kind)
    for stage in ("before", "during", "after"):
        endpoint = values[stage]["state"]["target"]["endpoint_identity"]
        endpoint["project_name"] = "cross-stack-" + endpoint["run_id"]
        endpoint["identity_sha256"] = evidence.stable_hash(
            {
                key: value
                for key, value in endpoint.items()
                if key != "identity_sha256"
            }
        )

    violations = evidence.snapshot_violations(kind, values)

    assert any("endpoint identity" in violation for violation in violations)


@pytest.mark.parametrize(
    "kind",
    (
        "generation_worker_precommit_crash",
        "projection_consumer_unavailable",
        "qdrant_unavailable",
        "minio_pre_canon_unavailable",
        "minio_post_canon_unavailable",
    ),
)
def test_task4_and_task5_reject_mixed_stack_endpoint_snapshots(
    kind: str,
) -> None:
    values = valid_snapshots(kind)
    endpoint = values["during"]["state"]["target"]["endpoint_identity"]
    endpoint["api"]["container_id"] = token(kind, "cross-stack-api")
    endpoint["identity_sha256"] = evidence.stable_hash(
        {
            key: value
            for key, value in endpoint.items()
            if key != "identity_sha256"
        }
    )

    violations = evidence.snapshot_violations(kind, values)

    assert any("endpoint identity" in violation for violation in violations)


@pytest.mark.parametrize(
    ("kind", "dependency"),
    (
        ("qdrant_unavailable", "qdrant"),
        ("minio_pre_canon_unavailable", "minio"),
        ("minio_post_canon_unavailable", "minio"),
    ),
)
def test_consumed_optional_endpoint_cannot_be_missing_or_wrong_service(
    kind: str,
    dependency: str,
) -> None:
    for mutation in ("missing", "wrong-service"):
        values = valid_snapshots(kind)
        for stage in evidence.STAGES:
            endpoint = values[stage]["state"]["target"]["endpoint_identity"]
            if mutation == "missing":
                endpoint.pop(dependency)
            else:
                endpoint[dependency]["service"] = "forwin"
            endpoint["identity_sha256"] = evidence.stable_hash(
                {
                    key: value
                    for key, value in endpoint.items()
                    if key != "identity_sha256"
                }
            )

        violations = evidence.snapshot_violations(kind, values)

        assert any("endpoint identity" in violation for violation in violations)


@pytest.mark.parametrize(
    ("path", "value"),
    (
        ("database.attempts.0.status", "running"),
        ("external.upload_effect_count", 2),
        (
            "database.receipts",
            [receipt_record("publisher_captcha")],
        ),
    ),
)
def test_typed_risk_resume_must_not_bypass_the_paused_attempt(
    path: str,
    value: Any,
) -> None:
    kind = "publisher_captcha"
    values = valid_snapshots(kind)
    set_path(values["during"]["state"], path, value)

    assertions = evidence.derive_assertions(kind, values)

    assert assertions["no_bypass"] is False


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("detector", "runner-supplied"),
        ("boundary", "post-mutation"),
        ("risk_reason", "mfa"),
    ),
)
def test_typed_risk_requires_production_detector_evidence(
    field: str,
    value: Any,
) -> None:
    kind = "publisher_account_risk"
    values = valid_snapshots(kind)
    values["during"]["state"]["database"]["detector_evidence"][field] = value
    values["during"]["state"]["external"]["detector_evidence"][field] = value

    assertions = evidence.derive_assertions(kind, values)

    assert assertions["typed_pause_from_detector"] is False


@pytest.mark.parametrize("field", ("selector", "matched_text"))
def test_typed_risk_detector_requires_concrete_browser_observation(
    field: str,
) -> None:
    kind = "publisher_account_risk"
    values = valid_snapshots(kind)
    values["during"]["state"]["database"]["detector_evidence"][field] = ""
    values["during"]["state"]["external"]["detector_evidence"][field] = ""

    assert any(
        f"detector_evidence.{field} has invalid str value" in violation
        for violation in evidence.snapshot_violations(kind, values)
    )


def test_typed_risk_requires_matching_browser_detector_evidence() -> None:
    kind = "publisher_mfa"
    values = valid_snapshots(kind)
    values["during"]["state"]["external"]["detector_evidence"][
        "matched_text"
    ] = "different observation"

    assertions = evidence.derive_assertions(kind, values)

    assert assertions["typed_pause_from_detector"] is False


@pytest.mark.parametrize("mutation", ("duplicate", "journal_mismatch"))
def test_publisher_receipt_evidence_is_unique_and_journal_bound(
    mutation: str,
) -> None:
    kind = "publisher_backend_unavailable"
    values = valid_snapshots(kind)
    if mutation == "duplicate":
        receipts = values["after"]["state"]["database"]["receipts"]
        receipts.append(copy.deepcopy(receipts[0]))
    else:
        values["after"]["state"]["database"]["receipts"][0][
            "natural_key"
        ] = digest(kind, "receipt-mismatch")

    assert evidence.snapshot_violations(kind, values) == []
    assertions = evidence.derive_assertions(kind, values)
    assertion = "duplicate_receipts" if mutation == "duplicate" else "journal_replayed"
    assert assertions[assertion] != evidence.FAULT_CONTRACTS[kind][assertion]


def test_task6_publisher_contracts_require_independently_derived_terms() -> None:
    assert evidence.FAULT_CONTRACTS["publisher_backend_unavailable"] == {
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
    }
    assert evidence.FAULT_CONTRACTS["publisher_browser_unavailable"] == {
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
    }
    expected_risk = {
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
    }
    for kind in RISK_KINDS:
        assert evidence.FAULT_CONTRACTS[kind] == expected_risk
