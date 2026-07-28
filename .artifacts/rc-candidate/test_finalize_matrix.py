from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
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


def test_request_level_spark_evidence_uses_matrix_schema_v4() -> None:
    assert matrix.MATRIX_AUDIT_SCHEMA_VERSION == 4


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


def gate_outcome(
    *,
    gate_kind: str,
    scope: str,
    related_object_id: str,
    chapter_number: int,
    band_id: str,
    evaluated: bool,
    decision: str,
    blocked: bool,
    trace_id: str = "",
    overridden_by: str = "",
) -> dict:
    return {
        "schema_version": 1,
        "gate_id": "delegation",
        "gate_version": "v1",
        "responsibility_domain": gate_kind,
        "scope": scope,
        "candidate_id": related_object_id,
        "chapter_number": chapter_number,
        "band_id": band_id,
        "policy_version": 0,
        "evaluated": evaluated,
        "fired": True,
        "decision": decision,
        "blocked": blocked,
        "overridden_by": overridden_by,
        "issue_keys": [],
        "issue_groups": [],
        "evidence_refs": [],
        "trace_ids": [trace_id] if trace_id else [],
    }


def spark_chain(
    project_id: str,
    *,
    suffix: str = "1",
    terminal_type: str = "gate_delegation_decided",
) -> dict:
    request_id = f"request-{suffix}"
    trace_id = f"trace-{suffix}"
    trace_event_id = f"trace-event-{suffix}"
    terminal_id = f"terminal-{suffix}"
    approval_id = f"approval-{suffix}"
    checkpoint_id = f"checkpoint-{suffix}"
    task_id = f"task-{suffix}"
    causal_root_id = f"root-{suffix}"
    band_id = f"band-{suffix}"
    chapter_number = 10
    gate_kind = "band_checkpoint_pause"
    requested_model = "gpt-5.6-sol"
    backend = "codex_bridge"
    decided = terminal_type == "gate_delegation_decided"
    terminal_decision = "approve" if decided else "error"
    terminal_payload = {
        "gate_kind": gate_kind,
        "decision": "approve" if decided else "reject",
        "failure_reason": "" if decided else "llm_call_failed",
        "trace_id": trace_id,
        "requested_model": requested_model,
        "actual_model": requested_model if decided else "",
        "backend": backend if decided else "",
        "risk_level": "low" if decided else "",
        "findings": [],
        "evidence": [],
        "gate_related_object_type": "band_checkpoint",
        "gate_related_object_id": checkpoint_id,
        "gate_outcome": gate_outcome(
            gate_kind=gate_kind,
            scope="band",
            related_object_id=checkpoint_id,
            chapter_number=chapter_number,
            band_id=band_id,
            evaluated=True,
            decision=terminal_decision,
            blocked=not decided,
            trace_id=trace_id,
            overridden_by="spark" if decided else "",
        ),
    }
    common = {
        "project_id": project_id,
        "task_id": task_id,
        "band_id": band_id,
        "chapter_number": chapter_number,
        "scope": "band",
        "causal_root_id": causal_root_id,
    }
    events = [
        {
            **common,
            "id": request_id,
            "event_type": "gate_delegation_requested",
            "actor_type": "system",
            "actor_id": "",
            "related_object_type": "band_checkpoint",
            "related_object_id": checkpoint_id,
            "parent_event_id": "",
            "payload": {
                "gate_kind": gate_kind,
                "requested_model": requested_model,
                "related_object_type": "band_checkpoint",
                "related_object_id": checkpoint_id,
                "gate_outcome": gate_outcome(
                    gate_kind=gate_kind,
                    scope="band",
                    related_object_id=checkpoint_id,
                    chapter_number=chapter_number,
                    band_id=band_id,
                    evaluated=False,
                    decision="reject",
                    blocked=False,
                ),
            },
            "payload_error": "",
        },
        {
            **common,
            "id": trace_event_id,
            "event_type": "prompt_trace_recorded",
            "actor_type": "system",
            "actor_id": "",
            "related_object_type": "prompt_trace",
            "related_object_id": trace_id,
            "parent_event_id": request_id,
            "payload": {
                "gate_kind": gate_kind,
                "trace_id": trace_id,
                "requested_model": requested_model,
                "actual_model": requested_model if decided else "",
                "backend": backend if decided else "",
            },
            "payload_error": "",
        },
        {
            **common,
            "id": terminal_id,
            "event_type": terminal_type,
            "actor_type": "worker",
            "actor_id": requested_model if decided else "spark-gate-router",
            "related_object_type": "prompt_trace",
            "related_object_id": trace_id,
            "parent_event_id": trace_event_id,
            "payload": terminal_payload,
            "payload_error": "",
        },
    ]
    if decided:
        events.append(
            {
                **common,
                "id": approval_id,
                "event_type": "gate_delegation_approved",
                "actor_type": "worker",
                "actor_id": requested_model,
                "related_object_type": "band_checkpoint",
                "related_object_id": checkpoint_id,
                "parent_event_id": terminal_id,
                "payload": {
                    "gate_kind": gate_kind,
                    "trace_id": trace_id,
                    "requested_model": requested_model,
                    "actual_model": requested_model,
                    "backend": backend,
                    "gate_outcome": terminal_payload["gate_outcome"],
                },
                "payload_error": "",
            }
        )
    return {
        "events": events,
        "prompt_traces": [
            {
                "id": trace_id,
                "project_id": project_id,
                "decision_event_id": request_id,
                "trace_scope": "gate_delegation",
                "stage_key": f"gate_{gate_kind}",
                "template_id": "spark_pause_gate",
                "template_version": "v1",
                "backend": backend if decided else "",
                "permission_profile": "prompt_only_readonly",
                "fallback_used": False,
                "input_snapshot": {
                    "checkpoint": {
                        "id": checkpoint_id,
                        "project_id": project_id,
                        "arc_id": f"arc-{suffix}",
                        "band_id": band_id,
                        "chapter_start": 1,
                        "chapter_end": chapter_number,
                        "trigger_source": "auto_band_end",
                        "boundary_kind": "band_end",
                        "boundary_chapter": chapter_number,
                        "status": "warn",
                    },
                    "pause_policy": {
                        "review_interval_chapters": 0,
                        "manual_checkpoints": True,
                        "band_checkpoint_action": "pause_on_warn",
                        "gate_delegate": "spark",
                    },
                },
                "input_snapshot_error": "",
                "model_profile": {
                    "requested_model": requested_model,
                    "actual_model": requested_model if decided else "",
                    "backend": backend if decided else "",
                    "permission_profile": "prompt_only_readonly",
                    "fallback_used": False,
                },
                "model_profile_error": "",
                "output_summary": {
                    "gate_kind": gate_kind,
                    "parsed_decision": (
                        {
                            "decision": "approve",
                            "reason": "eligible checkpoint",
                            "risk_level": "low",
                            "findings": [],
                            "evidence": [],
                        }
                        if decided
                        else None
                    ),
                    "failure_reason": "" if decided else "llm_call_failed",
                    "requested_model": requested_model,
                    "actual_model": requested_model if decided else "",
                    "backend": backend if decided else "",
                },
                "output_summary_error": "",
            }
        ],
        "related_gate_objects": [
            {
                "request_event_id": request_id,
                "object_type": "band_checkpoint",
                "object_id": checkpoint_id,
                "exists": True,
                "project_id": project_id,
                "band_id": band_id,
                "boundary_kind": "band_end",
                "boundary_chapter": chapter_number,
                "trigger_source": "auto_band_end",
                "status": "overridden" if decided else "warn",
            }
        ],
        "causal_roots": [
            {
                "id": causal_root_id,
                "project_id": project_id,
                "task_id": task_id,
                "scope": "task",
                "event_type": "continue_requested",
                "related_object_type": "generation_task",
                "related_object_id": task_id,
                "parent_event_id": "",
                "causal_root_id": causal_root_id,
            }
        ],
    }


