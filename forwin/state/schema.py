from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class _BaseStateModel(BaseModel):
    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)


class CharacterState(_BaseStateModel):
    location: str = ""
    status: str = ""
    goal: str = ""
    power_level: str = ""
    mood: str = ""


class LocationState(_BaseStateModel):
    status: str = ""
    controlled_by: str = ""


class FactionState(_BaseStateModel):
    status: str = ""
    location: str = ""
    goal: str = ""
    power_level: str = ""


SCHEMA_BY_KIND: dict[str, type[_BaseStateModel]] = {
    "character": CharacterState,
    "location": LocationState,
    "faction": FactionState,
}

KNOWN_STATE_FIELDS: dict[str, set[str]] = {
    kind: set(model_cls.model_fields)
    for kind, model_cls in SCHEMA_BY_KIND.items()
}


def normalize_state_payload(state: dict[str, Any]) -> dict[str, str]:
    if not isinstance(state, dict):
        raise ValueError("State payload must be a dict.")

    normalized: dict[str, str] = {}
    for key, value in state.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError("State field names must be non-empty strings.")
        normalized_key = key.strip()
        normalized[normalized_key] = "" if value is None else str(value).strip()
    return normalized


def validate_state_payload(kind: str, state: dict[str, Any]) -> dict[str, str]:
    normalized = normalize_state_payload(state)
    model_cls = SCHEMA_BY_KIND.get(kind)
    if model_cls is None:
        return normalized
    model = model_cls(**normalized)
    return {
        key: value
        for key, value in model.model_dump(exclude_none=True).items()
        if isinstance(value, str)
    }


def prepare_state_change(
    kind: str,
    current_state: dict[str, Any],
    field: str,
    new_value: Any,
) -> tuple[str, dict[str, str]]:
    normalized_field = field.strip()
    if not normalized_field:
        raise ValueError("State change field cannot be empty.")

    known_fields = KNOWN_STATE_FIELDS.get(kind, set())
    if known_fields and normalized_field not in known_fields and normalized_field not in current_state:
        raise ValueError(
            f"Unsupported state field '{normalized_field}' for entity kind '{kind}'."
        )

    next_state = dict(current_state)
    next_state[normalized_field] = "" if new_value is None else str(new_value).strip()
    return normalized_field, validate_state_payload(kind, next_state)
