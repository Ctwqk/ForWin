from __future__ import annotations

import pytest

from forwin.publisher_runtime.auth import (
    ExtensionAuthService,
    PublisherExtensionAuthError,
    PublisherExtensionAuthNotConfigured,
)
from forwin.publishers.manager import PublisherManager


def test_extension_auth_service_verifies_key_without_leaking_candidate() -> None:
    auth = ExtensionAuthService(extension_api_key="expected-secret")

    with pytest.raises(PublisherExtensionAuthError) as ctx:
        auth.verify_extension_api_key("wrong-secret")

    assert "wrong-secret" not in str(ctx.value)
    auth.verify_extension_api_key("expected-secret")


def test_extension_auth_service_reports_missing_configuration() -> None:
    auth = ExtensionAuthService(extension_api_key="")

    with pytest.raises(PublisherExtensionAuthNotConfigured):
        auth.verify_extension_api_key("provided")

    assert auth.backend_ready_payload() == {"extension_api_key_configured": False}


def test_publisher_manager_auth_facade_keeps_legacy_exception_imports() -> None:
    manager = PublisherManager(lambda: None, extension_api_key="expected-secret")

    with pytest.raises(PublisherExtensionAuthError):
        manager.verify_extension_api_key("")

    manager.verify_extension_api_key("expected-secret")
    assert manager.backend_ready_payload() == {"extension_api_key_configured": True}
