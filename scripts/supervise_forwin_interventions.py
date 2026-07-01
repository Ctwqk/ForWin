#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import warnings
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.monitor_forwin_runtime import (
    http_json,
    json_loads_result,
    redact_sensitive,
    run_command,
    utc_now,
)


LOGIN_ERROR_PARTS = ("login", "登录", "未登录", "过期", "扫码", "authenticate", "auth")
ATTENTION_REVIEW_DECISIONS = {"CHANGES_REQUESTED"}
ATTENTION_MERGE_STATES = {"BLOCKED", "DIRTY", "UNKNOWN"}
TERMINAL_FAILED_STATUSES = {"failed", "error"}
TERMINAL_SUCCESS_STATUSES = {"completed", "succeeded", "success"}


def _short_text(value: Any, *, limit: int = 320) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _append_block(
    blocked_items: list[dict[str, Any]],
    *,
    kind: str,
    message: str,
    severity: str = "warning",
    **context: Any,
) -> None:
    item = {"kind": kind, "severity": severity, "message": _short_text(message)}
    for key, value in context.items():
        if value not in (None, "", [], {}):
            item[key] = value
    blocked_items.append(item)


def _action(actions_taken: list[dict[str, Any]], kind: str, **context: Any) -> None:
    item = {"kind": kind}
    for key, value in context.items():
        if value not in (None, "", [], {}):
            item[key] = value
    actions_taken.append(item)


def _gh_json(args: list[str]) -> dict[str, Any]:
    proc = run_command(args, timeout=20)
    if not proc.get("ok"):
        error = proc.get("error") or proc.get("stderr") or proc.get("stdout") or "gh command failed"
        return {"ok": False, "error": _short_text(error)}
    try:
        payload = json.loads(str(proc.get("stdout") or ""))
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"gh returned invalid JSON: {exc}"}
    if not isinstance(payload, list):
        return {"ok": False, "error": "gh returned a non-list payload"}
    return {"ok": True, "items": payload}


def http_json_full(url: str, *, timeout: float = 15.0, max_bytes: int = 8_000_000) -> dict[str, Any]:
    try:
        with urlopen(url, timeout=timeout) as response:
            status = int(getattr(response, "status", 0) or 0)
            body = response.read(max_bytes + 1)
    except HTTPError as exc:
        return {"ok": False, "status": exc.code, "error": str(exc)}
    except URLError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}
    if len(body) > max_bytes:
        return {"ok": False, "status": status, "error": f"response exceeded {max_bytes} bytes"}
    try:
        payload: Any = json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        return {"ok": False, "status": status, "error": f"invalid JSON response: {exc}"}
    return {"ok": 200 <= status < 300, "status": status, "payload": payload}


def _summarize_status_check_rollup(items: Any) -> dict[str, int]:
    counts = {"total": 0, "failed": 0, "pending": 0}
    if not isinstance(items, list):
        return counts
    for item in items:
        if not isinstance(item, dict):
            continue
        counts["total"] += 1
        state = str(item.get("state") or item.get("status") or item.get("conclusion") or "").upper()
        if state in {"FAILURE", "FAILED", "ERROR", "TIMED_OUT", "ACTION_REQUIRED", "CANCELLED"}:
            counts["failed"] += 1
        elif state in {"PENDING", "QUEUED", "IN_PROGRESS", "REQUESTED", "WAITING"}:
            counts["pending"] += 1
    return counts


