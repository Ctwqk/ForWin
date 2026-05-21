from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.models import ProjectProgressionRule, new_id


@dataclass(slots=True)
class ActiveProgressionRule:
    id: str
    rule_type: str
    severity: str
    chapter_start: int
    chapter_end: int
    payload: dict[str, Any]


class ProgressionRuleRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_rule(
        self,
        *,
        project_id: str,
        rule_type: str,
        chapter_start: int,
        chapter_end: int,
        severity: str,
        payload: dict[str, Any],
    ) -> ProjectProgressionRule:
        row = ProjectProgressionRule(
            id=new_id(),
            project_id=project_id,
            rule_type=rule_type,
            chapter_start=max(1, int(chapter_start or 1)),
            chapter_end=max(0, int(chapter_end or 0)),
            severity=severity if severity in {"warning", "blocking"} else "warning",
            payload_json=json.dumps(payload, ensure_ascii=False, sort_keys=True),
            active=True,
        )
        self.session.add(row)
        self.session.flush()
        return row


def active_progression_rules_for_chapter(
    session: Session,
    *,
    project_id: str,
    chapter_number: int,
) -> list[ActiveProgressionRule]:
    chapter = int(chapter_number or 0)
    rows = session.execute(
        select(ProjectProgressionRule)
        .where(
            ProjectProgressionRule.project_id == project_id,
            ProjectProgressionRule.active.is_(True),
            ProjectProgressionRule.chapter_start <= chapter,
            (ProjectProgressionRule.chapter_end == 0)
            | (ProjectProgressionRule.chapter_end >= chapter),
        )
        .order_by(
            ProjectProgressionRule.chapter_start.asc(),
            ProjectProgressionRule.created_at.asc(),
            ProjectProgressionRule.id.asc(),
        )
    ).scalars().all()
    return [_to_active_rule(row) for row in rows]


def _to_active_rule(row: ProjectProgressionRule) -> ActiveProgressionRule:
    try:
        payload = json.loads(row.payload_json or "{}")
    except (TypeError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return ActiveProgressionRule(
        id=str(row.id or ""),
        rule_type=str(row.rule_type or ""),
        severity=str(row.severity or "warning"),
        chapter_start=int(row.chapter_start or 0),
        chapter_end=int(row.chapter_end or 0),
        payload=payload,
    )


__all__ = [
    "ActiveProgressionRule",
    "ProgressionRuleRepository",
    "active_progression_rules_for_chapter",
]