def empty_spark_evidence() -> dict:
    return {
        "events": [],
        "prompt_traces": [],
        "related_gate_objects": [],
        "causal_roots": [],
    }


def chapter_spark_chain(project_id: str) -> dict:
    chain = spark_chain(project_id, suffix="chapter")
    request_id = "request-chapter"
    trace_id = "trace-chapter"
    chapter_number = 20
    gate_kind = "chapter_review_interval"
    review_id = "review-chapter"
    plan_id = "plan-chapter"
    draft_id = "draft-chapter"
    for event in chain["events"]:
        event["band_id"] = ""
        event["chapter_number"] = chapter_number
        event["scope"] = "chapter"
        payload = event["payload"]
        payload["gate_kind"] = gate_kind
        if "gate_outcome" in payload:
            outcome = payload["gate_outcome"]
            outcome["responsibility_domain"] = gate_kind
            outcome["scope"] = "chapter"
            outcome["candidate_id"] = review_id
            outcome["chapter_number"] = chapter_number
            outcome["band_id"] = ""
        if event["event_type"] == "gate_delegation_requested":
            event["related_object_type"] = "chapter_review"
            event["related_object_id"] = review_id
            payload["related_object_type"] = "chapter_review"
            payload["related_object_id"] = review_id
        elif event["event_type"] == "gate_delegation_decided":
            payload["gate_related_object_type"] = "chapter_review"
            payload["gate_related_object_id"] = review_id
        elif event["event_type"] == "gate_delegation_approved":
            event["related_object_type"] = "chapter_review"
            event["related_object_id"] = review_id
    trace = chain["prompt_traces"][0]
    trace["stage_key"] = f"gate_{gate_kind}"
    trace["input_snapshot"] = {
        "gate_reason": "review interval 10 reached",
        "chapter_plan": {
            "id": plan_id,
            "chapter_number": chapter_number,
            "title": "Interval chapter",
            "one_line": "Review the interval boundary.",
            "status": "planned",
        },
        "draft_id": draft_id,
        "review_id": review_id,
        "review_verdict": {
            "verdict": "warn",
            "final_residual_decision": None,
            "repair_verification": None,
            "residual_review_issues": [],
        },
        "review_interval_chapters": 10,
        "pause_policy": {
            "review_interval_chapters": 10,
            "manual_checkpoints": True,
            "band_checkpoint_action": "pause_on_warn",
            "gate_delegate": "spark",
        },
    }
    trace["output_summary"]["gate_kind"] = gate_kind
    chain["related_gate_objects"] = [
        {
            "request_event_id": request_id,
            "object_type": "chapter_review",
            "object_id": review_id,
            "exists": True,
            "project_id": project_id,
            "chapter_plan_id": plan_id,
            "chapter_number": chapter_number,
            "draft_id": draft_id,
            "verdict": "warn",
        }
    ]
    return chain


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
        "pause": {
            "review_interval_chapters": 0,
            "manual_checkpoints": profile == "standard",
            "band_checkpoint_action": (
                "pause_on_warn" if profile == "standard" else "continue"
            ),
            "gate_delegate": delegate,
        },
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
    spark = (
        spark_chain(mcp["project"]["id"])
        if delegate == "spark"
        else empty_spark_evidence()
    )
    if delegate == "spark":
        mcp["gate_ledger"]["metrics"].append(
            {
                "gate_id": "delegation",
                "gate_versions": ["v1"],
                "responsibility_domains": ["band_checkpoint_pause"],
                "opportunities": 1,
                "evaluations": 1,
                "fires": 1,
                "blocks": 0,
                "pauses": 0,
                "approvals": 1,
                "overrides": 1,
                "post_override_incident_proxy": 0,
                "post_pass_incident_proxy": 0,
                "unknown_legacy_count": 0,
                "fire_rate": 1.0,
                "block_rate": 0.0,
                "override_rate": 1.0,
            }
        )
        mcp["cost_report"]["gate_costs"].append(
            {
                "gate_id": "delegation",
                "metrics": {
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
                    "provider_usage_attempts": 0,
                    "codex_usage_attempts": 1,
                    "estimated_usage_attempts": 0,
                    "missing_usage_attempts": 0,
                },
            }
        )
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
        "tasks": (
            [
                {
                    "task_id": "task-1",
                    "project_id": mcp["project"]["id"],
                    "run_until_chapter": target,
                    "status": "completed",
                }
            ]
            if delegate == "spark"
            else []
        ),
        "gate_ledger": mcp["gate_ledger"],
        "cost_report": mcp["cost_report"],
        "rule_provenance": mcp["rule_provenance"],
        "policy": {"version": 1, "policy": policy},
        "database": database,
        "candidate_spark_model": "gpt-5.6-sol",
        "operational": {"spark": spark},
    }