def github_prs_snapshot(
    repo: str,
    limit: int,
    blocked_items: list[dict[str, Any]],
    actions_taken: list[dict[str, Any]],
) -> dict[str, Any]:
    if not repo:
        return {"ok": True, "skipped": True, "reason": "github repo not configured", "prs": []}
    fields = "number,title,url,isDraft,reviewDecision,mergeStateStatus,updatedAt,statusCheckRollup"
    result = _gh_json(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--limit",
            str(limit),
            "--json",
            fields,
        ]
    )
    if not result.get("ok"):
        _append_block(
            blocked_items,
            kind="github_prs_unavailable",
            severity="warning",
            message=str(result.get("error") or "GitHub PR check failed"),
        )
        return {"ok": False, "error": result.get("error"), "prs": []}
    prs: list[dict[str, Any]] = []
    for item in result.get("items", []):
        if not isinstance(item, dict):
            continue
        checks = _summarize_status_check_rollup(item.get("statusCheckRollup"))
        pr = {
            "number": item.get("number"),
            "title": _short_text(item.get("title")),
            "url": item.get("url"),
            "is_draft": bool(item.get("isDraft")),
            "review_decision": item.get("reviewDecision") or "",
            "merge_state_status": item.get("mergeStateStatus") or "",
            "updated_at": item.get("updatedAt") or "",
            "checks": checks,
        }
        prs.append(pr)
        review_decision = str(pr["review_decision"]).upper()
        merge_state = str(pr["merge_state_status"]).upper()
        if (
            review_decision in ATTENTION_REVIEW_DECISIONS
            or merge_state in ATTENTION_MERGE_STATES
            or checks["failed"] > 0
        ):
            _append_block(
                blocked_items,
                kind="github_pr_needs_attention",
                severity="warning",
                message=f"PR #{pr['number']} needs attention",
                url=pr.get("url"),
                review_decision=pr["review_decision"],
                merge_state_status=pr["merge_state_status"],
                failed_checks=checks["failed"],
            )
    _action(actions_taken, "checked_github_prs", count=len(prs))
    return {"ok": True, "count": len(prs), "prs": prs}


def github_issues_snapshot(
    repo: str,
    limit: int,
    blocked_items: list[dict[str, Any]],
    actions_taken: list[dict[str, Any]],
) -> dict[str, Any]:
    if not repo:
        return {"ok": True, "skipped": True, "reason": "github repo not configured", "issues": []}
    fields = "number,title,url,labels,assignees,updatedAt"
    result = _gh_json(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--limit",
            str(limit),
            "--json",
            fields,
        ]
    )
    if not result.get("ok"):
        _append_block(
            blocked_items,
            kind="github_issues_unavailable",
            severity="warning",
            message=str(result.get("error") or "GitHub issue check failed"),
        )
        return {"ok": False, "error": result.get("error"), "issues": []}
    issues: list[dict[str, Any]] = []
    for item in result.get("items", []):
        if not isinstance(item, dict):
            continue
        labels = item.get("labels") if isinstance(item.get("labels"), list) else []
        assignees = item.get("assignees") if isinstance(item.get("assignees"), list) else []
        issues.append(
            {
                "number": item.get("number"),
                "title": _short_text(item.get("title")),
                "url": item.get("url"),
                "labels": [_short_text(label.get("name")) for label in labels if isinstance(label, dict)],
                "assignees": [_short_text(user.get("login")) for user in assignees if isinstance(user, dict)],
                "updated_at": item.get("updatedAt") or "",
            }
        )
    _action(actions_taken, "checked_github_issues", count=len(issues))
    return {"ok": True, "count": len(issues), "issues": issues}


def _is_login_error(job: dict[str, Any]) -> bool:
    haystack = " ".join(
        str(job.get(key) or "") for key in ("message", "error", "status", "current_url")
    ).lower()
    return any(part.lower() in haystack for part in LOGIN_ERROR_PARTS)


def _summarize_upload_job(job: dict[str, Any]) -> dict[str, Any]:
    result_payload = job.get("result_payload") if isinstance(job.get("result_payload"), dict) else {}
    intervention = (
        result_payload.get("codex_intervention")
        if isinstance(result_payload.get("codex_intervention"), dict)
        else {}
    )
    summary = {
        "job_id": job.get("job_id"),
        "task_kind": job.get("task_kind") or "chapter_upload",
        "project_id": job.get("project_id") or "",
        "platform": job.get("platform") or "",
        "status": job.get("status") or "",
        "book_name": _short_text(job.get("book_name")),
        "chapter_title": _short_text(job.get("chapter_title")),
        "publish": bool(job.get("publish")),
        "message": _short_text(job.get("message")),
        "error": _short_text(job.get("error")),
        "updated_at": job.get("updated_at") or "",
        "created_at": job.get("created_at") or "",
        "finished_at": job.get("finished_at") or "",
    }
    if intervention:
        summary["codex_intervention"] = {
            "status": _short_text(intervention.get("status")),
            "has_call": isinstance(intervention.get("call"), dict),
            "error": _short_text(intervention.get("error")),
        }
    return summary


