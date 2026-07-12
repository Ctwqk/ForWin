from __future__ import annotations

from collections.abc import Callable
from typing import Any

from forwin.application.project_control import ProjectControlApplicationService


def build_handlers(
    *,
    service: ProjectControlApplicationService,
) -> dict[str, Callable[..., Any]]:
    return service.handlers()


__all__ = ["build_handlers"]
