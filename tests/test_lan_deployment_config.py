from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _compose_text() -> str:
    return (ROOT / "docker-compose.yml").read_text(encoding="utf-8")


def test_compose_env_file_does_not_reference_personal_path() -> None:
    compose = _compose_text()

    assert "/home/taiwei/ForWin/.env" not in compose
    assert "${FORWIN_ENV_FILE:-.env}" in compose
    assert "required: false" in compose


def test_compose_default_forwin_http_bind_is_localhost() -> None:
    compose = _compose_text()

    assert '"${FORWIN_HTTP_BIND:-127.0.0.1}:${FORWIN_HTTP_PORT:-8899}:8899"' in compose
    assert '"8899:8899"' not in compose


def test_compose_requires_minio_credentials() -> None:
    compose = _compose_text()

    assert "${FORWIN_MINIO_ACCESS_KEY:?set FORWIN_MINIO_ACCESS_KEY in .env}" in compose
    assert "${FORWIN_MINIO_SECRET_KEY:?set FORWIN_MINIO_SECRET_KEY in .env}" in compose
    assert "forwinminio123" not in compose


def test_compose_pins_external_service_images() -> None:
    compose = _compose_text()

    assert "qdrant/qdrant:latest" not in compose
    assert "minio/minio:latest" not in compose
    assert "qdrant/qdrant:${QDRANT_IMAGE_TAG:-v1.15.4}" in compose
    assert "minio/minio:${MINIO_IMAGE_TAG:-RELEASE.2025-09-07T16-13-09Z}" in compose
