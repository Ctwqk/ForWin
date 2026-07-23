from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


ROOT = Path(__file__).parent


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


smoke = load_module("release_smoke_finalizer", ROOT / "finalize_smoke.py")
lifecycle = load_module("smoke_lifecycle_fixtures", ROOT / "smoke_lifecycle.py")
matrix_tests = load_module("matrix_test_fixtures_smoke", ROOT / "test_finalize_matrix.py")


def candidate_manifest() -> dict:
    collected_at = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    return {
        "collected_at": collected_at.isoformat(),
        "source": {"sha": "a" * 40, "tree": "b" * 40},
        "images": {
            "runtime": {
                "tag": "runtime:v1",
                "image_id": "runtime-id",
                "revision": "a" * 40,
            },
            "publisher_browser": {
                "tag": "browser:v1",
                "image_id": "browser-id",
                "revision": "a" * 40,
            },
            "postgres": {"tag": "postgres:v1", "image_id": "postgres-id"},
            "qdrant": {"tag": "qdrant:v1", "image_id": "qdrant-id"},
            "minio": {"tag": "minio:v1", "image_id": "minio-id"},
        },
        "release_harness": {
            "files": [
                {
                    "path": smoke.LIFECYCLE_MODULE_PATH.relative_to(
                        smoke.ROOT
                    ).as_posix(),
                    "sha256": smoke.l200.sha256_file(
                        smoke.LIFECYCLE_MODULE_PATH
                    ),
                }
            ]
        },
        "runtime_policy": {
            "quality_profile": "standard",
            "gate_delegate": "human",
        },
    }


def complete_identity(candidate_path: Path, candidate: dict) -> dict:
    project = "smoke-stack"
    images = candidate["images"]

    def containers(services: set[str], image_key: str | None) -> list[dict]:
        return [
            {
                "compose_project": project,
                "compose_service": service,
                "image_id": images[image_key or service]["image_id"],
            }
            for service in sorted(services)
        ]

    return {
        "source_sha": candidate["source"]["sha"],
        "source_tree": candidate["source"]["tree"],
        "candidate_manifest": {
            "path": str(candidate_path),
            "sha256": smoke.l200.sha256_file(candidate_path),
        },
        "images": candidate["images"],
        "candidate_stack": {
            "compose_project": project,
            "runtime_containers": containers(
                set(smoke.l200.EXPECTED_RUNTIME_SERVICES),
                "runtime",
            ),
            "publisher_browser_containers": containers(
                set(smoke.l200.EXPECTED_BROWSER_SERVICES),
                "publisher_browser",
            ),
            "dependency_containers": containers(
                set(smoke.l200.EXPECTED_DEPENDENCY_SERVICES),
                None,
            ),
            "connection_bindings": {
                "compose_project": project,
                "api": {
                    "compose_service": "forwin",
                    "host_ip": "127.0.0.1",
                    "host_port": 18899,
                },
                "mcp": {
                    "compose_service": "forwin-mcp",
                    "host_ip": "127.0.0.1",
                    "host_port": 18896,
                },
                "database": {
                    "compose_service": "postgres",
                    "host_ip": "127.0.0.1",
                    "host_port": 55434,
                },
                "qdrant": {
                    "compose_service": "qdrant",
                    "host_ip": "127.0.0.1",
                    "host_port": 16337,
                },
            },
        },
    }


def complete_transcript(identity: dict, project_id: str) -> dict:
    recorder = lifecycle.Recorder("run-1")
    bindings = identity["candidate_stack"]["connection_bindings"]

    def append(
        tool: str,
        *,
        transport: str = "mcp_http",
        stage_key: str = "",
        task_id: str = "",
    ) -> None:
        recorder.append(
            tool=tool,
            transport=transport,
            arguments={"project_id": project_id, "stage_key": stage_key},
            project_id=project_id,
            stage_key=stage_key,
            task_id=task_id,
            request_id=f"request-{len(recorder.operations) + 1}",
        )

    append("project_create")
    append("project_policy_update", transport="http")
    for stage in lifecycle.GENESIS_STAGES:
        append("genesis_stage_generate", stage_key=stage)
        append("genesis_stage_lock", stage_key=stage)
    append("task_active_generation_check")
    append("project_start_writing", task_id="task-1")
    return {
        "schema_version": 1,
        "result": "handoff_started",
        "source_sha": identity["source_sha"],
        "source_tree": identity["source_tree"],
        "run_id": "run-1",
        "started_at": "2026-07-22T12:01:00+00:00",
        "completed_at": "2026-07-22T12:02:00+00:00",
        "project_id": project_id,
        "handoff_task_id": "task-1",
        "target": 30,
        "candidate_manifest": identity["candidate_manifest"],
        "harness": {
            "path": str(smoke.LIFECYCLE_MODULE_PATH),
            "sha256": smoke.l200.sha256_file(smoke.LIFECYCLE_MODULE_PATH),
        },
        "mcp_url": (
            f"http://{bindings['mcp']['host_ip']}:"
            f"{bindings['mcp']['host_port']}/mcp"
        ),
        "api_url": (
            f"http://{bindings['api']['host_ip']}:"
            f"{bindings['api']['host_port']}"
        ),
        "operation_count": len(recorder.operations),
        "operation_chain_head": recorder.operations[-1]["operation_sha256"],
        "operations": recorder.operations,
    }


