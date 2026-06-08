from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class _BaseStateModel(BaseModel):
    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)


class CharacterState(_BaseStateModel):
    location: str = ""
    location_id: str = ""
    status: str = ""
    goal: str = ""
    power_level: str = ""
    mood: str = ""
    role_state: str = ""
    knowledge_state: str = ""
    possession_state: str = ""
    life_state: str = ""
    custody_state: str = ""
    injury_state: str = ""
    participation_state: str = ""
    terminal_event_id: str = ""
    terminal_event_chapter: str = ""
    bridge_event_id: str = ""


class LocationState(_BaseStateModel):
    status: str = ""
    controlled_by: str = ""


class FactionState(_BaseStateModel):
    status: str = ""
    location: str = ""
    location_id: str = ""
    headquarters_location_id: str = ""
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

STATE_FIELD_ALIASES: dict[str, dict[str, str]] = {
    "character": {
        "role": "role_state",
        "角色": "role_state",
        "身份": "role_state",
        "knowledge": "knowledge_state",
        "认知": "knowledge_state",
        "认知状态": "knowledge_state",
        "已知信息": "knowledge_state",
        "知道的信息": "knowledge_state",
        "possession": "possession_state",
        "possessions": "possession_state",
        "持有物": "possession_state",
        "持有物品": "possession_state",
        "持有证据": "possession_state",
        "目标": "goal",
        "状态": "status",
        "情绪": "mood",
        "位置": "location",
        "地点": "location",
    }
}


def normalize_state_field(kind: str, field: str) -> str:
    normalized_field = field.strip()
    aliases = STATE_FIELD_ALIASES.get(kind, {})
    return aliases.get(normalized_field, normalized_field)


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
    normalized_field = normalize_state_field(kind, field)
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
