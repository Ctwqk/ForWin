from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from forwin.api_publisher_routes import build_handlers


class _FakePublisherManager:
    def __init__(self) -> None:
        self.checked_keys: list[str | None] = []

    def verify_extension_api_key(self, value: str | None) -> None:
        self.checked_keys.append(value)
        if value != "secret":
            from forwin.publishers.manager import PublisherExtensionAuthError

            raise PublisherExtensionAuthError("bad key")

    def preferred_client_heartbeat(self, **_kwargs):
        return {"ok": True, "client_id": "client-1"}


def test_extension_heartbeat_status_requires_extension_key() -> None:
    manager = _FakePublisherManager()
    handlers = build_handlers(
        get_publisher_manager=lambda: manager,
        extension_root=Path("browser_extension/forwin-publisher"),
    )

    with pytest.raises(HTTPException) as exc:
        handlers["publisher_extension_heartbeat_status"](x_forwin_extension_key=None)

    assert exc.value.status_code == 401
    assert manager.checked_keys == [None]
    assert handlers["publisher_extension_heartbeat_status"](
        x_forwin_extension_key="secret"
    ) == {"ok": True, "client_id": "client-1"}
