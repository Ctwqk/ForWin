from __future__ import annotations

import importlib.util
import json
from collections.abc import Callable
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).with_name("finalize_recovery.py")
SPEC = importlib.util.spec_from_file_location("finalize_recovery", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
recovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recovery)

SOURCE_SHA = "9" * 40
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
    fault_id = f"{kind}-1"
    artifacts = []
    for stage in ("before", "during", "after"):
        path = tmp_path / f"{kind}-{stage}.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "source_sha": SOURCE_SHA,
                    "fault_kind": kind,
                    "fault_id": fault_id,
                    "stage": stage,
                    "state": {},
                }
            ),
            encoding="utf-8",
        )
        artifacts.append(
            {
                "stage": stage,
                "path": str(path),
                "sha256": recovery.sha256_file(path),
            }
        )
    report = {
        "schema_version": 1,
        "fault_kind": kind,
        "fault_id": fault_id,
        "source_sha": SOURCE_SHA,
        "result": "pass",
        "fault_time": "2026-07-22T12:00:00+00:00",
        "recovery_time": "2026-07-22T12:01:00+00:00",
        "expected": ["consumer fails without changing Canon"],
        "actual": ["consumer recovered through its durable identity"],
        "replay_result": "pass",
        "assertions": dict(recovery.FAULT_CONTRACTS[kind]),
        "artifacts": artifacts,
    }
    contract = recovery.SERVICE_FAULTS.get(kind)
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
                "fault_service_recovered",
                report["recovery_time"],
                "recovery_time",
                report["recovery_time"],
            ),
        ):
            event = {
                "schema_version": 1,
                "recorded_at": recorded_at,
                "action": action,
                "identity": identity,
                "previous_event_sha256": previous,
            }
            if time_field:
                event.update(
                    fault_id=report["fault_id"],
                    service=contract["service"],
                    **{time_field: time_value},
                )
            event["event_sha256"] = recovery.event_hash(event)
            previous = event["event_sha256"]
            events.append(event)
        event_path = tmp_path / f"{kind}-events.jsonl"
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
        "publisher_backend_unavailable.same_job_reclaimed=False, expected=True"
        in violations
    )


def test_recovery_event_log_rejects_tampered_chain(tmp_path: Path) -> None:
    path = tmp_path / "stack-events.jsonl"
    first = {
        "schema_version": 1,
        "recorded_at": "2026-07-22T12:00:00+00:00",
        "action": "fault_service_stopped",
        "fault_id": "qdrant-1",
        "service": "qdrant",
        "previous_event_sha256": "0" * 64,
    }
    first["event_sha256"] = recovery.event_hash(first)
    second = {
        "schema_version": 1,
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
        }
    manifest = {
        "schema_version": 1,
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
        "recovery fault_id is reused: publisher_captcha-1 "
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


def test_service_fault_requires_its_own_event_log(tmp_path: Path) -> None:
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

    assert "qdrant_unavailable.before artifact identity mismatch" in violations
