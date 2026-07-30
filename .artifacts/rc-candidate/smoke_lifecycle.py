#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import ipaddress
import json
import os
import subprocess
import sys
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

import httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport


ROOT = Path(__file__).resolve().parents[2]
ARTIFACT_DIR = Path(__file__).resolve().parent
if str(ARTIFACT_DIR) not in sys.path:
    sys.path.insert(0, str(ARTIFACT_DIR))

from release_http_auth import (  # noqa: E402
    BasicAuthConfigurationError,
    basic_auth_credentials,
)

HOST_ENV_ALLOWLIST = frozenset(
    {
        "HOME",
        "LANG",
        "LC_ALL",
        "LOGNAME",
        "PATH",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TMPDIR",
        "USER",
    }
)
CONTROL_ENV_KEYS = frozenset(
    {
        "COMPOSE_DISABLE_ENV_FILE",
        "COMPOSE_ENV_FILES",
        "COMPOSE_FILE",
        "COMPOSE_PATH_SEPARATOR",
        "COMPOSE_PROFILES",
        "COMPOSE_PROJECT_NAME",
        "DOCKER_CERT_PATH",
        "DOCKER_CONFIG",
        "DOCKER_CONTEXT",
        "DOCKER_HOST",
        "DOCKER_TLS_VERIFY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_WORK_TREE",
    }
)
GENESIS_STAGES = (
    "brief",
    "world",
    "map",
    "story_engine",
    "book_blueprint",
    "bootstrap",
)
TARGET = 30


class LifecycleError(RuntimeError):
    pass


def direct_api_client(
    *,
    base_url: str,
    headers: Mapping[str, str] | None = None,
    timeout: float = 60,
    source: Mapping[str, str] | None = None,
) -> httpx.AsyncClient:
    try:
        credentials = basic_auth_credentials(source)
    except BasicAuthConfigurationError as exc:
        raise LifecycleError(str(exc)) from exc
    return httpx.AsyncClient(
        base_url=base_url,
        timeout=timeout,
        auth=(
            httpx.BasicAuth(*credentials)
            if credentials is not None
            else None
        ),
        trust_env=False,
        follow_redirects=False,
        headers=headers,
    )


def direct_mcp_http_client(
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
    auth: httpx.Auth | None = None,
    follow_redirects: bool = True,
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers=headers,
        timeout=timeout or httpx.Timeout(30.0, read=300.0),
        auth=auth,
        trust_env=False,
        follow_redirects=False,
    )


def direct_mcp_client(url: str, *, timeout: int = 900) -> Client:
    transport = StreamableHttpTransport(
        url,
        httpx_client_factory=direct_mcp_http_client,
    )
    return Client(transport, timeout=timeout)


