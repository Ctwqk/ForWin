from __future__ import annotations

import pytest
from fastapi import HTTPException

from forwin.api_publisher_ops import _require_extension_auth
from forwin.publishers.manager import (
    PublisherExtensionAuthError,
    PublisherExtensionAuthNotConfigured,
    PublisherManager,
)


def _manager(api_key: str = "") -> PublisherManager:
    return PublisherManager(lambda: None, extension_api_key=api_key)


def test_verify_extension_api_key_requires_configured_key() -> None:
    manager = _manager("")

    with pytest.raises(PublisherExtensionAuthNotConfigured):
        manager.verify_extension_api_key("provided")


@pytest.mark.parametrize("provided", [None, "", "wrong-secret"])
def test_verify_extension_api_key_rejects_missing_or_wrong_key(provided: str | None) -> None:
    manager = _manager("expected-secret")

    with pytest.raises(PublisherExtensionAuthError) as ctx:
        manager.verify_extension_api_key(provided)

    assert "wrong-secret" not in str(ctx.value)


def test_verify_extension_api_key_accepts_correct_key() -> None:
    _manager("expected-secret").verify_extension_api_key("expected-secret")


def test_require_extension_auth_maps_not_configured_to_503() -> None:
    with pytest.raises(HTTPException) as ctx:
        _require_extension_auth(_manager(""), "provided")

    assert ctx.value.status_code == 503


@pytest.mark.parametrize("provided", [None, "", "wrong-secret"])
def test_require_extension_auth_maps_bad_key_to_401(provided: str | None) -> None:
    with pytest.raises(HTTPException) as ctx:
        _require_extension_auth(_manager("expected-secret"), provided)

    assert ctx.value.status_code == 401
    assert "wrong-secret" not in str(ctx.value.detail)


def test_require_extension_auth_accepts_correct_key() -> None:
    _require_extension_auth(_manager("expected-secret"), "expected-secret")
