from __future__ import annotations

import base64

import httpx
import pytest
from fastapi import FastAPI, Header, HTTPException, Request

from forwin.api_auth import (
    basic_auth_enabled,
    make_basic_auth_middleware,
    require_operator_principal,
)
from forwin.config import InfrastructureConfig


def _authorization(user: str, password: str) -> str:
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _build_app(config: InfrastructureConfig) -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def optional_basic_auth(request, call_next):
        if not basic_auth_enabled(config):
            return await call_next(request)
        return await make_basic_auth_middleware(config)(request, call_next)

    @app.get("/")
    async def home():
        return {"page": "home"}

    @app.get("/api/projects")
    async def projects():
        return [{"id": "project-1"}]

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post("/api/publishers/extension/heartbeat")
    async def extension_heartbeat(
        x_forwin_extension_key: str | None = Header(default=None),
    ):
        if x_forwin_extension_key != config.publisher_extension_api_key:
            raise HTTPException(401, "extension auth failed")
        return {"ok": True}

    @app.get("/api/publishers/upload-jobs/{job_id}")
    async def get_upload_job(job_id: str):
        return {"job_id": job_id}

    @app.post("/operator-action")
    async def operator_action(request: Request):
        principal = require_operator_principal(request, config)
        return {
            "actor_id": principal.actor_id,
            "auth_method": principal.auth_method,
        }

    return app


async def _request(app: FastAPI, method: str, path: str, **kwargs) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        return await client.request(method, path, **kwargs)


@pytest.mark.asyncio
async def test_basic_auth_disabled_by_default() -> None:
    app = _build_app(InfrastructureConfig())

    response = await _request(app, "GET", "/api/projects")

    assert response.status_code == 200
    assert response.json() == [{"id": "project-1"}]


@pytest.mark.asyncio
async def test_basic_auth_rejects_missing_or_wrong_credentials() -> None:
    app = _build_app(
        InfrastructureConfig(http_basic_user="alice", http_basic_password="secret")
    )

    missing = await _request(app, "GET", "/api/projects")
    wrong = await _request(
        app,
        "GET",
        "/api/projects",
        headers={"Authorization": _authorization("alice", "wrong")},
    )

    assert missing.status_code == 401
    assert missing.text == "Authentication required"
    assert missing.headers["www-authenticate"] == 'Basic realm="ForWin"'
    assert wrong.status_code == 401


@pytest.mark.asyncio
async def test_basic_auth_accepts_correct_credentials() -> None:
    app = _build_app(
        InfrastructureConfig(http_basic_user="alice", http_basic_password="secret")
    )

    response = await _request(
        app,
        "GET",
        "/api/projects",
        headers={"Authorization": _authorization("alice", "secret")},
    )

    assert response.status_code == 200
    assert response.json() == [{"id": "project-1"}]

    operator = await _request(
        app,
        "POST",
        "/operator-action",
        headers={"Authorization": _authorization("alice", "secret")},
    )
    assert operator.status_code == 200
    assert operator.json() == {"actor_id": "basic:alice", "auth_method": "basic"}


@pytest.mark.asyncio
async def test_operator_action_fails_closed_without_a_stable_principal() -> None:
    app = _build_app(InfrastructureConfig())

    response = await _request(app, "POST", "/operator-action")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "operator_auth_not_configured"


@pytest.mark.asyncio
async def test_trusted_proxy_identity_requires_an_explicit_source_network() -> None:
    config = InfrastructureConfig(
        http_trusted_operator_header="X-Authenticated-User",
        http_trusted_operator_proxies=("10.0.0.0/24",),
    )
    app = _build_app(config)
    transport = httpx.ASGITransport(app=app, client=("10.0.0.12", 45000))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        accepted = await client.post(
            "/operator-action",
            headers={"X-Authenticated-User": "oncall@example.com"},
        )

    untrusted_transport = httpx.ASGITransport(
        app=app, client=("192.168.1.12", 45000)
    )
    async with httpx.AsyncClient(
        transport=untrusted_transport, base_url="http://testserver"
    ) as client:
        rejected = await client.post(
            "/operator-action",
            headers={"X-Authenticated-User": "oncall@example.com"},
        )

    assert accepted.status_code == 200
    assert accepted.json() == {
        "actor_id": "proxy:oncall@example.com",
        "auth_method": "trusted_proxy",
    }
    assert rejected.status_code == 401


