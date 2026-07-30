from __future__ import annotations

import ast
import copy
import importlib.util
import json
import multiprocessing
import os
import re
import shlex
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any
from urllib.parse import urlsplit

import pytest
import yaml


MODULE_PATH = Path(__file__).with_name("recovery_stack.py")
SPEC = importlib.util.spec_from_file_location("recovery_stack", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
stack = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(stack)

CANDIDATE_MCP_PATH = Path(__file__).with_name("candidate_mcp_call.py")
CANDIDATE_MCP_SPEC = importlib.util.spec_from_file_location(
    "candidate_mcp_recovery_probe",
    CANDIDATE_MCP_PATH,
)
assert (
    CANDIDATE_MCP_SPEC is not None
    and CANDIDATE_MCP_SPEC.loader is not None
)
candidate_mcp = importlib.util.module_from_spec(CANDIDATE_MCP_SPEC)
CANDIDATE_MCP_SPEC.loader.exec_module(candidate_mcp)

FINALIZER_PATH = Path(__file__).with_name("finalize_recovery.py")
FINALIZER_SPEC = importlib.util.spec_from_file_location(
    "finalize_recovery_contract",
    FINALIZER_PATH,
)
assert FINALIZER_SPEC is not None and FINALIZER_SPEC.loader is not None
finalizer = importlib.util.module_from_spec(FINALIZER_SPEC)
FINALIZER_SPEC.loader.exec_module(finalizer)

RECOVERY_RUNBOOK_PATH = Path(__file__).with_name(
    "recovery-evidence-runbook.md"
)
RECOVERY_RUNNER_PATHS = (
    Path(__file__).with_name("generation_projection_recovery.py"),
    Path(__file__).with_name("minio_recovery.py"),
    Path(__file__).with_name("publisher_recovery.py"),
)
SOURCE_SHA = "f" * 40
TEST_CONTAINER_SUFFIXES = {
    "postgres": "postgres",
    "qdrant": "qdrant",
    "minio": "minio",
    "forwin": "api",
    "generation-worker": "generation-worker",
    "outbox-worker": "outbox-worker",
    "forwin-mcp": "mcp",
    "publisher-worker": "publisher-worker",
    "publisher-browser": "publisher-browser",
}


def frozen_identity() -> dict[str, Any]:
    return {
        "source_sha": SOURCE_SHA,
        "harness": {
            "candidate_mcp_call": {
                "path": str(CANDIDATE_MCP_PATH.resolve()),
                "sha256": stack.sha256_file(CANDIDATE_MCP_PATH.resolve()),
            }
        },
    }


def module_assignment_literal(tree: ast.Module, name: str):
    assignments = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == name
            for target in node.targets
        )
    ]
    assert len(assignments) == 1, f"expected one {name} assignment"
    return ast.literal_eval(assignments[0].value)


def recovery_runner_contracts(
) -> dict[str, tuple[tuple[str, ...], dict[str, int]]]:
    contracts = {}
    root = Path(__file__).parents[2]
    for runner_path in RECOVERY_RUNNER_PATHS:
        tree = ast.parse(
            runner_path.read_text(encoding="utf-8"),
            filename=str(runner_path),
        )
        parse_args_functions = [
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "parse_args"
        ]
        assert len(parse_args_functions) == 1
        parse_args_function = parse_args_functions[0]
        run_parser_assignments = [
            node
            for node in ast.walk(parse_args_function)
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and node.value.func.attr == "add_parser"
            and node.value.args
            and isinstance(node.value.args[0], ast.Constant)
            and node.value.args[0].value == "run"
        ]
        assert len(run_parser_assignments) == 1
        run_parser_assignment = run_parser_assignments[0]
        assert len(run_parser_assignment.targets) == 1
        run_parser_target = run_parser_assignment.targets[0]
        assert isinstance(run_parser_target, ast.Name)

        option_arity = {}
        fault_choices_name = None
        for node in ast.walk(parse_args_function):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == run_parser_target.id
            ):
                continue
            option_names = tuple(
                argument.value
                for argument in node.args
                if isinstance(argument, ast.Constant)
                and isinstance(argument.value, str)
            )
            assert len(option_names) == len(node.args) == 1
            option = option_names[0]
            assert option.startswith("--")
            keywords = {
                keyword.arg: keyword.value
                for keyword in node.keywords
                if keyword.arg is not None
            }
            assert ast.literal_eval(keywords["required"]) is True
            if option == "--fault-kind":
                choices = keywords.get("choices")
                assert isinstance(choices, ast.Name)
                fault_choices_name = choices.id
            assert "action" not in keywords
            arity = (
                ast.literal_eval(keywords["nargs"])
                if "nargs" in keywords
                else 1
            )
            assert arity == 1, f"{option} must accept exactly one value"
            assert option not in option_arity
            option_arity[option] = arity

        assert fault_choices_name is not None
        supported_faults = module_assignment_literal(
            tree,
            fault_choices_name,
        )
        assert isinstance(supported_faults, tuple)
        assert all(isinstance(fault, str) for fault in supported_faults)
        runner = runner_path.relative_to(root).as_posix()
        contracts[runner] = (supported_faults, option_arity)

    assert len(contracts) == 3
    return contracts


def test_active_generation_probe_payload_requires_consistent_public_state(
) -> None:
    payload = {
        "has_active_generation_task": False,
        "active_task_ids": [],
        "active_count": 0,
        "safe_to_restart": True,
    }

    assert candidate_mcp.validate_active_generation_check(payload) == payload


@pytest.mark.parametrize(
    "payload",
    (
        {},
        {
            "has_active_generation_task": "false",
            "active_task_ids": [],
            "active_count": 0,
            "safe_to_restart": True,
        },
        {
            "has_active_generation_task": False,
            "active_task_ids": ["task-1"],
            "active_count": 0,
            "safe_to_restart": True,
        },
        {
            "has_active_generation_task": True,
            "active_task_ids": ["task-1"],
            "active_count": 1,
            "safe_to_restart": True,
        },
    ),
)
def test_active_generation_probe_payload_rejects_malformed_or_drifting_state(
    payload: dict,
) -> None:
    with pytest.raises(RuntimeError, match="active generation check"):
        candidate_mcp.validate_active_generation_check(payload)


def test_mcp_functional_probe_calls_read_only_tool_not_health_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setenv("PYTHONPATH", "/tmp/untrusted-shadow")
    monkeypatch.setenv("FORWIN_HTTP_BASIC_PASSWORD", "must-not-reach-helper")

    def fake_command(
        *args: str,
        **kwargs: object,
    ) -> str:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return "validated"

    monkeypatch.setattr(
        stack,
        "published_endpoint_identity",
        lambda service, port, *, run_identity: {
            "service": service,
            "host": "127.0.0.1",
            "host_port": 24112,
            "container_port": port,
        },
    )
    monkeypatch.setattr(stack, "command", fake_command)
    monkeypatch.setattr(
        stack,
        "compose_process",
        lambda *_args, **_kwargs: pytest.fail(
            "MCP probe must use the verified published endpoint"
        ),
    )

    result = stack.functional_probe(
        "forwin-mcp",
        run_identity={"run_id": "a" * 32},
        expected_helper_sha256=stack.sha256_file(
            CANDIDATE_MCP_PATH.resolve()
        ),
    )

    command = tuple(captured["args"])
    assert command[:3] == (
        sys.executable,
        "-I",
        str(CANDIDATE_MCP_PATH.resolve()),
    )
    assert "task_active_generation_check" in command
    assert "--expect-active-generation-check" in command
    assert "--url" in command
    assert "http://127.0.0.1:24112/mcp" in command
    assert "/health" not in " ".join(command)
    environment = captured["kwargs"]["env"]
    assert isinstance(environment, dict)
    assert set(environment) <= stack.HOST_ENV_ALLOWLIST
    assert "PYTHONPATH" not in environment
    assert "FORWIN_HTTP_BASIC_PASSWORD" not in environment
    assert result["passed"] is True
    assert result["helper_sha256"] == stack.sha256_file(
        CANDIDATE_MCP_PATH.resolve()
    )


def test_mcp_functional_probe_rejects_helper_changed_during_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = tmp_path / "candidate_mcp_call.py"
    helper.write_text("print('original')\n", encoding="utf-8")
    monkeypatch.setattr(stack, "CANDIDATE_MCP_CALL", helper)
    monkeypatch.setattr(
        stack,
        "published_endpoint_identity",
        lambda service, port, *, run_identity: {
            "service": service,
            "host": "127.0.0.1",
            "host_port": 24112,
            "container_port": port,
        },
    )

    def mutate_helper(*_args: str, **_kwargs: object) -> str:
        helper.write_text("print('changed')\n", encoding="utf-8")
        return "validated"

    monkeypatch.setattr(stack, "command", mutate_helper)

    with pytest.raises(stack.StackError, match="helper changed"):
        stack.functional_probe(
            "forwin-mcp",
            run_identity={"run_id": "a" * 32},
            expected_helper_sha256=stack.sha256_file(helper),
        )


