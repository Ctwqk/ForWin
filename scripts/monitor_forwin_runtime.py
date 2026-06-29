#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


REPO_ROOT = Path(__file__).resolve().parents[1]
SENSITIVE_KEY_PARTS = (
    "api_key",
    "authorization",
    "csrf",
    "password",
    "secret",
    "token",
)
SENSITIVE_EXACT_KEYS = {"cookie", "cookies", "set-cookie"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).lower()
            if normalized in SENSITIVE_EXACT_KEYS or any(part in normalized for part in SENSITIVE_KEY_PARTS):
                redacted[str(key)] = "[redacted]"
            else:
                redacted[str(key)] = redact_sensitive(item)
        return redacted
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    return value


def json_loads_result(result: Any) -> dict[str, Any]:
    text = "".join(getattr(item, "text", "") for item in getattr(result, "content", []))
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text[:500]}
    return payload if isinstance(payload, dict) else {"value": payload}


def http_json(url: str, *, timeout: float = 5.0) -> dict[str, Any]:
    try:
        with urlopen(url, timeout=timeout) as response:
            status = int(getattr(response, "status", 0) or 0)
            body = response.read(65536).decode("utf-8", errors="replace")
    except HTTPError as exc:
        return {"ok": False, "status": exc.code, "error": str(exc)}
    except URLError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}
    try:
        payload: Any = json.loads(body)
    except json.JSONDecodeError:
        payload = {"raw": body[:500]}
    ok = 200 <= status < 300
    if isinstance(payload, dict) and payload.get("status") not in (None, "ok"):
        ok = False
    return {"ok": ok, "status": status, "payload": redact_sensitive(payload)}


def run_command(args: list[str], *, timeout: float = 15.0) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            args,
            cwd=str(REPO_ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        return {"ok": False, "error": str(exc)}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "command timed out"}
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


def parse_replicas(value: str) -> tuple[int, int] | None:
    parts = str(value or "").split("/", 1)
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def _container_status_is_running(status: str) -> bool:
    return "Up " in str(status or "")


def _containers_snapshot_from_output(output: str, required_services: set[str], *, source: str) -> dict[str, Any]:
    services: list[dict[str, Any]] = []
    for line in str(output or "").splitlines():
        parts = line.split(maxsplit=2)
        if len(parts) < 3:
            continue
        name, image, status = parts
        service_name = name.split(".", 1)[0]
        if service_name not in required_services:
            continue
        services.append(
            {
                "name": service_name,
                "container": name,
                "image": image,
                "status": status,
                "running": _container_status_is_running(status),
            }
        )
    seen = {item["name"] for item in services if item.get("running")}
    missing = sorted(required_services - seen)
    return {"ok": not missing, "source": source, "missing": missing, "services": services}


def docker_services_snapshot(context: str, *, colima_profile: str = "") -> dict[str, Any]:
    required = {
        "forwin-app-swarm",
        "forwin-generation-worker-swarm",
        "forwin-mcp-swarm",
        "forwin-publisher-worker-swarm",
        "forwin-outbox-worker-swarm",
    }
    proc = run_command(
        ["docker", "--context", context, "service", "ls", "--filter", "name=forwin"],
        timeout=15,
    )
    context_error = ""
    if proc.get("ok"):
        services: list[dict[str, Any]] = []
        for line in str(proc.get("stdout") or "").splitlines()[1:]:
            parts = line.split()
            if len(parts) < 5:
                continue
            replicas = parse_replicas(parts[3])
            services.append(
                {
                    "name": parts[1],
                    "mode": parts[2],
                    "replicas": parts[3],
                    "image": parts[4],
                    "running": bool(replicas and replicas[1] > 0 and replicas[0] >= replicas[1]),
                }
            )
        seen = {item["name"] for item in services if item.get("running")}
        missing = sorted(required - seen)
        return {"ok": not missing, "source": f"docker-context:{context}", "missing": missing, "services": services}
    context_error = str(proc.get("stderr") or proc.get("error") or proc.get("stdout") or "")

    if colima_profile:
        colima_proc = run_command(
            [
                "colima",
                "ssh",
                "-p",
                colima_profile,
                "--",
                "docker",
                "ps",
                "--format",
                "{{.Names}} {{.Image}} {{.Status}}",
            ],
            timeout=15,
        )
        if colima_proc.get("ok"):
            snapshot = _containers_snapshot_from_output(
                str(colima_proc.get("stdout") or ""),
                required,
                source=f"colima:{colima_profile}",
            )
            snapshot["context_error"] = context_error
            return snapshot
        return {
            "ok": False,
            "source": f"colima:{colima_profile}",
            "error": colima_proc.get("stderr") or colima_proc.get("error") or colima_proc.get("stdout"),
            "context_error": context_error,
        }

    return {"ok": False, "source": f"docker-context:{context}", "error": context_error}


