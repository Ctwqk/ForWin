#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import ipaddress
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text


ROOT = Path(__file__).resolve().parents[2]
MATRIX_MODULE_PATH = Path(__file__).with_name("finalize_matrix.py")
L200_MODULE_PATH = Path(__file__).with_name("l200_evidence.py")
LIFECYCLE_MODULE_PATH = Path(__file__).with_name("smoke_lifecycle.py")
DEFAULT_OUTPUT = ROOT / ".artifacts/v5-post-decision-smoke"
TARGET = 30


class SmokeError(RuntimeError):
    pass


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise SmokeError(f"cannot load release helper: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


matrix = load_module("forwin_matrix_finalizer", MATRIX_MODULE_PATH)
l200 = load_module("forwin_smoke_l200_helpers", L200_MODULE_PATH)


def load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SmokeError(f"candidate manifest is missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SmokeError(f"candidate manifest is invalid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise SmokeError("candidate manifest must contain an object")
    return payload


def normalized_time(value: object) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except ValueError as exc:
        raise SmokeError(f"invalid ISO timestamp: {value}") from exc
    return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)


def candidate_identity(
    manifest_path: Path,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    source_sha = str((manifest.get("source") or {}).get("sha") or "")
    if not source_sha:
        raise SmokeError("candidate manifest has no source SHA")
    if matrix.command("git", "rev-parse", "HEAD") != source_sha:
        raise SmokeError("current HEAD differs from the smoke candidate")
    if matrix.command("git", "status", "--porcelain=v1", "--untracked-files=no"):
        raise SmokeError("tracked worktree is dirty")
    images = manifest.get("images") or {}
    verified_images: dict[str, Any] = {}
    for key in ("runtime", "publisher_browser"):
        expected = images.get(key) or {}
        tag = str(expected.get("tag") or "")
        if (
            not tag
            or not expected.get("image_id")
            or expected.get("revision") != source_sha
        ):
            raise SmokeError(f"candidate image identity is incomplete: {key}")
        actual = l200.image_identity(tag)
        if actual != {
            "tag": tag,
            "image_id": expected["image_id"],
            "revision": source_sha,
        }:
            raise SmokeError(f"candidate image identity changed: {key}")
        verified_images[key] = actual
    for key in ("postgres", "qdrant", "minio"):
        expected = images.get(key) or {}
        tag = str(expected.get("tag") or "")
        image_id = str(expected.get("image_id") or "")
        if not tag or not image_id:
            raise SmokeError(f"candidate dependency image is incomplete: {key}")
        actual = l200.image_identity(tag)
        if actual.get("image_id") != image_id:
            raise SmokeError(f"candidate dependency image changed: {key}")
        verified_images[key] = {
            "tag": tag,
            "image_id": image_id,
        }
    return {
        "source_sha": source_sha,
        "source_tree": matrix.command("git", "rev-parse", "HEAD^{tree}"),
        "tracked_worktree_clean": True,
        "candidate_manifest": {
            "path": str(manifest_path),
            "sha256": l200.sha256_file(manifest_path),
        },
        "images": verified_images,
    }


def identity_violations(
    candidate: dict[str, Any],
    identity: dict[str, Any],
) -> list[str]:
    violations: list[str] = []
    candidate_source = candidate.get("source") or {}
    source_sha = str(candidate_source.get("sha") or "")
    source_tree = str(candidate_source.get("tree") or "")
    if identity.get("source_sha") != source_sha:
        violations.append("smoke identity source SHA mismatch")
    if not source_tree or identity.get("source_tree") != source_tree:
        violations.append("smoke identity source tree mismatch")
    candidate_images = candidate.get("images") or {}
    identity_images = identity.get("images") or {}
    expected_image_keys = {
        "runtime",
        "publisher_browser",
        "postgres",
        "qdrant",
        "minio",
    }
    if set(identity_images) != expected_image_keys:
        violations.append("smoke identity image set mismatch")
    for key in expected_image_keys:
        candidate_image = candidate_images.get(key) or {}
        identity_image = identity_images.get(key) or {}
        for field in ("tag", "image_id"):
            if not identity_image.get(field) or (
                identity_image.get(field) != candidate_image.get(field)
            ):
                violations.append(
                    f"smoke identity image mismatch: {key}.{field}"
                )
        if key in {"runtime", "publisher_browser"} and (
            identity_image.get("revision") != source_sha
            or candidate_image.get("revision") != source_sha
        ):
            violations.append(f"smoke identity image revision mismatch: {key}")

    stack = identity.get("candidate_stack") or {}
    project = str(stack.get("compose_project") or "")
    groups = (
        (
            "runtime_containers",
            l200.EXPECTED_RUNTIME_SERVICES,
            str((identity_images.get("runtime") or {}).get("image_id") or ""),
        ),
        (
            "publisher_browser_containers",
            l200.EXPECTED_BROWSER_SERVICES,
            str(
                (identity_images.get("publisher_browser") or {}).get(
                    "image_id"
                )
                or ""
            ),
        ),
        (
            "dependency_containers",
            l200.EXPECTED_DEPENDENCY_SERVICES,
            "",
        ),
    )
    seen_services: set[str] = set()
    dependency_image_ids = {
        key: str((identity_images.get(key) or {}).get("image_id") or "")
        for key in l200.EXPECTED_DEPENDENCY_SERVICES
    }
    for group_name, expected_services, shared_image_id in groups:
        containers = stack.get(group_name)
        if not isinstance(containers, list):
            violations.append(f"smoke candidate stack group missing: {group_name}")
            continue
        services = {
            str((item or {}).get("compose_service") or "")
            for item in containers
            if isinstance(item, dict)
        }
        if services != set(expected_services) or len(containers) != len(
            expected_services
        ):
            violations.append(
                f"smoke candidate stack service set mismatch: {group_name}"
            )
        for item in containers:
            if not isinstance(item, dict):
                continue
            service = str(item.get("compose_service") or "")
            seen_services.add(service)
            if not project or item.get("compose_project") != project:
                violations.append(
                    f"smoke candidate stack project mismatch: {service}"
                )
            expected_image_id = (
                dependency_image_ids.get(service, "")
                if group_name == "dependency_containers"
                else shared_image_id
            )
            if (
                not expected_image_id
                or item.get("image_id") != expected_image_id
            ):
                violations.append(
                    f"smoke candidate stack image mismatch: {service}"
                )
    if seen_services != (
        set(l200.EXPECTED_RUNTIME_SERVICES)
        | set(l200.EXPECTED_BROWSER_SERVICES)
        | set(l200.EXPECTED_DEPENDENCY_SERVICES)
    ):
        violations.append("smoke candidate stack complete service set mismatch")
    bindings = stack.get("connection_bindings") or {}
    if bindings.get("compose_project") != project:
        violations.append("smoke connection binding project mismatch")
    expected_bindings = {
        "api": "forwin",
        "mcp": "forwin-mcp",
        "database": "postgres",
        "qdrant": "qdrant",
    }
    for key, service in expected_bindings.items():
        binding = bindings.get(key) or {}
        try:
            host = ipaddress.ip_address(str(binding.get("host_ip") or ""))
        except ValueError:
            host = None
        if (
            binding.get("compose_service") != service
            or host is None
            or not host.is_loopback
            or int(binding.get("host_port") or 0) <= 0
        ):
            violations.append(f"smoke connection binding mismatch: {key}")
    return violations


def fresh_project_state(database_url: str, project_id: str) -> dict[str, Any]:
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    """
                    SELECT
                      (SELECT count(*) FROM projects) AS projects,
                      (SELECT created_at FROM projects WHERE id=:project_id)
                        AS project_created_at
                    """
                ),
                {"project_id": project_id},
            ).mappings().one()
        return {
            "projects": int(row["projects"] or 0),
            "project_created_at": str(row["project_created_at"] or ""),
        }
    finally:
        engine.dispose()


