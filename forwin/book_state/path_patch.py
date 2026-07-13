from __future__ import annotations

from typing import Any


def apply_path_patch(
    payload: dict[str, Any],
    field_path: str,
    value: Any,
    *,
    op: str = "set",
) -> None:
    parts = [part for part in field_path.split(".") if part]
    if not parts:
        return
    cursor = payload
    for part in parts[:-1]:
        nested = cursor.get(part)
        if not isinstance(nested, dict):
            nested = {}
            cursor[part] = nested
        cursor = nested
    key = parts[-1]
    if op == "append":
        current = cursor.setdefault(key, [])
        if isinstance(current, list):
            current.append(value)
        else:
            cursor[key] = [current, value]
    elif op == "remove":
        current = cursor.get(key)
        if isinstance(current, list):
            cursor[key] = [item for item in current if item != value]
        else:
            cursor.pop(key, None)
    elif op == "merge" and isinstance(value, dict):
        current = cursor.get(key)
        if isinstance(current, dict):
            current.update(value)
        else:
            cursor[key] = dict(value)
    else:
        cursor[key] = value


__all__ = ["apply_path_patch"]
