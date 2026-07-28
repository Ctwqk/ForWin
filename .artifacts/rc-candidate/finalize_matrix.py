#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text


ROOT = Path(__file__).resolve().parents[2]
MATRIX_ROOT = ROOT / ".artifacts/v4-matrix-candidate"
DEFAULT_OUTPUT = MATRIX_ROOT / "final-audit"
L200_MODULE_PATH = Path(__file__).with_name("l200_evidence.py")
MATRIX_AUDIT_SCHEMA_VERSION = 4
EXPECTED_CELLS = {
    "L30": {"target": 30, "profile": "standard", "delegate": "human"},
    "L60S": {"target": 60, "profile": "standard", "delegate": "spark"},
    "L60P": {"target": 60, "profile": "pulp", "delegate": "human"},
    "L100": {"target": 100, "profile": "standard", "delegate": "spark"},
}
SPARK_EVENT_TYPES = frozenset(
    {
        "gate_delegation_requested",
        "prompt_trace_recorded",
        "gate_delegation_decided",
        "gate_delegation_failed",
        "gate_delegation_approved",
    }
)
SPARK_TERMINAL_TYPES = frozenset(
    {"gate_delegation_decided", "gate_delegation_failed"}
)
SPARK_PERMISSION_PROFILE = "prompt_only_readonly"


class MatrixAuditError(RuntimeError):
    pass


