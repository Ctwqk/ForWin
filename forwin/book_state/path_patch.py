from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any


def path_parts(field_path: str) -> list[str]:
    return [part for part in field_path.split(".") if part]


@dataclass(frozen=True)
class BlockedPathValue:
    """A populated non-mapping ancestor that prevents resolving the requested leaf."""

    field_path: str
    value: Any


def current_path_value(payload: Any, field_path: str) -> Any:
    """Read a leaf, distinguishing a blocked traversal from a resolved value."""
    current = payload
    parts = path_parts(field_path)
    for index, part in enumerate(parts):
        if not isinstance(current, dict):
            return BlockedPathValue(".".join(parts[:index]), current) if current is not None else None
        current = current.get(part)
    return current


def apply_path_patch(
    payload: dict[str, Any],
    field_path: str,
    value: Any,
    *,
    op: str = "set",
) -> None:
    parts = path_parts(field_path)
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


def cas_patch_fields(
    payload: dict[str, Any], field_path: str, new_value: Any, *, op: str = "set",
) -> dict[str, Any]:
    """Read a CAS value, or merge a first write into its existing parent."""
    parts = path_parts(field_path)
    field_path = ".".join(parts)
    current: Any = payload
    parents = []
    for index, part in enumerate(parts):
        if not isinstance(current, dict):
            break
        if index:
            parents.append((".".join(parts[:index]), current))
        current = current.get(part)
    else:
        if current is not None:
            return dict(op=op, field_path=field_path, old_value=deepcopy(current), new_value=new_value)
    if not parents:
        raise ValueError(f"{field_path} has no current value or parent")
    parent_path, parent = parents[-1]
    updated = deepcopy(parent)
    suffix = field_path[len(parent_path) + 1:]
    apply_path_patch(updated, suffix, new_value, op=op)
    leaf = suffix.split(".", 1)[0]
    return dict(op="merge", field_path=parent_path, old_value=deepcopy(parent),
                new_value={leaf: updated[leaf]} if leaf in updated else {})


__all__ = ["BlockedPathValue", "apply_path_patch", "cas_patch_fields", "current_path_value", "path_parts"]
