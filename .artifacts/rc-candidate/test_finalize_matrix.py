from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).parent


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


matrix = load_module("matrix_finalizer", ROOT / "finalize_matrix.py")
fixtures = load_module("l200_test_fixtures", ROOT / "test_l200_evidence.py")


def test_matrix_command_uses_l200_host_environment(
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
        matrix.l200,
        "command_environment",
        lambda: {"SAFE": "1"},
    )
    monkeypatch.setattr(matrix.subprocess, "run", fake_run)

    assert matrix.command("git", "rev-parse", "HEAD") == "ok"
    assert captured["env"] == {"SAFE": "1"}


def test_matrix_finalizer_refuses_nonempty_output_directory(
    tmp_path: Path,
) -> None:
    output = tmp_path / "final-audit"
    output.mkdir()
    stale_manifest = output / "manifest.json"
    stale_manifest.write_text('{"result":"pass"}\n', encoding="utf-8")

    with pytest.raises(matrix.MatrixAuditError, match="is not empty"):
        matrix.prepare_output_directory(output)

    assert stale_manifest.read_text(encoding="utf-8") == '{"result":"pass"}\n'


def evidence(name: str) -> dict:
    expected = matrix.EXPECTED_CELLS[name]
    target = expected["target"]
    delegate = expected["delegate"]
    profile = expected["profile"]
    mcp = fixtures.mcp_state()
    mcp["project"]["target_total_chapters"] = target
    mcp["project"]["accepted_chapter_count"] = target
    mcp["chapters"] = mcp["chapters"][:target]
    policy = {
        "quality_profile": profile,
        "pause": {"gate_delegate": delegate},
    }
    database = fixtures.database_state()
    database["canon"].update(
        {
            "committed": target,
            "distinct_chapters": target,
            "last_chapter": target,
        }
    )
    database["candidates"]["accepted_canon"] = target
    database["snapshots"].update(
        {"world_snapshot_through": target, "map_snapshot_through": target}
    )
    database["outbox"]["total"] = target * 3
    database["outbox"]["event_type_counts"] = {
        event_type: target for event_type in matrix.l200.EXPECTED_OUTBOX_TYPES
    }
    for projection in database["projections"]:
        projection["target_chapter_number"] = target
        projection["projected_chapter_number"] = target
    database["maintenance"]["total"] = target * 4
    database["maintenance"]["step_counts"] = {
        step: target for step in matrix.l200.EXPECTED_MAINTENANCE_STEPS
    }
    spark = {
        "gate_delegation_requested": 1 if delegate == "spark" else 0,
        "gate_delegation_decided": 1 if delegate == "spark" else 0,
        "gate_delegation_approved": 1 if delegate == "spark" else 0,
        "gate_delegation_failed": 0,
    }
    return {
        "manifest_cell": {
            "project_id": mcp["project"]["id"],
            "status": "complete",
            "target": target,
            "profile": profile,
            "delegate": delegate,
            "policy_version": 1,
            "policy_hash": matrix.l200.canonical_hash(policy),
        },
        "project": mcp["project"],
        "chapters": mcp["chapters"],
        "active_task_check": mcp["active_task_check"],
        "tasks": [],
        "gate_ledger": mcp["gate_ledger"],
        "cost_report": mcp["cost_report"],
        "rule_provenance": mcp["rule_provenance"],
        "policy": {"version": 1, "policy": policy},
        "database": database,
        "operational": {"spark": spark},
    }


def test_human_cell_complete_fixture_passes() -> None:
    assert matrix.validate_cell("L30", evidence("L30")) == []


def test_spark_cell_requires_terminal_delegation_for_every_request() -> None:
    item = evidence("L60S")
    assert matrix.validate_cell("L60S", item) == []
    item["operational"]["spark"]["gate_delegation_requested"] = 2
    violations = matrix.validate_cell("L60S", item)
    assert "Spark delegation terminal count=1, requested=2" in violations


def test_projection_and_canon_identity_fail_closed() -> None:
    item = evidence("L100")
    item["database"]["canon"]["candidate_identity_mismatches"] = 1
    item["database"]["projections"][0]["projected_chapter_number"] = 99
    violations = matrix.validate_cell("L100", item)
    assert "canon.candidate_identity_mismatches=1, expected=0" in violations
    assert "projection obsidian projected=99" in violations


