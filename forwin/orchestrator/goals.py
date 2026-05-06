from __future__ import annotations

import json


def normalize_goals_payload(payload: object) -> list[str]:
    if isinstance(payload, str):
        goal = payload.strip()
        return [goal] if len(goal) >= 2 else []
    if not isinstance(payload, list):
        return []
    return [str(item).strip() for item in payload if len(str(item).strip()) >= 2]


def load_goals_json(raw: str) -> list[str]:
    try:
        payload = json.loads(raw or "[]") or []
    except (json.JSONDecodeError, TypeError):
        payload = []
    return normalize_goals_payload(payload)