def test_mcp_functional_probe_rejects_helper_changed_after_freeze(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = tmp_path / "candidate_mcp_call.py"
    helper.write_text("print('frozen')\n", encoding="utf-8")
    expected = stack.sha256_file(helper)
    helper.write_text("print('drifted')\n", encoding="utf-8")
    monkeypatch.setattr(stack, "CANDIDATE_MCP_CALL", helper)
    monkeypatch.setattr(
        stack,
        "command",
        lambda *_args, **_kwargs: pytest.fail(
            "drifted helper must not execute"
        ),
    )

    with pytest.raises(stack.StackError, match="frozen identity"):
        stack.functional_probe(
            "forwin-mcp",
            run_identity={"run_id": "a" * 32},
            expected_helper_sha256=expected,
        )


def test_v1_mcp_endpoint_identity_uses_base_compose_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container_id = "v1-mcp-container"
    payload = [
        {
            "Id": container_id,
            "Image": "sha256:" + "a" * 64,
            "Config": {
                "Labels": {
                    "com.docker.compose.project": stack.PROJECT,
                    "com.docker.compose.service": "forwin-mcp",
                }
            },
            "State": {"Running": True},
            "NetworkSettings": {
                "Ports": {
                    "8896/tcp": [
                        {
                            "HostIp": "127.0.0.1",
                            "HostPort": "24112",
                        }
                    ]
                }
            },
        }
    ]
    monkeypatch.setattr(
        stack,
        "compose_container_id",
        lambda *_args, **_kwargs: container_id,
    )
    monkeypatch.setattr(
        stack,
        "command",
        lambda *_args, **_kwargs: json.dumps(payload),
    )

    identity = stack.published_endpoint_identity(
        "forwin-mcp",
        8896,
        run_identity=None,
    )

    assert identity["container_id"] == container_id
    assert identity["host_port"] == 24112


def controller_recovery_endpoints() -> tuple[str, str]:
    api_url = (
        f"http://{stack.COMPOSE_ENV['FORWIN_HTTP_BIND']}:"
        f"{stack.COMPOSE_ENV['FORWIN_HTTP_PORT']}"
    )
    mcp_url = (
        f"http://{stack.COMPOSE_ENV['FORWIN_MCP_DEBUG_BIND']}/mcp"
    )
    return api_url, mcp_url


def controller_recovery_database_url() -> str:
    service_url = urlsplit(stack.ISOLATED_DATABASE_URL)
    database_host, database_port = stack.COMPOSE_ENV[
        "FORWIN_RECOVERY_POSTGRES_BIND"
    ].rsplit(":", 1)
    scheme = service_url.scheme.split("+", 1)[0]
    literal_parts = (
        scheme,
        service_url.username,
        service_url.password,
        database_host,
        database_port,
        service_url.path.removeprefix("/"),
    )
    assert all(
        isinstance(part, str)
        and re.fullmatch(r"[A-Za-z0-9._~-]+", part)
        for part in literal_parts
    ), "controller database identity must be statically shell-safe"
    return (
        f"{scheme}://{service_url.username}:{service_url.password}"
        f"@{database_host}:{database_port}{service_url.path}"
    )


def live_recovery_bash_blocks(runbook: str) -> tuple[str, str]:
    sections = re.findall(
        r"^## Live Recovery Faults[ \t]*\n(.*?)(?=^## |\Z)",
        runbook,
        re.DOTALL | re.MULTILINE,
    )
    assert len(sections) == 1, "expected one Live Recovery Faults section"
    bash_blocks = re.findall(
        r"^```bash[ \t]*\n(.*?)^```[ \t]*$",
        sections[0],
        re.DOTALL | re.MULTILINE,
    )
    assert len(bash_blocks) == 2, (
        "live recovery section must contain setup and command bash blocks"
    )
    return bash_blocks[0], bash_blocks[1]


def recovery_finalizer_bash_block(runbook: str) -> str:
    sections = re.findall(
        r"^## Finalize[ \t]*\n(.*?)(?=^## |\Z)",
        runbook,
        re.DOTALL | re.MULTILINE,
    )
    assert len(sections) == 1, "expected one recovery Finalize section"
    bash_blocks = re.findall(
        r"^```bash[ \t]*\n(.*?)^```[ \t]*$",
        sections[0],
        re.DOTALL | re.MULTILINE,
    )
    assert len(bash_blocks) == 1, (
        "recovery Finalize section must contain one finalizer Bash block"
    )
    return bash_blocks[0]


def parse_live_recovery_setup(setup_block: str) -> dict[str, str]:
    assert "\r" not in setup_block, (
        "carriage return is forbidden in recovery setup block"
    )
    assert "\0" not in setup_block, (
        "NUL is forbidden in recovery setup block"
    )
    assert setup_block.endswith("\n"), (
        "recovery setup block must end with a newline"
    )

    qdrant_bind = stack.COMPOSE_ENV["FORWIN_QDRANT_DEBUG_BIND"]
    minio_bind = stack.COMPOSE_ENV["FORWIN_RECOVERY_MINIO_API_BIND"]
    canonical_sequence = (
        (
            "FORWIN_RECOVERY_CANDIDATE_MANIFEST",
            (
                'export FORWIN_RECOVERY_CANDIDATE_MANIFEST="$(',
                "  realpath .artifacts/v5-rc/candidate-draft.json",
                ')"',
            ),
            "$(realpath .artifacts/v5-rc/candidate-draft.json)",
        ),
        (
            "FORWIN_RECOVERY_DATABASE_URL",
            (
                'export FORWIN_RECOVERY_DATABASE_URL="'
                f'{controller_recovery_database_url()}"',
            ),
            controller_recovery_database_url(),
        ),
        (
            "FORWIN_RECOVERY_QDRANT_URL",
            (
                'export FORWIN_RECOVERY_QDRANT_URL="'
                f'http://{qdrant_bind}"',
            ),
            f"http://{qdrant_bind}",
        ),
        (
            "FORWIN_RECOVERY_MINIO_ENDPOINT",
            (
                'export FORWIN_RECOVERY_MINIO_ENDPOINT='
                f'"{minio_bind}"',
            ),
            minio_bind,
        ),
        (
            "FORWIN_RECOVERY_MINIO_ACCESS_KEY",
            (
                'export FORWIN_RECOVERY_MINIO_ACCESS_KEY="'
                f'{stack.ISOLATED_MINIO_ACCESS_KEY}"',
            ),
            stack.ISOLATED_MINIO_ACCESS_KEY,
        ),
        (
            "FORWIN_RECOVERY_MINIO_SECRET_KEY",
            (
                'export FORWIN_RECOVERY_MINIO_SECRET_KEY="'
                f'{stack.ISOLATED_MINIO_SECRET_KEY}"',
            ),
            stack.ISOLATED_MINIO_SECRET_KEY,
        ),
        (
            "FORWIN_RECOVERY_MINIO_BUCKET",
            (
                'export FORWIN_RECOVERY_MINIO_BUCKET="'
                f'{stack.COMPOSE_ENV["FORWIN_MINIO_BUCKET"]}"',
            ),
            stack.COMPOSE_ENV["FORWIN_MINIO_BUCKET"],
        ),
        (
            "FORWIN_RECOVERY_MINIO_PREFIX",
            (
                'export FORWIN_RECOVERY_MINIO_PREFIX="'
                f'{stack.COMPOSE_ENV["FORWIN_MINIO_PREFIX"]}"',
            ),
            stack.COMPOSE_ENV["FORWIN_MINIO_PREFIX"],
        ),
        (
            "FORWIN_RECOVERY_MINIO_SECURE",
            (
                'export FORWIN_RECOVERY_MINIO_SECURE="'
                f'{stack.COMPOSE_ENV["FORWIN_MINIO_SECURE"]}"',
            ),
            stack.COMPOSE_ENV["FORWIN_MINIO_SECURE"],
        ),
        (
            "RECOVERY_RUN_ID",
            ('export RECOVERY_RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"',),
            "$(date -u +%Y%m%dT%H%M%SZ)-$$",
        ),
        (
            "RECOVERY_EVIDENCE_ROOT",
            (
                'export RECOVERY_EVIDENCE_ROOT="$(',
                "  pwd -P",
                ")/.artifacts/v5-recovery-live/$RECOVERY_RUN_ID\"",
            ),
            (
                "$(pwd -P)/.artifacts/v5-recovery-live/"
                "$RECOVERY_RUN_ID"
            ),
        ),
        (
            None,
            ('test ! -e "$RECOVERY_EVIDENCE_ROOT"',),
            None,
        ),
    )

    lines = setup_block[:-1].split("\n")
    exports = {}
    index = 0
    for name, physical_lines, value in canonical_sequence:
        width = len(physical_lines)
        assert tuple(lines[index : index + width]) == physical_lines, (
            "recovery setup export/test paragraphs must match the "
            "canonical sequence"
        )
        if name is not None:
            exports[name] = value
        index += width
    assert index == len(lines), (
        "recovery setup export/test sequence must contain no extra command"
    )
    return exports


def normalize_restricted_shell_command(
    paragraph: str,
    command_kind: str,
) -> str:
    assert "\r" not in paragraph, (
        f"carriage return is forbidden in {command_kind} command block"
    )
    assert "\0" not in paragraph, (
        f"NUL is forbidden in {command_kind} command block"
    )
    assert "'" not in paragraph, (
        f"single quote is forbidden in {command_kind} command block"
    )
    lines = paragraph.split("\n")
    assert lines and all(lines), (
        f"empty physical {command_kind} command line"
    )
    assert all(
        line.endswith("\\") and not line.endswith("\\\\")
        for line in lines[:-1]
    ), f"malformed {command_kind} command continuation"
    assert not lines[-1].endswith("\\"), (
        f"malformed terminal {command_kind} command continuation"
    )
    normalized = paragraph.replace("\\\n", "")
    assert "\n" not in normalized, (
        f"noncanonical {command_kind} command continuation"
    )
    assert "'" not in normalized, (
        f"single quote is forbidden in normalized {command_kind} command"
    )
    return normalized


def assert_canonical_option_source(
    normalized: str,
    option: str,
    source_value: str,
) -> None:
    pattern = (
        rf"(?:^|[ \t]){re.escape(option)}[ \t]+"
        rf"{re.escape(source_value)}(?=$|[ \t])"
    )
    assert re.search(pattern, normalized) is not None, (
        f"{option} must use its canonical shell value"
    )


def parse_recovery_finalizer_command(
    runbook: str,
    runner_commands: list[tuple[str, dict[str, str]]],
) -> None:
    finalizer_block = recovery_finalizer_bash_block(runbook)
    assert "\r" not in finalizer_block, (
        "carriage return is forbidden in recovery finalizer block"
    )
    assert "\0" not in finalizer_block, (
        "NUL is forbidden in recovery finalizer block"
    )
    assert "'" not in finalizer_block, (
        "single quote is forbidden in recovery finalizer block"
    )
    assert finalizer_block.endswith("\n"), (
        "recovery finalizer block must end with a newline"
    )
    paragraphs = re.split(
        r"\n[ \t]*\n",
        finalizer_block[:-1],
    )
    assert len(paragraphs) == 2, (
        "recovery finalizer block must contain exact setup and command "
        "paragraphs"
    )
    setup_paragraph, command_paragraph = paragraphs
    canonical_setup_lines = (
        'export RECOVERY_FINAL_DIR="$(',
        "  pwd -P",
        ')/.artifacts/v5-recovery-final/$RECOVERY_RUN_ID"',
        'test ! -e "$RECOVERY_FINAL_DIR"',
    )
    assert tuple(setup_paragraph.split("\n")) == canonical_setup_lines, (
        "recovery finalizer setup must create and test one new final directory"
    )

    normalized = normalize_restricted_shell_command(
        command_paragraph,
        "finalizer",
    )
    assert re.search(r"\$\(|[`;&|<>]", normalized) is None, (
        "shell metacharacter is forbidden in finalizer command block"
    )
    try:
        tokens = shlex.split(normalized)
    except ValueError as exc:
        raise AssertionError("malformed finalizer shell invocation") from exc

    root = Path(__file__).parents[2]
    finalizer_path = FINALIZER_PATH.relative_to(root).as_posix()
    assert len(tokens) >= 4 and tokens[:4] == [
        "uv",
        "run",
        "python",
        finalizer_path,
    ], "finalizer command must use the one canonical finalizer path"
    assert len(runner_commands) == 11, (
        "finalizer contract requires exactly eleven runner commands"
    )
    expected_options = (
        "--candidate-manifest",
        *(("--fault-report",) * len(runner_commands)),
        "--output",
    )
    option_tokens = tokens[4:]
    assert len(option_tokens) == len(expected_options) * 2, (
        "finalizer command must contain one value at every exact option "
        "position"
    )
    observed_options = tuple(option_tokens[::2])
    option_values = tuple(option_tokens[1::2])
    assert observed_options == expected_options, (
        "finalizer options must be one candidate manifest, eleven fault "
        "reports, and one output in canonical order"
    )
    assert not any(value.startswith("--") for value in option_values), (
        "finalizer option value is missing"
    )

    candidate_manifest = option_values[0]
    fault_reports = option_values[1:-1]
    output = option_values[-1]
    assert candidate_manifest == "$FORWIN_RECOVERY_CANDIDATE_MANIFEST", (
        "finalizer candidate manifest must use the canonical setup variable"
    )
    assert output == "$RECOVERY_FINAL_DIR/manifest.json", (
        "finalizer output must lie in the new recovery final directory"
    )

    finalizer_children = []
    for report in fault_reports:
        match = re.fullmatch(
            r"\$RECOVERY_EVIDENCE_ROOT/"
            r"(?P<child>[0-9]{2}-[a-z0-9]+(?:-[a-z0-9]+)*)/"
            r"fault-report\.json",
            report,
        )
        assert match is not None, (
            "finalizer fault report must be a canonical runner child report"
        )
        finalizer_children.append(match.group("child"))

    runner_children = []
    for _, options in runner_commands:
        evidence_dir = options["--evidence-dir"]
        prefix = "$RECOVERY_EVIDENCE_ROOT/"
        assert evidence_dir.startswith(prefix)
        runner_children.append(evidence_dir.removeprefix(prefix))

    assert len(set(finalizer_children)) == len(runner_commands), (
        "finalizer fault reports must cover every runner child exactly once"
    )
    assert finalizer_children == runner_children, (
        "finalizer fault reports must match runner evidence children in order"
    )

    canonical_command_lines = [
        f"uv run python {finalizer_path} \\",
        (
            '  --candidate-manifest '
            '"$FORWIN_RECOVERY_CANDIDATE_MANIFEST" \\'
        ),
        *(
            f'  --fault-report "{report}" \\'
            for report in fault_reports
        ),
        '  --output "$RECOVERY_FINAL_DIR/manifest.json"',
    ]
    assert command_paragraph.split("\n") == canonical_command_lines, (
        "finalizer command lines must use the exact restricted shell grammar"
    )


def parse_live_recovery_commands(
    runbook: str,
) -> list[tuple[str, dict[str, str]]]:
    contracts = recovery_runner_contracts()
    setup_block, command_block = live_recovery_bash_blocks(runbook)
    setup_exports = parse_live_recovery_setup(setup_block)
    assert "\r" not in command_block, (
        "carriage return is forbidden in runner command block"
    )
    assert "\0" not in command_block, (
        "NUL is forbidden in runner command block"
    )
    assert command_block.endswith("\n"), (
        "runner command block must end with a newline"
    )
    api_url, mcp_url = controller_recovery_endpoints()
    database_url_env = "FORWIN_RECOVERY_DATABASE_URL"
    assert setup_exports[database_url_env] == (
        controller_recovery_database_url()
    )

    commands = []
    command_body = command_block[:-1]
    for paragraph in re.split(r"\n[ \t]*\n", command_body):
        lines = paragraph.split("\n")
        if all(
            line.lstrip().startswith("#") for line in lines
        ):
            continue
        normalized = normalize_restricted_shell_command(
            paragraph,
            "runner",
        )
        assert re.search(r"\$\(|[`;&|<>]", normalized) is None, (
            "shell metacharacter is forbidden in runner command block"
        )
        try:
            tokens = shlex.split(normalized)
        except ValueError as exc:
            raise AssertionError("malformed shell invocation") from exc
        assert tokens, "empty command paragraph"
        assert len(tokens) >= 5 and tokens[:3] == [
            "uv",
            "run",
            "python",
        ], "runner command shape must start with uv run python"
        runner = tokens[3]
        assert runner in contracts, (
            "runner path must be one of the three canonical runner paths"
        )
        assert tokens[4] == "run", "runner must use the run subcommand"
        assert lines[0] == f"uv run python {runner} run \\", (
            "runner command first line must use its exact canonical path"
        )
        supported_faults, option_arity = contracts[runner]
        options = {}
        index = 5
        while index < len(tokens):
            option = tokens[index]
            assert option.startswith("--"), (
                f"unexpected positional token: {option}"
            )
            assert option in option_arity, f"unknown option: {option}"
            assert option not in options, f"duplicate option: {option}"
            arity = option_arity[option]
            values = tokens[index + 1 : index + 1 + arity]
            assert len(values) == arity and not any(
                value.startswith("--") for value in values
            ), f"missing value for option: {option}"
            options[option] = values[0]
            index += arity + 1

        missing_options = set(option_arity) - set(options)
        assert not missing_options, (
            "missing required options: " + ", ".join(sorted(missing_options))
        )

        fault_kind = options["--fault-kind"]
        assert fault_kind in supported_faults, (
            "--fault-kind must be supported by its AST-derived runner"
        )
        fault_id = options["--fault-id"]
        fault_id_match = re.fullmatch(
            r"\$\{RECOVERY_RUN_ID\}-"
            r"(?P<child>(?P<ordinal>[0-9]{2})-"
            r"(?P<slug>[a-z0-9]+(?:-[a-z0-9]+)*))",
            fault_id,
        )
        assert fault_id_match is not None, (
            "--fault-id must use the canonical recovery run ID grammar"
        )
        child = fault_id_match.group("child")
        assert fault_id_match.group("ordinal") == (
            f"{len(commands) + 1:02d}"
        ), "--fault-id ordinal must match command order"

        evidence_dir = options["--evidence-dir"]
        assert evidence_dir == f"$RECOVERY_EVIDENCE_ROOT/{child}", (
            "fault/evidence suffix must match one-to-one"
        )
        assert options["--candidate-manifest"] == (
            "$FORWIN_RECOVERY_CANDIDATE_MANIFEST"
        ), "--candidate-manifest must use the canonical setup variable"
        assert options["--mcp-url"] == mcp_url, (
            "--mcp-url must match the controller-derived endpoint"
        )
        assert options["--api-url"] == api_url, (
            "--api-url must match the controller-derived endpoint"
        )
        assert options["--database-url-env"] == database_url_env, (
            "--database-url-env must name the canonical setup export"
        )

        canonical_sources = {
            "--fault-kind": fault_kind,
            "--fault-id": f'"{fault_id}"',
            "--candidate-manifest": (
                '"$FORWIN_RECOVERY_CANDIDATE_MANIFEST"'
            ),
            "--mcp-url": mcp_url,
            "--api-url": api_url,
            "--database-url-env": database_url_env,
            "--evidence-dir": f'"{evidence_dir}"',
        }
        for option, source_value in canonical_sources.items():
            assert_canonical_option_source(
                normalized,
                option,
                source_value,
            )
        expected_option_order = tuple(option_arity)
        assert tuple(options) == expected_option_order, (
            "runner options must use their AST-derived order"
        )
        canonical_command_lines = [
            f"uv run python {runner} run \\",
            *(
                (
                    f"  {option} {canonical_sources[option]}"
                    + (" \\" if position < len(expected_option_order) - 1 else "")
                )
                for position, option in enumerate(expected_option_order)
            ),
        ]
        assert lines == canonical_command_lines, (
            "runner option lines must use the exact restricted shell grammar"
        )
        commands.append((runner, options))

    assert len(commands) == 11, (
        "designated live recovery block must contain exactly 11 commands"
    )
    expected_runner_faults = {
        (runner, fault_kind)
        for runner, (fault_kinds, _) in contracts.items()
        for fault_kind in fault_kinds
    }
    observed_runner_faults = {
        (runner, options["--fault-kind"])
        for runner, options in commands
    }
    assert observed_runner_faults == expected_runner_faults, (
        "runner commands must cover every AST-derived fault exactly once"
    )
    parse_recovery_finalizer_command(runbook, commands)
    return commands


def mutate_live_recovery_runbook(runbook: str, mutation: str) -> str:
    api_url, mcp_url = controller_recovery_endpoints()
    parsed_api_url = urlsplit(api_url)
    parsed_mcp_url = urlsplit(mcp_url)
    assert parsed_api_url.hostname is not None
    assert parsed_mcp_url.port is not None
    expanded_api_url = (
        f"{parsed_api_url.scheme}://{parsed_api_url.hostname}:"
        f"${{FORWIN_HTTP_PORT}}{parsed_api_url.path}"
    )
    wrong_mcp_url = mcp_url.replace(
        f":{parsed_mcp_url.port}",
        f":{parsed_mcp_url.port - 1}",
    )
    api_option = f"  --api-url {api_url} \\\n"
    mcp_option = f"  --mcp-url {mcp_url} \\\n"
    database_export = (
        'export FORWIN_RECOVERY_DATABASE_URL="'
        f'{controller_recovery_database_url()}"\n'
    )
    candidate_manifest_export = (
        'export FORWIN_RECOVERY_CANDIDATE_MANIFEST="$(\n'
        "  realpath .artifacts/v5-rc/candidate-draft.json\n"
        ')"\n'
    )
    qdrant_url_export = (
        'export FORWIN_RECOVERY_QDRANT_URL="'
        f'http://{stack.COMPOSE_ENV["FORWIN_QDRANT_DEBUG_BIND"]}"\n'
    )
    minio_endpoint_export = (
        'export FORWIN_RECOVERY_MINIO_ENDPOINT="'
        f'{stack.COMPOSE_ENV["FORWIN_RECOVERY_MINIO_API_BIND"]}"\n'
    )
    minio_secure_export = (
        'export FORWIN_RECOVERY_MINIO_SECURE="'
        f'{stack.COMPOSE_ENV["FORWIN_MINIO_SECURE"]}"\n'
    )
    recovery_run_id_export = (
        'export RECOVERY_RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"\n'
    )
    recovery_evidence_root_export = (
        'export RECOVERY_EVIDENCE_ROOT="$(\n'
        "  pwd -P\n"
        ')/.artifacts/v5-recovery-live/$RECOVERY_RUN_ID"\n'
    )
    setup_test_end = 'test ! -e "$RECOVERY_EVIDENCE_ROOT"\n```'
    first_fault_id = (
        '  --fault-id "${RECOVERY_RUN_ID}-01-generation-precommit" \\\n'
    )
    candidate_manifest_option = (
        '  --candidate-manifest '
        '"$FORWIN_RECOVERY_CANDIDATE_MANIFEST" \\\n'
    )
    first_evidence_dir = (
        '  --evidence-dir "$RECOVERY_EVIDENCE_ROOT/'
        '01-generation-precommit"'
    )
    first_runner_line = (
        "uv run python .artifacts/rc-candidate/"
        "generation_projection_recovery.py run \\\n"
    )
    database_url_env_option = (
        "  --database-url-env FORWIN_RECOVERY_DATABASE_URL \\\n"
    )
    first_runner_tail = (
        first_fault_id
        + candidate_manifest_option
        + mcp_option
        + api_option
        + database_url_env_option
        + first_evidence_dir
    )
    command_block_end = (
        '  --evidence-dir "$RECOVERY_EVIDENCE_ROOT/'
        '11-publisher-account-risk"\n```'
    )
    root = Path(__file__).parents[2]
    finalizer_path = FINALIZER_PATH.relative_to(root).as_posix()
    finalizer_command_line = f"uv run python {finalizer_path} \\\n"
    first_finalizer_report = (
        '  --fault-report "$RECOVERY_EVIDENCE_ROOT/'
        '01-generation-precommit/fault-report.json" \\\n'
    )
    second_finalizer_report = (
        '  --fault-report "$RECOVERY_EVIDENCE_ROOT/'
        '02-generation-postcommit/fault-report.json" \\\n'
    )
    finalizer_output = '  --output "$RECOVERY_FINAL_DIR/manifest.json"'
    finalizer_block_end = finalizer_output + "\n```"
    finalizer_test = 'test ! -e "$RECOVERY_FINAL_DIR"\n'

    def append_command(paragraph: str) -> str:
        return command_block_end.replace(
            "\n```",
            f"\n\n{paragraph}\n```",
        )

    replacements = {
        "wrong_subcommand": (
            "generation_projection_recovery.py run \\",
            "generation_projection_recovery.py recover \\",
        ),
        "unsupported_runner_path": (
            "generation_projection_recovery.py run \\",
            "unexpected_recovery.py run \\",
        ),
        "single_quoted_split_runner_path": (
            first_runner_line,
            (
                "uv run python '.artifacts/rc-candidate/"
                "generation_projection_reco\\\n"
                "very.py' run \\\n"
            ),
        ),
        "single_quoted_uv": (
            first_runner_line,
            first_runner_line.replace("uv run", "'uv' run", 1),
        ),
        "single_quoted_runner_path": (
            first_runner_line,
            first_runner_line.replace(
                ".artifacts/rc-candidate/"
                "generation_projection_recovery.py",
                "'.artifacts/rc-candidate/"
                "generation_projection_recovery.py'",
            ),
        ),
        "single_quoted_run_subcommand": (
            first_runner_line,
            first_runner_line.replace(" run \\\n", " 'run' \\\n"),
        ),
        "unknown_option": (
            api_option,
            f"{api_option}  --unexpected value \\\n",
        ),
        "missing_option": (api_option, ""),
        "duplicate_option": (api_option, api_option * 2),
        "reordered_runner_options": (
            mcp_option + api_option,
            api_option + mcp_option,
        ),
        "collapsed_runner_option_lines": (
            mcp_option + api_option,
            mcp_option.removesuffix("\\\n")
            + api_option.removeprefix("  "),
        ),
        "malformed_continuation": (
            mcp_option,
            mcp_option.removesuffix("\\\n") + "\n",
        ),
        "command_substitution_continuation": (
            first_fault_id,
            (
                '  --fault-id "${RECOVERY_RUN_ID}-'
                "01-generation-precommit$\\\n"
                "(uv run python .artifacts/rc-candidate/"
                'unexpected_recovery.py run)" \\\n'
            ),
        ),
        "carriage_return_in_option": (
            first_fault_id,
            first_fault_id.replace(
                'generation-precommit"',
                'generation-precommit\rother"',
            ),
        ),
        "nul_in_option": (
            first_fault_id,
            first_fault_id.replace(
                'generation-precommit"',
                'generation-precommit\0other"',
            ),
        ),
        "extra_positional": (
            first_evidence_dir,
            first_evidence_dir + " unexpected positional",
        ),
        "wrong_run_id_expansion": (
            first_fault_id,
            first_fault_id.replace(
                "${RECOVERY_RUN_ID}",
                "${OTHER_RUN_ID}",
            ),
        ),
        "mismatched_fault_evidence_suffix": (
            first_evidence_dir,
            first_evidence_dir.replace(
                "01-generation-precommit",
                "02-generation-postcommit",
            ),
        ),
        "candidate_manifest_not_first": (
            candidate_manifest_export + database_export,
            database_export + candidate_manifest_export,
        ),
        "evidence_root_before_run_id": (
            recovery_run_id_export + recovery_evidence_root_export,
            recovery_evidence_root_export + recovery_run_id_export,
        ),
        "swapped_plain_setup_exports": (
            qdrant_url_export + minio_endpoint_export,
            minio_endpoint_export + qdrant_url_export,
        ),
        "duplicate_setup_paragraph": (
            database_export,
            database_export * 2,
        ),
        "missing_setup_paragraph": (minio_secure_export, ""),
        "setup_command_before_terminal_test": (
            setup_test_end,
            setup_test_end.replace(
                "test ! -e",
                "echo unexpected\n"
                "test ! -e",
            ),
        ),
        "setup_command_after_terminal_test": (
            setup_test_end,
            setup_test_end.replace(
                "\n```",
                "\necho unexpected\n```",
            ),
        ),
        "renamed_paired_runner_child": (
            first_runner_tail,
            first_runner_tail.replace(
                "01-generation-precommit",
                "01-renamed",
            ),
        ),
        "reordered_finalizer_inputs": (
            first_finalizer_report + second_finalizer_report,
            second_finalizer_report + first_finalizer_report,
        ),
        "renamed_finalizer_input": (
            first_finalizer_report,
            first_finalizer_report.replace(
                "01-generation-precommit",
                "01-renamed",
            ),
        ),
        "missing_finalizer_input": (first_finalizer_report, ""),
        "duplicate_finalizer_input": (
            first_finalizer_report + second_finalizer_report,
            first_finalizer_report * 2,
        ),
        "wrong_finalizer_path": (
            finalizer_command_line,
            finalizer_command_line.replace(
                finalizer_path,
                ".artifacts/rc-candidate/unexpected_finalizer.py",
            ),
        ),
        "wrong_finalizer_subcommand": (
            finalizer_command_line,
            finalizer_command_line.replace(" \\\n", " run \\\n"),
        ),
        "single_quoted_finalizer_path": (
            finalizer_command_line,
            finalizer_command_line.replace(
                finalizer_path,
                f"'{finalizer_path}'",
            ),
        ),
        "unknown_finalizer_option": (
            finalizer_output,
            "  --unexpected value \\\n" + finalizer_output,
        ),
        "extra_finalizer_command": (
            finalizer_block_end,
            finalizer_block_end.replace(
                "\n```",
                "\necho unexpected\n```",
            ),
        ),
        "unsafe_finalizer_shell_expansion": (
            finalizer_output,
            finalizer_output.replace(
                "manifest.json",
                "manifest$\\\n(echo unsafe).json",
            ),
        ),
        "wrong_finalizer_output": (
            finalizer_output,
            finalizer_output.replace(
                "$RECOVERY_FINAL_DIR",
                "$RECOVERY_EVIDENCE_ROOT",
            ),
        ),
        "missing_finalizer_existence_test": (finalizer_test, ""),
        "unsafe_database_export": (
            database_export,
            (
                'export FORWIN_RECOVERY_DATABASE_URL="'
                f"{controller_recovery_database_url()}"
                '$(uv run python unexpected_recovery.py)"\n'
            ),
        ),
        "candidate_manifest_parameter_expansion": (
            first_fault_id + candidate_manifest_option,
            first_fault_id + candidate_manifest_option.replace(
                '"$FORWIN_RECOVERY_CANDIDATE_MANIFEST"',
                '"${FORWIN_RECOVERY_CANDIDATE_MANIFEST:-/tmp/other.json}"',
            ),
        ),
        "candidate_manifest_command_substitution_continuation": (
            first_fault_id + candidate_manifest_option,
            (
                first_fault_id
                + '  --candidate-manifest "$\\\n'
                + "(uv run python .artifacts/rc-candidate/"
                + 'unexpected_recovery.py run)" \\\n'
            ),
        ),
        "api_url_shell_expansion": (
            api_option,
            api_option.replace(api_url, expanded_api_url),
        ),
        "wrong_mcp_endpoint": (
            mcp_option,
            mcp_option.replace(mcp_url, wrong_mcp_url),
        ),
        "extra_command": (
            command_block_end,
            append_command("echo unexpected"),
        ),
        "export_command_substitution": (
            command_block_end,
            append_command(
                'export HIDDEN_RUN="$(uv run python '
                ".artifacts/rc-candidate/unexpected_recovery.py run)\""
            ),
        ),
        "export_backtick_substitution": (
            command_block_end,
            append_command(
                "export HIDDEN_RUN=\"`uv run python "
                ".artifacts/rc-candidate/unexpected_recovery.py run`\""
            ),
        ),
        "benign_export": (
            command_block_end,
            append_command("export HIDDEN_RUN=disabled"),
        ),
        "quoted_semicolon": (
            first_fault_id,
            first_fault_id.replace(
                'generation-precommit"',
                'generation-precommit;hidden"',
            ),
        ),
        "quoted_and_operator": (
            first_fault_id,
            first_fault_id.replace(
                'generation-precommit"',
                'generation-precommit&&hidden"',
            ),
        ),
        "quoted_or_operator": (
            first_fault_id,
            first_fault_id.replace(
                'generation-precommit"',
                'generation-precommit||hidden"',
            ),
        ),
        "quoted_pipe": (
            first_fault_id,
            first_fault_id.replace(
                'generation-precommit"',
                'generation-precommit|hidden"',
            ),
        ),
        "quoted_redirect": (
            first_fault_id,
            first_fault_id.replace(
                'generation-precommit"',
                'generation-precommit>hidden"',
            ),
        ),
    }
    old, new = replacements[mutation]
    assert runbook.count(old) >= 1
    return runbook.replace(old, new, 1)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("wrong_subcommand", "run subcommand"),
        ("unsupported_runner_path", "canonical runner path"),
        ("single_quoted_split_runner_path", "single quote"),
        ("single_quoted_uv", "single quote"),
        ("single_quoted_runner_path", "single quote"),
        ("single_quoted_run_subcommand", "single quote"),
        ("unknown_option", "unknown option"),
        ("missing_option", "missing required options"),
        ("duplicate_option", "duplicate option"),
        ("reordered_runner_options", "runner option"),
        ("collapsed_runner_option_lines", "runner option"),
        ("malformed_continuation", "continuation"),
        ("command_substitution_continuation", "shell metacharacter"),
        ("carriage_return_in_option", "carriage return"),
        ("nul_in_option", "NUL"),
        ("extra_positional", "unexpected positional"),
        ("wrong_run_id_expansion", "fault-id"),
        (
            "mismatched_fault_evidence_suffix",
            "fault/evidence suffix",
        ),
        ("candidate_manifest_not_first", "setup"),
        ("evidence_root_before_run_id", "setup"),
        ("swapped_plain_setup_exports", "setup"),
        ("duplicate_setup_paragraph", "setup"),
        ("missing_setup_paragraph", "setup"),
        ("setup_command_before_terminal_test", "setup"),
        ("setup_command_after_terminal_test", "setup"),
        ("renamed_paired_runner_child", "finalizer"),
        ("reordered_finalizer_inputs", "finalizer"),
        ("renamed_finalizer_input", "finalizer"),
        ("missing_finalizer_input", "finalizer"),
        ("duplicate_finalizer_input", "finalizer"),
        ("wrong_finalizer_path", "finalizer"),
        ("wrong_finalizer_subcommand", "finalizer"),
        ("single_quoted_finalizer_path", "finalizer"),
        ("unknown_finalizer_option", "finalizer"),
        ("extra_finalizer_command", "finalizer"),
        ("unsafe_finalizer_shell_expansion", "finalizer"),
        ("wrong_finalizer_output", "finalizer"),
        ("missing_finalizer_existence_test", "finalizer"),
        ("unsafe_database_export", "setup export"),
        (
            "candidate_manifest_parameter_expansion",
            "candidate-manifest",
        ),
        (
            "candidate_manifest_command_substitution_continuation",
            "shell metacharacter",
        ),
        ("api_url_shell_expansion", "api-url"),
        ("wrong_mcp_endpoint", "mcp-url"),
        ("extra_command", "runner command shape"),
        ("export_command_substitution", "shell metacharacter"),
        ("export_backtick_substitution", "shell metacharacter"),
        ("benign_export", "runner command shape"),
        ("quoted_semicolon", "shell metacharacter"),
        ("quoted_and_operator", "shell metacharacter"),
        ("quoted_or_operator", "shell metacharacter"),
        ("quoted_pipe", "shell metacharacter"),
        ("quoted_redirect", "shell metacharacter"),
    ],
)
def test_live_recovery_runbook_parser_rejects_mutations(
    mutation: str,
    message: str,
) -> None:
    runbook = RECOVERY_RUNBOOK_PATH.read_text(encoding="utf-8")
    mutated = mutate_live_recovery_runbook(runbook, mutation)

    with pytest.raises(AssertionError, match=message):
        parse_live_recovery_commands(mutated)


def test_live_recovery_qdrant_fault_uses_the_candidate_collection() -> None:
    runbook = RECOVERY_RUNBOOK_PATH.read_text(encoding="utf-8")
    setup_block, _command_block = live_recovery_bash_blocks(runbook)

    assert "FORWIN_RECOVERY_QDRANT_COLLECTION" not in parse_live_recovery_setup(
        setup_block
    )
    assert re.search(
        r"effective\s+`FORWIN_LLM_KB_QDRANT_COLLECTION` from the exact\s+"
        r"candidate Compose",
        runbook,
    )


def test_live_recovery_runbook_commands_match_controller_contract() -> None:
    runbook = RECOVERY_RUNBOOK_PATH.read_text(encoding="utf-8")
    commands = parse_live_recovery_commands(runbook)
    expected_runner_by_fault = {}
    for runner, (supported_faults, _) in recovery_runner_contracts().items():
        for fault_kind in supported_faults:
            assert fault_kind not in expected_runner_by_fault
            expected_runner_by_fault[fault_kind] = runner

    observed_runner_by_fault = {}
    for runner, options in commands:
        fault_kind = options["--fault-kind"]
        assert fault_kind not in observed_runner_by_fault
        observed_runner_by_fault[fault_kind] = runner

    assert len(commands) == 11
    assert observed_runner_by_fault == expected_runner_by_fault

    api_endpoint = (
        "http",
        stack.COMPOSE_ENV["FORWIN_HTTP_BIND"],
        int(stack.COMPOSE_ENV["FORWIN_HTTP_PORT"]),
        "",
    )
    mcp_host, mcp_port = stack.COMPOSE_ENV[
        "FORWIN_MCP_DEBUG_BIND"
    ].rsplit(":", 1)
    mcp_endpoint = ("http", mcp_host, int(mcp_port), "/mcp")
    for _, options in commands:
        api_url = urlsplit(options["--api-url"])
        assert (
            api_url.scheme,
            api_url.hostname,
            api_url.port,
            api_url.path,
        ) == api_endpoint
        mcp_url = urlsplit(options["--mcp-url"])
        assert (
            mcp_url.scheme,
            mcp_url.hostname,
            mcp_url.port,
            mcp_url.path,
        ) == mcp_endpoint

    exports = dict(
        re.findall(
            r'^export ([A-Z][A-Z0-9_]*)="([^"\n]*)"$',
            runbook,
            re.MULTILINE,
        )
    )
    database_envs = {
        options["--database-url-env"] for _, options in commands
    }
    assert len(database_envs) == 1
    database_url = urlsplit(exports[database_envs.pop()])
    database_host, database_port = stack.COMPOSE_ENV[
        "FORWIN_RECOVERY_POSTGRES_BIND"
    ].rsplit(":", 1)
    service_database_url = urlsplit(stack.ISOLATED_DATABASE_URL)
    assert (
        database_url.hostname,
        database_url.port,
        database_url.username,
        database_url.password,
        database_url.path,
    ) == (
        database_host,
        int(database_port),
        service_database_url.username,
        service_database_url.password,
        service_database_url.path,
    )

    fault_ids = [options["--fault-id"] for _, options in commands]
    evidence_dirs = [options["--evidence-dir"] for _, options in commands]
    assert len(set(fault_ids)) == len(commands)
    assert len(set(evidence_dirs)) == len(commands)
    assert all(
        fault_id.startswith("${RECOVERY_RUN_ID}-")
        for fault_id in fault_ids
    )
    assert all(
        evidence_dir.startswith("$RECOVERY_EVIDENCE_ROOT/")
        for evidence_dir in evidence_dirs
    )


def run_mark_process(
    evidence_dir: str,
    phase: str,
    results: multiprocessing.Queue,
) -> None:
    os.environ[stack.EVIDENCE_DIR_ENV] = evidence_dir
    try:
        event = stack.mark_fault("publisher_captcha", phase, "fault-1")
    except Exception as exc:
        results.put(("error", str(exc)))
    else:
        results.put(("ok", event["action"]))


def run_fresh_up_process(
    evidence_dir: str,
    results: multiprocessing.Queue,
) -> None:
    os.environ[stack.EVIDENCE_DIR_ENV] = evidence_dir
    try:
        stack.fresh_up("fault-1")
    except Exception as exc:
        results.put(("error", str(exc)))
    else:
        results.put(("ok", "fresh_up_completed"))


def run_destroy_process(
    evidence_dir: str,
    results: multiprocessing.Queue,
) -> None:
    os.environ[stack.EVIDENCE_DIR_ENV] = evidence_dir
    try:
        stack.destroy()
    except Exception as exc:
        results.put(("error", str(exc)))
    else:
        results.put(("ok", "destroyed"))


def run_setup_hold_process(
    evidence_dir: str,
    results: multiprocessing.Queue,
) -> None:
    os.environ[stack.EVIDENCE_DIR_ENV] = evidence_dir
    try:
        event = stack.setup_hold_service(
            "outbox-worker",
            "fault-1",
            "hold-1",
            "minio_post_canon_unavailable",
            "auxiliary",
        )
    except Exception as exc:
        results.put(("error", str(exc)))
    else:
        results.put(("ok", event["action"]))


def run_abort_process(
    evidence_dir: str,
    results: multiprocessing.Queue,
) -> None:
    os.environ[stack.EVIDENCE_DIR_ENV] = evidence_dir
    try:
        stack.abort_recovery_run(
            "fault-1",
            "preflight",
            "operator requested abort",
        )
    except Exception as exc:
        results.put(("error", str(exc)))
    else:
        results.put(("ok", "unexpected-success"))


def run_lock_probe_process(
    evidence_dir: str,
    results: multiprocessing.Queue,
) -> None:
    path = stack.controller_lock_path(Path(evidence_dir))
    try:
        with path.open("a+", encoding="utf-8") as handle:
            stack.fcntl.flock(
                handle.fileno(),
                stack.fcntl.LOCK_EX | stack.fcntl.LOCK_NB,
            )
            stack.fcntl.flock(handle.fileno(), stack.fcntl.LOCK_UN)
    except Exception as exc:
        results.put(("error", str(exc)))
    else:
        results.put(("ok", "acquired"))


def joined_process_result(
    process: multiprocessing.Process,
    results: multiprocessing.Queue,
) -> tuple[str, str]:
    process.join(timeout=5)
    if process.is_alive():
        process.terminate()
        process.join(timeout=2)
        pytest.fail(f"child process did not finish: {process.name}")
    assert process.exitcode == 0
    return results.get(timeout=2)


def recovery_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    fault_id: str = "fault-1",
) -> tuple[dict, dict, dict]:
    evidence_dir = (tmp_path / fault_id).resolve()
    monkeypatch.setenv(stack.EVIDENCE_DIR_ENV, str(evidence_dir))
    run_id = (
        "a" * 32
        if fault_id == "fault-1"
        else stack.stable_hash(fault_id)[:32]
    )
    run_identity = {
        "run_id": run_id,
        "evidence_directory": str(evidence_dir),
        "database_volume_name": (
            f"forwin-v5-recovery-{run_id}-postgres-data"
        ),
    }
    volume_absent = {
        "name": run_identity["database_volume_name"],
        "exists": False,
    }
    volume_present = {
        "name": run_identity["database_volume_name"],
        "exists": True,
        "created_at": "2026-07-22T11:58:30+00:00",
        "fingerprint": stack.stable_hash(
            {
                "created_at": "2026-07-22T11:58:30+00:00",
                "name": run_identity["database_volume_name"],
            }
        ),
    }
    identity = frozen_identity()
    stack.append_event(
        "fresh_up_started",
        fault_id=fault_id,
        requested_at="2026-07-22T11:58:00+00:00",
        identity=identity,
        run_identity=run_identity,
        database_volume=volume_absent,
    )
    stack.append_event(
        "fresh_up_completed",
        fault_id=fault_id,
        requested_at="2026-07-22T11:58:00+00:00",
        identity=identity,
        run_identity=run_identity,
        database_volume=volume_present,
    )
    return run_identity, volume_present, identity


