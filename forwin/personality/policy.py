from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from forwin.models.project import Project

from .models import CharacterPersonalityPolicy


class CharacterPersonalityPolicyResolver:
    def __init__(self, session: Session | None = None) -> None:
        self.session = session

    def resolve_for_project(self, project_id: str) -> CharacterPersonalityPolicy:
        if self.session is None:
            return CharacterPersonalityPolicy()
        project = self.session.get(Project, project_id)
        if project is None:
            return CharacterPersonalityPolicy()
        automation = _json_object(getattr(project, "automation_json", "") or "{}")
        raw_policy = automation.get("character_personality")
        if not isinstance(raw_policy, dict):
            return CharacterPersonalityPolicy()
        allowed = set(CharacterPersonalityPolicy.model_fields)
        payload: dict[str, Any] = {}
        for key, value in raw_policy.items():
            if key in allowed:
                payload[key] = value
        return CharacterPersonalityPolicy.model_validate(payload)


def _json_object(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}