def replace_spark_evidence(item: dict, spark: dict) -> None:
    item["operational"]["spark"] = spark
    if spark["prompt_traces"]:
        item["policy"]["policy"]["pause"] = dict(
            spark["prompt_traces"][0]["input_snapshot"]["pause_policy"]
        )
        item["manifest_cell"]["policy_hash"] = matrix.l200.canonical_hash(
            item["policy"]["policy"]
        )
    events = spark["events"]
    requests = [
        event
        for event in events
        if event["event_type"] == "gate_delegation_requested"
    ]
    terminals = [
        event
        for event in events
        if event["event_type"]
        in {"gate_delegation_decided", "gate_delegation_failed"}
    ]
    approvals = [
        event
        for event in events
        if event["event_type"] == "gate_delegation_approved"
    ]
    item["tasks"] = [
        {
            "task_id": request["task_id"],
            "project_id": request["project_id"],
            "run_until_chapter": item["manifest_cell"]["target"],
            "status": "completed",
        }
        for request in requests
    ]
    blocked = sum(
        bool(event["payload"]["gate_outcome"]["blocked"])
        for event in terminals
    )
    overridden = sum(
        bool(event["payload"]["gate_outcome"]["overridden_by"])
        for event in terminals
    )
    metrics = [
        metric
        for metric in item["gate_ledger"]["metrics"]
        if metric["gate_id"] != "delegation"
    ]
    metrics.append(
        {
            "gate_id": "delegation",
            "gate_versions": ["v1"],
            "responsibility_domains": (
                sorted(
                    {
                        event["payload"]["gate_kind"]
                        for event in requests
                    }
                )
                or ["delegated_gate_resolution"]
            ),
            "opportunities": len(requests),
            "evaluations": len(terminals),
            "fires": len(terminals),
            "blocks": blocked,
            "pauses": 0,
            "approvals": len(approvals),
            "overrides": overridden,
            "post_override_incident_proxy": 0,
            "post_pass_incident_proxy": 0,
            "unknown_legacy_count": 0,
            "fire_rate": 0.0,
            "block_rate": 0.0,
            "override_rate": 0.0,
        }
    )
    item["gate_ledger"]["metrics"] = metrics
    item["cost_report"]["gate_costs"] = [
        cost
        for cost in item["cost_report"]["gate_costs"]
        if cost["gate_id"] != "delegation"
    ]
    if spark["prompt_traces"]:
        item["cost_report"]["gate_costs"].append(
            {
                "gate_id": "delegation",
                "metrics": {
                    "attempts": len(spark["prompt_traces"]),
                    "successes": sum(
                        event["event_type"] == "gate_delegation_decided"
                        for event in terminals
                    ),
                },
            }
        )


