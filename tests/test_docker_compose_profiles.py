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


def test_publisher_browser_uses_browser_image_target() -> None:
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))

    assert compose["services"]["forwin"]["build"] == "."
    assert compose["services"]["generation-worker"]["build"] == "."
    assert compose["services"]["forwin-mcp"]["build"] == "."
    browser_build = compose["services"]["publisher-browser"]["build"]
    assert browser_build["context"] == "."
    assert browser_build["target"] == "publisher-browser-runtime"
