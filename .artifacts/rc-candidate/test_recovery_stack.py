from __future__ import annotations

import importlib.util
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest
import yaml


MODULE_PATH = Path(__file__).with_name("recovery_stack.py")
SPEC = importlib.util.spec_from_file_location("recovery_stack", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
stack = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(stack)

FINALIZER_PATH = Path(__file__).with_name("finalize_recovery.py")
FINALIZER_SPEC = importlib.util.spec_from_file_location(
    "finalize_recovery_contract",
    FINALIZER_PATH,
)
assert FINALIZER_SPEC is not None and FINALIZER_SPEC.loader is not None
finalizer = importlib.util.module_from_spec(FINALIZER_SPEC)
FINALIZER_SPEC.loader.exec_module(finalizer)

SOURCE_SHA = "f" * 40


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
    identity = {"source_sha": SOURCE_SHA}
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
    identity = {"source_sha": SOURCE_SHA}
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
    runtime_env.write_text("", encoding="utf-8")
    provider_env.write_text("", encoding="utf-8")
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

    payload["services"]["qdrant"]["container_name"] = "forwin-v5-recovery-qdrant"
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
    identity = {"source_sha": SOURCE_SHA}
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
    assert "not path.is_symlink()" in args[5]
    assert args[-1] == "/app/data/publisher_covers"
    assert kwargs == {"run_identity": run_identity}
    with pytest.raises(stack.StackError, match="beneath /app/data"):
        stack.file_inventory.__wrapped__(
            "publisher-browser",
            "fault-inventory",
            "/etc",
        )


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
    identity = {"source_sha": SOURCE_SHA}
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


def test_fresh_up_records_stable_run_identity_and_database_volume_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_dir = (tmp_path / "fault-1").resolve()
    monkeypatch.setenv(stack.EVIDENCE_DIR_ENV, str(evidence_dir))
    monkeypatch.setattr(stack.secrets, "token_hex", lambda _size: "c" * 32)
    identity = {"source_sha": SOURCE_SHA}
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
    identity = {"source_sha": SOURCE_SHA}
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


def test_fresh_up_preserves_original_and_cleanup_errors_in_setup_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_dir = (tmp_path / "cleanup-failed").resolve()
    monkeypatch.setenv(stack.EVIDENCE_DIR_ENV, str(evidence_dir))
    monkeypatch.setattr(stack.secrets, "token_hex", lambda _size: "9" * 32)
    identity = {"source_sha": SOURCE_SHA}
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
    identity = {"source_sha": SOURCE_SHA}
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
    identity = {"source_sha": SOURCE_SHA}
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


def test_setup_hold_rejects_duplicate_overlap_and_mismatched_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _run_identity, volume, identity = recovery_lifecycle(tmp_path, monkeypatch)
    running = {service: True for service in stack.SERVICES}

    def inspect_service(service: str, **_kwargs: object) -> dict:
        return {
            "service": service,
            "exists": True,
            "running": running[service],
            "container_id": f"{service}-container",
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

    stack.setup_hold_service("outbox-worker", "fault-1", "hold-1")
    independent = stack.setup_hold_service(
        "publisher-worker",
        "fault-1",
        "hold-2",
    )
    assert independent["action"] == "setup_service_held"

    with pytest.raises(stack.StackError, match="already used"):
        stack.setup_hold_service("minio", "fault-1", "hold-1")
    with pytest.raises(stack.StackError, match="active setup hold"):
        stack.setup_hold_service("outbox-worker", "fault-1", "hold-3")
    with pytest.raises(stack.StackError, match="matching setup hold"):
        stack.setup_release_service("outbox-worker", "fault-1", "wrong-id")
    with pytest.raises(stack.StackError, match="service mismatch"):
        stack.setup_release_service("minio", "fault-1", "hold-1")
    with pytest.raises(stack.StackError, match="hold identity"):
        stack.setup_hold_service("outbox-worker", "fault-1", "../unsafe")


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
        stack.setup_hold_service("outbox-worker", "fault-1", "late-hold")

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
        requested_at="2026-07-22T12:00:00+00:00",
        hold_time="2026-07-22T12:00:01+00:00",
        identity=identity,
        run_identity=run_identity,
        database_volume=volume,
        before={"exists": True, "running": True},
        after={"exists": True, "running": False},
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
            requested_at="2026-07-22T12:00:00+00:00",
            hold_time="2026-07-22T12:00:01+00:00",
            identity=identity,
            run_identity=run_identity,
            database_volume=volume,
            before={"exists": True, "running": True},
            after={"exists": True, "running": False},
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
                "hold_id": "hold-1",
                "service": "outbox-worker",
            }
        ]
        if state == "active-hold"
        else []
    )
    with pytest.raises(stack.StackError, match="terminal"):
        stack.setup_hold_service("outbox-worker", "fault-1", "too-late")


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
    identity = {"source_sha": SOURCE_SHA}
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
    identity = {"source_sha": SOURCE_SHA}
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
