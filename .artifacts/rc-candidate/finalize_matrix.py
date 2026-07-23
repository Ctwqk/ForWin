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
EXPECTED_CELLS = {
    "L30": {"target": 30, "profile": "standard", "delegate": "human"},
    "L60S": {"target": 60, "profile": "standard", "delegate": "spark"},
    "L60P": {"target": 60, "profile": "pulp", "delegate": "human"},
    "L100": {"target": 100, "profile": "standard", "delegate": "spark"},
}


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
    }
    identity["connection_bindings"] = l200.connection_bindings(args, identity)
    return identity


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
                "spark": {
                    key: int(events.get(key, 0))
                    for key in (
                        "gate_delegation_requested",
                        "gate_delegation_decided",
                        "gate_delegation_approved",
                        "gate_delegation_failed",
                    )
                },
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
        "operational": operational,
    }


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
    if l200.canonical_hash(live_policy) != str(cell.get("policy_hash") or ""):
        violations.append("live policy hash differs from frozen matrix policy")
    if live_policy.get("quality_profile") != profile:
        violations.append("live policy quality_profile mismatch")
    pause = live_policy.get("pause") or {}
    if pause.get("gate_delegate") != delegate:
        violations.append("live policy gate_delegate mismatch")
    violations.extend(report_contract_violations(str(project["id"]), evidence))
    violations.extend(integrity_violations(target, evidence["database"]))
    spark = evidence["operational"]["spark"]
    requested = int(spark.get("gate_delegation_requested") or 0)
    terminal = int(spark.get("gate_delegation_decided") or 0) + int(
        spark.get("gate_delegation_failed") or 0
    )
    if delegate == "spark":
        if requested <= 0:
            violations.append("Spark cell has no delegation request evidence")
        if terminal != requested:
            violations.append(
                f"Spark delegation terminal count={terminal}, requested={requested}"
            )
    elif requested:
        violations.append(f"human cell unexpectedly requested Spark {requested} times")
    return violations


def validate_cell(name: str, evidence: dict[str, Any]) -> list[str]:
    expected = EXPECTED_CELLS[name]
    return validate_completed_run(
        evidence,
        target=int(expected["target"]),
        profile=str(expected["profile"]),
        delegate=str(expected["delegate"]),
    )


def final_report(identity: dict[str, Any], results: dict[str, Any]) -> str:
    all_passed = all(not item["violations"] for item in results.values())
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
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
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
    passed = all(not item["violations"] for item in results.values())
    report_path = output / "final-report.md"
    l200.atomic_write(report_path, final_report(identity, results))
    audit_manifest = {
        "schema_version": 1,
        "audited_at": now(),
        "result": "pass" if passed else "fail",
        "identity": identity,
        "auditor": {
            "path": str(Path(__file__).resolve()),
            "sha256": l200.sha256_file(Path(__file__).resolve()),
            "database_helper_path": str(L200_MODULE_PATH),
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
