from __future__ import annotations

import copy
import importlib.util
import json
import sys
from collections.abc import Callable
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).with_name("finalize_recovery.py")
SPEC = importlib.util.spec_from_file_location("finalize_recovery", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
recovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recovery)

EVALUATOR_PATH = Path(__file__).with_name("recovery_evidence.py")
EVALUATOR_SPEC = importlib.util.spec_from_file_location(
    "finalizer_test_recovery_evidence", EVALUATOR_PATH
)
assert EVALUATOR_SPEC is not None and EVALUATOR_SPEC.loader is not None
evidence = importlib.util.module_from_spec(EVALUATOR_SPEC)
EVALUATOR_SPEC.loader.exec_module(evidence)
FIXTURE_PATH = Path(__file__).with_name("test_recovery_evidence.py")
FIXTURE_SPEC = importlib.util.spec_from_file_location(
    "finalizer_test_snapshot_fixtures", FIXTURE_PATH
)
assert FIXTURE_SPEC is not None and FIXTURE_SPEC.loader is not None
snapshot_fixtures = importlib.util.module_from_spec(FIXTURE_SPEC)
sys.modules[FIXTURE_SPEC.name] = snapshot_fixtures
FIXTURE_SPEC.loader.exec_module(snapshot_fixtures)

SOURCE_SHA = "9" * 40
DESTROY_SERVICES = {
    service: {"exists": False, "running": False}
    for service in (
        "forwin",
        "forwin-mcp",
        "generation-worker",
        "minio",
        "outbox-worker",
        "postgres",
        "publisher-browser",
        "publisher-worker",
        "qdrant",
    )
}
HARNESS_PATHS = {
    "controller": recovery.RECOVERY_CONTROLLER_PATH.resolve(),
    "compose_file": (recovery.ROOT / "docker-compose.yml").resolve(),
    "compose_override": recovery.RECOVERY_CONTROLLER_PATH.with_name(
        "docker-compose.recovery.yml"
    ).resolve(),
    "finalizer": recovery.RECOVERY_CONTROLLER_PATH.with_name(
        "finalize_v1.py"
    ).resolve(),
}
CANDIDATE_RELEASE_PATHS = frozenset(
    {
        *HARNESS_PATHS.values(),
        MODULE_PATH.resolve(),
        EVALUATOR_PATH.resolve(),
        recovery.GATE_HELPER_PATH.resolve(),
    }
)


