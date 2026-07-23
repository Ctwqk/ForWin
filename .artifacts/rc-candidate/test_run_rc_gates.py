from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).with_name("run_rc_gates.py")
SPEC = importlib.util.spec_from_file_location("run_rc_gates", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
gates = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gates)

SOURCE_SHA = "d" * 40


def test_gate_environment_rejects_execution_control() -> None:
    with pytest.raises(gates.GateError, match="PYTEST_ADDOPTS"):
        gates.gate_environment(
            {
                "HOME": "/tmp/home",
                "PATH": "/usr/bin",
                "PYTEST_ADDOPTS": "-k one_test",
            }
        )


def test_gate_environment_is_minimal_and_offline() -> None:
    environment = gates.gate_environment(
        {
            "HOME": "/tmp/home",
            "LANG": "en_US.UTF-8",
            "PATH": "/usr/bin",
            "FORWIN_TEST_DATABASE_URL": "postgresql://test",
            "FORWIN_E2E_REAL_API": "1",
            "MINIMAX_API_KEY": "must-not-leak",
            "UNRELATED": "drop",
            "VIRTUAL_ENV": "/tmp/untrusted-venv",
        }
    )

    assert environment == {
        "FORWIN_TEST_DATABASE_URL": "postgresql://test",
        "HOME": "/tmp/home",
        "LANG": "en_US.UTF-8",
        "PATH": "/usr/bin",
        "PYTHONNOUSERSITE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "UV_OFFLINE": "1",
    }


def test_gate_commands_pin_tools_and_pytest_plugins() -> None:
    steps = {step["name"]: step for step in gates.gate_steps()}

    assert steps["ruff"]["command"] == [
        "uvx",
        "--offline",
        f"ruff=={gates.RUFF_VERSION}",
        "check",
        "forwin",
        "tests",
    ]
    assert steps["full-suite"]["command"][:9] == [
        "uv",
        "run",
        "--offline",
        "python",
        "-m",
        "pytest",
        "-p",
        "pytest_asyncio.plugin",
        "-q",
    ]


def test_gate_step_uses_sanitized_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    class Completed:
        returncode = 0

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return Completed()

    monkeypatch.setattr(gates, "gate_environment", lambda: {"SAFE": "1"})
    monkeypatch.setattr(gates.subprocess, "run", fake_run)

    result = gates.run_step(
        {
            "name": "diff-check",
            "kind": "command",
            "command": ["git", "diff", "--check"],
        },
        tmp_path,
    )

    assert result["passed"] is True
    assert captured["env"] == {"SAFE": "1"}


def test_frozen_identity_is_derived_from_candidate_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_tag = "forwin-v5-rc-runtime:new"
    browser_tag = "forwin-v5-rc-browser:new"
    runtime_id = "sha256:" + "1" * 64
    browser_id = "sha256:" + "2" * 64
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
                },
            }
        ),
        encoding="utf-8",
    )

    def fake_command(*command: str) -> str:
        if command == ("git", "rev-parse", "HEAD"):
            return SOURCE_SHA
        if command == ("git", "status", "--porcelain=v1", "--untracked-files=no"):
            return ""
        if command == ("git", "rev-parse", "HEAD^{tree}"):
            return "e" * 40
        if command[:3] == ("docker", "image", "inspect"):
            tag = command[3]
            image_id = runtime_id if tag == runtime_tag else browser_id
            return json.dumps(
                [
                    {
                        "Id": image_id,
                        "Config": {
                            "Labels": {
                                "org.opencontainers.image.revision": SOURCE_SHA
                            }
                        },
                    }
                ]
            )
        raise AssertionError(command)

    monkeypatch.setattr(gates, "command_output", fake_command)

    identity = gates.assert_frozen(manifest_path)

    assert identity["source_sha"] == SOURCE_SHA
    assert identity["runtime_image"]["tag"] == runtime_tag
    assert identity["runtime_image"]["image_id"] == runtime_id
    assert identity["browser_image"]["tag"] == browser_tag
    assert identity["browser_image"]["image_id"] == browser_id


def test_resume_rejects_runner_code_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = {"source_sha": SOURCE_SHA}
    output = tmp_path / "gate-output"
    output.mkdir()
    (output / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "identity": identity,
                "runner": {"path": str(MODULE_PATH), "sha256": "stale"},
                "steps": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(gates, "assert_frozen", lambda _path: identity)
    monkeypatch.setattr(gates, "gate_steps", lambda: [])
    monkeypatch.setattr(gates, "selected_steps", lambda _names: [])
    args = argparse.Namespace(
        rc_manifest=tmp_path / "candidate.json",
        output_dir=output,
        resume=True,
        step=[],
    )

    with pytest.raises(gates.GateError, match="runner"):
        gates.run_gates(args)


def test_resume_refuses_sealed_release_gate_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = {"source_sha": SOURCE_SHA}
    output = tmp_path / "gate-output"
    output.mkdir()
    (output / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "identity": identity,
                "runner": {
                    "path": str(MODULE_PATH.resolve()),
                    "sha256": gates.sha256_file(MODULE_PATH),
                },
                "steps": [],
                "release_gate_passed": True,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(gates, "assert_frozen", lambda _path: identity)
    monkeypatch.setattr(gates, "gate_steps", lambda: [])
    monkeypatch.setattr(gates, "selected_steps", lambda _names: [])
    args = argparse.Namespace(
        rc_manifest=tmp_path / "candidate.json",
        output_dir=output,
        resume=True,
        step=[],
    )

    with pytest.raises(gates.GateError, match="already passed"):
        gates.run_gates(args)


def test_v5_gate_includes_publisher_backend_reclaim_contract() -> None:
    assert "tests/test_publisher_runtime_covers.py" in gates.V5_TESTS
    assert "tests/test_publisher_worker_cli.py" in gates.V5_TESTS
    assert "tests/test_runtime_worker_roles.py" in gates.V5_TESTS