def load_l200_module():
    spec = importlib.util.spec_from_file_location("forwin_l200_evidence", L200_MODULE_PATH)
    if spec is None or spec.loader is None:
        raise MatrixAuditError(f"cannot load evidence helpers: {L200_MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


l200 = load_l200_module()


def now() -> str:
    return datetime.now(UTC).isoformat()


def command(*args: str) -> str:
    completed = subprocess.run(
        args,
        cwd=ROOT,
        env=l200.command_environment(),
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise MatrixAuditError(f"command failed ({' '.join(args)}): {detail}")
    return completed.stdout.strip()


def load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MatrixAuditError(f"required artifact is missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise MatrixAuditError(f"invalid JSON artifact: {path}") from exc
    if not isinstance(payload, dict):
        raise MatrixAuditError(f"expected object artifact: {path}")
    return payload


def prepare_output_directory(output: Path) -> None:
    if output.exists() and (
        not output.is_dir() or any(output.iterdir())
    ):
        raise MatrixAuditError(
            f"matrix audit output directory is not empty: {output}"
        )
    output.mkdir(parents=True, exist_ok=True)


def assert_matrix_identity(manifest: dict[str, Any], matrix_path: Path) -> dict[str, Any]:
    source_sha = str(manifest.get("source_sha") or "")
    if not source_sha:
        raise MatrixAuditError("matrix manifest has no source_sha")
    if command("git", "rev-parse", "HEAD") != source_sha:
        raise MatrixAuditError("current HEAD is not the frozen matrix source")
    if command("git", "status", "--porcelain=v1", "--untracked-files=no"):
        raise MatrixAuditError("tracked worktree changed during matrix")
    if int(manifest.get("code_changes_during_run") or 0) != 0:
        raise MatrixAuditError("matrix reports code_changes_during_run != 0")
    harness = MATRIX_ROOT / "matrix_run.py"
    if str(manifest.get("harness_sha256") or "") != l200.sha256_file(harness):
        raise MatrixAuditError("matrix harness changed after freeze")
    runtime = manifest.get("runtime_image") or {}
    browser = manifest.get("browser_image") or {}
    for expected in (runtime, browser):
        actual = l200.image_identity(str(expected.get("tag") or ""))
        if actual.get("image_id") != expected.get("image_id"):
            raise MatrixAuditError(f"matrix image changed: {expected.get('tag')}")
        if actual.get("revision") != source_sha:
            raise MatrixAuditError(f"matrix image revision mismatch: {expected.get('tag')}")
    return {
        "source_sha": source_sha,
        "source_tree": command("git", "rev-parse", "HEAD^{tree}"),
        "matrix_manifest": {
            "path": str(matrix_path),
            "sha256": l200.sha256_file(matrix_path),
        },
        "harness": {"path": str(harness), "sha256": l200.sha256_file(harness)},
        "runtime_image": runtime,
        "browser_image": browser,
        "tracked_worktree_clean": True,
        "code_changes_during_run": 0,
    }


def candidate_stack_identity(
    args: argparse.Namespace,
    *,
    runtime_image_id: str,
    browser_image_id: str,
) -> dict[str, Any]:
    runtime_containers = l200.verify_container_set(
        args.runtime_container,
        expected_image_id=runtime_image_id,
        role="runtime",
        expected_services=l200.EXPECTED_RUNTIME_SERVICES,
    )
    browser_containers = l200.verify_container_set(
        args.browser_container,
        expected_image_id=browser_image_id,
        role="publisher-browser",
        expected_services=l200.EXPECTED_BROWSER_SERVICES,
    )
    dependency_containers = l200.verify_container_set(
        args.dependency_container,
        role="dependency",
        expected_services=l200.EXPECTED_DEPENDENCY_SERVICES,
    )
    all_containers = (
        runtime_containers + browser_containers + dependency_containers
    )
    compose_projects = {
        str(container.get("compose_project") or "")
        for container in all_containers
    }
    if "" in compose_projects or len(compose_projects) != 1:
        raise MatrixAuditError(
            "candidate services belong to different Compose stacks"
        )
    l200.verify_candidate_mount_policy(all_containers)
    identity = {
        "compose_project": next(iter(compose_projects)),
        "runtime_containers": runtime_containers,
        "publisher_browser_containers": browser_containers,
        "dependency_containers": dependency_containers,
        "spark_model": candidate_spark_model(args.runtime_container),
    }
    identity["connection_bindings"] = l200.connection_bindings(args, identity)
    return identity


def candidate_spark_model(runtime_containers: list[str]) -> str:
    discovered: list[str] = []
    for name in runtime_containers:
        try:
            payload = json.loads(
                command("docker", "container", "inspect", name)
            )
        except json.JSONDecodeError as exc:
            raise MatrixAuditError(
                f"docker returned invalid JSON for runtime container {name}"
            ) from exc
        if len(payload) != 1:
            raise MatrixAuditError(
                f"expected one runtime container for {name}"
            )
        item = payload[0]
        config = item.get("Config") or {}
        labels = config.get("Labels") or {}
        if labels.get("com.docker.compose.service") != "generation-worker":
            continue
        environment: dict[str, str] = {}
        for raw in config.get("Env") or []:
            key, separator, value = str(raw).partition("=")
            if separator:
                environment[key] = value
        model = str(
            environment.get("FORWIN_CODEX_DEFAULT_MODEL") or ""
        ).strip()
        if model:
            discovered.append(model)
    if len(discovered) != 1:
        raise MatrixAuditError(
            "candidate generation-worker must expose exactly one "
            "FORWIN_CODEX_DEFAULT_MODEL"
        )
    return discovered[0]


def parsed_json_object(value: Any) -> tuple[dict[str, Any], str]:
    if isinstance(value, dict):
        return dict(value), ""
    try:
        parsed = json.loads(str(value or "{}"))
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return {}, f"{exc.__class__.__name__}: {exc}"
    if not isinstance(parsed, dict):
        return {}, f"expected object, got {type(parsed).__name__}"
    return parsed, ""


def selected_gate_snapshot(value: dict[str, Any]) -> dict[str, Any]:
    pause_policy = value.get("pause_policy")
    selected: dict[str, Any] = {
        "pause_policy": (
            {
                key: pause_policy.get(key)
                for key in (
                    "review_interval_chapters",
                    "manual_checkpoints",
                    "band_checkpoint_action",
                    "gate_delegate",
                )
            }
            if isinstance(pause_policy, dict)
            else pause_policy
        )
    }
    checkpoint = value.get("checkpoint")
    if isinstance(checkpoint, dict):
        selected["checkpoint"] = {
            key: checkpoint.get(key)
            for key in (
                "id",
                "project_id",
                "arc_id",
                "band_id",
                "chapter_start",
                "chapter_end",
                "trigger_source",
                "boundary_kind",
                "boundary_chapter",
                "status",
            )
        }
        return selected
    chapter_plan = value.get("chapter_plan")
    selected.update(
        {
            "gate_reason": value.get("gate_reason"),
            "chapter_plan": (
                {
                    key: chapter_plan.get(key)
                    for key in (
                        "id",
                        "chapter_number",
                        "title",
                        "one_line",
                        "status",
                    )
                }
                if isinstance(chapter_plan, dict)
                else chapter_plan
            ),
            "draft_id": value.get("draft_id"),
            "review_id": value.get("review_id"),
            "review_interval_chapters": value.get(
                "review_interval_chapters"
            ),
        }
    )
    review_verdict = value.get("review_verdict")
    selected["review_verdict"] = (
        {
            key: review_verdict.get(key)
            for key in (
                "verdict",
                "final_residual_decision",
                "repair_verification",
                "residual_review_issues",
            )
        }
        if isinstance(review_verdict, dict)
        else review_verdict
    )
    return selected


def selected_trace_output(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value.get(key)
        for key in (
            "gate_kind",
            "parsed_decision",
            "failure_reason",
            "requested_model",
            "actual_model",
            "backend",
        )
    }


def _related_gate_object(
    connection,
    request: dict[str, Any],
) -> dict[str, Any]:
    object_type = str(request.get("related_object_type") or "")
    object_id = str(request.get("related_object_id") or "")
    base = {
        "request_event_id": str(request.get("id") or ""),
        "object_type": object_type,
        "object_id": object_id,
        "exists": False,
    }
    if object_type == "band_checkpoint":
        row = connection.execute(
            text(
                """
                SELECT id,project_id,band_id,trigger_source,
                       boundary_kind,boundary_chapter,status
                FROM band_checkpoints WHERE id=:object_id
                """
            ),
            {"object_id": object_id},
        ).mappings().one_or_none()
        if row is not None:
            base.update(dict(row))
            base["exists"] = True
        return base
    if object_type == "chapter_plan":
        row = connection.execute(
            text(
                """
                SELECT id,project_id,chapter_number,status
                FROM chapter_plans WHERE id=:object_id
                """
            ),
            {"object_id": object_id},
        ).mappings().one_or_none()
        if row is not None:
            base.update(dict(row))
            base["exists"] = True
        return base
    if object_type == "chapter_review":
        row = connection.execute(
            text(
                """
                SELECT review.id,plan.project_id,
                       plan.id AS chapter_plan_id,
                       plan.chapter_number,draft.id AS draft_id,
                       review.verdict
                FROM chapter_reviews AS review
                JOIN chapter_drafts AS draft
                  ON draft.id=review.draft_id
                JOIN chapter_plans AS plan
                  ON plan.id=draft.chapter_plan_id
                WHERE review.id=:object_id
                """
            ),
            {"object_id": object_id},
        ).mappings().one_or_none()
        if row is not None:
            base.update(dict(row))
            base["exists"] = True
        return base
    return base


def collect_spark_evidence(connection, project_id: str) -> dict[str, Any]:
    event_rows = connection.execute(
        text(
            """
            SELECT id,project_id,task_id,band_id,chapter_number,scope,
                   event_type,actor_type,actor_id,related_object_type,
                   related_object_id,parent_event_id,causal_root_id,
                   payload_json
            FROM decision_events AS event
            WHERE project_id=:project_id
              AND (
                event_type IN (
                  'gate_delegation_requested',
                  'gate_delegation_decided',
                  'gate_delegation_failed',
                  'gate_delegation_approved'
                )
                OR (
                  event_type='prompt_trace_recorded'
                  AND (
                    parent_event_id IN (
                      SELECT id FROM decision_events
                      WHERE project_id=:project_id
                        AND event_type='gate_delegation_requested'
                    )
                    OR related_object_id IN (
                      SELECT id FROM prompt_traces
                      WHERE project_id=:project_id
                        AND trace_scope='gate_delegation'
                    )
                  )
                )
              )
            ORDER BY event.created_at,event.id
            """
        ),
        {"project_id": project_id},
    ).mappings()
    events: list[dict[str, Any]] = []
    for row in event_rows:
        item = dict(row)
        payload, error = parsed_json_object(item.pop("payload_json", "{}"))
        item["payload"] = payload
        item["payload_error"] = error
        events.append(item)

    trace_rows = connection.execute(
        text(
            """
            SELECT id,project_id,decision_event_id,trace_scope,stage_key,
                   template_id,template_version,backend,permission_profile,
                   fallback_used,input_snapshot_json,model_profile_json,
                   output_summary_json
            FROM prompt_traces
            WHERE project_id=:project_id
              AND trace_scope='gate_delegation'
            ORDER BY created_at,id
            """
        ),
        {"project_id": project_id},
    ).mappings()
    traces: list[dict[str, Any]] = []
    for row in trace_rows:
        item = dict(row)
        for source_key, target_key, selector in (
            ("input_snapshot_json", "input_snapshot", selected_gate_snapshot),
            ("model_profile_json", "model_profile", lambda value: value),
            ("output_summary_json", "output_summary", selected_trace_output),
        ):
            parsed, error = parsed_json_object(item.pop(source_key, "{}"))
            item[target_key] = selector(parsed)
            item[f"{target_key}_error"] = error
        traces.append(item)

    requests = [
        event
        for event in events
        if event.get("event_type") == "gate_delegation_requested"
    ]
    causal_roots = [
        dict(row)
        for row in connection.execute(
            text(
                """
                SELECT id,project_id,task_id,scope,event_type,
                       related_object_type,related_object_id,
                       parent_event_id,causal_root_id
                FROM decision_events
                WHERE project_id=:project_id
                  AND id IN (
                    SELECT causal_root_id FROM decision_events
                    WHERE project_id=:project_id
                      AND event_type='gate_delegation_requested'
                  )
                ORDER BY created_at,id
                """
            ),
            {"project_id": project_id},
        ).mappings()
    ]
    return {
        "events": events,
        "prompt_traces": traces,
        "related_gate_objects": [
            _related_gate_object(connection, request)
            for request in requests
        ],
        "causal_roots": causal_roots,
    }


def operational_metrics(database_url: str, project_id: str) -> dict[str, Any]:
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            events = {
                str(key or "unknown"): int(total or 0)
                for key, total in connection.execute(
                    text(
                        """
                        SELECT event_type,count(*) FROM decision_events
                        WHERE project_id=:project_id
                        GROUP BY event_type ORDER BY event_type
                        """
                    ),
                    {"project_id": project_id},
                )
            }
            repairs = {
                str(key or "unknown"): int(total or 0)
                for key, total in connection.execute(
                    text(
                        """
                        SELECT coalesce(nullif(repair_scope,''),'unknown'),count(*)
                        FROM chapter_rewrite_attempts WHERE project_id=:project_id
                        GROUP BY 1 ORDER BY 1
                        """
                    ),
                    {"project_id": project_id},
                )
            }
            plan_blockers = int(
                connection.execute(
                    text(
                        """
                        SELECT count(*) FROM future_plan_audit_runs
                        WHERE project_id=:project_id
                          AND (status IN ('blocked','fail','failed')
                               OR blocking_reasons_json::jsonb<>'[]'::jsonb)
                        """
                    ),
                    {"project_id": project_id},
                ).scalar_one()
                or 0
            )
            return {
                "event_type_counts": events,
                "repair_scope_counts": repairs,
                "plan_health_blockers": plan_blockers,
                "canon_blocks": int(events.get("canon_commit_blocked", 0)),
                "entity_conflicts": int(
                    events.get("entity_plan_conflict", 0)
                    + events.get("entity_alias_conflict", 0)
                ),
                "task_reclaims": sum(
                    count for key, count in events.items() if "reclaim" in key
                ),
                "spark": collect_spark_evidence(connection, project_id),
            }
    finally:
        engine.dispose()


def report_contract_violations(
    project_id: str,
    reports: dict[str, Any],
) -> list[str]:
    return l200.report_contract_violations(
        gate=reports["gate_ledger"],
        cost=reports["cost_report"],
        rules=reports["rule_provenance"],
        project_id=project_id,
        scope="project",
    )


def integrity_violations(
    target: int,
    database: dict[str, Any],
) -> list[str]:
    violations: list[str] = []
    canon = database["canon"]
    for key, expected in (
        ("committed", target),
        ("non_committed", 0),
        ("distinct_chapters", target),
        ("first_chapter", 1),
        ("last_chapter", target),
        ("duplicate_ids", 0),
        ("duplicate_candidate_refs", 0),
        ("duplicate_idempotency_keys", 0),
        ("duplicate_chapters", 0),
        ("missing_world_snapshot_refs", 0),
        ("missing_map_snapshot_refs", 0),
        ("wrong_world_snapshot_identity", 0),
        ("wrong_map_snapshot_identity", 0),
        ("candidate_identity_mismatches", 0),
        ("chapter_identity_mismatches", 0),
    ):
        if int(canon.get(key) or 0) != expected:
            violations.append(f"canon.{key}={canon.get(key)}, expected={expected}")
    candidates = database["candidates"]
    for key, expected in (
        ("accepted_canon", target),
        ("missing_commit_id", 0),
        ("reverse_identity_mismatches", 0),
        ("duplicate_accepted_chapters", 0),
    ):
        if int(candidates.get(key) or 0) != expected:
            violations.append(f"candidates.{key}={candidates.get(key)}, expected={expected}")
    for key in (
        "missing_graph_delta_refs",
        "invalid_graph_delta_refs",
        "duplicate_graph_delta_refs",
    ):
        if int(database["graph"].get(key) or 0):
            violations.append(f"graph.{key}={database['graph'].get(key)}")
    for key in ("world_snapshot_through", "map_snapshot_through"):
        if int(database["snapshots"].get(key) or 0) != target:
            violations.append(f"snapshots.{key}={database['snapshots'].get(key)}")
    for key in (
        "duplicate_entity_identities",
        "duplicate_alias_identities",
        "orphan_aliases",
    ):
        if int(database["entities"].get(key) or 0):
            violations.append(f"entities.{key}={database['entities'].get(key)}")
    outbox = database["outbox"]
    if int(outbox.get("total") or 0) != target * 3:
        violations.append(f"outbox.total={outbox.get('total')}, expected={target * 3}")
    for event_type in l200.EXPECTED_OUTBOX_TYPES:
        actual = int((outbox.get("event_type_counts") or {}).get(event_type, 0))
        if actual != target:
            violations.append(f"outbox.{event_type}={actual}, expected={target}")
    for key in (
        "duplicate_event_ids",
        "backlog",
        "failed",
        "missing_expected_events",
        "identity_mismatches",
        "unexpected_canon_events",
    ):
        if int(outbox.get(key) or 0):
            violations.append(f"outbox.{key}={outbox.get(key)}")
    projections = {row["projection_kind"]: row for row in database["projections"]}
    if set(projections) != set(l200.EXPECTED_PROJECTIONS):
        violations.append(f"projection kinds mismatch: {sorted(projections)}")
    for kind in l200.EXPECTED_PROJECTIONS:
        row = projections.get(kind) or {}
        if row.get("status") != "healthy":
            violations.append(f"projection {kind} status={row.get('status')}")
        if int(row.get("target_chapter_number") or 0) != target:
            violations.append(f"projection {kind} target={row.get('target_chapter_number')}")
        if int(row.get("projected_chapter_number") or 0) != target:
            violations.append(f"projection {kind} projected={row.get('projected_chapter_number')}")
    maintenance = database["maintenance"]
    if int(maintenance.get("total") or 0) != target * 4:
        violations.append(f"maintenance.total={maintenance.get('total')}, expected={target * 4}")
    for step in l200.EXPECTED_MAINTENANCE_STEPS:
        actual = int((maintenance.get("step_counts") or {}).get(step, 0))
        if actual != target:
            violations.append(f"maintenance.{step}={actual}, expected={target}")
    for key in ("backlog", "duplicate_idempotency_keys", "duplicate_canon_steps"):
        if int(maintenance.get(key) or 0):
            violations.append(f"maintenance.{key}={maintenance.get(key)}")
    if int(database["tasks"].get("active_rows") or 0):
        violations.append(f"database active task rows={database['tasks'].get('active_rows')}")
    for key in (
        "unsettled_jobs",
        "duplicate_job_idempotency_keys",
        "duplicate_canon_platform_jobs",
        "duplicate_receipt_keys",
        "invalid_current_attempt_refs",
    ):
        if int(database["publisher"].get(key) or 0):
            violations.append(f"publisher.{key}={database['publisher'].get(key)}")
    return violations


async def collect_cell(
    args: argparse.Namespace,
    name: str,
    cell: dict[str, Any],
) -> dict[str, Any]:
    project_id = str(cell.get("project_id") or "")
    if not project_id:
        raise MatrixAuditError(f"{name} has no project_id")
    namespace = argparse.Namespace(mcp_url=args.mcp_url, project_id=project_id)
    mcp = await l200.collect_mcp_state(namespace)
    policy = await l200.fetch_policy(args.api_url, project_id)
    database = l200.collect_database_state(args.database_url, project_id)
    operational = operational_metrics(args.database_url, project_id)
    return {
        "name": name,
        "manifest_cell": cell,
        "project": mcp["project"],
        "chapters": mcp["chapters"],
        "active_task_check": mcp["active_task_check"],
        "tasks": mcp["tasks"],
        "gate_ledger": mcp["gate_ledger"],
        "cost_report": mcp["cost_report"],
        "rule_provenance": mcp["rule_provenance"],
        "policy": policy,
        "database": database,
        "candidate_spark_model": str(
            getattr(args, "candidate_spark_model", "") or ""
        ),
        "operational": operational,
    }


def _evidence_rows(
    spark: dict[str, Any],
    key: str,
    violations: list[str],
) -> list[dict[str, Any]]:
    value = spark.get(key)
    if not isinstance(value, list):
        violations.append(f"Spark evidence {key} must be a list")
        return []
    rows: list[dict[str, Any]] = []
    for index, row in enumerate(value):
        if not isinstance(row, dict):
            violations.append(
                f"Spark evidence {key}[{index}] must be an object"
            )
            continue
        rows.append(row)
    return rows


def _unique_rows(
    rows: list[dict[str, Any]],
    *,
    key: str,
    label: str,
    violations: list[str],
) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        identity = str(row.get(key) or "")
        if not identity:
            violations.append(f"{label} has no {key}")
            continue
        if identity in indexed:
            violations.append(f"duplicate {label} {key}={identity}")
            continue
        indexed[identity] = row
    return indexed


def _gate_outcome_violations(
    payload: dict[str, Any],
    *,
    request: dict[str, Any],
    gate_kind: str,
    evaluated: bool,
    trace_id: str,
    context: str,
) -> list[str]:
    violations: list[str] = []
    outcome = payload.get("gate_outcome")
    if not isinstance(outcome, dict):
        return [f"{context} has no structured gate_outcome"]
    expected = {
        "schema_version": 1,
        "gate_id": "delegation",
        "responsibility_domain": gate_kind,
        "scope": str(request.get("scope") or ""),
        "candidate_id": str(request.get("related_object_id") or ""),
        "chapter_number": int(request.get("chapter_number") or 0),
        "band_id": str(request.get("band_id") or ""),
        "evaluated": evaluated,
        "fired": True,
    }
    for key, expected_value in expected.items():
        if outcome.get(key) != expected_value:
            violations.append(
                f"{context} gate_outcome.{key}={outcome.get(key)!r}, "
                f"expected={expected_value!r}"
            )
    expected_trace_ids = [trace_id] if trace_id else []
    if outcome.get("trace_ids") != expected_trace_ids:
        violations.append(
            f"{context} gate_outcome.trace_ids="
            f"{outcome.get('trace_ids')!r}, expected={expected_trace_ids!r}"
        )
    return violations


def _review_snapshot_is_eligible(review: Any) -> bool:
    if not isinstance(review, dict):
        return False
    if review.get("verdict") not in {"pass", "warn"}:
        return False
    final_residual = review.get("final_residual_decision")
    if final_residual is not None:
        if not isinstance(final_residual, dict):
            return False
        if final_residual.get("decision") != "force_accept":
            return False
        if final_residual.get("canon_risk") == "high":
            return False
    verification = review.get("repair_verification")
    if verification is not None:
        if not isinstance(verification, dict):
            return False
        if not verification.get("fixed_all_must_fix"):
            return False
        if not verification.get("preserved_all_must_preserve"):
            return False
    residuals = review.get("residual_review_issues")
    if not isinstance(residuals, list):
        return False
    for issue in residuals:
        if not isinstance(issue, dict):
            return False
        if issue.get("blocking") or issue.get("severity") == "error":
            return False
    return True


def _eligibility_violations(
    request: dict[str, Any],
    trace: dict[str, Any],
    source: dict[str, Any] | None,
    task: dict[str, Any] | None,
    frozen_pause_policy: dict[str, Any],
) -> list[str]:
    request_id = str(request.get("id") or "")
    gate_kind = str((request.get("payload") or {}).get("gate_kind") or "")
    snapshot = trace.get("input_snapshot")
    violations: list[str] = []
    if not isinstance(snapshot, dict):
        return [f"request {request_id} has no eligibility snapshot"]
    snapshot_pause_policy = snapshot.get("pause_policy")
    if not isinstance(snapshot_pause_policy, dict):
        violations.append(
            f"request {request_id} has no pause policy snapshot"
        )
        snapshot_pause_policy = {}
    if snapshot_pause_policy != frozen_pause_policy:
        violations.append(
            f"request {request_id} pause policy snapshot differs "
            "from frozen project policy"
        )
    if snapshot_pause_policy.get("gate_delegate") != "spark":
        violations.append(
            f"request {request_id} snapshot is not Spark-delegated"
        )
    if source is None or not source.get("exists"):
        violations.append(
            f"request {request_id} related gate object does not exist"
        )
        return violations
    object_type = str(request.get("related_object_type") or "")
    object_id = str(request.get("related_object_id") or "")
    if source.get("object_type") != object_type:
        violations.append(
            f"request {request_id} related gate object type mismatch"
        )
    if source.get("object_id") != object_id:
        violations.append(
            f"request {request_id} related gate object id mismatch"
        )
    if source.get("project_id") != request.get("project_id"):
        violations.append(
            f"request {request_id} related gate project mismatch"
        )

    if object_type == "band_checkpoint":
        checkpoint = snapshot.get("checkpoint")
        if not isinstance(checkpoint, dict):
            return [
                *violations,
                f"request {request_id} has no checkpoint eligibility snapshot",
            ]
        status = str(checkpoint.get("status") or "")
        if status != "warn":
            violations.append(
                f"request {request_id} automatic checkpoint status="
                f"{status}, expected=warn"
            )
        if gate_kind != "band_checkpoint_pause":
            violations.append(
                f"request {request_id} checkpoint gate_kind={gate_kind}, "
                "expected=band_checkpoint_pause"
            )
        trigger_source = str(checkpoint.get("trigger_source") or "")
        if trigger_source != "auto_band_end":
            violations.append(
                f"request {request_id} automatic checkpoint "
                f"trigger_source={trigger_source}, expected=auto_band_end"
            )
        if checkpoint.get("boundary_kind") != "band_end":
            violations.append(
                f"request {request_id} automatic checkpoint "
                f"boundary_kind={checkpoint.get('boundary_kind')}, "
                "expected=band_end"
            )
        action = str(
            snapshot_pause_policy.get("band_checkpoint_action") or ""
        )
        if action not in {"pause_on_warn", "pause_always"}:
            violations.append(
                f"request {request_id} automatic checkpoint policy "
                f"action={action} is not pausing"
            )
        source_status = str(source.get("status") or "")
        if source_status not in {"warn", "overridden"}:
            violations.append(
                f"request {request_id} automatic checkpoint source "
                f"status={source_status} is inconsistent with a "
                "delegated warning"
            )
        for key, expected in (
            ("id", object_id),
            ("project_id", request.get("project_id")),
            ("band_id", request.get("band_id")),
            ("boundary_chapter", request.get("chapter_number")),
        ):
            if checkpoint.get(key) != expected:
                violations.append(
                    f"request {request_id} checkpoint.{key}="
                    f"{checkpoint.get(key)!r}, expected={expected!r}"
                )
        if request.get("scope") != "band":
            violations.append(
                f"request {request_id} checkpoint scope="
                f"{request.get('scope')}, expected=band"
            )
        for key in (
            "band_id",
            "trigger_source",
            "boundary_kind",
            "boundary_chapter",
        ):
            if source.get(key) != checkpoint.get(key):
                violations.append(
                    f"request {request_id} checkpoint source {key} mismatch"
                )
        return violations

    if object_type not in {"chapter_plan", "chapter_review"}:
        violations.append(
            f"request {request_id} unsupported related gate object "
            f"type={object_type}"
        )
        return violations
    if gate_kind != "chapter_review_interval":
        violations.append(
            f"request {request_id} chapter gate_kind={gate_kind} "
            "is not an eligible optional interval"
        )
    if request.get("scope") != "chapter":
        violations.append(
            f"request {request_id} chapter gate scope="
            f"{request.get('scope')}, expected=chapter"
        )
    chapter_plan = snapshot.get("chapter_plan")
    if not isinstance(chapter_plan, dict):
        return [
            *violations,
            f"request {request_id} has no chapter plan eligibility snapshot",
        ]
    chapter_number = int(request.get("chapter_number") or 0)
    interval = int(snapshot.get("review_interval_chapters") or 0)
    if snapshot_pause_policy.get("review_interval_chapters") != interval:
        violations.append(
            f"request {request_id} review interval policy mismatch"
        )
    if interval <= 0 or chapter_number <= 0 or chapter_number % interval:
        violations.append(
            f"request {request_id} chapter {chapter_number} is not on "
            f"review interval {interval}"
        )
    if snapshot.get("gate_reason") != f"review interval {interval} reached":
        violations.append(
            f"request {request_id} review interval reason mismatch"
        )
    if chapter_plan.get("chapter_number") != chapter_number:
        violations.append(
            f"request {request_id} chapter plan number mismatch"
        )
    if not _review_snapshot_is_eligible(snapshot.get("review_verdict")):
        violations.append(
            f"request {request_id} review verdict is not Canon-eligible"
        )
    last_requested = int(
        (task or {}).get("run_until_chapter") or 0
    )
    if last_requested <= 0:
        violations.append(
            f"request {request_id} task has no deterministic last "
            "requested chapter"
        )
    elif chapter_number == last_requested:
        violations.append(
            f"request {request_id} is at task last requested chapter "
            f"{last_requested}"
        )
    if source.get("chapter_number") != chapter_number:
        violations.append(
            f"request {request_id} related chapter number mismatch"
        )
    if object_type == "chapter_plan":
        if chapter_plan.get("id") != object_id:
            violations.append(
                f"request {request_id} chapter plan identity mismatch"
            )
        if snapshot.get("review_id"):
            violations.append(
                f"request {request_id} chapter-plan source has a review id"
            )
    else:
        if snapshot.get("review_id") != object_id:
            violations.append(
                f"request {request_id} chapter review identity mismatch"
            )
        if source.get("chapter_plan_id") != chapter_plan.get("id"):
            violations.append(
                f"request {request_id} review chapter plan mismatch"
            )
        if source.get("draft_id") != snapshot.get("draft_id"):
            violations.append(
                f"request {request_id} review draft mismatch"
            )
        review = snapshot.get("review_verdict")
        if (
            isinstance(review, dict)
            and source.get("verdict") != review.get("verdict")
        ):
            violations.append(
                f"request {request_id} review verdict source mismatch"
            )
    return violations


def spark_delegation_violations(
    spark: Any,
    *,
    project_id: str,
    delegate: str,
    tasks: Any,
    frozen_pause_policy: Any,
    candidate_spark_model: str,
) -> list[str]:
    if not isinstance(spark, dict):
        return ["Spark evidence must be an object"]
    violations: list[str] = []
    events = _evidence_rows(spark, "events", violations)
    traces = _evidence_rows(spark, "prompt_traces", violations)
    sources = _evidence_rows(
        spark,
        "related_gate_objects",
        violations,
    )
    roots = _evidence_rows(spark, "causal_roots", violations)
    if violations:
        return violations
    if delegate == "human":
        if events or traces or sources or roots:
            violations.append(
                "human cell unexpectedly contains Spark delegation evidence"
            )
        return violations
    if delegate != "spark":
        return [f"unsupported gate delegate={delegate}"]
    if not isinstance(tasks, list):
        violations.append("collected tasks must be a list")
        task_rows: list[dict[str, Any]] = []
    else:
        task_rows = [
            task for task in tasks if isinstance(task, dict)
        ]
        if len(task_rows) != len(tasks):
            violations.append("collected tasks contain a non-object row")
    if not isinstance(frozen_pause_policy, dict):
        violations.append("frozen pause policy must be an object")
        frozen_pause_policy = {}
    candidate_spark_model = str(candidate_spark_model or "").strip()
    if not candidate_spark_model:
        violations.append("candidate Spark model is empty")

    _unique_rows(
        events,
        key="id",
        label="Spark event",
        violations=violations,
    )
    _unique_rows(
        traces,
        key="id",
        label="PromptTrace",
        violations=violations,
    )
    sources_by_request = _unique_rows(
        sources,
        key="request_event_id",
        label="related gate object",
        violations=violations,
    )
    _unique_rows(
        roots,
        key="id",
        label="causal root event",
        violations=violations,
    )
    tasks_by_id = _unique_rows(
        task_rows,
        key="task_id",
        label="generation task",
        violations=violations,
    )
    for event in events:
        event_id = str(event.get("id") or "<missing>")
        event_type = str(event.get("event_type") or "")
        if event_type not in SPARK_EVENT_TYPES:
            violations.append(
                f"Spark event {event_id} has unsupported type={event_type}"
            )
        if event.get("project_id") != project_id:
            violations.append(
                f"Spark event {event_id} project identity mismatch"
            )
        if event.get("payload_error"):
            violations.append(
                f"Spark event {event_id} payload is invalid: "
                f"{event.get('payload_error')}"
            )
        if not isinstance(event.get("payload"), dict):
            violations.append(
                f"Spark event {event_id} payload must be an object"
            )
    for trace in traces:
        trace_id = str(trace.get("id") or "<missing>")
        if trace.get("project_id") != project_id:
            violations.append(
                f"PromptTrace {trace_id} project identity mismatch"
            )
        for field in (
            "input_snapshot",
            "model_profile",
            "output_summary",
        ):
            if trace.get(f"{field}_error"):
                violations.append(
                    f"PromptTrace {trace_id} {field} is invalid: "
                    f"{trace.get(f'{field}_error')}"
                )

    requests = [
        event
        for event in events
        if event.get("event_type") == "gate_delegation_requested"
    ]
    trace_events = [
        event
        for event in events
        if event.get("event_type") == "prompt_trace_recorded"
    ]
    terminals = [
        event
        for event in events
        if event.get("event_type") in SPARK_TERMINAL_TYPES
    ]
    approvals = [
        event
        for event in events
        if event.get("event_type") == "gate_delegation_approved"
    ]
    request_ids = {str(event.get("id") or "") for event in requests}
    for trace in traces:
        decision_event_id = str(trace.get("decision_event_id") or "")
        if decision_event_id not in request_ids:
            violations.append(
                f"PromptTrace {trace.get('id')} references unknown request "
                f"{decision_event_id}"
            )
    for source in sources:
        request_event_id = str(source.get("request_event_id") or "")
        if request_event_id not in request_ids:
            violations.append(
                "related gate object references unknown request "
                f"{request_event_id}"
            )

    consumed_trace_events: set[str] = set()
    consumed_terminals: set[str] = set()
    consumed_approvals: set[str] = set()
    consumed_roots: set[str] = set()
    for request in requests:
        request_id = str(request.get("id") or "")
        payload = request.get("payload")
        if not isinstance(payload, dict):
            continue
        gate_kind = str(payload.get("gate_kind") or "")
        requested_model = str(payload.get("requested_model") or "")
        if not gate_kind:
            violations.append(f"request {request_id} has no gate_kind")
        if not requested_model:
            violations.append(f"request {request_id} has no requested_model")
        elif requested_model != candidate_spark_model:
            violations.append(
                f"request {request_id} requested model={requested_model}, "
                f"candidate={candidate_spark_model}"
            )
        if payload.get("related_object_type") != request.get(
            "related_object_type"
        ):
            violations.append(
                f"request {request_id} related object type payload mismatch"
            )
        if payload.get("related_object_id") != request.get(
            "related_object_id"
        ):
            violations.append(
                f"request {request_id} related object id payload mismatch"
            )
        task_id = str(request.get("task_id") or "")
        task = tasks_by_id.get(task_id)
        if task is None:
            violations.append(
                f"request {request_id} task {task_id} is not in "
                "collected tasks"
            )
        elif task.get("project_id") != project_id:
            violations.append(
                f"request {request_id} task project identity mismatch"
            )
        causal_root_id = str(request.get("causal_root_id") or "")
        if not causal_root_id:
            violations.append(f"request {request_id} has no causal root")
        matching_roots = [
            root for root in roots if root.get("id") == causal_root_id
        ]
        if len(matching_roots) != 1:
            violations.append(
                f"request {request_id} has {len(matching_roots)} "
                "causal root events"
            )
        else:
            root = matching_roots[0]
            consumed_roots.add(causal_root_id)
            expected_root = {
                "project_id": project_id,
                "task_id": task_id,
                "scope": "task",
                "related_object_type": "generation_task",
                "related_object_id": task_id,
                "parent_event_id": "",
                "causal_root_id": causal_root_id,
            }
            if root.get("event_type") not in {
                "generation_requested",
                "continue_requested",
            }:
                violations.append(
                    f"request {request_id} causal root event_type="
                    f"{root.get('event_type')} is invalid"
                )
            for key, expected in expected_root.items():
                if root.get(key) != expected:
                    violations.append(
                        f"request {request_id} causal root {key} mismatch"
                    )
        violations.extend(
            _gate_outcome_violations(
                payload,
                request=request,
                gate_kind=gate_kind,
                evaluated=False,
                trace_id="",
                context=f"request {request_id}",
            )
        )
        request_outcome = payload.get("gate_outcome")
        if (
            isinstance(request_outcome, dict)
            and (
                request_outcome.get("decision") != "reject"
                or request_outcome.get("blocked") is not False
                or request_outcome.get("overridden_by") not in {"", None}
            )
        ):
            violations.append(
                f"request {request_id} unevaluated outcome is invalid"
            )

        matching_traces = [
            trace
            for trace in traces
            if trace.get("decision_event_id") == request_id
        ]
        if len(matching_traces) != 1:
            violations.append(
                f"request {request_id} has {len(matching_traces)} "
                "PromptTrace rows"
            )
            continue
        trace = matching_traces[0]
        trace_id = str(trace.get("id") or "")
        if trace.get("trace_scope") != "gate_delegation":
            violations.append(
                f"PromptTrace {trace_id} has wrong trace_scope"
            )
        if trace.get("stage_key") != f"gate_{gate_kind}":
            violations.append(
                f"PromptTrace {trace_id} stage_key does not match "
                f"{gate_kind}"
            )
        if trace.get("template_id") != "spark_pause_gate":
            violations.append(
                f"PromptTrace {trace_id} has wrong template_id"
            )
        if trace.get("template_version") != "v1":
            violations.append(
                f"PromptTrace {trace_id} has wrong template_version"
            )
        if trace.get("permission_profile") != SPARK_PERMISSION_PROFILE:
            violations.append(
                f"PromptTrace {trace_id} has wrong permission profile"
            )
        model_profile = trace.get("model_profile")
        if not isinstance(model_profile, dict):
            violations.append(
                f"PromptTrace {trace_id} model_profile must be an object"
            )
            model_profile = {}
        if model_profile.get("requested_model") != requested_model:
            violations.append(
                f"PromptTrace {trace_id} requested model mismatch"
            )
        if (
            model_profile.get("permission_profile")
            != SPARK_PERMISSION_PROFILE
        ):
            violations.append(
                f"PromptTrace {trace_id} model permission mismatch"
            )
        if model_profile.get("backend") != trace.get("backend"):
            violations.append(
                f"PromptTrace {trace_id} backend column mismatch"
            )

        matching_trace_events = [
            event
            for event in trace_events
            if event.get("parent_event_id") == request_id
        ]
        if len(matching_trace_events) != 1:
            violations.append(
                f"request {request_id} has {len(matching_trace_events)} "
                "prompt_trace_recorded events"
            )
            continue
        trace_event = matching_trace_events[0]
        trace_event_id = str(trace_event.get("id") or "")
        consumed_trace_events.add(trace_event_id)
        trace_payload = trace_event.get("payload")
        if not isinstance(trace_payload, dict):
            trace_payload = {}
        if (
            trace_event.get("related_object_type") != "prompt_trace"
            or trace_event.get("related_object_id") != trace_id
            or trace_payload.get("trace_id") != trace_id
        ):
            violations.append(
                f"request {request_id} prompt trace event linkage mismatch"
            )
        if trace_payload.get("gate_kind") != gate_kind:
            violations.append(
                f"request {request_id} prompt trace gate_kind mismatch"
            )
        for key, expected in (
            ("requested_model", requested_model),
            ("actual_model", model_profile.get("actual_model")),
            ("backend", model_profile.get("backend")),
        ):
            if trace_payload.get(key) != expected:
                violations.append(
                    f"request {request_id} prompt trace {key} mismatch"
                )

        matching_terminals = [
            event
            for event in terminals
            if event.get("parent_event_id") == trace_event_id
        ]
        consumed_terminals.update(
            str(event.get("id") or "") for event in matching_terminals
        )
        if len(matching_terminals) != 1:
            violations.append(
                f"request {request_id} has {len(matching_terminals)} "
                "terminal events"
            )
            continue
        terminal = matching_terminals[0]
        terminal_id = str(terminal.get("id") or "")
        for field in (
            "project_id",
            "task_id",
            "band_id",
            "chapter_number",
            "scope",
            "causal_root_id",
        ):
            if terminal.get(field) != request.get(field):
                violations.append(
                    f"request {request_id} terminal {field} mismatch"
                )
            if trace_event.get(field) != request.get(field):
                violations.append(
                    f"request {request_id} trace event {field} mismatch"
                )
        terminal_payload = terminal.get("payload")
        if not isinstance(terminal_payload, dict):
            terminal_payload = {}
        if (
            terminal.get("related_object_type") != "prompt_trace"
            or terminal.get("related_object_id") != trace_id
            or terminal_payload.get("trace_id") != trace_id
        ):
            violations.append(
                f"request {request_id} terminal trace linkage mismatch"
            )
        for key, expected in (
            ("gate_kind", gate_kind),
            ("requested_model", requested_model),
            (
                "actual_model",
                model_profile.get("actual_model"),
            ),
            ("backend", model_profile.get("backend")),
            (
                "gate_related_object_type",
                request.get("related_object_type"),
            ),
            ("gate_related_object_id", request.get("related_object_id")),
        ):
            if terminal_payload.get(key) != expected:
                violations.append(
                    f"request {request_id} terminal {key} mismatch"
                )
        output_summary = trace.get("output_summary")
        if not isinstance(output_summary, dict):
            violations.append(
                f"PromptTrace {trace_id} output_summary must be an object"
            )
            output_summary = {}
        for key in (
            "gate_kind",
            "requested_model",
            "actual_model",
            "backend",
        ):
            if output_summary.get(key) != terminal_payload.get(key):
                violations.append(
                    f"request {request_id} trace output {key} mismatch"
                )

        terminal_type = str(terminal.get("event_type") or "")
        outcome = terminal_payload.get("gate_outcome")
        violations.extend(
            _gate_outcome_violations(
                terminal_payload,
                request=request,
                gate_kind=gate_kind,
                evaluated=True,
                trace_id=trace_id,
                context=f"terminal {terminal_id}",
            )
        )
        if terminal_type == "gate_delegation_decided":
            if model_profile.get("actual_model") != requested_model:
                violations.append(
                    f"request {request_id} decided with unproven actual model"
                )
            if model_profile.get("backend") != "codex_bridge":
                violations.append(
                    f"request {request_id} decided outside codex_bridge"
                )
            if terminal_payload.get("failure_reason"):
                violations.append(
                    f"request {request_id} decided with failure_reason"
                )
            parsed_decision = output_summary.get("parsed_decision")
            if (
                not isinstance(parsed_decision, dict)
                or parsed_decision.get("decision")
                != terminal_payload.get("decision")
            ):
                violations.append(
                    f"request {request_id} parsed decision mismatch"
                )
            decision = terminal_payload.get("decision")
            if not isinstance(outcome, dict):
                decision = ""
            elif decision == "approve":
                if (
                    outcome.get("decision") != "approve"
                    or outcome.get("blocked") is not False
                    or outcome.get("overridden_by") != "spark"
                ):
                    violations.append(
                        f"request {request_id} approved outcome is invalid"
                    )
            elif decision == "reject":
                if (
                    outcome.get("decision") != "reject"
                    or outcome.get("blocked") is not True
                    or outcome.get("overridden_by") not in {"", None}
                ):
                    violations.append(
                        f"request {request_id} rejected outcome is invalid"
                    )
            else:
                violations.append(
                    f"request {request_id} decided with invalid decision="
                    f"{decision}"
                )
        else:
            if not terminal_payload.get("failure_reason"):
                violations.append(
                    f"request {request_id} failed without failure_reason"
                )
            if output_summary.get("failure_reason") != terminal_payload.get(
                "failure_reason"
            ):
                violations.append(
                    f"request {request_id} failure reason trace mismatch"
                )
            if (
                isinstance(outcome, dict)
                and (
                    outcome.get("decision") != "error"
                    or outcome.get("blocked") is not True
                    or outcome.get("overridden_by") not in {"", None}
                )
            ):
                violations.append(
                    f"request {request_id} failed outcome is invalid"
                )

        matching_approvals = [
            event
            for event in approvals
            if event.get("parent_event_id") == terminal_id
        ]
        consumed_approvals.update(
            str(event.get("id") or "") for event in matching_approvals
        )
        approved = (
            terminal_type == "gate_delegation_decided"
            and terminal_payload.get("decision") == "approve"
        )
        expected_approvals = 1 if approved else 0
        if len(matching_approvals) != expected_approvals:
            violations.append(
                f"request {request_id} has {len(matching_approvals)} "
                f"approval events, expected={expected_approvals}"
            )
        if matching_approvals:
            approval = matching_approvals[0]
            approval_payload = approval.get("payload")
            if not isinstance(approval_payload, dict):
                approval_payload = {}
            for field in (
                "project_id",
                "task_id",
                "band_id",
                "chapter_number",
                "scope",
                "causal_root_id",
            ):
                if approval.get(field) != request.get(field):
                    violations.append(
                        f"request {request_id} approval {field} mismatch"
                    )
            if (
                approval.get("related_object_type")
                != request.get("related_object_type")
                or approval.get("related_object_id")
                != request.get("related_object_id")
                or approval_payload.get("trace_id") != trace_id
                or approval_payload.get("gate_outcome") != outcome
            ):
                violations.append(
                    f"request {request_id} approval linkage mismatch"
                )

        violations.extend(
            _eligibility_violations(
                request,
                trace,
                sources_by_request.get(request_id),
                task,
                frozen_pause_policy,
            )
        )

    for trace_event in trace_events:
        trace_event_id = str(trace_event.get("id") or "")
        if trace_event_id not in consumed_trace_events:
            violations.append(
                f"orphan prompt_trace_recorded event {trace_event_id}"
            )
    for terminal in terminals:
        terminal_id = str(terminal.get("id") or "")
        if terminal_id not in consumed_terminals:
            violations.append(f"orphan Spark terminal event {terminal_id}")
    for approval in approvals:
        approval_id = str(approval.get("id") or "")
        if approval_id not in consumed_approvals:
            violations.append(f"orphan Spark approval event {approval_id}")
    for root in roots:
        root_id = str(root.get("id") or "")
        if root_id not in consumed_roots:
            violations.append(f"orphan causal root event {root_id}")
    return violations


def verified_spark_request_count(
    evidence: dict[str, Any],
) -> int:
    cell = evidence.get("manifest_cell") or {}
    delegate = str(cell.get("delegate") or "")
    project = evidence.get("project") or {}
    spark = (evidence.get("operational") or {}).get("spark")
    if spark_delegation_violations(
        spark,
        project_id=str(project.get("id") or ""),
        delegate=delegate,
        tasks=evidence.get("tasks"),
        frozen_pause_policy=(
            (evidence.get("policy") or {}).get("policy") or {}
        ).get("pause"),
        candidate_spark_model=str(
            evidence.get("candidate_spark_model") or ""
        ),
    ):
        return 0
    if not isinstance(spark, dict):
        return 0
    events = spark.get("events")
    if not isinstance(events, list):
        return 0
    return sum(
        1
        for event in events
        if isinstance(event, dict)
        and event.get("event_type") == "gate_delegation_requested"
    )


def spark_report_violations(
    *,
    spark: Any,
    gate_ledger: Any,
    cost_report: Any,
) -> list[str]:
    if not isinstance(spark, dict):
        return ["Spark evidence must be an object"]
    events = spark.get("events")
    traces = spark.get("prompt_traces")
    if not isinstance(events, list) or not isinstance(traces, list):
        return []
    request_rows = [
        event
        for event in events
        if isinstance(event, dict)
        and event.get("event_type") == "gate_delegation_requested"
    ]
    terminal_rows = [
        event
        for event in events
        if isinstance(event, dict)
        and event.get("event_type") in SPARK_TERMINAL_TYPES
    ]
    approval_count = sum(
        1
        for event in events
        if isinstance(event, dict)
        and event.get("event_type") == "gate_delegation_approved"
    )
    blocked_count = 0
    override_count = 0
    for terminal in terminal_rows:
        payload = terminal.get("payload")
        outcome = payload.get("gate_outcome") if isinstance(payload, dict) else None
        if isinstance(outcome, dict):
            blocked_count += int(bool(outcome.get("blocked")))
            override_count += int(bool(outcome.get("overridden_by")))
    expected_ledger = {
        "opportunities": len(request_rows),
        "evaluations": len(terminal_rows),
        "fires": len(terminal_rows),
        "blocks": blocked_count,
        "pauses": 0,
        "approvals": approval_count,
        "overrides": override_count,
        "unknown_legacy_count": 0,
    }
    violations: list[str] = []
    ledger_metrics = (
        gate_ledger.get("metrics")
        if isinstance(gate_ledger, dict)
        else None
    )
    delegation_metrics = (
        [
            metric
            for metric in ledger_metrics
            if isinstance(metric, dict)
            and metric.get("gate_id") == "delegation"
        ]
        if isinstance(ledger_metrics, list)
        else []
    )
    if request_rows and len(delegation_metrics) != 1:
        violations.append(
            "delegation gate ledger metric count="
            f"{len(delegation_metrics)}, expected=1"
        )
    for metric in delegation_metrics:
        for key, expected in expected_ledger.items():
            if metric.get(key) != expected:
                violations.append(
                    f"delegation gate ledger {key}={metric.get(key)}, "
                    f"expected={expected}"
                )
        expected_domains = sorted(
            {
                str((event.get("payload") or {}).get("gate_kind") or "")
                for event in request_rows
                if str((event.get("payload") or {}).get("gate_kind") or "")
            }
        )
        if request_rows and metric.get(
            "responsibility_domains"
        ) != expected_domains:
            violations.append(
                "delegation gate ledger responsibility domains mismatch"
            )

    gate_costs = (
        cost_report.get("gate_costs")
        if isinstance(cost_report, dict)
        else None
    )
    delegation_costs = (
        [
            item
            for item in gate_costs
            if isinstance(item, dict)
            and item.get("gate_id") == "delegation"
        ]
        if isinstance(gate_costs, list)
        else []
    )
    if traces and not delegation_costs:
        violations.append("delegation cost report metric is missing")
    if len(delegation_costs) > 1:
        violations.append(
            "delegation cost report metric count="
            f"{len(delegation_costs)}, expected at most 1"
        )
    if delegation_costs:
        metrics = delegation_costs[0].get("metrics")
        if not isinstance(metrics, dict):
            violations.append(
                "delegation cost report metrics must be an object"
            )
        else:
            attempts = int(metrics.get("attempts") or 0)
            successes = int(metrics.get("successes") or 0)
            decided = sum(
                1
                for terminal in terminal_rows
                if terminal.get("event_type") == "gate_delegation_decided"
            )
            if attempts < len(traces):
                violations.append(
                    f"delegation cost attempts={attempts}, "
                    f"expected at least {len(traces)}"
                )
            if successes < decided:
                violations.append(
                    f"delegation cost successes={successes}, "
                    f"expected at least {decided}"
                )
            if successes > attempts:
                violations.append(
                    f"delegation cost successes={successes} exceed "
                    f"attempts={attempts}"
                )
    return violations


def validate_completed_run(
    evidence: dict[str, Any],
    *,
    target: int,
    profile: str,
    delegate: str,
) -> list[str]:
    cell = evidence["manifest_cell"]
    project = evidence["project"]
    chapters = evidence["chapters"]
    violations: list[str] = []
    manifest_project_id = str(cell.get("project_id") or "")
    mcp_project_id = str(project.get("id") or "")
    if manifest_project_id != mcp_project_id:
        violations.append(
            f"manifest project_id={manifest_project_id}, "
            f"MCP project id={mcp_project_id}"
        )
    if cell.get("status") != "complete":
        violations.append(f"manifest status={cell.get('status')}, expected=complete")
    if int(cell.get("target") or 0) != target:
        violations.append(f"manifest target={cell.get('target')}, expected={target}")
    if cell.get("profile") != profile:
        violations.append(f"manifest profile={cell.get('profile')}")
    if cell.get("delegate") != delegate:
        violations.append(f"manifest delegate={cell.get('delegate')}")
    if int(project.get("target_total_chapters") or 0) != target:
        violations.append(f"project target={project.get('target_total_chapters')}")
    if int(project.get("accepted_chapter_count") or 0) != target:
        violations.append(f"project accepted={project.get('accepted_chapter_count')}")
    if int(project.get("needs_review_chapter_count") or 0):
        violations.append(f"project needs_review={project.get('needs_review_chapter_count')}")
    failed = (project.get("generation_control") or {}).get("failed_chapters") or []
    if failed:
        violations.append(f"project failed_chapters={failed}")
    if evidence["active_task_check"].get("has_active_generation_task"):
        violations.append("active generation task remains")
    expected_numbers = list(range(1, target + 1))
    accepted_numbers = sorted(
        int(item.get("chapter_number") or 0)
        for item in chapters
        if item.get("status") == "accepted"
    )
    if accepted_numbers != expected_numbers or len(chapters) != target:
        violations.append("chapters are not exactly accepted 1..target")
    live_policy = evidence["policy"].get("policy") or {}
    frozen_policy_version = int(cell.get("policy_version") or 0)
    live_policy_version = int(evidence["policy"].get("version") or 0)
    database_policy_version = int(
        (evidence["database"].get("freeze_audit") or {}).get(
            "runtime_policy_version"
        )
        or 0
    )
    if frozen_policy_version <= 0:
        violations.append(
            f"manifest policy_version={frozen_policy_version}, expected positive"
        )
    if live_policy_version != frozen_policy_version:
        violations.append(
            f"live policy version={live_policy_version}, "
            f"frozen={frozen_policy_version}"
        )
    if database_policy_version != frozen_policy_version:
        violations.append(
            f"database policy version={database_policy_version}, "
            f"frozen={frozen_policy_version}"
        )
    if l200.canonical_hash(live_policy) != str(cell.get("policy_hash") or ""):
        violations.append("live policy hash differs from frozen matrix policy")
    if live_policy.get("quality_profile") != profile:
        violations.append("live policy quality_profile mismatch")
    pause = live_policy.get("pause") or {}
    if pause.get("gate_delegate") != delegate:
        violations.append("live policy gate_delegate mismatch")
    violations.extend(report_contract_violations(str(project["id"]), evidence))
    violations.extend(integrity_violations(target, evidence["database"]))
    violations.extend(
        spark_delegation_violations(
            evidence["operational"].get("spark"),
            project_id=str(project["id"]),
            delegate=delegate,
            tasks=evidence.get("tasks"),
            frozen_pause_policy=pause,
            candidate_spark_model=str(
                evidence.get("candidate_spark_model") or ""
            ),
        )
    )
    violations.extend(
        spark_report_violations(
            spark=evidence["operational"].get("spark"),
            gate_ledger=evidence.get("gate_ledger"),
            cost_report=evidence.get("cost_report"),
        )
    )
    return violations


def matrix_operational_violations(results: dict[str, Any]) -> list[str]:
    requested = sum(
        verified_spark_request_count(item["evidence"])
        for item in results.values()
        if (item["evidence"].get("manifest_cell") or {}).get("delegate") == "spark"
    )
    if requested <= 0:
        return ["matrix has no live Spark delegation request evidence"]
    return []


def validate_cell(name: str, evidence: dict[str, Any]) -> list[str]:
    expected = EXPECTED_CELLS[name]
    return validate_completed_run(
        evidence,
        target=int(expected["target"]),
        profile=str(expected["profile"]),
        delegate=str(expected["delegate"]),
    )


def final_report(
    identity: dict[str, Any],
    results: dict[str, Any],
    matrix_violations: list[str] | None = None,
) -> str:
    matrix_violations = matrix_violations or []
    all_passed = not matrix_violations and all(
        not item["violations"] for item in results.values()
    )
    lines = [
        "# ForWin v5 R6 Matrix Final Audit",
        "",
        f"- Result: {'PASS' if all_passed else 'FAIL'}",
        f"- Source SHA: `{identity['source_sha']}`",
        "- Code changes during run: `0`",
        "",
        "| Cell | Target | Accepted | Review | Result |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    for name in EXPECTED_CELLS:
        item = results[name]
        project = item["evidence"]["project"]
        lines.append(
            f"| {name} | {EXPECTED_CELLS[name]['target']} | "
            f"{project.get('accepted_chapter_count', 0)} | "
            f"{project.get('needs_review_chapter_count', 0)} | "
            f"{'PASS' if not item['violations'] else 'FAIL'} |"
        )
    lines.extend(["", "## Findings", ""])
    if all_passed:
        lines.append("- All four matrix cells satisfy the R6 release invariants.")
    else:
        for violation in matrix_violations:
            lines.append(f"- `matrix`: {violation}")
        for name, item in results.items():
            for violation in item["violations"]:
                lines.append(f"- `{name}`: {violation}")
    lines.append("")
    return "\n".join(lines)


async def run(args: argparse.Namespace) -> int:
    args.database_url = str(os.environ.get(args.database_url_env) or "").strip()
    if not args.database_url:
        raise MatrixAuditError(
            f"database URL environment variable is empty: {args.database_url_env}"
        )
    matrix_path = args.matrix_manifest.resolve()
    manifest = load_json(matrix_path)
    if set(manifest.get("cells") or {}) != set(EXPECTED_CELLS):
        raise MatrixAuditError("matrix cell identities do not match release plan")
    identity = assert_matrix_identity(manifest, matrix_path)
    identity["candidate_stack"] = candidate_stack_identity(
        args,
        runtime_image_id=str(identity["runtime_image"]["image_id"]),
        browser_image_id=str(identity["browser_image"]["image_id"]),
    )
    args.candidate_spark_model = str(
        identity["candidate_stack"].get("spark_model") or ""
    )
    output = args.output_dir.resolve()
    prepare_output_directory(output)
    results: dict[str, Any] = {}
    for name in EXPECTED_CELLS:
        evidence = await collect_cell(args, name, manifest["cells"][name])
        violations = validate_cell(name, evidence)
        cell_dir = output / name
        l200.write_json(cell_dir / "evidence.json", evidence)
        results[name] = {
            "violations": violations,
            "evidence": evidence,
            "evidence_path": str(cell_dir / "evidence.json"),
            "evidence_sha256": l200.sha256_file(cell_dir / "evidence.json"),
        }
    matrix_violations = matrix_operational_violations(results)
    passed = not matrix_violations and all(
        not item["violations"] for item in results.values()
    )
    report_path = output / "final-report.md"
    l200.atomic_write(
        report_path,
        final_report(identity, results, matrix_violations),
    )
    audit_manifest = {
        "schema_version": MATRIX_AUDIT_SCHEMA_VERSION,
        "audited_at": now(),
        "result": "pass" if passed else "fail",
        "violations": matrix_violations,
        "identity": identity,
        "auditor": {
            "path": str(Path(__file__).resolve()),
            "source_path": str(Path(__file__).resolve().relative_to(ROOT)),
            "sha256": l200.sha256_file(Path(__file__).resolve()),
            "database_helper_path": str(L200_MODULE_PATH),
            "database_helper_source_path": str(
                L200_MODULE_PATH.relative_to(ROOT)
            ),
            "database_helper_sha256": l200.sha256_file(L200_MODULE_PATH),
        },
        "cells": {
            name: {
                "project_id": item["evidence"]["project"]["id"],
                "target": EXPECTED_CELLS[name]["target"],
                "violations": item["violations"],
                "evidence_path": item["evidence_path"],
                "evidence_sha256": item["evidence_sha256"],
            }
            for name, item in results.items()
        },
        "final_report": {
            "path": str(report_path),
            "sha256": l200.sha256_file(report_path),
        },
    }
    l200.write_json(output / "manifest.json", audit_manifest)
    print(report_path)
    return 0 if passed else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Independently finalize the immutable ForWin R6 matrix."
    )
    parser.add_argument(
        "--matrix-manifest",
        type=Path,
        default=MATRIX_ROOT / "manifest.json",
    )
    parser.add_argument("--mcp-url", required=True)
    parser.add_argument("--api-url", required=True)
    parser.add_argument(
        "--runtime-container",
        action="append",
        required=True,
        help="Candidate runtime container; repeat for all five runtime services.",
    )
    parser.add_argument(
        "--browser-container",
        action="append",
        required=True,
        help="Candidate publisher-browser container.",
    )
    parser.add_argument(
        "--dependency-container",
        action="append",
        required=True,
        help="Candidate dependency container; repeat for postgres, qdrant, and minio.",
    )
    parser.add_argument(
        "--database-url-env",
        default="FORWIN_MATRIX_DATABASE_URL",
        help="Environment variable containing the PostgreSQL SQLAlchemy URL.",
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> int:
    try:
        return asyncio.run(run(parse_args()))
    except (MatrixAuditError, l200.EvidenceError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