def test_human_cell_complete_fixture_passes() -> None:
    assert matrix.validate_cell("L30", evidence("L30")) == []


def test_spark_cell_requires_terminal_delegation_for_every_request() -> None:
    item = evidence("L60S")
    assert matrix.validate_cell("L60S", item) == []

    second = spark_chain(item["project"]["id"], suffix="2")
    item["operational"]["spark"]["events"].extend(second["events"])
    item["operational"]["spark"]["prompt_traces"].extend(
        second["prompt_traces"]
    )
    item["operational"]["spark"]["related_gate_objects"].extend(
        second["related_gate_objects"]
    )
    second_trace_event = next(
        event
        for event in item["operational"]["spark"]["events"]
        if event["id"] == "trace-event-2"
    )
    second_terminal = next(
        event
        for event in item["operational"]["spark"]["events"]
        if event["id"] == "terminal-2"
    )
    second_terminal["parent_event_id"] = "trace-event-1"

    violations = matrix.validate_cell("L60S", item)

    assert any(
        "request request-1 has 2 terminal events" in violation
        for violation in violations
    )
    assert any(
        "request request-2 has 0 terminal events" in violation
        for violation in violations
    )
    assert second_trace_event["parent_event_id"] == "request-2"


def test_balanced_aggregate_counts_without_request_chains_fail_closed() -> None:
    item = evidence("L60S")
    item["operational"]["spark"] = {
        "gate_delegation_requested": 1,
        "gate_delegation_decided": 1,
        "gate_delegation_approved": 1,
        "gate_delegation_failed": 0,
    }

    violations = matrix.validate_cell("L60S", item)

    assert "Spark evidence events must be a list" in violations
    assert "Spark evidence prompt_traces must be a list" in violations


