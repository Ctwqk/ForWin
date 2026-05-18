from __future__ import annotations

import importlib
from types import ModuleType
from typing import Any

app = importlib.import_module("forwin.api_core.app")
automation = importlib.import_module("forwin.api_core.automation")
generation = importlib.import_module("forwin.api_core.generation")
project_helpers = importlib.import_module("forwin.api_core.project_helpers")
runtime = importlib.import_module("forwin.api_core.runtime")
state = importlib.import_module("forwin.api_core.state")
tasks = importlib.import_module("forwin.api_core.tasks")

_MODULES: tuple[ModuleType, ...] = (
    app,
    automation,
    generation,
    project_helpers,
    tasks,
    runtime,
    state,
)
_STATE_NAMES = {
    name
    for name in state.__dict__
    if not name.startswith("__")
}


def exported_names() -> list[str]:
    names: set[str] = set()
    for module in _MODULES:
        names.update(
            name
            for name in module.__dict__
            if not name.startswith("__")
        )
    return sorted(names)


def get_export(name: str) -> Any:
    if name in _STATE_NAMES:
        return getattr(state, name)
    for module in _MODULES:
        if name in module.__dict__:
            return getattr(module, name)
    raise AttributeError(name)


def set_export(name: str, value: Any) -> bool:
    updated = False
    if name in _STATE_NAMES:
        setattr(state, name, value)
        updated = True
    for module in _MODULES:
        if name in module.__dict__:
            setattr(module, name, value)
            updated = True
    return updated


def del_export(name: str) -> bool:
    updated = False
    if name in _STATE_NAMES and hasattr(state, name):
        delattr(state, name)
        updated = True
    for module in _MODULES:
        if name in module.__dict__:
            delattr(module, name)
            updated = True
    return updated