def fault_report(
    tmp_path: Path,
    kind: str,
    *,
    candidate: dict | None = None,
    candidate_path: Path | None = None,
) -> dict:
    snapshot_fixtures.SOURCE_SHA = SOURCE_SHA
    snapshots = snapshot_fixtures.valid_snapshots(kind)
    fault_id = snapshots["before"]["fault_id"]
    fault_dir = tmp_path / fault_id
    fault_dir.mkdir(parents=True, exist_ok=True)
    artifacts = []
    for stage in ("before", "during", "after"):
        path = fault_dir / f"{stage}.json"
        path.write_text(json.dumps(snapshots[stage]), encoding="utf-8")
        artifacts.append(
            {
                "stage": stage,
                "path": str(path),
                "sha256": recovery.sha256_file(path),
            }
        )
    report = {
        "schema_version": 2,
        "fault_kind": kind,
        "fault_id": fault_id,
        "source_sha": SOURCE_SHA,
        "result": "pass",
        "fault_time": "2026-07-22T12:00:00+00:00",
        "recovery_time": "2026-07-22T12:01:00+00:00",
        "expected": ["consumer fails without changing Canon"],
        "actual": ["consumer recovered through its durable identity"],
        "replay_result": "pass",
        "assertions": evidence.derive_assertions(kind, snapshots),
        "artifacts": artifacts,
        "evaluator": {
            "path": str(EVALUATOR_PATH.resolve()),
            "sha256": recovery.sha256_file(EVALUATOR_PATH),
        },
    }
    contract = recovery.SERVICE_FAULTS.get(kind)
    if contract is None:
        contract = {
            "service": "",
            "fault_action": "fault_marked",
            "recovery_action": "recovery_marked",
            "fault_time_field": "fault_time",
        }
    else:
        contract = {**contract, "recovery_action": "fault_service_recovered"}
    if contract is not None:
        identity = (
            {
                "source_sha": SOURCE_SHA,
                "source_tree": (candidate.get("source") or {}).get("tree"),
                "runtime_image": (candidate.get("images") or {}).get("runtime"),
                "browser_image": (candidate.get("images") or {}).get(
                    "publisher_browser"
                ),
                "dependency_images": {
                    key: (candidate.get("images") or {}).get(key)
                    for key in ("postgres", "qdrant", "minio")
                },
                "candidate_manifest": {
                    "path": str(candidate_path),
                    "sha256": recovery.sha256_file(candidate_path),
                },
                "harness": {
                    key: {
                        "path": str(path),
                        "sha256": recovery.sha256_file(path),
                    }
                    for key, path in HARNESS_PATHS.items()
                },
                "docker": {
                    "context": "desktop-linux",
                    "endpoint": "unix:///tmp/docker.sock",
                    "daemon_id": "daemon-1",
                },
            }
            if candidate is not None and candidate_path is not None
            else {"source_sha": SOURCE_SHA}
        )
        events = []
        previous = "0" * 64
        for action, recorded_at, time_field, time_value in (
            (
                "fresh_up_started",
                "2026-07-22T11:58:00+00:00",
                "",
                "",
            ),
            (
                "fresh_up_completed",
                "2026-07-22T11:59:00+00:00",
                "",
                "",
            ),
            (
                contract["fault_action"],
                report["fault_time"],
                contract["fault_time_field"],
                report["fault_time"],
            ),
            (
                contract["recovery_action"],
                report["recovery_time"],
                "recovery_time",
                report["recovery_time"],
            ),
            (
                "destroyed",
                "2026-07-22T12:02:00+00:00",
                "",
                "",
            ),
        ):
            event = {
                "schema_version": 2,
                "recorded_at": recorded_at,
                "action": action,
                "fault_id": report["fault_id"],
                "identity": identity,
                "previous_event_sha256": previous,
            }
            if time_field:
                event.update(
                    fault_kind=kind,
                    **{time_field: time_value},
                )
                if contract["service"]:
                    event["service"] = contract["service"]
            if action == "destroyed":
                event["after"] = {"services": copy.deepcopy(DESTROY_SERVICES)}
            event["event_sha256"] = recovery.event_hash(event)
            previous = event["event_sha256"]
            events.append(event)
        event_path = fault_dir / "events.jsonl"
        event_path.write_text(
            "\n".join(json.dumps(item, sort_keys=True) for item in events) + "\n",
            encoding="utf-8",
        )
        report["event_log"] = {
            "path": str(event_path),
            "sha256": recovery.sha256_file(event_path),
            "event_count": len(events),
            "chain_head": previous,
        }
    return report


def test_every_fault_contract_accepts_complete_evidence(tmp_path: Path) -> None:
    for kind in recovery.FAULT_CONTRACTS:
        assert recovery.fault_report_violations(
            fault_report(tmp_path, kind),
            source_sha=SOURCE_SHA,
        ) == []


def test_publisher_backend_fault_requires_same_job_reclaim(tmp_path: Path) -> None:
    report = fault_report(tmp_path, "publisher_backend_unavailable")
    report["assertions"]["same_job_reclaimed"] = False

    violations = recovery.fault_report_violations(
        report,
        source_sha=SOURCE_SHA,
    )

    assert (
        "publisher_backend_unavailable.report assertions do not match derived assertions"
        in violations
    )


def test_report_assertion_types_must_match_derived_values(tmp_path: Path) -> None:
    report = fault_report(tmp_path, "publisher_backend_unavailable")
    report["assertions"]["same_job_reclaimed"] = 1

    violations = recovery.fault_report_violations(
        report,
        source_sha=SOURCE_SHA,
    )

    assert (
        "publisher_backend_unavailable.report assertions do not match derived assertions"
        in violations
    )


@pytest.mark.parametrize("kind", tuple(recovery.FAULT_CONTRACTS))
def test_empty_snapshot_state_is_rejected(
    tmp_path: Path,
    kind: str,
) -> None:
    report = fault_report(tmp_path, kind)
    during = next(item for item in report["artifacts"] if item["stage"] == "during")
    path = Path(during["path"])
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["state"] = {}
    path.write_text(json.dumps(payload), encoding="utf-8")
    during["sha256"] = recovery.sha256_file(path)

    violations = recovery.fault_report_violations(report, source_sha=SOURCE_SHA)

    assert any("snapshot contract" in item for item in violations)


