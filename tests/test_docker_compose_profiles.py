from __future__ import annotations

from pathlib import Path

import yaml


def test_publisher_browser_is_profile_gated() -> None:
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    service_start = compose.index("  publisher-browser:")
    next_service = compose.index("\n  minio:", service_start)
    service_block = compose[service_start:next_service]

    assert "profiles:" in service_block
    assert '"publisher"' in service_block or "'publisher'" in service_block


def test_generation_worker_is_compose_managed_with_current_image() -> None:
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))

    worker = compose["services"]["generation-worker"]

    assert worker["build"] == "."
    assert worker["container_name"] == "forwin-generation-worker"
    assert worker["command"][:3] == ["python", "-m", "forwin.cli"]
    assert "generation-worker" in worker["command"]
    assert "forwin-data:/app/data" in worker["volumes"]
    assert worker["environment"] == compose["services"]["forwin"]["environment"]
    assert worker["healthcheck"] == {"disable": True}


def test_outbox_worker_is_compose_managed_with_current_image() -> None:
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))

    worker = compose["services"]["outbox-worker"]

    assert worker["build"] == "."
    assert worker["container_name"] == "forwin-outbox-worker"
    assert worker["command"][:3] == ["python", "-m", "forwin.cli"]
    assert "outbox-worker" in worker["command"]
    assert "forwin-data:/app/data" in worker["volumes"]
    assert worker["environment"] == compose["services"]["forwin"]["environment"]
    assert worker["healthcheck"] == {"disable": True}


def test_non_generation_services_depend_only_on_postgres_startup() -> None:
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))

    for service_name in ("forwin", "outbox-worker", "publisher-worker"):
        assert set(compose["services"][service_name]["depends_on"]) == {"postgres"}

    assert set(compose["services"]["generation-worker"]["depends_on"]) == {
        "postgres",
        "qdrant",
        "minio",
    }


def test_publisher_browser_uses_browser_image_target() -> None:
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))

    assert compose["services"]["forwin"]["build"] == "."
    assert compose["services"]["generation-worker"]["build"] == "."
    assert compose["services"]["outbox-worker"]["build"] == "."
    assert compose["services"]["forwin-mcp"]["build"] == "."
    browser_build = compose["services"]["publisher-browser"]["build"]
    assert browser_build["context"] == "."
    assert browser_build["target"] == "publisher-browser-runtime"


def test_publisher_browser_session_restore_is_opt_in() -> None:
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    service_start = compose.index("  publisher-browser:")
    next_service = compose.index("\n  minio:", service_start)
    service_block = compose[service_start:next_service]

    assert (
        "FORWIN_EXTENSION_RESTORE_BACKEND_SESSIONS="
        "${FORWIN_EXTENSION_RESTORE_BACKEND_SESSIONS:-false}"
    ) in service_block


def test_publisher_browser_launcher_gates_session_restore() -> None:
    launcher = Path("scripts/launch_linux_extension_browser.sh").read_text(encoding="utf-8")

    assert 'RESTORE_BACKEND_SESSIONS="${FORWIN_EXTENSION_RESTORE_BACKEND_SESSIONS:-false}"' in launcher
    assert 'is_truthy "$RESTORE_BACKEND_SESSIONS"' in launcher


def test_mcp_timeout_default_covers_long_genesis_operations() -> None:
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    environment = compose["services"]["forwin-mcp"]["environment"]
    env_example = Path(".env.example").read_text(encoding="utf-8")

    assert (
        "FORWIN_MCP_API_TIMEOUT_SECONDS=${FORWIN_MCP_API_TIMEOUT_SECONDS:-900}"
        in environment
    )
    assert "FORWIN_MCP_API_TIMEOUT_SECONDS=900" in env_example.splitlines()