def test_spark_request_requires_exact_prompt_trace_linkage() -> None:
    item = evidence("L60S")
    item["operational"]["spark"]["prompt_traces"][0][
        "decision_event_id"
    ] = "different-request"

    violations = matrix.validate_cell("L60S", item)

    assert any(
        "request request-1 has 0 PromptTrace rows" in violation
        for violation in violations
    )
    assert any(
        "PromptTrace trace-1 references unknown request different-request"
        in violation
        for violation in violations
    )


def test_spark_trace_event_must_preserve_model_route_identity() -> None:
    item = evidence("L60S")
    trace_event = next(
        event
        for event in item["operational"]["spark"]["events"]
        if event["event_type"] == "prompt_trace_recorded"
    )
    trace_event["payload"]["requested_model"] = "different-model"

    violations = matrix.validate_cell("L60S", item)

    assert any(
        "request request-1 prompt trace requested_model mismatch"
        in violation
        for violation in violations
    )


def test_spark_request_gate_outcome_must_be_unevaluated_reject() -> None:
    item = evidence("L60S")
    request = next(
        event
        for event in item["operational"]["spark"]["events"]
        if event["event_type"] == "gate_delegation_requested"
    )
    request["payload"]["gate_outcome"]["decision"] = "approve"

    violations = matrix.validate_cell("L60S", item)

    assert any(
        "request request-1 unevaluated outcome is invalid" in violation
        for violation in violations
    )


def test_spark_request_requires_eligible_optional_pause_snapshot() -> None:
    item = evidence("L60S")
    item["operational"]["spark"]["prompt_traces"][0]["input_snapshot"][
        "checkpoint"
    ]["status"] = "fail"

    violations = matrix.validate_cell("L60S", item)

    assert any(
        "request request-1 automatic checkpoint status=fail, expected=warn"
        in violation
        for violation in violations
    )


def test_automatic_checkpoint_pass_is_not_an_optional_pause() -> None:
    item = evidence("L60S")
    item["operational"]["spark"]["prompt_traces"][0]["input_snapshot"][
        "checkpoint"
    ]["status"] = "pass"
    item["operational"]["spark"]["related_gate_objects"][0][
        "status"
    ] = "pass"

    violations = matrix.validate_cell("L60S", item)

    assert any(
        "request request-1 automatic checkpoint status=pass, expected=warn"
        in violation
        for violation in violations
    )


def test_automatic_checkpoint_requires_pause_policy_action() -> None:
    item = evidence("L60S")
    item["operational"]["spark"]["prompt_traces"][0]["input_snapshot"][
        "pause_policy"
    ]["band_checkpoint_action"] = "continue"

    violations = matrix.validate_cell("L60S", item)

    assert any(
        "request request-1 automatic checkpoint policy action=continue "
        "is not pausing"
        in violation
        for violation in violations
    )


def test_automatic_checkpoint_requires_emitter_trigger_source() -> None:
    item = evidence("L60S")
    item["operational"]["spark"]["prompt_traces"][0]["input_snapshot"][
        "checkpoint"
    ]["trigger_source"] = "unrelated_source"
    item["operational"]["spark"]["related_gate_objects"][0][
        "trigger_source"
    ] = "unrelated_source"

    violations = matrix.validate_cell("L60S", item)

    assert any(
        "request request-1 automatic checkpoint trigger_source="
        "unrelated_source, expected=auto_band_end"
        in violation
        for violation in violations
    )


