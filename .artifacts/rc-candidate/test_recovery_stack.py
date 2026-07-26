from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import yaml


MODULE_PATH = Path(__file__).with_name("recovery_stack.py")
SPEC = importlib.util.spec_from_file_location("recovery_stack", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
stack = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(stack)

SOURCE_SHA = "f" * 40


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
        identity=identity,
        run_identity=run_identity,
        database_volume=volume_absent,
    )
    stack.append_event(
        "fresh_up_completed",
        fault_id=fault_id,
        identity=identity,
        run_identity=run_identity,
        database_volume=volume_present,
    )
    return run_identity, volume_present, identity


def isolated_compose_config() -> tuple[dict, dict]:
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
    identity = {
        "runtime_image": {"tag": runtime_tag},
        "browser_image": {"tag": browser_tag},
        "dependency_images": {
            service: {"tag": tag}
            for service, tag in dependency_tags.items()
        },
    }
    return {
        "name": stack.PROJECT,
        "services": services,
        "volumes": {
            "forwin-postgres": {
                "name": f"{stack.PROJECT}_forwin-postgres",
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
        assert {
            key: environment.get(key)
            for key in expected
        } == expected


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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
) -> None:
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

    with pytest.raises(stack.StackError, match="completed fresh-up"):
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
        identity={"source_sha": SOURCE_SHA},
        run_identity=run_identity,
        database_volume={"name": volume_name, "exists": False},
    )
    stack.append_event(
        "fresh_up_completed",
        fault_id="fault-1",
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
