from __future__ import annotations

import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = REPO_ROOT / "deploy" / "magi3" / "docker-compose.yml"
ENV_EXAMPLE_PATH = REPO_ROOT / "deploy" / "magi3" / "env.example"
DEPLOY_SCRIPT = REPO_ROOT / "scripts" / "deploy_magi3.sh"
BACKUP_SCRIPT = REPO_ROOT / "scripts" / "backup_magi3.sh"
TUNNEL_PLIST = REPO_ROOT / "deploy" / "magi3" / "com.forwin.magi3-tunnel.plist"
BACKUP_PLIST = REPO_ROOT / "deploy" / "magi3" / "com.forwin.magi3-backup.plist"


def load_compose() -> dict:
    return yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))


def test_every_published_port_binds_localhost() -> None:
    compose = load_compose()

    for service_name, service in compose["services"].items():
        for port in service.get("ports", []) or []:
            if isinstance(port, str):
                assert port.startswith("127.0.0.1:"), (service_name, port)
            else:
                assert port.get("host_ip") == "127.0.0.1", (service_name, port)


def test_compose_has_the_expected_services_and_volumes() -> None:
    compose = load_compose()

    assert compose.get("name") == "forwin"
    assert set(compose["services"]) == {
        "postgres",
        "qdrant",
        "embedding",
        "app",
        "mcp",
        "generation-worker",
        "outbox-worker",
        "publisher-worker",
        "publisher-browser",
        "migrate",
    }
    assert set(compose["volumes"]) == {
        "forwin-postgres",
        "forwin-qdrant",
        "forwin-data",
    }

    services = compose["services"]
    assert services["postgres"]["image"] == "postgres:16-alpine"
    assert services["qdrant"]["image"] == "qdrant/qdrant:v1.17.1"
    assert services["app"]["environment"]["FORWIN_ARTIFACT_BACKEND"] == "local"
    assert services["app"]["environment"]["FORWIN_EMBEDDING_BACKEND"] == "gateway"
    assert services["app"]["environment"]["FORWIN_EMBEDDING_BASE_URL"] == "http://embedding:8080"
    assert services["embedding"]["build"]["context"] == "./embedding-gateway"
    assert "ports" not in services["embedding"]
    assert services["migrate"]["profiles"] == ["tools"]
    assert services["migrate"]["command"] == ["alembic", "upgrade", "head"]

    for service_name in (
        "app",
        "mcp",
        "generation-worker",
        "outbox-worker",
        "publisher-worker",
        "publisher-browser",
        "migrate",
    ):
        build = services[service_name]["build"]
        assert build["context"] == "../.."
        expected_target = (
            "publisher-browser-runtime"
            if service_name == "publisher-browser"
            else "forwin-runtime"
        )
        assert build["target"] == expected_target

    assert services["generation-worker"]["command"] == [
        "python",
        "-m",
        "forwin.cli",
        "-v",
        "generation-worker",
        "--worker-id",
        "forwin-generation-worker",
        "--lease-seconds",
        "300",
        "--poll-interval",
        "2",
    ]
    assert services["outbox-worker"]["command"] == [
        "python",
        "-m",
        "forwin.cli",
        "-v",
        "outbox-worker",
        "--worker-id",
        "forwin-outbox-worker",
        "--poll-interval",
        "2",
    ]
    assert services["publisher-worker"]["command"] == [
        "python",
        "-m",
        "forwin.cli",
        "-v",
        "publisher-worker",
        "--limit",
        "1",
        "--poll-interval",
        "2",
    ]


def test_compose_sets_no_codex_variables() -> None:
    assert "codex" not in COMPOSE_PATH.read_text(encoding="utf-8").lower()