def test_modified_snapshot_cannot_reuse_passing_report_assertions(
    tmp_path: Path,
) -> None:
    report = fault_report(tmp_path, "qdrant_unavailable")
    after = next(item for item in report["artifacts"] if item["stage"] == "after")
    path = Path(after["path"])
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["state"]["database"]["outbox"]["attempt"] = 0
    path.write_text(json.dumps(payload), encoding="utf-8")
    after["sha256"] = recovery.sha256_file(path)

    violations = recovery.fault_report_violations(report, source_sha=SOURCE_SHA)

    assert (
        "qdrant_unavailable.report assertions do not match derived assertions"
        in violations
    )


def test_recovery_event_log_rejects_tampered_chain(tmp_path: Path) -> None:
    path = tmp_path / "stack-events.jsonl"
    first = {
        "schema_version": 2,
        "recorded_at": "2026-07-22T12:00:00+00:00",
        "action": "fault_service_stopped",
        "fault_id": "qdrant-1",
        "service": "qdrant",
        "previous_event_sha256": "0" * 64,
    }
    first["event_sha256"] = recovery.event_hash(first)
    second = {
        "schema_version": 2,
        "recorded_at": "2026-07-22T12:01:00+00:00",
        "action": "fault_service_recovered",
        "fault_id": "qdrant-1",
        "service": "qdrant",
        "previous_event_sha256": first["event_sha256"],
    }
    second["event_sha256"] = recovery.event_hash(second)
    path.write_text(
        "\n".join(json.dumps(item, sort_keys=True) for item in (first, second))
        + "\n",
        encoding="utf-8",
    )
    assert recovery.load_verified_events(path) == [first, second]

    first["service"] = "minio"
    path.write_text(
        "\n".join(json.dumps(item, sort_keys=True) for item in (first, second))
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(recovery.RecoveryEvidenceError, match="event hash mismatch"):
        recovery.load_verified_events(path)


def recovery_manifest(
    tmp_path: Path,
    *,
    candidate_path: Path | None = None,
) -> dict:
    if candidate_path is None:
        candidate_path = tmp_path / "candidate.json"
        candidate = {
            "source": {"sha": SOURCE_SHA, "tree": "8" * 40},
            "images": {
                "runtime": {
                    "tag": "runtime:v1",
                    "image_id": "runtime-id",
                    "revision": SOURCE_SHA,
                },
                "publisher_browser": {
                    "tag": "browser:v1",
                    "image_id": "browser-id",
                    "revision": SOURCE_SHA,
                },
                "postgres": {
                    "tag": "postgres:v1",
                    "image_id": "postgres-id",
                },
                "qdrant": {
                    "tag": "qdrant:v1",
                    "image_id": "qdrant-id",
                },
                "minio": {
                    "tag": "minio:v1",
                    "image_id": "minio-id",
                },
            },
            "release_harness": {
                "files": [
                    {
                        "path": path.relative_to(recovery.ROOT).as_posix(),
                        "sha256": recovery.sha256_file(path),
                    }
                    for path in CANDIDATE_RELEASE_PATHS
                ]
            },
        }
        candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
    else:
        candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    reports = {
        kind: fault_report(
            tmp_path,
            kind,
            candidate=candidate,
            candidate_path=candidate_path,
        )
        for kind in recovery.FAULT_CONTRACTS
    }
    fault_refs = {}
    for kind, report in reports.items():
        path = tmp_path / f"{kind}-report.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        fault_refs[kind] = {
            "path": str(path),
            "sha256": recovery.sha256_file(path),
            "fault_id": report["fault_id"],
            "result": "pass",
            "evaluator": report["evaluator"],
        }
    manifest = {
        "schema_version": 2,
        "source_sha": SOURCE_SHA,
        "result": "pass",
        "violations": [],
        "identity": {
            "source_sha": SOURCE_SHA,
            "source_tree": candidate["source"]["tree"],
            "runtime_image": candidate["images"]["runtime"],
            "browser_image": candidate["images"]["publisher_browser"],
            "rc_manifest": {
                "path": str(candidate_path),
                "sha256": recovery.sha256_file(candidate_path),
            },
        },
        "faults": fault_refs,
        "auditor": {
            "path": str(MODULE_PATH),
            "sha256": recovery.sha256_file(MODULE_PATH),
            "recovery_controller_path": str(recovery.RECOVERY_CONTROLLER_PATH),
            "recovery_controller_sha256": recovery.sha256_file(
                recovery.RECOVERY_CONTROLLER_PATH
            ),
            "gate_helper_path": str(recovery.GATE_HELPER_PATH),
            "gate_helper_sha256": recovery.sha256_file(recovery.GATE_HELPER_PATH),
        },
        "evaluator": {
            "path": str(EVALUATOR_PATH.resolve()),
            "sha256": recovery.sha256_file(EVALUATOR_PATH),
        },
    }
    report_path = tmp_path / "final-report.md"
    recovery.atomic_write(report_path, recovery.final_report(manifest))
    manifest["final_report"] = {
        "path": str(report_path),
        "sha256": recovery.sha256_file(report_path),
    }
    return manifest