def complete_evidence() -> dict:
    evidence = matrix_tests.evidence("L30")
    evidence["project"]["creation_status"] = "writing"
    evidence["genesis"] = {
        "project_id": evidence["project"]["id"],
        "creation_status": "writing",
        "can_start_writing": False,
        "stage_states": [
            {
                "stage_key": stage,
                "status": "locked",
                "locked": True,
            }
            for stage in smoke.l200.GENESIS_STAGES
        ],
    }
    evidence["policy"]["version"] = 1
    evidence["task_policy_snapshots"] = {
        "count": 1,
        "items": [
            {
                "task_id": "task-1",
                "status": "completed",
                "policy_version": 1,
                "payload_sha256": "a" * 64,
                "policy_snapshot_sha256": smoke.l200.canonical_hash(
                    evidence["policy"]["policy"]
                ),
            }
        ],
    }
    return evidence


def test_complete_fresh_30_smoke_passes() -> None:
    candidate = candidate_manifest()
    evidence = complete_evidence()
    fresh_state = {
        "projects": 1,
        "project_created_at": (
            datetime.fromisoformat(candidate["collected_at"]) + timedelta(minutes=1)
        ).isoformat(),
    }

    assert smoke.smoke_violations(
        candidate,
        evidence,
        fresh_state,
        profile="standard",
        delegate="human",
    ) == []


def test_smoke_rejects_project_created_before_candidate() -> None:
    candidate = candidate_manifest()
    evidence = complete_evidence()
    fresh_state = {
        "projects": 1,
        "project_created_at": "2026-07-22T11:59:00+00:00",
    }

    violations = smoke.smoke_violations(
        candidate,
        evidence,
        fresh_state,
        profile="standard",
        delegate="human",
    )

    assert "smoke project was not created after candidate collection" in violations


def test_smoke_rejects_unlocked_genesis_stage() -> None:
    candidate = candidate_manifest()
    evidence = complete_evidence()
    evidence["genesis"]["stage_states"][2]["locked"] = False
    fresh_state = {
        "projects": 1,
        "project_created_at": (
            datetime.fromisoformat(candidate["collected_at"]) + timedelta(minutes=1)
        ).isoformat(),
    }

    violations = smoke.smoke_violations(
        candidate,
        evidence,
        fresh_state,
        profile="standard",
        delegate="human",
    )

    assert "Genesis stage is not locked: map" in violations


def test_smoke_rejects_task_policy_snapshot_mismatch() -> None:
    candidate = candidate_manifest()
    evidence = complete_evidence()
    evidence["task_policy_snapshots"]["items"][0][
        "policy_snapshot_sha256"
    ] = "b" * 64
    fresh_state = {
        "projects": 1,
        "project_created_at": (
            datetime.fromisoformat(candidate["collected_at"]) + timedelta(minutes=1)
        ).isoformat(),
    }

    violations = smoke.smoke_violations(
        candidate,
        evidence,
        fresh_state,
        profile="standard",
        delegate="human",
    )

    assert "task task-1 policy snapshot differs from live policy" in violations


