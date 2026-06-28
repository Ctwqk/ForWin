#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str
    required: bool = True


def docker_ps_has_services(output: str, required_services: tuple[str, ...]) -> bool:
    lines = str(output or "").splitlines()
    for service in required_services:
        if not any(
            service in line.split()
            and ("Up" in line or "running" in line.lower())
            for line in lines
        ):
            return False
    return True


def _replicas_are_running(value: str) -> bool:
    parts = str(value or "").split("/", 1)
    if len(parts) != 2:
        return False
    try:
        running = int(parts[0])
        desired = int(parts[1])
    except ValueError:
        return False
    return desired > 0 and running >= desired


def swarm_service_ls_has_services(output: str, required_services: tuple[str, ...]) -> bool:
    lines = str(output or "").splitlines()
    service_replicas: dict[str, str] = {}
    for line in lines:
        parts = line.split()
        if len(parts) >= 2 and parts[0] in required_services:
            service_replicas[parts[0]] = parts[1]
        elif len(parts) >= 4 and parts[1] in required_services:
            service_replicas[parts[1]] = parts[3]
    return all(_replicas_are_running(service_replicas.get(service, "")) for service in required_services)


def docker_container_ps_has_services(output: str, required_services: tuple[str, ...]) -> bool:
    lines = str(output or "").splitlines()
    running_services: set[str] = set()
    for line in lines:
        stripped = line.strip()
        if "Up " not in stripped:
            continue
        parts = stripped.split(maxsplit=1)
        if not parts:
            continue
        container_name = parts[0]
        for service in required_services:
            if container_name == service or container_name.startswith(f"{service}."):
                running_services.add(service)
    return all(service in running_services for service in required_services)


def codex_mcp_has_forwin(output: str) -> bool:
    for line in str(output or "").splitlines():
        parts = line.split()
        if parts and parts[0] == "forwin":
            return True
    return False


def run_command(args: list[str], *, cwd: Path = REPO_ROOT, timeout: float = 15.0) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            args,
            cwd=str(cwd),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def check_docker_services() -> CheckResult:
    proc = run_command(["docker", "compose", "ps", "forwin", "forwin-mcp"])
    compose_detail = ""
    if proc is None:
        compose_detail = "docker compose command is unavailable or timed out"
    else:
        output = "\n".join([proc.stdout, proc.stderr]).strip()
        if proc.returncode == 0 and docker_ps_has_services(output, ("forwin", "forwin-mcp")):
            return CheckResult("docker compose services", True, "forwin and forwin-mcp appear to be running", required=False)
        compose_detail = output or f"exit code {proc.returncode}"

    contexts = []
    configured_context = os.environ.get("FORWIN_SWARM_DOCKER_CONTEXT", "").strip()
    if configured_context:
        contexts.append(configured_context)
    contexts.append("swarm-manager-150")
    for context in dict.fromkeys(contexts):
        swarm_proc = run_command(["docker", "--context", context, "service", "ls", "--filter", "name=forwin"], timeout=10)
        if swarm_proc is None:
            continue
        swarm_output = "\n".join([swarm_proc.stdout, swarm_proc.stderr]).strip()
        if swarm_proc.returncode == 0 and swarm_service_ls_has_services(swarm_output, ("forwin-app-swarm", "forwin-mcp-swarm")):
            return CheckResult(
                "docker swarm services",
                True,
                f"forwin-app-swarm and forwin-mcp-swarm are running via context {context}",
                required=False,
            )

    colima_profile = os.environ.get("FORWIN_COLIMA_PROFILE", "swarmbridged").strip()
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
            timeout=10,
        )
        if colima_proc is not None:
            colima_output = "\n".join([colima_proc.stdout, colima_proc.stderr]).strip()
            if colima_proc.returncode == 0 and docker_container_ps_has_services(
                colima_output,
                ("forwin-app-swarm", "forwin-mcp-swarm"),
            ):
                return CheckResult(
                    "docker colima containers",
                    True,
                    f"forwin-app-swarm and forwin-mcp-swarm are running via colima profile {colima_profile}",
                    required=False,
                )

    return CheckResult(
        "docker compose/swarm services",
        False,
        compose_detail or "forwin services were not found through compose or swarm",
        required=False,
    )