def reseal_event_log(report: dict, events: list[dict]) -> None:
    previous = "0" * 64
    for event in events:
        event["previous_event_sha256"] = previous
        event["event_sha256"] = recovery.event_hash(event)
        previous = event["event_sha256"]
    path = Path(report["event_log"]["path"])
    path.write_text(
        "\n".join(json.dumps(event, sort_keys=True) for event in events) + "\n",
        encoding="utf-8",
    )
    report["event_log"].update(
        sha256=recovery.sha256_file(path),
        event_count=len(events),
        chain_head=previous,
    )


def rewrite_fault_report(
    manifest: dict,
    kind: str,
    update: Callable[[dict], None],
) -> dict:
    item = manifest["faults"][kind]
    path = Path(item["path"])
    report = json.loads(path.read_text(encoding="utf-8"))
    update(report)
    path.write_text(json.dumps(report), encoding="utf-8")
    item["fault_id"] = report["fault_id"]
    item["event_log"] = report.get("event_log")
    item["sha256"] = recovery.sha256_file(path)
    return report


def test_complete_recovery_manifest_revalidates_all_faults(tmp_path: Path) -> None:
    manifest = recovery_manifest(tmp_path)
    assert recovery.recovery_manifest_violations(
        manifest,
        source_sha=SOURCE_SHA,
    ) == []


def test_recovery_manifest_requires_every_independent_fault(tmp_path: Path) -> None:
    manifest = recovery_manifest(tmp_path)
    manifest["faults"].pop("publisher_mfa")
    violations = recovery.recovery_manifest_violations(
        manifest,
        source_sha=SOURCE_SHA,
    )
    assert "recovery fault identities mismatch" in violations


def test_recovery_manifest_rejects_reused_fault_id(tmp_path: Path) -> None:
    manifest = recovery_manifest(tmp_path)
    reused_id = manifest["faults"]["publisher_captcha"]["fault_id"]

    def reuse_fault_id(report: dict) -> None:
        report["fault_id"] = reused_id
        for artifact in report["artifacts"]:
            path = Path(artifact["path"])
            snapshot = json.loads(path.read_text(encoding="utf-8"))
            snapshot["fault_id"] = reused_id
            path.write_text(json.dumps(snapshot), encoding="utf-8")
            artifact["sha256"] = recovery.sha256_file(path)

    rewrite_fault_report(manifest, "publisher_mfa", reuse_fault_id)

    violations = recovery.recovery_manifest_violations(
        manifest,
        source_sha=SOURCE_SHA,
    )

    assert (
        f"recovery fault_id is reused: {reused_id} "
        "(publisher_captcha, publisher_mfa)"
    ) in violations


def test_recovery_manifest_rejects_shared_service_event_log(tmp_path: Path) -> None:
    manifest = recovery_manifest(tmp_path)
    kinds = ("qdrant_unavailable", "projection_consumer_unavailable")
    events = []
    for kind in kinds:
        report_path = Path(manifest["faults"][kind]["path"])
        report = json.loads(report_path.read_text(encoding="utf-8"))
        events.extend(
            json.loads(line)
            for line in Path(report["event_log"]["path"])
            .read_text(encoding="utf-8")
            .splitlines()
        )
    previous = "0" * 64
    for event in events:
        event["previous_event_sha256"] = previous
        event["event_sha256"] = recovery.event_hash(event)
        previous = event["event_sha256"]
    shared_path = tmp_path / "shared-service-events.jsonl"
    shared_path.write_text(
        "\n".join(json.dumps(event, sort_keys=True) for event in events) + "\n",
        encoding="utf-8",
    )
    shared_identity = {
        "path": str(shared_path),
        "sha256": recovery.sha256_file(shared_path),
        "event_count": len(events),
        "chain_head": previous,
    }
    for kind in kinds:
        rewrite_fault_report(
            manifest,
            kind,
            lambda report: report.update(event_log=shared_identity),
        )

    violations = recovery.recovery_manifest_violations(
        manifest,
        source_sha=SOURCE_SHA,
    )

    assert (
        "recovery service event log is reused: "
        "projection_consumer_unavailable, qdrant_unavailable"
    ) in violations


