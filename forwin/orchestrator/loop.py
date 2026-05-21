"""Compatibility proxy for the split writing orchestrator implementation.

Architecture guard markers retained for source-based boundary tests; implementation now
lives in forwin.orchestrator_loop_core.*.

from forwin.extractor.book_state_graph_delta import BookStateGraphDeltaExtractor
BookStateDirectCommitService
book_state_result = commit_service.compile_approved
"""
from __future__ import annotations

import sys
from types import ModuleType
from typing import Any

from forwin.orchestrator_loop_core import exports as _loop_exports
from forwin.orchestrator_loop_core.service import WritingOrchestrator


class _LoopModule(ModuleType):
    def __getattribute__(self, name: str) -> Any:
        if name.startswith("__") or name in {"_LoopModule", "_loop_exports"}:
            return ModuleType.__getattribute__(self, name)
        try:
            return _loop_exports.get_export(name)
        except AttributeError:
            return ModuleType.__getattribute__(self, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("__") or name in {"_LoopModule", "_loop_exports"}:
            ModuleType.__setattr__(self, name, value)
            return
        _loop_exports.set_export(name, value)
        ModuleType.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        if _loop_exports.del_export(name):
            self.__dict__.pop(name, None)
            return
        ModuleType.__delattr__(self, name)


_module = sys.modules[__name__]
_module.__class__ = _LoopModule
__all__ = _loop_exports.exported_names()

for _name in __all__:
    if _name not in {"_LoopModule", "_loop_exports"}:
        globals()[_name] = _loop_exports.get_export(_name)