def upload_jobs_snapshot(
    api_base: str,
    limit: int,
    blocked_items: list[dict[str, Any]],
    actions_taken: list[dict[str, Any]],
) -> dict[str, Any]:
    query = urlencode({"limit": max(1, min(int(limit or 30), 100))})
    response = http_json_full(f"{api_base.rstrip('/')}/api/publishers/upload-jobs?{query}")
    payload = response.get("payload")
    if not response.get("ok") or not isinstance(payload, list):
        _append_block(
            blocked_items,
            kind="upload_jobs_unavailable",
            severity="warning",
            message=str(response.get("error") or "publisher upload jobs API unavailable"),
        )
        return {"ok": False, "error": response.get("error") or "unexpected upload jobs response", "jobs": []}
    jobs = [_summarize_upload_job(item) for item in payload if isinstance(item, dict)]
    recovered_upload_keys: set[tuple[str, str, bool]] = set()
    for raw, job in zip([item for item in payload if isinstance(item, dict)], jobs, strict=False):
        status = str(job.get("status") or "").lower()
        recovery_key = (
            str(job.get("platform") or ""),
            str(job.get("task_kind") or "chapter_upload"),
            bool(raw.get("publish")),
        )
        recovered_by_newer_success = bool(recovery_key in recovered_upload_keys)
        if status in TERMINAL_SUCCESS_STATUSES:
            recovered_upload_keys.add(recovery_key)
        if status in TERMINAL_FAILED_STATUSES and not recovered_by_newer_success:
            _append_block(
                blocked_items,
                kind="upload_job_failed",
                severity="warning",
                message=f"Upload job {job.get('job_id')} failed",
                job_id=job.get("job_id"),
                platform=job.get("platform"),
            )
        if _is_login_error(raw) and not recovered_by_newer_success:
            _append_block(
                blocked_items,
                kind="publisher_login_required",
                severity="human",
                message=f"{job.get('platform') or 'publisher'} login appears required for upload job",
                job_id=job.get("job_id"),
                platform=job.get("platform"),
            )
        intervention = job.get("codex_intervention") if isinstance(job.get("codex_intervention"), dict) else {}
        if intervention.get("status") in {"request_failed", "required"} and not recovered_by_newer_success:
            _append_block(
                blocked_items,
                kind="codex_intervention_required",
                severity="codex",
                message=f"Upload job {job.get('job_id')} needs Codex intervention",
                job_id=job.get("job_id"),
                platform=job.get("platform"),
            )
        if raw.get("publish") is False:
            _action(
                actions_taken,
                "observed_non_publishing_upload_job",
                job_id=job.get("job_id"),
                platform=job.get("platform"),
                status=job.get("status"),
            )
    _action(actions_taken, "checked_upload_jobs", count=len(jobs))
    return {"ok": True, "count": len(jobs), "jobs": jobs}


async def _generation_tasks_snapshot_async(mcp_url: str) -> dict[str, Any]:
    warnings.simplefilter("ignore")
    try:
        from fastmcp import Client
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"fastmcp import failed: {exc}"}
    try:
        async with Client(mcp_url) as client:
            active = json_loads_result(await client.call_tool("task_active_generation_check", {}))
            snapshot: dict[str, Any] = {
                "ok": True,
                "has_active_generation_task": bool(active.get("has_active_generation_task")),
                "active_task_ids": active.get("active_task_ids", []),
                "safe_to_restart": bool(active.get("safe_to_restart")),
                "message": active.get("message", ""),
                "tasks": [],
            }
            try:
                task_list = json_loads_result(await client.call_tool("task_list", {"limit": 10}))
            except Exception as exc:  # noqa: BLE001
                snapshot["task_list_error"] = f"{exc.__class__.__name__}: {exc}"
            else:
                raw_tasks = task_list.get("tasks", task_list.get("items", []))
                if isinstance(raw_tasks, list):
                    snapshot["tasks"] = [
                        {
                            "task_id": task.get("task_id") or task.get("id"),
                            "status": task.get("status") or "",
                            "project_id": task.get("project_id") or "",
                            "current_stage": task.get("current_stage") or "",
                            "current_chapter": task.get("current_chapter"),
                            "heartbeat_at": task.get("heartbeat_at") or "",
                            "message": _short_text(task.get("message")),
                            "error": _short_text(task.get("error")),
                        }
                        for task in raw_tasks
                        if isinstance(task, dict)
                    ]
            return snapshot
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}


def generation_tasks_snapshot(mcp_url: str) -> dict[str, Any]:
    return asyncio.run(_generation_tasks_snapshot_async(mcp_url))


