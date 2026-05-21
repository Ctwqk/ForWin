from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.models import ChapterPlan


class ProtagonistMacroStatus(BaseModel):
    project_id: str
    as_of_chapter: int
    status_tier: int = 0
    wealth_tier: int = 0
    enemy_tier: int = 0
    market_space: str = ""
    evidence_refs: list[str] = Field(default_factory=list)
    source: str = "book_state_macro_projection"

    @field_validator("status_tier", "wealth_tier", "enemy_tier", mode="before")
    @classmethod
    def _clean_tier(cls, value: object) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    @field_validator("market_space", mode="before")
    @classmethod
    def _clean_text(cls, value: object) -> str:
        return str(value or "").strip()


def derive_protagonist_macro_status(
    session: Session,
    *,
    project_id: str,
    as_of_chapter: int,
) -> ProtagonistMacroStatus:
    rows = session.execute(
        select(ChapterPlan)
        .where(
            ChapterPlan.project_id == project_id,
            ChapterPlan.status == "accepted",
            ChapterPlan.chapter_number <= int(as_of_chapter or 0),
        )
        .order_by(ChapterPlan.chapter_number.asc())
    ).scalars().all()
    status = ProtagonistMacroStatus(
        project_id=project_id,
        as_of_chapter=int(as_of_chapter or 0),
    )
    for row in rows:
        payload = _loads(row.experience_plan_json, {})
        macro = payload.get("macro_status") if isinstance(payload, dict) else None
        if not isinstance(macro, dict):
            continue
        update: dict[str, Any] = {}
        for key in ("status_tier", "wealth_tier", "enemy_tier", "market_space"):
            if key in macro and macro[key] not in (None, ""):
                update[key] = macro[key]
        if not update:
            continue
        refs = list(status.evidence_refs)
        refs.append(f"chapter_plan:{int(row.chapter_number or 0)}")
        update["evidence_refs"] = refs[-8:]
        status = status.model_copy(update=update)
    return status


def _loads(value: str, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback


__all__ = ["ProtagonistMacroStatus", "derive_protagonist_macro_status"]