def test_worker_subcommands_exist_in_cli() -> None:
    cli_source = (REPO_ROOT / "forwin" / "cli.py").read_text(encoding="utf-8")
    help_result = subprocess.run(
        [sys.executable, "-m", "forwin.cli", "--help"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    for command in ("generation-worker", "outbox-worker", "publisher-worker"):
        assert f'"{command}"' in cli_source
        assert command in help_result.stdout


def test_scripts_parse() -> None:
    for script in (DEPLOY_SCRIPT, BACKUP_SCRIPT):
        subprocess.run(["bash", "-n", str(script)], check=True)


def test_compose_uses_in_network_service_urls_and_no_external_ip_literals() -> None:
    text = COMPOSE_PATH.read_text(encoding="utf-8")

    assert (
        "postgresql+psycopg://forwin:${FORWIN_POSTGRES_PASSWORD}@postgres:5432/forwin"
        in text
    )
    assert "http://qdrant:6333" in text
    assert "minio" not in text.lower()
    assert set(re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text)) <= {
        "127.0.0.1", "0.0.0.0"
    }


def test_publisher_browser_has_swarm_tmpfs_and_healthcheck() -> None:
    browser = load_compose()["services"]["publisher-browser"]

    assert any(
        entry.startswith("/dev/shm:") and "size=1073741824" in entry
        for entry in browser["tmpfs"]
    )
    health_test = " ".join(browser["healthcheck"]["test"])
    assert "http://127.0.0.1:9222/json/version" in health_test
    assert "scripts/check_publisher_browser_heartbeat.py --wait-seconds 0" in health_test
    assert browser["healthcheck"] | {"test": None} == {
        "test": None, "interval": "30s", "timeout": "15s", "retries": 3,
        "start_period": "90s",
    }


def test_runtime_volumes_and_mcp_healthcheck() -> None:
    services = load_compose()["services"]
    for name, service in services.items():
        if name not in {"postgres", "qdrant", "embedding", "migrate"}:
            assert "forwin-data:/app/data" in service["volumes"]
    for name, target in {
        "postgres": "/var/lib/postgresql/data", "qdrant": "/qdrant/storage",
    }.items():
        assert services[name]["volumes"] == [f"forwin-{name}:{target}"]
    assert services["mcp"]["command"] == [
        "python", "-m", "uvicorn", "forwin.mcp.http:app", "--host", "0.0.0.0",
        "--port", "8896",
    ]
    assert services["mcp"]["healthcheck"] == {
        "test": ["CMD-SHELL", "python -c \"import httpx; r=httpx.get('http://localhost:8896/health', timeout=3); raise SystemExit(0 if r.status_code == 200 else 1)\""],
        "interval": "10s", "timeout": "5s", "retries": 3, "start_period": "5s",
    }
    for name in ("generation-worker", "outbox-worker", "publisher-worker"):
        assert services[name]["healthcheck"] == {"disable": True}


def test_env_example_has_provider_key_names_only() -> None:
    env_text = ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
    names = {
        line.split("=", 1)[0].strip()
        for line in env_text.splitlines()
        if line.strip() and not line.lstrip().startswith("#") and "=" in line
    }

    assert {
        "FORWIN_POSTGRES_PASSWORD",
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_BASE_URL",
        "DEEPSEEK_MODEL",
        "KIMI_API_KEY",
        "KIMI_BASE_URL",
        "KIMI_MODEL",
        "MINIMAX_API_KEY",
        "MINIMAX_BASE_URL",
        "MINIMAX_MODEL",
        "FORWIN_PUBLISHER_SESSION_SECRET",
    } <= names
    assert "codex" not in env_text.lower()
    assert all(
        not line.split("=", 1)[1]
        for line in env_text.splitlines()
        if line and not line.startswith("#") and "=" in line
    )


def test_deploy_script_has_safety_contract() -> None:
    source = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    assert "set -euo pipefail" in source
    assert "git status --porcelain" in source
    assert "origin/master" in source
    assert "magi3:~/forwin/repo.git" in source
    assert "docker compose" in source
    assert "build" in source
    # The remote script arrives on ssh stdin; a migrate run that reads stdin would swallow the rest of it.
    assert "--profile tools run --rm -T migrate </dev/null" in source
    assert "up -d --remove-orphans" in source
    assert "180" in source
    assert ".deploy-commit" in source


def test_backup_script_has_retention_and_size_contract() -> None:
    source = BACKUP_SCRIPT.read_text(encoding="utf-8")

    assert "set -euo pipefail" in source
    assert "pg_dump" in source
    assert "-Fc" in source
    assert "forwin-magi3" in source
    assert "date -u" in source
    assert "1024" in source
    assert "14" in source
    assert "forwin-data" in source


def test_launchd_agents_have_required_schedule_and_tunnel() -> None:
    tunnel = plistlib.loads(TUNNEL_PLIST.read_bytes())
    backup = plistlib.loads(BACKUP_PLIST.read_bytes())

    tunnel_args = tunnel["ProgramArguments"]
    assert tunnel["Label"] == "com.forwin.magi3-tunnel"
    assert tunnel["KeepAlive"] is True
    assert tunnel["RunAtLoad"] is True
    assert tunnel_args[:2] == ["/usr/bin/ssh", "-N"]
    assert "ExitOnForwardFailure=yes" in tunnel_args
    assert "ServerAliveInterval=30" in tunnel_args
    assert "28899:127.0.0.1:8899" in tunnel_args
    assert "28896:127.0.0.1:8896" in tunnel_args
    assert tunnel_args[-1] == "magi3"

    assert backup["Label"] == "com.forwin.magi3-backup"
    assert backup["StartCalendarInterval"] == {"Hour": 3, "Minute": 17}
    assert backup["ProgramArguments"] == [
        "/Users/magi1/ForWin-source-github/scripts/backup_magi3.sh"
    ]
    for key in ("StandardOutPath", "StandardErrorPath"):
        assert backup[key] == "/Users/magi1/Library/Logs/forwin-magi3-backup.log"


def test_remote_env_is_gitignored() -> None:
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "deploy/magi3/.env"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    )
    assert result.stdout.strip() == "deploy/magi3/.env"


