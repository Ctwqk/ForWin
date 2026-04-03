from __future__ import annotations

import json


def load_goals_json(raw: str) -> list[str]:
    try:
        payload = json.loads(raw or "[]") or []
    except (json.JSONDecodeError, TypeError):
        payload = []
    return [str(item).strip() for item in payload if str(item).strip()]
