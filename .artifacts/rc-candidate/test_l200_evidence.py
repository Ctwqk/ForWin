from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).with_name("l200_evidence.py")
RC_COLLECTOR_PATH = Path(__file__).with_name("collect_rc_manifest.py")
SPEC = importlib.util.spec_from_file_location("l200_evidence", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
l200 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(l200)


def args() -> argparse.Namespace:
    return argparse.Namespace(project_id="project-200", expected_target=200)


def test_l200_cli_starts_without_pythonpath() -> None:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONHOME", "PYTHONPATH"}
    }
    completed = subprocess.run(
        [
            sys.executable,
            str(MODULE_PATH),
            "verify-final",
            "--help",
        ],
        cwd=MODULE_PATH.parents[2],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "verify-final" in completed.stdout


def test_l200_finalization_refuses_already_finalized_manifest(
    tmp_path: Path,
) -> None:
    with pytest.raises(l200.EvidenceError, match="already finalized"):
        l200.assert_final_output_unsealed(
            tmp_path,
            {
                "finalized_at": "2026-07-23T12:00:00+00:00",
                "result": "pass",
                "artifacts": {},
            },
        )


def test_l200_finalization_refuses_partial_final_artifacts(
    tmp_path: Path,
) -> None:
    (tmp_path / "final-report.md").write_text("partial\n", encoding="utf-8")

    with pytest.raises(
        l200.EvidenceError,
        match="final output already exists: final-report.md",
    ):
        l200.assert_final_output_unsealed(tmp_path, {"valid": True})


def test_l200_command_environment_rejects_git_control() -> None:
    with pytest.raises(l200.EvidenceError, match="GIT_DIR"):
        l200.command_environment(
            {
                "HOME": "/tmp/home",
                "PATH": "/usr/bin",
                "GIT_DIR": "/tmp/other-repository",
            }
        )


def test_l200_command_uses_minimal_environment(
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

    monkeypatch.setattr(l200, "command_environment", lambda: {"SAFE": "1"})
    monkeypatch.setattr(l200.subprocess, "run", fake_run)

    assert l200.command("git", "rev-parse", "HEAD") == "ok"
    assert captured["env"] == {"SAFE": "1"}


def gate_report(*, scope: str = "project", band_id: str = "") -> dict:
    return {
        "schema_version": 1,
        "scope": scope,
        "project_id": "project-200",
        "band_id": band_id,
        "project_count": 1,
        "event_count": 1,
        "checkpoint_count": 1,
        "metrics": [
            {
                "gate_id": "band_checkpoint",
                "gate_versions": ["v1"],
                "responsibility_domains": ["band_integrity"],
                "opportunities": 1,
                "evaluations": 1,
                "fires": 0,
                "blocks": 0,
                "pauses": 0,
                "approvals": 1,
                "overrides": 0,
                "post_override_incident_proxy": 0,
                "post_pass_incident_proxy": 0,
                "unknown_legacy_count": 0,
                "fire_rate": 0.0,
                "block_rate": 0.0,
                "override_rate": 0.0,
            }
        ],
    }


def cost_report(*, band_id: str = "") -> dict:
    totals = {
        "attempts": 1,
        "successes": 1,
        "retries": 0,
        "fallbacks": 0,
        "input_chars": 100,
        "output_chars": 50,
        "prompt_tokens": 25,
        "completion_tokens": 10,
        "total_tokens": 35,
        "duration_ms": 10,
        "provider_usage_attempts": 1,
        "codex_usage_attempts": 0,
        "estimated_usage_attempts": 0,
        "missing_usage_attempts": 0,
    }
    return {
        "schema_version": 1,
        "project_id": "project-200",
        "chapter_number": 0,
        "band_id": band_id,
        "candidate_id": "",
        "project_count": 1,
        "trace_count": 1,
        "event_count": 0,
        "totals": totals,
        "dimensions": (
            [{"dimension": "band", "value": band_id, "metrics": totals}]
            if band_id
            else []
        ),
        "gate_costs": [],
        "manual_action_count": 0,
        "manual_action_duration_ms": 0,
        "unknown_manual_duration_count": 0,
        "manual_actions": [],
    }


def rule_report() -> dict:
    return {
        "schema_version": 1,
        "project_id": "project-200",
        "project_count": 1,
        "global_code_backed_rules": [
            {
                "rule_key": "reference_classifier.generic",
                "summary": "General linguistic reference classification.",
                "scope": "global",
                "status": "active",
                "code_owner": "forwin.reference_classifier",
            }
        ],
        "genre_rule_candidates": [],
        "project_rules": [],
        "recommendations": [],
    }


def run_manifest(directory: Path) -> dict:
    return {
        "checkpoints": [
            {
                "chapter": chapter,
                "accepted_at_observation": chapter,
                "sha256": f"sha-{chapter}",
            }
            for chapter in l200.CHECKPOINTS
        ],
        "band_reports": [
            {
                "band_id": "band-all",
                "chapter_start": 1,
                "chapter_end": 200,
                "directory": str(directory),
            }
        ],
        "code_changes_during_run": 0,
        "freeze_audit_state": {
            "runtime_policy_version": 1,
            "runtime_policy_update_events": 0,
            "runtime_policy_update_sha256": "a" * 64,
            "active_rule_events": 0,
            "active_rule_event_sha256": "b" * 64,
        },
    }


def mcp_state() -> dict:
    chapters = [
        {
            "chapter_number": chapter,
            "title": f"Chapter {chapter}",
            "status": "accepted",
            "char_count": 3000,
        }
        for chapter in range(1, 201)
    ]
    project = {
        "id": "project-200",
        "accepted_chapter_count": 200,
        "needs_review_chapter_count": 0,
        "generation_control": {"failed_chapters": []},
    }
    return {
        "project": project,
        "chapters": chapters,
        "active_task_check": {"has_active_generation_task": False},
        "tasks": [],
        "gate_ledger": gate_report(),
        "cost_report": cost_report(),
        "rule_provenance": rule_report(),
    }


def database_state() -> dict:
    return {
        "canon": {
            "committed": 200,
            "non_committed": 0,
            "distinct_chapters": 200,
            "first_chapter": 1,
            "last_chapter": 200,
            "duplicate_ids": 0,
            "duplicate_candidate_refs": 0,
            "duplicate_idempotency_keys": 0,
            "duplicate_chapters": 0,
            "missing_world_snapshot_refs": 0,
            "missing_map_snapshot_refs": 0,
            "wrong_world_snapshot_identity": 0,
            "wrong_map_snapshot_identity": 0,
            "candidate_identity_mismatches": 0,
            "chapter_identity_mismatches": 0,
        },
        "candidates": {
            "accepted_canon": 200,
            "missing_commit_id": 0,
            "reverse_identity_mismatches": 0,
            "duplicate_accepted_chapters": 0,
        },
        "graph": {
            "missing_graph_delta_refs": 0,
            "invalid_graph_delta_refs": 0,
            "duplicate_graph_delta_refs": 0,
            "unreferenced_chapter_graph_deltas": 0,
            "orphan_graph_delta_patches": 0,
            "duplicate_semantic_graph_deltas": 0,
            "duplicate_semantic_graph_delta_patches": 0,
        },
        "snapshots": {
            "world_snapshot_through": 200,
            "map_snapshot_through": 200,
        },
        "entities": {
            "duplicate_entity_identities": 0,
            "duplicate_alias_identities": 0,
            "orphan_aliases": 0,
        },
        "outbox": {
            "total": 600,
            "event_type_counts": {
                event_type: 200 for event_type in l200.EXPECTED_OUTBOX_TYPES
            },
            "duplicate_event_ids": 0,
            "backlog": 0,
            "failed": 0,
            "missing_expected_events": 0,
            "identity_mismatches": 0,
            "unexpected_canon_events": 0,
        },
        "projections": [
            {
                "projection_kind": kind,
                "status": "healthy",
                "target_canon_commit_id": "canon-200",
                "target_chapter_number": 200,
                "projected_canon_commit_id": "canon-200",
                "projected_chapter_number": 200,
                "last_event_id": "canon-key-200:canon.projection.requested",
                "source_digest": (
                    "d" * 64 if kind == "chapter_memory" else "e" * 64
                ),
                "last_error": "",
            }
            for kind in l200.EXPECTED_PROJECTIONS
        ],
        "projection_target": {
            "canon_commit_id": "canon-200",
            "chapter_number": 200,
            "event_id": "canon-key-200:canon.projection.requested",
            "chapter_memory_source_digest": "d" * 64,
            "llm_kb_source_digest": "e" * 64,
        },
        "projection_artifacts": {
            "violations": [],
            "obsidian": {
                "source_digest": "e" * 64,
                "as_of_chapter": 200,
            },
            "llm_kb": {
                "source_digest": "e" * 64,
                "as_of_chapter": 200,
            },
        },
        "projection_vectors": {
            "violations": [],
            "chapter_memory": {"point_count": 200},
            "llm_kb": {"point_count": 1},
        },
        "freeze_audit": {
            "runtime_policy_version": 1,
            "runtime_policy_update_events": 0,
            "runtime_policy_update_sha256": "a" * 64,
            "active_rule_events": 0,
            "active_rule_event_sha256": "b" * 64,
        },
        "maintenance": {
            "total": 800,
            "step_counts": {
                step: 200 for step in l200.EXPECTED_MAINTENANCE_STEPS
            },
            "backlog": 0,
            "duplicate_idempotency_keys": 0,
            "duplicate_canon_steps": 0,
        },
        "tasks": {"active_rows": 0},
        "publisher": {
            "unsettled_jobs": 0,
            "duplicate_job_idempotency_keys": 0,
            "duplicate_canon_platform_jobs": 0,
            "duplicate_receipt_keys": 0,
            "invalid_current_attempt_refs": 0,
        },
        "bands": [
            {
                "band_id": "band-all",
                "chapter_start": 1,
                "chapter_end": 200,
                "status": "pass",
            }
        ],
    }


def write_band_reports(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    l200.write_json(
        directory / "gate-ledger.json", gate_report(scope="band", band_id="band-all")
    )
    l200.write_json(directory / "cost-report.json", cost_report(band_id="band-all"))
    l200.write_json(directory / "rule-provenance.json", rule_report())


def write_finalized_output(directory: Path) -> dict:
    mcp = mcp_state()
    database = database_state()
    band_directory = directory / "bands" / "001-200-test"
    write_band_reports(band_directory)
    manifest = {
        "schema_version": 1,
        "run": "forwin-v5-final-l200-no-hotfix",
        "valid": True,
        "finalized_at": "2026-07-23T12:00:00+00:00",
        "project_id": "project-200",
        "expected_target": 200,
        "quality_profile": "standard",
        "gate_delegate": "human",
        "result": "pass",
        "violations": [],
        "code_changes_during_run": 0,
        "freeze_audit_state": database["freeze_audit"],
        "freeze_identity": {"source_sha": "a" * 40},
        "final_freeze_identity": {"source_sha": "a" * 40},
        "connection_bindings": {"compose_project": "candidate"},
        "final_connection_bindings": {"compose_project": "candidate"},
        "live_schema_identity": {"sha256": "b" * 64},
        "final_live_schema_identity": {"sha256": "b" * 64},
        "collector": {
            "path": str(MODULE_PATH.resolve()),
            "sha256": l200.sha256_file(MODULE_PATH),
        },
        "checkpoints": [
            {
                "chapter": chapter,
                "accepted_at_observation": chapter,
                "sha256": f"sha-{chapter}",
            }
            for chapter in l200.CHECKPOINTS
        ],
        "band_reports": [
            {
                "band_id": "band-all",
                "chapter_start": 1,
                "chapter_end": 200,
                "status": "pass",
                "directory": str(band_directory),
            }
        ],
    }
    l200.atomic_write(
        directory / "chapter-status.csv",
        l200.chapter_csv(mcp["chapters"]),
    )
    l200.write_json(directory / "gate-ledger.json", mcp["gate_ledger"])
    l200.write_json(directory / "cost-report.json", mcp["cost_report"])
    l200.write_json(
        directory / "rule-provenance.json",
        mcp["rule_provenance"],
    )
    l200.write_json(
        directory / "canon-integrity.json",
        {
            "schema_version": 1,
            "project_id": "project-200",
            "canon": database["canon"],
            "candidates": database["candidates"],
            "graph": database["graph"],
            "snapshots": database["snapshots"],
            "entities": database["entities"],
            "violations": [],
        },
    )
    l200.write_json(
        directory / "task-recovery.json",
        {
            "schema_version": 1,
            "project_id": "project-200",
            "active_task_check": mcp["active_task_check"],
            "mcp_tasks": mcp["tasks"],
            **database["tasks"],
        },
    )
    l200.write_json(
        directory / "projection-publisher.json",
        {
            "schema_version": 1,
            "project_id": "project-200",
            "outbox": database["outbox"],
            "projections": database["projections"],
            "projection_target": database["projection_target"],
            "projection_artifacts": database["projection_artifacts"],
            "projection_vectors": database["projection_vectors"],
            "maintenance": database["maintenance"],
            "publisher": database["publisher"],
        },
    )
    l200.atomic_write(
        directory / "final-report.md",
        l200.final_report_markdown(
            args(),
            manifest,
            mcp,
            database,
            [],
        ),
    )
    manifest["artifacts"] = {
        name: l200.sha256_file(directory / name)
        for name in l200.FINAL_ARTIFACT_NAMES
    }
    l200.write_json(directory / "manifest.json", manifest)
    return manifest


def write_projection_tree(
    data_root: Path,
    *,
    project_id: str = "project-200",
    as_of_chapter: int = 200,
    llm_kb_source_digest: str = "e" * 64,
) -> str:
    obsidian_root = data_root / "world_vaults" / project_id
    obsidian_root.mkdir(parents=True)
    managed_path = obsidian_root / "00_Index.md"
    managed_path.write_text("# Canon index\n", encoding="utf-8")
    obsidian_manifest = {
        "schema_version": 1,
        "project_id": project_id,
        "as_of_chapter": as_of_chapter,
        "files": {
            "00_Index.md": {
                "sha256": l200.sha256_file(managed_path),
                "kind": "markdown",
            }
        },
    }
    l200.write_json(
        obsidian_root / ".forwin-projection-manifest.json",
        obsidian_manifest,
    )
    obsidian_source_digest = l200.canonical_hash(obsidian_manifest)

    llm_root = data_root / "llm_kb" / project_id
    llm_root.mkdir(parents=True)
    markdown = (
        "# Projection\n\n"
        f"as_of_chapter: {as_of_chapter}\n"
        f"source_digest: {llm_kb_source_digest}\n"
    )
    for file_name in l200.EXPECTED_LLM_KB_MARKDOWN_FILES:
        (llm_root / file_name).write_text(markdown, encoding="utf-8")
    jsonl_files = set(l200.EXPECTED_LLM_KB_JSONL_FILES)
    for file_name in jsonl_files:
        content = ""
        if file_name == "facts.jsonl":
            content = json.dumps(
                {
                    "id": "fact-1",
                    "as_of_chapter": as_of_chapter,
                    "source_digest": "f" * 64,
                }
            ) + "\n"
        (llm_root / file_name).write_text(content, encoding="utf-8")
    vector_payload_hashes = l200.llm_kb_vector_payload_hashes(
        llm_root,
        project_id=project_id,
        source_digest=llm_kb_source_digest,
        target_chapter=as_of_chapter,
    )
    retrieval_index = {
        "project_id": project_id,
        "as_of_chapter": as_of_chapter,
        "source_digest": llm_kb_source_digest,
        "projection_version": "llm_kb_v2",
        "files": sorted(
            set(l200.EXPECTED_LLM_KB_FILES) - {"retrieval_index.json"}
        ),
        "root_policy": "writer_safe",
        "canon_source": "BookState DB canon",
        "vector_index": {
            "backend": "qdrant",
            "collection": "llm_kb_vectors",
            "section_count": len(vector_payload_hashes),
            "dims": 384,
        },
    }
    l200.write_json(llm_root / "retrieval_index.json", retrieval_index)
    return obsidian_source_digest


def frozen_rc_manifest(tmp_path: Path) -> dict:
    source_sha = "a" * 40
    evidence = {}
    for key in (
        "v1_preflight",
        "release_gates",
        "live_recovery",
        "post_decision_smoke",
    ):
        path = tmp_path / f"{key}.json"
        payload = {"source_sha": source_sha}
        if key == "release_gates":
            payload["release_gate_passed"] = True
        else:
            payload["result"] = "pass"
        path.write_text(
            json.dumps(payload),
            encoding="utf-8",
        )
        evidence[key] = {
            "path": str(path),
            "sha256": l200.sha256_file(path),
            "source_sha": source_sha,
            "result": "pass",
        }
    routing = {
        "selected_profile": {
            "id": "env-minimax",
            "model": "primary-model",
            "base_url_target_sha256": "1" * 64,
            "api_key_configured": True,
        },
        "fallback_profiles": [],
        "codex": {
            "enabled": False,
            "default_model": "codex-model",
            "bridge_target_sha256": "2" * 64,
        },
        "embedding": {
            "backend": "gateway",
            "api_key_configured": False,
            "base_url_target_sha256": "3" * 64,
            "model": "embedding-model",
            "dims": 384,
            "required": True,
        },
    }
    passive_routing = {
        **routing,
        "selected_profile": {
            **routing["selected_profile"],
            "api_key_configured": False,
        },
    }
    routing_hash = l200.canonical_hash(routing)
    passive_routing_hash = l200.canonical_hash(passive_routing)
    model_services = {
        service: {
            "container": f"stack-{service}",
            "container_id": f"{service}-id",
            "runtime_image_id": "sha256:runtime",
            "routing": (
                routing
                if service
                in {"forwin", "generation-worker", "outbox-worker"}
                else passive_routing
            ),
            "routing_sha256": (
                routing_hash
                if service
                in {"forwin", "generation-worker", "outbox-worker"}
                else passive_routing_hash
            ),
        }
        for service in l200.EXPECTED_RUNTIME_SERVICES
    }
    aggregate_routing_hash = l200.canonical_hash(
        {
            service: item["routing_sha256"]
            for service, item in sorted(model_services.items())
        }
    )
    return {
        "schema_version": 3,
        "collected_at": datetime(2026, 7, 22, 12, 0, tzinfo=UTC).isoformat(),
        "source": {
            "sha": source_sha,
            "tree": "b" * 40,
            "tracked_worktree_clean": True,
        },
        "images": {
            name: {
                "tag": f"forwin/{name}:rc",
                "image_id": f"sha256:{name}",
                "revision": source_sha,
            }
            for name in ("runtime", "publisher_browser")
        },
        "baseline_schema": {
            "path": str(MODULE_PATH),
            "sha256": l200.sha256_file(MODULE_PATH),
        },
        "runtime_policy": {
            "schema_version": 2,
            "quality_profile": "standard",
            "gate_delegate": "human",
        },
        "model_profiles": {
            "effective_container_fields": {
                "compose_project": "forwin-rc",
                "services": model_services,
                "routing_group_hashes": {
                    "model_execution": routing_hash,
                    "passive": passive_routing_hash,
                },
                "routing_sha256": aggregate_routing_hash,
            },
            "revision": {"sha256": "model"},
        },
        "prompt_revision": {"sha256": "prompt"},
        "skill_registry_revision": {"sha256": "skill"},
        "report_tools": {
            "schema_versions": {
                "GateLedgerReportView": 1,
                "CostLedgerReportView": 1,
                "RuleProvenanceReportView": 1,
            },
            "revision": {"sha256": "reports"},
        },
        "matrix_evidence": {"code_changes_during_run": 0},
        "freeze_contract": {
            "code_changes_during_run": 0,
            "prompt_changes_during_run": 0,
        },
        "release_candidate": {
            "status": "frozen",
            "source_sha": source_sha,
            "annotated_tag": "v5.0.0-rc1",
            "tag_object_sha": "c" * 40,
            "evidence": evidence,
        },
        "collector": {
            "path": str(RC_COLLECTOR_PATH),
            "sha256": l200.sha256_file(RC_COLLECTOR_PATH),
        },
    }


def test_rc_model_routing_accepts_least_privilege_role_groups(
    tmp_path: Path,
) -> None:
    manifest = frozen_rc_manifest(tmp_path)
    effective = manifest["model_profiles"]["effective_container_fields"]

    assert l200.rc_model_routing_violations(
        effective,
        expected_runtime_image="sha256:runtime",
        selected_profile_id="env-minimax",
    ) == []


def test_rc_model_routing_rejects_credentials_in_passive_role(
    tmp_path: Path,
) -> None:
    manifest = frozen_rc_manifest(tmp_path)
    effective = manifest["model_profiles"]["effective_container_fields"]
    effective["services"]["forwin-mcp"]["routing"][
        "selected_profile"
    ]["api_key_configured"] = True

    violations = l200.rc_model_routing_violations(
        effective,
        expected_runtime_image="sha256:runtime",
        selected_profile_id="env-minimax",
    )

    assert "RC passive model routing has credentials: forwin-mcp" in violations


def test_complete_fixture_passes_every_final_invariant(tmp_path: Path) -> None:
    write_band_reports(tmp_path)
    assert l200.final_violations(
        args(), run_manifest(tmp_path), mcp_state(), database_state()
    ) == []


def test_missing_checkpoint_and_active_database_task_are_blocking(tmp_path: Path) -> None:
    write_band_reports(tmp_path)
    manifest = run_manifest(tmp_path)
    manifest["checkpoints"].pop()
    database = database_state()
    database["tasks"]["active_rows"] = 1
    violations = l200.final_violations(args(), manifest, mcp_state(), database)
    assert any("checkpoint evidence incomplete" in item for item in violations)
    assert "database active generation rows=1" in violations


def test_projection_lag_and_duplicate_canon_identity_are_blocking(tmp_path: Path) -> None:
    write_band_reports(tmp_path)
    database = database_state()
    database["canon"]["duplicate_candidate_refs"] = 1
    database["projections"][0]["projected_chapter_number"] = 199
    violations = l200.final_violations(
        args(), run_manifest(tmp_path), mcp_state(), database
    )
    assert "canon.duplicate_candidate_refs=1, expected=0" in violations
    assert "projection obsidian projected=199" in violations


def test_projection_identity_digest_and_error_corruption_are_blocking(
    tmp_path: Path,
) -> None:
    write_band_reports(tmp_path)
    database = database_state()
    obsidian = database["projections"][0]
    obsidian["target_canon_commit_id"] = "stale-target"
    obsidian["projected_canon_commit_id"] = "stale-projection"
    obsidian["last_event_id"] = "stale-event"
    obsidian["source_digest"] = ""
    obsidian["last_error"] = "projection failed"
    chapter_memory = next(
        item
        for item in database["projections"]
        if item["projection_kind"] == "chapter_memory"
    )
    chapter_memory["source_digest"] = "f" * 64
    llm_kb = next(
        item
        for item in database["projections"]
        if item["projection_kind"] == "llm_kb"
    )
    llm_kb["source_digest"] = "a" * 64

    violations = l200.final_violations(
        args(), run_manifest(tmp_path), mcp_state(), database
    )

    assert "projection obsidian target Canon=stale-target" in violations
    assert "projection obsidian projected Canon=stale-projection" in violations
    assert "projection obsidian event=stale-event" in violations
    assert "projection obsidian source digest is invalid" in violations
    assert "projection obsidian last_error is not empty" in violations
    assert "projection chapter_memory source digest mismatch" in violations
    assert "projection llm_kb source digest mismatch" in violations


def test_projection_artifact_tree_revalidates_managed_outputs(tmp_path: Path) -> None:
    obsidian_digest = write_projection_tree(tmp_path)

    state = l200.inspect_projection_artifact_tree(
        tmp_path,
        project_id="project-200",
        target_chapter=200,
        expected_llm_kb_source_digest="e" * 64,
    )

    assert state["violations"] == []
    assert state["obsidian"]["source_digest"] == obsidian_digest
    assert state["llm_kb"]["source_digest"] == "e" * 64
    assert set(state["llm_kb"]["file_hashes"]) == set(
        l200.EXPECTED_LLM_KB_FILES
    )
    assert len(state["llm_kb"]["vector_payload_hashes"]) > 1


def test_projection_artifact_tree_detects_file_and_index_tampering(
    tmp_path: Path,
) -> None:
    write_projection_tree(tmp_path)
    (
        tmp_path / "world_vaults" / "project-200" / "00_Index.md"
    ).write_text("# tampered\n", encoding="utf-8")
    index_path = tmp_path / "llm_kb" / "project-200" / "retrieval_index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["as_of_chapter"] = 199
    index_path.write_text(json.dumps(index), encoding="utf-8")

    state = l200.inspect_projection_artifact_tree(
        tmp_path,
        project_id="project-200",
        target_chapter=200,
        expected_llm_kb_source_digest="e" * 64,
    )

    assert "obsidian file hash mismatch: 00_Index.md" in state["violations"]
    assert "llm_kb as_of_chapter=199, expected=200" in state["violations"]


def test_projection_artifact_violation_and_digest_mismatch_are_blocking(
    tmp_path: Path,
) -> None:
    write_band_reports(tmp_path)
    database = database_state()
    database["projection_artifacts"]["violations"] = ["managed file is stale"]
    database["projection_artifacts"]["obsidian"]["source_digest"] = "a" * 64

    violations = l200.final_violations(
        args(), run_manifest(tmp_path), mcp_state(), database
    )

    assert "projection artifact: managed file is stale" in violations
    assert "projection obsidian artifact digest mismatch" in violations


def test_projection_artifacts_are_copied_from_the_verified_api_container(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "source"
    write_projection_tree(source_root)
    copied_from: list[str] = []

    def fake_command(*command: str) -> str:
        assert command[:3] == ("docker", "container", "cp")
        source = command[3]
        destination = Path(command[4])
        copied_from.append(source)
        if "/world_vaults/" in source:
            local_source = source_root / "world_vaults" / "project-200"
        else:
            local_source = source_root / "llm_kb" / "project-200"
        shutil.copytree(local_source, destination)
        return ""

    monkeypatch.setattr(l200, "command", fake_command)
    state = l200.collect_projection_artifacts(
        {
            "runtime_containers": [
                {
                    "name": "stack-forwin-1",
                    "compose_service": "forwin",
                    "container_id": "container-id",
                },
                {
                    "name": "stack-worker-1",
                    "compose_service": "generation-worker",
                    "container_id": "worker-id",
                },
            ]
        },
        project_id="project-200",
        target_chapter=200,
        expected_llm_kb_source_digest="e" * 64,
    )

    assert state["violations"] == []
    assert state["source_container"] == {
        "name": "stack-forwin-1",
        "container_id": "container-id",
        "compose_service": "forwin",
    }
    assert copied_from == [
        "stack-forwin-1:/app/data/world_vaults/project-200",
        "stack-forwin-1:/app/data/llm_kb/project-200",
    ]


def test_qdrant_projection_payload_sets_must_match_exactly() -> None:
    chapter_payload = {
        "project_id": "project-200",
        "chapter_number": 1,
        "title": "Chapter one",
        "summary": "Summary",
        "excerpt": "Excerpt",
    }
    vector_payload = {
        "project_id": "project-200",
        "index_kind": "llm_kb",
        "as_of_chapter": 200,
        "source_digest": "e" * 64,
    }
    chapter_points = {"chapter-point": chapter_payload}
    vector_points = {"vector-point": vector_payload}
    expected_chapters = {
        "chapter-point": l200.canonical_hash(chapter_payload)
    }
    expected_vectors = {
        "vector-point": l200.canonical_hash(vector_payload)
    }

    assert l200.qdrant_projection_violations(
        chapter_points=chapter_points,
        llm_kb_points=vector_points,
        expected_chapter_hashes=expected_chapters,
        expected_llm_kb_hashes=expected_vectors,
    ) == []

    chapter_points["extra"] = chapter_payload
    vector_points["vector-point"] = {
        **vector_payload,
        "as_of_chapter": 199,
    }
    violations = l200.qdrant_projection_violations(
        chapter_points=chapter_points,
        llm_kb_points=vector_points,
        expected_chapter_hashes=expected_chapters,
        expected_llm_kb_hashes=expected_vectors,
    )
    assert "chapter_memory Qdrant point set mismatch" in violations
    assert "llm_kb Qdrant payload mismatch: vector-point" in violations


def test_qdrant_scroll_collects_all_pages() -> None:
    class Response:
        def __init__(self, payload: dict) -> None:
            self.payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self.payload

    class Client:
        def __init__(self) -> None:
            self.requests: list[dict] = []

        def post(self, _url: str, *, json: dict) -> Response:
            self.requests.append(json)
            if len(self.requests) == 1:
                return Response(
                    {
                        "result": {
                            "points": [
                                {"id": "one", "payload": {"value": 1}}
                            ],
                            "next_page_offset": "next",
                        }
                    }
                )
            return Response(
                {
                    "result": {
                        "points": [
                            {"id": "two", "payload": {"value": 2}}
                        ],
                        "next_page_offset": None,
                    }
                }
            )

    client = Client()
    points = l200.scroll_qdrant_points(
        "http://127.0.0.1:16337",
        collection="chapter_memories",
        project_id="project-200",
        client=client,
    )

    assert points == {
        "one": {"value": 1},
        "two": {"value": 2},
    }
    assert client.requests[1]["offset"] == "next"


def test_projection_vector_collection_uses_verified_runtime_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chapter_payload = {
        "project_id": "project-200",
        "chapter_number": 1,
    }
    llm_payload = {
        "project_id": "project-200",
        "index_kind": "llm_kb",
    }
    calls: list[tuple[str, str, str]] = []

    class Client:
        def __enter__(self) -> Client:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    client = Client()
    client_options: dict = {}

    def fake_client(**kwargs):
        client_options.update(kwargs)
        return client

    monkeypatch.setattr(l200.httpx, "Client", fake_client)

    def fake_scroll(
        base_url: str,
        *,
        collection: str,
        project_id: str,
        client: object,
    ) -> dict[str, dict]:
        assert client is not None
        calls.append((base_url, collection, project_id))
        return (
            {"chapter-point": chapter_payload}
            if collection == "chapter_memories"
            else {"llm-point": llm_payload}
        )

    monkeypatch.setattr(l200, "scroll_qdrant_points", fake_scroll)
    state = l200.collect_projection_vectors(
        {
            "qdrant": {
                "container_id": "qdrant-id",
                "host_ip": "127.0.0.1",
                "host_port": 16337,
            }
        },
        {
            "runtime_containers": [
                {
                    "name": "forwin-api",
                    "container_id": "api-id",
                    "compose_service": "forwin",
                    "projection_config": {
                        "chapter_memory_collection": "chapter_memories",
                        "llm_kb_collection": "llm_kb_vectors",
                    },
                }
            ]
        },
        project_id="project-200",
        projection_target={
            "chapter_memory_payload_hashes": {
                "chapter-point": l200.canonical_hash(chapter_payload)
            }
        },
        projection_artifacts={
            "llm_kb": {
                "vector_index": {"collection": "llm_kb_vectors"},
                "vector_payload_hashes": {
                    "llm-point": l200.canonical_hash(llm_payload)
                },
            }
        },
    )

    assert state["violations"] == []
    assert state["chapter_memory"]["point_count"] == 1
    assert state["llm_kb"]["point_count"] == 1
    assert calls == [
        (
            "http://127.0.0.1:16337",
            "chapter_memories",
            "project-200",
        ),
        (
            "http://127.0.0.1:16337",
            "llm_kb_vectors",
            "project-200",
        ),
    ]
    assert client_options == {
        "timeout": 60,
        "trust_env": False,
        "follow_redirects": False,
    }


def test_release_http_client_factories_disable_env_and_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sync_sentinel = object()
    async_sentinel = object()
    sync_options: dict = {}
    async_options: dict = {}

    def fake_sync(**kwargs):
        sync_options.update(kwargs)
        return sync_sentinel

    def fake_async(**kwargs):
        async_options.update(kwargs)
        return async_sentinel

    monkeypatch.setattr(l200.httpx, "Client", fake_sync)
    monkeypatch.setattr(l200.httpx, "AsyncClient", fake_async)
    timeout = l200.httpx.Timeout(12)

    assert l200.direct_sync_client(timeout=60) is sync_sentinel
    assert (
        l200.direct_mcp_http_client(
            headers={"X-Test": "1"},
            timeout=timeout,
            auth=None,
        )
        is async_sentinel
    )
    assert sync_options == {
        "timeout": 60,
        "trust_env": False,
        "follow_redirects": False,
    }
    assert async_options == {
        "headers": {"X-Test": "1"},
        "timeout": timeout,
        "auth": None,
        "trust_env": False,
        "follow_redirects": False,
    }


def test_policy_fetch_disables_env_and_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options: dict = {}

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"policy": {"quality_profile": "standard"}}

    class AsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get(self, path: str) -> Response:
            assert path == "/api/projects/project-200/policy"
            return Response()

    def fake_async_client(**kwargs):
        options.update(kwargs)
        return AsyncClient()

    monkeypatch.setattr(l200.httpx, "AsyncClient", fake_async_client)

    result = asyncio.run(
        l200.fetch_policy("http://127.0.0.1:18899", "project-200")
    )

    assert result == {"policy": {"quality_profile": "standard"}}
    assert options == {
        "base_url": "http://127.0.0.1:18899",
        "timeout": 60,
        "trust_env": False,
        "follow_redirects": False,
    }


def test_qdrant_projection_violation_is_blocking(tmp_path: Path) -> None:
    write_band_reports(tmp_path)
    database = database_state()
    database["projection_vectors"]["violations"] = [
        "chapter_memory Qdrant point set mismatch"
    ]

    violations = l200.final_violations(
        args(), run_manifest(tmp_path), mcp_state(), database
    )

    assert (
        "projection vector: chapter_memory Qdrant point set mismatch"
        in violations
    )


def test_policy_or_rule_audit_ledger_drift_is_blocking(tmp_path: Path) -> None:
    write_band_reports(tmp_path)
    database = database_state()
    database["freeze_audit"]["runtime_policy_update_events"] = 1
    database["freeze_audit"]["active_rule_events"] = 1

    violations = l200.final_violations(
        args(), run_manifest(tmp_path), mcp_state(), database
    )

    assert "policy/rule freeze audit ledger changed" in violations


@pytest.mark.parametrize(
    "field",
    [
        "unreferenced_chapter_graph_deltas",
        "orphan_graph_delta_patches",
        "duplicate_semantic_graph_deltas",
        "duplicate_semantic_graph_delta_patches",
    ],
)
def test_graph_ledger_corruption_is_blocking(tmp_path: Path, field: str) -> None:
    write_band_reports(tmp_path)
    database = database_state()
    database["graph"][field] = 1

    violations = l200.final_violations(
        args(), run_manifest(tmp_path), mcp_state(), database
    )

    assert f"graph.{field}=1" in violations


def test_connection_hashes_do_not_expose_database_credentials() -> None:
    value = argparse.Namespace(
        api_url="http://127.0.0.1:1",
        mcp_url="http://127.0.0.1:2/mcp",
        database_url="postgresql://user:secret@127.0.0.1/database",
    )
    hashes = l200.connection_hashes(value)
    assert "secret" not in repr(hashes)
    assert all(len(item) == 64 for item in hashes.values())
    rotated = argparse.Namespace(
        api_url=value.api_url,
        mcp_url=value.mcp_url,
        database_url="postgresql://other:new-password@127.0.0.1/database",
    )
    assert l200.connection_hashes(rotated) == hashes


def test_artifact_redaction_covers_headers_cookies_queries_and_assignments() -> None:
    raw = (
        "Authorization: Bearer bearer-secret\n"
        "Proxy-Authorization: Basic basic-secret\n"
        "Cookie: session=cookie-secret; preference=dark\n"
        "request=https://example.test/path?access_token=query-secret&safe=1 "
        "api_key=assignment-secret"
    )

    redacted = l200.redact_text(raw)

    for secret in (
        "bearer-secret",
        "basic-secret",
        "cookie-secret",
        "query-secret",
        "assignment-secret",
    ):
        assert secret not in redacted
    assert "safe=1" in redacted
    assert redacted.count("<redacted>") >= 5


def test_connection_targets_bind_to_verified_compose_services() -> None:
    selected = argparse.Namespace(
        api_url="http://127.0.0.1:19099",
        mcp_url="http://127.0.0.1:19096/mcp",
        database_url=(
            "postgresql+psycopg://forwin:secret@127.0.0.1:55434/forwin"
        ),
    )
    freeze = {
        "compose_project": "forwin-l200",
        "runtime_containers": [
            {
                "name": "forwin-api",
                "container_id": "api-id",
                "compose_service": "forwin",
                "published_ports": {
                    "8899/tcp": [
                        {"host_ip": "127.0.0.1", "host_port": 19099}
                    ]
                },
            },
            {
                "name": "forwin-mcp",
                "container_id": "mcp-id",
                "compose_service": "forwin-mcp",
                "published_ports": {
                    "8896/tcp": [
                        {"host_ip": "127.0.0.1", "host_port": 19096}
                    ]
                },
            },
        ],
        "dependency_containers": [
            {
                "name": "forwin-postgres",
                "container_id": "postgres-id",
                "compose_service": "postgres",
                "published_ports": {
                    "5432/tcp": [
                        {"host_ip": "127.0.0.1", "host_port": 55434}
                    ]
                },
            },
            {
                "name": "forwin-qdrant",
                "container_id": "qdrant-id",
                "compose_service": "qdrant",
                "published_ports": {
                    "6333/tcp": [
                        {"host_ip": "127.0.0.1", "host_port": 16337}
                    ]
                },
            },
        ],
    }

    bindings = l200.connection_bindings(selected, freeze)

    assert bindings["api"]["container_id"] == "api-id"
    assert bindings["mcp"]["container_id"] == "mcp-id"
    assert bindings["database"]["container_id"] == "postgres-id"
    assert bindings["qdrant"]["container_id"] == "qdrant-id"
    assert bindings["database"]["database"] == "forwin"
    assert "secret" not in repr(bindings)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("api_url", "http://127.0.0.1:19199"),
        ("mcp_url", "http://127.0.0.1:19196/mcp"),
        (
            "database_url",
            "postgresql+psycopg://forwin:secret@127.0.0.1:55435/forwin",
        ),
    ],
)
def test_unbound_connection_target_is_rejected(
    field: str,
    value: str,
) -> None:
    selected = argparse.Namespace(
        api_url="http://127.0.0.1:19099",
        mcp_url="http://127.0.0.1:19096/mcp",
        database_url=(
            "postgresql+psycopg://forwin:secret@127.0.0.1:55434/forwin"
        ),
    )
    setattr(selected, field, value)
    freeze = {
        "runtime_containers": [
            {
                "name": "forwin-api",
                "container_id": "api-id",
                "compose_service": "forwin",
                "published_ports": {
                    "8899/tcp": [
                        {"host_ip": "127.0.0.1", "host_port": 19099}
                    ]
                },
            },
            {
                "name": "forwin-mcp",
                "container_id": "mcp-id",
                "compose_service": "forwin-mcp",
                "published_ports": {
                    "8896/tcp": [
                        {"host_ip": "127.0.0.1", "host_port": 19096}
                    ]
                },
            },
        ],
        "dependency_containers": [
            {
                "name": "forwin-postgres",
                "container_id": "postgres-id",
                "compose_service": "postgres",
                "published_ports": {
                    "5432/tcp": [
                        {"host_ip": "127.0.0.1", "host_port": 55434}
                    ]
                },
            },
            {
                "name": "forwin-qdrant",
                "container_id": "qdrant-id",
                "compose_service": "qdrant",
                "published_ports": {
                    "6333/tcp": [
                        {"host_ip": "127.0.0.1", "host_port": 16337}
                    ]
                },
            },
        ],
    }

    with pytest.raises(l200.FreezeViolation) as raised:
        l200.connection_bindings(selected, freeze)

    assert raised.value.category == "config"


def test_chapter_csv_has_one_row_per_chapter() -> None:
    body = l200.chapter_csv(mcp_state()["chapters"])
    assert len(body.splitlines()) == 201
    assert body.splitlines()[0].startswith("chapter_number,title,status")


def test_runtime_configuration_drift_invalidates_config_freeze() -> None:
    initial = {
        "freeze_identity": {
            "compose_project": "stack",
            "runtime_containers": [
                {
                    "name": "api",
                    "image_id": "image",
                    "compose_service": "forwin",
                    "configuration_sha256": "before",
                }
            ],
            "publisher_browser_containers": [],
        }
    }
    current = {
        "compose_project": "stack",
        "runtime_containers": [
            {
                "name": "api",
                "image_id": "image",
                "compose_service": "forwin",
                "configuration_sha256": "after",
            }
        ],
        "publisher_browser_containers": [],
    }
    with pytest.raises(l200.FreezeViolation) as raised:
        l200.verify_runtime_configuration(initial, current)
    assert raised.value.category == "config"


def test_runtime_model_routing_drift_invalidates_freeze() -> None:
    initial = {
        "freeze_identity": {
            "compose_project": "stack",
            "runtime_containers": [],
            "publisher_browser_containers": [],
            "dependency_containers": [],
            "model_routing": {"routing_sha256": "before"},
        }
    }
    current = {
        "compose_project": "stack",
        "runtime_containers": [],
        "publisher_browser_containers": [],
        "dependency_containers": [],
        "model_routing": {"routing_sha256": "after"},
    }

    with pytest.raises(l200.FreezeViolation) as raised:
        l200.verify_runtime_configuration(initial, current)

    assert raised.value.category == "model_routing"


def test_runtime_container_replacement_invalidates_config_freeze() -> None:
    initial = {
        "freeze_identity": {
            "compose_project": "stack",
            "runtime_containers": [
                {
                    "name": "api",
                    "container_id": "before",
                    "image_id": "image",
                    "compose_service": "forwin",
                    "configuration_sha256": "config",
                }
            ],
            "publisher_browser_containers": [],
            "dependency_containers": [],
            "model_routing": {},
        }
    }
    current = {
        "compose_project": "stack",
        "runtime_containers": [
            {
                "name": "api",
                "container_id": "after",
                "image_id": "image",
                "compose_service": "forwin",
                "configuration_sha256": "config",
            }
        ],
        "publisher_browser_containers": [],
        "dependency_containers": [],
        "model_routing": {},
    }

    with pytest.raises(l200.FreezeViolation) as raised:
        l200.verify_runtime_configuration(initial, current)

    assert raised.value.category == "config"


def test_candidate_mount_policy_rejects_host_bind_injection() -> None:
    identities = [
        {
            "name": "forwin-api",
            "compose_service": "forwin",
            "mounts": [
                {
                    "type": "bind",
                    "source": "/host/forwin",
                    "destination": "/app/forwin",
                    "rw": False,
                }
            ],
        }
    ]

    with pytest.raises(l200.FreezeViolation) as raised:
        l200.verify_candidate_mount_policy(identities)

    assert raised.value.category == "config"


def test_candidate_mount_policy_requires_one_shared_runtime_data_volume() -> None:
    identities = [
        {
            "name": service,
            "compose_service": service,
            "mounts": (
                []
                if service == "forwin-mcp"
                else [
                    {
                        "type": "volume",
                        "source": "forwin-data",
                        "destination": "/app/data",
                        "rw": True,
                    }
                ]
            ),
        }
        for service in l200.EXPECTED_RUNTIME_SERVICES
    ]
    identities.append(
        {
            "name": "publisher-browser",
            "compose_service": "publisher-browser",
            "mounts": [
                {
                    "type": "volume",
                    "source": "forwin-data",
                    "destination": "/app/data",
                    "rw": True,
                }
            ],
        }
    )

    l200.verify_candidate_mount_policy(identities)
    identities[-1]["mounts"][0]["source"] = "other-data"
    with pytest.raises(l200.FreezeViolation, match="shared data volume"):
        l200.verify_candidate_mount_policy(identities)


def test_live_schema_drift_invalidates_schema_freeze(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        l200,
        "database_schema_identity",
        lambda _url: {"alembic_revision": "head", "schema_sha256": "after"},
    )
    with pytest.raises(l200.FreezeViolation) as raised:
        l200.verify_database_schema(
            {
                "live_schema_identity": {
                    "alembic_revision": "head",
                    "schema_sha256": "before",
                }
            },
            "unused",
        )
    assert raised.value.category == "schema"


def test_empty_measurement_reports_fail_closed() -> None:
    assert l200.gate_report_violations(
        {"schema_version": 1, "scope": "band"},
        project_id="project-200",
        scope="band",
        band_id="band-1",
    )
    assert l200.cost_report_violations(
        {"schema_version": 1},
        project_id="project-200",
        band_id="band-1",
    )
    assert l200.rule_report_violations(
        {"schema_version": 1}, project_id="project-200"
    )


def test_checkpoint_chain_rejects_late_or_tampered_evidence(tmp_path: Path) -> None:
    initialized = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    manifest = {
        "run": "forwin-v5-final-l200-no-hotfix",
        "project_id": "project-200",
        "initialized_at": initialized.isoformat(),
        "rc_manifest": {"sha256": "rc"},
        "checkpoints": [],
    }
    manifest["checkpoint_chain_anchor"] = l200.checkpoint_chain_anchor(manifest)
    checkpoint_path = tmp_path / "checkpoints" / "025.json"
    payload = {
        "requested_checkpoint": 25,
        "accepted_at_observation": 25,
        "observed_at": (initialized + timedelta(minutes=1)).isoformat(),
    }
    l200.write_json(checkpoint_path, payload)
    entry = {
        "chapter": 25,
        "accepted_at_observation": 25,
        "observed_at": payload["observed_at"],
        "path": "checkpoints/025.json",
        "sha256": l200.sha256_file(checkpoint_path),
        "previous_chain_sha256": manifest["checkpoint_chain_anchor"],
    }
    entry["chain_sha256"] = l200.canonical_hash(entry)
    manifest["checkpoints"] = [entry]
    l200.verify_checkpoint_artifact_entries(tmp_path, manifest)

    entry["accepted_at_observation"] = 50
    with pytest.raises(l200.FreezeViolation, match="manifest/payload mismatch"):
        l200.verify_checkpoint_artifact_entries(tmp_path, manifest)


def test_frozen_rc_manifest_reverifies_release_evidence_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = frozen_rc_manifest(tmp_path)
    source_sha = manifest["source"]["sha"]

    def fake_command(*command: str) -> str:
        if command[1:3] == ("cat-file", "-t"):
            return "tag"
        if command[1:3] == ("rev-parse", "refs/tags/v5.0.0-rc1^{}"):
            return source_sha
        if command[1:3] == ("rev-parse", "refs/tags/v5.0.0-rc1"):
            return "c" * 40
        raise AssertionError(command)

    monkeypatch.setattr(l200, "command", fake_command)
    evidence_path = Path(
        manifest["release_candidate"]["evidence"]["live_recovery"]["path"]
    )
    evidence_path.write_text('{"source_sha":"tampered","result":"pass"}', encoding="utf-8")

    selected = argparse.Namespace(quality_profile="standard", gate_delegate="human")
    with pytest.raises(l200.EvidenceError, match="evidence file hash mismatch"):
        l200.validate_frozen_rc_manifest(selected, manifest)


def test_frozen_rc_manifest_requires_v1_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = frozen_rc_manifest(tmp_path)
    source_sha = manifest["source"]["sha"]
    del manifest["release_candidate"]["evidence"]["v1_preflight"]

    def fake_command(*command: str) -> str:
        if command[1:3] == ("cat-file", "-t"):
            return "tag"
        if command[1:3] == ("rev-parse", "refs/tags/v5.0.0-rc1^{}"):
            return source_sha
        if command[1:3] == ("rev-parse", "refs/tags/v5.0.0-rc1"):
            return "c" * 40
        raise AssertionError(command)

    monkeypatch.setattr(l200, "command", fake_command)
    selected = argparse.Namespace(quality_profile="standard", gate_delegate="human")

    with pytest.raises(l200.EvidenceError, match="v1_preflight"):
        l200.validate_frozen_rc_manifest(selected, manifest)


def test_frozen_rc_manifest_reverifies_its_collector(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = frozen_rc_manifest(tmp_path)
    source_sha = manifest["source"]["sha"]

    def fake_command(*command: str) -> str:
        if command[1:3] == ("cat-file", "-t"):
            return "tag"
        if command[1:3] == ("rev-parse", "refs/tags/v5.0.0-rc1^{}"):
            return source_sha
        if command[1:3] == ("rev-parse", "refs/tags/v5.0.0-rc1"):
            return "c" * 40
        raise AssertionError(command)

    monkeypatch.setattr(l200, "command", fake_command)
    manifest["collector"]["sha256"] = "0" * 64
    selected = argparse.Namespace(quality_profile="standard", gate_delegate="human")

    with pytest.raises(l200.EvidenceError, match="collector hash mismatch"):
        l200.validate_frozen_rc_manifest(selected, manifest)


def test_frozen_rc_manifest_rejects_shallow_release_gate_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = frozen_rc_manifest(tmp_path)
    source_sha = manifest["source"]["sha"]

    def fake_command(*command: str) -> str:
        if command[1:3] == ("cat-file", "-t"):
            return "tag"
        if command[1:3] == ("rev-parse", "refs/tags/v5.0.0-rc1^{}"):
            return source_sha
        if command[1:3] == ("rev-parse", "refs/tags/v5.0.0-rc1"):
            return "c" * 40
        raise AssertionError(command)

    monkeypatch.setattr(l200, "command", fake_command)
    selected = argparse.Namespace(quality_profile="standard", gate_delegate="human")

    with pytest.raises(l200.EvidenceError, match="release gate evidence invalid"):
        l200.validate_frozen_rc_manifest(selected, manifest)


def test_frozen_rc_manifest_rejects_shallow_smoke_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = frozen_rc_manifest(tmp_path)
    source_sha = manifest["source"]["sha"]

    def fake_command(*command: str) -> str:
        if command[1:3] == ("cat-file", "-t"):
            return "tag"
        if command[1:3] == ("rev-parse", "refs/tags/v5.0.0-rc1^{}"):
            return source_sha
        if command[1:3] == ("rev-parse", "refs/tags/v5.0.0-rc1"):
            return "c" * 40
        raise AssertionError(command)

    monkeypatch.setattr(l200, "command", fake_command)
    selected = argparse.Namespace(quality_profile="standard", gate_delegate="human")

    with pytest.raises(l200.EvidenceError, match="post-decision smoke evidence invalid"):
        l200.validate_frozen_rc_manifest(selected, manifest)


def test_frozen_rc_manifest_rejects_shallow_recovery_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = frozen_rc_manifest(tmp_path)
    source_sha = manifest["source"]["sha"]

    def fake_command(*command: str) -> str:
        if command[1:3] == ("cat-file", "-t"):
            return "tag"
        if command[1:3] == ("rev-parse", "refs/tags/v5.0.0-rc1^{}"):
            return source_sha
        if command[1:3] == ("rev-parse", "refs/tags/v5.0.0-rc1"):
            return "c" * 40
        raise AssertionError(command)

    monkeypatch.setattr(l200, "command", fake_command)
    selected = argparse.Namespace(quality_profile="standard", gate_delegate="human")

    with pytest.raises(l200.EvidenceError, match="live recovery evidence invalid"):
        l200.validate_frozen_rc_manifest(selected, manifest)


def test_frozen_rc_manifest_rejects_nonfinal_matrix_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = frozen_rc_manifest(tmp_path)
    source_sha = manifest["source"]["sha"]

    def fake_command(*command: str) -> str:
        if command[1:3] == ("cat-file", "-t"):
            return "tag"
        if command[1:3] == ("rev-parse", "refs/tags/v5.0.0-rc1^{}"):
            return source_sha
        if command[1:3] == ("rev-parse", "refs/tags/v5.0.0-rc1"):
            return "c" * 40
        raise AssertionError(command)

    monkeypatch.setattr(l200, "command", fake_command)
    selected = argparse.Namespace(quality_profile="standard", gate_delegate="human")

    with pytest.raises(l200.EvidenceError, match="matrix evidence is not a final pass"):
        l200.validate_frozen_rc_manifest(selected, manifest)


def test_final_artifact_set_revalidates_deterministic_report(
    tmp_path: Path,
) -> None:
    manifest = write_finalized_output(tmp_path)

    summary = l200.verify_final_artifact_set(tmp_path, manifest)

    assert summary["accepted"] == 200
    assert summary["active_generation_task"] is False


def test_final_artifact_set_rejects_forged_report_with_updated_hash(
    tmp_path: Path,
) -> None:
    manifest = write_finalized_output(tmp_path)
    report_path = tmp_path / "final-report.md"
    report_path.write_text("# Forged L200 PASS\n", encoding="utf-8")
    manifest["artifacts"]["final-report.md"] = l200.sha256_file(report_path)

    with pytest.raises(l200.EvidenceError, match="report content mismatch"):
        l200.verify_final_artifact_set(tmp_path, manifest)


def test_final_artifact_set_rejects_rewritten_chapter_evidence(
    tmp_path: Path,
) -> None:
    manifest = write_finalized_output(tmp_path)
    chapter_path = tmp_path / "chapter-status.csv"
    body = chapter_path.read_text(encoding="utf-8")
    chapter_path.write_text(
        body.replace("200,Chapter 200,accepted", "200,Chapter 200,needs_review"),
        encoding="utf-8",
    )
    manifest["artifacts"]["chapter-status.csv"] = l200.sha256_file(chapter_path)

    with pytest.raises(l200.EvidenceError, match="accepted chapters 1..200"):
        l200.verify_final_artifact_set(tmp_path, manifest)


def test_final_artifact_set_classifies_malformed_hashed_payload(
    tmp_path: Path,
) -> None:
    manifest = write_finalized_output(tmp_path)
    projection_path = tmp_path / "projection-publisher.json"
    projection = l200.load_json(projection_path)
    projection["projections"] = "not-a-projection-list"
    l200.write_json(projection_path, projection)
    manifest["artifacts"]["projection-publisher.json"] = l200.sha256_file(
        projection_path
    )

    with pytest.raises(l200.EvidenceError, match="payload is malformed"):
        l200.verify_final_artifact_set(tmp_path, manifest)