def test_task_policy_snapshot_summary_redacts_execution_payload() -> None:
    policy = {
        "quality_profile": "standard",
        "pause": {"gate_delegate": "human"},
    }
    payload = {
        "mode": "initial",
        "premise": "private story premise",
        "genre": "fantasy",
        "num_chapters": 30,
        "policy_version": 1,
        "policy_snapshot": policy,
    }

    summary = smoke.summarize_task_policy_snapshots(
        [
            {
                "id": "task-1",
                "status": "completed",
                "execution_payload_json": json.dumps(payload),
            }
        ]
    )

    assert summary == {
        "count": 1,
        "items": [
            {
                "task_id": "task-1",
                "status": "completed",
                "policy_version": 1,
                "payload_sha256": smoke.l200.canonical_hash(payload),
                "policy_snapshot_sha256": smoke.l200.canonical_hash(policy),
            }
        ],
    }
    assert "premise" not in json.dumps(summary)


def test_complete_lifecycle_transcript_passes(tmp_path: Path) -> None:
    candidate = candidate_manifest()
    candidate_path = tmp_path / "candidate.json"
    candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
    identity = complete_identity(candidate_path, candidate)
    evidence = complete_evidence()
    transcript = complete_transcript(identity, evidence["project"]["id"])

    assert smoke.identity_violations(candidate, identity) == []
    assert (
        smoke.operation_transcript_violations(
            candidate,
            identity,
            transcript,
            evidence,
        )
        == []
    )


def test_lifecycle_urls_require_numeric_loopback() -> None:
    assert (
        lifecycle.require_loopback_url(
            "http://127.0.0.1:8899",
            path="",
            label="API URL",
        )
        == "http://127.0.0.1:8899"
    )
    with pytest.raises(lifecycle.LifecycleError, match="direct loopback"):
        lifecycle.require_loopback_url(
            "http://localhost:8899",
            path="",
            label="API URL",
        )


def test_lifecycle_command_environment_rejects_docker_control() -> None:
    with pytest.raises(lifecycle.LifecycleError, match="DOCKER_HOST"):
        lifecycle.command_environment(
            {
                "HOME": "/tmp/home",
                "PATH": "/usr/bin",
                "DOCKER_HOST": "tcp://production.example:2376",
            }
        )


def test_lifecycle_command_uses_minimal_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    class Completed:
        returncode = 0
        stdout = "ok\n"
        stderr = ""

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return Completed()

    monkeypatch.setattr(
        lifecycle,
        "command_environment",
        lambda: {"SAFE": "1"},
    )
    monkeypatch.setattr(lifecycle.subprocess, "run", fake_run)

    assert lifecycle.command("git", "rev-parse", "HEAD") == "ok"
    assert captured["env"] == {"SAFE": "1"}


def test_lifecycle_mcp_client_disables_env_and_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = object()
    options: dict = {}

    def fake_async_client(**kwargs):
        options.update(kwargs)
        return sentinel

    monkeypatch.setattr(lifecycle.httpx, "AsyncClient", fake_async_client)
    timeout = lifecycle.httpx.Timeout(12)

    assert (
        lifecycle.direct_mcp_http_client(
            headers={"X-Test": "1"},
            timeout=timeout,
            auth=None,
        )
        is sentinel
    )
    assert options == {
        "headers": {"X-Test": "1"},
        "timeout": timeout,
        "auth": None,
        "trust_env": False,
        "follow_redirects": False,
    }


def test_lifecycle_transcript_rejects_endpoint_not_bound_to_candidate_stack(
    tmp_path: Path,
) -> None:
    candidate = candidate_manifest()
    candidate_path = tmp_path / "candidate.json"
    candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
    identity = complete_identity(candidate_path, candidate)
    evidence = complete_evidence()
    transcript = complete_transcript(identity, evidence["project"]["id"])
    transcript["api_url"] = "http://127.0.0.1:19999"

    violations = smoke.operation_transcript_violations(
        candidate,
        identity,
        transcript,
        evidence,
    )

    assert "smoke lifecycle endpoint binding mismatch" in violations


def test_lifecycle_transcript_rejects_missing_genesis_operation(
    tmp_path: Path,
) -> None:
    candidate = candidate_manifest()
    candidate_path = tmp_path / "candidate.json"
    candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
    identity = complete_identity(candidate_path, candidate)
    evidence = complete_evidence()
    transcript = complete_transcript(identity, evidence["project"]["id"])
    transcript["operations"].pop(3)

    violations = smoke.operation_transcript_violations(
        candidate,
        identity,
        transcript,
        evidence,
    )

    assert "smoke lifecycle operation sequence mismatch" in violations