@pytest.mark.asyncio
async def test_basic_auth_exempts_health_and_extension_paths() -> None:
    config = InfrastructureConfig(
        http_basic_user="alice",
        http_basic_password="secret",
        publisher_extension_api_key="extension-secret",
        publisher_session_secret="test-session-secret",
        publisher_session_encryption_required=True,
    )
    app = _build_app(config)

    health = await _request(app, "GET", "/health")
    extension_missing_key = await _request(
        app,
        "POST",
        "/api/publishers/extension/heartbeat",
        json={"client_id": "client-1"},
    )
    extension_correct_key = await _request(
        app,
        "POST",
        "/api/publishers/extension/heartbeat",
        headers={"X-Forwin-Extension-Key": "extension-secret"},
        json={"client_id": "client-1"},
    )

    assert health.status_code == 200
    assert extension_missing_key.status_code == 401
    assert extension_missing_key.headers.get("www-authenticate") is None
    assert extension_missing_key.json()["detail"] == "extension auth failed"
    assert extension_correct_key.status_code == 200


@pytest.mark.asyncio
async def test_extension_key_cannot_bypass_basic_auth_for_ordinary_job_routes() -> None:
    config = InfrastructureConfig(
        http_basic_user="alice",
        http_basic_password="secret",
        publisher_extension_api_key="extension-secret",
        publisher_session_secret="test-session-secret",
        publisher_session_encryption_required=True,
    )
    app = _build_app(config)

    missing_key = await _request(app, "GET", "/api/publishers/upload-jobs/job-1")
    correct_key = await _request(
        app,
        "GET",
        "/api/publishers/upload-jobs/job-1",
        headers={"X-Forwin-Extension-Key": "extension-secret"},
    )

    assert missing_key.status_code == 401
    assert missing_key.headers["www-authenticate"] == 'Basic realm="ForWin"'
    assert correct_key.status_code == 401
    assert correct_key.headers["www-authenticate"] == 'Basic realm="ForWin"'


def test_config_rejects_partial_basic_auth() -> None:
    with pytest.raises(ValueError, match="must be set together"):
        InfrastructureConfig(http_basic_user="alice")

    with pytest.raises(ValueError, match="must be set together"):
        InfrastructureConfig(http_basic_password="secret")


def test_config_rejects_partial_or_invalid_trusted_operator_proxy() -> None:
    with pytest.raises(ValueError, match="must be set together"):
        InfrastructureConfig(http_trusted_operator_header="X-Authenticated-User")

    with pytest.raises(ValueError, match="must be set together"):
        InfrastructureConfig(http_trusted_operator_proxies=("10.0.0.0/24",))

    with pytest.raises(ValueError, match="valid HTTP header"):
        InfrastructureConfig(
            http_trusted_operator_header="bad header",
            http_trusted_operator_proxies=("10.0.0.0/24",),
        )

    with pytest.raises(ValueError, match="valid IP network"):
        InfrastructureConfig(
            http_trusted_operator_header="X-Authenticated-User",
            http_trusted_operator_proxies=("not-a-network",),
        )


def test_config_from_env_rejects_partial_basic_auth(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FORWIN_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.delenv("FORWIN_HTTP_BASIC_USER", raising=False)
    monkeypatch.delenv("FORWIN_HTTP_BASIC_PASSWORD", raising=False)
    monkeypatch.setenv("FORWIN_HTTP_BASIC_USER", "alice")

    with pytest.raises(ValueError, match="must be set together"):
        InfrastructureConfig.from_env()
