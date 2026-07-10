"""ForWin ASGI entrypoint."""
from __future__ import annotations

from forwin.api_core.app import app, lifespan

__all__ = ["app", "lifespan"]