def publisher_platforms_snapshot(api_base: str, expected_platforms: set[str]) -> dict[str, Any]:
    response = http_json(f"{api_base.rstrip('/')}/api/publishers/platforms")
    payload = response.get("payload")
    if not response.get("ok") or not isinstance(payload, list):
        return {"ok": False, "error": response.get("error") or "unexpected publisher platform response"}
    platforms: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        platform_id = str(item.get("platform_id") or "")
        preferred = item.get("preferred_client_state") if isinstance(item.get("preferred_client_state"), dict) else {}
        latest = item.get("latest_client_state") if isinstance(item.get("latest_client_state"), dict) else {}
        session = item.get("browser_session_state") if isinstance(item.get("browser_session_state"), dict) else {}
        platforms.append(
            {
                "platform_id": platform_id,
                "connected": bool(item.get("connected")),
                "preferred_connected": bool(preferred.get("connected")),
                "latest_connected": bool(latest.get("connected")),
                "session_connected": bool(session.get("connected")),
                "last_heartbeat_at": item.get("last_heartbeat_at"),
            }
        )
    by_id = {item["platform_id"]: item for item in platforms}
    missing_expected = sorted(
        platform for platform in expected_platforms if not by_id.get(platform, {}).get("connected")
    )
    return {"ok": not missing_expected, "missing_expected": missing_expected, "platforms": platforms}


async def mcp_snapshot(mcp_url: str, project_id: str, task_id: str) -> dict[str, Any]:
    warnings.simplefilter("ignore")
    try:
        from fastmcp import Client
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"fastmcp import failed: {exc}"}
    try:
        async with Client(mcp_url) as client:
            args = {"project_id": project_id} if project_id else {}
            active = json_loads_result(await client.call_tool("task_active_generation_check", args))
            snapshot: dict[str, Any] = {
                "ok": not bool(active.get("has_active_generation_task")),
                "active_generation": {
                    "has_active_generation_task": bool(active.get("has_active_generation_task")),
                    "active_task_ids": active.get("active_task_ids", []),
                    "safe_to_restart": bool(active.get("safe_to_restart")),
                    "message": active.get("message", ""),
                },
            }
            if task_id:
                task = json_loads_result(await client.call_tool("task_get", {"task_id": task_id}))
                snapshot["task"] = {
                    "task_id": task.get("task_id"),
                    "status": task.get("status"),
                    "current_stage": task.get("current_stage"),
                    "current_chapter": task.get("current_chapter"),
                    "completed_chapters": task.get("completed_chapters", []),
                    "failed_chapters": task.get("failed_chapters", []),
                    "heartbeat_at": task.get("heartbeat_at"),
                    "message": task.get("message"),
                    "error": task.get("error"),
                }
            if project_id:
                project = json_loads_result(await client.call_tool("project_get", {"project_id": project_id}))
                snapshot["project"] = {
                    "project_id": project.get("id"),
                    "title": project.get("title"),
                    "creation_status": project.get("creation_status"),
                    "generated_chapter_count": project.get("generated_chapter_count"),
                    "accepted_chapter_count": project.get("accepted_chapter_count"),
                    "needs_review_chapter_count": project.get("needs_review_chapter_count"),
                    "next_gate": project.get("next_gate"),
                }
            return snapshot
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}


def build_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    expected = set(args.expect_platform_connected or [])
    snapshot = {
        "timestamp": utc_now(),
        "api_health": http_json(f"{args.api_base.rstrip('/')}/health"),
        "mcp_health": http_json(args.mcp_health_url),
        "docker_services": docker_services_snapshot(args.docker_context, colima_profile=args.colima_profile),
        "publisher_platforms": publisher_platforms_snapshot(args.api_base, expected),
        "mcp": asyncio.run(mcp_snapshot(args.mcp_url, args.project_id, args.task_id)),
    }
    required_checks = ["api_health", "mcp_health", "docker_services"]
    snapshot["ok"] = all(bool(snapshot[name].get("ok")) for name in required_checks)
    if expected:
        snapshot["ok"] = bool(snapshot["ok"] and snapshot["publisher_platforms"].get("ok"))
    return redact_sensitive(snapshot)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only ForWin runtime monitor.")
    parser.add_argument("--api-base", default="http://127.0.0.1:8899")
    parser.add_argument("--mcp-url", default="http://127.0.0.1:8896/mcp")
    parser.add_argument("--mcp-health-url", default="http://127.0.0.1:8896/health")
    parser.add_argument("--docker-context", default="swarm-manager-150")
    parser.add_argument("--colima-profile", default="")
    parser.add_argument("--project-id", default="")
    parser.add_argument("--task-id", default="")
    parser.add_argument("--duration-minutes", type=float, default=120.0)
    parser.add_argument("--duration-seconds", type=float, default=None)
    parser.add_argument("--interval-seconds", type=float, default=60.0)
    parser.add_argument("--log-file", default="")
    parser.add_argument(
        "--expect-platform-connected",
        action="append",
        default=[],
        help="Platform id that must report connected=true, such as fanqie or qidian.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    duration = float(args.duration_seconds) if args.duration_seconds is not None else float(args.duration_minutes) * 60.0
    interval = max(float(args.interval_seconds), 1.0)
    log_file = Path(args.log_file) if args.log_file else REPO_ROOT / ".codex-monitor" / f"forwin-runtime-{int(time.time())}.jsonl"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max(duration, 0.0)
    failures = 0
    samples = 0
    while True:
        snapshot = build_snapshot(args)
        samples += 1
        if not snapshot.get("ok"):
            failures += 1
        line = json.dumps(snapshot, ensure_ascii=True, sort_keys=True)
        with log_file.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        print(line, flush=True)
        if time.monotonic() >= deadline:
            break
        time.sleep(min(interval, max(deadline - time.monotonic(), 0.0)))
    summary = {"timestamp": utc_now(), "samples": samples, "failures": failures, "log_file": str(log_file)}
    print(json.dumps(summary, ensure_ascii=True, sort_keys=True), flush=True)
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