@pytest.fixture
def shell_harness(tmp_path: Path) -> dict:
    """Run the real shell scripts without SSH, Git or Docker side effects."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    remote_home = tmp_path / "remote"
    env_dir = remote_home / "forwin/src/deploy/magi3"
    env_dir.mkdir(parents=True)
    (env_dir / ".env").write_text("TEST_SECRET=must-not-be-printed\n")
    shim = fake_bin / "shim"
    shim.write_text(f"#!{sys.executable}\n" + r'''
import json
import os
from pathlib import Path
import subprocess
import sys

kind = Path(sys.argv[0]).name
args = sys.argv[1:]
if kind == "shim":
    kind, *args = args
remote = os.environ.get("FAKE_REMOTE") == "1"
log = Path(os.environ["FAKE_LOG"])
with log.open("a") as stream:
    stream.write(json.dumps({"command": kind, "args": args, "remote": remote}) + "\n")
failure = os.environ.get("FAKE_FAIL", "")

if kind == "git":
    action = args[0]
    label = ("remote-" if remote else "local-") + action
    if failure == label:
        sys.exit(9)
    if action == "status":
        print(os.environ.get("FAKE_DIRTY", ""), end="")
    elif action == "rev-parse":
        ref = args[-1].split("^")[0]
        print("a" * 40 if ref == "origin/master" else ref)
    elif action == "merge-base":
        sys.exit(0 if os.environ.get("FAKE_ANCESTOR", "1") == "1" else 1)
    elif action == "clone":
        Path(args[-1]).mkdir(parents=True)
elif kind == "ssh":
    assert args[0] == "magi3"
    if args[1:4] == ["bash", "-s", "--"]:
        env = dict(os.environ, HOME=os.environ["FAKE_REMOTE_HOME"], FAKE_REMOTE="1",
                   BASH_ENV=os.environ["FAKE_BASH_ENV"])
        result = subprocess.run(["/bin/bash", "-s", "--", *args[4:]],
                                input=sys.stdin.buffer.read(), env=env)
        sys.exit(result.returncode)
    command = args[1]
    if "pg_dump" in command:
        sys.stdout.buffer.write(b"D" * int(os.environ.get("FAKE_DUMP_BYTES", "2048")))
        sys.exit(9 if failure == "dump" else 0)
    elif "tar czf" in command:
        sys.stdout.buffer.write(b"T" * 2048)
        sys.exit(9 if failure == "archive" else 0)
    else:
        assert "git init --bare -q ~/forwin/repo.git" in command
        sys.exit(9 if failure == "init" else 0)
elif kind == "docker":
    action = next(arg for arg in args if arg in {"build", "run", "up"})
    sys.exit(9 if failure == {"run": "migrate"}.get(action, action) else 0)
elif kind == "curl":
    sys.exit(1 if failure == "health" or failure == "health-" + args[-1].split(":")[-1].split("/")[0] else 0)
elif kind == "date":
    print("20261025T031700Z" if args[0] == "-u" else os.environ.get("FAKE_DAY", "1"))
else:
    raise AssertionError(kind)
''')
    shim.chmod(0o755)
    for name in ("git", "ssh", "date"):
        (fake_bin / name).symlink_to(shim)
    bash_env = tmp_path / "bash_env"
    bash_env.write_text("\n".join(
        f"{name}() {{ {shlex.quote(str(shim))} {name} \"$@\"; }}"
        for name in ("git", "docker", "curl")
    ) + "\nsleep() { SECONDS=$((SECONDS + 181)); }\n")
    return {
        "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
        "HOME": str(tmp_path / "local"),
        "FAKE_REMOTE_HOME": str(remote_home),
        "FAKE_LOG": str(tmp_path / "commands.jsonl"),
        "FAKE_BASH_ENV": str(bash_env),
    }


def run_shell(harness: dict, script: Path, *args: str, **settings: str):
    result = subprocess.run(
        ["/bin/bash", str(script), *args],
        env={**os.environ, **harness, **settings},
        capture_output=True, text=True, timeout=15,
    )
    assert "must-not-be-printed" not in result.stdout + result.stderr
    return result


def shell_calls(harness: dict, command: str) -> list[dict]:
    log = Path(harness["FAKE_LOG"])
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return [call for call in calls if call["command"] == command]


@pytest.mark.parametrize("ref", [None, "b" * 40])
def test_deploy_checks_out_and_records_exact_master_commit(shell_harness: dict, ref) -> None:
    args = [] if ref is None else ["--ref", ref]
    result = run_shell(shell_harness, DEPLOY_SCRIPT, *args)
    assert result.returncode == 0, result.stderr
    sha = ref or "a" * 40
    git = shell_calls(shell_harness, "git")
    assert {"command": "git", "args": ["fetch", "origin", "master"], "remote": False} in git
    assert any(call["args"] == ["merge-base", "--is-ancestor", sha, "origin/master"] for call in git)
    assert any(call["args"] == ["push", "ssh://magi3/~/forwin/repo.git", f"{sha}:refs/heads/deploy", "--force"] for call in git)
    assert any(call["remote"] and call["args"] == ["fetch", "origin", "deploy"] for call in git)
    assert any(call["remote"] and call["args"] == ["checkout", "--detach", sha] for call in git)
    docker = shell_calls(shell_harness, "docker")
    prefix = ["compose", "-p", "forwin", "-f", "deploy/magi3/docker-compose.yml", "--env-file", "deploy/magi3/.env"]
    assert [call["args"] for call in docker] == [
        prefix + ["build"], prefix + ["--profile", "tools", "run", "--rm", "-T", "migrate"],
        prefix + ["up", "-d", "--remove-orphans"],
    ]
    assert [call["args"][-1] for call in shell_calls(shell_harness, "curl")] == [
        "http://127.0.0.1:8899/health", "http://127.0.0.1:8896/health",
    ]
    marker = Path(shell_harness["FAKE_REMOTE_HOME"]) / "forwin/src/.deploy-commit"
    assert marker.read_text() == sha + "\n"


@pytest.mark.parametrize("settings", [
    {"FAKE_DIRTY": " M tracked\n"}, {"FAKE_DIRTY": "?? untracked\n"},
    {"FAKE_ANCESTOR": "0"}, {"FAKE_FAIL": "local-fetch"},
    {"FAKE_FAIL": "local-status"},
])
def test_deploy_rejects_unsafe_source_before_contacting_magi3(shell_harness: dict, settings) -> None:
    result = run_shell(shell_harness, DEPLOY_SCRIPT, **settings)
    assert result.returncode != 0
    assert shell_calls(shell_harness, "ssh") == []


@pytest.mark.parametrize("args", [["--ref"], ["--ref", "master"], ["--ref", "a;echo secret"], ["--unknown"]])
def test_deploy_rejects_invalid_arguments(shell_harness: dict, args) -> None:
    assert run_shell(shell_harness, DEPLOY_SCRIPT, *args).returncode != 0
    assert shell_calls(shell_harness, "git") == []


@pytest.mark.parametrize("failure", ["init", "local-push", "remote-fetch", "remote-checkout", "build", "migrate", "up", "health-8899", "health-8896"])
def test_deploy_failure_does_not_replace_success_marker(shell_harness: dict, failure: str) -> None:
    marker = Path(shell_harness["FAKE_REMOTE_HOME"]) / "forwin/src/.deploy-commit"
    marker.write_text("previous-success\n")
    result = run_shell(shell_harness, DEPLOY_SCRIPT, FAKE_FAIL=failure)
    assert result.returncode != 0
    assert marker.read_text() == "previous-success\n"
    if failure == "migrate":
        assert not any("up" in call["args"] for call in shell_calls(shell_harness, "docker"))
    if failure.startswith("health"):
        assert "180 seconds" in result.stderr


def test_deploy_data_stores_only_skips_migration_health_and_marker(shell_harness: dict) -> None:
    result = run_shell(shell_harness, DEPLOY_SCRIPT, "--data-stores-only")
    assert result.returncode == 0, result.stderr
    docker = shell_calls(shell_harness, "docker")
    assert len(docker) == 2
    assert docker[0]["args"][-1] == "build"
    assert docker[1]["args"][-4:] == ["up", "-d", "postgres", "qdrant"]
    assert shell_calls(shell_harness, "curl") == []
    assert not (Path(shell_harness["FAKE_REMOTE_HOME"]) / "forwin/src/.deploy-commit").exists()


def test_first_deploy_seeds_clone_and_requests_host_only_env(shell_harness: dict) -> None:
    shutil.rmtree(Path(shell_harness["FAKE_REMOTE_HOME"]) / "forwin/src")
    result = run_shell(shell_harness, DEPLOY_SCRIPT)
    assert result.returncode != 0
    assert "Create deploy/magi3/.env" in result.stderr
    assert any(call["args"][0] == "clone" for call in shell_calls(shell_harness, "git"))
    assert shell_calls(shell_harness, "docker") == []


def test_backup_streams_private_dump_and_prunes_only_old_backups(shell_harness: dict) -> None:
    backup_dir = Path(shell_harness["HOME"]) / "backups/forwin-magi3"
    backup_dir.mkdir(parents=True)
    old = [backup_dir / "forwin-old.dump", backup_dir / "forwin-data-old.tgz"]
    keep = [backup_dir / "forwin-recent.dump", backup_dir / "unrelated.txt"]
    for path in old + keep:
        path.touch()
        age = 13 if path.name == "forwin-recent.dump" else 15
        os.utime(path, (time.time() - age * 86400,) * 2)
    result = run_shell(shell_harness, BACKUP_SCRIPT)
    assert result.returncode == 0, result.stderr
    dump = backup_dir / "forwin-20261025T031700Z.dump"
    assert dump.stat().st_size == 2048
    assert dump.stat().st_mode & 0o777 == 0o600
    assert all(not path.exists() for path in old)
    assert all(path.exists() for path in keep)
    ssh = shell_calls(shell_harness, "ssh")
    assert len(ssh) == 1
    assert "docker compose -p forwin -f ~/forwin/src/deploy/magi3/docker-compose.yml --env-file ~/forwin/src/deploy/magi3/.env exec -T postgres pg_dump -U forwin -Fc forwin" in ssh[0]["args"][1]


@pytest.mark.parametrize("day,args", [("7", []), ("1", ["--with-data"])])
def test_backup_weekly_or_forced_data_archive(shell_harness: dict, day: str, args) -> None:
    result = run_shell(shell_harness, BACKUP_SCRIPT, *args, FAKE_DAY=day)
    assert result.returncode == 0, result.stderr
    archive = Path(shell_harness["HOME"]) / "backups/forwin-magi3/forwin-data-20261025T031700Z.tgz"
    assert archive.stat().st_size == 2048
    assert archive.stat().st_mode & 0o777 == 0o600
    assert "docker run --rm -v forwin_forwin-data:/data alpine tar czf - -C /data ." in shell_calls(shell_harness, "ssh")[1]["args"][1]


@pytest.mark.parametrize("settings", [{"FAKE_DUMP_BYTES": "1023"}, {"FAKE_FAIL": "dump"}, {"FAKE_FAIL": "archive"}])
def test_backup_rejects_failed_streams_and_cleans_partial_files(shell_harness: dict, settings) -> None:
    result = run_shell(shell_harness, BACKUP_SCRIPT, "--with-data", **settings)
    assert result.returncode != 0
    files = list((Path(shell_harness["HOME"]) / "backups/forwin-magi3").iterdir())
    assert not any(".partial." in path.name for path in files)
    assert not any(path.suffix == ".tgz" for path in files)
    if settings.get("FAKE_FAIL") != "archive":
        assert files == []


def test_backup_accepts_exactly_one_kibibyte(shell_harness: dict) -> None:
    result = run_shell(shell_harness, BACKUP_SCRIPT, FAKE_DUMP_BYTES="1024")
    assert result.returncode == 0, result.stderr
