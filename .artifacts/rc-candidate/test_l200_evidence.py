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


def test_l200_cli_exposes_bootstrap_and_continuous_monitor() -> None:
    completed = subprocess.run(
        [sys.executable, str(MODULE_PATH), "--help"],
        cwd=MODULE_PATH.parents[2],
        env={
            key: value
            for key, value in os.environ.items()
            if key not in {"PYTHONHOME", "PYTHONPATH"}
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "bootstrap" in completed.stdout
    assert "monitor" in completed.stdout


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


def test_collect_mcp_state_unwraps_report_tool_result_envelopes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeClientContext:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_args: object) -> None:
            return None

    project = {"id": "project-200"}
    chapters = [{"chapter_number": 1, "status": "accepted"}]
    active = {"has_active_generation_task": False}
    tasks = {"tasks": [{"project_id": "project-200", "id": "task-1"}]}
    reports = {
        "gate_ledger_report": gate_report(),
        "cost_report": cost_report(),
        "rule_provenance_report": rule_report(),
    }

    async def fake_call_mcp(
        _client: object,
        name: str,
        _arguments: dict,
    ) -> object:
        direct = {
            "project_get": project,
            "chapter_list": chapters,
            "task_active_generation_check": active,
            "task_list": tasks,
        }
        if name in reports:
            return {"result": reports[name]}
        return direct[name]

    monkeypatch.setattr(
        l200,
        "direct_mcp_client",
        lambda _url: FakeClientContext(),
    )
    monkeypatch.setattr(l200, "call_mcp", fake_call_mcp)

    state = asyncio.run(
        l200.collect_mcp_state(
            argparse.Namespace(
                mcp_url="http://127.0.0.1:18897/mcp",
                project_id="project-200",
            )
        )
    )

    assert state["project"] == project
    assert state["chapters"] == chapters
    assert state["active_task_check"] == active
    assert state["tasks"] == tasks["tasks"]
    assert state["gate_ledger"] == reports["gate_ledger_report"]
    assert state["cost_report"] == reports["cost_report"]
    assert state["rule_provenance"] == reports["rule_provenance_report"]


@pytest.mark.parametrize(
    "payload",
    (
        {},
        {"result": None},
        {"result": []},
        {"result": {}, "copied": {}},
    ),
)
def test_mcp_report_result_rejects_noncanonical_envelopes(payload: object) -> None:
    with pytest.raises(l200.EvidenceError, match="report result envelope"):
        l200.mcp_report_result(payload, tool_name="cost_report")


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


def test_collect_completed_band_reports_unwraps_report_tool_result_envelopes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class FakeClientContext:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_args: object) -> None:
            return None

    band_id = "band-1"
    reports = {
        "gate_ledger_report": gate_report(scope="band", band_id=band_id),
        "cost_report": cost_report(band_id=band_id),
        "rule_provenance_report": rule_report(),
    }

    async def fake_call_mcp(
        _client: object,
        name: str,
        _arguments: dict,
    ) -> object:
        return {"result": reports[name]}

    monkeypatch.setattr(
        l200,
        "direct_mcp_client",
        lambda _url: FakeClientContext(),
    )
    monkeypatch.setattr(l200, "call_mcp", fake_call_mcp)

    entries = asyncio.run(
        l200.collect_completed_band_reports(
            argparse.Namespace(
                mcp_url="http://127.0.0.1:18897/mcp",
                project_id="project-200",
            ),
            bands=[
                {
                    "band_id": band_id,
                    "chapter_start": 1,
                    "chapter_end": 25,
                    "status": "pass",
                }
            ],
            output_dir=tmp_path,
            run_manifest={
                "initialized_at": "2026-07-23T12:00:00+00:00",
                "rule_provenance": reports["rule_provenance_report"],
                "rule_state_hash": l200.canonical_hash(
                    l200.frozen_rule_state(reports["rule_provenance_report"])
                ),
            },
            accepted_at_collection=25,
        )
    )

    evidence_dir = Path(entries[0]["directory"])
    assert json.loads(
        (evidence_dir / "gate-ledger.json").read_text(encoding="utf-8")
    ) == reports["gate_ledger_report"]
    assert json.loads(
        (evidence_dir / "cost-report.json").read_text(encoding="utf-8")
    ) == reports["cost_report"]
    assert json.loads(
        (evidence_dir / "rule-provenance.json").read_text(encoding="utf-8")
    ) == reports["rule_provenance_report"]