def command_environment(
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    values = os.environ if source is None else source
    control = sorted(
        key for key, value in values.items() if value and key in CONTROL_ENV_KEYS
    )
    if control:
        raise LifecycleError(
            "lifecycle control environment is not allowed: "
            + ", ".join(control)
        )
    return {
        key: value
        for key, value in values.items()
        if key in HOST_ENV_ALLOWLIST
    }


def now() -> str:
    return datetime.now(UTC).isoformat()


def canonical_hash(value: Any) -> str:
    body = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def command(*args: str) -> str:
    completed = subprocess.run(
        args,
        cwd=ROOT,
        env=command_environment(),
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise LifecycleError(f"command failed ({' '.join(args)}): {detail}")
    return completed.stdout.strip()


def load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LifecycleError(f"candidate manifest is missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise LifecycleError(f"candidate manifest is invalid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise LifecycleError("candidate manifest must contain an object")
    return payload


def require_loopback_url(value: str, *, path: str, label: str) -> str:
    parsed = urlsplit(value)
    try:
        host = ipaddress.ip_address(str(parsed.hostname or ""))
        port = int(parsed.port or 0)
    except (ValueError, TypeError):
        host = None
        port = 0
    if (
        parsed.scheme != "http"
        or host is None
        or not host.is_loopback
        or port <= 0
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path.rstrip("/") != path
        or parsed.query
        or parsed.fragment
    ):
        raise LifecycleError(f"{label} must be a direct loopback {path or '/'} URL")
    return value.rstrip("/")


def result_payload(result: Any) -> Any:
    if result.structured_content is not None:
        return result.structured_content
    if result.data is not None:
        payload = getattr(result.data, "root", result.data)
        return (
            payload.model_dump(mode="json")
            if hasattr(payload, "model_dump")
            else payload
        )
    for content in result.content:
        raw = getattr(content, "text", None)
        if raw:
            return json.loads(raw)
    raise LifecycleError("candidate MCP returned no payload")


def project_id_from(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    project = payload.get("project")
    if isinstance(project, dict):
        project_id = str(project.get("id") or "")
        if project_id:
            return project_id
    return str(payload.get("project_id") or "")


def task_id_from(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    task = payload.get("task")
    if isinstance(task, dict):
        task_id = str(task.get("id") or "")
        if task_id:
            return task_id
    return str(payload.get("task_id") or "")


def atomic_write(path: Path, payload: dict[str, Any]) -> None:
    if path.exists():
        raise LifecycleError(f"lifecycle transcript already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


class Recorder:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.operations: list[dict[str, Any]] = []

    def append(
        self,
        *,
        tool: str,
        transport: str,
        arguments: dict[str, Any],
        project_id: str,
        stage_key: str = "",
        task_id: str = "",
        result_ok: bool = True,
        request_id: str,
    ) -> None:
        previous = (
            str(self.operations[-1]["operation_sha256"])
            if self.operations
            else "0" * 64
        )
        operation = {
            "index": len(self.operations) + 1,
            "recorded_at": now(),
            "run_id": self.run_id,
            "tool": tool,
            "transport": transport,
            "request_id": request_id,
            "arguments_sha256": canonical_hash(arguments),
            "project_id": project_id,
            "stage_key": stage_key,
            "task_id": task_id,
            "result_ok": result_ok,
            "previous_operation_sha256": previous,
        }
        operation["operation_sha256"] = canonical_hash(operation)
        self.operations.append(operation)


async def call_mcp(
    client: Client,
    recorder: Recorder,
    *,
    tool: str,
    arguments: dict[str, Any],
    project_id: str = "",
    stage_key: str = "",
) -> Any:
    request_id = str(uuid.uuid4())
    result = result_payload(await client.call_tool(tool, arguments))
    resolved_project_id = project_id or project_id_from(result)
    recorder.append(
        tool=tool,
        transport="mcp_http",
        arguments=arguments,
        project_id=resolved_project_id,
        stage_key=stage_key,
        task_id=task_id_from(result),
        result_ok=bool(result.get("ok", True)) if isinstance(result, dict) else True,
        request_id=request_id,
    )
    return result


def policy_update_payload(current: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    policy = current.get("policy")
    if not isinstance(policy, dict):
        raise LifecycleError("candidate API returned no runtime policy")
    chapter_length = policy.get("chapter_length") or {}
    pause = policy.get("pause") or {}
    if not isinstance(chapter_length, dict) or not isinstance(pause, dict):
        raise LifecycleError("candidate API returned malformed runtime policy")
    return {
        "expected_version": int(current.get("version") or 0),
        "quality_profile": args.quality_profile,
        "model_profile_id": str(policy.get("model_profile_id") or ""),
        "min_chapter_chars": int(chapter_length.get("min_chars") or 0),
        "target_chapter_chars": int(chapter_length.get("target_chars") or 0),
        "max_chapter_chars": int(chapter_length.get("max_chars") or 0),
        "review_interval_chapters": int(
            pause.get("review_interval_chapters") or 0
        ),
        "manual_checkpoints": bool(pause.get("manual_checkpoints", True)),
        "band_checkpoint_action": str(
            pause.get("band_checkpoint_action") or "pause_on_warn"
        ),
        "gate_delegate": args.gate_delegate,
        "reason": "v5-post-decision-smoke",
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    candidate_path = args.candidate_manifest.resolve()
    candidate = load_json(candidate_path)
    source = candidate.get("source") or {}
    source_sha = str(source.get("sha") or "")
    source_tree = str(source.get("tree") or "")
    if (
        not source_sha
        or command("git", "rev-parse", "HEAD") != source_sha
        or command("git", "rev-parse", "HEAD^{tree}") != source_tree
    ):
        raise LifecycleError("candidate source identity differs from the worktree")
    if command("git", "status", "--porcelain=v1", "--untracked-files=no"):
        raise LifecycleError("tracked worktree is dirty")
    mcp_url = require_loopback_url(args.mcp_url, path="/mcp", label="MCP URL")
    api_url = require_loopback_url(args.api_url, path="", label="API URL")
    premise = args.premise_file.resolve().read_text(encoding="utf-8").strip()
    if not premise:
        raise LifecycleError("smoke premise file is empty")
    setting_summary = (
        args.setting_summary_file.resolve().read_text(encoding="utf-8").strip()
        if args.setting_summary_file is not None
        else ""
    )
    run_id = uuid.uuid4().hex
    recorder = Recorder(run_id)
    started_at = now()
    async with direct_mcp_client(mcp_url) as mcp:
        create_arguments = {
            "title": args.title,
            "premise": premise,
            "genre": args.genre,
            "setting_summary": setting_summary,
            "target_total_chapters": TARGET,
        }
        created = await call_mcp(
            mcp,
            recorder,
            tool="project_create",
            arguments=create_arguments,
        )
        project_id = project_id_from(created)
        if not project_id:
            raise LifecycleError("project_create returned no project identity")

        request_id = str(uuid.uuid4())
        async with direct_api_client(
            base_url=api_url,
            timeout=60,
            headers={"X-Request-ID": request_id},
        ) as http:
            current = (
                await http.get(f"/api/projects/{project_id}/policy")
            )
            current.raise_for_status()
            update = policy_update_payload(current.json(), args)
            response = await http.put(
                f"/api/projects/{project_id}/policy",
                json=update,
            )
            response.raise_for_status()
        recorder.append(
            tool="project_policy_update",
            transport="http",
            arguments=update,
            project_id=project_id,
            result_ok=True,
            request_id=request_id,
        )

        for stage in GENESIS_STAGES:
            await call_mcp(
                mcp,
                recorder,
                tool="genesis_stage_generate",
                arguments={"project_id": project_id, "stage_key": stage},
                project_id=project_id,
                stage_key=stage,
            )
            await call_mcp(
                mcp,
                recorder,
                tool="genesis_stage_lock",
                arguments={"project_id": project_id, "stage_key": stage},
                project_id=project_id,
                stage_key=stage,
            )
        active = await call_mcp(
            mcp,
            recorder,
            tool="task_active_generation_check",
            arguments={"project_id": project_id},
            project_id=project_id,
        )
        if not isinstance(active, dict) or active.get(
            "has_active_generation_task"
        ) is not False:
            raise LifecycleError("an active generation task exists before handoff")
        started = await call_mcp(
            mcp,
            recorder,
            tool="project_start_writing",
            arguments={
                "project_id": project_id,
                "auto_continue": True,
                "run_until_chapter": TARGET,
            },
            project_id=project_id,
        )
        task_id = task_id_from(started)
        if not task_id:
            raise LifecycleError("project_start_writing returned no task identity")

    return {
        "schema_version": 1,
        "result": "handoff_started",
        "source_sha": source_sha,
        "source_tree": source_tree,
        "run_id": run_id,
        "started_at": started_at,
        "completed_at": now(),
        "project_id": project_id,
        "handoff_task_id": task_id,
        "target": TARGET,
        "candidate_manifest": {
            "path": str(candidate_path),
            "sha256": sha256_file(candidate_path),
        },
        "harness": {
            "path": str(Path(__file__).resolve()),
            "sha256": sha256_file(Path(__file__).resolve()),
        },
        "mcp_url": mcp_url,
        "api_url": api_url,
        "operation_count": len(recorder.operations),
        "operation_chain_head": recorder.operations[-1]["operation_sha256"],
        "operations": recorder.operations,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create the ForWin v5 fresh-30 project through HTTP and MCP."
    )
    parser.add_argument("--candidate-manifest", type=Path, required=True)
    parser.add_argument("--mcp-url", required=True)
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--premise-file", type=Path, required=True)
    parser.add_argument("--setting-summary-file", type=Path)
    parser.add_argument("--genre", default="fantasy")
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
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    transcript = asyncio.run(run(args))
    atomic_write(args.output.resolve(), transcript)
    print(args.output.resolve())
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except LifecycleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