def test_recovery_manifest_rejects_shared_risk_event_chain(
    tmp_path: Path,
) -> None:
    manifest = recovery_manifest(tmp_path)
    source_kind = "publisher_captcha"
    target_kind = "publisher_mfa"
    source_report = json.loads(
        Path(manifest["faults"][source_kind]["path"]).read_text(
            encoding="utf-8"
        )
    )
    rewrite_fault_report(
        manifest,
        target_kind,
        lambda report: report.update(event_log=source_report["event_log"]),
    )

    violations = recovery.recovery_manifest_violations(
        manifest,
        source_sha=SOURCE_SHA,
    )

    assert (
        "recovery service event log is reused: "
        "publisher_captcha, publisher_mfa"
    ) in violations


def test_recovery_manifest_rejects_auditor_drift(tmp_path: Path) -> None:
    manifest = recovery_manifest(tmp_path)
    manifest["auditor"]["sha256"] = "0" * 64
    violations = recovery.recovery_manifest_violations(
        manifest,
        source_sha=SOURCE_SHA,
    )
    assert "recovery auditor artifact hash mismatch: path" in violations


def test_recovery_manifest_rejects_forged_report_and_updated_hash(
    tmp_path: Path,
) -> None:
    manifest = recovery_manifest(tmp_path)
    report_path = tmp_path / "final-report.md"
    report_path.write_text("# Forged pass\n", encoding="utf-8")
    manifest["final_report"]["sha256"] = recovery.sha256_file(report_path)

    violations = recovery.recovery_manifest_violations(
        manifest,
        source_sha=SOURCE_SHA,
    )

    assert "recovery final report content mismatch" in violations


def test_every_fault_requires_its_own_event_log(tmp_path: Path) -> None:
    report = fault_report(tmp_path, "qdrant_unavailable")
    report.pop("event_log")
    violations = recovery.fault_report_violations(
        report,
        source_sha=SOURCE_SHA,
    )
    assert "qdrant_unavailable.independent event log is missing" in violations


def test_service_fault_requires_fresh_stack_lifecycle(tmp_path: Path) -> None:
    report = fault_report(tmp_path, "qdrant_unavailable")
    events = recovery.load_verified_events(Path(report["event_log"]["path"]))
    reseal_event_log(report, events[2:])

    violations = recovery.fault_report_violations(
        report,
        source_sha=SOURCE_SHA,
    )

    assert "qdrant_unavailable.fresh stack lifecycle mismatch" in violations


def test_service_fault_requires_fault_before_recovery(tmp_path: Path) -> None:
    report = fault_report(tmp_path, "qdrant_unavailable")
    events = recovery.load_verified_events(Path(report["event_log"]["path"]))
    reseal_event_log(report, [*events[:2], events[3], events[2]])

    violations = recovery.fault_report_violations(
        report,
        source_sha=SOURCE_SHA,
    )

    assert "qdrant_unavailable.service event order mismatch" in violations


def test_fault_requires_terminal_destroy(tmp_path: Path) -> None:
    report = fault_report(tmp_path, "publisher_captcha")
    events = recovery.load_verified_events(Path(report["event_log"]["path"]))
    reseal_event_log(report, events[:-1])

    violations = recovery.fault_report_violations(
        report,
        source_sha=SOURCE_SHA,
    )

    assert "publisher_captcha.terminal destroy lifecycle mismatch" in violations


def test_destroy_requires_exact_absent_service_inventory(tmp_path: Path) -> None:
    report = fault_report(tmp_path, "publisher_mfa")
    events = recovery.load_verified_events(Path(report["event_log"]["path"]))
    events[-1]["after"]["services"].pop("qdrant")
    reseal_event_log(report, events)

    violations = recovery.fault_report_violations(
        report,
        source_sha=SOURCE_SHA,
    )

    assert "publisher_mfa.destroyed service inventory mismatch" in violations


def test_fault_snapshot_identity_cannot_be_detached(tmp_path: Path) -> None:
    report = fault_report(tmp_path, "qdrant_unavailable")
    before = next(item for item in report["artifacts"] if item["stage"] == "before")
    path = Path(before["path"])
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["fault_id"] = "different-fault"
    path.write_text(json.dumps(payload), encoding="utf-8")
    before["sha256"] = recovery.sha256_file(path)

    violations = recovery.fault_report_violations(
        report,
        source_sha=SOURCE_SHA,
    )

    assert (
        "qdrant_unavailable.snapshot contract: during.fault_id mismatch"
        in violations
    )