def run_manifest(directory: Path) -> dict:
    return {
        "initialized_at": "2026-07-23T12:00:00+00:00",
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
        "rule_provenance": rule_report(),
        "rule_state_hash": l200.canonical_hash(
            l200.frozen_rule_state(rule_report())
        ),
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
            "duplicate_ids": 0,
            "duplicate_project_chapter_versions": 0,
            "duplicate_candidate_draft_refs": 0,
            "invalid_identity_rows": 0,
            "chapter_plan_identity_mismatches": 0,
            "chapter_draft_identity_mismatches": 0,
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
    l200.write_json(
        directory / "metadata.json",
        {
            "schema_version": 1,
            "project_id": "project-200",
            "band": {
                "band_id": "band-all",
                "chapter_start": 1,
                "chapter_end": 200,
                "status": "pass",
            },
            "s1_scope": "band",
            "s3_scope": "band",
            "s2_scope": "frozen_project_snapshot_linked_to_band",
            "s2_frozen_at": "2026-07-23T12:00:00+00:00",
            "s2_rule_state_sha256": l200.canonical_hash(
                l200.frozen_rule_state(rule_report())
            ),
            "s2_rule_provenance_sha256": l200.canonical_hash(rule_report()),
            "s2_continuity_protocol": l200.ATTESTATION_PROTOCOL,
            "s2_band_chapter_end": 200,
            "s2_collected_at_accepted_count": 200,
        },
    )


def write_finalized_output(directory: Path) -> dict:
    mcp = mcp_state()
    database = database_state()
    band_directory = directory / "bands" / "001-200-test"
    write_band_reports(band_directory)
    manifest = {
        "schema_version": 1,
        "run": "forwin-v5-final-l200-no-hotfix",
        "valid": True,
        "initialized_at": "2026-07-23T12:00:00+00:00",
        "finalized_at": "2026-07-23T12:00:10+00:00",
        "project_id": "project-200",
        "expected_target": 200,
        "quality_profile": "standard",
        "gate_delegate": "human",
        "result": "pass",
        "violations": [],
        "code_changes_during_run": 0,
        "freeze_audit_state": database["freeze_audit"],
        "policy_hash": "d" * 64,
        "rule_provenance": rule_report(),
        "rule_state_hash": l200.canonical_hash(
            l200.frozen_rule_state(rule_report())
        ),
        "freeze_identity": {"source_sha": "a" * 40, "source_tree": "b" * 40},
        "final_freeze_identity": {"source_sha": "a" * 40, "source_tree": "b" * 40},
        "connection_bindings": {"compose_project": "candidate"},
        "final_connection_bindings": {"compose_project": "candidate"},
        "live_schema_identity": {"sha256": "b" * 64},
        "final_live_schema_identity": {"sha256": "b" * 64},
        "collector": {
            "path": str(MODULE_PATH.resolve()),
            "sha256": l200.sha256_file(MODULE_PATH),
        },
        "rc_manifest": {"path": "/tmp/rc.json", "sha256": "c" * 64},
        "attestation": {
            "protocol": l200.ATTESTATION_PROTOCOL,
            "directory": "attestation",
            "max_gap_seconds": l200.ATTESTATION_MAX_GAP_SECONDS,
        },
        "bootstrap_audit_state": {"event_count": 14, "event_sha256": "f" * 64},
        "final_bootstrap_audit_state": {
            "event_count": 14,
            "event_sha256": "f" * 64,
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
    transcript = _bootstrap_transcript()
    transcript["collector_sha256"] = manifest["collector"]["sha256"]
    l200.write_json(directory / l200.BOOTSTRAP_TRANSCRIPT_NAME, transcript)
    bootstrap_summary = l200.verify_bootstrap_transcript(
        transcript,
        project_id="project-200",
        source_sha="a" * 40,
        source_tree="b" * 40,
        rc_manifest_sha256="c" * 64,
        collector_sha256=manifest["collector"]["sha256"],
        mcp_target_sha256="e" * 64,
    )
    manifest["bootstrap_transcript"] = {
        "path": l200.BOOTSTRAP_TRANSCRIPT_NAME,
        "sha256": l200.sha256_file(directory / l200.BOOTSTRAP_TRANSCRIPT_NAME),
        "mcp_target_sha256": "e" * 64,
        "summary": bootstrap_summary,
    }
    _write_attestation_chain(directory, manifest)
    manifest["final_attestation"] = l200.verify_attestation_protocol(
        directory,
        manifest,
        require_terminated=True,
    )
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
            follow_redirects=True,
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
    monkeypatch.setenv("FORWIN_HTTP_BASIC_USER", "release-operator")
    monkeypatch.setenv("FORWIN_HTTP_BASIC_PASSWORD", "release-password")

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
    auth = options.pop("auth")
    authenticated = next(
        auth.auth_flow(l200.httpx.Request("GET", "http://forwin.invalid"))
    )
    assert authenticated.headers["Authorization"].startswith("Basic ")
    assert options == {
        "base_url": "http://127.0.0.1:18899",
        "timeout": 60,
        "trust_env": False,
        "follow_redirects": False,
    }


def test_policy_fetch_rejects_partial_basic_auth_before_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FORWIN_HTTP_BASIC_USER", "release-operator")
    monkeypatch.delenv("FORWIN_HTTP_BASIC_PASSWORD", raising=False)

    def unexpected_client(**_kwargs):
        raise AssertionError("HTTP client must not be created")

    monkeypatch.setattr(l200.httpx, "AsyncClient", unexpected_client)

    with pytest.raises(l200.EvidenceError, match="must be set together"):
        asyncio.run(
            l200.fetch_policy("http://127.0.0.1:18899", "project-generic")
        )


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


@pytest.fixture
def complete_frozen_rc_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict:
    """Use real evidence validators; replace only Git's external source record."""
    fixture_path = Path(__file__).with_name("test_collect_rc_manifest.py")
    spec = importlib.util.spec_from_file_location("rc_manifest_fixtures", fixture_path)
    assert spec is not None and spec.loader is not None
    fixtures = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixtures)
    source_sha, source_tree = fixtures.SOURCE_SHA, fixtures.SOURCE_TREE
    original_run = subprocess.run

    def git_record(command, **kwargs):
        if command[0] != "git":
            return original_run(command, **kwargs)
        args = list(command[1:])
        if args == ["cat-file", "-t", "refs/tags/v5.0.0-rc1"]:
            output = "tag"
        elif args == ["rev-parse", "refs/tags/v5.0.0-rc1^{}"]:
            output = source_sha
        elif args == ["rev-parse", "refs/tags/v5.0.0-rc1"]:
            output = "d" * 40
        elif args == ["rev-parse", "--verify", f"{source_sha}^{{commit}}"]:
            output = source_sha
        elif args == ["rev-parse", "--verify", f"{source_sha}^{{tree}}"]:
            output = source_tree
        elif args[:2] == ["ls-files", "--error-unmatch"]:
            output = args[2]
        elif args[:1] == ["hash-object"]:
            return original_run(command, **kwargs)
        elif args[:1] == ["rev-parse"] and args[1].startswith(source_sha + ":"):
            path = MODULE_PATH.parents[2] / args[1].split(":", 1)[1]
            return original_run(["git", "hash-object", str(path)], **kwargs)
        else:
            return subprocess.CompletedProcess(command, 128, "", "unknown commit")
        return subprocess.CompletedProcess(command, 0, output + "\n", "")

    monkeypatch.setattr(subprocess, "run", git_record)
    manifest = frozen_rc_manifest(tmp_path)
    final_args = fixtures.final_args(tmp_path)
    candidate = json.loads((tmp_path / "shared-candidate.json").read_text())
    manifest["schema_version"] = 3
    manifest["collected_at"] = "2026-07-23T12:00:00+00:00"
    manifest["source"]["tree"] = source_tree
    manifest["images"] = candidate["images"]
    for service in manifest["model_profiles"]["effective_container_fields"]["services"].values():
        service["runtime_image_id"] = candidate["images"]["runtime"]["image_id"]
    manifest["release_harness"] = fixtures.collector.tree_revision(
        *fixtures.collector.RELEASE_HARNESS_PATHS
    )
    matrix_path = tmp_path / "matrix-audit.json"
    fixtures.write_matrix_audit(matrix_path)
    manifest["matrix_evidence"] = fixtures.collector.load_matrix_manifest(
        matrix_path, source_sha, require_final_audit=True
    )
    manifest["release_candidate"] = fixtures.collector.collect_release_candidate(
        final_args, source_sha
    )
    return manifest


@pytest.mark.parametrize("with_tag", [False, True])
def test_frozen_rc_accepts_complete_commit_record(
    complete_frozen_rc_manifest: dict,
    with_tag: bool,
) -> None:
    manifest = complete_frozen_rc_manifest
    if not with_tag:
        manifest["release_candidate"]["annotated_tag"] = ""
        manifest["release_candidate"]["tag_object_sha"] = ""

    result = l200.validate_frozen_rc_manifest(
        argparse.Namespace(quality_profile="standard", gate_delegate="human"),
        manifest,
    )

    assert result["source_sha"] == "a" * 40
    assert result["annotated_tag"] == ("v5.0.0-rc1" if with_tag else "")


@pytest.mark.parametrize(
    ("field", "value"),
    [("sha", ""), ("sha", "a" * 7), ("sha", "HEAD"), ("tree", ""), ("tree", "f" * 40)],
)
def test_tagless_frozen_rc_rejects_invalid_commit_record(
    complete_frozen_rc_manifest: dict,
    field: str,
    value: str,
) -> None:
    manifest = complete_frozen_rc_manifest
    manifest["release_candidate"]["annotated_tag"] = ""
    manifest["release_candidate"]["tag_object_sha"] = ""
    manifest["source"][field] = value

    with pytest.raises(l200.EvidenceError, match="source|commit"):
        l200.validate_frozen_rc_manifest(
            argparse.Namespace(quality_profile="standard", gate_delegate="human"),
            manifest,
        )


@pytest.mark.parametrize("missing", ["source", "evidence", "image", "clean_tree"])
def test_tagless_frozen_rc_preserves_release_requirements(
    complete_frozen_rc_manifest: dict,
    missing: str,
) -> None:
    manifest = complete_frozen_rc_manifest
    manifest["release_candidate"]["annotated_tag"] = ""
    manifest["release_candidate"]["tag_object_sha"] = ""
    if missing == "source":
        manifest["release_candidate"]["source_sha"] = "f" * 40
    elif missing == "evidence":
        del manifest["release_candidate"]["evidence"]["live_recovery"]
    elif missing == "image":
        manifest["images"]["runtime"]["revision"] = "f" * 40
    else:
        manifest["source"]["tracked_worktree_clean"] = False

    with pytest.raises(l200.EvidenceError):
        l200.validate_frozen_rc_manifest(
            argparse.Namespace(quality_profile="standard", gate_delegate="human"),
            manifest,
        )


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


def _write_attestation_chain(
    directory: Path,
    manifest: dict,
    *,
    observation_result: str = "pass",
    observation_started_at: datetime | None = None,
    observation_completed_at: datetime | None = None,
    termination_started_at: datetime | None = None,
    session_created_at: datetime | None = None,
) -> None:
    initialized = datetime.fromisoformat(manifest["initialized_at"])
    session_created_at = session_created_at or initialized + timedelta(seconds=1)
    observation_started_at = observation_started_at or initialized + timedelta(seconds=5)
    observation_completed_at = observation_completed_at or initialized + timedelta(seconds=6)
    termination_started_at = termination_started_at or initialized + timedelta(seconds=7)
    root = directory / "attestation"
    records = root / "records"
    records.mkdir(parents=True)
    (root / ".collector.lock").touch()
    session = {
        "schema_version": 1,
        "protocol": "forwin-l200-continuous-attestation-v1",
        "collector_session_id": "collector-session-1",
        "created_at": session_created_at.isoformat(),
        "run_anchor": l200.attestation_run_anchor(manifest),
        "collector": dict(manifest["collector"]),
        "rc_manifest": dict(manifest["rc_manifest"]),
        "max_gap_seconds": l200.ATTESTATION_MAX_GAP_SECONDS,
        "interval_seconds": l200.ATTESTATION_DEFAULT_INTERVAL_SECONDS,
    }
    l200.write_json(root / "session.json", session)
    session_sha256 = l200.sha256_file(root / "session.json")
    observation = {
        "schema_version": 1,
        "sequence": 1,
        "record_type": "observation",
        "collector_session_id": session["collector_session_id"],
        "session_sha256": session_sha256,
        "previous_record_sha256": session_sha256,
        "started_at": observation_started_at.isoformat(),
        "completed_at": observation_completed_at.isoformat(),
        "result": observation_result,
        "checks": l200.attestation_expected_checks(manifest),
        "failure_category": "code" if observation_result == "fail" else "",
        "failure_reason": "observed dirty tree" if observation_result == "fail" else "",
    }
    observation["record_sha256"] = l200.attestation_record_hash(observation)
    l200.write_json(records / "00000001.json", observation)
    termination = {
        "schema_version": 1,
        "sequence": 2,
        "record_type": "termination",
        "collector_session_id": session["collector_session_id"],
        "session_sha256": session_sha256,
        "previous_record_sha256": observation["record_sha256"],
        "started_at": termination_started_at.isoformat(),
        "completed_at": (termination_started_at + timedelta(seconds=1)).isoformat(),
        "result": "pass" if observation_result == "pass" else "fail",
        "termination_reason": "signal",
    }
    termination["record_sha256"] = l200.attestation_record_hash(termination)
    l200.write_json(records / "00000002.json", termination)
    state = {
        "schema_version": 1,
        "collector_session_id": session["collector_session_id"],
        "session_sha256": session_sha256,
        "status": "terminated" if observation_result == "pass" else "failed",
        "sequence": 2,
        "chain_head": termination["record_sha256"],
        "started_at": session["created_at"],
        "last_observed_at": observation_completed_at.isoformat(),
        "terminated_at": termination["completed_at"],
        "termination_reason": "signal" if observation_result == "pass" else "drift",
        "permanent_failure": observation_result != "pass",
    }
    l200.write_json(root / "state.json", state)


def _attestation_manifest() -> dict:
    return {
        "run": "forwin-v5-final-l200-no-hotfix",
        "project_id": "project-200",
        "initialized_at": "2026-07-23T12:00:00+00:00",
        "collector": {"path": str(MODULE_PATH.resolve()), "sha256": "a" * 64},
        "rc_manifest": {"path": "/tmp/rc.json", "sha256": "b" * 64},
        "freeze_identity": {"source_sha": "a" * 40, "source_tree": "b" * 40},
        "connection_bindings": {"compose_project": "candidate"},
        "live_schema_identity": {"schema_sha256": "c" * 64},
        "policy_hash": "d" * 64,
        "rule_state_hash": "e" * 64,
        "freeze_audit_state": {"event_sha256": "f" * 64},
        "bootstrap_audit_state": {"event_sha256": "1" * 64},
        "attestation": {
            "protocol": "forwin-l200-continuous-attestation-v1",
            "directory": "attestation",
            "max_gap_seconds": l200.ATTESTATION_MAX_GAP_SECONDS,
        },
    }


def test_continuous_attestation_requires_complete_normal_termination(
    tmp_path: Path,
) -> None:
    manifest = _attestation_manifest()
    _write_attestation_chain(tmp_path, manifest)

    summary = l200.verify_attestation_protocol(
        tmp_path,
        manifest,
        require_terminated=True,
        reference_time=datetime(2026, 7, 23, 12, 0, 9, tzinfo=UTC),
    )

    assert summary["status"] == "terminated"
    assert summary["observation_count"] == 1
    assert summary["permanent_failure"] is False


def test_continuous_attestation_rejects_hash_chained_incomplete_pass_checks(
    tmp_path: Path,
) -> None:
    manifest = _attestation_manifest()
    _write_attestation_chain(tmp_path, manifest)
    root = tmp_path / "attestation"
    observation_path = root / "records" / "00000001.json"
    termination_path = root / "records" / "00000002.json"
    observation = l200.load_json(observation_path)
    observation["checks"].pop("schema_identity_sha256")
    observation["record_sha256"] = l200.attestation_record_hash(observation)
    l200.write_json(observation_path, observation)
    termination = l200.load_json(termination_path)
    termination["previous_record_sha256"] = observation["record_sha256"]
    termination["record_sha256"] = l200.attestation_record_hash(termination)
    l200.write_json(termination_path, termination)
    state = l200.load_json(root / "state.json")
    state["chain_head"] = termination["record_sha256"]
    l200.write_json(root / "state.json", state)

    with pytest.raises(l200.FreezeViolation, match="pass checks mismatch"):
        l200.verify_attestation_protocol(
            tmp_path,
            manifest,
            require_terminated=True,
        )


def test_continuous_attestation_rejects_bounded_gap_and_observed_revert(
    tmp_path: Path,
) -> None:
    manifest = _attestation_manifest()
    initialized = datetime.fromisoformat(manifest["initialized_at"])
    _write_attestation_chain(
        tmp_path,
        manifest,
        observation_started_at=initialized + timedelta(seconds=5),
        observation_completed_at=initialized + timedelta(seconds=6),
        termination_started_at=initialized
        + timedelta(seconds=l200.ATTESTATION_MAX_GAP_SECONDS + 7),
    )
    with pytest.raises(l200.FreezeViolation, match="attestation gap"):
        l200.verify_attestation_protocol(
            tmp_path,
            manifest,
            require_terminated=True,
            reference_time=initialized
            + timedelta(seconds=l200.ATTESTATION_MAX_GAP_SECONDS + 9),
        )
    shutil.rmtree(tmp_path / "attestation")
    _write_attestation_chain(tmp_path, manifest, observation_result="fail")
    with pytest.raises(l200.FreezeViolation, match="permanent failure"):
        l200.verify_attestation_protocol(
            tmp_path,
            manifest,
            require_terminated=True,
            reference_time=initialized + timedelta(seconds=9),
        )


def test_continuous_attestation_must_start_within_first_bounded_gap(
    tmp_path: Path,
) -> None:
    manifest = _attestation_manifest()
    initialized = datetime.fromisoformat(manifest["initialized_at"])
    session_started = initialized + timedelta(
        seconds=l200.ATTESTATION_MAX_GAP_SECONDS + 1
    )
    _write_attestation_chain(
        tmp_path,
        manifest,
        session_created_at=session_started,
        observation_started_at=session_started + timedelta(seconds=1),
        observation_completed_at=session_started + timedelta(seconds=2),
        termination_started_at=session_started + timedelta(seconds=3),
    )

    with pytest.raises(l200.FreezeViolation, match="gap from init"):
        l200.verify_attestation_protocol(
            tmp_path,
            manifest,
            require_terminated=True,
            reference_time=session_started + timedelta(seconds=5),
        )


def test_continuous_attestation_rejects_running_or_identity_swapped_finalizer(
    tmp_path: Path,
) -> None:
    manifest = _attestation_manifest()
    _write_attestation_chain(tmp_path, manifest)
    root = tmp_path / "attestation"
    (root / "records" / "00000002.json").unlink()
    observation = l200.load_json(root / "records" / "00000001.json")
    state = l200.load_json(root / "state.json")
    state.update(
        {
            "status": "running",
            "sequence": 1,
            "chain_head": observation["record_sha256"],
            "terminated_at": "",
            "termination_reason": "",
        }
    )
    l200.write_json(root / "state.json", state)
    with pytest.raises(l200.FreezeViolation, match="did not terminate normally"):
        l200.verify_attestation_protocol(
            tmp_path,
            manifest,
            require_terminated=True,
        )

    session = l200.load_json(root / "session.json")
    session["collector"]["sha256"] = "0" * 64
    l200.write_json(root / "session.json", session)
    with pytest.raises(l200.FreezeViolation, match="session identity mismatch"):
        l200.verify_attestation_protocol(
            tmp_path,
            manifest,
            require_terminated=False,
        )

def _bootstrap_transcript() -> dict:
    run_id = "bootstrap-run-1"
    project_id = "project-200"
    sequence = [("project_create", ""), ("project_policy_update", "")]
    for stage in l200.GENESIS_STAGES:
        sequence.extend(
            [("genesis_stage_generate", stage), ("genesis_stage_lock", stage)]
        )
    operations = []
    previous = "0" * 64
    for index, (tool, stage) in enumerate(sequence, start=1):
        operation = {
            "index": index,
            "recorded_at": f"2026-07-23T12:00:{index:02d}+00:00",
            "run_id": run_id,
            "tool": tool,
            "transport": "http" if tool == "project_policy_update" else "mcp_http",
            "request_id": f"request-{index}",
            "arguments_sha256": f"{index:064x}",
            "result_sha256": f"{index + 100:064x}",
            "project_id": project_id,
            "stage_key": stage,
            "result_ok": True,
            "previous_operation_sha256": previous,
        }
        operation["operation_sha256"] = l200.canonical_hash(operation)
        operations.append(operation)
        previous = operation["operation_sha256"]
    return {
        "schema_version": 1,
        "result": "genesis_ready",
        "source_sha": "a" * 40,
        "source_tree": "b" * 40,
        "rc_manifest_sha256": "c" * 64,
        "collector_sha256": "d" * 64,
        "mcp_target_sha256": "e" * 64,
        "run_id": run_id,
        "started_at": "2026-07-23T12:00:00+00:00",
        "completed_at": "2026-07-23T12:00:20+00:00",
        "target": 200,
        "project_id": project_id,
        "operation_count": len(operations),
        "operation_chain_head": previous,
        "operations": operations,
    }


def test_bootstrap_transcript_binds_supported_project_and_genesis_operations() -> None:
    transcript = _bootstrap_transcript()

    summary = l200.verify_bootstrap_transcript(
        transcript,
        project_id="project-200",
        source_sha="a" * 40,
        source_tree="b" * 40,
        rc_manifest_sha256="c" * 64,
        collector_sha256="d" * 64,
        mcp_target_sha256="e" * 64,
    )

    assert summary["operation_count"] == 14
    assert summary["operation_chain_head"] == transcript["operation_chain_head"]

    transcript["operations"][3]["result_ok"] = False
    with pytest.raises(l200.EvidenceError, match="operation integrity"):
        l200.verify_bootstrap_transcript(
            transcript,
            project_id="project-200",
            source_sha="a" * 40,
            source_tree="b" * 40,
            rc_manifest_sha256="c" * 64,
            collector_sha256="d" * 64,
            mcp_target_sha256="e" * 64,
        )


def test_init_binding_rejects_equivalent_database_state_without_transcript(
    tmp_path: Path,
) -> None:
    selected = argparse.Namespace(
        output_dir=tmp_path,
        project_id="project-200",
        mcp_url="http://127.0.0.1:18897/mcp",
    )
    rc_manifest = {"source": {"sha": "a" * 40, "tree": "b" * 40}}

    with pytest.raises(l200.EvidenceError, match="bootstrap transcript is missing"):
        l200.load_and_verify_bootstrap_transcript(
            selected,
            rc_manifest,
            rc_manifest_sha256="c" * 64,
            collector_sha256="d" * 64,
        )


def test_band_s2_is_frozen_project_snapshot_linked_to_collection_count(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class FakeClientContext:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_args: object) -> None:
            return None

    reports = {
        "gate_ledger_report": gate_report(scope="band", band_id="band-1"),
        "cost_report": cost_report(band_id="band-1"),
    }

    async def fake_call_mcp(
        _client: object,
        name: str,
        _arguments: dict,
    ) -> object:
        return {"result": reports[name]}

    monkeypatch.setattr(l200, "direct_mcp_client", lambda _url: FakeClientContext())
    monkeypatch.setattr(l200, "call_mcp", fake_call_mcp)
    manifest = {
        "initialized_at": "2026-07-23T12:00:00+00:00",
        "rule_provenance": rule_report(),
        "rule_state_hash": l200.canonical_hash(l200.frozen_rule_state(rule_report())),
    }
    entries = asyncio.run(
        l200.collect_completed_band_reports(
            argparse.Namespace(
                mcp_url="http://127.0.0.1:18897/mcp",
                project_id="project-200",
            ),
            bands=[
                {
                    "band_id": "band-1",
                    "chapter_start": 1,
                    "chapter_end": 25,
                    "status": "pass",
                }
            ],
            output_dir=tmp_path,
            run_manifest=manifest,
            accepted_at_collection=31,
        )
    )
    metadata = l200.load_json(Path(entries[0]["directory"]) / "metadata.json")

    assert metadata["s2_scope"] == "frozen_project_snapshot_linked_to_band"
    assert metadata["s2_frozen_at"] == manifest["initialized_at"]
    assert metadata["s2_collected_at_accepted_count"] == 31
    assert metadata["s2_band_chapter_end"] == 25
    assert "project_snapshot_at_band_completion" not in json.dumps(metadata)


def test_legacy_instantaneous_band_s2_claim_is_release_blocking(
    tmp_path: Path,
) -> None:
    write_band_reports(tmp_path)
    metadata_path = tmp_path / "metadata.json"
    metadata = l200.load_json(metadata_path)
    metadata["s2_scope"] = "project_snapshot_at_band_completion"
    l200.write_json(metadata_path, metadata)

    violations = l200.final_violations(
        args(), run_manifest(tmp_path), mcp_state(), database_state()
    )

    assert any("frozen S2 evidence is invalid" in item for item in violations)


@pytest.mark.parametrize(
    "metric",
    (
        "duplicate_ids",
        "duplicate_project_chapter_versions",
        "duplicate_candidate_draft_refs",
        "invalid_identity_rows",
        "chapter_plan_identity_mismatches",
        "chapter_draft_identity_mismatches",
    ),
)
def test_all_candidate_identity_failures_are_release_blocking(
    tmp_path: Path,
    metric: str,
) -> None:
    write_band_reports(tmp_path)
    database = database_state()
    database["candidates"][metric] = 1

    violations = l200.final_violations(
        args(), run_manifest(tmp_path), mcp_state(), database
    )

    assert f"candidates.{metric}=1, expected=0" in violations


def test_canon_duplicate_idempotency_sql_ignores_empty_retry_keys() -> None:
    normalized = " ".join(l200.CANON_INTEGRITY_SQL.split())

    assert "count(*) FILTER (WHERE c.idempotency_key<>'')" in normalized
    assert "count(DISTINCT c.idempotency_key) FILTER (WHERE c.idempotency_key<>'')" in normalized