def classify_generation_tasks(
    snapshot: dict[str, Any],
    blocked_items: list[dict[str, Any]],
    actions_taken: list[dict[str, Any]],
) -> dict[str, Any]:
    if not snapshot.get("ok"):
        _append_block(
            blocked_items,
            kind="generation_tasks_unavailable",
            severity="warning",
            message=str(snapshot.get("error") or "generation task check failed"),
        )
        return snapshot
    if snapshot.get("has_active_generation_task"):
        _action(
            actions_taken,
            "observed_active_generation_task",
            active_task_ids=snapshot.get("active_task_ids", []),
        )
    latest_project_seen: set[str] = set()
    recovered_projects: set[str] = set()
    for task in snapshot.get("tasks", []):
        if not isinstance(task, dict):
            continue
        status = str(task.get("status") or "").lower()
        project_id = str(task.get("project_id") or "")
        superseded_by_project_success = bool(project_id and project_id in recovered_projects)
        if project_id and project_id not in latest_project_seen:
            latest_project_seen.add(project_id)
            if status in TERMINAL_SUCCESS_STATUSES:
                recovered_projects.add(project_id)
        if status in {"failed", "error"} and not superseded_by_project_success:
            _append_block(
                blocked_items,
                kind="generation_task_failed",
                severity="codex",
                message=f"Generation task {task.get('task_id')} failed",
                task_id=task.get("task_id"),
                project_id=task.get("project_id"),
            )
        elif status in {"paused", "needs_review"} and not superseded_by_project_success:
            _append_block(
                blocked_items,
                kind="generation_task_needs_operator",
                severity="operator",
                message=f"Generation task {task.get('task_id')} is {status}",
                task_id=task.get("task_id"),
                project_id=task.get("project_id"),
            )
    _action(actions_taken, "checked_generation_tasks")
    return snapshot


def publisher_browser_heartbeat_snapshot(
    api_base: str,
    expected_platforms: list[str],
    blocked_items: list[dict[str, Any]],
    actions_taken: list[dict[str, Any]],
) -> dict[str, Any]:
    response = http_json(f"{api_base.rstrip('/')}/api/publishers/platforms")
    payload = response.get("payload")
    if not response.get("ok") or not isinstance(payload, list):
        _append_block(
            blocked_items,
            kind="publisher_heartbeat_unavailable",
            severity="warning",
            message=str(response.get("error") or "publisher platforms API unavailable"),
        )
        return {"ok": False, "error": response.get("error") or "unexpected publisher platform response", "platforms": []}
    platforms: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        preferred = item.get("preferred_client_state") if isinstance(item.get("preferred_client_state"), dict) else {}
        latest = item.get("latest_client_state") if isinstance(item.get("latest_client_state"), dict) else {}
        session = item.get("browser_session_state") if isinstance(item.get("browser_session_state"), dict) else {}
        platforms.append(
            {
                "platform_id": str(item.get("platform_id") or ""),
                "connected": bool(item.get("connected")),
                "preferred_connected": bool(preferred.get("connected")),
                "latest_connected": bool(latest.get("connected")),
                "session_connected": bool(session.get("connected")),
                "last_heartbeat_at": item.get("last_heartbeat_at") or "",
            }
        )
    by_id = {item["platform_id"]: item for item in platforms}
    missing_expected = sorted(
        platform for platform in expected_platforms if not by_id.get(platform, {}).get("connected")
    )
    for platform in missing_expected:
        _append_block(
            blocked_items,
            kind="publisher_login_required",
            severity="human",
            message=f"{platform} publisher login is not connected",
            platform=platform,
        )
    _action(actions_taken, "checked_publisher_browser_heartbeat", count=len(platforms))
    return {
        "ok": not missing_expected,
        "missing_expected": missing_expected,
        "platforms": platforms,
    }


