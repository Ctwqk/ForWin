from __future__ import annotations

import importlib
from types import ModuleType
from typing import Any

common = importlib.import_module("forwin.orchestrator_loop_core.common")
run_control = importlib.import_module("forwin.orchestrator_loop_core.run_control")
acceptance = importlib.import_module("forwin.orchestrator_loop_core.acceptance")
governance = importlib.import_module("forwin.orchestrator_loop_core.governance")
runtime_helpers = importlib.import_module("forwin.orchestrator_loop_core.runtime_helpers")
review_autofix = importlib.import_module("forwin.orchestrator_loop_core.review_autofix")
repair_loop = importlib.import_module("forwin.orchestrator_loop_core.repair_loop")
project_chapters = importlib.import_module("forwin.orchestrator_loop_core.project_chapters")
writer_attention = importlib.import_module("forwin.orchestrator_loop_core.writer_attention")
quality_gates = importlib.import_module("forwin.orchestrator_loop_core.quality_gates")
world_projection = importlib.import_module("forwin.orchestrator_loop_core.world_projection")
finalization = importlib.import_module("forwin.orchestrator_loop_core.finalization")
service = importlib.import_module("forwin.orchestrator_loop_core.service")

_MODULES: tuple[ModuleType, ...] = (
    service,
    finalization,
    world_projection,
    quality_gates,
    writer_attention,
    project_chapters,
    repair_loop,
    review_autofix,
    runtime_helpers,
    governance,
    acceptance,
    run_control,
    common,
)


def exported_names() -> list[str]:
    names: set[str] = set()
    for module in _MODULES:
        names.update(name for name in module.__dict__ if not name.startswith("__"))
    return sorted(names)


def get_export(name: str) -> Any:
    for module in _MODULES:
        if name in module.__dict__:
            return getattr(module, name)
    raise AttributeError(name)


def set_export(name: str, value: Any) -> bool:
    updated = False
    for module in _MODULES:
        if name in module.__dict__:
            setattr(module, name, value)
            updated = True
    return updated


def del_export(name: str) -> bool:
    updated = False
    for module in _MODULES:
        if name in module.__dict__:
            delattr(module, name)
            updated = True
    return updated