def check_json_health(name: str, url: str) -> CheckResult:
    try:
        with urlopen(url, timeout=5) as response:
            status = int(getattr(response, "status", 0) or 0)
            body = response.read(2048).decode("utf-8", errors="replace")
    except URLError as exc:
        return CheckResult(name, False, str(exc))
    except Exception as exc:  # noqa: BLE001
        return CheckResult(name, False, f"{exc.__class__.__name__}: {exc}")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        payload = {}
    if 200 <= status < 300 and isinstance(payload, dict) and payload.get("status") == "ok":
        return CheckResult(name, True, url)
    return CheckResult(name, False, f"unexpected status/body: {status} {body[:160]}")


def check_plugin_mcp_config(expected_mcp_url: str) -> CheckResult:
    config_path = REPO_ROOT / "plugins" / "forwin-operator" / ".mcp.json"
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return CheckResult("plugin MCP config", False, f"missing {config_path}")
    except json.JSONDecodeError as exc:
        return CheckResult("plugin MCP config", False, f"invalid JSON: {exc}")
    server = payload.get("mcpServers", {}).get("forwin", {}) if isinstance(payload, dict) else {}
    if not isinstance(server, dict):
        return CheckResult("plugin MCP config", False, "mcpServers.forwin is missing")
    if server.get("transport") != "streamable_http":
        return CheckResult("plugin MCP config", False, "forwin transport must be streamable_http")
    if server.get("url") != expected_mcp_url:
        return CheckResult("plugin MCP config", False, f"expected {expected_mcp_url}, got {server.get('url', '')}")
    return CheckResult("plugin MCP config", True, expected_mcp_url)


def check_codex_mcp_registration() -> CheckResult:
    proc = run_command(["codex", "mcp", "list"], timeout=10)
    if proc is None:
        return CheckResult("codex MCP registration", False, "codex command is unavailable or timed out", required=False)
    output = "\n".join([proc.stdout, proc.stderr]).strip()
    if proc.returncode != 0:
        return CheckResult("codex MCP registration", False, output or f"exit code {proc.returncode}", required=False)
    if not codex_mcp_has_forwin(output):
        return CheckResult("codex MCP registration", False, "codex mcp list does not contain a forwin entry", required=False)
    return CheckResult("codex MCP registration", True, "forwin is registered", required=False)


def _python_for_import_check() -> Path:
    python_bin = REPO_ROOT / ".venv" / "bin" / "python"
    return python_bin if python_bin.exists() else Path(sys.executable)


def check_python_imports() -> CheckResult:
    python_bin = _python_for_import_check()
    proc = run_command(
        [
            str(python_bin),
            "-c",
            "import fastmcp, pytest; print('fastmcp and pytest import ok')",
        ],
        timeout=15,
    )
    if proc is None:
        return CheckResult("python environment", False, "import check timed out", required=False)
    output = "\n".join([proc.stdout, proc.stderr]).strip()
    if proc.returncode != 0:
        return CheckResult("python environment", False, output or f"exit code {proc.returncode}", required=False)
    return CheckResult("python environment", True, f"{python_bin}: {output}", required=False)


def build_results(*, api_health_url: str, mcp_health_url: str, mcp_url: str) -> list[CheckResult]:
    return [
        check_json_health("forwin API health", api_health_url),
        check_json_health("forwin MCP health", mcp_health_url),
        check_plugin_mcp_config(mcp_url),
        check_docker_services(),
        check_codex_mcp_registration(),
        check_python_imports(),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only readiness check for Codex operating ForWin through MCP.")
    parser.add_argument("--api-health-url", default="http://127.0.0.1:8899/health")
    parser.add_argument("--mcp-health-url", default="http://127.0.0.1:8896/health")
    parser.add_argument("--mcp-url", default="http://127.0.0.1:8896/mcp")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Fail when optional diagnostics such as docker compose, global Codex MCP registration, or test imports fail.",
    )
    args = parser.parse_args(argv)

    results = build_results(api_health_url=args.api_health_url, mcp_health_url=args.mcp_health_url, mcp_url=args.mcp_url)
    for result in results:
        status = "OK" if result.ok else ("FAIL" if result.required else "WARN")
        print(f"[{status}] {result.name}: {result.detail}")
    if args.strict:
        return 0 if all(result.ok for result in results) else 1
    return 0 if all(result.ok for result in results if result.required) else 1


if __name__ == "__main__":
    sys.exit(main())
