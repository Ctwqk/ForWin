from __future__ import annotations

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
    auth = options.pop("auth")
    authenticated = next(
        auth.auth_flow(httpx.Request("GET", "http://forwin.invalid"))
    )
    assert authenticated.headers["Authorization"].startswith("Basic ")
    assert options == {
        "base_url": "http://127.0.0.1:18899",
        "timeout": 60,
        "trust_env": False,
        "follow_redirects": False,
        "headers": {"X-Request-ID": "request-generic"},
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