def test_manifest_cell_is_bound_to_mcp_project_identity() -> None:
    item = evidence("L30")
    item["project"]["id"] = "different-project"

    violations = matrix.validate_cell("L30", item)

    assert (
        "manifest project_id=project-200, MCP project id=different-project"
        in violations
    )


def test_frozen_policy_version_detects_change_and_restore() -> None:
    item = evidence("L30")
    item["policy"]["version"] = 2
    item["database"]["freeze_audit"]["runtime_policy_version"] = 2

    violations = matrix.validate_cell("L30", item)

    assert "live policy version=2, frozen=1" in violations
    assert "database policy version=2, frozen=1" in violations


def test_completed_run_validator_supports_fresh_release_smoke() -> None:
    assert matrix.validate_completed_run(
        evidence("L30"),
        target=30,
        profile="standard",
        delegate="human",
    ) == []


def test_candidate_stack_binds_endpoints_to_verified_compose_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identities = {
        "runtime": [
            {
                "name": service,
                "compose_project": "candidate",
                "compose_service": service,
            }
            for service in sorted(matrix.l200.EXPECTED_RUNTIME_SERVICES)
        ],
        "publisher-browser": [
            {
                "name": "publisher-browser",
                "compose_project": "candidate",
                "compose_service": "publisher-browser",
            }
        ],
        "dependency": [
            {
                "name": service,
                "compose_project": "candidate",
                "compose_service": service,
            }
            for service in sorted(matrix.l200.EXPECTED_DEPENDENCY_SERVICES)
        ],
    }
    verified_roles: list[tuple[str, str]] = []

    def fake_verify_container_set(
        names: list[str],
        *,
        expected_image_id: str = "",
        role: str,
        expected_services: frozenset[str],
    ) -> list[dict]:
        assert set(names) == set(expected_services)
        verified_roles.append((role, expected_image_id))
        return identities[role]

    monkeypatch.setattr(
        matrix.l200,
        "verify_container_set",
        fake_verify_container_set,
    )
    monkeypatch.setattr(
        matrix.l200,
        "verify_candidate_mount_policy",
        lambda containers: None,
    )

    def fake_bindings(args: argparse.Namespace, freeze: dict) -> dict:
        assert args.api_url == "http://127.0.0.1:8899"
        assert freeze["compose_project"] == "candidate"
        return {"compose_project": "candidate", "api": {"compose_service": "forwin"}}

    monkeypatch.setattr(matrix.l200, "connection_bindings", fake_bindings)
    args = argparse.Namespace(
        runtime_container=sorted(matrix.l200.EXPECTED_RUNTIME_SERVICES),
        browser_container=["publisher-browser"],
        dependency_container=sorted(matrix.l200.EXPECTED_DEPENDENCY_SERVICES),
        api_url="http://127.0.0.1:8899",
        mcp_url="http://127.0.0.1:8896/mcp",
        database_url="postgresql://user:secret@127.0.0.1:5432/forwin",
    )

    stack = matrix.candidate_stack_identity(
        args,
        runtime_image_id="runtime-id",
        browser_image_id="browser-id",
    )

    assert verified_roles == [
        ("runtime", "runtime-id"),
        ("publisher-browser", "browser-id"),
        ("dependency", ""),
    ]
    assert stack["compose_project"] == "candidate"
    assert stack["connection_bindings"]["api"]["compose_service"] == "forwin"


def test_candidate_stack_rejects_cross_compose_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_verify_container_set(
        names: list[str],
        *,
        expected_image_id: str = "",
        role: str,
        expected_services: frozenset[str],
    ) -> list[dict]:
        project = "other" if role == "dependency" else "candidate"
        return [
            {
                "name": service,
                "compose_project": project,
                "compose_service": service,
            }
            for service in sorted(expected_services)
        ]

    monkeypatch.setattr(
        matrix.l200,
        "verify_container_set",
        fake_verify_container_set,
    )
    args = argparse.Namespace(
        runtime_container=sorted(matrix.l200.EXPECTED_RUNTIME_SERVICES),
        browser_container=["publisher-browser"],
        dependency_container=sorted(matrix.l200.EXPECTED_DEPENDENCY_SERVICES),
        api_url="http://127.0.0.1:8899",
        mcp_url="http://127.0.0.1:8896/mcp",
        database_url="postgresql://user:secret@127.0.0.1:5432/forwin",
    )

    with pytest.raises(matrix.MatrixAuditError, match="different Compose stacks"):
        matrix.candidate_stack_identity(
            args,
            runtime_image_id="runtime-id",
            browser_image_id="browser-id",
        )
