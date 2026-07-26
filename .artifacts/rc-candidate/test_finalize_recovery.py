from __future__ import annotations

import copy
import importlib.util
import json
import shutil
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


def volume_fingerprint(name: str, created_at: str) -> str:
    return evidence.stable_hash({"created_at": created_at, "name": name})


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
    fault_dir = (tmp_path / fault_id).resolve()
    fault_dir.mkdir(parents=True, exist_ok=True)
    volume_name = f"forwin-v5-recovery-{fault_id}-postgres-data"
    volume_created_at = "2026-07-22T11:58:30+00:00"
    volume_present = {
        "name": volume_name,
        "exists": True,
        "created_at": volume_created_at,
        "fingerprint": volume_fingerprint(volume_name, volume_created_at),
    }
    volume_absent = {"name": volume_name, "exists": False}
    run_identity = {
        "run_id": f"run-{fault_id}",
        "evidence_directory": str(fault_dir),
        "database_volume_name": volume_name,
    }
    publisher_event_identity: dict | None = None
    if kind in recovery.FAULT_CONTRACTS:
        endpoint = copy.deepcopy(
            snapshots["before"]["state"]["target"]["endpoint_identity"]
        )
        run_identity["run_id"] = endpoint["run_id"]
        endpoint["project_name"] = (
            f"forwin-v5-recovery-{run_identity['run_id']}"
        )
        if candidate is not None and candidate_path is not None:
            publisher_event_identity = {
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
            }
            endpoint.update(
                source_tree=publisher_event_identity["source_tree"],
                candidate_manifest_sha256=publisher_event_identity[
                    "candidate_manifest"
                ]["sha256"],
            )
            endpoint["api"]["image_id"] = publisher_event_identity[
                "runtime_image"
            ]["image_id"]
            endpoint["mcp"]["image_id"] = publisher_event_identity[
                "runtime_image"
            ]["image_id"]
            endpoint["database"]["image_id"] = publisher_event_identity[
                "dependency_images"
            ]["postgres"]["image_id"]
            for endpoint_key in ("qdrant", "minio"):
                if endpoint_key in endpoint:
                    endpoint[endpoint_key]["image_id"] = (
                        publisher_event_identity["dependency_images"][
                            endpoint_key
                        ]["image_id"]
                    )
        else:
            publisher_event_identity = {
                "source_sha": SOURCE_SHA,
                "source_tree": endpoint["source_tree"],
                "runtime_image": {
                    "image_id": endpoint["api"]["image_id"],
                },
                "browser_image": {
                    "image_id": "sha256:" + evidence.stable_hash(
                        {"kind": kind, "image": "publisher-browser"}
                    ),
                },
                "dependency_images": {
                    "postgres": {
                        "image_id": endpoint["database"]["image_id"],
                    },
                    "qdrant": (
                        {"image_id": endpoint["qdrant"]["image_id"]}
                        if "qdrant" in endpoint
                        else {}
                    ),
                    "minio": (
                        {"image_id": endpoint["minio"]["image_id"]}
                        if "minio" in endpoint
                        else {}
                    ),
                },
                "candidate_manifest": {
                    "sha256": endpoint["candidate_manifest_sha256"],
                },
            }
        endpoint["candidate_identity_sha256"] = evidence.stable_hash(
            {
                key: publisher_event_identity.get(key)
                for key in (
                    "source_sha",
                    "source_tree",
                    "runtime_image",
                    "browser_image",
                    "dependency_images",
                    "candidate_manifest",
                )
            }
        )
        endpoint["identity_sha256"] = evidence.stable_hash(
            {
                key: value
                for key, value in endpoint.items()
                if key != "identity_sha256"
            }
        )
        for stage in ("before", "during", "after"):
            snapshots[stage]["state"]["target"]["endpoint_identity"] = (
                copy.deepcopy(endpoint)
            )
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
        if publisher_event_identity is not None:
            identity = {
                **publisher_event_identity,
                **(
                    {
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
                    if candidate is not None
                    else {}
                ),
            }
        events = []
        previous = "0" * 64
        lifecycle = [
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
            *(
                [
                    (
                        "endpoints_bound",
                        "2026-07-22T11:59:01+00:00",
                        "",
                        "",
                    )
                ]
                if publisher_event_identity is not None else []
            ),
            *(
                [
                    (
                        "setup_service_held",
                        "2026-07-22T11:59:10+00:00",
                        "",
                        "",
                    ),
                    (
                        "setup_service_released",
                        "2026-07-22T11:59:20+00:00",
                        "",
                        "",
                    ),
                ]
                if kind == "publisher_backend_unavailable"
                else []
            ),
            *(
                [
                    (
                        "setup_service_held",
                        "2026-07-22T11:59:11+00:00",
                        "",
                        "",
                    )
                ]
                if kind in recovery.PUBLISHER_RISK_FAULTS
                else []
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
            *(
                [
                    (
                        "setup_service_discarded",
                        "2026-07-22T12:01:11+00:00",
                        "",
                        "",
                    )
                ]
                if kind in recovery.PUBLISHER_RISK_FAULTS
                else []
            ),
            (
                "destroyed",
                "2026-07-22T12:02:00+00:00",
                "",
                "",
            ),
        ]
        for action, recorded_at, time_field, time_value in lifecycle:
            event = {
                "schema_version": 2,
                "recorded_at": recorded_at,
                "action": action,
                "fault_id": report["fault_id"],
                "identity": identity,
                "run_identity": copy.deepcopy(run_identity),
                "previous_event_sha256": previous,
            }
            event["database_volume"] = copy.deepcopy(
                volume_absent
                if action in {"fresh_up_started", "destroyed"}
                else volume_present
            )
            if time_field:
                event.update(
                    fault_kind=kind,
                    **{time_field: time_value},
                )
                if contract["service"]:
                    event["service"] = contract["service"]
            if (
                action == "fresh_up_completed"
                and publisher_event_identity is not None
            ):
                event["sentinel"] = copy.deepcopy(endpoint["sentinel"])
                runtime_image_id = publisher_event_identity[
                    "runtime_image"
                ]["image_id"]
                browser_image_id = publisher_event_identity[
                    "browser_image"
                ]["image_id"]
                dependency_images = publisher_event_identity[
                    "dependency_images"
                ]
                service_image_ids = {
                    "forwin": runtime_image_id,
                    "forwin-mcp": runtime_image_id,
                    "generation-worker": runtime_image_id,
                    "minio": (
                        dependency_images.get("minio") or {}
                    ).get("image_id"),
                    "outbox-worker": runtime_image_id,
                    "postgres": dependency_images["postgres"]["image_id"],
                    "publisher-browser": browser_image_id,
                    "publisher-worker": runtime_image_id,
                    "qdrant": (
                        dependency_images.get("qdrant") or {}
                    ).get("image_id"),
                }
                fresh_services = {
                    service: {
                        "exists": True,
                        "running": True,
                        "container_id": f"{fault_id}-{service}",
                        "image_id": image_id
                        or "sha256:"
                        + evidence.stable_hash(
                            {
                                "fault_id": fault_id,
                                "service": service,
                                "role": "dependency-image",
                            }
                        ),
                    }
                    for service, image_id in service_image_ids.items()
                }
                endpoint_services = (
                    ("forwin", endpoint["api"]),
                    ("forwin-mcp", endpoint["mcp"]),
                    ("postgres", endpoint["database"]),
                )
                endpoint_services += tuple(
                    (name, endpoint[name])
                    for name in ("qdrant", "minio")
                    if name in endpoint
                )
                for service, published in endpoint_services:
                    fresh_services[service] = {
                        "exists": True,
                        "running": True,
                        "container_id": published["container_id"],
                        "image_id": published["image_id"],
                    }
                if kind in recovery.PUBLISHER_RISK_FAULTS:
                    terminal = snapshots["after"]["state"]["external"][
                        "browser_hold_terminal"
                    ]
                    fresh_services["publisher-browser"].update(
                        container_id=terminal["container_id"],
                        image_id=terminal["image_id"],
                    )
                if kind == "publisher_backend_unavailable":
                    fresh_services["publisher-worker"].update(
                        container_id="publisher-worker-boundary-container",
                    )
                event["after"] = {
                    "services": fresh_services
                }
            if action == "endpoints_bound":
                event["endpoint_identity"] = copy.deepcopy(endpoint)
            if (
                action
                in {
                "setup_service_held",
                "setup_service_discarded",
                }
                and kind in recovery.PUBLISHER_RISK_FAULTS
            ):
                terminal = snapshots["after"]["state"]["external"][
                    "browser_hold_terminal"
                ]
                event.update(
                    hold_id=terminal["hold_id"],
                    service="publisher-browser",
                    fault_kind=kind,
                    purpose="auxiliary",
                    requested_at=(
                        "2026-07-22T11:59:10+00:00"
                        if action == "setup_service_held"
                        else "2026-07-22T12:01:10+00:00"
                    ),
                )
                if action == "setup_service_held":
                    event["hold_time"] = event["recorded_at"]
                    event["before"] = {
                        "service": "publisher-browser",
                        "exists": True,
                        "running": True,
                        "container_id": terminal["container_id"],
                        "image_id": terminal["image_id"],
                    }
                    event["after"] = {
                        "service": "publisher-browser",
                        "exists": True,
                        "running": False,
                        "container_id": terminal["container_id"],
                        "image_id": terminal["image_id"],
                    }
                else:
                    event["discard_time"] = event["recorded_at"]
                    stopped = {
                        "service": "publisher-browser",
                        "exists": True,
                        "running": False,
                        "container_id": terminal["container_id"],
                        "image_id": terminal["image_id"],
                    }
                    event["before"] = copy.deepcopy(stopped)
                    event["after"] = copy.deepcopy(stopped)
            if (
                kind == "publisher_backend_unavailable"
                and action
                in {"setup_service_held", "setup_service_released"}
            ):
                held = {
                    "service": "publisher-worker",
                    "exists": True,
                    "running": action == "setup_service_released",
                    "container_id": "publisher-worker-boundary-container",
                    "image_id": publisher_event_identity[
                        "runtime_image"
                    ]["image_id"],
                }
                event.update(
                    hold_id=f"backend-fixture-{fault_id}",
                    service="publisher-worker",
                    fault_kind=kind,
                    purpose="pre-fault-boundary",
                    requested_at=event["recorded_at"],
                )
                if action == "setup_service_held":
                    event["hold_time"] = event["recorded_at"]
                    event["before"] = {**held, "running": True}
                    event["after"] = {**held, "running": False}
                else:
                    event["release_time"] = event["recorded_at"]
                    event["before"] = {**held, "running": False}
                    event["after"] = {
                        **held,
                        "running": True,
                        "probe": {"passed": True},
                    }
            if action == "destroyed":
                event["after"] = {"services": copy.deepcopy(DESTROY_SERVICES)}
                event["database_volume_before"] = copy.deepcopy(volume_present)
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


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("expected", None),
        ("expected", []),
        ("actual", "arbitrary display prose"),
        ("actual", {"anything": "goes"}),
    ),
)
def test_explanatory_prose_never_changes_pass(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    report = fault_report(tmp_path, "qdrant_unavailable")
    if value is None:
        report.pop(field)
    else:
        report[field] = value

    assert recovery.fault_report_violations(
        report,
        source_sha=SOURCE_SHA,
    ) == []


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


@pytest.mark.parametrize(
    ("mutation", "expected"),
    (
        ("absent-replay", "snapshot contract"),
        ("copied-boolean", "snapshot contract"),
        (
            "identity-drift",
            "report assertions do not match derived assertions",
        ),
        (
            "wrong-rowcount",
            "report assertions do not match derived assertions",
        ),
        (
            "wrong-final-state",
            "report assertions do not match derived assertions",
        ),
        (
            "wrong-claim-advancement",
            "report assertions do not match derived assertions",
        ),
    ),
)
def test_finalizer_independently_requires_same_event_replay(
    mutation: str,
    expected: str,
    tmp_path: Path,
) -> None:
    kind = "minio_post_canon_unavailable"
    report = fault_report(tmp_path, kind)
    after = next(item for item in report["artifacts"] if item["stage"] == "after")
    path = Path(after["path"])
    payload = json.loads(path.read_text(encoding="utf-8"))
    database = payload["state"]["database"]
    if mutation == "absent-replay":
        del database["phase3_replay_release"]
    elif mutation == "copied-boolean":
        database["replay_observed"] = True
    elif mutation == "identity-drift":
        database["phase3_replay_final"]["row_id"] = "other-row"
    elif mutation == "wrong-rowcount":
        database["phase3_replay_release"]["conditional_rowcount"] = 0
    elif mutation == "wrong-final-state":
        database["phase3_replay_final"]["status"] = "pending"
    else:
        database["phase3_replay_final"]["attempts"] += 1
    path.write_text(json.dumps(payload), encoding="utf-8")
    after["sha256"] = recovery.sha256_file(path)

    violations = recovery.fault_report_violations(
        report,
        source_sha=SOURCE_SHA,
    )

    assert any(
        f"{kind}.{expected}" in item
        for item in violations
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
                    "image_id": "sha256:" + "1" * 64,
                    "revision": SOURCE_SHA,
                },
                "publisher_browser": {
                    "tag": "browser:v1",
                    "image_id": "sha256:" + "2" * 64,
                    "revision": SOURCE_SHA,
                },
                "postgres": {
                    "tag": "postgres:v1",
                    "image_id": "sha256:" + "3" * 64,
                },
                "qdrant": {
                    "tag": "qdrant:v1",
                    "image_id": "sha256:" + "4" * 64,
                },
                "minio": {
                    "tag": "minio:v1",
                    "image_id": "sha256:" + "5" * 64,
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
        path = Path(report["event_log"]["path"]).resolve().parent / "report.json"
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


def test_fault_rejects_missing_database_volume_evidence(tmp_path: Path) -> None:
    report = fault_report(tmp_path, "qdrant_unavailable")
    events = recovery.load_verified_events(Path(report["event_log"]["path"]))
    for event in events:
        event.pop("database_volume", None)
        event.pop("database_volume_before", None)
    reseal_event_log(report, events)

    violations = recovery.fault_report_violations(
        report,
        source_sha=SOURCE_SHA,
    )

    assert "qdrant_unavailable.database volume lifecycle mismatch" in violations


def test_fault_rejects_forged_volume_creation_identity(tmp_path: Path) -> None:
    report = fault_report(tmp_path, "qdrant_unavailable")
    events = recovery.load_verified_events(Path(report["event_log"]["path"]))
    volume_name = events[1]["database_volume"]["name"]
    forged = {
        "name": volume_name,
        "exists": True,
        "created_at": "not-a-docker-created-at",
        "fingerprint": volume_fingerprint(
            volume_name,
            "not-a-docker-created-at",
        ),
    }
    for event in events:
        if event["action"] not in {"fresh_up_started", "destroyed"}:
            event["database_volume"] = copy.deepcopy(forged)
        if event["action"] == "destroyed":
            event["database_volume_before"] = copy.deepcopy(forged)
    reseal_event_log(report, events)

    violations = recovery.fault_report_violations(
        report,
        source_sha=SOURCE_SHA,
    )

    assert "qdrant_unavailable.database volume lifecycle mismatch" in violations


def test_recovery_manifest_rejects_reused_database_volume(
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
    source_events = recovery.load_verified_events(
        Path(source_report["event_log"]["path"])
    )
    source_run = source_events[0]["run_identity"]
    source_present = source_events[1]["database_volume"]
    source_absent = source_events[0]["database_volume"]

    def reuse_volume(report: dict) -> None:
        events = recovery.load_verified_events(Path(report["event_log"]["path"]))
        for event in events:
            event["run_identity"]["run_id"] = source_run["run_id"]
            event["run_identity"]["evidence_directory"] = source_run[
                "evidence_directory"
            ]
            event["run_identity"]["database_volume_name"] = source_run[
                "database_volume_name"
            ]
            event["database_volume"] = copy.deepcopy(
                source_absent
                if event["action"] in {"fresh_up_started", "destroyed"}
                else source_present
            )
            if event["action"] == "destroyed":
                event["database_volume_before"] = copy.deepcopy(source_present)
        reseal_event_log(report, events)

    rewrite_fault_report(manifest, target_kind, reuse_volume)

    violations = recovery.recovery_manifest_violations(
        manifest,
        source_sha=SOURCE_SHA,
    )

    assert (
        "recovery database volume name is reused: "
        "publisher_captcha, publisher_mfa"
    ) in violations
    assert (
        "recovery database volume fingerprint is reused: "
        "publisher_captcha, publisher_mfa"
    ) in violations
    assert (
        "recovery evidence directory is reused: "
        "publisher_captcha, publisher_mfa"
    ) in violations
    assert (
        "recovery run_id is reused: publisher_captcha, publisher_mfa"
        in violations
    )


def test_fault_rejects_noncanonical_run_directory(tmp_path: Path) -> None:
    report = fault_report(tmp_path, "qdrant_unavailable")
    events = recovery.load_verified_events(Path(report["event_log"]["path"]))
    fault_dir = Path(report["event_log"]["path"]).parent
    noncanonical = fault_dir / ".." / fault_dir.name
    for event in events:
        event["run_identity"]["evidence_directory"] = str(noncanonical)
    reseal_event_log(report, events)

    violations = recovery.fault_report_violations(
        report,
        source_sha=SOURCE_SHA,
    )

    assert "qdrant_unavailable.evidence directory is not canonical" in violations


def test_recovery_manifest_rejects_shared_snapshot_directory(
    tmp_path: Path,
) -> None:
    manifest = recovery_manifest(tmp_path)
    shared_dir = (tmp_path / "shared-snapshots").resolve()
    shared_dir.mkdir()
    kinds = ("qdrant_unavailable", "projection_consumer_unavailable")

    for kind in kinds:
        def move_snapshots(report: dict, *, fault_kind: str = kind) -> None:
            for artifact in report["artifacts"]:
                source = Path(artifact["path"])
                target = shared_dir / f"{fault_kind}-{artifact['stage']}.json"
                shutil.copyfile(source, target)
                artifact["path"] = str(target)
                artifact["sha256"] = recovery.sha256_file(target)

        rewrite_fault_report(manifest, kind, move_snapshots)

    violations = recovery.recovery_manifest_violations(
        manifest,
        source_sha=SOURCE_SHA,
    )

    assert "qdrant_unavailable.before artifact directory mismatch" in violations
    assert (
        "recovery artifact directory is reused: "
        "projection_consumer_unavailable, qdrant_unavailable"
    ) in violations


def test_recovery_manifest_rejects_report_directory_escape(
    tmp_path: Path,
) -> None:
    manifest = recovery_manifest(tmp_path)
    escaped_dir = (tmp_path / "escaped-reports").resolve()
    escaped_dir.mkdir()
    kinds = ("qdrant_unavailable", "projection_consumer_unavailable")
    for kind in kinds:
        item = manifest["faults"][kind]
        source = Path(item["path"])
        escaped = escaped_dir / f"{kind}.json"
        shutil.copyfile(source, escaped)
        item["path"] = str(escaped)
        item["sha256"] = recovery.sha256_file(escaped)

    violations = recovery.recovery_manifest_violations(
        manifest,
        source_sha=SOURCE_SHA,
    )

    assert "qdrant_unavailable.report directory mismatch" in violations
    assert (
        "recovery report directory is reused: "
        "projection_consumer_unavailable, qdrant_unavailable"
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
    fault_index = next(
        index
        for index, event in enumerate(events)
        if event["action"] == "fault_service_stopped"
    )
    recovery_index = next(
        index
        for index, event in enumerate(events)
        if event["action"] == "fault_service_recovered"
    )
    events[fault_index], events[recovery_index] = (
        events[recovery_index],
        events[fault_index],
    )
    reseal_event_log(report, events)

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


def test_fault_rejects_events_after_destroy(tmp_path: Path) -> None:
    report = fault_report(tmp_path, "publisher_captcha")
    events = recovery.load_verified_events(Path(report["event_log"]["path"]))
    after_destroy = copy.deepcopy(events[-1])
    after_destroy["action"] = "post_destroy_marker"
    after_destroy.pop("database_volume_before")
    after_destroy.pop("after")
    events.append(after_destroy)
    reseal_event_log(report, events)

    violations = recovery.fault_report_violations(
        report,
        source_sha=SOURCE_SHA,
    )

    assert "publisher_captcha.terminal destroy lifecycle mismatch" in violations


def setup_hold_event(
    template: dict,
    *,
    fault_kind: str,
    action: str,
    hold_id: str,
    service: str,
    requested_at: str,
    confirmed_at: str,
) -> dict:
    event = copy.deepcopy(template)
    event["action"] = action
    event["hold_id"] = hold_id
    event["service"] = service
    event["requested_at"] = requested_at
    event["recorded_at"] = confirmed_at
    event["fault_kind"] = fault_kind
    event["purpose"] = "auxiliary"
    event.pop("fault_time", None)
    event.pop("recovery_time", None)
    fresh_services = template["after"]["services"]
    container_id = fresh_services[service]["container_id"]
    image_id = fresh_services[service]["image_id"]
    if action == "setup_service_held":
        event["hold_time"] = confirmed_at
        event["before"] = {
            "service": service,
            "exists": True,
            "running": True,
            "container_id": container_id,
            "image_id": image_id,
        }
        event["after"] = {
            "service": service,
            "exists": True,
            "running": False,
            "container_id": container_id,
            "image_id": image_id,
        }
    elif action == "setup_service_released":
        event["release_time"] = confirmed_at
        event["before"] = {
            "service": service,
            "exists": True,
            "running": False,
            "container_id": container_id,
            "image_id": image_id,
        }
        event["after"] = {
            "service": service,
            "exists": True,
            "running": True,
            "container_id": container_id,
            "image_id": image_id,
            "probe": {"passed": True},
        }
    else:
        event["discard_time"] = confirmed_at
        event["before"] = {
            "service": service,
            "exists": True,
            "running": False,
            "container_id": container_id,
            "image_id": image_id,
        }
        event["after"] = {
            "service": service,
            "exists": True,
            "running": False,
            "container_id": container_id,
            "image_id": image_id,
        }
    return event


def balanced_hold_events(
    template: dict,
    *,
    prefix: str,
    fault_kind: str = "minio_pre_canon_unavailable",
) -> list[dict]:
    return [
        setup_hold_event(
            template,
            fault_kind=fault_kind,
            action="setup_service_held",
            hold_id=f"{prefix}-hold",
            service="outbox-worker",
            requested_at="2026-07-22T11:59:10+00:00",
            confirmed_at="2026-07-22T11:59:11+00:00",
        ),
        setup_hold_event(
            template,
            fault_kind=fault_kind,
            action="setup_service_released",
            hold_id=f"{prefix}-hold",
            service="outbox-worker",
            requested_at="2026-07-22T12:01:10+00:00",
            confirmed_at="2026-07-22T12:01:11+00:00",
        ),
    ]


def test_finalizer_allows_terminal_discard_of_typed_risk_auxiliary_hold(
    tmp_path: Path,
) -> None:
    report = fault_report(tmp_path, "publisher_captcha")

    assert recovery.fault_report_violations(
        report,
        source_sha=SOURCE_SHA,
    ) == []


@pytest.mark.parametrize(
    ("mutation", "expected"),
    (
        ("running-after", "setup discard stopped postcondition mismatch"),
        ("without-hold", "hold discard has no matching hold"),
        ("wrong-service", "hold discard does not match held service"),
    ),
)
def test_finalizer_rejects_invalid_setup_discard_lifecycle(
    mutation: str,
    expected: str,
    tmp_path: Path,
) -> None:
    report = fault_report(tmp_path, "publisher_captcha")
    events = recovery.load_verified_events(Path(report["event_log"]["path"]))
    held = next(
        event
        for event in events
        if event["action"] == "setup_service_held"
    )
    discarded = next(
        event
        for event in events
        if event["action"] == "setup_service_discarded"
    )
    if mutation == "running-after":
        discarded["after"]["running"] = True
    elif mutation == "without-hold":
        events.remove(held)
    else:
        discarded["service"] = "outbox-worker"
        discarded["before"]["service"] = "outbox-worker"
        discarded["after"]["service"] = "outbox-worker"
    reseal_event_log(report, events)

    violations = recovery.fault_report_violations(
        report,
        source_sha=SOURCE_SHA,
    )

    assert f"publisher_captcha.{expected}" in violations


@pytest.mark.parametrize(
    ("mutation", "expected"),
    (
        ("missing", "endpoint binding event count mismatch"),
        ("duplicate", "endpoint binding event count mismatch"),
        ("sentinel", "endpoint sentinel is not fresh-up bound"),
        ("snapshot", "endpoint identity is not event-bound"),
        (
            "fresh-container",
            "endpoint published container mismatch: forwin",
        ),
        ("candidate", "endpoint candidate identity mismatch"),
        ("malformed", "endpoint published container mismatch: forwin"),
    ),
)
@pytest.mark.parametrize(
    "kind",
    (
        "generation_worker_precommit_crash",
        "qdrant_unavailable",
        "minio_post_canon_unavailable",
        "publisher_backend_unavailable",
    ),
)
def test_finalizer_rejects_unbound_recovery_endpoint_identity(
    kind: str,
    mutation: str,
    expected: str,
    tmp_path: Path,
) -> None:
    report = fault_report(tmp_path, kind)
    events = recovery.load_verified_events(Path(report["event_log"]["path"]))
    endpoint = next(
        event for event in events if event["action"] == "endpoints_bound"
    )
    if mutation == "missing":
        events.remove(endpoint)
    elif mutation == "duplicate":
        events.insert(events.index(endpoint) + 1, copy.deepcopy(endpoint))
    elif mutation == "sentinel":
        fresh = next(
            event
            for event in events
            if event["action"] == "fresh_up_completed"
        )
        fresh["sentinel"]["sentinel_id"] = "f" * 64
    elif mutation == "snapshot":
        endpoint["endpoint_identity"]["run_id"] = "e" * 32
    elif mutation == "fresh-container":
        fresh = next(
            event
            for event in events
            if event["action"] == "fresh_up_completed"
        )
        fresh["after"]["services"]["forwin"]["container_id"] = (
            "different-container"
        )
    elif mutation == "candidate":
        endpoint["identity"]["source_tree"] = "f" * 40
    else:
        endpoint["endpoint_identity"]["api"] = []
    reseal_event_log(report, events)

    violations = recovery.fault_report_violations(
        report,
        source_sha=SOURCE_SHA,
    )

    assert f"{kind}.{expected}" in violations


def test_finalizer_rejects_typed_risk_discard_not_bound_to_after_snapshot(
    tmp_path: Path,
) -> None:
    report = fault_report(tmp_path, "publisher_mfa")
    events = recovery.load_verified_events(Path(report["event_log"]["path"]))
    discarded = next(
        event
        for event in events
        if event["action"] == "setup_service_discarded"
    )
    discarded["before"]["container_id"] = "different-browser-container"
    discarded["after"]["container_id"] = "different-browser-container"
    reseal_event_log(report, events)

    violations = recovery.fault_report_violations(
        report,
        source_sha=SOURCE_SHA,
    )

    assert (
        "publisher_mfa.typed-risk discard is not final-snapshot bound"
        in violations
    )


@pytest.mark.parametrize(
    ("mutation", "expected"),
    (
        ("purpose", "setup hold targets primary fault service"),
        ("discard", "pre-fault primary hold lifecycle mismatch"),
        ("order", "pre-fault primary hold order mismatch"),
        (
            "container",
            "setup hold terminal identity continuity mismatch",
        ),
        ("image", "setup hold terminal identity continuity mismatch"),
        ("duplicate-terminal", "hold release has no matching hold"),
    ),
)
def test_publisher_backend_primary_hold_is_narrowly_ordered_and_bound(
    mutation: str,
    expected: str,
    tmp_path: Path,
) -> None:
    kind = "publisher_backend_unavailable"
    report = fault_report(tmp_path, kind)
    events = recovery.load_verified_events(Path(report["event_log"]["path"]))
    held = next(
        event for event in events if event["action"] == "setup_service_held"
    )
    released = next(
        event
        for event in events
        if event["action"] == "setup_service_released"
    )
    if mutation == "purpose":
        held["purpose"] = released["purpose"] = "auxiliary"
    elif mutation == "discard":
        released["action"] = "setup_service_discarded"
        released["discard_time"] = released.pop("release_time")
        released["after"]["running"] = False
        released["after"].pop("probe")
    elif mutation == "order":
        fault = next(
            event
            for event in events
            if event["action"] == "fault_service_killed"
        )
        release_index = events.index(released)
        fault_index = events.index(fault)
        events[release_index], events[fault_index] = (
            events[fault_index],
            events[release_index],
        )
    elif mutation == "container":
        released["after"]["container_id"] = "different-container"
    elif mutation == "image":
        released["before"]["image_id"] = "sha256:" + "e" * 64
    else:
        events.insert(events.index(released) + 1, copy.deepcopy(released))
    reseal_event_log(report, events)

    violations = recovery.fault_report_violations(
        report,
        source_sha=SOURCE_SHA,
    )

    assert f"{kind}.{expected}" in violations


def test_finalizer_allows_balanced_setup_holds_around_primary_fault(
    tmp_path: Path,
) -> None:
    report = fault_report(tmp_path, "minio_pre_canon_unavailable")
    events = recovery.load_verified_events(Path(report["event_log"]["path"]))
    first_pair = balanced_hold_events(events[1], prefix="preapproval")
    second_pair = balanced_hold_events(events[1], prefix="replay")
    second_pair[0]["requested_at"] = "2026-07-22T12:01:20+00:00"
    second_pair[0]["hold_time"] = "2026-07-22T12:01:21+00:00"
    second_pair[0]["recorded_at"] = "2026-07-22T12:01:21+00:00"
    second_pair[1]["requested_at"] = "2026-07-22T12:01:30+00:00"
    second_pair[1]["release_time"] = "2026-07-22T12:01:31+00:00"
    second_pair[1]["recorded_at"] = "2026-07-22T12:01:31+00:00"
    reseal_event_log(
        report,
        [
            *events[:3],
            first_pair[0],
            events[3],
            events[4],
            first_pair[1],
            *second_pair,
            events[5],
        ],
    )

    assert recovery.fault_report_violations(
        report,
        source_sha=SOURCE_SHA,
    ) == []


@pytest.mark.parametrize(
    ("mutation", "expected"),
    (
        ("duplicate-id", "hold identity is duplicated or reused"),
        ("wrong-service", "hold release does not match held service"),
        ("wrong-id", "hold release has no matching hold"),
        ("unbalanced", "setup holds are not balanced"),
        ("same-service-overlap", "setup holds overlap for service"),
        ("primary-service", "setup hold targets primary fault service"),
    ),
)
def test_finalizer_rejects_invalid_setup_hold_lifecycles(
    mutation: str,
    expected: str,
    tmp_path: Path,
) -> None:
    report = fault_report(tmp_path, "minio_pre_canon_unavailable")
    events = recovery.load_verified_events(Path(report["event_log"]["path"]))
    pair = balanced_hold_events(events[1], prefix="first")
    extra = balanced_hold_events(events[1], prefix="second")
    inserted: list[dict]
    if mutation == "duplicate-id":
        extra[0]["hold_id"] = pair[0]["hold_id"]
        extra[1]["hold_id"] = pair[0]["hold_id"]
        inserted = [*pair, *extra]
    elif mutation == "wrong-service":
        pair[1]["service"] = "qdrant"
        inserted = pair
    elif mutation == "wrong-id":
        pair[1]["hold_id"] = "not-the-held-id"
        inserted = pair
    elif mutation == "unbalanced":
        inserted = pair[:1]
    elif mutation == "same-service-overlap":
        inserted = [pair[0], extra[0], pair[1], extra[1]]
    else:
        pair[0]["service"] = pair[1]["service"] = "minio"
        inserted = pair
    reseal_event_log(
        report,
        [*events[:3], *inserted, *events[3:]],
    )

    violations = recovery.fault_report_violations(
        report,
        source_sha=SOURCE_SHA,
    )

    assert f"minio_pre_canon_unavailable.{expected}" in violations


@pytest.mark.parametrize(
    ("mutation", "expected"),
    (
        ("identity", "setup hold event identities mismatch"),
        ("naive-time", "setup hold timestamp is invalid"),
        (
            "probe",
            "setup release readiness/probe postcondition mismatch",
        ),
        (
            "confirmation-order",
            "setup hold confirmation predates request",
        ),
        (
            "observation-service",
            "setup hold non-running postcondition mismatch",
        ),
        (
            "pair-time",
            "setup release predates hold confirmation",
        ),
        (
            "recorded-time",
            "setup hold confirmation follows recording",
        ),
        (
            "active-run-identity",
            "setup hold active-run identity mismatch",
        ),
        (
            "before-fresh-complete",
            "setup hold predates fresh-up completion",
        ),
    ),
)
def test_finalizer_strictly_binds_setup_hold_evidence(
    mutation: str,
    expected: str,
    tmp_path: Path,
) -> None:
    report = fault_report(tmp_path, "minio_pre_canon_unavailable")
    events = recovery.load_verified_events(Path(report["event_log"]["path"]))
    pair = balanced_hold_events(events[1], prefix="strict")
    if mutation == "identity":
        pair[0]["identity"] = {"source_sha": "different"}
    elif mutation == "naive-time":
        pair[0]["hold_time"] = "2026-07-22T11:59:11"
    elif mutation == "probe":
        pair[1]["after"]["probe"]["passed"] = False
    elif mutation == "confirmation-order":
        pair[0]["hold_time"] = "2026-07-22T11:59:09+00:00"
    elif mutation == "observation-service":
        pair[0]["after"]["service"] = "publisher-worker"
    elif mutation == "pair-time":
        pair[1]["requested_at"] = "2026-07-22T11:59:10+00:00"
    elif mutation == "recorded-time":
        pair[1]["recorded_at"] = "2026-07-22T12:01:10+00:00"
    elif mutation == "active-run-identity":
        for event in pair:
            event["before"]["container_id"] = "different-run-container"
            event["after"]["container_id"] = "different-run-container"
    else:
        pair[0]["requested_at"] = "2026-07-22T11:58:59+00:00"
    reseal_event_log(report, [*events[:3], *pair, *events[3:]])

    violations = recovery.fault_report_violations(
        report,
        source_sha=SOURCE_SHA,
    )

    assert f"minio_pre_canon_unavailable.{expected}" in violations


def test_finalizer_rejects_an_extra_primary_fault_pair(
    tmp_path: Path,
) -> None:
    report = fault_report(tmp_path, "minio_pre_canon_unavailable")
    events = recovery.load_verified_events(Path(report["event_log"]["path"]))
    extra_fault = copy.deepcopy(events[2])
    extra_fault["service"] = "qdrant"
    extra_recovery = copy.deepcopy(events[3])
    extra_recovery["service"] = "qdrant"
    reseal_event_log(
        report,
        [
            *events[:2],
            extra_fault,
            extra_recovery,
            *events[2:],
        ],
    )

    violations = recovery.fault_report_violations(
        report,
        source_sha=SOURCE_SHA,
    )

    assert (
        "minio_pre_canon_unavailable.primary fault/recovery cardinality mismatch"
        in violations
    )


@pytest.mark.parametrize("terminal", (True, False))
def test_finalizer_rejects_setup_blocked_and_requires_it_terminal(
    terminal: bool,
    tmp_path: Path,
) -> None:
    report = fault_report(tmp_path, "publisher_captcha")
    events = recovery.load_verified_events(Path(report["event_log"]["path"]))
    blocked = copy.deepcopy(events[2])
    blocked["action"] = "setup_blocked"
    blocked["failure_stage"] = "operator-abort"
    blocked["failure_reason"] = "preflight failed"
    blocked.pop("fault_kind", None)
    blocked.pop("fault_time", None)
    if terminal:
        rewritten = [*events[:2], blocked]
    else:
        rewritten = [*events[:2], blocked, *events[2:]]
    reseal_event_log(report, rewritten)

    violations = recovery.fault_report_violations(
        report,
        source_sha=SOURCE_SHA,
    )

    assert "publisher_captcha.setup_blocked cannot pass" in violations
    if not terminal:
        assert "publisher_captcha.setup_blocked is not terminal" in violations


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
