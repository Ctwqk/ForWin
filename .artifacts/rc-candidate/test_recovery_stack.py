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
    return {"name": stack.PROJECT, "services": services}, identity


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

    assert first["previous_event_sha256"] == "0" * 64
    assert second["previous_event_sha256"] == first["event_sha256"]
    assert stack.load_verified_events() == [first, second]


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

    monkeypatch.setattr(stack, "assert_frozen", lambda: identity)
    monkeypatch.setattr(
        stack,
        "assert_isolated_compose",
        lambda checked_identity: isolated_checks.append(checked_identity),
        raising=False,
    )
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