def configure_primary_fresh_up_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    run_digit: str,
) -> tuple[Path, dict, dict]:
    evidence_dir = (tmp_path / f"fresh-failure-{run_digit}").resolve()
    monkeypatch.setenv(stack.EVIDENCE_DIR_ENV, str(evidence_dir))
    monkeypatch.setattr(stack.secrets, "token_hex", lambda _size: run_digit * 32)
    identity = frozen_identity()
    volume_name = (
        f"forwin-v5-recovery-{run_digit * 32}-postgres-data"
    )
    absent = {"name": volume_name, "exists": False}
    monkeypatch.setattr(stack, "assert_frozen", lambda **_kwargs: identity)
    monkeypatch.setattr(
        stack,
        "assert_isolated_compose",
        lambda _identity, *, run_identity: None,
    )
    monkeypatch.setattr(
        stack,
        "database_volume_observation",
        lambda _run_identity, **_kwargs: absent,
    )
    monkeypatch.setattr(
        stack,
        "stack_snapshot",
        lambda **_kwargs: {"stage": "before", "services": {}},
    )
    monkeypatch.setattr(
        stack,
        "compose",
        lambda *args, **_kwargs: (
            (_ for _ in ()).throw(
                stack.StackError("primary dependency setup failure")
            )
        ),
    )
    monkeypatch.setattr(
        stack,
        "compose_process",
        lambda *args, **_kwargs: subprocess.CompletedProcess(
            args,
            0,
            "",
            "",
        ),
    )
    monkeypatch.setattr(
        stack,
        "destroyed_service_inventory",
        lambda _run_identity, **_kwargs: {
            service: {"exists": False, "running": False}
            for service in stack.SERVICES
        },
    )
    monkeypatch.setattr(
        stack,
        "now",
        lambda: "2026-07-22T12:00:00+00:00",
    )
    return evidence_dir, identity, absent


def isolated_compose_config(
    *,
    run_identity: dict | None = None,
) -> tuple[dict, dict]:
    runtime_tag = "forwin-runtime:v1"
    browser_tag = "forwin-browser:v1"
    dependency_tags = {
        "postgres": "postgres:16-alpine",
        "qdrant": "qdrant/qdrant:v1.17.1",
        "minio": "minio/minio:release",
    }
    environment = {
        "FORWIN_DATABASE_URL": stack.ISOLATED_DATABASE_URL,
        "FORWIN_QDRANT_URL": stack.ISOLATED_QDRANT_URL,
        "FORWIN_ARTIFACT_BACKEND": "minio",
        "FORWIN_MINIO_ENDPOINT": stack.ISOLATED_MINIO_ENDPOINT,
        "FORWIN_MINIO_ACCESS_KEY": stack.ISOLATED_MINIO_ACCESS_KEY,
        "FORWIN_MINIO_SECRET_KEY": stack.ISOLATED_MINIO_SECRET_KEY,
        "FORWIN_MINIO_BUCKET": "forwin-recovery-artifacts",
        "FORWIN_MINIO_PREFIX": "artifacts",
        "FORWIN_MINIO_SECURE": "false",
        "FORWIN_API_BASE_URL": stack.ISOLATED_API_BASE_URL,
        "FORWIN_BACKEND_URL": stack.ISOLATED_API_BASE_URL,
    }
    services = {
        service: {
            "image": (
                browser_tag if service == "publisher-browser" else runtime_tag
            ),
            "environment": dict(environment),
        }
        for service in (
            "forwin",
            "generation-worker",
            "outbox-worker",
            "forwin-mcp",
            "publisher-worker",
            "publisher-browser",
        )
    }
    services["postgres"] = {
        "image": dependency_tags["postgres"],
        "volumes": [
            {
                "type": "volume",
                "source": "forwin-postgres",
                "target": "/var/lib/postgresql/data",
            }
        ],
        "environment": {
            "POSTGRES_USER": "forwin",
            "POSTGRES_PASSWORD": "forwin",
            "POSTGRES_DB": "forwin",
        }
    }
    services["qdrant"] = {"image": dependency_tags["qdrant"]}
    services["minio"] = {
        "image": dependency_tags["minio"],
        "environment": {
            "MINIO_ROOT_USER": stack.ISOLATED_MINIO_ACCESS_KEY,
            "MINIO_ROOT_PASSWORD": stack.ISOLATED_MINIO_SECRET_KEY,
        }
    }
    services["generation-worker"]["environment"]["FORWIN_DATABASE_URL"] = (
        stack.GENERATION_WORKER_DATABASE_URL
    )
    services["outbox-worker"]["environment"]["FORWIN_DATABASE_URL"] = (
        stack.OUTBOX_WORKER_DATABASE_URL
    )
    services["publisher-worker"]["environment"]["FORWIN_DATABASE_URL"] = (
        stack.PUBLISHER_WORKER_DATABASE_URL
    )
    identity = {
        "runtime_image": {"tag": runtime_tag},
        "browser_image": {"tag": browser_tag},
        "dependency_images": {
            service: {"tag": tag}
            for service, tag in dependency_tags.items()
        },
    }
    project_name = (
        stack.PROJECT
        if run_identity is None
        else stack.recovery_project_name(run_identity)
    )
    database_volume_name = (
        f"{stack.PROJECT}_forwin-postgres"
        if run_identity is None
        else run_identity["database_volume_name"]
    )
    container_suffixes = {
        "forwin": "api",
        "generation-worker": "generation-worker",
        "outbox-worker": "outbox-worker",
        "postgres": "postgres",
        "qdrant": "qdrant",
        "forwin-mcp": "mcp",
        "publisher-worker": "publisher-worker",
        "publisher-browser": "publisher-browser",
        "minio": "minio",
    }
    for service, suffix in container_suffixes.items():
        services[service]["container_name"] = f"{project_name}-{suffix}"
    return {
        "name": project_name,
        "services": services,
        "volumes": {
            "forwin-postgres": {
                "name": database_volume_name,
            }
        },
    }, identity


def test_effective_compose_validator_rejects_external_database_url() -> None:
    payload, identity = isolated_compose_config()
    stack.validate_isolated_compose_config(payload, identity=identity)
    payload["services"]["generation-worker"]["environment"][
        "FORWIN_DATABASE_URL"
    ] = "postgresql+psycopg://external-host:5432/production"

    with pytest.raises(stack.StackError, match="generation-worker.*DATABASE_URL"):
        stack.validate_isolated_compose_config(payload, identity=identity)


def test_effective_compose_validator_rejects_extra_service() -> None:
    payload, identity = isolated_compose_config()
    payload["services"]["postgres-test"] = {}

    with pytest.raises(stack.StackError, match="service set"):
        stack.validate_isolated_compose_config(payload, identity=identity)


def test_llm_kb_collection_uses_the_runtime_default_when_compose_omits_it(
) -> None:
    from forwin.config import InfrastructureConfig

    payload, _identity = isolated_compose_config()
    runtime_default = InfrastructureConfig.model_fields[
        "llm_kb_qdrant_collection"
    ].default

    assert stack.llm_kb_collection_from_compose_config(payload) == runtime_default


def test_llm_kb_collection_rejects_effective_runtime_drift() -> None:
    payload, _identity = isolated_compose_config()
    for service in stack.LLM_KB_RUNTIME_SERVICES:
        payload["services"][service]["environment"][
            "FORWIN_LLM_KB_QDRANT_COLLECTION"
        ] = "candidate-runtime-vectors"
    payload["services"]["outbox-worker"]["environment"][
        "FORWIN_LLM_KB_QDRANT_COLLECTION"
    ] = "detached-outbox-vectors"

    with pytest.raises(stack.StackError, match="LLM-KB Qdrant collection drift"):
        stack.llm_kb_collection_from_compose_config(payload)


def test_explicit_default_and_runtime_defaults_do_not_count_as_drift() -> None:
    from forwin.config import InfrastructureConfig

    payload, _identity = isolated_compose_config()
    runtime_default = InfrastructureConfig.model_fields[
        "llm_kb_qdrant_collection"
    ].default
    payload["services"]["outbox-worker"]["environment"][
        "FORWIN_LLM_KB_QDRANT_COLLECTION"
    ] = runtime_default

    assert stack.llm_kb_collection_from_compose_config(payload) == runtime_default