def summarize_task_policy_snapshots(
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for row in rows:
        task_id = str(row.get("id") or "")
        try:
            payload = json.loads(str(row.get("execution_payload_json") or ""))
        except json.JSONDecodeError as exc:
            raise SmokeError(
                f"generation task has invalid execution payload: {task_id}"
            ) from exc
        if not task_id or not isinstance(payload, dict):
            raise SmokeError("generation task execution payload identity is invalid")
        policy = payload.get("policy_snapshot")
        policy_version = int(payload.get("policy_version") or 0)
        if not isinstance(policy, dict) or policy_version <= 0:
            raise SmokeError(
                f"generation task has no valid policy snapshot: {task_id}"
            )
        items.append(
            {
                "task_id": task_id,
                "status": str(row.get("status") or ""),
                "policy_version": policy_version,
                "payload_sha256": l200.canonical_hash(payload),
                "policy_snapshot_sha256": l200.canonical_hash(policy),
            }
        )
    return {"count": len(items), "items": items}


def task_policy_snapshots(database_url: str, project_id: str) -> dict[str, Any]:
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            rows = [
                dict(row)
                for row in connection.execute(
                    text(
                        """
                        SELECT id,status,execution_payload_json
                        FROM generation_tasks
                        WHERE project_id=:project_id
                          AND task_kind='generation'
                        ORDER BY created_at,id
                        """
                    ),
                    {"project_id": project_id},
                ).mappings()
            ]
        return summarize_task_policy_snapshots(rows)
    finally:
        engine.dispose()


def lifecycle_violations(evidence: dict[str, Any]) -> list[str]:
    violations: list[str] = []
    project = evidence.get("project") or {}
    genesis = evidence.get("genesis") or {}
    project_id = str(project.get("id") or "")
    if project.get("creation_status") != "writing":
        violations.append("smoke project did not complete the writing handoff")
    if genesis.get("project_id") != project_id:
        violations.append("Genesis project identity mismatch")
    if genesis.get("creation_status") != "writing":
        violations.append("Genesis did not complete the writing handoff")
    stages = {
        str(item.get("stage_key") or ""): item
        for item in genesis.get("stage_states") or []
        if isinstance(item, dict)
    }
    if set(stages) != set(l200.GENESIS_STAGES):
        violations.append(f"Genesis stage identities mismatch: {sorted(stages)}")
    for stage in l200.GENESIS_STAGES:
        item = stages.get(stage) or {}
        if item.get("status") != "locked" or item.get("locked") is not True:
            violations.append(f"Genesis stage is not locked: {stage}")
    task_evidence = evidence.get("task_policy_snapshots") or {}
    task_items = task_evidence.get("items")
    if not isinstance(task_items, list) or not task_items:
        violations.append("generation task policy snapshots are missing")
        task_items = []
    if int(task_evidence.get("count") or 0) != len(task_items):
        violations.append("generation task policy snapshot count mismatch")
    live_policy_response = evidence.get("policy") or {}
    live_policy = live_policy_response.get("policy") or {}
    live_policy_version = int(live_policy_response.get("version") or 0)
    expected_policy_hash = l200.canonical_hash(live_policy)
    task_ids: set[str] = set()
    for item in task_items:
        task_id = str((item or {}).get("task_id") or "")
        if not task_id or task_id in task_ids:
            violations.append("generation task policy snapshot identity mismatch")
            continue
        task_ids.add(task_id)
        if str(item.get("status") or "") not in l200.TERMINAL_GENERATION_STATUSES:
            violations.append(f"task {task_id} is not terminal")
        if (
            live_policy_version <= 0
            or int(item.get("policy_version") or 0) != live_policy_version
        ):
            violations.append(f"task {task_id} policy version mismatch")
        if item.get("policy_snapshot_sha256") != expected_policy_hash:
            violations.append(
                f"task {task_id} policy snapshot differs from live policy"
            )
        payload_hash = str(item.get("payload_sha256") or "")
        if len(payload_hash) != 64:
            violations.append(f"task {task_id} payload hash is invalid")
    return violations


def operation_transcript_violations(
    candidate: dict[str, Any],
    identity: dict[str, Any],
    transcript: dict[str, Any],
    evidence: dict[str, Any],
) -> list[str]:
    violations: list[str] = []
    source = candidate.get("source") or {}
    project_id = str((evidence.get("project") or {}).get("id") or "")
    if (
        int(transcript.get("schema_version") or 0) != 1
        or transcript.get("result") != "handoff_started"
        or int(transcript.get("target") or 0) != TARGET
    ):
        violations.append("smoke lifecycle transcript summary mismatch")
    if (
        transcript.get("source_sha") != source.get("sha")
        or transcript.get("source_tree") != source.get("tree")
    ):
        violations.append("smoke lifecycle transcript source identity mismatch")
    if transcript.get("project_id") != project_id:
        violations.append("smoke lifecycle transcript project identity mismatch")
    candidate_artifact = transcript.get("candidate_manifest") or {}
    if candidate_artifact != identity.get("candidate_manifest"):
        violations.append("smoke lifecycle candidate manifest identity mismatch")
    harness = transcript.get("harness") or {}
    if (
        Path(str(harness.get("path") or "")).resolve()
        != LIFECYCLE_MODULE_PATH.resolve()
        or not LIFECYCLE_MODULE_PATH.is_file()
        or l200.sha256_file(LIFECYCLE_MODULE_PATH)
        != harness.get("sha256")
    ):
        violations.append("smoke lifecycle harness identity mismatch")
    release_files = {
        str(item.get("path") or ""): str(item.get("sha256") or "")
        for item in (candidate.get("release_harness") or {}).get("files") or []
        if isinstance(item, dict)
    }
    lifecycle_source_path = LIFECYCLE_MODULE_PATH.relative_to(ROOT).as_posix()
    if release_files.get(lifecycle_source_path) != harness.get("sha256"):
        violations.append("smoke lifecycle harness is not bound by candidate source")
    bindings = (
        (identity.get("candidate_stack") or {}).get("connection_bindings") or {}
    )

    def binding_url(name: str, path: str = "") -> str:
        binding = bindings.get(name) or {}
        try:
            host = ipaddress.ip_address(str(binding.get("host_ip") or ""))
            port = int(binding.get("host_port") or 0)
        except (ValueError, TypeError):
            return ""
        if not host.is_loopback or port <= 0:
            return ""
        rendered_host = f"[{host}]" if host.version == 6 else str(host)
        return f"http://{rendered_host}:{port}{path}"

    if (
        transcript.get("api_url") != binding_url("api")
        or transcript.get("mcp_url") != binding_url("mcp", "/mcp")
    ):
        violations.append("smoke lifecycle endpoint binding mismatch")

    expected_sequence = [
        ("project_create", "", "mcp_http"),
        ("project_policy_update", "", "http"),
    ]
    for stage in l200.GENESIS_STAGES:
        expected_sequence.extend(
            [
                ("genesis_stage_generate", stage, "mcp_http"),
                ("genesis_stage_lock", stage, "mcp_http"),
            ]
        )
    expected_sequence.extend(
        [
            ("task_active_generation_check", "", "mcp_http"),
            ("project_start_writing", "", "mcp_http"),
        ]
    )
    operations = transcript.get("operations")
    if not isinstance(operations, list):
        operations = []
    actual_sequence = [
        (
            str((item or {}).get("tool") or ""),
            str((item or {}).get("stage_key") or ""),
            str((item or {}).get("transport") or ""),
        )
        for item in operations
        if isinstance(item, dict)
    ]
    if actual_sequence != expected_sequence:
        violations.append("smoke lifecycle operation sequence mismatch")
    if int(transcript.get("operation_count") or 0) != len(operations):
        violations.append("smoke lifecycle operation count mismatch")
    previous = "0" * 64
    run_id = str(transcript.get("run_id") or "")
    request_ids: set[str] = set()
    for index, operation in enumerate(operations, start=1):
        if not isinstance(operation, dict):
            violations.append("smoke lifecycle operation is not an object")
            continue
        operation_hash = str(operation.get("operation_sha256") or "")
        expected_hash = l200.canonical_hash(
            {
                key: value
                for key, value in operation.items()
                if key != "operation_sha256"
            }
        )
        request_id = str(operation.get("request_id") or "")
        if (
            int(operation.get("index") or 0) != index
            or operation.get("previous_operation_sha256") != previous
            or operation_hash != expected_hash
            or operation.get("run_id") != run_id
            or operation.get("project_id") != project_id
            or operation.get("result_ok") is not True
            or len(str(operation.get("arguments_sha256") or "")) != 64
            or not request_id
            or request_id in request_ids
        ):
            violations.append(
                f"smoke lifecycle operation integrity mismatch: {index}"
            )
        request_ids.add(request_id)
        previous = operation_hash
    if (
        not run_id
        or not operations
        or transcript.get("operation_chain_head") != previous
    ):
        violations.append("smoke lifecycle operation chain head mismatch")
    handoff_task_id = str(transcript.get("handoff_task_id") or "")
    if (
        not operations
        or str((operations[-1] or {}).get("task_id") or "")
        != handoff_task_id
    ):
        violations.append("smoke lifecycle handoff task identity mismatch")
    task_ids = {
        str((item or {}).get("task_id") or "")
        for item in (
            (evidence.get("task_policy_snapshots") or {}).get("items") or []
        )
        if isinstance(item, dict)
    }
    if not handoff_task_id or handoff_task_id not in task_ids:
        violations.append("smoke lifecycle handoff task is absent from final state")
    try:
        candidate_time = normalized_time(candidate.get("collected_at"))
        started_at = normalized_time(transcript.get("started_at"))
        completed_at = normalized_time(transcript.get("completed_at"))
        if started_at <= candidate_time or completed_at <= started_at:
            violations.append("smoke lifecycle transcript timestamps are invalid")
    except SmokeError as exc:
        violations.append(str(exc))
    return violations


def smoke_violations(
    candidate_manifest: dict[str, Any],
    evidence: dict[str, Any],
    fresh_state: dict[str, Any],
    *,
    profile: str,
    delegate: str,
) -> list[str]:
    violations = matrix.validate_completed_run(
        evidence,
        target=TARGET,
        profile=profile,
        delegate=delegate,
    )
    policy = candidate_manifest.get("runtime_policy") or {}
    if policy.get("quality_profile") != profile:
        violations.append("candidate quality profile differs from smoke selection")
    if policy.get("gate_delegate") != delegate:
        violations.append("candidate gate delegate differs from smoke selection")
    violations.extend(lifecycle_violations(evidence))
    if int(fresh_state.get("projects") or 0) != 1:
        violations.append(
            f"smoke database projects={fresh_state.get('projects')}, expected=1"
        )
    try:
        candidate_time = normalized_time(candidate_manifest.get("collected_at"))
        project_time = normalized_time(fresh_state.get("project_created_at"))
        if project_time <= candidate_time:
            violations.append(
                "smoke project was not created after candidate collection"
            )
    except SmokeError as exc:
        violations.append(str(exc))
    return violations


def report(identity: dict[str, Any], evidence: dict[str, Any], violations: list[str]) -> str:
    project = evidence["project"]
    lines = [
        "# ForWin v5 Post-Decision Fresh 30 Smoke",
        "",
        f"- Result: {'PASS' if not violations else 'FAIL'}",
        f"- Source SHA: `{identity['source_sha']}`",
        f"- Project: `{project.get('id', '')}`",
        f"- Accepted: `{project.get('accepted_chapter_count', 0)}` / `{TARGET}`",
        f"- Needs review: `{project.get('needs_review_chapter_count', 0)}`",
        (
            "- Locked Genesis stages: "
            f"`{len((evidence.get('genesis') or {}).get('stage_states') or [])}` "
            f"/ `{len(l200.GENESIS_STAGES)}`"
        ),
        (
            "- Task policy snapshots: "
            f"`{(evidence.get('task_policy_snapshots') or {}).get('count', 0)}`"
        ),
        "",
        "## Findings",
        "",
    ]
    lines.extend(
        ["- All fresh-30 release-smoke invariants passed."]
        if not violations
        else [f"- {item}" for item in violations]
    )
    lines.append("")
    return "\n".join(lines)


async def run(args: argparse.Namespace) -> int:
    database_url = str(os.environ.get(args.database_url_env) or "").strip()
    if not database_url:
        raise SmokeError(
            f"database URL environment variable is empty: {args.database_url_env}"
        )
    args.database_url = database_url
    manifest_path = args.candidate_manifest.resolve()
    candidate = load_json(manifest_path)
    transcript_path = args.operation_transcript.resolve()
    transcript = load_json(transcript_path)
    identity = candidate_identity(manifest_path, candidate)
    identity["candidate_stack"] = matrix.candidate_stack_identity(
        args,
        runtime_image_id=str(identity["images"]["runtime"]["image_id"]),
        browser_image_id=str(
            identity["images"]["publisher_browser"]["image_id"]
        ),
    )
    identity_errors = identity_violations(candidate, identity)
    if identity_errors:
        raise SmokeError("; ".join(identity_errors))
    cell = {
        "project_id": args.project_id,
        "status": "complete",
        "target": TARGET,
        "profile": args.quality_profile,
        "delegate": args.gate_delegate,
        "policy_hash": "",
    }
    namespace = argparse.Namespace(
        mcp_url=args.mcp_url,
        api_url=args.api_url,
        database_url=database_url,
    )
    evidence = await matrix.collect_cell(namespace, "fresh-30", cell)
    evidence["genesis"] = await l200.fetch_genesis(args.mcp_url, args.project_id)
    evidence["task_policy_snapshots"] = task_policy_snapshots(
        database_url,
        args.project_id,
    )
    live_policy = evidence["policy"].get("policy") or {}
    cell["policy_hash"] = l200.canonical_hash(live_policy)
    fresh_state = fresh_project_state(database_url, args.project_id)
    violations = smoke_violations(
        candidate,
        evidence,
        fresh_state,
        profile=args.quality_profile,
        delegate=args.gate_delegate,
    )
    violations.extend(
        operation_transcript_violations(
            candidate,
            identity,
            transcript,
            evidence,
        )
    )

    output = args.output_dir.resolve()
    if output.exists() and any(output.iterdir()):
        raise SmokeError(f"smoke output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    evidence_path = output / "evidence.json"
    l200.write_json(evidence_path, evidence)
    report_path = output / "final-report.md"
    l200.atomic_write(report_path, report(identity, evidence, violations))
    manifest = {
        "schema_version": 1,
        "source_sha": identity["source_sha"],
        "result": "pass" if not violations else "fail",
        "target": TARGET,
        "project_id": args.project_id,
        "quality_profile": args.quality_profile,
        "gate_delegate": args.gate_delegate,
        "accepted": int(evidence["project"].get("accepted_chapter_count") or 0),
        "needs_review": int(
            evidence["project"].get("needs_review_chapter_count") or 0
        ),
        "has_active_generation_task": bool(
            evidence["active_task_check"].get("has_active_generation_task")
        ),
        "code_changes_during_run": 0,
        "identity": identity,
        "fresh_project": fresh_state,
        "violations": violations,
        "evidence": {
            "path": str(evidence_path),
            "sha256": l200.sha256_file(evidence_path),
        },
        "final_report": {
            "path": str(report_path),
            "sha256": l200.sha256_file(report_path),
        },
        "operation_transcript": {
            "path": str(transcript_path),
            "sha256": l200.sha256_file(transcript_path),
        },
        "auditor": {
            "path": str(Path(__file__).resolve()),
            "sha256": l200.sha256_file(Path(__file__).resolve()),
            "matrix_helper_path": str(MATRIX_MODULE_PATH),
            "matrix_helper_sha256": l200.sha256_file(MATRIX_MODULE_PATH),
            "database_helper_path": str(L200_MODULE_PATH),
            "database_helper_sha256": l200.sha256_file(L200_MODULE_PATH),
            "lifecycle_runner_path": str(LIFECYCLE_MODULE_PATH),
            "lifecycle_runner_sha256": l200.sha256_file(
                LIFECYCLE_MODULE_PATH
            ),
        },
    }
    l200.write_json(output / "manifest.json", manifest)
    print(report_path)
    return 0 if not violations else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Finalize the fresh 30-chapter post-decision RC smoke."
    )
    parser.add_argument("--candidate-manifest", type=Path, required=True)
    parser.add_argument("--operation-transcript", type=Path, required=True)
    parser.add_argument("--project-id", required=True)
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
        default="FORWIN_SMOKE_DATABASE_URL",
    )
    parser.add_argument(
        "--quality-profile",
        choices=("standard", "pulp"),
        required=True,
    )
    parser.add_argument(
        "--gate-delegate",
        choices=("human", "spark"),
        required=True,
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> int:
    try:
        return asyncio.run(run(parse_args()))
    except (SmokeError, matrix.MatrixAuditError, l200.EvidenceError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
