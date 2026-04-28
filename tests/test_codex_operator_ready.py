from __future__ import annotations

from scripts.check_codex_operator_ready import (
    CheckResult,
    codex_mcp_has_forwin,
    docker_ps_has_services,
    main,
)


def test_docker_ps_parser_requires_both_forwin_services_running() -> None:
    output = """
NAME          IMAGE          COMMAND   SERVICE      CREATED   STATUS
forwin        forwin-forwin   "..."     forwin       1m ago    Up 1m (healthy)
forwin-mcp    forwin-forwin   "..."     forwin-mcp   1m ago    Up 1m (healthy)
"""

    assert docker_ps_has_services(output, ("forwin", "forwin-mcp"))
    assert not docker_ps_has_services(output.replace("forwin-mcp", "missing"), ("forwin", "forwin-mcp"))


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
