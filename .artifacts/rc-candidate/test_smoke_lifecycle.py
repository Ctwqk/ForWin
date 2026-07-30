from __future__ import annotations

import asyncio
import base64
import importlib.util
from pathlib import Path

import httpx
import pytest


MODULE_PATH = Path(__file__).with_name("smoke_lifecycle.py")
SPEC = importlib.util.spec_from_file_location("smoke_lifecycle", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


def test_direct_api_client_uses_complete_basic_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options: dict = {}
    sentinel = object()

    def fake_async_client(**kwargs):
        options.update(kwargs)
        return sentinel

    monkeypatch.setattr(smoke.httpx, "AsyncClient", fake_async_client)

    client = smoke.direct_api_client(
        base_url="http://127.0.0.1:18899",
        headers={"X-Request-ID": "request-generic"},
        source={
            "FORWIN_HTTP_BASIC_USER": "release-operator",
            "FORWIN_HTTP_BASIC_PASSWORD": "release-password",
        },
    )

    assert client is sentinel
    expected_authorization = "Basic " + base64.b64encode(
        b"release-operator:release-password"
    ).decode("ascii")
    assert options == {
        "base_url": "http://127.0.0.1:18899",
        "timeout": 60,
        "trust_env": False,
        "follow_redirects": False,
        "headers": {
            "X-Request-ID": "request-generic",
            "Authorization": expected_authorization,
        },
    }


def test_direct_api_client_rejects_partial_basic_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_client(**_kwargs):
        raise AssertionError("HTTP client must not be created")

    monkeypatch.setattr(smoke.httpx, "AsyncClient", unexpected_client)

    with pytest.raises(smoke.LifecycleError, match="must be set together"):
        smoke.direct_api_client(
            base_url="http://127.0.0.1:18899",
            source={"FORWIN_HTTP_BASIC_USER": "release-operator"},
        )


def test_direct_api_client_preserves_explicit_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options: dict = {}
    real_async_client = httpx.AsyncClient

    def capture_client(**kwargs):
        options.update(kwargs)
        return object()

    monkeypatch.setattr(smoke.httpx, "AsyncClient", capture_client)
    smoke.direct_api_client(
        base_url="http://forwin.invalid",
        headers={"Authorization": "Bearer explicit-token"},
        source={
            "FORWIN_HTTP_BASIC_USER": "release-operator",
            "FORWIN_HTTP_BASIC_PASSWORD": "release-password",
        },
    )

    observed: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["authorization"] = request.headers["Authorization"]
        return httpx.Response(200, json={"ok": True})

    options["transport"] = httpx.MockTransport(handler)

    async def request() -> None:
        async with real_async_client(**options) as client:
            response = await client.get("/api/generic")
            response.raise_for_status()

    asyncio.run(request())

    assert observed["authorization"] == "Bearer explicit-token"