def test_automatic_checkpoint_source_status_must_preserve_pause_state() -> None:
    item = evidence("L60S")
    item["operational"]["spark"]["related_gate_objects"][0][
        "status"
    ] = "pass"

    violations = matrix.validate_cell("L60S", item)

    assert any(
        "request request-1 automatic checkpoint source status=pass "
        "is inconsistent with a delegated warning"
        in violation
        for violation in violations
    )


def test_spark_model_must_match_candidate_generation_worker_route() -> None:
    item = evidence("L60S")
    for event in item["operational"]["spark"]["events"]:
        payload = event["payload"]
        if "requested_model" in payload:
            payload["requested_model"] = "different-codex-model"
        if payload.get("actual_model"):
            payload["actual_model"] = "different-codex-model"
        if event["actor_id"] == "gpt-5.6-sol":
            event["actor_id"] = "different-codex-model"
    trace = item["operational"]["spark"]["prompt_traces"][0]
    trace["model_profile"]["requested_model"] = "different-codex-model"
    trace["model_profile"]["actual_model"] = "different-codex-model"
    trace["output_summary"]["requested_model"] = "different-codex-model"
    trace["output_summary"]["actual_model"] = "different-codex-model"

    violations = matrix.validate_cell("L60S", item)

    assert any(
        "request request-1 requested model=different-codex-model, "
        "candidate=gpt-5.6-sol"
        in violation
        for violation in violations
    )


def test_spark_request_requires_task_and_causal_root_provenance() -> None:
    item = evidence("L60S")
    item["tasks"] = []
    item["operational"]["spark"]["causal_roots"] = []

    violations = matrix.validate_cell("L60S", item)

    assert any(
        "request request-1 task task-1 is not in collected tasks"
        in violation
        for violation in violations
    )
    assert any(
        "request request-1 has 0 causal root events" in violation
        for violation in violations
    )


def test_spark_request_requires_existing_authoritative_gate_object() -> None:
    item = evidence("L60S")
    item["operational"]["spark"]["related_gate_objects"][0]["exists"] = False

    violations = matrix.validate_cell("L60S", item)

    assert any(
        "request request-1 related gate object does not exist" in violation
        for violation in violations
    )


def test_spark_chains_are_bound_to_gate_ledger_counts() -> None:
    item = evidence("L60S")
    delegation = next(
        metric
        for metric in item["gate_ledger"]["metrics"]
        if metric["gate_id"] == "delegation"
    )
    delegation["opportunities"] = 2

    violations = matrix.validate_cell("L60S", item)

    assert (
        "delegation gate ledger opportunities=2, expected=1"
        in violations
    )


def test_spark_chains_require_delegation_cost_trace_evidence() -> None:
    item = evidence("L60S")
    item["cost_report"]["gate_costs"] = []

    violations = matrix.validate_cell("L60S", item)

    assert "delegation cost report metric is missing" in violations


def test_chapter_interval_spark_chain_uses_general_eligibility_contract() -> None:
    item = evidence("L60S")
    replace_spark_evidence(
        item,
        chapter_spark_chain(item["project"]["id"]),
    )

    assert matrix.validate_cell("L60S", item) == []


def test_chapter_interval_rejects_ineligible_review_snapshot() -> None:
    item = evidence("L60S")
    replace_spark_evidence(
        item,
        chapter_spark_chain(item["project"]["id"]),
    )
    item["operational"]["spark"]["prompt_traces"][0]["input_snapshot"][
        "review_verdict"
    ]["residual_review_issues"] = [
        {"severity": "error", "blocking": True}
    ]

    violations = matrix.validate_cell("L60S", item)

    assert any(
        "request request-chapter review verdict is not Canon-eligible"
        in violation
        for violation in violations
    )


def test_chapter_interval_excludes_task_last_requested_chapter() -> None:
    item = evidence("L60S")
    replace_spark_evidence(
        item,
        chapter_spark_chain(item["project"]["id"]),
    )
    item["tasks"][0]["run_until_chapter"] = 20

    violations = matrix.validate_cell("L60S", item)

    assert any(
        "request request-chapter is at task last requested chapter 20"
        in violation
        for violation in violations
    )


