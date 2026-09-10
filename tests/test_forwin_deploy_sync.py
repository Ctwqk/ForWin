from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
REVISION = "a" * 40
APP_ID = "sha256:" + "1" * 64
BROWSER_ID = "sha256:" + "2" * 64
ROLES = [
    "app",
    "mcp",
    "generation-worker",
    "publisher-worker",
    "outbox-worker",
    "publisher-browser",
]


def load_controller():
    path = ROOT / "deploy/swarm/forwin_release.py"
    assert path.exists(), "ForWin release controller is not implemented"
    spec = importlib.util.spec_from_file_location("forwin_release_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_command_timeout_reports_unknown_outcome_without_echoing_arguments(monkeypatch):
    module = load_controller()

    def timeout(argv, **kwargs):
        assert kwargs.get("timeout") == 60
        raise subprocess.TimeoutExpired(argv, 60)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(RuntimeError, match="unknown") as error:
        module.execute(["ssh", "private-argument"])
    assert "private-argument" not in str(error.value)


class FakeCommands:
    """Only the Docker/SSH process boundary is fake; release decisions are real."""

    def __init__(self, fail="", bad_revision=False):
        self.calls = []
        self.fail = fail
        self.bad_revision = bad_revision
        self.jobs = {}
        self.specs = {}
        for role in ROLES:
            name = f"forwin-{role}-swarm"
            self.specs[name] = {
                "ID": name,
                "Version": {"Index": 12},
                "Spec": {
                    "Name": name,
                    "Mode": {"Replicated": {"Replicas": 1}},
                    "TaskTemplate": {
                        "ContainerSpec": {
                            "Image": "old-image",
                            "Env": [
                                "PATH=/usr/bin",
                                "FORWIN_DATABASE_URL=private-test-value",
                            ],
                            "Mounts": [
                                {
                                    "Type": "volume",
                                    "Source": "data",
                                    "Target": "/app/data",
                                }
                            ],
                        },
                        "Networks": [{"Target": "network"}],
                    },
                },
            }

    def __call__(self, argv):
        self.calls.append(list(argv))
        if argv[0] == "ssh":
            command = argv[-1]
            if self.fail == "transport" and "limactl" not in command:
                raise RuntimeError("host Docker unavailable")
            if "image inspect" in command:
                browser = "forwin-publisher-browser:" in command
                return json.dumps(
                    [
                        {
                            "Id": BROWSER_ID if browser else APP_ID,
                            "Config": {
                                "Labels": {
                                    "org.opencontainers.image.revision": "unknown"
                                    if self.bad_revision
                                    else REVISION
                                },
                                "Env": [
                                    f"FORWIN_SOURCE_REVISION={REVISION}",
                                    "PATH=/app/.venv/bin:/usr/bin",
                                ],
                            },
                        }
                    ]
                )
            if "info" in command:
                return "other-node" if self.fail == "node" else "node"
            if " run " in command:
                assert "--network none" in command
                assert "--read-only" in command
                assert "--entrypoint /app/.venv/bin/python" in command
                assert APP_ID in command or BROWSER_ID in command
                assert "--env" not in command and "--mount" not in command
                return ""
            if "container inspect" in command:
                browser = "publisher-browser" in command
                return json.dumps(
                    [
                        {
                            "Image": BROWSER_ID if browser else APP_ID,
                            "Config": {
                                "Env": [
                                    "PATH=/app/.venv/bin:/usr/bin",
                                    f"FORWIN_SOURCE_REVISION={REVISION}",
                                ],
                                "Labels": {
                                    "org.opencontainers.image.revision": REVISION
                                },
                            },
                        }
                    ]
                )
            if "exec" in command:
                if self.fail == "health" and "publisher-browser" in command:
                    raise RuntimeError("injected health failure")
                return ""
            raise AssertionError(argv)
        assert argv[0] == "docker", argv
        if argv[1:3] == ["service", "inspect"]:
            return json.dumps([self.specs[name] for name in argv[3:]])
        if argv[1:3] == ["service", "scale"]:
            for item in argv[3:]:
                if "=" in item and not item.startswith("--"):
                    name, replicas = item.split("=")
                    self.specs[name]["Spec"]["Mode"]["Replicated"]["Replicas"] = int(
                        replicas
                    )
            return ""
        if argv[1:3] == ["service", "ps"]:
            name = argv[-1]
            if name in self.jobs:
                return name + "-task"
            return (
                name + "-task"
                if self.specs[name]["Spec"]["Mode"]["Replicated"]["Replicas"]
                else ""
            )
        if argv[1] == "inspect":
            tasks = []
            for item in argv[2:]:
                name = item.removesuffix("-task")
                if name in self.jobs:
                    phase = self.jobs[name]
                    tasks.append(
                        {
                            "Status": {
                                "State": "failed" if phase == self.fail else "complete",
                                "ContainerStatus": {
                                    "ExitCode": 1 if phase == self.fail else 0
                                },
                            },
                            "NodeID": "node",
                            "DesiredState": "shutdown",
                        }
                    )
                else:
                    tasks.append(
                        {
                            "Status": {
                                "State": "running",
                                "ContainerStatus": {"ContainerID": name},
                            },
                            "NodeID": "node",
                            "DesiredState": "running",
                        }
                    )
            return json.dumps(tasks)
        if argv[1:3] == ["service", "create"]:
            # Maintenance must inherit only config/mounts and run without API/worker startup.
            name = argv[argv.index("--name") + 1]
            phase = argv[argv.index("--phase") + 1]
            assert "--no-healthcheck" in argv
            assert argv[argv.index("--restart-condition") + 1] == "none"
            assert "/app/.venv/bin/python" in argv
            assert not any(
                self.specs[s]["Spec"]["Mode"]["Replicated"]["Replicas"]
                for s in self.specs
            )
            env_file = Path(argv[argv.index("--env-file") + 1])
            assert env_file.stat().st_mode & 0o777 == 0o600
            assert "PATH=" not in env_file.read_text()
            assert "FORWIN_SOURCE_REVISION=" not in env_file.read_text()
            self.jobs[name] = phase
            return name
        if argv[1:3] == ["service", "update"]:
            name = argv[-1]
            spec = self.specs[name]["Spec"]
            assert spec["Mode"]["Replicated"]["Replicas"] == 0
            assert argv[argv.index("--env-rm") + 1] == "PATH"
            spec["TaskTemplate"]["ContainerSpec"]["Image"] = argv[
                argv.index("--image") + 1
            ]
            return ""
        if argv[1:3] == ["service", "rm"]:
            return ""
        if argv[1:3] == ["service", "logs"]:
            phase = self.jobs[argv[-1]]
            if self.fail == "missing_proof":
                return ""
            return "FORWIN_MAINT_RESULT " + json.dumps(
                {
                    "phase": phase,
                    "source_revision": REVISION,
                    "schema_before": ["old"],
                    "schema_after": ["new"],
                    "backup": "forwin-backup-test" if phase == "backup" else None,
                }
            )
        raise AssertionError(argv)


def release(tmp_path, *, fail="", bad_revision=False, attestation=True):
    module = load_controller()
    commands = FakeCommands(fail, bad_revision)
    evidence = tmp_path / "forwin-quiescence.json"
    if attestation:
        evidence.write_text(
            json.dumps(
                {
                    "source_revision": REVISION,
                    "expires_at": time.time() + 600,
                    "generation_paused": True,
                    "publisher_idle": True,
                    "admission_closed": True,
                    "automation_paused": True,
                    "evidence": "MCP tasks idle; operator closed admission and paused automation",
                    "service_versions": {name: 12 for name in commands.specs},
                }
            )
        )
        evidence.chmod(0o600)
    controller = module.Release(
        revision=REVISION,
        app_image="forwin-forwin:deploy-" + REVISION[:12],
        browser_image="forwin-publisher-browser:deploy-" + REVISION[:12],
        state_dir=tmp_path,
        command=commands,
        poll_interval=0,
        timeout=1,
    )
    return controller, commands


def test_release_validates_identity_stops_backs_up_migrates_then_updates_all_roles(
    tmp_path,
):
    controller, commands = release(tmp_path)
    controller.run()
    state = json.loads((tmp_path / "forwin-release.json").read_text())
    assert state["phase"] == "deployed"
    assert state["source_revision"] == REVISION
    assert state["images"] == {"app": APP_ID, "browser": BROWSER_ID}
    assert state["maintenance"]["migrate"]["schema_after"] == ["new"]
    assert list(commands.jobs.values()) == ["backup", "migrate", "verify"]
    creates = [c for c in commands.calls if c[1:3] == ["service", "create"]]
    assert all(c[c.index("/app/.venv/bin/python") - 1] == APP_ID for c in creates)
    assert len([c for c in commands.calls if c[1:3] == ["service", "update"]]) == 6
    for call in commands.calls:
        if call[1:3] == ["service", "update"]:
            expected = (
                BROWSER_ID if call[-1].endswith("publisher-browser-swarm") else APP_ID
            )
            assert call[call.index("--image") + 1] == expected
    assert all(
        s["Spec"]["Mode"]["Replicated"]["Replicas"] == 1
        for s in commands.specs.values()
    )
    assert (tmp_path / "forwin-release.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "fail,phase",
    [
        ("backup", "quiesced"),
        ("migrate", "migration_started"),
        ("verify", "migrated"),
        ("health", "migrated"),
    ],
)
def test_failure_keeps_roles_stopped_without_old_image_rollback(tmp_path, fail, phase):
    controller, commands = release(tmp_path, fail=fail)
    with pytest.raises(RuntimeError):
        controller.run()
    state = json.loads((tmp_path / "forwin-release.json").read_text())
    assert state["phase"] == phase
    assert state["error"]
    assert all(
        s["Spec"]["Mode"]["Replicated"]["Replicas"] == 0
        for s in commands.specs.values()
    )
    assert not any("rollback" in call for call in commands.calls)
    if fail == "backup":
        assert list(commands.jobs.values()) == ["backup"]
    with pytest.raises(RuntimeError, match="unfinished"):
        controller.run()


@pytest.mark.parametrize(
    "kind", ["missing", "expired", "wrong_revision", "spec_changed", "not_paused"]
)
def test_missing_or_stale_quiescence_evidence_cannot_stop_services(tmp_path, kind):
    controller, commands = release(tmp_path, attestation=kind != "missing")
    path = tmp_path / "forwin-quiescence.json"
    if path.exists():
        value = json.loads(path.read_text())
        if kind == "expired":
            value["expires_at"] = 0
        if kind == "wrong_revision":
            value["source_revision"] = "b" * 40
        if kind == "spec_changed":
            value["service_versions"]["forwin-app-swarm"] = 11
        if kind == "not_paused":
            value["automation_paused"] = False
        path.write_text(json.dumps(value))
    with pytest.raises(RuntimeError, match="quiescence"):
        controller.run()
    assert not any(c[1:3] == ["service", "scale"] for c in commands.calls)


def test_unknown_image_revision_fails_before_downtime(tmp_path):
    controller, commands = release(tmp_path, bad_revision=True)
    with pytest.raises(RuntimeError, match="revision"):
        controller.run()
    assert not any(c[1:3] == ["service", "scale"] for c in commands.calls)


def test_build_daemon_must_match_the_service_node_before_downtime(tmp_path):
    controller, commands = release(tmp_path, fail="node")
    with pytest.raises(RuntimeError, match="node"):
        controller.run()
    assert not any(c[1:3] == ["service", "scale"] for c in commands.calls)


def test_colima_image_and_runtime_inspection_fallback_is_verified(tmp_path):
    controller, commands = release(tmp_path, fail="transport")
    controller.run()
    assert (
        json.loads((tmp_path / "forwin-release.json").read_text())["phase"]
        == "deployed"
    )
    assert any("limactl" in c[-1] and "image inspect" in c[-1] for c in commands.calls)


def test_old_service_source_revision_is_removed_before_maintenance_and_startup(
    tmp_path,
):
    controller, commands = release(tmp_path)
    for row in commands.specs.values():
        row["Spec"]["TaskTemplate"]["ContainerSpec"]["Env"].append(
            "FORWIN_SOURCE_REVISION=old-image"
        )
    controller.run()
    updates = [c for c in commands.calls if c[1:3] == ["service", "update"]]
    assert len(updates) == 6
    assert all("FORWIN_SOURCE_REVISION" in command for command in updates)


def test_zero_exit_without_maintenance_proof_cannot_advance_to_migration(tmp_path):
    controller, commands = release(tmp_path, fail="missing_proof")
    with pytest.raises(RuntimeError, match="proof"):
        controller.run()
    assert list(commands.jobs.values()) == ["backup"]
    assert (
        json.loads((tmp_path / "forwin-release.json").read_text())["phase"]
        == "quiesced"
    )


@pytest.mark.parametrize("fallback", [False, True])
def test_shell_build_passes_full_revision_on_primary_and_lima_paths(tmp_path, fallback):
    extension = ROOT / "deploy/swarm/deploy-sync-extension.sh"
    assert extension.exists(), "ForWin shell extension is not implemented"
    log = tmp_path / "commands"
    script = """
set -euo pipefail
source "$1"
log() { :; }
remote_sh() {
  printf '%s\n' "$2" >> "$2_UNUSED"
  if [ "$FALLBACK" = yes ] && [[ "$2" != *limactl* ]]; then return 1; fi
}
forwin_build_image image forwin-runtime "$REVISION"
""".replace('"$2_UNUSED"', '"$COMMAND_LOG"')
    env = {
        **os.environ,
        "COMMAND_LOG": str(log),
        "FALLBACK": "yes" if fallback else "no",
        "REVISION": REVISION,
    }
    result = subprocess.run(
        ["bash", "-c", script, "test", str(extension)],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    commands = log.read_text().splitlines()
    assert len(commands) == (2 if fallback else 1)
    assert all(
        f"--build-arg FORWIN_SOURCE_REVISION={REVISION}" in command
        for command in commands
    )
    assert all("--target forwin-runtime" in command for command in commands)


def test_failed_deploy_cannot_write_source_marker_even_inside_shell_conditional(
    tmp_path,
):
    extension = ROOT / "deploy/swarm/deploy-sync-extension.sh"
    assert extension.exists(), "ForWin shell extension is not implemented"
    marker = tmp_path / "marker"
    script = """
set -euo pipefail
source "$1"
repo_commit() { printf '%s' "$REVISION"; }
sync_stage_to_target() { return 0; }
forwin_deploy_project() { return 1; }
write_target_marker() { touch "$MARKER"; }
if forwin_sync_and_deploy forwin ForWin master host target; then exit 4; fi
"""
    env = {**os.environ, "REVISION": REVISION, "MARKER": str(marker)}
    result = subprocess.run(
        ["bash", "-c", script, "test", str(extension)],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert not marker.exists()


@pytest.mark.parametrize("active", [False, True])
def test_maintenance_runs_real_unified_migration_in_isolated_legacy_postgres(
    tmp_path, active
):
    from tests.test_legacy_forward_migration import _insert_legacy, _legacy_database

    script = ROOT / "scripts/forwin_deploy_maintenance.py"
    assert script.exists(), "Candidate maintenance entry is not implemented"
    engine = _legacy_database("deploy-maintenance")
    if active:
        with engine.begin() as conn:
            _insert_legacy(conn, "projects", "p")
            _insert_legacy(
                conn, "generation_tasks", "t", project_id="p", status="queued"
            )
    env = os.environ.copy()
    env.update(
        FORWIN_DATABASE_URL=engine.url.render_as_string(hide_password=False),
        FORWIN_SOURCE_REVISION=REVISION,
    )
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--phase",
            "migrate",
            "--release-id",
            "test-release",
            "--source-revision",
            REVISION,
        ],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == (1 if active else 0), result.stderr
    if not active:
        proof_line = next(
            line
            for line in result.stdout.splitlines()
            if line.startswith("FORWIN_MAINT_RESULT ")
        )
        proof = json.loads(proof_line.removeprefix("FORWIN_MAINT_RESULT "))
        assert proof["schema_before"] == ["0001_v5_baseline"]
        verify = subprocess.run(
            [
                sys.executable,
                str(script),
                "--phase",
                "verify",
                "--release-id",
                "test-release",
                "--source-revision",
                REVISION,
            ],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        assert verify.returncode == 0, verify.stderr
    from alembic.script import ScriptDirectory

    from forwin.models.base import alembic_config

    head = ScriptDirectory.from_config(
        alembic_config(engine.url.render_as_string(hide_password=False))
    ).get_current_head()
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0001_v5_baseline" if active else head
        )
    engine.dispose()


def test_missing_runtime_dependency_fails_before_stopping_old_roles(tmp_path):
    controller, commands = release(tmp_path)

    def broken_image(argv):
        if argv[0] == "ssh" and " run " in argv[-1]:
            raise RuntimeError("runtime dependency missing")
        return commands(argv)

    controller.command = broken_image
    with pytest.raises(RuntimeError, match="dependency"):
        controller.run()
    assert not any(c[1:3] == ["service", "scale"] for c in commands.calls)


def test_stopping_failure_still_attempts_every_other_role(tmp_path):
    controller, commands = release(tmp_path)

    def partially_unavailable(argv):
        if argv[1:3] == ["service", "scale"] and argv[-1] == "forwin-publisher-browser-swarm=0":
            raise RuntimeError("browser service unavailable")
        return commands(argv)

    controller.command = partially_unavailable
    with pytest.raises(RuntimeError, match="stop"):
        controller.stop_all()
    assert all(
        row["Spec"]["Mode"]["Replicated"]["Replicas"] == 0
        for name, row in commands.specs.items()
        if name != "forwin-publisher-browser-swarm"
    )


def test_a_new_release_keeps_the_previous_private_service_spec_evidence(tmp_path):
    controller, _ = release(tmp_path)
    controller.run()
    previous = json.loads(controller.state_path.read_text())
    second, _ = release(tmp_path)
    second.run()
    assert second.state["release_id"] != previous["release_id"]
    history = tmp_path / "forwin-releases" / (previous["release_id"] + ".json")
    assert json.loads(history.read_text()) == previous
    assert history.stat().st_mode & 0o777 == 0o600
    assert history.parent.stat().st_mode & 0o777 == 0o700


def test_error_evidence_write_failure_cannot_skip_stopping_started_roles(tmp_path, monkeypatch):
    controller, commands = release(tmp_path, fail="health")
    # The import helper creates a private module; use its function globals.
    namespace = type(controller).run.__globals__
    original = namespace["write_private"]

    def storage_full(path, value):
        if value.get("error"):
            raise OSError("simulated evidence volume full")
        original(path, value)

    monkeypatch.setitem(namespace, "write_private", storage_full)
    with pytest.raises(RuntimeError, match="state.*write|write.*state"):
        controller.run()
    assert all(
        row["Spec"]["Mode"]["Replicated"]["Replicas"] == 0
        for row in commands.specs.values()
    )
    assert json.loads(controller.state_path.read_text())["phase"] == "migrated"
