from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _compose_text() -> str:
    return (ROOT / "docker-compose.yml").read_text(encoding="utf-8")


def _dockerfile_text() -> str:
    return (ROOT / "Dockerfile").read_text(encoding="utf-8")


def test_dockerfile_includes_runtime_skill_registry() -> None:
    dockerfile = _dockerfile_text()

    assert "COPY forwin_skills/ forwin_skills/" in dockerfile


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
    assert "qdrant/qdrant:${QDRANT_IMAGE_TAG:-v1.17.1}" in compose
    assert "minio/minio:${MINIO_IMAGE_TAG:-RELEASE.2025-09-07T16-13-09Z}" in compose


def test_compose_forwin_mcp_avoids_host_8898_collision() -> None:
    compose = _compose_text()
    mcp_block = compose.split("\n  forwin-mcp:\n", 1)[1].split("\n  publisher-browser:", 1)[0]

    assert '"--port", "8896"' in mcp_block
    assert "FORWIN_MCP_PORT=${FORWIN_MCP_PORT:-8896}" in mcp_block
    assert '"${FORWIN_MCP_DEBUG_BIND:-127.0.0.1:8896}:8896"' in mcp_block
    assert "http://localhost:8896/health" in mcp_block
    assert "8898" not in mcp_block


def test_browser_fixture_uses_host_reachable_qdrant_url() -> None:
    fixture = (ROOT / "tests" / "browser" / "conftest.py").read_text(encoding="utf-8")

    assert '"FORWIN_QDRANT_URL": "http://127.0.0.1:6335"' in fixture


def test_compose_publishes_forwin_qdrant_on_dedicated_local_debug_port() -> None:
    compose = _compose_text()
    qdrant_block = compose.split("\n  qdrant:\n", 1)[1].split("\n  forwin-mcp:", 1)[0]

    assert '"${FORWIN_QDRANT_DEBUG_BIND:-127.0.0.1:6335}:6333"' in qdrant_block


def test_publisher_extension_manifest_does_not_request_all_hosts() -> None:
    manifest = (ROOT / "browser_extension" / "forwin-publisher" / "manifest.json").read_text(encoding="utf-8")

    assert '"http://*/*"' not in manifest
    assert '"https://*/*"' not in manifest


def test_readme_names_service_process_target_roles() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    normalized = " ".join(readme.split())

    assert "forwin-app-swarm" in readme
    assert "forwin-generation-worker-swarm" in readme
    assert "forwin-mcp-swarm" in readme
    assert "forwin-publisher-worker-swarm" in readme
    assert "forwin-publisher-browser-swarm" in readme
    assert "Postgres/Qdrant/MinIO services instead of starting app-local stateful containers on 126" in normalized
