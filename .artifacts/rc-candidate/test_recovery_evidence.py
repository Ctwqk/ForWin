from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

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


def state() -> dict[str, dict[str, Any]]:
    return {
        "target": {},
        "mcp": {},
        "api": {},
        "database": {},
        "external": {},
        "barrier": {},
    }


def snapshots(kind: str) -> dict[str, dict[str, Any]]:
    values: dict[str, dict[str, Any]] = {}
    for stage in evidence.STAGES:
        values[stage] = {
            "schema_version": 2,
            "source_sha": "a" * 40,
            "fault_kind": kind,
            "fault_id": f"fault-{kind}",
            "stage": stage,
            "state": state(),
        }
    return values


def canon_identity() -> dict[str, str]:
    return {
        "canon_id": "canon-1",
        "project_id": "project-1",
        "chapter_id": "chapter-1",
    }


def authoritative_identity() -> dict[str, str]:
    return {"entity_type": "canon", "natural_key": "project-1:chapter-1"}


def valid_snapshots(kind: str) -> dict[str, dict[str, Any]]:
    values = snapshots(kind)
    before = values["before"]["state"]
    during = values["during"]["state"]
    after = values["after"]["state"]
    canon = [canon_identity()]

    if kind == "generation_worker_precommit_crash":
        before["database"]["task"] = {"id": "task-1", "lease_epoch": 4}
        during["database"].update(
            {
                "task": {"id": "task-1", "lease_epoch": 4},
                "canon_commits": [],
            }
        )
        after["database"].update(
            {
                "task": {"id": "task-1", "lease_epoch": 5},
                "canon_commits": canon,
                "authoritative_identities": [authoritative_identity()],
            }
        )
    elif kind == "generation_worker_postcommit_crash":
        before["database"]["task"] = {"id": "task-1", "lease_epoch": 4}
        during["database"].update(
            {
                "task": {"id": "task-1", "lease_epoch": 4},
                "canon_commits": canon,
                "accepted_bundles": [{"bundle_id": "bundle-1"}],
            }
        )
        after["database"].update(
            {
                "task": {"id": "task-1", "lease_epoch": 5},
                "canon_commits": canon,
                "accepted_bundles": [{"bundle_id": "bundle-1"}],
                "authoritative_identities": [authoritative_identity()],
            }
        )
    elif kind == "qdrant_unavailable":
        for snapshot in values.values():
            snapshot["state"]["database"]["canon_commits"] = copy.deepcopy(canon)
        before["database"]["outbox"] = {"id": "outbox-1", "attempt": 0}
        during["database"]["outbox"] = {"id": "outbox-1", "attempt": 0}
        after["database"]["outbox"] = {"id": "outbox-1", "attempt": 1}
        after["external"].update(
            {
                "projections": [
                    {"name": "vector", "identity": "canon-1", "converged": True},
                    {"name": "search", "identity": "canon-1", "converged": True},
                ],
                "point_identities": [
                    {"collection": "canon", "point_id": "canon-1"}
                ],
            }
        )
    elif kind == "projection_consumer_unavailable":
        for snapshot in values.values():
            snapshot["state"]["database"].update(
                {
                    "canon_commits": copy.deepcopy(canon),
                    "outbox": {
                        "id": "outbox-1",
                        "payload": {"canon_id": "canon-1"},
                    },
                }
            )
        after["external"].update(
            {
                "projections": [
                    {"name": "vector", "identity": "canon-1", "converged": True}
                ],
                "projection_identities": [
                    {"projection": "vector", "identity": "canon-1"}
                ],
            }
        )
    elif kind == "minio_pre_canon_unavailable":
        for snapshot in values.values():
            snapshot["state"]["database"]["candidate"] = {
                "candidate_id": "candidate-1",
                "project_id": "project-1",
            }
        during["database"]["canon_commits"] = []
        after["database"].update(
            {
                "canon_commits": canon,
                "authoritative_identities": [authoritative_identity()],
            }
        )
    elif kind == "minio_post_canon_unavailable":
        for snapshot in values.values():
            snapshot["state"]["database"].update(
                {
                    "canon_commits": copy.deepcopy(canon),
                    "accepted_bundles": [{"bundle_id": "bundle-1"}],
                }
            )
        during["database"]["maintenance"] = {
            "natural_key": "world:project-1:canon-1",
            "attempt": 1,
            "lease_epoch": 8,
        }
        after["database"].update(
            {
                "maintenance": {
                    "natural_key": "world:project-1:canon-1",
                    "attempt": 2,
                    "lease_epoch": 9,
                },
                "authoritative_identities": [authoritative_identity()],
            }
        )
        during["external"]["artifact"] = {"key": "world/project-1/canon-1.json"}
        after["external"]["artifact"] = {"key": "world/project-1/canon-1.json"}
        after["barrier"]["residue_count"] = 0
    elif kind == "publisher_backend_unavailable":
        for snapshot in values.values():
            snapshot["state"]["database"]["canon_commits"] = copy.deepcopy(canon)
        during["database"]["job"] = {"id": "job-1", "owner_token": "owner-old"}
        after["database"].update(
            {
                "job": {"id": "job-1", "owner_token": "owner-new"},
                "jobs": [{"logical_key": "cover:fault-1"}],
                "attempts": [{"job_id": "job-1", "attempt": 1}],
                "receipts": [],
            }
        )
        after["api"]["stale_token_rejected"] = True
        after["external"].update(
            {"shared_path_readable": True, "orphan_residue_count": 0}
        )
    elif kind == "publisher_browser_unavailable":
        for snapshot in values.values():
            snapshot["state"]["database"].update(
                {
                    "canon_commits": copy.deepcopy(canon),
                    "job": {"id": "job-1", "status": "pending"},
                }
            )
        during["external"]["browser_heartbeat"] = {"healthy": False}
        after["external"]["browser_heartbeat"] = {"healthy": True}
        after["database"].update({"attempts": [], "receipts": []})
    else:
        before["database"]["job"] = {"id": "job-1", "status": "claimed"}
        during["database"]["job"] = {
            "id": "job-1",
            "status": "paused",
            "fence": "pre-mutation",
        }
        after["database"].update(
            {
                "job": {"id": "job-1", "status": "pending"},
                "receipts": [],
            }
        )
        after["api"].update(
            {
                "resume_actions": [
                    {
                        "action_id": "action-1",
                        "job_id": "job-1",
                        "fence": "pre-mutation",
                        "authenticated": True,
                        "idempotency_key": "resume-fault-1",
                    }
                ],
                "resume_replay": {
                    "action_id": "action-1",
                    "idempotency_key": "resume-fault-1",
                    "created_new_action": False,
                },
                "bypass_attempted": False,
            }
        )
    return values


