"""ForWin ASGI entrypoint."""
from __future__ import annotations

from forwin.http import create_app, lifespan

app = create_app()

__all__ = ["app", "create_app", "lifespan"]
