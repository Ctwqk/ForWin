from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).parent
MODULE_PATH = ROOT / "finalize_v1.py"
SPEC = importlib.util.spec_from_file_location("v1_finalizer", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
v1 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(v1)

SOURCE_SHA = "a" * 40
RUNTIME_IMAGE_ID = "sha256:" + "1" * 64
BROWSER_IMAGE_ID = "sha256:" + "2" * 64
DEPENDENCY_IMAGE_IDS = {
    "postgres": "sha256:" + "3" * 64,
    "qdrant": "sha256:" + "4" * 64,
    "minio": "sha256:" + "5" * 64,
}


def harness_paths() -> dict[str, Path]:
    return {
        "controller": v1.CONTROLLER_PATH,
        "compose_file": v1.ROOT / "docker-compose.yml",
        "compose_override": v1.CONTROLLER_PATH.with_name(
            "docker-compose.recovery.yml"
        ),
        "candidate_mcp_call": v1.CONTROLLER_PATH.with_name(
            "candidate_mcp_call.py"
        ),
        "http_auth": v1.CONTROLLER_PATH.with_name(
            "release_http_auth.py"
        ),
        "finalizer": MODULE_PATH,
    }


def candidate(path: Path) -> dict:
    payload = {
        "collected_at": "2026-07-22T11:59:00+00:00",
        "source": {"sha": SOURCE_SHA, "tree": "b" * 40},
        "images": {
            "runtime": {
                "tag": "forwin-runtime:v1",
                "image_id": RUNTIME_IMAGE_ID,
                "revision": SOURCE_SHA,
            },
            "publisher_browser": {
                "tag": "forwin-browser:v1",
                "image_id": BROWSER_IMAGE_ID,
                "revision": SOURCE_SHA,
            },
            **{
                service: {
                    "tag": f"{service}:v1",
                    "image_id": image_id,
                }
                for service, image_id in DEPENDENCY_IMAGE_IDS.items()
            },
        },
        "release_harness": {
            "files": [
                {
                    "path": artifact.relative_to(v1.ROOT).as_posix(),
                    "sha256": v1.sha256_file(artifact),
                }
                for artifact in harness_paths().values()
            ]
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def stack_snapshot() -> dict:
    runtime = {
        service: {
            "service": service,
            "exists": True,
            "running": True,
            "health": "healthy" if service in {"forwin", "forwin-mcp"} else "",
            "image_id": RUNTIME_IMAGE_ID,
            "probe": {"passed": True, "exit_code": 0},
        }
        for service in v1.EXPECTED_RUNTIME_SERVICES
    }
    dependencies = {
        service: {
            "service": service,
            "exists": True,
            "running": True,
            "health": "healthy",
            "image_id": DEPENDENCY_IMAGE_IDS[service],
            "probe": {"passed": True, "exit_code": 0},
        }
        for service in v1.EXPECTED_DEPENDENCY_SERVICES
    }
    browser = {
        "publisher-browser": {
            "service": "publisher-browser",
            "exists": True,
            "running": True,
            "health": "healthy",
            "image_id": BROWSER_IMAGE_ID,
            "probe": {"passed": True, "exit_code": 0},
        }
    }
    runtime["forwin-mcp"]["probe"]["helper_sha256"] = v1.sha256_file(
        harness_paths()["candidate_mcp_call"]
    )
    return {"services": {**runtime, **dependencies, **browser}}


def event_identity(candidate_path: Path) -> dict:
    candidate_payload = json.loads(candidate_path.read_text(encoding="utf-8"))
    return {
        "source_sha": SOURCE_SHA,
        "source_tree": candidate_payload["source"]["tree"],
        "docker": {
            "context": "desktop-linux",
            "endpoint": "unix:///tmp/docker.sock",
            "daemon_id": "daemon-1",
        },
        "candidate_manifest": {
            "path": str(candidate_path),
            "sha256": v1.sha256_file(candidate_path),
        },
        "harness": {
            key: {
                "path": str(path.resolve()),
                "sha256": v1.sha256_file(path.resolve()),
            }
            for key, path in harness_paths().items()
        },
    }


def event_log(path: Path, candidate_path: Path) -> None:
    stale_log_path = path.parent / "stale-schema-generation-worker.log"
    stale_log_path.write_text(
        "SchemaRevisionMismatchError: [FORWIN_SCHEMA_REVISION_MISMATCH] "
        "ForWin database schema is v1_stale_revision, "
        "expected 0001_v5_baseline.\n",
        encoding="utf-8",
    )
    events = [
        {
            "schema_version": 1,
            "recorded_at": "2026-07-22T12:00:00+00:00",
            "action": "v1_fresh_up_started",
            "run_id": "run-1",
            "previous_event_sha256": "0" * 64,
            "identity": event_identity(candidate_path),
        },
        {
            "schema_version": 1,
            "recorded_at": "2026-07-22T12:05:00+00:00",
            "action": "v1_preflight_completed",
            "run_id": "run-1",
            "previous_event_sha256": "",
            "identity": event_identity(candidate_path),
            "migration_cycle": {
                "steps": [
                    {"name": "upgrade_head_initial", "exit_code": 0},
                    {"name": "alembic_check", "exit_code": 0},
                    {"name": "downgrade_base", "exit_code": 0},
                    {"name": "upgrade_head_final", "exit_code": 0},
                ],
                "final_revision": "0001_v5_baseline",
            },
            "stale_schema_failfast": {
                "role": "generation-worker",
                "injected_revision": "v1_stale_revision",
                "startup_exit_code": 1,
                "error_code": "FORWIN_SCHEMA_REVISION_MISMATCH",
                "expected_error_observed": True,
                "startup_log": {
                    "path": str(stale_log_path),
                    "sha256": v1.sha256_file(stale_log_path),
                },
                "task_state_before": {"total": 0, "leased": 0},
                "task_state_after": {"total": 0, "leased": 0},
                "restored_revision": "0001_v5_baseline",
                "post_restore_exit_code": 0,
            },
            "embedding": {
                "backend": "gateway",
                "required": True,
                "base_url": "http://10.0.0.150:8080",
                "peer_ip": "10.0.0.150",
                "model": "all-MiniLM-L6-v2",
                "configured_dims": 384,
                "metadata_dims": 384,
                "vector_count": 1,
                "vector_dims": [384],
            },
            "after": stack_snapshot(),
        },
        {
            "schema_version": 1,
            "recorded_at": "2026-07-22T12:06:00+00:00",
            "action": "destroyed",
            "run_id": "run-1",
            "previous_event_sha256": "",
            "identity": event_identity(candidate_path),
            "before": stack_snapshot(),
            "after": {
                "services": {
                    service: {
                        "service": service,
                        "exists": False,
                    }
                    for service in v1.EXPECTED_SERVICES
                }
            },
        },
    ]
    previous = "0" * 64
    for event in events:
        event["previous_event_sha256"] = previous
        event["event_sha256"] = v1.event_hash(event)
        previous = event["event_sha256"]
    path.write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
        encoding="utf-8",
    )


def valid_fixture(tmp_path: Path) -> tuple[dict, Path, Path]:
    candidate_path = tmp_path / "candidate.json"
    payload = candidate(candidate_path)
    events_path = tmp_path / "stack-events.jsonl"
    event_log(events_path, candidate_path)
    return payload, candidate_path, events_path


def test_complete_v1_preflight_passes(tmp_path: Path) -> None:
    payload, candidate_path, events_path = valid_fixture(tmp_path)

    manifest = v1.build_manifest(
        candidate=payload,
        candidate_path=candidate_path,
        events_path=events_path,
    )
    report_path = tmp_path / "report.md"
    v1.atomic_write_text(report_path, v1.final_report(manifest))
    manifest["report"] = {
        "path": str(report_path),
        "sha256": v1.sha256_file(report_path),
    }

    assert manifest["result"] == "pass"
    assert manifest["source_sha"] == SOURCE_SHA
    assert manifest["violations"] == []
    assert manifest["auditor"].get("controller") == {
        "path": str(v1.CONTROLLER_PATH),
        "sha256": v1.sha256_file(v1.CONTROLLER_PATH),
    }
    assert v1.v1_manifest_violations(manifest, source_sha=SOURCE_SHA) == []


def test_v1_cli_writes_required_report(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _, candidate_path, events_path = valid_fixture(tmp_path)
    output = tmp_path / "manifest.json"
    monkeypatch.setattr(
        v1,
        "parse_args",
        lambda: argparse.Namespace(
            candidate_manifest=candidate_path,
            stack_events=events_path,
            output=output,
        ),
    )

    assert v1.main() == 0

    report_path = tmp_path / "report.md"
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert report_path.is_file()
    assert manifest["report"] == {
        "path": str(report_path),
        "sha256": v1.sha256_file(report_path),
    }
    assert "# ForWin v5 V1 Fresh-schema Release Gate" in report_path.read_text(
        encoding="utf-8"
    )


def test_v1_manifest_rejects_tampered_report(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _, candidate_path, events_path = valid_fixture(tmp_path)
    output = tmp_path / "manifest.json"
    monkeypatch.setattr(
        v1,
        "parse_args",
        lambda: argparse.Namespace(
            candidate_manifest=candidate_path,
            stack_events=events_path,
            output=output,
        ),
    )
    assert v1.main() == 0
    manifest = json.loads(output.read_text(encoding="utf-8"))
    (tmp_path / "report.md").write_text("tampered\n", encoding="utf-8")

    violations = v1.v1_manifest_violations(manifest, source_sha=SOURCE_SHA)

    assert "V1 report hash mismatch" in violations


def test_v1_manifest_rejects_forged_report_and_updated_hash(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _, candidate_path, events_path = valid_fixture(tmp_path)
    output = tmp_path / "manifest.json"
    monkeypatch.setattr(
        v1,
        "parse_args",
        lambda: argparse.Namespace(
            candidate_manifest=candidate_path,
            stack_events=events_path,
            output=output,
        ),
    )
    assert v1.main() == 0
    manifest = json.loads(output.read_text(encoding="utf-8"))
    report_path = tmp_path / "report.md"
    report_path.write_text("# Forged pass\n", encoding="utf-8")
    manifest["report"]["sha256"] = v1.sha256_file(report_path)

    violations = v1.v1_manifest_violations(manifest, source_sha=SOURCE_SHA)

    assert "V1 report content mismatch" in violations


def test_v1_manifest_rejects_tampered_controller(tmp_path: Path) -> None:
    payload, candidate_path, events_path = valid_fixture(tmp_path)
    manifest = v1.build_manifest(
        candidate=payload,
        candidate_path=candidate_path,
        events_path=events_path,
    )
    report_path = tmp_path / "report.md"
    v1.atomic_write_text(report_path, v1.final_report(manifest))
    manifest["report"] = {
        "path": str(report_path),
        "sha256": v1.sha256_file(report_path),
    }
    manifest["auditor"]["controller"]["sha256"] = "0" * 64

    violations = v1.v1_manifest_violations(manifest, source_sha=SOURCE_SHA)

    assert "V1 controller hash mismatch" in violations


def test_v1_cli_refuses_to_overwrite_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _, candidate_path, events_path = valid_fixture(tmp_path)
    output = tmp_path / "manifest.json"
    monkeypatch.setattr(
        v1,
        "parse_args",
        lambda: argparse.Namespace(
            candidate_manifest=candidate_path,
            stack_events=events_path,
            output=output,
        ),
    )
    assert v1.main() == 0

    with pytest.raises(v1.V1EvidenceError, match="already exists"):
        v1.main()


def test_v1_rejects_schema_role_that_started_on_stale_revision(
    tmp_path: Path,
) -> None:
    payload, candidate_path, events_path = valid_fixture(tmp_path)
    events = v1.load_verified_events(events_path)
    events[1]["stale_schema_failfast"]["startup_exit_code"] = 0

    violations = v1.preflight_violations(payload, events)

    assert "stale schema role startup unexpectedly succeeded" in violations


def test_v1_rejects_reclassified_stale_schema_log(tmp_path: Path) -> None:
    payload, _candidate_path, events_path = valid_fixture(tmp_path)
    events = v1.load_verified_events(events_path)
    log_path = Path(events[1]["stale_schema_failfast"]["startup_log"]["path"])
    log_path.write_text("unrelated startup failure\n", encoding="utf-8")
    events[1]["stale_schema_failfast"]["startup_log"]["sha256"] = v1.sha256_file(
        log_path
    )

    violations = v1.preflight_violations(payload, events)

    assert "stale schema startup log classification mismatch" in violations


def test_v1_rejects_start_completion_identity_drift(tmp_path: Path) -> None:
    payload, candidate_path, events_path = valid_fixture(tmp_path)
    events = v1.load_verified_events(events_path)
    events[0]["identity"]["source_tree"] = "c" * 40

    violations = v1.preflight_violations(payload, events)

    assert "V1 start/completion identity mismatch" in violations


def test_v1_rejects_harness_hash_mismatch(tmp_path: Path) -> None:
    payload, candidate_path, events_path = valid_fixture(tmp_path)
    events = v1.load_verified_events(events_path)
    for event in events:
        event["identity"]["harness"]["compose_override"]["sha256"] = "0" * 64

    violations = v1.preflight_violations(payload, events)

    assert "V1 harness hash mismatch: compose_override" in violations


def test_v1_rejects_mcp_probe_helper_not_bound_to_harness(
    tmp_path: Path,
) -> None:
    payload, candidate_path, events_path = valid_fixture(tmp_path)
    events = v1.load_verified_events(events_path)
    events[1]["after"]["services"]["forwin-mcp"]["probe"][
        "helper_sha256"
    ] = "0" * 64

    violations = v1.preflight_violations(payload, events)

    assert "V1 MCP helper probe hash mismatch" in violations


def test_v1_rejects_missing_runtime_role(tmp_path: Path) -> None:
    payload, candidate_path, events_path = valid_fixture(tmp_path)
    events = v1.load_verified_events(events_path)
    del events[1]["after"]["services"]["publisher-worker"]

    violations = v1.preflight_violations(payload, events)

    assert any("service set mismatch" in item for item in violations)


def test_v1_rejects_embedding_dimension_mismatch(tmp_path: Path) -> None:
    payload, candidate_path, events_path = valid_fixture(tmp_path)
    events = v1.load_verified_events(events_path)
    events[1]["embedding"]["vector_dims"] = [128]

    violations = v1.preflight_violations(payload, events)

    assert "embedding vector dimensions do not match configuration" in violations


def test_v1_rejects_non_lan_embedding_gateway(tmp_path: Path) -> None:
    payload, candidate_path, events_path = valid_fixture(tmp_path)
    events = v1.load_verified_events(events_path)
    events[1]["embedding"]["base_url"] = "https://embedding.example.com"

    violations = v1.preflight_violations(payload, events)

    assert "embedding gateway is not a private LAN address" in violations


def test_v1_rejects_missing_destroy_seal(tmp_path: Path) -> None:
    payload, candidate_path, events_path = valid_fixture(tmp_path)
    events = v1.load_verified_events(events_path)[:-1]

    violations = v1.preflight_violations(payload, events)

    assert "V1 destroy event count=0, expected=1" in violations


def test_v1_rejects_dependency_image_mismatch(tmp_path: Path) -> None:
    payload, candidate_path, events_path = valid_fixture(tmp_path)
    events = v1.load_verified_events(events_path)
    events[1]["after"]["services"]["qdrant"]["image_id"] = "sha256:wrong"

    violations = v1.preflight_violations(payload, events)

    assert "dependency service qdrant image mismatch" in violations


def test_v1_rejects_missing_functional_probe(tmp_path: Path) -> None:
    payload, _candidate_path, events_path = valid_fixture(tmp_path)
    events = v1.load_verified_events(events_path)
    del events[1]["after"]["services"]["minio"]["probe"]

    violations = v1.preflight_violations(payload, events)

    assert "service minio functional probe did not pass" in violations


def test_v1_rejects_embedding_tcp_peer_mismatch(tmp_path: Path) -> None:
    payload, candidate_path, events_path = valid_fixture(tmp_path)
    events = v1.load_verified_events(events_path)
    events[1]["embedding"]["peer_ip"] = "10.0.0.151"

    violations = v1.preflight_violations(payload, events)

    assert "embedding TCP peer differs from gateway address" in violations


def test_v1_manifest_rejects_tampered_event_log(tmp_path: Path) -> None:
    payload, candidate_path, events_path = valid_fixture(tmp_path)
    manifest = v1.build_manifest(
        candidate=payload,
        candidate_path=candidate_path,
        events_path=events_path,
    )
    events_path.write_text("{}\n", encoding="utf-8")

    violations = v1.v1_manifest_violations(manifest, source_sha=SOURCE_SHA)

    assert any("event log hash mismatch" in item for item in violations)