def test_dynamic_effective_compose_config_binds_project_containers_and_db_mount(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker CLI is unavailable")
    compose_version = subprocess.run(
        [docker, "compose", "version"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if compose_version.returncode:
        pytest.skip("Docker Compose CLI is unavailable")

    evidence_dir = (tmp_path / "dynamic-config").resolve()
    monkeypatch.setenv(stack.EVIDENCE_DIR_ENV, str(evidence_dir))
    run_identity = stack.new_recovery_run_identity(
        "dynamic-config",
        run_id="2" * 32,
        directory=evidence_dir,
    )
    runtime_env = tmp_path / "runtime.env"
    provider_env = tmp_path / "provider.env"
    runtime_env.write_text(
        "FORWIN_LLM_KB_QDRANT_COLLECTION=runtime-vectors\n",
        encoding="utf-8",
    )
    provider_env.write_text(
        "FORWIN_LLM_KB_QDRANT_COLLECTION=provider-vectors\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "candidate.json"
    runtime_tag = "forwin-v5-runtime:dynamic-config-test"
    browser_tag = "forwin-v5-browser:dynamic-config-test"
    dependency_tags = {
        "postgres": "postgres:16-alpine",
        "qdrant": "qdrant/qdrant:v1.17.1",
        "minio": "minio/minio:dynamic-config-test",
    }
    manifest.write_text(
        json.dumps(
            {
                "source": {"sha": SOURCE_SHA},
                "images": {
                    "runtime": {"tag": runtime_tag},
                    "publisher_browser": {"tag": browser_tag},
                    **{
                        service: {"tag": tag}
                        for service, tag in dependency_tags.items()
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(stack.CANDIDATE_MANIFEST_ENV, str(manifest))
    monkeypatch.setenv(stack.RUNTIME_ENV_FILE_ENV, str(runtime_env))
    monkeypatch.setenv(stack.PROVIDER_ENV_FILE_ENV, str(provider_env))
    completed = stack.compose_process(
        "config",
        "--format",
        "json",
        run_identity=run_identity,
        timeout_seconds=10,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    identity = {
        "runtime_image": {"tag": runtime_tag},
        "browser_image": {"tag": browser_tag},
        "dependency_images": {
            service: {"tag": tag}
            for service, tag in dependency_tags.items()
        },
    }
    stack.validate_isolated_compose_config(
        payload,
        identity=identity,
        run_identity=run_identity,
    )
    assert stack.llm_kb_collection_from_compose_config(payload) == (
        "provider-vectors"
    )
    project_name = stack.recovery_project_name(run_identity)
    assert payload["name"] == project_name
    assert payload["volumes"]["forwin-postgres"]["name"] == (
        run_identity["database_volume_name"]
    )
    assert {
        service: item["container_name"]
        for service, item in payload["services"].items()
    } == {
        "forwin": f"{project_name}-api",
        "generation-worker": f"{project_name}-generation-worker",
        "outbox-worker": f"{project_name}-outbox-worker",
        "postgres": f"{project_name}-postgres",
        "qdrant": f"{project_name}-qdrant",
        "forwin-mcp": f"{project_name}-mcp",
        "publisher-worker": f"{project_name}-publisher-worker",
        "publisher-browser": f"{project_name}-publisher-browser",
        "minio": f"{project_name}-minio",
    }
    postgres_mounts = payload["services"]["postgres"]["volumes"]
    assert len(postgres_mounts) == 1
    assert {
        key: postgres_mounts[0][key]
        for key in ("type", "source", "target")
    } == {
        "type": "volume",
        "source": "forwin-postgres",
        "target": "/var/lib/postgresql/data",
    }
    payload["services"]["qdrant"]["container_name"] = (
        "forwin-v5-recovery-qdrant"
    )
    with pytest.raises(stack.StackError, match="qdrant.*container"):
        stack.validate_isolated_compose_config(
            payload,
            identity=identity,
            run_identity=run_identity,
        )

    payload["services"]["qdrant"]["container_name"] = (
        f"{project_name}-qdrant"
    )
    payload["services"]["postgres"]["volumes"][0]["source"] = "shared-postgres"
    with pytest.raises(stack.StackError, match="PostgreSQL volume mount"):
        stack.validate_isolated_compose_config(
            payload,
            identity=identity,
            run_identity=run_identity,
        )


def test_endpoint_binding_uses_verified_dynamic_published_mappings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_identity, volume, identity = recovery_lifecycle(tmp_path, monkeypatch)
    mappings = {
        ("forwin", 8899): {
            "service": "forwin",
            "host": "127.0.0.1",
            "host_port": 24111,
            "container_port": 8899,
            "container_id": "api-container-dynamic",
            "image_id": "sha256:" + "c" * 64,
        },
        ("forwin-mcp", 8896): {
            "service": "forwin-mcp",
            "host": "127.0.0.1",
            "host_port": 24112,
            "container_port": 8896,
            "container_id": "mcp-container-dynamic",
            "image_id": "sha256:" + "c" * 64,
        },
        ("postgres", 5432): {
            "service": "postgres",
            "host": "127.0.0.1",
            "host_port": 24113,
            "container_port": 5432,
            "container_id": "postgres-container-dynamic",
            "image_id": "sha256:" + "d" * 64,
        },
        ("qdrant", 6333): {
            "service": "qdrant",
            "host": "127.0.0.1",
            "host_port": 24114,
            "container_port": 6333,
            "container_id": "qdrant-container-dynamic",
            "image_id": "sha256:" + "e" * 64,
        },
        ("minio", 9000): {
            "service": "minio",
            "host": "127.0.0.1",
            "host_port": 24115,
            "container_port": 9000,
            "container_id": "minio-container-dynamic",
            "image_id": "sha256:" + "f" * 64,
        },
    }
    sentinel = {
        "table": stack.RECOVERY_SENTINEL_TABLE,
        "sentinel_id": "e" * 64,
        "run_id": run_identity["run_id"],
        "fault_id": "fault-1",
        "source_sha": SOURCE_SHA,
    }
    events = stack.load_verified_events()
    events[1]["sentinel"] = sentinel
    previous = "0" * 64
    for event in events:
        event["previous_event_sha256"] = previous
        event["event_sha256"] = stack.event_hash(event)
        previous = event["event_sha256"]
    stack.events_path().write_text(
        "\n".join(json.dumps(event, sort_keys=True) for event in events)
        + "\n",
        encoding="utf-8",
    )
    probes: list[str] = []
    monkeypatch.setattr(stack, "assert_frozen", lambda: identity)
    monkeypatch.setattr(
        stack,
        "assert_isolated_compose",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        stack,
        "published_endpoint_identity",
        lambda service, container_port, **_kwargs: dict(
            mappings[(service, container_port)]
        ),
    )
    monkeypatch.setattr(
        stack,
        "read_recovery_sentinel",
        lambda **_kwargs: dict(sentinel),
    )
    monkeypatch.setattr(
        stack,
        "probe_loopback_health",
        lambda url: probes.append(url) or 200,
    )
    monkeypatch.setattr(
        stack,
        "confirmed_database_volume",
        lambda _run_identity, _expected: volume,
    )

    record = stack.bind_recovery_endpoints(
        "fault-1",
        "http://127.0.0.1:24111",
        "http://127.0.0.1:24112/mcp",
        "127.0.0.1",
        24113,
        "forwin",
        qdrant_url="http://127.0.0.1:24114",
        minio_url="http://127.0.0.1:24115",
    )

    assert record["api"]["container_id"] == "api-container-dynamic"
    assert record["mcp"]["endpoint_path"] == "/mcp"
    assert record["database"]["port"] == 24113
    assert record["qdrant"]["service"] == "qdrant"
    assert record["minio"]["service"] == "minio"
    assert probes == [
        "http://127.0.0.1:24111/health",
        "http://127.0.0.1:24112/health",
        "http://127.0.0.1:24114/readyz",
        "http://127.0.0.1:24115/minio/health/ready",
    ]
    assert record["identity_sha256"] == stack.stable_hash(
        {
            key: value
            for key, value in record.items()
            if key != "identity_sha256"
        }
    )


@pytest.mark.parametrize(
    ("mutation", "expected"),
    (
        ("missing-console", "published mapping"),
        ("extra-port", "published mapping"),
        ("public-console", "not loopback"),
        ("image", "container identity"),
    ),
)
def test_minio_endpoint_identity_requires_exact_active_published_mapping(
    mutation: str,
    expected: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_dir = (tmp_path / "evidence").resolve()
    monkeypatch.setenv(stack.EVIDENCE_DIR_ENV, str(evidence_dir))
    run_identity = stack.new_recovery_run_identity(
        "minio-endpoint-identity",
        run_id="4" * 32,
        directory=evidence_dir,
    )
    container_id = "minio-container-exact"
    ports: dict[str, Any] = {
        "9000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "24115"}],
        "9001/tcp": [{"HostIp": "127.0.0.1", "HostPort": "24116"}],
    }
    image_id = "sha256:" + "f" * 64
    if mutation == "missing-console":
        ports.pop("9001/tcp")
    elif mutation == "extra-port":
        ports["9999/tcp"] = [
            {"HostIp": "127.0.0.1", "HostPort": "24117"}
        ]
    elif mutation == "public-console":
        ports["9001/tcp"][0]["HostIp"] = "0.0.0.0"
    else:
        image_id = ""
    payload = [
        {
            "Id": container_id,
            "Image": image_id,
            "Config": {
                "Labels": {
                    "com.docker.compose.project": (
                        stack.recovery_project_name(run_identity)
                    ),
                    "com.docker.compose.service": "minio",
                }
            },
            "State": {"Running": True},
            "NetworkSettings": {"Ports": ports},
        }
    ]
    monkeypatch.setattr(
        stack,
        "compose_container_id",
        lambda *_args, **_kwargs: container_id,
    )
    monkeypatch.setattr(
        stack,
        "command",
        lambda *_args: json.dumps(payload),
    )

    with pytest.raises(stack.StackError, match=expected):
        stack.published_endpoint_identity(
            "minio",
            9000,
            run_identity=run_identity,
        )


@pytest.mark.parametrize(
    ("api_url", "mcp_url", "database_host", "database_port", "database_name"),
    (
        (
            "http://0.0.0.0:24111",
            "http://127.0.0.1:24112/mcp",
            "127.0.0.1",
            24113,
            "forwin",
        ),
        (
            "http://127.0.0.1:24111",
            "http://127.0.0.1:24112/wrong",
            "127.0.0.1",
            24113,
            "forwin",
        ),
        (
            "http://127.0.0.1:24111",
            "http://127.0.0.1:24112/mcp",
            "127.0.0.1",
            24199,
            "forwin",
        ),
        (
            "http://127.0.0.1:24111",
            "http://127.0.0.1:24112/mcp",
            "127.0.0.1",
            24113,
            "other",
        ),
    ),
)
def test_endpoint_binding_rejects_cross_stack_or_wrong_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    api_url: str,
    mcp_url: str,
    database_host: str,
    database_port: int,
    database_name: str,
) -> None:
    run_identity, _volume, identity = recovery_lifecycle(tmp_path, monkeypatch)
    monkeypatch.setattr(stack, "assert_frozen", lambda: identity)
    monkeypatch.setattr(
        stack,
        "assert_isolated_compose",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        stack,
        "published_endpoint_identity",
        lambda service, container_port, **_kwargs: {
            "service": service,
            "host": "127.0.0.1",
            "host_port": {
                8899: 24111,
                8896: 24112,
                5432: 24113,
            }[container_port],
            "container_port": container_port,
            "container_id": f"{service}-container",
            "image_id": "sha256:" + "c" * 64,
        },
    )

    with pytest.raises(stack.StackError):
        stack.bind_recovery_endpoints(
            "fault-1",
            api_url,
            mcp_url,
            database_host,
            database_port,
            database_name,
        )
def test_recovery_override_pins_stateful_endpoints_to_isolated_services() -> None:
    payload = yaml.safe_load(
        stack.COMPOSE_OVERRIDE.read_text(encoding="utf-8")
    )
    expected = {
        "FORWIN_DATABASE_URL": stack.ISOLATED_DATABASE_URL,
        "FORWIN_QDRANT_URL": stack.ISOLATED_QDRANT_URL,
        "FORWIN_ARTIFACT_BACKEND": "minio",
        "FORWIN_MINIO_ENDPOINT": stack.ISOLATED_MINIO_ENDPOINT,
        "FORWIN_MINIO_ACCESS_KEY": stack.ISOLATED_MINIO_ACCESS_KEY,
        "FORWIN_MINIO_SECRET_KEY": stack.ISOLATED_MINIO_SECRET_KEY,
        "FORWIN_API_BASE_URL": stack.ISOLATED_API_BASE_URL,
        "FORWIN_BACKEND_URL": stack.ISOLATED_API_BASE_URL,
    }
    application_services = (
        "forwin",
        "generation-worker",
        "outbox-worker",
        "forwin-mcp",
        "publisher-worker",
        "publisher-browser",
    )
    assert payload["services"]["postgres-test"]["profiles"] == [
        "recovery-excluded"
    ]

    for service in application_services:
        environment = payload["services"][service].get("environment") or {}
        service_expected = {
            **expected,
            "FORWIN_DATABASE_URL": stack.SERVICE_DATABASE_URLS.get(
                service,
                stack.ISOLATED_DATABASE_URL,
            ),
        }
        assert {
            key: environment.get(key)
            for key in service_expected
        } == service_expected


def test_recovery_override_binds_worker_database_application_names() -> None:
    payload = yaml.safe_load(
        stack.COMPOSE_OVERRIDE.read_text(encoding="utf-8")
    )

    generation_url = payload["services"]["generation-worker"][
        "environment"
    ]["FORWIN_DATABASE_URL"]
    outbox_url = payload["services"]["outbox-worker"]["environment"][
        "FORWIN_DATABASE_URL"
    ]
    publisher_url = payload["services"]["publisher-worker"]["environment"][
        "FORWIN_DATABASE_URL"
    ]

    assert generation_url == (
        f"{stack.ISOLATED_DATABASE_URL}"
        "?application_name=forwin-recovery-generation-worker"
    )
    assert outbox_url == (
        f"{stack.ISOLATED_DATABASE_URL}"
        "?application_name=forwin-recovery-outbox-worker"
    )
    assert publisher_url == (
        f"{stack.ISOLATED_DATABASE_URL}"
        "?application_name=forwin-recovery-publisher-worker"
    )
    assert len({generation_url, outbox_url, publisher_url}) == 3


def test_file_inventory_is_read_only_identity_checked_and_data_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = frozen_identity()
    run_identity = {"run_id": "3" * 32}
    calls: list[tuple[tuple[str, ...], dict]] = []
    monkeypatch.setattr(
        stack,
        "require_active_recovery_run",
        lambda fault_id: {
            "identity": identity,
            "run_identity": run_identity,
        },
    )
    monkeypatch.setattr(stack, "assert_frozen", lambda: identity)
    monkeypatch.setattr(
        stack,
        "assert_isolated_compose",
        lambda *_args, **_kwargs: None,
    )

    def compose(*args: str, **kwargs: object) -> str:
        calls.append((args, dict(kwargs)))
        return json.dumps(
            {
                "root": "/app/data/publisher_covers",
                "root_exists": True,
                "files": [],
            }
        )

    monkeypatch.setattr(stack, "compose", compose)
    payload = stack.file_inventory.__wrapped__(
        "publisher-browser",
        "fault-inventory",
        "/app/data/publisher_covers",
    )

    assert payload["files"] == []
    args, kwargs = calls[0]
    assert args[:4] == (
        "exec",
        "-T",
        "publisher-browser",
        "python",
    )
    assert "is_symlink" in args[5]
    assert args[-1] == "/app/data/publisher_covers"
    assert kwargs == {"run_identity": run_identity}
    with pytest.raises(stack.StackError, match="publisher cover root"):
        stack.file_inventory.__wrapped__(
            "publisher-browser",
            "fault-inventory",
            "/etc",
        )
    with pytest.raises(stack.StackError, match="publisher-browser"):
        stack.file_inventory.__wrapped__(
            "forwin",
            "fault-inventory",
            "/app/data/publisher_covers",
        )


@pytest.mark.parametrize(
    "payload",
    (
        {
            "root": "/app/data/publisher_covers",
            "root_exists": False,
            "files": [],
        },
        {
            "root": "/app/data/publisher_covers",
            "root_exists": True,
            "files": [
                {
                    "path": "../escaped",
                    "size": 1,
                    "content_sha256": "0" * 64,
                }
            ],
        },
        {
            "root": "/app/data/publisher_covers",
            "root_exists": True,
            "files": [
                {
                    "path": "manual//cover.png",
                    "size": 1,
                    "content_sha256": "0" * 64,
                }
            ],
        },
        {
            "root": "/app/data/publisher_covers",
            "root_exists": True,
            "files": [
                {
                    "path": "manual/cover.png",
                    "size": 1,
                    "content_sha256": "0" * 64,
                },
                {
                    "path": "manual/cover.png",
                    "size": 1,
                    "content_sha256": "0" * 64,
                },
            ],
        },
        {
            "root": "/app/data/publisher_covers",
            "root_exists": True,
            "files": [
                {
                    "path": "/absolute",
                    "size": 1,
                    "content_sha256": "0" * 64,
                }
            ],
        },
    ),
)
def test_file_inventory_rejects_missing_root_and_escaped_rows(
    payload: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = frozen_identity()
    run_identity = {"run_id": "4" * 32}
    monkeypatch.setattr(
        stack,
        "require_active_recovery_run",
        lambda _fault_id: {
            "identity": identity,
            "run_identity": run_identity,
        },
    )
    monkeypatch.setattr(stack, "assert_frozen", lambda: identity)
    monkeypatch.setattr(
        stack,
        "assert_isolated_compose",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        stack,
        "compose",
        lambda *_args, **_kwargs: json.dumps(payload),
    )

    with pytest.raises(stack.StackError, match="file inventory"):
        stack.file_inventory.__wrapped__(
            "publisher-browser",
            "fault-inventory-generalized",
            "/app/data/publisher_covers",
        )


@pytest.mark.parametrize(
    "layout",
    ("missing", "root-symlink", "ancestor-symlink", "leaf-symlink"),
)
def test_file_inventory_script_rejects_missing_and_symlinked_paths(
    layout: str,
    tmp_path: Path,
) -> None:
    real_data = tmp_path / "real-data"
    real_data.mkdir()
    expected = tmp_path / "app" / "data" / "publisher_covers"
    expected.parent.mkdir(parents=True)
    if layout == "root-symlink":
        expected.symlink_to(real_data, target_is_directory=True)
    elif layout == "ancestor-symlink":
        shutil.rmtree(expected.parent)
        linked_data = tmp_path / "linked-data"
        linked_data.mkdir()
        (tmp_path / "app" / "data").symlink_to(
            linked_data,
            target_is_directory=True,
        )
        (linked_data / "publisher_covers").mkdir()
    elif layout == "leaf-symlink":
        expected.mkdir()
        target = tmp_path / "outside.bin"
        target.write_bytes(b"outside")
        (expected / "escaped.bin").symlink_to(target)

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            stack.file_inventory_script(),
            str(expected),
            str(expected),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0


def test_recovery_override_parameterizes_all_container_and_database_volume_names(
) -> None:
    payload = yaml.safe_load(stack.COMPOSE_OVERRIDE.read_text(encoding="utf-8"))

    assert payload["volumes"]["forwin-postgres"]["name"] == (
        "${FORWIN_RECOVERY_DATABASE_VOLUME_NAME:"
        "?set FORWIN_RECOVERY_DATABASE_VOLUME_NAME}"
    )
    for service in stack.SERVICES:
        container_name = payload["services"][service]["container_name"]
        assert container_name.startswith(
            "${FORWIN_RECOVERY_PROJECT_NAME:?set FORWIN_RECOVERY_PROJECT_NAME}-"
        )


def test_recovery_identity_and_compose_images_come_from_candidate_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = tmp_path / "runtime.env"
    provider_file = tmp_path / "provider.env"
    env_file.write_text("FORWIN_DATABASE_URL=postgresql://example\n", encoding="utf-8")
    provider_file.write_text("PROVIDER_API_KEY=secret\n", encoding="utf-8")
    runtime_tag = "forwin-v5-runtime:final"
    browser_tag = "forwin-v5-browser:final"
    runtime_id = "sha256:" + "3" * 64
    browser_id = "sha256:" + "4" * 64
    dependency_ids = {
        "postgres": "sha256:" + "5" * 64,
        "qdrant": "sha256:" + "6" * 64,
        "minio": "sha256:" + "7" * 64,
    }
    dependency_tags = {
        "postgres": "postgres:16-alpine",
        "qdrant": "qdrant/qdrant:v1.17.1",
        "minio": "minio/minio:release",
    }
    manifest_path = tmp_path / "candidate.json"
    manifest_path.write_text(
        json.dumps(
            {
                "source": {"sha": SOURCE_SHA},
                "images": {
                    "runtime": {
                        "tag": runtime_tag,
                        "image_id": runtime_id,
                        "revision": SOURCE_SHA,
                    },
                    "publisher_browser": {
                        "tag": browser_tag,
                        "image_id": browser_id,
                        "revision": SOURCE_SHA,
                    },
                    **{
                        service: {
                            "tag": dependency_tags[service],
                            "image_id": dependency_ids[service],
                        }
                        for service in dependency_tags
                    },
                },
                "release_harness": {
                    "files": [
                        {
                            "path": Path(artifact["path"])
                            .relative_to(stack.ROOT)
                            .as_posix(),
                            "sha256": artifact["sha256"],
                        }
                        for artifact in stack.harness_identity().values()
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("FORWIN_RECOVERY_CANDIDATE_MANIFEST", str(manifest_path))
    monkeypatch.setenv("FORWIN_RECOVERY_ENV_FILE", str(env_file))
    monkeypatch.setenv("FORWIN_RECOVERY_PROVIDER_ENV_FILE", str(provider_file))
    monkeypatch.setenv(
        "FORWIN_DATABASE_URL",
        "postgresql+psycopg://external-host:5432/production",
    )
    monkeypatch.setenv("FORWIN_QDRANT_URL", "http://external-host:6333")
    monkeypatch.setenv("FORWIN_MINIO_ENDPOINT", "external-host:9000")
    monkeypatch.setenv("FORWIN_API_BASE_URL", "http://external-host:8899")
    monkeypatch.setenv(
        "FORWIN_PUBLISHER_BROWSER_BACKEND_URL",
        "http://external-host:8899",
    )
    monkeypatch.setenv("UNRELATED_HOST_SECRET", "must-not-reach-compose")

    def fake_command(*command: str, **_kwargs: object) -> str:
        if command == ("git", "rev-parse", "HEAD"):
            return SOURCE_SHA
        if command == ("git", "rev-parse", "HEAD^{tree}"):
            return "a" * 40
        if command == ("git", "status", "--porcelain=v1", "--untracked-files=no"):
            return ""
        if command == ("docker", "context", "show"):
            return "desktop-linux"
        if command == ("docker", "context", "inspect", "desktop-linux"):
            return json.dumps(
                [
                    {
                        "Name": "desktop-linux",
                        "Endpoints": {
                            "docker": {
                                "Host": "unix:///tmp/docker.sock"
                            }
                        },
                    }
                ]
            )
        if command == ("docker", "info", "--format", "{{json .}}"):
            return json.dumps(
                {
                    "ID": "daemon-1",
                    "Name": "docker-desktop",
                    "ServerVersion": "28.0.0",
                    "OperatingSystem": "Docker Desktop",
                    "Architecture": "aarch64",
                }
            )
        if command[:3] == ("docker", "image", "inspect"):
            tag = command[3]
            image_id = {
                runtime_tag: runtime_id,
                browser_tag: browser_id,
                **{
                    dependency_tags[service]: dependency_ids[service]
                    for service in dependency_tags
                },
            }[tag]
            labels = (
                {"org.opencontainers.image.revision": SOURCE_SHA}
                if tag in {runtime_tag, browser_tag}
                else {}
            )
            return json.dumps(
                [
                    {
                        "Id": image_id,
                        "Config": {"Labels": labels},
                    }
                ]
            )
        raise AssertionError(command)

    monkeypatch.setattr(stack, "command", fake_command)

    identity = stack.assert_frozen()
    environment = stack.compose_environment()

    assert identity["source_sha"] == SOURCE_SHA
    assert identity["runtime_image"]["tag"] == runtime_tag
    assert identity["browser_image"]["tag"] == browser_tag
    assert identity["dependency_images"] == {
        service: {
            "tag": dependency_tags[service],
            "image_id": dependency_ids[service],
        }
        for service in dependency_tags
    }
    assert identity.get("harness") == stack.harness_identity()
    assert environment["FORWIN_RECOVERY_RUNTIME_IMAGE"] == runtime_tag
    assert environment["FORWIN_RECOVERY_BROWSER_IMAGE"] == browser_tag
    assert environment["FORWIN_RECOVERY_SOURCE_SHA"] == SOURCE_SHA
    assert environment["FORWIN_RECOVERY_POSTGRES_IMAGE"] == dependency_tags["postgres"]
    assert environment["FORWIN_RECOVERY_QDRANT_IMAGE"] == dependency_tags["qdrant"]
    assert environment["FORWIN_RECOVERY_MINIO_IMAGE"] == dependency_tags["minio"]
    assert (
        environment["FORWIN_DATABASE_URL"]
        == "postgresql+psycopg://forwin:forwin@postgres:5432/forwin"
    )
    assert environment["FORWIN_QDRANT_URL"] == "http://qdrant:6333"
    assert environment["FORWIN_MINIO_ENDPOINT"] == "minio:9000"
    assert environment["FORWIN_API_BASE_URL"] == "http://forwin:8899"
    assert (
        environment["FORWIN_PUBLISHER_BROWSER_BACKEND_URL"]
        == "http://forwin:8899"
    )
    assert "UNRELATED_HOST_SECRET" not in environment


def test_compose_environment_rejects_docker_endpoint_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOCKER_HOST", "tcp://production.example:2376")

    with pytest.raises(stack.StackError, match="control environment"):
        stack.compose_environment()


def test_docker_execution_identity_rejects_remote_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_command(*command: str, **_kwargs: object) -> str:
        if command == ("docker", "context", "show"):
            return "remote"
        if command == ("docker", "context", "inspect", "remote"):
            return json.dumps(
                [
                    {
                        "Name": "remote",
                        "Endpoints": {
                            "docker": {
                                "Host": "tcp://production.example:2376"
                            }
                        },
                    }
                ]
            )
        raise AssertionError(command)

    monkeypatch.setattr(stack, "command", fake_command)

    with pytest.raises(stack.StackError, match="local Unix socket"):
        stack.docker_execution_identity()


def test_recovery_events_are_written_to_explicit_hash_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_dir = tmp_path / "evidence"
    monkeypatch.setenv("FORWIN_RECOVERY_EVIDENCE_DIR", str(evidence_dir))
    monkeypatch.setattr(stack, "now", lambda: "2026-07-22T12:00:00+00:00")

    first = stack.append_event("fault_started", fault_id="qdrant-1")
    second = stack.append_event("fault_recovered", fault_id="qdrant-1")

    assert first["schema_version"] == 2
    assert first["previous_event_sha256"] == "0" * 64
    assert second["previous_event_sha256"] == first["event_sha256"]
    assert stack.load_verified_events() == [first, second]


def test_recovery_run_identity_is_canonical_and_drives_unique_compose_resources(
    tmp_path: Path,
) -> None:
    run_identity = stack.new_recovery_run_identity(
        "fault-1",
        run_id="b" * 32,
        directory=(tmp_path / "evidence").resolve(),
    )

    assert run_identity == {
        "run_id": "b" * 32,
        "evidence_directory": str((tmp_path / "evidence").resolve()),
        "database_volume_name": (
            "forwin-v5-recovery-" + "b" * 32 + "-postgres-data"
        ),
    }
    assert stack.recovery_project_name(run_identity) == (
        "forwin-v5-recovery-" + "b" * 32
    )

    with pytest.raises(stack.StackError, match="fault identity"):
        stack.new_recovery_run_identity(
            "",
            run_id="b" * 32,
            directory=(tmp_path / "empty").resolve(),
        )
    with pytest.raises(stack.StackError, match="run identity"):
        stack.new_recovery_run_identity(
            "fault-1",
            run_id="../shared",
            directory=(tmp_path / "invalid").resolve(),
        )


def test_recovery_harness_binds_v1_and_recovery_finalizers_unambiguously() -> None:
    harness = stack.harness_identity()

    assert Path(harness["candidate_mcp_call"]["path"]).name == (
        "candidate_mcp_call.py"
    )
    assert Path(harness["http_auth"]["path"]).name == "release_http_auth.py"
    assert Path(harness["v1_finalizer"]["path"]).name == "finalize_v1.py"
    assert Path(harness["recovery_finalizer"]["path"]).name == (
        "finalize_recovery.py"
    )
    assert "finalizer" not in harness


def test_database_volume_observation_normalizes_real_docker_inspect_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_dir = (tmp_path / "volume-observation").resolve()
    monkeypatch.setenv(stack.EVIDENCE_DIR_ENV, str(evidence_dir))
    run_identity = stack.new_recovery_run_identity(
        "volume-observation",
        run_id="3" * 32,
        directory=evidence_dir,
    )
    volume_name = run_identity["database_volume_name"]
    project_name = stack.recovery_project_name(run_identity)

    def fake_command(*command: str, **_kwargs: object) -> str:
        if command == (
            "docker",
            "volume",
            "ls",
            "--format",
            "{{.Name}}",
        ):
            return volume_name
        if command == ("docker", "volume", "inspect", volume_name):
            return json.dumps(
                [
                    {
                        "Name": volume_name,
                        "CreatedAt": "2026-07-22T20:58:30+09:00",
                        "Labels": {
                            "com.docker.compose.project": project_name,
                            "com.docker.compose.volume": "forwin-postgres",
                        },
                    }
                ]
            )
        raise AssertionError(command)

    monkeypatch.setattr(stack, "command", fake_command)

    observation = stack.database_volume_observation(run_identity)

    assert observation == {
        "name": volume_name,
        "exists": True,
        "created_at": "2026-07-22T11:58:30+00:00",
        "fingerprint": stack.stable_hash(
            {
                "created_at": "2026-07-22T11:58:30+00:00",
                "name": volume_name,
            }
        ),
    }


@pytest.mark.parametrize(
    "inspect_output",
    [
        "not-json",
        "{}",
        "[]",
        "[{}]",
        '[{"Name":"wrong-volume","CreatedAt":"2026-07-22T12:00:00Z"}]',
        '[{"Name":"placeholder","Labels":{}}]',
    ],
)
def test_database_volume_observation_rejects_malformed_or_missing_inspect(
    inspect_output: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_dir = (tmp_path / "malformed-volume").resolve()
    monkeypatch.setenv(stack.EVIDENCE_DIR_ENV, str(evidence_dir))
    run_identity = stack.new_recovery_run_identity(
        "malformed-volume",
        run_id="4" * 32,
        directory=evidence_dir,
    )
    volume_name = run_identity["database_volume_name"]
    rendered_output = inspect_output.replace("placeholder", volume_name)

    def fake_command(*command: str, **_kwargs: object) -> str:
        if command[1:3] == ("volume", "ls"):
            return volume_name
        if command == ("docker", "volume", "inspect", volume_name):
            return rendered_output
        raise AssertionError(command)

    monkeypatch.setattr(stack, "command", fake_command)

    with pytest.raises(stack.StackError, match="volume|inspect|identity|creation"):
        stack.database_volume_observation(run_identity)


@pytest.mark.parametrize(
    "created_at",
    [None, "not-a-time", "2026-07-22T12:00:00"],
)
def test_database_volume_observation_requires_aware_created_at(
    created_at: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_dir = (tmp_path / "invalid-created-at").resolve()
    monkeypatch.setenv(stack.EVIDENCE_DIR_ENV, str(evidence_dir))
    run_identity = stack.new_recovery_run_identity(
        "invalid-created-at",
        run_id="d" * 32,
        directory=evidence_dir,
    )
    volume_name = run_identity["database_volume_name"]
    project_name = stack.recovery_project_name(run_identity)

    def fake_command(*command: str, **_kwargs: object) -> str:
        if command[1:3] == ("volume", "ls"):
            return volume_name
        if command == ("docker", "volume", "inspect", volume_name):
            return json.dumps(
                [
                    {
                        "Name": volume_name,
                        "CreatedAt": created_at,
                        "Labels": {
                            "com.docker.compose.project": project_name,
                            "com.docker.compose.volume": "forwin-postgres",
                        },
                    }
                ]
            )
        raise AssertionError(command)

    monkeypatch.setattr(stack, "command", fake_command)

    with pytest.raises(stack.StackError, match="creation time"):
        stack.database_volume_observation(run_identity)


def test_database_volume_observation_rejects_missing_inspect_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_dir = (tmp_path / "missing-inspect").resolve()
    monkeypatch.setenv(stack.EVIDENCE_DIR_ENV, str(evidence_dir))
    run_identity = stack.new_recovery_run_identity(
        "missing-inspect",
        run_id="e" * 32,
        directory=evidence_dir,
    )
    volume_name = run_identity["database_volume_name"]

    def fake_command(*command: str, **_kwargs: object) -> str:
        if command[1:3] == ("volume", "ls"):
            return volume_name
        if command == ("docker", "volume", "inspect", volume_name):
            raise stack.StackError("Docker volume inspect result is missing")
        raise AssertionError(command)

    monkeypatch.setattr(stack, "command", fake_command)

    with pytest.raises(stack.StackError, match="inspect result is missing"):
        stack.database_volume_observation(run_identity)


def test_fresh_volume_rejects_created_at_before_requested_at_from_inspect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_dir = (tmp_path / "old-volume").resolve()
    monkeypatch.setenv(stack.EVIDENCE_DIR_ENV, str(evidence_dir))
    run_identity = stack.new_recovery_run_identity(
        "old-volume",
        run_id="5" * 32,
        directory=evidence_dir,
    )
    volume_name = run_identity["database_volume_name"]
    project_name = stack.recovery_project_name(run_identity)

    def fake_command(*command: str, **_kwargs: object) -> str:
        if command[1:3] == ("volume", "ls"):
            return volume_name
        if command == ("docker", "volume", "inspect", volume_name):
            return json.dumps(
                [
                    {
                        "Name": volume_name,
                        "CreatedAt": "2026-07-22T11:59:59Z",
                        "Labels": {
                            "com.docker.compose.project": project_name,
                            "com.docker.compose.volume": "forwin-postgres",
                        },
                    }
                ]
            )
        raise AssertionError(command)

    monkeypatch.setattr(stack, "command", fake_command)

    with pytest.raises(stack.StackError, match="predates fresh-up request"):
        stack.confirmed_fresh_database_volume(
            run_identity,
            requested_at="2026-07-22T12:00:00+00:00",
        )


def test_fresh_volume_accepts_docker_second_precision_created_at(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_dir = (tmp_path / "second-precision-volume").resolve()
    monkeypatch.setenv(stack.EVIDENCE_DIR_ENV, str(evidence_dir))
    run_identity = stack.new_recovery_run_identity(
        "second-precision-volume",
        run_id="7" * 32,
        directory=evidence_dir,
    )
    volume_name = run_identity["database_volume_name"]
    project_name = stack.recovery_project_name(run_identity)

    def fake_command(*command: str, **_kwargs: object) -> str:
        if command[1:3] == ("volume", "ls"):
            return volume_name
        if command == ("docker", "volume", "inspect", volume_name):
            return json.dumps(
                [
                    {
                        "Name": volume_name,
                        "CreatedAt": "2026-07-22T12:00:00Z",
                        "Labels": {
                            "com.docker.compose.project": project_name,
                            "com.docker.compose.volume": "forwin-postgres",
                        },
                    }
                ]
            )
        raise AssertionError(command)

    monkeypatch.setattr(stack, "command", fake_command)

    observation = stack.confirmed_fresh_database_volume(
        run_identity,
        requested_at="2026-07-22T12:00:00.900000+00:00",
    )

    assert observation["exists"] is True
    assert observation["created_at"] == "2026-07-22T12:00:00+00:00"


def test_volume_creation_comparison_preserves_fractional_precision() -> None:
    created_at = stack.normalized_utc_time(
        "2026-07-22T12:00:00.500000+00:00",
        field="created_at",
    )
    requested_at = stack.normalized_utc_time(
        "2026-07-22T12:00:00.900000+00:00",
        field="requested_at",
    )

    assert stack.volume_creation_predates_request(created_at, requested_at)


def test_active_run_rejects_volume_created_before_fresh_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_dir = (tmp_path / "old-event-volume").resolve()
    monkeypatch.setenv(stack.EVIDENCE_DIR_ENV, str(evidence_dir))
    run_identity = stack.new_recovery_run_identity(
        "old-event-volume",
        run_id="6" * 32,
        directory=evidence_dir,
    )
    volume_name = run_identity["database_volume_name"]
    identity = frozen_identity()
    stack.append_event(
        "fresh_up_started",
        fault_id="old-event-volume",
        requested_at="2026-07-22T12:00:00+00:00",
        identity=identity,
        run_identity=run_identity,
        database_volume={"name": volume_name, "exists": False},
    )
    created_at = "2026-07-22T11:59:59+00:00"
    stack.append_event(
        "fresh_up_completed",
        fault_id="old-event-volume",
        requested_at="2026-07-22T12:00:00+00:00",
        identity=identity,
        run_identity=run_identity,
        database_volume={
            "name": volume_name,
            "exists": True,
            "created_at": created_at,
            "fingerprint": stack.stable_hash(
                {"created_at": created_at, "name": volume_name}
            ),
        },
    )

    with pytest.raises(stack.StackError, match="predates fresh-up request"):
        stack.require_active_recovery_run("old-event-volume")


def test_active_run_accepts_docker_second_precision_created_at(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_dir = (tmp_path / "second-precision-event").resolve()
    monkeypatch.setenv(stack.EVIDENCE_DIR_ENV, str(evidence_dir))
    run_identity = stack.new_recovery_run_identity(
        "second-precision-event",
        run_id="8" * 32,
        directory=evidence_dir,
    )
    volume_name = run_identity["database_volume_name"]
    identity = frozen_identity()
    requested_at = "2026-07-22T12:00:00.900000+00:00"
    created_at = "2026-07-22T12:00:00+00:00"
    volume = {
        "name": volume_name,
        "exists": True,
        "created_at": created_at,
        "fingerprint": stack.stable_hash(
            {"created_at": created_at, "name": volume_name}
        ),
    }
    stack.append_event(
        "fresh_up_started",
        fault_id="second-precision-event",
        requested_at=requested_at,
        identity=identity,
        run_identity=run_identity,
        database_volume={"name": volume_name, "exists": False},
    )
    stack.append_event(
        "fresh_up_completed",
        fault_id="second-precision-event",
        requested_at=requested_at,
        identity=identity,
        run_identity=run_identity,
        database_volume=volume,
    )

    active = stack.require_active_recovery_run("second-precision-event")

    assert active["database_volume"] == volume


def test_fresh_up_records_stable_run_identity_and_database_volume_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_dir = (tmp_path / "fault-1").resolve()
    monkeypatch.setenv(stack.EVIDENCE_DIR_ENV, str(evidence_dir))
    monkeypatch.setattr(stack.secrets, "token_hex", lambda _size: "c" * 32)
    identity = frozen_identity()
    volume_name = "forwin-v5-recovery-" + "c" * 32 + "-postgres-data"
    absent = {"name": volume_name, "exists": False}
    present = {
        "name": volume_name,
        "exists": True,
        "created_at": "2026-07-22T11:58:30+00:00",
        "fingerprint": stack.stable_hash(
            {
                "created_at": "2026-07-22T11:58:30+00:00",
                "name": volume_name,
            }
        ),
    }
    volume_observations = iter([absent, present])
    events: list[tuple[str, dict]] = []
    compose_calls: list[tuple[dict, tuple[str, ...]]] = []

    monkeypatch.setattr(stack, "assert_frozen", lambda **_kwargs: identity)
    monkeypatch.setattr(
        stack,
        "assert_isolated_compose",
        lambda checked_identity, *, run_identity: None,
    )
    monkeypatch.setattr(stack, "require_new_evidence_run", lambda: None)
    monkeypatch.setattr(
        stack,
        "database_volume_observation",
        lambda _run_identity: next(volume_observations),
        raising=False,
    )
    monkeypatch.setattr(
        stack,
        "stack_snapshot",
        lambda **kwargs: {
            "stage": "after" if kwargs.get("probe") else "before",
            "services": {},
        },
    )
    monkeypatch.setattr(
        stack,
        "append_event",
        lambda action, **payload: events.append((action, payload)) or payload,
    )
    monkeypatch.setattr(
        stack,
        "compose",
        lambda *args, run_identity: (
            compose_calls.append((run_identity, tuple(args))) or ""
        ),
    )
    monkeypatch.setattr(
        stack,
        "wait_service",
        lambda _service, *, run_identity: {"running": True},
    )
    monkeypatch.setattr(
        stack,
        "now",
        lambda: "2026-07-22T11:58:00+00:00",
    )

    stack.fresh_up("fault-1")

    expected_run_identity = {
        "run_id": "c" * 32,
        "evidence_directory": str(evidence_dir),
        "database_volume_name": volume_name,
    }
    assert events[0] == (
        "fresh_up_started",
        {
            "fault_id": "fault-1",
            "requested_at": "2026-07-22T11:58:00+00:00",
            "identity": identity,
            "run_identity": expected_run_identity,
            "database_volume": absent,
            "before": {"stage": "before", "services": {}},
        },
    )
    assert events[-1][0] == "fresh_up_completed"
    assert events[-1][1]["run_identity"] == expected_run_identity
    assert events[-1][1]["database_volume"] == present
    assert all(
        run_identity == expected_run_identity
        for run_identity, _args in compose_calls
    )


@pytest.mark.parametrize(
    "failure_stage",
    [
        "initial_down",
        "dependency_up",
        "dependency_readiness",
        "migration",
        "application_up",
        "application_readiness",
        "functional_probe",
        "database_volume_postcondition",
    ],
)
def test_fresh_up_failure_cleans_resources_and_writes_terminal_setup_blocked(
    failure_stage: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_dir = (tmp_path / failure_stage).resolve()
    monkeypatch.setenv(stack.EVIDENCE_DIR_ENV, str(evidence_dir))
    monkeypatch.setattr(stack.secrets, "token_hex", lambda _size: "8" * 32)
    identity = frozen_identity()
    volume_name = "forwin-v5-recovery-" + "8" * 32 + "-postgres-data"
    absent = {"name": volume_name, "exists": False}
    present = {
        "name": volume_name,
        "exists": True,
        "created_at": "2026-07-22T12:00:01+00:00",
        "fingerprint": stack.stable_hash(
            {
                "created_at": "2026-07-22T12:00:01+00:00",
                "name": volume_name,
            }
        ),
    }
    state = {
        "volume_calls": 0,
        "cleanup_started": False,
    }
    cleanup_calls: list[
        tuple[tuple[str, ...], dict[str, object]]
    ] = []

    def fake_volume_observation(
        _run_identity: dict,
        **_kwargs: object,
    ) -> dict:
        state["volume_calls"] += 1
        if state["cleanup_started"]:
            return absent
        if state["volume_calls"] == 1:
            return absent
        if failure_stage == "database_volume_postcondition":
            raise stack.StackError("volume postcondition failed")
        return present

    def fake_compose(*args: str, **_kwargs: object) -> str:
        if failure_stage == "initial_down" and args[:1] == ("down",):
            raise stack.StackError("initial down failed")
        if (
            failure_stage == "dependency_up"
            and args[:2] == ("up", "--detach")
            and "postgres" in args
        ):
            raise stack.StackError("dependency up failed")
        if failure_stage == "migration" and args[:1] == ("run",):
            raise stack.StackError("migration failed")
        if (
            failure_stage == "application_up"
            and args[:2] == ("up", "--detach")
            and "forwin" in args
        ):
            raise stack.StackError("application up failed")
        return ""

    def fake_wait_service(service: str, **_kwargs: object) -> dict:
        if failure_stage == "dependency_readiness" and service == "postgres":
            raise stack.StackError("dependency readiness failed")
        if failure_stage == "application_readiness" and service == "forwin":
            raise stack.StackError("application readiness failed")
        return {"running": True}

    def fake_stack_snapshot(**kwargs: object) -> dict:
        if failure_stage == "functional_probe" and kwargs.get("probe"):
            raise stack.StackError("functional probe failed")
        return {
            "stage": "after" if kwargs.get("probe") else "before",
            "services": {},
        }

    def fake_compose_process(
        *args: str,
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        cleanup_calls.append((tuple(args), kwargs))
        state["cleanup_started"] = True
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(stack, "assert_frozen", lambda **_kwargs: identity)
    monkeypatch.setattr(
        stack,
        "assert_isolated_compose",
        lambda _identity, *, run_identity: None,
    )
    monkeypatch.setattr(stack, "database_volume_observation", fake_volume_observation)
    monkeypatch.setattr(stack, "compose", fake_compose)
    monkeypatch.setattr(stack, "compose_process", fake_compose_process)
    monkeypatch.setattr(stack, "wait_service", fake_wait_service)
    monkeypatch.setattr(stack, "stack_snapshot", fake_stack_snapshot)
    monkeypatch.setattr(
        stack,
        "destroyed_service_inventory",
        lambda _run_identity, **_kwargs: {
            service: {"exists": False, "running": False}
            for service in stack.SERVICES
        },
    )
    monkeypatch.setattr(
        stack,
        "now",
        lambda: "2026-07-22T12:00:00+00:00",
    )

    with pytest.raises(stack.StackError, match=failure_stage):
        stack.fresh_up("fault-1")

    events = stack.load_verified_events()
    assert [event["action"] for event in events] == [
        "fresh_up_started",
        "setup_blocked",
    ]
    blocked = events[-1]
    assert blocked["failure_stage"] == failure_stage
    assert blocked["failure_reason"]
    assert blocked["cleanup_requested_at"]
    assert blocked["cleanup_confirmed_at"]
    assert blocked["cleanup_error"] is None
    assert blocked["database_volume"] == absent
    assert blocked["after"]["services"] == {
        service: {"exists": False, "running": False}
        for service in stack.SERVICES
    }
    finalizer_violations, _run_summary = finalizer.run_resource_violations(
        "publisher_captcha",
        fault_id="fault-1",
        events=events,
        event_path=stack.events_path(),
        artifact_paths={},
    )
    assert finalizer_violations == [
        "publisher_captcha.setup_blocked cannot pass",
        "publisher_captcha.primary fault/recovery cardinality mismatch",
        "publisher_captcha.database volume lifecycle mismatch",
    ]
    assert len(cleanup_calls) == 1
    cleanup_args, cleanup_kwargs = cleanup_calls[0]
    assert cleanup_args == ("down", "--volumes", "--remove-orphans")
    assert cleanup_kwargs["run_identity"] == blocked["run_identity"]
    assert cleanup_kwargs["timeout_stage"] == "teardown command"
    assert 0 < (
        cleanup_kwargs["deadline"] - stack.time.monotonic()
    ) <= stack.CLEANUP_TIMEOUT_SECONDS
    with pytest.raises(stack.StackError, match="terminal"):
        stack.append_event("snapshot", label="retry-not-allowed")


@pytest.mark.parametrize("interrupt_type", (KeyboardInterrupt, SystemExit))
def test_fresh_up_interrupt_cleans_and_reraises_without_setup_blocked(
    interrupt_type: type[BaseException],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_dir = (tmp_path / interrupt_type.__name__).resolve()
    monkeypatch.setenv(stack.EVIDENCE_DIR_ENV, str(evidence_dir))
    monkeypatch.setattr(stack.secrets, "token_hex", lambda _size: "6" * 32)
    identity = frozen_identity()
    volume_name = "forwin-v5-recovery-" + "6" * 32 + "-postgres-data"
    absent = {"name": volume_name, "exists": False}
    cleanup_calls: list[dict] = []
    monkeypatch.setattr(stack, "assert_frozen", lambda **_kwargs: identity)
    monkeypatch.setattr(
        stack,
        "assert_isolated_compose",
        lambda _identity, *, run_identity: None,
    )
    monkeypatch.setattr(
        stack,
        "database_volume_observation",
        lambda _run_identity, **_kwargs: absent,
    )
    monkeypatch.setattr(
        stack,
        "stack_snapshot",
        lambda **_kwargs: {"services": {}},
    )
    monkeypatch.setattr(
        stack,
        "compose",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(interrupt_type()),
    )

    def cleanup(
        run_identity: dict,
        *,
        fallback_timestamp: str,
    ) -> dict:
        cleanup_calls.append(run_identity)
        return {
            "cleanup_requested_at": fallback_timestamp,
            "cleanup_confirmed_at": fallback_timestamp,
            "cleanup_error": None,
            "database_volume": absent,
            "after": {
                "observed_at": fallback_timestamp,
                "services": {
                    service: {"exists": False, "running": False}
                    for service in stack.SERVICES
                },
            },
        }

    monkeypatch.setattr(stack, "cleanup_recovery_run", cleanup)
    monkeypatch.setattr(
        stack,
        "now",
        lambda: "2026-07-22T12:00:00+00:00",
    )

    with pytest.raises(interrupt_type):
        stack.fresh_up("fault-interrupt-generalized")

    events = stack.load_verified_events()
    assert [event["action"] for event in events] == [
        "fresh_up_started",
        "interrupted_cleanup",
    ]
    assert cleanup_calls == [events[0]["run_identity"]]
    assert events[-1]["cleanup_confirmed"] is True
    assert not {
        "setup_blocked",
        "fault_marked",
        "recovery_marked",
        "fault_service_stopped",
        "fault_service_killed",
        "fault_service_recovered",
    }.intersection(event["action"] for event in events)


@pytest.mark.parametrize(
    ("boundary", "interruption", "partial_shape"),
    (
        (
            "startup",
            KeyboardInterrupt("startup interrupted"),
            "volume-removed-services-remain",
        ),
        (
            "post-completion",
            SystemExit(19),
            "services-removed-volume-remains",
        ),
    ),
)
def test_fresh_up_partial_interrupt_cleanup_stays_nonterminal_and_preserves_signal(
    boundary: str,
    interruption: BaseException,
    partial_shape: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_dir = (tmp_path / boundary).resolve()
    monkeypatch.setenv(stack.EVIDENCE_DIR_ENV, str(evidence_dir))
    monkeypatch.setattr(stack.secrets, "token_hex", lambda _size: "a" * 32)
    identity = frozen_identity()
    volume_name = "forwin-v5-recovery-" + "a" * 32 + "-postgres-data"
    absent = {"name": volume_name, "exists": False}
    created_at = "2026-07-22T12:00:00+00:00"
    present = {
        "name": volume_name,
        "exists": True,
        "created_at": created_at,
        "fingerprint": stack.stable_hash(
            {"created_at": created_at, "name": volume_name}
        ),
    }
    absent_services = {
        service: {"exists": False, "running": False}
        for service in stack.SERVICES
    }
    partial_services = copy.deepcopy(absent_services)
    partial_volume = absent
    if partial_shape == "volume-removed-services-remain":
        partial_services["postgres"] = {"exists": True, "running": False}
    else:
        partial_volume = present
    cleanup = {
        "cleanup_requested_at": "2026-07-22T12:00:01+00:00",
        "cleanup_confirmed_at": None,
        "cleanup_error": "partial cleanup remains",
        "database_volume": partial_volume,
        "after": {
            "observed_at": "2026-07-22T12:00:02+00:00",
            "services": partial_services,
        },
    }

    monkeypatch.setattr(stack, "assert_frozen", lambda **_kwargs: identity)
    monkeypatch.setattr(
        stack,
        "assert_isolated_compose",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        stack,
        "database_volume_observation",
        lambda *_args, **_kwargs: absent,
    )
    monkeypatch.setattr(
        stack,
        "confirmed_fresh_database_volume",
        lambda *_args, **_kwargs: present,
    )
    monkeypatch.setattr(
        stack,
        "initialize_recovery_sentinel",
        lambda **_kwargs: {
            "table": "forwin_recovery_run_sentinel",
            "sentinel_id": "b" * 64,
            "run_id": "a" * 32,
            "fault_id": f"fault-{boundary}",
            "source_sha": SOURCE_SHA,
        },
    )
    monkeypatch.setattr(
        stack,
        "stack_snapshot",
        lambda **_kwargs: {"services": {}},
    )
    monkeypatch.setattr(
        stack,
        "wait_service",
        lambda service, **_kwargs: {
            "service": service,
            "exists": True,
            "running": True,
        },
    )
    monkeypatch.setattr(
        stack,
        "cleanup_recovery_run",
        lambda *_args, **_kwargs: copy.deepcopy(cleanup),
    )
    monkeypatch.setattr(
        stack,
        "now",
        lambda: "2026-07-22T12:00:00+00:00",
    )
    if boundary == "startup":
        monkeypatch.setattr(
            stack,
            "compose",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(interruption),
        )
    else:
        monkeypatch.setattr(stack, "compose", lambda *_args, **_kwargs: "")
        monkeypatch.setattr(
            stack,
            "print",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(interruption),
            raising=False,
        )

    with pytest.raises(type(interruption)) as captured:
        stack.fresh_up(f"fault-{boundary}")

    assert captured.value is interruption
    expected_actions = ["fresh_up_started"]
    if boundary == "post-completion":
        expected_actions.append("fresh_up_completed")
    assert [
        event["action"] for event in stack.load_verified_events()
    ] == expected_actions


def interrupt_namespace_container(
    run_identity: dict,
    service: str,
    *,
    container_id: str | None = None,
    image_id: str | None = None,
    name: str | None = None,
    project_label: str | None = None,
    service_label: str | None = None,
    include_service_label: bool = True,
) -> dict:
    project_name = (
        f"forwin-v5-recovery-{run_identity['run_id']}"
    )
    expected_name = (
        f"{project_name}-{TEST_CONTAINER_SUFFIXES[service]}"
        if service in TEST_CONTAINER_SUFFIXES
        else f"{project_name}-{service}"
    )
    labels = {
        "com.docker.compose.project": (
            project_name if project_label is None else project_label
        ),
    }
    if include_service_label:
        labels["com.docker.compose.service"] = (
            service if service_label is None else service_label
        )
    return {
        "Id": container_id or f"container-{service}",
        "Image": image_id or f"sha256:image-{service}",
        "Name": f"/{name or expected_name}",
        "Config": {"Labels": labels},
        "State": {"Running": True},
    }


def install_interrupt_namespace_docker(
    monkeypatch: pytest.MonkeyPatch,
    run_identity: dict,
    containers: list[dict],
    *,
    malformed: str | None = None,
) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []
    containers_by_id = {
        container["Id"]: copy.deepcopy(container)
        for container in containers
    }
    project_name = (
        f"forwin-v5-recovery-{run_identity['run_id']}"
    )

    def fake_command(*args: str, **_kwargs: object) -> str:
        calls.append(tuple(args))
        if args[:5] == (
            "docker",
            "container",
            "ls",
            "--all",
            "--no-trunc",
        ):
            if malformed == "list-json":
                return "{not-json"
            selector = args[args.index("--filter") + 1]
            if selector == (
                "label=com.docker.compose.project="
                f"{project_name}"
            ):
                selected = [
                    container
                    for container in containers
                    if (
                        (container.get("Config") or {})
                        .get("Labels", {})
                        .get("com.docker.compose.project")
                        == project_name
                    )
                ]
            elif selector.startswith("name=^") and selector.endswith("$"):
                selected_name = selector.removeprefix("name=^").removesuffix(
                    "$"
                )
                selected = [
                    container
                    for container in containers
                    if container.get("Name") == f"/{selected_name}"
                ]
            else:
                raise AssertionError(f"unexpected Docker selector: {selector}")
            return "\n".join(
                json.dumps({"ID": container["Id"]})
                for container in selected
            )
        if args[:3] == ("docker", "container", "inspect"):
            if malformed == "inspect-json":
                return "[not-json"
            if malformed == "inspect-shape":
                return json.dumps({"Id": args[3]})
            return json.dumps(
                [containers_by_id[container_id] for container_id in args[3:]]
            )
        raise AssertionError(f"unexpected Docker command: {args}")

    monkeypatch.setattr(stack, "command", fake_command)
    return calls


def confirmed_interrupt_cleanup(run_identity: dict) -> dict:
    timestamp = "2026-07-22T12:02:01+00:00"
    return {
        "cleanup_requested_at": "2026-07-22T12:02:00+00:00",
        "cleanup_confirmed_at": timestamp,
        "cleanup_error": None,
        "database_volume": {
            "name": run_identity["database_volume_name"],
            "exists": False,
        },
        "after": {
            "observed_at": timestamp,
            "services": {
                service: {"exists": False, "running": False}
                for service in stack.SERVICES
            },
        },
    }


def interrupt_cleanup_prefix(
    stack_module: object,
    *,
    fault_id: str,
    run_id: str,
    evidence_dir: Path,
    completed: bool,
) -> tuple[dict, dict, dict]:
    run_identity = stack_module.new_recovery_run_identity(
        fault_id,
        run_id=run_id,
        directory=evidence_dir,
    )
    identity = frozen_identity()
    volume_name = run_identity["database_volume_name"]
    absent = {"name": volume_name, "exists": False}
    requested_at = "2026-07-22T12:00:00+00:00"
    stack_module.append_event(
        "fresh_up_started",
        fault_id=fault_id,
        requested_at=requested_at,
        identity=identity,
        run_identity=run_identity,
        database_volume=absent,
    )
    created_at = "2026-07-22T12:00:01+00:00"
    present = {
        "name": volume_name,
        "exists": True,
        "created_at": created_at,
        "fingerprint": stack_module.stable_hash(
            {"created_at": created_at, "name": volume_name}
        ),
    }
    if completed:
        project_name = f"forwin-v5-recovery-{run_id}"
        completed_services = {
            service: {
                "service": service,
                "exists": True,
                "container_id": f"container-{service}",
                "name": (
                    f"{project_name}-{TEST_CONTAINER_SUFFIXES[service]}"
                ),
                "image_id": f"sha256:image-{service}",
                "running": True,
            }
            for service in TEST_CONTAINER_SUFFIXES
        }
        stack_module.append_event(
            "fresh_up_completed",
            fault_id=fault_id,
            requested_at=requested_at,
            identity=identity,
            run_identity=run_identity,
            database_volume=present,
            sentinel={
                "table": "forwin_recovery_run_sentinel",
                "sentinel_id": "c" * 64,
                "run_id": run_id,
                "fault_id": fault_id,
                "source_sha": SOURCE_SHA,
            },
            after={"services": completed_services},
        )
    return run_identity, identity, present


def reseal_stack_events(stack_module: object, events: list[dict]) -> None:
    previous = "0" * 64
    for event in events:
        event["previous_event_sha256"] = previous
        event["event_sha256"] = stack_module.event_hash(event)
        previous = event["event_sha256"]
    stack_module.events_path().write_text(
        "\n".join(json.dumps(event, sort_keys=True) for event in events)
        + "\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize("completed", (False, True))
@pytest.mark.parametrize(
    "partial_shape",
    (
        "volume-removed-services-remain",
        "services-removed-volume-remains",
    ),
)
def test_interrupt_cleanup_retries_partial_exact_run_until_one_terminal_event(
    completed: bool,
    partial_shape: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fault_id = f"retry-{partial_shape}-{completed}"
    evidence_dir = (tmp_path / fault_id).resolve()
    monkeypatch.setenv(stack.EVIDENCE_DIR_ENV, str(evidence_dir))
    run_identity, identity, present = interrupt_cleanup_prefix(
        stack,
        fault_id=fault_id,
        run_id=("d" if completed else "e") * 32,
        evidence_dir=evidence_dir,
        completed=completed,
    )
    absent = {
        "name": run_identity["database_volume_name"],
        "exists": False,
    }
    absent_services = {
        service: {"exists": False, "running": False}
        for service in stack.SERVICES
    }
    partial_services = copy.deepcopy(absent_services)
    partial_volume = absent
    live_volume = absent
    if partial_shape == "volume-removed-services-remain":
        partial_services["publisher-browser"] = {
            "exists": True,
            "running": False,
        }
    else:
        partial_volume = present
        live_volume = present
    partial = {
        "cleanup_requested_at": "2026-07-22T12:01:00+00:00",
        "cleanup_confirmed_at": None,
        "cleanup_error": "exact run still has resources",
        "database_volume": partial_volume,
        "after": {
            "observed_at": "2026-07-22T12:01:01+00:00",
            "services": partial_services,
        },
    }
    confirmed = {
        "cleanup_requested_at": "2026-07-22T12:02:00+00:00",
        "cleanup_confirmed_at": "2026-07-22T12:02:01+00:00",
        "cleanup_error": None,
        "database_volume": absent,
        "after": {
            "observed_at": "2026-07-22T12:02:01+00:00",
            "services": absent_services,
        },
    }
    cleanup_results = iter((partial, confirmed))
    cleanup_calls: list[dict] = []
    live_services = (
        ("publisher-browser",)
        if partial_shape == "volume-removed-services-remain"
        else ()
    )
    install_interrupt_namespace_docker(
        monkeypatch,
        run_identity,
        [
            interrupt_namespace_container(run_identity, service)
            for service in live_services
        ],
    )

    def cleanup(
        observed_run_identity: dict,
        *,
        fallback_timestamp: str,
    ) -> dict:
        assert fallback_timestamp
        cleanup_calls.append(copy.deepcopy(observed_run_identity))
        return copy.deepcopy(next(cleanup_results))

    monkeypatch.setattr(stack, "assert_frozen", lambda **_kwargs: identity)
    monkeypatch.setattr(
        stack,
        "assert_isolated_compose",
        lambda _identity, *, run_identity: None,
    )
    monkeypatch.setattr(
        stack,
        "database_volume_observation",
        lambda _run_identity, **_kwargs: copy.deepcopy(live_volume),
    )
    monkeypatch.setattr(stack, "cleanup_recovery_run", cleanup)
    monkeypatch.setattr(
        stack,
        "now",
        lambda: "2026-07-22T12:00:30+00:00",
    )

    with pytest.raises(
        stack.StackError,
        match="did not confirm terminal resource removal",
    ):
        stack.interrupt_cleanup_recovery_run(fault_id)

    prefix_actions = ["fresh_up_started"]
    if completed:
        prefix_actions.append("fresh_up_completed")
    assert [
        event["action"] for event in stack.load_verified_events()
    ] == prefix_actions

    terminal = stack.interrupt_cleanup_recovery_run(fault_id)

    assert terminal["action"] == "interrupted_cleanup"
    assert terminal["cleanup_confirmed"] is True
    assert terminal["database_volume"] == absent
    assert terminal["after"]["services"] == absent_services
    assert [
        event["action"] for event in stack.load_verified_events()
    ] == [*prefix_actions, "interrupted_cleanup"]
    assert cleanup_calls == [run_identity, run_identity]

    with pytest.raises(stack.StackError, match="terminal"):
        stack.interrupt_cleanup_recovery_run(fault_id)
    assert cleanup_calls == [run_identity, run_identity]
    assert sum(
        event["action"] == "interrupted_cleanup"
        for event in stack.load_verified_events()
    ) == 1


@pytest.mark.parametrize(
    ("rejection", "expected_error"),
    (
        ("foreign-same-project-orphan", "service label"),
        ("unknown-service", "unknown service"),
        ("duplicate-service", "duplicate.*postgres"),
        ("wrong-project-name-squatter", "project label"),
        ("service-name-mismatch", "container name"),
    ),
)
def test_interrupt_cleanup_rejects_unproved_live_namespace_before_teardown(
    rejection: str,
    expected_error: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fault_id = f"namespace-reject-{rejection}"
    evidence_dir = (tmp_path / fault_id).resolve()
    monkeypatch.setenv(stack.EVIDENCE_DIR_ENV, str(evidence_dir))
    run_identity, identity, _present = interrupt_cleanup_prefix(
        stack,
        fault_id=fault_id,
        run_id="1" * 32,
        evidence_dir=evidence_dir,
        completed=False,
    )
    project_name = f"forwin-v5-recovery-{'1' * 32}"
    postgres_name = f"{project_name}-postgres"
    qdrant_name = f"{project_name}-qdrant"
    if rejection == "foreign-same-project-orphan":
        containers = [
            interrupt_namespace_container(
                run_identity,
                "retired-worker",
                name=f"{project_name}-retired-worker",
                include_service_label=False,
            )
        ]
    elif rejection == "unknown-service":
        containers = [
            interrupt_namespace_container(
                run_identity,
                "retired-worker",
                name=f"{project_name}-retired-worker",
            )
        ]
    elif rejection == "duplicate-service":
        containers = [
            interrupt_namespace_container(run_identity, "postgres"),
            interrupt_namespace_container(
                run_identity,
                "postgres",
                container_id="container-postgres-duplicate",
                name=f"{postgres_name}-duplicate",
            ),
        ]
    elif rejection == "wrong-project-name-squatter":
        containers = [
            interrupt_namespace_container(
                run_identity,
                "postgres",
                name=postgres_name,
                project_label="foreign-compose-project",
            )
        ]
    else:
        containers = [
            interrupt_namespace_container(
                run_identity,
                "postgres",
                name=qdrant_name,
            )
        ]
    install_interrupt_namespace_docker(
        monkeypatch,
        run_identity,
        containers,
    )
    cleanup_calls: list[dict] = []
    monkeypatch.setattr(stack, "assert_frozen", lambda **_kwargs: identity)
    monkeypatch.setattr(
        stack,
        "assert_isolated_compose",
        lambda _identity, *, run_identity: None,
    )
    monkeypatch.setattr(
        stack,
        "database_volume_observation",
        lambda _run_identity, **_kwargs: {
            "name": run_identity["database_volume_name"],
            "exists": False,
        },
    )
    monkeypatch.setattr(
        stack,
        "cleanup_recovery_run",
        lambda observed, **_kwargs: (
            cleanup_calls.append(copy.deepcopy(observed))
            or confirmed_interrupt_cleanup(run_identity)
        ),
    )

    with pytest.raises(stack.StackError, match=expected_error):
        stack.interrupt_cleanup_recovery_run(fault_id)

    assert cleanup_calls == []
    assert [
        event["action"] for event in stack.load_verified_events()
    ] == ["fresh_up_started"]


@pytest.mark.parametrize("drift", ("container", "image"))
def test_interrupt_cleanup_rejects_completed_container_identity_drift_before_teardown(
    drift: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fault_id = f"completed-{drift}-drift"
    evidence_dir = (tmp_path / fault_id).resolve()
    monkeypatch.setenv(stack.EVIDENCE_DIR_ENV, str(evidence_dir))
    run_identity, identity, present = interrupt_cleanup_prefix(
        stack,
        fault_id=fault_id,
        run_id="2" * 32,
        evidence_dir=evidence_dir,
        completed=True,
    )
    container = interrupt_namespace_container(run_identity, "postgres")
    if drift == "container":
        container["Id"] = "replacement-postgres-container"
    else:
        container["Image"] = "sha256:replacement-postgres-image"
    install_interrupt_namespace_docker(
        monkeypatch,
        run_identity,
        [container],
    )
    cleanup_calls: list[dict] = []
    monkeypatch.setattr(stack, "assert_frozen", lambda **_kwargs: identity)
    monkeypatch.setattr(
        stack,
        "assert_isolated_compose",
        lambda _identity, *, run_identity: None,
    )
    monkeypatch.setattr(
        stack,
        "database_volume_observation",
        lambda _run_identity, **_kwargs: copy.deepcopy(present),
    )
    monkeypatch.setattr(
        stack,
        "cleanup_recovery_run",
        lambda observed, **_kwargs: (
            cleanup_calls.append(copy.deepcopy(observed))
            or confirmed_interrupt_cleanup(run_identity)
        ),
    )

    with pytest.raises(stack.StackError, match=f"{drift} identity drift"):
        stack.interrupt_cleanup_recovery_run(fault_id)

    assert cleanup_calls == []
    assert [
        event["action"] for event in stack.load_verified_events()
    ] == ["fresh_up_started", "fresh_up_completed"]


@pytest.mark.parametrize(
    "malformed",
    ("list-json", "inspect-json", "inspect-shape"),
)
def test_interrupt_cleanup_fails_closed_on_malformed_docker_namespace_json(
    malformed: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fault_id = f"malformed-namespace-{malformed}"
    evidence_dir = (tmp_path / fault_id).resolve()
    monkeypatch.setenv(stack.EVIDENCE_DIR_ENV, str(evidence_dir))
    run_identity, identity, _present = interrupt_cleanup_prefix(
        stack,
        fault_id=fault_id,
        run_id="3" * 32,
        evidence_dir=evidence_dir,
        completed=False,
    )
    install_interrupt_namespace_docker(
        monkeypatch,
        run_identity,
        [interrupt_namespace_container(run_identity, "postgres")],
        malformed=malformed,
    )
    cleanup_calls: list[dict] = []
    monkeypatch.setattr(stack, "assert_frozen", lambda **_kwargs: identity)
    monkeypatch.setattr(
        stack,
        "assert_isolated_compose",
        lambda _identity, *, run_identity: None,
    )
    monkeypatch.setattr(
        stack,
        "database_volume_observation",
        lambda _run_identity, **_kwargs: {
            "name": run_identity["database_volume_name"],
            "exists": False,
        },
    )
    monkeypatch.setattr(
        stack,
        "cleanup_recovery_run",
        lambda observed, **_kwargs: (
            cleanup_calls.append(copy.deepcopy(observed))
            or confirmed_interrupt_cleanup(run_identity)
        ),
    )

    with pytest.raises(stack.StackError):
        stack.interrupt_cleanup_recovery_run(fault_id)

    assert cleanup_calls == []
    assert [
        event["action"] for event in stack.load_verified_events()
    ] == ["fresh_up_started"]


@pytest.mark.parametrize(
    "present_services",
    (
        (),
        ("postgres",),
        ("postgres", "minio", "forwin-mcp", "publisher-browser"),
    ),
)
def test_interrupt_cleanup_accepts_legitimate_start_only_service_subsets(
    present_services: tuple[str, ...],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subset_name = "none" if not present_services else "-".join(present_services)
    fault_id = f"legitimate-start-subset-{subset_name}"
    evidence_dir = (tmp_path / fault_id).resolve()
    monkeypatch.setenv(stack.EVIDENCE_DIR_ENV, str(evidence_dir))
    run_identity, identity, _present = interrupt_cleanup_prefix(
        stack,
        fault_id=fault_id,
        run_id="4" * 32,
        evidence_dir=evidence_dir,
        completed=False,
    )
    docker_calls = install_interrupt_namespace_docker(
        monkeypatch,
        run_identity,
        [
            interrupt_namespace_container(run_identity, service)
            for service in present_services
        ],
    )
    cleanup_calls: list[dict] = []
    monkeypatch.setattr(stack, "assert_frozen", lambda **_kwargs: identity)
    monkeypatch.setattr(
        stack,
        "assert_isolated_compose",
        lambda _identity, *, run_identity: None,
    )
    monkeypatch.setattr(
        stack,
        "database_volume_observation",
        lambda _run_identity, **_kwargs: {
            "name": run_identity["database_volume_name"],
            "exists": False,
        },
    )
    monkeypatch.setattr(
        stack,
        "cleanup_recovery_run",
        lambda observed, **_kwargs: (
            cleanup_calls.append(copy.deepcopy(observed))
            or confirmed_interrupt_cleanup(run_identity)
        ),
    )

    terminal = stack.interrupt_cleanup_recovery_run(fault_id)

    assert terminal["action"] == "interrupted_cleanup"
    assert cleanup_calls == [run_identity]
    project_name = f"forwin-v5-recovery-{'4' * 32}"
    selectors = [
        call[call.index("--filter") + 1]
        for call in docker_calls
        if call[:5]
        == ("docker", "container", "ls", "--all", "--no-trunc")
    ]
    assert set(selectors) == {
        (
            "label=com.docker.compose.project="
            f"{project_name}"
        ),
        *{
            f"name=^{project_name}-{suffix}$"
            for suffix in TEST_CONTAINER_SUFFIXES.values()
        },
    }
    assert len(selectors) == 1 + len(TEST_CONTAINER_SUFFIXES)
    assert [
        event["action"] for event in stack.load_verified_events()
    ] == ["fresh_up_started", "interrupted_cleanup"]


@pytest.mark.parametrize(
    "rejection",
    (
        "fault",
        "identity",
        "compose-project",
        "foreign-volume",
        "terminal",
    ),
)
def test_interrupt_cleanup_rejects_nonmatching_or_terminal_run(
    rejection: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fault_id = f"interrupt-reject-{rejection}"
    evidence_dir = (tmp_path / fault_id).resolve()
    monkeypatch.setenv(stack.EVIDENCE_DIR_ENV, str(evidence_dir))
    run_identity, identity, present = interrupt_cleanup_prefix(
        stack,
        fault_id=fault_id,
        run_id="f" * 32,
        evidence_dir=evidence_dir,
        completed=rejection != "foreign-volume",
    )
    if rejection == "identity":
        events = stack.load_verified_events()
        events[-1]["identity"] = {"source_sha": "0" * 40}
        reseal_stack_events(stack, events)
    if rejection == "terminal":
        absent = {
            "name": run_identity["database_volume_name"],
            "exists": False,
        }
        absent_services = {
            service: {"exists": False, "running": False}
            for service in stack.SERVICES
        }
        stack.append_event(
            "interrupted_cleanup",
            fault_id=fault_id,
            requested_at="2026-07-22T12:01:00+00:00",
            identity=identity,
            run_identity=run_identity,
            database_volume_before=present,
            database_volume=absent,
            cleanup_requested_at="2026-07-22T12:01:00+00:00",
            cleanup_confirmed_at="2026-07-22T12:01:01+00:00",
            cleanup_confirmed=True,
            cleanup_error=None,
            after={
                "observed_at": "2026-07-22T12:01:01+00:00",
                "services": absent_services,
            },
        )
    cleanup_called: list[bool] = []
    volume_observed: list[bool] = []
    monkeypatch.setattr(stack, "assert_frozen", lambda **_kwargs: identity)

    def isolated(
        _identity: dict,
        *,
        run_identity: dict,
    ) -> None:
        if rejection == "compose-project":
            raise stack.StackError("different Compose project")

    monkeypatch.setattr(stack, "assert_isolated_compose", isolated)

    def volume_observation(
        _run_identity: dict,
        **_kwargs: object,
    ) -> dict:
        volume_observed.append(True)
        if rejection == "foreign-volume":
            raise stack.StackError("foreign database volume")
        return copy.deepcopy(present)

    monkeypatch.setattr(
        stack,
        "database_volume_observation",
        volume_observation,
    )
    monkeypatch.setattr(
        stack,
        "cleanup_recovery_run",
        lambda *_args, **_kwargs: cleanup_called.append(True) or {},
    )

    requested_fault = "different-fault" if rejection == "fault" else fault_id
    with pytest.raises(stack.StackError):
        stack.interrupt_cleanup_recovery_run(requested_fault)

    assert cleanup_called == []
    if rejection == "foreign-volume":
        assert volume_observed == [True]
    assert sum(
        event["action"] == "interrupted_cleanup"
        for event in stack.load_verified_events()
    ) == (1 if rejection == "terminal" else 0)


@pytest.mark.parametrize(
    ("boundary", "expected_stage"),
    (
        ("serialization", "response_serialization"),
        ("print", "response_print"),
    ),
)
def test_fresh_up_response_boundary_interrupt_is_terminal_and_propagates(
    boundary: str,
    expected_stage: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_dir = (tmp_path / boundary).resolve()
    monkeypatch.setenv(stack.EVIDENCE_DIR_ENV, str(evidence_dir))
    monkeypatch.setattr(stack.secrets, "token_hex", lambda _size: "8" * 32)
    identity = frozen_identity()
    volume_name = "forwin-v5-recovery-" + "8" * 32 + "-postgres-data"
    absent = {"name": volume_name, "exists": False}
    present = {
        "name": volume_name,
        "exists": True,
        "created_at": "2026-07-22T12:00:00+00:00",
        "fingerprint": stack.stable_hash(
            {
                "created_at": "2026-07-22T12:00:00+00:00",
                "name": volume_name,
            }
        ),
    }
    destroyed = {
        service: {"exists": False, "running": False}
        for service in stack.SERVICES
    }
    monkeypatch.setattr(stack, "assert_frozen", lambda **_kwargs: identity)
    monkeypatch.setattr(
        stack,
        "assert_isolated_compose",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        stack,
        "database_volume_observation",
        lambda *_args, **_kwargs: absent,
    )
    monkeypatch.setattr(
        stack,
        "confirmed_fresh_database_volume",
        lambda *_args, **_kwargs: present,
    )
    monkeypatch.setattr(
        stack,
        "initialize_recovery_sentinel",
        lambda **_kwargs: {
            "table": "forwin_recovery_run_sentinel",
            "sentinel_id": "f" * 64,
            "run_id": "8" * 32,
            "fault_id": "fault-response-boundary",
            "source_sha": SOURCE_SHA,
        },
    )
    monkeypatch.setattr(stack, "compose", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(
        stack,
        "wait_service",
        lambda service, **_kwargs: {
            "service": service,
            "exists": True,
            "running": True,
        },
    )
    monkeypatch.setattr(
        stack,
        "stack_snapshot",
        lambda **_kwargs: {"services": {}},
    )
    monkeypatch.setattr(
        stack,
        "cleanup_recovery_run",
        lambda _run_identity, *, fallback_timestamp: {
            "cleanup_requested_at": fallback_timestamp,
            "cleanup_confirmed_at": fallback_timestamp,
            "cleanup_error": None,
            "database_volume": absent,
            "after": {
                "observed_at": fallback_timestamp,
                "services": destroyed,
            },
        },
    )
    monkeypatch.setattr(
        stack,
        "now",
        lambda: "2026-07-22T12:00:00+00:00",
    )
    if boundary == "serialization":
        real_dumps = stack.json.dumps

        def interrupt_response_serialization(
            value: object, **kwargs: object
        ) -> str:
            if kwargs.get("indent") == 2:
                raise KeyboardInterrupt
            return real_dumps(value, **kwargs)

        monkeypatch.setattr(
            stack.json,
            "dumps",
            interrupt_response_serialization,
        )
    else:
        monkeypatch.setattr(
            stack,
            "print",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                KeyboardInterrupt()
            ),
            raising=False,
        )

    with pytest.raises(KeyboardInterrupt):
        stack.fresh_up("fault-response-boundary")

    events = stack.load_verified_events()
    assert [event["action"] for event in events] == [
        "fresh_up_started",
        "fresh_up_completed",
        "interrupted_cleanup",
    ]
    assert events[-1]["interrupted_stage"] == expected_stage
    assert events[-1]["cleanup_confirmed"] is True
    assert events[-1]["database_volume"] == absent
    assert not any(
        event["action"] == "setup_blocked" for event in events
    )


def test_fresh_up_cleanup_interruption_preserves_original_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_dir = (tmp_path / "cleanup-interruption").resolve()
    monkeypatch.setenv(stack.EVIDENCE_DIR_ENV, str(evidence_dir))
    monkeypatch.setattr(stack.secrets, "token_hex", lambda _size: "7" * 32)
    identity = frozen_identity()
    volume_name = "forwin-v5-recovery-" + "7" * 32 + "-postgres-data"
    absent = {"name": volume_name, "exists": False}
    monkeypatch.setattr(stack, "assert_frozen", lambda **_kwargs: identity)
    monkeypatch.setattr(
        stack,
        "assert_isolated_compose",
        lambda _identity, *, run_identity: None,
    )
    monkeypatch.setattr(
        stack,
        "database_volume_observation",
        lambda _run_identity, **_kwargs: absent,
    )
    monkeypatch.setattr(
        stack,
        "stack_snapshot",
        lambda **_kwargs: {"services": {}},
    )
    monkeypatch.setattr(
        stack,
        "compose",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    cleanup_called: list[bool] = []

    def interrupted_cleanup(*_args: object, **_kwargs: object) -> dict:
        cleanup_called.append(True)
        raise SystemExit(12)

    monkeypatch.setattr(
        stack,
        "cleanup_recovery_run",
        interrupted_cleanup,
    )

    with pytest.raises(KeyboardInterrupt):
        stack.fresh_up("fault-cleanup-interrupt-generalized")

    assert cleanup_called == [True]
    assert [event["action"] for event in stack.load_verified_events()] == [
        "fresh_up_started"
    ]


def test_fresh_up_preserves_original_and_cleanup_errors_in_setup_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_dir = (tmp_path / "cleanup-failed").resolve()
    monkeypatch.setenv(stack.EVIDENCE_DIR_ENV, str(evidence_dir))
    monkeypatch.setattr(stack.secrets, "token_hex", lambda _size: "9" * 32)
    identity = frozen_identity()
    volume_name = "forwin-v5-recovery-" + "9" * 32 + "-postgres-data"
    absent = {"name": volume_name, "exists": False}

    monkeypatch.setattr(stack, "assert_frozen", lambda **_kwargs: identity)
    monkeypatch.setattr(
        stack,
        "assert_isolated_compose",
        lambda _identity, *, run_identity: None,
    )
    monkeypatch.setattr(
        stack,
        "database_volume_observation",
        lambda _run_identity, **_kwargs: absent,
    )
    monkeypatch.setattr(
        stack,
        "stack_snapshot",
        lambda **_kwargs: {"stage": "before", "services": {}},
    )
    monkeypatch.setattr(
        stack,
        "compose",
        lambda *args, **_kwargs: (
            (_ for _ in ()).throw(stack.StackError("original dependency failure"))
        ),
    )
    monkeypatch.setattr(
        stack,
        "compose_process",
        lambda *args, **_kwargs: subprocess.CompletedProcess(
            args,
            1,
            "",
            "cleanup down failed",
        ),
    )
    monkeypatch.setattr(
        stack,
        "destroyed_service_inventory",
        lambda _run_identity, **_kwargs: (
            (_ for _ in ()).throw(stack.StackError("service residue remains"))
        ),
    )
    monkeypatch.setattr(
        stack,
        "now",
        lambda: "2026-07-22T12:00:00+00:00",
    )

    with pytest.raises(stack.StackError, match="original dependency failure"):
        stack.fresh_up("fault-1")

    events = stack.load_verified_events()
    assert [event["action"] for event in events] == [
        "fresh_up_started",
        "setup_blocked",
    ]
    blocked = events[-1]
    assert blocked["failure_reason"] == "original dependency failure"
    assert "cleanup down failed" in blocked["cleanup_error"]
    assert "service residue remains" in blocked["cleanup_error"]
    assert blocked["cleanup_confirmed_at"] is None
    assert all(event["action"] != "destroyed" for event in events)
    with pytest.raises(stack.StackError, match="terminal"):
        stack.destroy()


def test_fresh_up_preserves_primary_error_when_cleanup_helper_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _evidence_dir, _identity, _absent = configure_primary_fresh_up_failure(
        tmp_path,
        monkeypatch,
        run_digit="1",
    )
    monkeypatch.setattr(
        stack,
        "cleanup_recovery_run",
        lambda *_args, **_kwargs: (
            (_ for _ in ()).throw(RuntimeError("cleanup helper exploded"))
        ),
    )

    with pytest.raises(stack.StackError) as captured:
        stack.fresh_up("fault-cleanup-helper")

    message = str(captured.value)
    assert "setup_failure=initial_down: primary dependency setup failure" in message
    assert "cleanup_error=cleanup helper: cleanup helper exploded" in message
    assert "terminal_recording_error=<none>" in message
    events = stack.load_verified_events()
    assert [event["action"] for event in events] == [
        "fresh_up_started",
        "setup_blocked",
    ]
    blocked = events[-1]
    assert blocked["setup_failure"] == (
        "initial_down: primary dependency setup failure"
    )
    assert "cleanup helper exploded" in blocked["cleanup_error"]
    assert blocked["terminal_recording_error"] is None
    assert blocked["cleanup_confirmed_at"] is None
    assert all(
        event["action"] not in {"fresh_up_completed", "destroyed"}
        for event in events
    )


def test_fresh_up_uses_safe_timestamp_when_cleanup_clock_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _evidence_dir, _identity, absent = configure_primary_fresh_up_failure(
        tmp_path,
        monkeypatch,
        run_digit="2",
    )
    timestamps = iter(
        [
            "2026-07-22T12:00:00+00:00",
            "2026-07-22T12:00:00+00:00",
        ]
    )

    def failing_now() -> str:
        try:
            return next(timestamps)
        except StopIteration as exc:
            raise RuntimeError("cleanup clock unavailable") from exc

    monkeypatch.setattr(stack, "now", failing_now)

    with pytest.raises(stack.StackError) as captured:
        stack.fresh_up("fault-cleanup-clock")

    message = str(captured.value)
    assert "setup_failure=initial_down: primary dependency setup failure" in message
    assert "cleanup clock unavailable" in message
    assert "terminal_recording_error=<none>" in message
    events = stack.load_verified_events()
    assert [event["action"] for event in events] == [
        "fresh_up_started",
        "setup_blocked",
    ]
    blocked = events[-1]
    assert blocked["database_volume"] == absent
    assert blocked["cleanup_requested_at"] == (
        "2026-07-22T12:00:00+00:00"
    )
    assert "cleanup clock unavailable" in blocked["cleanup_error"]
    assert blocked["terminal_recording_error"] is None
    assert all(
        event["action"] not in {"fresh_up_completed", "destroyed"}
        for event in events
    )


def test_fresh_up_falls_back_when_setup_blocked_helper_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _evidence_dir, _identity, _absent = configure_primary_fresh_up_failure(
        tmp_path,
        monkeypatch,
        run_digit="3",
    )
    monkeypatch.setattr(
        stack,
        "append_setup_blocked",
        lambda **_kwargs: (
            (_ for _ in ()).throw(OSError("blocked helper failed"))
        ),
    )

    with pytest.raises(stack.StackError) as captured:
        stack.fresh_up("fault-terminal-helper")

    message = str(captured.value)
    assert "setup_failure=initial_down: primary dependency setup failure" in message
    assert "terminal_recording_error=blocked helper failed" in message
    events = stack.load_verified_events()
    assert [event["action"] for event in events] == [
        "fresh_up_started",
        "setup_blocked",
    ]
    assert events[-1]["terminal_recording_error"] == "blocked helper failed"
    assert all(
        event["action"] not in {"fresh_up_completed", "destroyed"}
        for event in events
    )


def test_fresh_up_combines_terminal_append_error_and_seals_incomplete_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _evidence_dir, _identity, _absent = configure_primary_fresh_up_failure(
        tmp_path,
        monkeypatch,
        run_digit="4",
    )
    original_append_event = stack.append_event

    def fail_terminal_append(action: str, **payload: object) -> dict:
        if action == "setup_blocked":
            raise OSError("terminal append failed")
        return original_append_event(action, **payload)

    monkeypatch.setattr(stack, "append_event", fail_terminal_append)

    with pytest.raises(stack.StackError) as captured:
        stack.fresh_up("fault-terminal-append")

    message = str(captured.value)
    assert "setup_failure=initial_down: primary dependency setup failure" in message
    assert "cleanup_error=<none>" in message
    assert "terminal_recording_error=" in message
    assert "terminal append failed" in message
    events = stack.load_verified_events()
    assert [event["action"] for event in events] == ["fresh_up_started"]
    assert all(
        event["action"] not in {"fresh_up_completed", "destroyed"}
        for event in events
    )

    commands = {
        "config": stack.validate_config,
        "fresh-up": lambda: stack.fresh_up("another-fault"),
        "v1-up": stack.v1_up,
        "destroy": stack.destroy,
        "snapshot": lambda: stack.snapshot("after-failure"),
        "stop": lambda: stack.stop_fault_service(
            "qdrant",
            "fault-terminal-append",
        ),
        "start": lambda: stack.start_fault_service(
            "qdrant",
            "fault-terminal-append",
        ),
        "kill": lambda: stack.kill_fault_service(
            "generation-worker",
            "fault-terminal-append",
        ),
        "mark": lambda: stack.mark_fault(
            "publisher_captcha",
            "fault",
            "fault-terminal-append",
        ),
    }
    for command_name, invoke in commands.items():
        with pytest.raises(
            stack.StackError,
            match="incomplete fresh-up",
        ):
            invoke()
        assert command_name
    assert stack.load_verified_events() == events


def test_fresh_up_primary_survives_terminal_recording_coordinator_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _evidence_dir, _identity, _absent = configure_primary_fresh_up_failure(
        tmp_path,
        monkeypatch,
        run_digit="9",
    )
    monkeypatch.setattr(
        stack,
        "record_setup_blocked",
        lambda **_kwargs: (
            (_ for _ in ()).throw(
                RuntimeError("terminal coordinator exploded")
            )
        ),
    )

    with pytest.raises(stack.StackError) as captured:
        stack.fresh_up("fault-terminal-coordinator")

    message = str(captured.value)
    assert "setup_failure=initial_down: primary dependency setup failure" in message
    assert "cleanup_error=cleanup status unavailable" in message
    assert (
        "terminal_recording_error=record_setup_blocked: "
        "terminal coordinator exploded"
    ) in message
    assert [
        event["action"] for event in stack.load_verified_events()
    ] == ["fresh_up_started"]
    with pytest.raises(stack.StackError, match="incomplete fresh-up"):
        stack.validate_config()


@pytest.mark.parametrize(
    ("timeout_boundary", "expected_operations"),
    [
        ("before_down_launch", []),
        ("down", ["down"]),
        (
            "service_inspect",
            [
                "down",
                "service_ps:postgres",
                "service_inspect:postgres",
            ],
        ),
        (
            "volume_inspect",
            [
                "down",
                *[f"service_ps:{service}" for service in stack.SERVICES],
                "volume_ls",
                "volume_inspect",
            ],
        ),
    ],
)
def test_cleanup_deadline_bounds_all_docker_confirmation_and_releases_lock(
    timeout_boundary: str,
    expected_operations: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_compose = stack.compose
    real_compose_process = stack.compose_process
    real_volume_observation = stack.database_volume_observation
    real_destroyed_inventory = stack.destroyed_service_inventory
    evidence_dir, _identity, absent = configure_primary_fresh_up_failure(
        tmp_path,
        monkeypatch,
        run_digit={
            "before_down_launch": "8",
            "down": "5",
            "service_inspect": "6",
            "volume_inspect": "7",
        }[timeout_boundary],
    )
    run_identity = stack.new_recovery_run_identity(
        f"fault-{timeout_boundary}",
        run_id={
            "before_down_launch": "8",
            "down": "5",
            "service_inspect": "6",
            "volume_inspect": "7",
        }[timeout_boundary]
        * 32,
        directory=evidence_dir,
    )
    clock = {"value": 0.0}
    operations: list[str] = []
    timeouts: list[tuple[str, float | None]] = []
    initial_volume_observed = {"done": False}
    setup_compose_failed = {"done": False}

    def initial_then_real_volume(
        checked_run_identity: dict,
        **kwargs: object,
    ) -> dict:
        if not initial_volume_observed["done"] and "deadline" not in kwargs:
            initial_volume_observed["done"] = True
            return absent
        return real_volume_observation(checked_run_identity, **kwargs)

    def setup_then_real_compose(
        *args: str,
        **kwargs: object,
    ) -> str:
        if not setup_compose_failed["done"]:
            setup_compose_failed["done"] = True
            raise stack.StackError("primary dependency setup failure")
        return real_compose(*args, **kwargs)

    def fake_subprocess_run(
        command: list[str] | tuple[str, ...],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        command_tuple = tuple(command)
        timeout = kwargs.get("timeout")
        operation: str
        if command_tuple[:2] == ("docker", "compose"):
            if "down" in command_tuple:
                operation = "down"
            elif "ps" in command_tuple:
                operation = f"service_ps:{command_tuple[-1]}"
            else:
                raise AssertionError(command_tuple)
        elif command_tuple[:3] == ("docker", "volume", "ls"):
            operation = "volume_ls"
        elif command_tuple[:3] == ("docker", "volume", "inspect"):
            operation = "volume_inspect"
        elif command_tuple[:3] == ("docker", "container", "inspect"):
            operation = (
                "service_inspect:"
                + command_tuple[-1].removeprefix("container-")
            )
        else:
            raise AssertionError(command_tuple)
        operations.append(operation)
        timeouts.append(
            (
                operation,
                float(timeout) if timeout is not None else None,
            )
        )
        if (
            timeout_boundary == "down"
            and operation == "down"
        ) or (
            timeout_boundary == "service_inspect"
            and operation == "service_inspect:postgres"
        ) or (
            timeout_boundary == "volume_inspect"
            and operation == "volume_inspect"
        ):
            clock["value"] = float(stack.CLEANUP_TIMEOUT_SECONDS)
            raise subprocess.TimeoutExpired(
                command_tuple,
                timeout=float(timeout or 0),
            )
        if operation == "down":
            clock["value"] = 1.0
            return subprocess.CompletedProcess(command_tuple, 0, "", "")
        if operation.startswith("service_ps:"):
            if timeout_boundary == "service_inspect":
                service = operation.partition(":")[2]
                return subprocess.CompletedProcess(
                    command_tuple,
                    0,
                    f"container-{service}",
                    "",
                )
            return subprocess.CompletedProcess(command_tuple, 0, "", "")
        if operation == "volume_ls":
            return subprocess.CompletedProcess(
                command_tuple,
                0,
                run_identity["database_volume_name"],
                "",
            )
        return subprocess.CompletedProcess(command_tuple, 0, "[]", "")

    monkeypatch.setattr(stack, "compose", setup_then_real_compose)
    monkeypatch.setattr(stack, "compose_process", real_compose_process)
    monkeypatch.setattr(
        stack,
        "database_volume_observation",
        initial_then_real_volume,
    )
    monkeypatch.setattr(
        stack,
        "destroyed_service_inventory",
        real_destroyed_inventory,
    )
    monkeypatch.setattr(
        stack,
        "compose_environment",
        lambda **_kwargs: (
            clock.update(
                {
                    "value": float(stack.CLEANUP_TIMEOUT_SECONDS),
                }
            )
            if timeout_boundary == "before_down_launch"
            else None
        )
        or {
            "FORWIN_RECOVERY_ENV_FILE": str(tmp_path / "unused.env"),
        },
    )
    monkeypatch.setattr(stack.time, "monotonic", lambda: clock["value"])
    monkeypatch.setattr(stack.subprocess, "run", fake_subprocess_run)

    started = time.perf_counter()
    with pytest.raises(stack.StackError) as captured:
        stack.fresh_up(f"fault-{timeout_boundary}")
    elapsed = time.perf_counter() - started

    message = str(captured.value)
    assert elapsed < 1
    assert "setup_failure=initial_down: primary dependency setup failure" in message
    assert "cleanup_error=" in message
    assert "timed out" in message
    assert "terminal_recording_error=<none>" in message
    assert operations == expected_operations
    assert all(timeout is not None and timeout > 0 for _name, timeout in timeouts)
    assert all(
        timeout <= stack.CLEANUP_TIMEOUT_SECONDS
        for _name, timeout in timeouts
        if timeout is not None
    )
    events = stack.load_verified_events()
    assert [event["action"] for event in events] == [
        "fresh_up_started",
        "setup_blocked",
    ]
    assert events[-1]["cleanup_confirmed_at"] is None
    assert "timed out" in events[-1]["cleanup_error"]
    assert all(
        event["action"] not in {"fresh_up_completed", "destroyed"}
        for event in events
    )

    context = multiprocessing.get_context("fork")
    results = context.Queue()
    probe = context.Process(
        target=run_lock_probe_process,
        args=(str(evidence_dir), results),
        name=f"lock-probe-{timeout_boundary}",
    )
    probe.start()
    assert joined_process_result(probe, results) == ("ok", "acquired")


@pytest.mark.parametrize(
    ("method_name", "service", "fault_action", "time_field"),
    [
        (
            "stop_fault_service",
            "qdrant",
            "fault_service_stopped",
            "fault_time",
        ),
        (
            "kill_fault_service",
            "generation-worker",
            "fault_service_killed",
            "crash_time",
        ),
    ],
)
def test_fault_timestamp_is_captured_after_non_running_inspection(
    method_name: str,
    service: str,
    fault_action: str,
    time_field: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(stack.EVIDENCE_DIR_ENV, str((tmp_path / "fault").resolve()))
    timeline: list[str] = []
    times = iter(
        [
            "2026-07-22T12:00:00+00:00",
            "2026-07-22T12:00:01+00:00",
        ]
    )
    identity = frozen_identity()
    run_identity = {
        "run_id": "d" * 32,
        "evidence_directory": "/tmp/fault-1",
        "database_volume_name": (
            "forwin-v5-recovery-" + "d" * 32 + "-postgres-data"
        ),
    }
    volume = {
        "name": run_identity["database_volume_name"],
        "exists": True,
        "created_at": "2026-07-22T11:58:30+00:00",
        "fingerprint": "volume-fingerprint",
    }
    inspections = iter(
        [
            {"service": service, "running": True, "container_id": "container-1"},
            {"service": service, "running": False, "container_id": "container-1"},
        ]
    )
    events: list[tuple[str, dict]] = []

    monkeypatch.setattr(stack, "assert_frozen", lambda: identity)
    monkeypatch.setattr(
        stack,
        "assert_isolated_compose",
        lambda _identity, *, run_identity: None,
    )
    monkeypatch.setattr(
        stack,
        "require_active_recovery_run",
        lambda fault_id, **_kwargs: {
            "fault_id": fault_id,
            "run_identity": run_identity,
            "database_volume": volume,
            "identity": identity,
            "events": [],
        },
        raising=False,
    )
    monkeypatch.setattr(
        stack,
        "inspect_service",
        lambda _service, *, run_identity: (
            timeline.append("inspect") or next(inspections)
        ),
    )
    monkeypatch.setattr(
        stack,
        "compose",
        lambda *_args, run_identity: timeline.append("compose") or "",
    )
    monkeypatch.setattr(
        stack,
        "command",
        lambda *_args, **_kwargs: timeline.append("docker") or "",
    )
    monkeypatch.setattr(
        stack,
        "confirmed_database_volume",
        lambda _run_identity, _expected: volume,
        raising=False,
    )
    monkeypatch.setattr(
        stack,
        "append_event",
        lambda action, **payload: events.append((action, payload)) or payload,
    )

    def fake_now() -> str:
        value = next(times)
        timeline.append(f"time:{value}")
        return value

    monkeypatch.setattr(stack, "now", fake_now)

    getattr(stack, method_name)(service, "fault-1")

    event = events[-1]
    assert event[0] == fault_action
    assert event[1]["requested_at"] == "2026-07-22T12:00:00+00:00"
    assert event[1][time_field] == "2026-07-22T12:00:01+00:00"
    assert event[1]["run_identity"] == run_identity
    assert event[1]["database_volume"] == volume
    assert timeline.index("time:2026-07-22T12:00:00+00:00") < min(
        index
        for index, item in enumerate(timeline)
        if item in {"compose", "docker"}
    )
    assert timeline.index("inspect", 1) < timeline.index(
        "time:2026-07-22T12:00:01+00:00"
    )


def test_recovery_timestamp_is_captured_after_readiness_and_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(stack.EVIDENCE_DIR_ENV, str((tmp_path / "fault").resolve()))
    timeline: list[str] = []
    times = iter(
        [
            "2026-07-22T12:01:00+00:00",
            "2026-07-22T12:01:01+00:00",
        ]
    )
    identity = frozen_identity()
    run_identity = {
        "run_id": "e" * 32,
        "evidence_directory": "/tmp/fault-1",
        "database_volume_name": (
            "forwin-v5-recovery-" + "e" * 32 + "-postgres-data"
        ),
    }
    volume = {
        "name": run_identity["database_volume_name"],
        "exists": True,
        "created_at": "2026-07-22T11:58:30+00:00",
        "fingerprint": "volume-fingerprint",
    }
    events: list[tuple[str, dict]] = []

    monkeypatch.setattr(stack, "assert_frozen", lambda: identity)
    monkeypatch.setattr(
        stack,
        "assert_isolated_compose",
        lambda _identity, *, run_identity: None,
    )
    monkeypatch.setattr(
        stack,
        "require_active_recovery_run",
        lambda fault_id, **_kwargs: {
            "fault_id": fault_id,
            "run_identity": run_identity,
            "database_volume": volume,
            "identity": identity,
            "events": [
                {
                    "action": "fault_service_stopped",
                    "service": "qdrant",
                }
            ],
        },
        raising=False,
    )
    monkeypatch.setattr(
        stack,
        "inspect_service",
        lambda _service, *, run_identity: {
            "service": "qdrant",
            "running": False,
            "container_id": "container-1",
        },
    )
    monkeypatch.setattr(
        stack,
        "compose",
        lambda *_args, run_identity: timeline.append("compose") or "",
    )
    monkeypatch.setattr(
        stack,
        "wait_service",
        lambda _service, *, run_identity: (
            timeline.append("ready")
            or {"service": "qdrant", "running": True}
        ),
    )
    monkeypatch.setattr(
        stack,
        "functional_probe",
        lambda _service, *, run_identity: (
            timeline.append("probe") or {"passed": True}
        ),
    )
    monkeypatch.setattr(
        stack,
        "confirmed_database_volume",
        lambda _run_identity, _expected: volume,
        raising=False,
    )
    monkeypatch.setattr(
        stack,
        "append_event",
        lambda action, **payload: events.append((action, payload)) or payload,
    )

    def fake_now() -> str:
        value = next(times)
        timeline.append(f"time:{value}")
        return value

    monkeypatch.setattr(stack, "now", fake_now)

    stack.start_fault_service("qdrant", "fault-1")

    event = events[-1]
    assert event[0] == "fault_service_recovered"
    assert event[1]["requested_at"] == "2026-07-22T12:01:00+00:00"
    assert event[1]["recovery_time"] == "2026-07-22T12:01:01+00:00"
    assert timeline.index("probe") < timeline.index(
        "time:2026-07-22T12:01:01+00:00"
    )


def test_typed_marker_rejects_duplicate_wrong_order_and_wrong_fault_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_identity, volume, identity = recovery_lifecycle(tmp_path, monkeypatch)
    monkeypatch.setattr(stack, "assert_frozen", lambda **_kwargs: identity)
    monkeypatch.setattr(
        stack,
        "assert_isolated_compose",
        lambda _identity, *, run_identity: None,
    )
    monkeypatch.setattr(
        stack,
        "confirmed_database_volume",
        lambda _run_identity, _expected: volume,
        raising=False,
    )

    fault = stack.mark_fault("publisher_captcha", "fault", "fault-1")
    recovery = stack.mark_fault("publisher_captcha", "recovery", "fault-1")

    assert fault["action"] == "fault_marked"
    assert fault["fault_kind"] == "publisher_captcha"
    assert recovery["action"] == "recovery_marked"
    assert recovery["fault_kind"] == "publisher_captcha"
    assert fault["run_identity"] == recovery["run_identity"] == run_identity
    assert "assertions" not in fault

    with pytest.raises(stack.StackError, match="duplicate"):
        stack.mark_fault("publisher_captcha", "recovery", "fault-1")
    with pytest.raises(stack.StackError, match="fault identity"):
        stack.mark_fault("publisher_captcha", "fault", "different-fault")

    other_dir = (tmp_path / "wrong-order").resolve()
    monkeypatch.setenv(stack.EVIDENCE_DIR_ENV, str(other_dir))
    recovery_lifecycle(
        tmp_path,
        monkeypatch,
        fault_id="wrong-order",
    )
    with pytest.raises(stack.StackError, match="before fault marker"):
        stack.mark_fault("publisher_mfa", "recovery", "wrong-order")


def test_typed_marker_rejects_an_existing_service_fault(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_identity, volume, identity = recovery_lifecycle(tmp_path, monkeypatch)
    stack.append_event(
        "fault_service_stopped",
        fault_id="fault-1",
        service="qdrant",
        fault_time="2026-07-22T12:00:00+00:00",
        identity=identity,
        run_identity=run_identity,
        database_volume=volume,
    )

    with pytest.raises(stack.StackError, match="duplicate fault"):
        stack.mark_fault("publisher_captcha", "fault", "fault-1")


def test_setup_hold_pairs_are_serial_and_can_overlap_primary_fault(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_identity, volume, identity = recovery_lifecycle(tmp_path, monkeypatch)
    running = {service: True for service in stack.SERVICES}
    timeline: list[str] = []
    clock = iter(
        f"2026-07-22T12:0{minute}:{second:02d}+00:00"
        for minute in range(6)
        for second in range(60)
    )

    def inspect_service(service: str, **_kwargs: object) -> dict:
        timeline.append(f"inspect:{service}:{running[service]}")
        return {
            "service": service,
            "exists": True,
            "running": running[service],
            "container_id": f"{service}-container",
            "image_id": f"sha256:{service}-image",
        }

    def compose(*args: str, **_kwargs: object) -> str:
        timeline.append("compose:" + ":".join(args))
        if args[0] == "stop":
            running[args[-1]] = False
        elif args[0] == "start":
            running[args[-1]] = True
        return ""

    monkeypatch.setattr(stack, "assert_frozen", lambda **_kwargs: identity)
    monkeypatch.setattr(
        stack,
        "assert_isolated_compose",
        lambda _identity, *, run_identity: None,
    )
    monkeypatch.setattr(stack, "inspect_service", inspect_service)
    monkeypatch.setattr(stack, "compose", compose)
    monkeypatch.setattr(
        stack,
        "wait_service",
        lambda service, **_kwargs: timeline.append(f"ready:{service}")
        or {
            "service": service,
            "exists": True,
            "running": True,
            "container_id": f"{service}-container",
            "image_id": f"sha256:{service}-image",
        },
    )
    monkeypatch.setattr(
        stack,
        "functional_probe",
        lambda service, **_kwargs: timeline.append(f"probe:{service}")
        or {"passed": True},
    )
    monkeypatch.setattr(
        stack,
        "confirmed_database_volume",
        lambda _run_identity, _expected: volume,
    )
    def fake_now() -> str:
        value = next(clock)
        timeline.append(f"time:{value}")
        return value

    monkeypatch.setattr(stack, "now", fake_now)

    first_hold = stack.setup_hold_service(
        "outbox-worker",
        "fault-1",
        "outbox-preapproval",
        "minio_post_canon_unavailable",
        "auxiliary",
    )
    stack.append_event(
        "fault_service_stopped",
        fault_id="fault-1",
        service="minio",
        requested_at="2026-07-22T12:01:00+00:00",
        fault_time="2026-07-22T12:01:01+00:00",
        identity=identity,
        run_identity=run_identity,
        database_volume=volume,
    )
    stack.append_event(
        "fault_service_recovered",
        fault_id="fault-1",
        service="minio",
        requested_at="2026-07-22T12:02:00+00:00",
        recovery_time="2026-07-22T12:02:01+00:00",
        identity=identity,
        run_identity=run_identity,
        database_volume=volume,
    )
    first_release = stack.setup_release_service(
        "outbox-worker",
        "fault-1",
        "outbox-preapproval",
    )
    second_hold = stack.setup_hold_service(
        "outbox-worker",
        "fault-1",
        "outbox-replay",
        "minio_post_canon_unavailable",
        "auxiliary",
    )
    second_release = stack.setup_release_service(
        "outbox-worker",
        "fault-1",
        "outbox-replay",
    )

    assert first_hold["action"] == "setup_service_held"
    assert first_release["action"] == "setup_service_released"
    assert second_hold["hold_id"] == "outbox-replay"
    assert second_release["hold_id"] == "outbox-replay"
    assert first_hold["before"]["running"] is True
    assert first_hold["after"]["running"] is False
    assert first_release["before"]["running"] is False
    assert first_release["after"]["running"] is True
    assert first_hold["identity"] == first_release["identity"] == identity
    assert first_hold["run_identity"] == run_identity
    assert first_hold["database_volume"] == volume
    assert timeline.index("time:2026-07-22T12:00:00+00:00") < timeline.index(
        "compose:stop:--timeout:10:outbox-worker"
    )
    assert timeline.index("inspect:outbox-worker:False") < timeline.index(
        "time:2026-07-22T12:00:01+00:00"
    )
    assert timeline.index("compose:start:outbox-worker") < timeline.index(
        "ready:outbox-worker"
    )
    assert timeline.index("ready:outbox-worker") < timeline.index(
        "probe:outbox-worker"
    )
    assert timeline.index("probe:outbox-worker") < timeline.index(
        f"time:{first_release['release_time']}"
    )
    assert [
        event["action"] for event in stack.load_verified_events()
    ] == [
        "fresh_up_started",
        "fresh_up_completed",
        "setup_service_held",
        "fault_service_stopped",
        "fault_service_recovered",
        "setup_service_released",
        "setup_service_held",
        "setup_service_released",
    ]


def test_publisher_backend_primary_hold_requires_exact_pre_fault_purpose(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_identity, volume, identity = recovery_lifecycle(tmp_path, monkeypatch)
    running = {"publisher-worker": True}

    def service_state(service: str, **_kwargs: object) -> dict:
        return {
            "service": service,
            "exists": True,
            "running": running[service],
            "container_id": f"{service}-boundary-container",
            "image_id": "sha256:" + "b" * 64,
        }

    def compose(*args: str, **_kwargs: object) -> str:
        running[args[-1]] = args[0] == "start"
        return ""

    monkeypatch.setattr(stack, "assert_frozen", lambda **_kwargs: identity)
    monkeypatch.setattr(
        stack,
        "assert_isolated_compose",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(stack, "inspect_service", service_state)
    monkeypatch.setattr(stack, "wait_service", service_state)
    monkeypatch.setattr(stack, "compose", compose)
    monkeypatch.setattr(
        stack,
        "functional_probe",
        lambda *_args, **_kwargs: {"passed": True},
    )
    monkeypatch.setattr(
        stack,
        "confirmed_database_volume",
        lambda _run_identity, _expected: volume,
    )

    held = stack.setup_hold_service(
        "publisher-worker",
        "fault-1",
        "backend-boundary",
        "publisher_backend_unavailable",
        "pre-fault-boundary",
    )
    with pytest.raises(stack.StackError, match="only for an auxiliary hold"):
        stack.setup_discard_service(
            "publisher-worker",
            "fault-1",
            "backend-boundary",
        )
    released = stack.setup_release_service(
        "publisher-worker",
        "fault-1",
        "backend-boundary",
    )

    assert held["purpose"] == released["purpose"] == "pre-fault-boundary"
    assert held["after"]["container_id"] == released["before"]["container_id"]
    assert held["after"]["image_id"] == released["after"]["image_id"]
    with pytest.raises(stack.StackError, match="already used"):
        stack.setup_hold_service(
            "publisher-worker",
            "fault-1",
            "backend-boundary",
            "publisher_backend_unavailable",
            "pre-fault-boundary",
        )


@pytest.mark.parametrize(
    ("service", "fault_kind", "purpose", "expected"),
    (
        (
            "publisher-worker",
            "publisher_backend_unavailable",
            "auxiliary",
            "primary fault service",
        ),
        (
            "outbox-worker",
            "publisher_backend_unavailable",
            "pre-fault-boundary",
            "pre-fault purpose",
        ),
        (
            "publisher-worker",
            "publisher_captcha",
            "pre-fault-boundary",
            "pre-fault purpose",
        ),
    ),
)
def test_controller_rejects_every_other_pre_fault_primary_hold(
    service: str,
    fault_kind: str,
    purpose: str,
    expected: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recovery_lifecycle(tmp_path, monkeypatch)

    with pytest.raises(stack.StackError, match=expected):
        stack.setup_hold_service(
            service,
            "fault-1",
            f"wrong-boundary-{service}",
            fault_kind,
            purpose,
        )


def test_setup_hold_rejects_duplicate_overlap_and_mismatched_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_identity, volume, identity = recovery_lifecycle(tmp_path, monkeypatch)
    running = {service: True for service in stack.SERVICES}

    def inspect_service(service: str, **_kwargs: object) -> dict:
        return {
            "service": service,
            "exists": True,
            "running": running[service],
            "container_id": f"{service}-container",
            "image_id": f"sha256:{service}-image",
        }

    def compose(*args: str, **_kwargs: object) -> str:
        if args[0] == "stop":
            running[args[-1]] = False
        elif args[0] == "start":
            running[args[-1]] = True
        return ""

    monkeypatch.setattr(stack, "assert_frozen", lambda **_kwargs: identity)
    monkeypatch.setattr(
        stack,
        "assert_isolated_compose",
        lambda _identity, *, run_identity: None,
    )
    monkeypatch.setattr(stack, "inspect_service", inspect_service)
    monkeypatch.setattr(stack, "compose", compose)
    monkeypatch.setattr(
        stack,
        "confirmed_database_volume",
        lambda _run_identity, _expected: volume,
    )

    stack.setup_hold_service(
        "outbox-worker",
        "fault-1",
        "hold-1",
        "minio_post_canon_unavailable",
        "auxiliary",
    )
    independent = stack.setup_hold_service(
        "publisher-worker",
        "fault-1",
        "hold-2",
        "minio_post_canon_unavailable",
        "auxiliary",
    )
    assert independent["action"] == "setup_service_held"

    with pytest.raises(stack.StackError, match="already used"):
        stack.setup_hold_service(
            "qdrant",
            "fault-1",
            "hold-1",
            "minio_post_canon_unavailable",
            "auxiliary",
        )
    with pytest.raises(stack.StackError, match="active setup hold"):
        stack.setup_hold_service(
            "outbox-worker",
            "fault-1",
            "hold-3",
            "minio_post_canon_unavailable",
            "auxiliary",
        )
    with pytest.raises(stack.StackError, match="matching setup hold"):
        stack.setup_release_service("outbox-worker", "fault-1", "wrong-id")
    with pytest.raises(stack.StackError, match="service mismatch"):
        stack.setup_release_service("minio", "fault-1", "hold-1")
    with pytest.raises(stack.StackError, match="hold identity"):
        stack.setup_hold_service(
            "outbox-worker",
            "fault-1",
            "../unsafe",
            "minio_post_canon_unavailable",
            "auxiliary",
        )


def test_setup_hold_cli_surface_and_post_destroy_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_identity, volume, identity = recovery_lifecycle(tmp_path, monkeypatch)
    stack.append_event(
        "fault_marked",
        fault_id="fault-1",
        fault_kind="publisher_captcha",
        fault_time="2026-07-22T12:00:00+00:00",
        identity=identity,
        run_identity=run_identity,
        database_volume=volume,
    )
    stack.append_event(
        "recovery_marked",
        fault_id="fault-1",
        fault_kind="publisher_captcha",
        recovery_time="2026-07-22T12:01:00+00:00",
        identity=identity,
        run_identity=run_identity,
        database_volume=volume,
    )
    stack.append_event(
        "destroyed",
        fault_id="fault-1",
        identity=identity,
        run_identity=run_identity,
        database_volume_before=volume,
        database_volume={
            "name": run_identity["database_volume_name"],
            "exists": False,
        },
    )
    with pytest.raises(stack.StackError, match="terminal"):
        stack.setup_hold_service(
            "outbox-worker",
            "fault-1",
            "late-hold",
            "publisher_captcha",
            "auxiliary",
        )

    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(stack.__file__),
            "setup-release",
            "outbox-worker",
            "--fault-id",
            "fault-1",
            "--hold-id",
            "hold-1",
        ],
    )
    args = stack.parse_args()
    assert (
        args.command,
        args.service,
        args.fault_id,
        args.hold_id,
    ) == ("setup-release", "outbox-worker", "fault-1", "hold-1")

    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(stack.__file__),
            "abort",
            "--fault-id",
            "fault-1",
            "--stage",
            "preflight",
            "--reason",
            "failed",
        ],
    )
    abort_args = stack.parse_args()
    assert (
        abort_args.command,
        abort_args.fault_id,
        abort_args.stage,
        abort_args.reason,
    ) == ("abort", "fault-1", "preflight", "failed")


def test_destroy_rejects_unbalanced_setup_hold_before_compose(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_identity, volume, identity = recovery_lifecycle(tmp_path, monkeypatch)
    stack.append_event(
        "setup_service_held",
        fault_id="fault-1",
        hold_id="hold-1",
        service="outbox-worker",
        fault_kind="minio_post_canon_unavailable",
        purpose="auxiliary",
        requested_at="2026-07-22T12:00:00+00:00",
        hold_time="2026-07-22T12:00:01+00:00",
        identity=identity,
        run_identity=run_identity,
        database_volume=volume,
        before={
            "exists": True,
            "running": True,
            "container_id": "outbox-worker-container",
            "image_id": "sha256:outbox-worker-image",
        },
        after={
            "exists": True,
            "running": False,
            "container_id": "outbox-worker-container",
            "image_id": "sha256:outbox-worker-image",
        },
    )
    stack.append_event(
        "fault_marked",
        fault_id="fault-1",
        fault_kind="publisher_mfa",
        fault_time="2026-07-22T12:01:00+00:00",
        identity=identity,
        run_identity=run_identity,
        database_volume=volume,
    )
    stack.append_event(
        "recovery_marked",
        fault_id="fault-1",
        fault_kind="publisher_mfa",
        recovery_time="2026-07-22T12:02:00+00:00",
        identity=identity,
        run_identity=run_identity,
        database_volume=volume,
    )
    monkeypatch.setattr(
        stack,
        "assert_frozen",
        lambda **_kwargs: pytest.fail("destroy mutated an unbalanced hold run"),
    )

    with pytest.raises(stack.StackError, match="active setup hold"):
        stack.destroy()


def test_setup_discard_balances_only_an_active_hold_without_starting_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_identity, volume, identity = recovery_lifecycle(tmp_path, monkeypatch)
    running = {"publisher-browser": True}
    compose_calls: list[tuple[str, ...]] = []

    def inspect_service(service: str, **_kwargs: object) -> dict:
        return {
            "service": service,
            "exists": True,
            "running": running[service],
            "container_id": f"{service}-container-generalized",
            "image_id": f"sha256:{service}-image-generalized",
        }

    def compose(*args: str, **_kwargs: object) -> str:
        compose_calls.append(args)
        if args[0] == "stop":
            running[args[-1]] = False
        return ""

    monkeypatch.setattr(stack, "assert_frozen", lambda **_kwargs: identity)
    monkeypatch.setattr(
        stack,
        "assert_isolated_compose",
        lambda _identity, *, run_identity: None,
    )
    monkeypatch.setattr(stack, "inspect_service", inspect_service)
    monkeypatch.setattr(stack, "compose", compose)
    monkeypatch.setattr(
        stack,
        "confirmed_database_volume",
        lambda _run_identity, _expected: volume,
    )

    stack.setup_hold_service(
        "publisher-browser",
        "fault-1",
        "risk-fixture-generalized",
        "publisher_captcha",
        "auxiliary",
    )
    discarded = stack.setup_discard_service(
        "publisher-browser",
        "fault-1",
        "risk-fixture-generalized",
    )

    assert discarded["action"] == "setup_service_discarded"
    assert discarded["before"]["running"] is False
    assert discarded["after"]["running"] is False
    assert discarded["after"]["container_id"] == (
        "publisher-browser-container-generalized"
    )
    assert not any(call[0] == "start" for call in compose_calls)
    assert stack.setup_hold_state(
        stack.load_verified_events()
    )["active_by_id"] == {}

    with pytest.raises(stack.StackError, match="matching setup hold"):
        stack.setup_discard_service(
            "publisher-browser",
            "fault-1",
            "risk-fixture-generalized",
        )
    with pytest.raises(stack.StackError, match="matching setup hold"):
        stack.setup_discard_service(
            "publisher-browser",
            "fault-1",
            "never-held-generalized",
        )


def test_setup_discard_rejects_service_drift_and_running_held_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_identity, volume, identity = recovery_lifecycle(tmp_path, monkeypatch)
    running = {"publisher-browser": True, "outbox-worker": True}

    def inspect_service(service: str, **_kwargs: object) -> dict:
        return {
            "service": service,
            "exists": True,
            "running": running[service],
            "container_id": f"{service}-container",
            "image_id": f"sha256:{service}-image",
        }

    def compose(*args: str, **_kwargs: object) -> str:
        if args[0] == "stop":
            running[args[-1]] = False
        return ""

    monkeypatch.setattr(stack, "assert_frozen", lambda **_kwargs: identity)
    monkeypatch.setattr(
        stack,
        "assert_isolated_compose",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(stack, "inspect_service", inspect_service)
    monkeypatch.setattr(stack, "compose", compose)
    monkeypatch.setattr(
        stack,
        "confirmed_database_volume",
        lambda _run_identity, _expected: volume,
    )
    stack.setup_hold_service(
        "publisher-browser",
        "fault-1",
        "risk-hold-generic",
        "publisher_captcha",
        "auxiliary",
    )

    with pytest.raises(stack.StackError, match="service mismatch"):
        stack.setup_discard_service(
            "outbox-worker",
            "fault-1",
            "risk-hold-generic",
        )

    running["publisher-browser"] = True
    with pytest.raises(stack.StackError, match="running before setup discard"):
        stack.setup_discard_service(
            "publisher-browser",
            "fault-1",
            "risk-hold-generic",
        )
    running["publisher-browser"] = False
    stack.append_event(
        "fault_service_stopped",
        fault_id="fault-1",
        service="publisher-browser",
        fault_time="2026-07-22T12:00:00+00:00",
        identity=identity,
        run_identity=run_identity,
        database_volume=volume,
    )
    with pytest.raises(stack.StackError, match="primary fault service"):
        stack.setup_discard_service(
            "publisher-browser",
            "fault-1",
            "risk-hold-generic",
        )


@pytest.mark.parametrize(
    "state",
    ("before-primary", "active-hold", "after-primary-fault"),
)
def test_abort_records_terminal_setup_blocked_from_any_active_state(
    state: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_identity, volume, identity = recovery_lifecycle(tmp_path, monkeypatch)
    if state == "active-hold":
        stack.append_event(
            "setup_service_held",
            fault_id="fault-1",
            hold_id="hold-1",
            service="outbox-worker",
            fault_kind="minio_post_canon_unavailable",
            purpose="auxiliary",
            requested_at="2026-07-22T12:00:00+00:00",
            hold_time="2026-07-22T12:00:01+00:00",
            identity=identity,
            run_identity=run_identity,
            database_volume=volume,
            before={
                "exists": True,
                "running": True,
                "container_id": "outbox-worker-container",
                "image_id": "sha256:outbox-worker-image",
            },
            after={
                "exists": True,
                "running": False,
                "container_id": "outbox-worker-container",
                "image_id": "sha256:outbox-worker-image",
            },
        )
    elif state == "after-primary-fault":
        stack.append_event(
            "fault_service_stopped",
            fault_id="fault-1",
            service="minio",
            requested_at="2026-07-22T12:00:00+00:00",
            fault_time="2026-07-22T12:00:01+00:00",
            identity=identity,
            run_identity=run_identity,
            database_volume=volume,
        )
    absent = {
        "name": run_identity["database_volume_name"],
        "exists": False,
    }
    cleanup = {
        "cleanup_requested_at": "2026-07-22T12:03:00+00:00",
        "cleanup_confirmed_at": "2026-07-22T12:03:01+00:00",
        "cleanup_error": None,
        "database_volume": absent,
        "after": {
            "observed_at": "2026-07-22T12:03:01+00:00",
            "services": {
                service: {"exists": False, "running": False}
                for service in stack.SERVICES
            },
        },
    }
    monkeypatch.setattr(stack, "assert_frozen", lambda **_kwargs: identity)
    monkeypatch.setattr(
        stack,
        "assert_isolated_compose",
        lambda _identity, *, run_identity: None,
    )
    monkeypatch.setattr(
        stack,
        "confirmed_database_volume",
        lambda _run_identity, _expected: volume,
    )
    monkeypatch.setattr(
        stack,
        "cleanup_recovery_run",
        lambda _run_identity, *, fallback_timestamp: cleanup,
    )

    with pytest.raises(
        stack.StackError,
        match="setup_failure=task5-preflight: operator requested abort",
    ):
        stack.abort_recovery_run(
            "fault-1",
            "task5-preflight",
            "operator requested\x00\nabort",
        )

    events = stack.load_verified_events()
    assert events[-2]["action"] == "abort_started"
    blocked = events[-1]
    assert blocked["action"] == "setup_blocked"
    assert blocked["failure_stage"] == "task5-preflight"
    assert blocked["failure_reason"] == "operator requested abort"
    assert blocked["cleanup_confirmed"] is True
    assert blocked["cleanup_error"] is None
    assert blocked["active_state"]["active_holds"] == (
        [
            {
                "container_id": "outbox-worker-container",
                "fault_kind": "minio_post_canon_unavailable",
                "hold_id": "hold-1",
                "image_id": "sha256:outbox-worker-image",
                "purpose": "auxiliary",
                "service": "outbox-worker",
            }
        ]
        if state == "active-hold"
        else []
    )
    with pytest.raises(stack.StackError, match="terminal"):
        stack.setup_hold_service(
            "outbox-worker",
            "fault-1",
            "too-late",
            "minio_post_canon_unavailable",
            "auxiliary",
        )


def test_abort_cleanup_and_terminal_append_failures_preserve_primary_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _run_identity, volume, identity = recovery_lifecycle(tmp_path, monkeypatch)
    monkeypatch.setattr(stack, "assert_frozen", lambda **_kwargs: identity)
    monkeypatch.setattr(
        stack,
        "assert_isolated_compose",
        lambda _identity, *, run_identity: None,
    )
    monkeypatch.setattr(
        stack,
        "confirmed_database_volume",
        lambda _run_identity, _expected: volume,
    )
    monkeypatch.setattr(
        stack,
        "cleanup_recovery_run",
        lambda *_args, **_kwargs: (
            (_ for _ in ()).throw(stack.StackError("cleanup exploded"))
        ),
    )
    real_append_event = stack.append_event

    def fail_terminal_append(action: str, **payload: object) -> dict:
        if action == "setup_blocked":
            raise OSError("terminal disk failure")
        return real_append_event(action, **payload)

    monkeypatch.setattr(stack, "append_event", fail_terminal_append)

    with pytest.raises(stack.StackError) as exc_info:
        stack.abort_recovery_run(
            "fault-1",
            "operator-abort",
            "primary reason",
        )

    detail = str(exc_info.value)
    assert "setup_failure=operator-abort: primary reason" in detail
    assert "cleanup helper: cleanup exploded" in detail
    assert "terminal disk failure" in detail
    assert stack.load_verified_events()[-1]["action"] == "abort_started"
    context = multiprocessing.get_context("fork")
    results = context.Queue()
    probe = context.Process(
        target=run_lock_probe_process,
        args=(str(stack.evidence_directory()), results),
        name="abort-lock-release-probe",
    )
    probe.start()
    assert joined_process_result(probe, results) == ("ok", "acquired")
    with pytest.raises(stack.StackError, match="incomplete abort"):
        stack.snapshot("sealed-after-abort")


def test_concurrent_setup_hold_and_abort_share_controller_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_identity, volume, identity = recovery_lifecycle(tmp_path, monkeypatch)
    context = multiprocessing.get_context("fork")
    hold_entered = context.Event()
    inspection_count = {"value": 0}

    def slow_assert_frozen(**_kwargs: object) -> dict:
        hold_entered.set()
        time.sleep(0.5)
        return identity

    monkeypatch.setattr(stack, "assert_frozen", slow_assert_frozen)
    monkeypatch.setattr(
        stack,
        "assert_isolated_compose",
        lambda _identity, *, run_identity: None,
    )
    def inspect_for_hold(service: str, **_kwargs: object) -> dict:
        inspection_count["value"] += 1
        return {
            "service": service,
            "exists": True,
            "running": inspection_count["value"] == 1,
            "container_id": f"{service}-container",
            "image_id": f"sha256:{service}-image",
        }

    monkeypatch.setattr(stack, "inspect_service", inspect_for_hold)
    monkeypatch.setattr(stack, "compose", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(
        stack,
        "confirmed_database_volume",
        lambda _run_identity, _expected: volume,
    )
    results = context.Queue()
    hold_process = context.Process(
        target=run_setup_hold_process,
        args=(run_identity["evidence_directory"], results),
        name="setup-hold",
    )
    abort_process = context.Process(
        target=run_abort_process,
        args=(run_identity["evidence_directory"], results),
        name="abort-during-hold",
    )

    hold_process.start()
    assert hold_entered.wait(timeout=2)
    abort_process.start()
    outcomes = [
        joined_process_result(hold_process, results),
        joined_process_result(abort_process, results),
    ]

    assert ("ok", "setup_service_held") in outcomes
    assert any(
        status == "error"
        and "controller transaction already active" in detail
        for status, detail in outcomes
    )
    events = stack.load_verified_events()
    assert events[-1]["action"] == "setup_service_held"
    assert stack.load_verified_events() == events


def test_concurrent_mark_transactions_allow_exactly_one_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_identity, volume, identity = recovery_lifecycle(tmp_path, monkeypatch)
    evidence_dir = run_identity["evidence_directory"]
    context = multiprocessing.get_context("fork")
    first_entered = context.Event()

    def slow_assert_frozen(**_kwargs: object) -> dict:
        first_entered.set()
        time.sleep(0.5)
        return identity

    monkeypatch.setattr(stack, "assert_frozen", slow_assert_frozen)
    monkeypatch.setattr(
        stack,
        "assert_isolated_compose",
        lambda _identity, *, run_identity: None,
    )
    monkeypatch.setattr(
        stack,
        "confirmed_database_volume",
        lambda _run_identity, _expected: volume,
    )
    results = context.Queue()
    first = context.Process(
        target=run_mark_process,
        args=(evidence_dir, "fault", results),
        name="first-mark",
    )
    second = context.Process(
        target=run_mark_process,
        args=(evidence_dir, "fault", results),
        name="second-mark",
    )

    first.start()
    assert first_entered.wait(timeout=2)
    second.start()

    outcomes = [
        joined_process_result(first, results),
        joined_process_result(second, results),
    ]
    assert [status for status, _detail in outcomes].count("ok") == 1
    assert [status for status, _detail in outcomes].count("error") == 1
    assert any(
        "controller transaction already active" in detail
        for status, detail in outcomes
        if status == "error"
    )
    events = stack.load_verified_events()
    assert [
        event["action"] for event in events
    ] == ["fresh_up_started", "fresh_up_completed", "fault_marked"]


def test_concurrent_fresh_up_transactions_allow_exactly_one_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_dir = (tmp_path / "concurrent-fresh").resolve()
    monkeypatch.setenv(stack.EVIDENCE_DIR_ENV, str(evidence_dir))
    context = multiprocessing.get_context("fork")
    first_entered = context.Event()
    identity = frozen_identity()
    volume_name = "forwin-v5-recovery-" + "7" * 32 + "-postgres-data"
    absent = {"name": volume_name, "exists": False}
    present = {
        "name": volume_name,
        "exists": True,
        "created_at": "2026-07-22T12:00:01+00:00",
        "fingerprint": stack.stable_hash(
            {
                "created_at": "2026-07-22T12:00:01+00:00",
                "name": volume_name,
            }
        ),
    }
    volume_calls = {"count": 0}

    def slow_isolated_compose(
        _identity: dict,
        *,
        run_identity: dict,
    ) -> None:
        first_entered.set()
        time.sleep(0.5)

    def fake_volume_observation(_run_identity: dict) -> dict:
        volume_calls["count"] += 1
        return absent if volume_calls["count"] == 1 else present

    monkeypatch.setattr(stack, "assert_frozen", lambda **_kwargs: identity)
    monkeypatch.setattr(stack, "assert_isolated_compose", slow_isolated_compose)
    monkeypatch.setattr(stack.secrets, "token_hex", lambda _size: "7" * 32)
    monkeypatch.setattr(stack, "database_volume_observation", fake_volume_observation)
    monkeypatch.setattr(
        stack,
        "compose",
        lambda *args, **_kwargs: "",
    )
    monkeypatch.setattr(
        stack,
        "wait_service",
        lambda _service, **_kwargs: {"running": True},
    )
    monkeypatch.setattr(
        stack,
        "stack_snapshot",
        lambda **kwargs: {
            "stage": "after" if kwargs.get("probe") else "before",
            "services": {},
        },
    )
    monkeypatch.setattr(
        stack,
        "now",
        lambda: "2026-07-22T12:00:00+00:00",
    )
    results = context.Queue()
    first = context.Process(
        target=run_fresh_up_process,
        args=(str(evidence_dir), results),
        name="first-fresh-up",
    )
    second = context.Process(
        target=run_fresh_up_process,
        args=(str(evidence_dir), results),
        name="second-fresh-up",
    )

    first.start()
    assert first_entered.wait(timeout=2)
    second.start()

    outcomes = [
        joined_process_result(first, results),
        joined_process_result(second, results),
    ]
    assert [status for status, _detail in outcomes].count("ok") == 1
    assert [status for status, _detail in outcomes].count("error") == 1
    assert any(
        "controller transaction already active" in detail
        for status, detail in outcomes
        if status == "error"
    )
    assert [
        event["action"] for event in stack.load_verified_events()
    ] == ["fresh_up_started", "fresh_up_completed"]


def test_marker_cannot_enter_while_destroy_transaction_is_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_identity, volume, identity = recovery_lifecycle(tmp_path, monkeypatch)
    stack.append_event(
        "fault_marked",
        fault_id="fault-1",
        fault_kind="publisher_captcha",
        fault_time="2026-07-22T12:00:00+00:00",
        identity=identity,
        run_identity=run_identity,
        database_volume=volume,
    )
    stack.append_event(
        "recovery_marked",
        fault_id="fault-1",
        fault_kind="publisher_captcha",
        recovery_time="2026-07-22T12:01:00+00:00",
        identity=identity,
        run_identity=run_identity,
        database_volume=volume,
    )
    context = multiprocessing.get_context("fork")
    destroy_entered = context.Event()

    def slow_assert_frozen(**_kwargs: object) -> dict:
        destroy_entered.set()
        time.sleep(0.5)
        return identity

    monkeypatch.setattr(stack, "assert_frozen", slow_assert_frozen)
    monkeypatch.setattr(
        stack,
        "assert_isolated_compose",
        lambda _identity, *, run_identity: None,
    )
    monkeypatch.setattr(
        stack,
        "confirmed_database_volume",
        lambda _run_identity, _expected: volume,
    )
    monkeypatch.setattr(
        stack,
        "stack_snapshot",
        lambda **_kwargs: {"observed_at": "before", "services": {}},
    )
    monkeypatch.setattr(stack, "compose", lambda *args, **_kwargs: "")
    monkeypatch.setattr(
        stack,
        "destroyed_service_inventory",
        lambda _run_identity: {
            service: {"exists": False, "running": False}
            for service in stack.SERVICES
        },
    )
    monkeypatch.setattr(
        stack,
        "database_volume_observation",
        lambda _run_identity: {
            "name": run_identity["database_volume_name"],
            "exists": False,
        },
    )
    results = context.Queue()
    destroy_process = context.Process(
        target=run_destroy_process,
        args=(run_identity["evidence_directory"], results),
        name="destroy",
    )
    marker_process = context.Process(
        target=run_mark_process,
        args=(run_identity["evidence_directory"], "recovery", results),
        name="marker-during-destroy",
    )

    destroy_process.start()
    assert destroy_entered.wait(timeout=2)
    marker_process.start()

    outcomes = [
        joined_process_result(destroy_process, results),
        joined_process_result(marker_process, results),
    ]
    assert ("ok", "destroyed") in outcomes
    assert any(
        status == "error"
        and "controller transaction already active" in detail
        for status, detail in outcomes
    )
    assert stack.load_verified_events()[-1]["action"] == "destroyed"


def test_active_transaction_does_not_block_unrelated_recovery_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_run, first_volume, identity = recovery_lifecycle(
        tmp_path,
        monkeypatch,
        fault_id="fault-1",
    )
    stack.append_event(
        "fault_marked",
        fault_id="fault-1",
        fault_kind="publisher_captcha",
        fault_time="2026-07-22T12:00:00+00:00",
        identity=identity,
        run_identity=first_run,
        database_volume=first_volume,
    )
    stack.append_event(
        "recovery_marked",
        fault_id="fault-1",
        fault_kind="publisher_captcha",
        recovery_time="2026-07-22T12:01:00+00:00",
        identity=identity,
        run_identity=first_run,
        database_volume=first_volume,
    )
    second_run, second_volume, _identity = recovery_lifecycle(
        tmp_path,
        monkeypatch,
        fault_id="fault-2",
    )
    context = multiprocessing.get_context("fork")
    destroy_entered = context.Event()

    def conditional_slow_assert_frozen(**_kwargs: object) -> dict:
        if stack.evidence_directory() == Path(
            first_run["evidence_directory"]
        ):
            destroy_entered.set()
            time.sleep(0.5)
        return identity

    monkeypatch.setattr(stack, "assert_frozen", conditional_slow_assert_frozen)
    monkeypatch.setattr(
        stack,
        "assert_isolated_compose",
        lambda _identity, *, run_identity: None,
    )
    monkeypatch.setattr(
        stack,
        "confirmed_database_volume",
        lambda _run_identity, expected: expected,
    )
    monkeypatch.setattr(
        stack,
        "stack_snapshot",
        lambda **_kwargs: {"observed_at": "before", "services": {}},
    )
    monkeypatch.setattr(stack, "compose", lambda *args, **_kwargs: "")
    monkeypatch.setattr(
        stack,
        "destroyed_service_inventory",
        lambda _run_identity: {
            service: {"exists": False, "running": False}
            for service in stack.SERVICES
        },
    )
    monkeypatch.setattr(
        stack,
        "database_volume_observation",
        lambda run_identity: {
            "name": run_identity["database_volume_name"],
            "exists": False,
        },
    )
    results = context.Queue()
    destroy_process = context.Process(
        target=run_destroy_process,
        args=(first_run["evidence_directory"], results),
        name="unrelated-run-destroy",
    )

    destroy_process.start()
    assert destroy_entered.wait(timeout=2)
    monkeypatch.setenv(
        stack.EVIDENCE_DIR_ENV,
        second_run["evidence_directory"],
    )
    marker = stack.mark_fault("publisher_captcha", "fault", "fault-2")

    assert marker["action"] == "fault_marked"
    assert joined_process_result(destroy_process, results) == ("ok", "destroyed")
    first_lock = stack.controller_lock_path(
        Path(first_run["evidence_directory"])
    )
    second_lock = stack.controller_lock_path(
        Path(second_run["evidence_directory"])
    )
    assert first_lock != second_lock
    assert first_lock.parent == Path(first_run["evidence_directory"]).parent
    assert second_lock.parent == Path(second_run["evidence_directory"]).parent
    assert not first_lock.is_relative_to(first_run["evidence_directory"])
    assert not second_lock.is_relative_to(second_run["evidence_directory"])
    assert {
        path.name
        for path in Path(second_run["evidence_directory"]).iterdir()
    } == {"stack-events.jsonl"}
    assert second_volume["exists"] is True


@pytest.mark.parametrize(
    "fault_kind",
    [
        "",
        "publisher_backend_unavailable",
        "publisher_unknown_risk",
    ],
)
def test_typed_marker_rejects_non_risk_fault_kind(
    fault_kind: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(stack.EVIDENCE_DIR_ENV, str((tmp_path / "fault").resolve()))
    with pytest.raises(stack.StackError, match="typed publisher risk"):
        stack.mark_fault(fault_kind, "fault", "fault-1")


def test_destroy_requires_completed_recovery_and_is_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_identity, volume, identity = recovery_lifecycle(tmp_path, monkeypatch)
    stack.append_event(
        "fault_marked",
        fault_id="fault-1",
        fault_kind="publisher_mfa",
        fault_time="2026-07-22T12:00:00+00:00",
        identity=identity,
        run_identity=run_identity,
        database_volume=volume,
    )
    stack.append_event(
        "recovery_marked",
        fault_id="fault-1",
        fault_kind="publisher_mfa",
        recovery_time="2026-07-22T12:01:00+00:00",
        identity=identity,
        run_identity=run_identity,
        database_volume=volume,
    )
    absent = {"name": run_identity["database_volume_name"], "exists": False}
    volume_observations = iter([volume, absent])
    compose_calls: list[tuple[str, ...]] = []

    monkeypatch.setattr(stack, "assert_frozen", lambda: identity)
    monkeypatch.setattr(
        stack,
        "assert_isolated_compose",
        lambda _identity, *, run_identity: None,
    )
    monkeypatch.setattr(
        stack,
        "database_volume_observation",
        lambda _run_identity: next(volume_observations),
        raising=False,
    )
    monkeypatch.setattr(
        stack,
        "stack_snapshot",
        lambda **_kwargs: {"stage": "before", "services": {}},
    )
    monkeypatch.setattr(
        stack,
        "compose",
        lambda *args, run_identity: compose_calls.append(tuple(args)) or "",
    )
    monkeypatch.setattr(
        stack,
        "destroyed_service_inventory",
        lambda _run_identity: {
            service: {"exists": False, "running": False}
            for service in stack.SERVICES
        },
        raising=False,
    )
    monkeypatch.setattr(
        stack,
        "now",
        lambda: "2026-07-22T12:02:00+00:00",
    )

    stack.destroy()

    destroyed = stack.load_verified_events()[-1]
    assert destroyed["action"] == "destroyed"
    assert destroyed["fault_id"] == "fault-1"
    assert destroyed["run_identity"] == run_identity
    assert destroyed["database_volume_before"] == volume
    assert destroyed["database_volume"] == absent
    assert destroyed["after"]["services"] == {
        service: {"exists": False, "running": False}
        for service in stack.SERVICES
    }
    assert compose_calls == [("down", "--volumes", "--remove-orphans")]

    with pytest.raises(stack.StackError, match="terminal"):
        stack.append_event("snapshot", label="too-late")


def test_destroy_refuses_missing_fresh_completion_without_compose(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_dir = (tmp_path / "fault-1").resolve()
    monkeypatch.setenv(stack.EVIDENCE_DIR_ENV, str(evidence_dir))
    run_identity = stack.new_recovery_run_identity(
        "fault-1",
        run_id="f" * 32,
        directory=evidence_dir,
    )
    stack.append_event(
        "fresh_up_started",
        fault_id="fault-1",
        requested_at="2026-07-22T11:58:00+00:00",
        identity={"source_sha": SOURCE_SHA},
        run_identity=run_identity,
        database_volume={
            "name": run_identity["database_volume_name"],
            "exists": False,
        },
    )
    compose_calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        stack,
        "compose",
        lambda *args, **_kwargs: compose_calls.append(tuple(args)) or "",
    )

    with pytest.raises(stack.StackError, match="incomplete fresh-up"):
        stack.destroy()

    assert compose_calls == []


def test_destroy_rejects_a_mixed_fault_recovery_pair_before_compose(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_identity, volume, identity = recovery_lifecycle(tmp_path, monkeypatch)
    stack.append_event(
        "fault_service_stopped",
        fault_id="fault-1",
        service="qdrant",
        fault_time="2026-07-22T12:00:00+00:00",
        identity=identity,
        run_identity=run_identity,
        database_volume=volume,
    )
    stack.append_event(
        "recovery_marked",
        fault_id="fault-1",
        fault_kind="publisher_mfa",
        recovery_time="2026-07-22T12:01:00+00:00",
        identity=identity,
        run_identity=run_identity,
        database_volume=volume,
    )
    monkeypatch.setattr(
        stack,
        "assert_frozen",
        lambda **_kwargs: pytest.fail("destroy read harness before pair validation"),
    )

    with pytest.raises(stack.StackError, match="matching fault recovery"):
        stack.destroy()


def test_active_run_rejects_unparseable_database_volume_creation_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_dir = (tmp_path / "fault-1").resolve()
    monkeypatch.setenv(stack.EVIDENCE_DIR_ENV, str(evidence_dir))
    run_identity = stack.new_recovery_run_identity(
        "fault-1",
        run_id="1" * 32,
        directory=evidence_dir,
    )
    volume_name = run_identity["database_volume_name"]
    stack.append_event(
        "fresh_up_started",
        fault_id="fault-1",
        requested_at="2026-07-22T11:58:00+00:00",
        identity={"source_sha": SOURCE_SHA},
        run_identity=run_identity,
        database_volume={"name": volume_name, "exists": False},
    )
    stack.append_event(
        "fresh_up_completed",
        fault_id="fault-1",
        requested_at="2026-07-22T11:58:00+00:00",
        identity={"source_sha": SOURCE_SHA},
        run_identity=run_identity,
        database_volume={
            "name": volume_name,
            "exists": True,
            "created_at": "not-a-time",
            "fingerprint": stack.stable_hash(
                {"created_at": "not-a-time", "name": volume_name}
            ),
        },
    )

    with pytest.raises(stack.StackError, match="creation time"):
        stack.require_active_recovery_run("fault-1")


def test_v1_up_records_migration_schema_role_and_embedding_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(stack.EVIDENCE_DIR_ENV, str((tmp_path / "v1").resolve()))
    identity = frozen_identity()
    snapshots = [
        {"stage": "before", "services": {}},
        {"stage": "after", "services": {"forwin": {"running": True}}},
    ]
    compose_calls: list[tuple[str, ...]] = []
    events: list[tuple[str, dict]] = []
    waited: list[str] = []
    isolated_checks: list[dict] = []
    migration = {
        "steps": [
            {"name": name, "exit_code": 0}
            for name in stack.V1_MIGRATION_STEPS
        ],
        "final_revision": "0001_v5_baseline",
    }
    stale = {
        "role": "generation-worker",
        "injected_revision": "v1_stale_revision",
        "startup_exit_code": 1,
        "expected_error_observed": True,
        "restored_revision": "0001_v5_baseline",
        "post_restore_exit_code": 0,
    }
    embedding = {
        "backend": "gateway",
        "required": True,
        "configured_dims": 384,
        "metadata_dims": 384,
        "vector_count": 1,
        "vector_dims": [384],
    }

    monkeypatch.setattr(stack, "assert_frozen", lambda **_kwargs: identity)
    monkeypatch.setattr(
        stack,
        "assert_isolated_compose",
        lambda checked_identity: isolated_checks.append(checked_identity),
        raising=False,
    )
    monkeypatch.setattr(stack, "reject_terminal_evidence_directory", lambda: None)
    monkeypatch.setattr(stack, "require_new_evidence_run", lambda: None)
    monkeypatch.setattr(
        stack,
        "stack_snapshot",
        lambda **_kwargs: snapshots.pop(0),
    )
    monkeypatch.setattr(
        stack,
        "append_event",
        lambda action, **payload: events.append((action, payload)) or payload,
    )
    monkeypatch.setattr(
        stack,
        "compose",
        lambda *args: compose_calls.append(tuple(args)) or "",
    )
    monkeypatch.setattr(
        stack,
        "wait_service",
        lambda service: waited.append(service) or {"running": True},
    )
    monkeypatch.setattr(stack, "run_v1_migration_cycle", lambda: migration)
    monkeypatch.setattr(
        stack,
        "verify_v1_stale_schema_failfast",
        lambda final_revision: stale,
    )
    monkeypatch.setattr(stack, "run_v1_embedding_smoke", lambda: embedding)
    monkeypatch.setattr(stack.secrets, "token_hex", lambda _size: "run-1")

    stack.v1_up()

    assert isolated_checks == [identity]
    assert events[0][0] == "v1_fresh_up_started"
    assert events[-1] == (
        "v1_preflight_completed",
        {
            "run_id": "run-1",
            "identity": identity,
            "migration_cycle": migration,
            "stale_schema_failfast": stale,
            "embedding": embedding,
            "after": {"stage": "after", "services": {"forwin": {"running": True}}},
        },
    )
    assert compose_calls[:2] == [
        ("down", "--volumes", "--remove-orphans"),
        ("up", "--detach", "postgres", "qdrant", "minio"),
    ]
    assert set(waited) == set(stack.SERVICES)


def test_v1_embedding_smoke_rejects_wrong_vector_dimensions() -> None:
    with pytest.raises(stack.StackError, match="vector dimensions"):
        stack.validate_v1_embedding_smoke(
            {
                "backend": "gateway",
                "required": True,
                "base_url": "http://10.0.0.150:8080",
                "peer_ip": "10.0.0.150",
                "configured_dims": 384,
                "metadata_dims": 384,
                "vector_count": 1,
                "vector_dims": [128],
            }
        )


def test_v1_embedding_smoke_rejects_non_lan_gateway() -> None:
    with pytest.raises(stack.StackError, match="private LAN"):
        stack.validate_v1_embedding_smoke(
            {
                "backend": "gateway",
                "required": True,
                "base_url": "https://embedding.example.com",
                "peer_ip": "10.0.0.150",
                "configured_dims": 384,
                "metadata_dims": 384,
                "vector_count": 1,
                "vector_dims": [384],
            }
        )


def test_v1_embedding_smoke_rejects_unexpected_tcp_peer() -> None:
    with pytest.raises(stack.StackError, match="TCP peer"):
        stack.validate_v1_embedding_smoke(
            {
                "backend": "gateway",
                "required": True,
                "base_url": "http://10.0.0.150:8080",
                "peer_ip": "10.0.0.151",
                "configured_dims": 384,
                "metadata_dims": 384,
                "vector_count": 1,
                "vector_dims": [384],
            }
        )


def test_v1_embedding_probe_disables_environment_proxies() -> None:
    assert "trust_env=False" in stack._V1_EMBEDDING_SCRIPT
    assert "peer_ip" in stack._V1_EMBEDDING_SCRIPT


def test_v1_alembic_revision_parser_accepts_head_output() -> None:
    assert (
        stack.parse_v1_alembic_revision("0001_v5_baseline (head)\n")
        == "0001_v5_baseline"
    )