@pytest.mark.parametrize("kind", FAULT_KINDS)
def test_empty_state_reports_required_paths_for_every_fault(kind: str) -> None:
    values = snapshots(kind)
    for snapshot in values.values():
        snapshot["state"] = {}

    violations = evidence.snapshot_violations(kind, values)

    assert violations
    assert any(".state.target is missing" in violation for violation in violations)
    assert any(".state.database is missing" in violation for violation in violations)


@pytest.mark.parametrize("kind", FAULT_KINDS)
def test_valid_snapshot_contract_derives_only_passing_assertions(kind: str) -> None:
    values = valid_snapshots(kind)

    assert evidence.snapshot_violations(kind, values) == []
    assertions = evidence.derive_assertions(kind, values)
    assert evidence.assertion_violations(kind, assertions) == []


def test_unknown_snapshot_and_state_keys_are_rejected() -> None:
    values = valid_snapshots("generation_worker_precommit_crash")
    values["before"]["operator_note"] = "not evidence"
    values["after"]["state"]["summary"] = {}

    violations = evidence.snapshot_violations(
        "generation_worker_precommit_crash", values
    )

    assert "before has unknown keys: ['operator_note']" in violations
    assert "after.state has unknown keys: ['summary']" in violations


def test_unknown_assertion_keys_are_rejected() -> None:
    kind = "generation_worker_precommit_crash"
    assertions = evidence.derive_assertions(kind, valid_snapshots(kind))
    assertions["operator_override"] = True

    assert evidence.assertion_violations(kind, assertions) == [
        f"{kind}.assertions has unknown keys: ['operator_override']"
    ]


def test_assertion_values_require_the_exact_expected_type() -> None:
    kind = "generation_worker_precommit_crash"
    assertions = evidence.derive_assertions(kind, valid_snapshots(kind))
    assertions["same_task_reclaimed"] = 1

    assert evidence.assertion_violations(kind, assertions) == [
        f"{kind}.same_task_reclaimed=1, expected=True"
    ]


