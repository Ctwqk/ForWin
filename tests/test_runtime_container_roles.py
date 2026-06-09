from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_api_app_constructs_runtime_container_with_api_role() -> None:
    source = (ROOT / "forwin" / "api_core" / "app.py").read_text(encoding="utf-8")

    assert "RuntimeContainer.from_config(api_state._config, role=\"api\")" in source


def test_cli_constructs_worker_runtime_containers_with_worker_roles() -> None:
    source = (ROOT / "forwin" / "cli.py").read_text(encoding="utf-8")

    assert "RuntimeContainer.from_config(config, role=\"generation_worker\")" in source
    assert "RuntimeContainer.from_config(config, role=\"publisher_worker\")" in source