def codex_bridge_health_snapshot(
    api_base: str,
    blocked_items: list[dict[str, Any]],
    actions_taken: list[dict[str, Any]],
) -> dict[str, Any]:
    response = http_json(f"{api_base.rstrip('/')}/api/settings/codex/health")
    payload = response.get("payload")
    if not isinstance(payload, dict):
        _append_block(
            blocked_items,
            kind="codex_bridge_unavailable",
            severity="warning",
            message=str(response.get("error") or "Codex Bridge health API unavailable"),
        )
        return {"ok": False, "error": response.get("error") or "unexpected Codex Bridge health response"}
    summary = {
        "ok": bool(payload.get("healthy")),
        "enabled": bool(payload.get("enabled")),
        "healthy": bool(payload.get("healthy")),
        "status": payload.get("status") or "",
        "backend": payload.get("backend") or "codex_bridge",
        "message": _short_text(payload.get("message")),
        "bridge_url": payload.get("bridge_url") or "",
    }
    if not summary["ok"]:
        _append_block(
            blocked_items,
            kind="codex_bridge_unhealthy",
            severity="codex",
            message=str(summary.get("message") or summary.get("status") or "Codex Bridge is unhealthy"),
            status=summary.get("status"),
            enabled=summary.get("enabled"),
        )
    _action(actions_taken, "checked_codex_bridge_health", status=summary.get("status"))
    return summary


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    blocked_items: list[dict[str, Any]] = []
    actions_taken: list[dict[str, Any]] = []
    expected_platforms = list(getattr(args, "expect_platform_connected", []) or [])
    if getattr(args, "skip_github", False):
        github_prs = {"ok": True, "skipped": True, "reason": "skip_github enabled", "prs": []}
        github_issues = {"ok": True, "skipped": True, "reason": "skip_github enabled", "issues": []}
    else:
        github_prs = github_prs_snapshot(
            str(getattr(args, "github_repo", "") or ""),
            int(getattr(args, "github_limit", 20) or 20),
            blocked_items,
            actions_taken,
        )
        github_issues = github_issues_snapshot(
            str(getattr(args, "github_repo", "") or ""),
            int(getattr(args, "github_limit", 20) or 20),
            blocked_items,
            actions_taken,
        )
    upload_jobs = upload_jobs_snapshot(
        str(getattr(args, "api_base", "") or ""),
        int(getattr(args, "upload_job_limit", 30) or 30),
        blocked_items,
        actions_taken,
    )
    generation_tasks = classify_generation_tasks(
        generation_tasks_snapshot(str(getattr(args, "mcp_url", "") or "")),
        blocked_items,
        actions_taken,
    )
    publisher_heartbeat = publisher_browser_heartbeat_snapshot(
        str(getattr(args, "api_base", "") or ""),
        expected_platforms,
        blocked_items,
        actions_taken,
    )
    codex_bridge = codex_bridge_health_snapshot(
        str(getattr(args, "api_base", "") or ""),
        blocked_items,
        actions_taken,
    )
    report = {
        "checked_at": utc_now(),
        "github_prs_checked": github_prs,
        "issues_checked": github_issues,
        "upload_jobs_checked": upload_jobs,
        "generation_tasks_checked": generation_tasks,
        "publisher_browser_heartbeat": publisher_heartbeat,
        "codex_bridge_health": codex_bridge,
        "actions_taken": actions_taken,
        "blocked_items": blocked_items,
    }
    return redact_sensitive(report)


def write_jsonl(path: str | Path, report: dict[str, Any]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(report, ensure_ascii=False, sort_keys=True) + "\n")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only two-hour ForWin intervention supervisor.")
    parser.add_argument("--api-base", default="http://127.0.0.1:8899")
    parser.add_argument("--mcp-url", default="http://127.0.0.1:8896/mcp")
    parser.add_argument("--github-repo", default="Ctwqk/ForWin")
    parser.add_argument("--github-limit", type=int, default=20)
    parser.add_argument("--upload-job-limit", type=int, default=30)
    parser.add_argument("--output-jsonl", default="")
    parser.add_argument("--latest-json", default="")
    parser.add_argument("--skip-github", action="store_true")
    parser.add_argument("--no-fail-on-blocked", action="store_true")
    parser.add_argument(
        "--expect-platform-connected",
        action="append",
        default=[],
        help="Publisher platform id that should be connected, such as fanqie or qidian.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = build_report(args)
    if args.output_jsonl:
        write_jsonl(args.output_jsonl, report)
    if args.latest_json:
        latest = Path(args.latest_json)
        latest.parent.mkdir(parents=True, exist_ok=True)
        latest.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True), flush=True)
    has_blocks = bool(report.get("blocked_items"))
    return 0 if args.no_fail_on_blocked or not has_blocks else 1


if __name__ == "__main__":
    sys.exit(main())
