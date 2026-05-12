from __future__ import annotations

from pathlib import Path


def test_publisher_browser_is_profile_gated() -> None:
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    service_start = compose.index("  publisher-browser:")
    next_service = compose.index("\n  minio:", service_start)
    service_block = compose[service_start:next_service]

    assert "profiles:" in service_block
    assert '"publisher"' in service_block or "'publisher'" in service_block
