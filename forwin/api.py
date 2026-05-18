"""Compatibility proxy for the split ForWin API implementation."""
from __future__ import annotations

import sys
from types import ModuleType
from typing import Any

from forwin.api_core import exports as _api_exports
from forwin.api_core.app import app  # ensure the app and route handlers are registered


class _ApiModule(ModuleType):
    def __getattribute__(self, name: str) -> Any:
        if name.startswith("__") or name in {"_ApiModule", "_api_exports"}:
            return ModuleType.__getattribute__(self, name)
        try:
            return _api_exports.get_export(name)
        except AttributeError:
            return ModuleType.__getattribute__(self, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("__") or name in {"_ApiModule", "_api_exports"}:
            ModuleType.__setattr__(self, name, value)
            return
        _api_exports.set_export(name, value)
        ModuleType.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        if _api_exports.del_export(name):
            self.__dict__.pop(name, None)
            return
        ModuleType.__delattr__(self, name)


_module = sys.modules[__name__]
_module.__class__ = _ApiModule
__all__ = _api_exports.exported_names()

for _name in __all__:
    if _name not in {"_ApiModule", "_api_exports"}:
        globals()[_name] = _api_exports.get_export(_name)