def test_spark_cell_without_eligible_pause_opportunity_can_complete() -> None:
    item = evidence("L60S")
    replace_spark_evidence(item, empty_spark_evidence())

    assert matrix.validate_cell("L60S", item) == []


def test_matrix_requires_live_delegation_from_at_least_one_spark_cell() -> None:
    results = {
        name: {"evidence": evidence(name), "violations": []}
        for name in matrix.EXPECTED_CELLS
    }
    for item in results.values():
        if item["evidence"]["manifest_cell"]["delegate"] == "spark":
            replace_spark_evidence(
                item["evidence"],
                empty_spark_evidence(),
            )

    assert matrix.matrix_operational_violations(results) == [
        "matrix has no live Spark delegation request evidence"
    ]


def test_matrix_accepts_delegation_from_any_spark_cell() -> None:
    results = {
        name: {"evidence": evidence(name), "violations": []}
        for name in matrix.EXPECTED_CELLS
    }
    replace_spark_evidence(
        results["L60S"]["evidence"],
        empty_spark_evidence(),
    )

    assert matrix.matrix_operational_violations(results) == []


def test_matrix_accepts_live_failed_terminal_chain() -> None:
    results = {
        name: {"evidence": evidence(name), "violations": []}
        for name in matrix.EXPECTED_CELLS
    }
    replace_spark_evidence(
        results["L60S"]["evidence"],
        empty_spark_evidence(),
    )
    replace_spark_evidence(
        results["L100"]["evidence"],
        spark_chain(
            results["L100"]["evidence"]["project"]["id"],
            terminal_type="gate_delegation_failed",
        ),
    )

    assert matrix.validate_cell(
        "L100",
        results["L100"]["evidence"],
    ) == []
    assert matrix.matrix_operational_violations(results) == []


def test_finalizer_fails_without_matrix_level_live_spark_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    matrix_manifest = tmp_path / "matrix.json"
    matrix_manifest.write_text(
        json.dumps({"cells": {name: {} for name in matrix.EXPECTED_CELLS}}),
        encoding="utf-8",
    )
    output_dir = tmp_path / "audit"

    async def fake_collect_cell(
        _args: argparse.Namespace,
        name: str,
        _cell: dict,
    ) -> dict:
        item = evidence(name)
        if item["manifest_cell"]["delegate"] == "spark":
            replace_spark_evidence(item, empty_spark_evidence())
        return item

    monkeypatch.setattr(matrix, "collect_cell", fake_collect_cell)
    monkeypatch.setattr(
        matrix,
        "assert_matrix_identity",
        lambda _manifest, _path: {
            "source_sha": "a" * 40,
            "code_changes_during_run": 0,
            "runtime_image": {"image_id": "runtime-id"},
            "browser_image": {"image_id": "browser-id"},
        },
    )
    monkeypatch.setattr(
        matrix,
        "candidate_stack_identity",
        lambda *_args, **_kwargs: {"compose_project": "candidate"},
    )
    monkeypatch.setenv(
        "FORWIN_MATRIX_TEST_DATABASE_URL",
        "postgresql://user:secret@127.0.0.1:5432/forwin",
    )

    status = asyncio.run(
        matrix.run(
            argparse.Namespace(
                database_url_env="FORWIN_MATRIX_TEST_DATABASE_URL",
                matrix_manifest=matrix_manifest,
                output_dir=output_dir,
                mcp_url="http://127.0.0.1:8896/mcp",
                api_url="http://127.0.0.1:8899",
                runtime_container=[],
                browser_container=[],
                dependency_container=[],
            )
        )
    )

    manifest = json.loads(
        (output_dir / "manifest.json").read_text(encoding="utf-8")
    )
    report = (output_dir / "final-report.md").read_text(encoding="utf-8")
    assert status == 1
    assert manifest["result"] == "fail"
    assert manifest["violations"] == [
        "matrix has no live Spark delegation request evidence"
    ]
    assert "- Result: FAIL" in report
    assert "- `matrix`: matrix has no live Spark delegation request evidence" in report


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
    monkeypatch.setattr(
        matrix,
        "candidate_spark_model",
        lambda _containers: "gpt-5.6-sol",
    )
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
    assert stack["spark_model"] == "gpt-5.6-sol"
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