def test_snapshot_boolean_cannot_be_substituted_with_an_integer() -> None:
    kind = "publisher_backend_unavailable"
    values = valid_snapshots(kind)
    values["after"]["state"]["api"]["stale_token_rejected"] = 1

    assert (
        "after.state.api.stale_token_rejected is not a boolean"
        in evidence.snapshot_violations(kind, values)
    )


@pytest.mark.parametrize(
    ("field", "replacement", "expected"),
    (
        ("fault_kind", "qdrant_unavailable", "after.fault_kind mismatch"),
        ("fault_id", "other-fault", "after.fault_id mismatch"),
        ("source_sha", "b" * 40, "after.source_sha mismatch"),
        ("stage", "during", "after.stage mismatch"),
    ),
)
def test_cross_fault_snapshot_identities_are_rejected(
    field: str, replacement: str, expected: str
) -> None:
    kind = "generation_worker_postcommit_crash"
    values = valid_snapshots(kind)
    values["after"][field] = replacement

    assert expected in evidence.snapshot_violations(kind, values)


def test_stable_identity_rejects_mutable_timestamp_substitution() -> None:
    kind = "generation_worker_postcommit_crash"
    values = valid_snapshots(kind)
    values["after"]["state"]["database"]["accepted_bundles"] = [
        {"bundle_id": "bundle-1", "updated_at": "2026-07-26T00:00:00Z"}
    ]

    violations = evidence.snapshot_violations(kind, values)

    assert (
        "after.state.database.accepted_bundles contains mutable identity key "
        "'updated_at'"
    ) in violations


def test_duplicate_natural_keys_fail_the_derived_contract() -> None:
    kind = "generation_worker_precommit_crash"
    values = valid_snapshots(kind)
    values["after"]["state"]["database"]["authoritative_identities"].append(
        authoritative_identity()
    )

    assertions = evidence.derive_assertions(kind, values)

    assert assertions["duplicate_authoritative_identities"] == 1
    assert evidence.assertion_violations(kind, assertions) == [
        f"{kind}.duplicate_authoritative_identities=1, expected=0"
    ]


def test_empty_stable_inventory_does_not_prove_identity_unchanged() -> None:
    kind = "qdrant_unavailable"
    values = valid_snapshots(kind)
    for snapshot in values.values():
        snapshot["state"]["database"]["canon_commits"] = []

    assertions = evidence.derive_assertions(kind, values)

    assert assertions["canon_identity_unchanged"] is False
    assert (
        f"{kind}.canon_identity_unchanged=False, expected=True"
        in evidence.assertion_violations(kind, assertions)
    )


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


@pytest.mark.parametrize("kind", FAULT_KINDS)
def test_false_derived_assertion_is_reported(kind: str) -> None:
    values = valid_snapshots(kind)
    if kind == "generation_worker_precommit_crash":
        values["after"]["state"]["database"]["task"]["lease_epoch"] = 4
    elif kind == "generation_worker_postcommit_crash":
        values["after"]["state"]["database"]["accepted_bundles"] = [
            {"bundle_id": "bundle-2"}
        ]
    elif kind == "qdrant_unavailable":
        values["after"]["state"]["external"]["projections"][0]["converged"] = False
    elif kind == "projection_consumer_unavailable":
        values["after"]["state"]["database"]["outbox"]["payload"] = {
            "canon_id": "canon-2"
        }
    elif kind == "minio_pre_canon_unavailable":
        values["after"]["state"]["database"]["candidate"]["candidate_id"] = (
            "candidate-2"
        )
    elif kind == "minio_post_canon_unavailable":
        values["after"]["state"]["barrier"]["residue_count"] = 1
    elif kind == "publisher_backend_unavailable":
        values["after"]["state"]["api"]["stale_token_rejected"] = False
    elif kind == "publisher_browser_unavailable":
        values["after"]["state"]["external"]["browser_heartbeat"]["healthy"] = False
    else:
        values["after"]["state"]["api"]["resume_replay"][
            "created_new_action"
        ] = True

    assertions = evidence.derive_assertions(kind, values)

    assert evidence.assertion_violations(kind, assertions)
