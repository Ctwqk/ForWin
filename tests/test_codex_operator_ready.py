from __future__ import annotations

from scripts.check_codex_operator_ready import (
    CheckResult,
    check_docker_services,
    codex_mcp_has_forwin,
    docker_container_ps_has_services,
    docker_ps_has_services,
    main,
    swarm_service_ls_has_services,
)


def test_docker_ps_parser_requires_both_forwin_services_running() -> None:
    output = """
NAME          IMAGE          COMMAND   SERVICE      CREATED   STATUS
forwin        forwin-forwin   "..."     forwin       1m ago    Up 1m (healthy)
forwin-mcp    forwin-forwin   "..."     forwin-mcp   1m ago    Up 1m (healthy)
"""

    assert docker_ps_has_services(output, ("forwin", "forwin-mcp"))
    assert not docker_ps_has_services(output.replace("forwin-mcp", "missing"), ("forwin", "forwin-mcp"))


def test_swarm_service_parser_requires_both_forwin_services_replicated() -> None:
    compact_output = """
NAME                             REPLICAS   IMAGE
forwin-app-swarm                 1/1        forwin-forwin:deploy-abc123
forwin-mcp-swarm                 1/1        forwin-forwin:deploy-abc123
forwin-generation-worker-swarm   1/1        forwin-forwin:deploy-abc123
"""
    default_output = """
ID             NAME               MODE         REPLICAS   IMAGE
tzo1a4urr8gj   forwin-app-swarm   replicated   1/1        forwin-forwin:deploy-abc123
i6vubg2tfnv0   forwin-mcp-swarm   replicated   1/1        forwin-forwin:deploy-abc123
"""

    assert swarm_service_ls_has_services(compact_output, ("forwin-app-swarm", "forwin-mcp-swarm"))
    assert swarm_service_ls_has_services(default_output, ("forwin-app-swarm", "forwin-mcp-swarm"))
    assert not swarm_service_ls_has_services(
        compact_output.replace("forwin-mcp-swarm", "missing"),
        ("forwin-app-swarm", "forwin-mcp-swarm"),
    )
    assert not swarm_service_ls_has_services(
        compact_output.replace("1/1", "0/1", 1),
        ("forwin-app-swarm", "forwin-mcp-swarm"),
    )


def test_docker_container_parser_accepts_colima_swarm_task_names() -> None:
    output = """
forwin-app-swarm.1.abc forwin-forwin:deploy-abc Up 5 hours (healthy)
forwin-mcp-swarm.1.def forwin-forwin:deploy-abc Up 5 hours (healthy)
forwin-generation-worker-swarm.1.ghi forwin-forwin:deploy-abc Up 5 hours
"""

    assert docker_container_ps_has_services(output, ("forwin-app-swarm", "forwin-mcp-swarm"))
    assert not docker_container_ps_has_services(
        output.replace("forwin-mcp-swarm", "missing"),
        ("forwin-app-swarm", "forwin-mcp-swarm"),
    )
    assert not docker_container_ps_has_services(
        output.replace("Up 5 hours (healthy)", "Exited (1) 2 minutes ago", 1),
        ("forwin-app-swarm", "forwin-mcp-swarm"),
    )


def test_docker_check_accepts_swarm_when_compose_has_no_services(monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_run_command(args, **kwargs):
        calls.append(tuple(args))
        if args[:3] == ["docker", "compose", "ps"]:
            return type(
                "Proc",
                (),
                {"returncode": 0, "stdout": "NAME IMAGE COMMAND SERVICE CREATED STATUS PORTS\n", "stderr": ""},
            )()
        if args[:3] == ["docker", "--context", "swarm-manager-150"]:
            return type(
                "Proc",
                (),
                {
                    "returncode": 0,
                    "stdout": "NAME REPLICAS IMAGE\nforwin-app-swarm 1/1 image\nforwin-mcp-swarm 1/1 image\n",
                    "stderr": "",
                },
            )()
        raise AssertionError(args)

    monkeypatch.setattr("scripts.check_codex_operator_ready.run_command", fake_run_command)
    result = check_docker_services()

    assert result.ok
    assert "swarm" in result.detail
    assert ("docker", "--context", "swarm-manager-150", "service", "ls", "--filter", "name=forwin") in calls


def test_docker_check_accepts_colima_fallback_when_context_is_unavailable(monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_run_command(args, **kwargs):
        calls.append(tuple(args))
        if args[:3] == ["docker", "compose", "ps"]:
            return type("Proc", (), {"returncode": 1, "stdout": "", "stderr": "compose env missing"})()
        if args[:3] == ["docker", "--context", "swarm-manager-150"]:
            return type("Proc", (), {"returncode": 1, "stdout": "", "stderr": "context unavailable"})()
        if args[:4] == ["colima", "ssh", "-p", "swarmbridged"]:
            return type(
                "Proc",
                (),
                {
                    "returncode": 0,
                    "stdout": "\n".join(
                        [
                            "forwin-app-swarm.1.abc image Up 5 hours (healthy)",
                            "forwin-mcp-swarm.1.def image Up 5 hours (healthy)",
                        ]
                    ),
                    "stderr": "",
                },
            )()
        raise AssertionError(args)

    monkeypatch.delenv("FORWIN_COLIMA_PROFILE", raising=False)
    monkeypatch.setattr("scripts.check_codex_operator_ready.run_command", fake_run_command)
    result = check_docker_services()

    assert result.ok
    assert "colima profile swarmbridged" in result.detail
    assert ("colima", "ssh", "-p", "swarmbridged", "--", "docker", "ps", "--format", "{{.Names}} {{.Image}} {{.Status}}") in calls


def test_codex_mcp_parser_matches_forwin_name_column() -> None:
    output = """
Name        Url                         Status
playwright  npx                         enabled
forwin      http://127.0.0.1:8898/mcp   enabled
"""

    assert codex_mcp_has_forwin(output)
    assert not codex_mcp_has_forwin(output.replace("forwin", "not-forwin"))


def test_main_returns_failure_when_any_required_check_fails(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "scripts.check_codex_operator_ready.build_results",
        lambda **_: [
            CheckResult("one", True, "ok"),
            CheckResult("two", False, "missing"),
        ],
    )

    assert main([]) == 1
    output = capsys.readouterr().out
    assert "[OK] one: ok" in output
    assert "[FAIL] two: missing" in output


def test_main_does_not_fail_for_optional_diagnostics_by_default(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "scripts.check_codex_operator_ready.build_results",
        lambda **_: [
            CheckResult("forwin API health", True, "ok"),
            CheckResult("codex MCP registration", False, "not globally registered", required=False),
        ],
    )

    assert main([]) == 0
    output = capsys.readouterr().out
    assert "[OK] forwin API health: ok" in output
    assert "[WARN] codex MCP registration: not globally registered" in output


def test_main_strict_fails_for_optional_diagnostics(monkeypatch) -> None:
    monkeypatch.setattr(
        "scripts.check_codex_operator_ready.build_results",
        lambda **_: [
            CheckResult("forwin API health", True, "ok"),
            CheckResult("codex MCP registration", False, "not globally registered", required=False),
        ],
    )

    assert main(["--strict"]) == 1
