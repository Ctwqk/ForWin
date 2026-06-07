from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.models import ChapterPlan
from forwin.models.book_state import FactNodeRow


_MACRO_KEYS = ("status_tier", "wealth_tier", "enemy_tier", "market_space")


class ProtagonistMacroStatus(BaseModel):
    project_id: str
    as_of_chapter: int
    status_tier: int = 0
    wealth_tier: int = 0
    enemy_tier: int = 0
    market_space: str = ""
    evidence_refs: list[str] = Field(default_factory=list)
    source: str = "macro_status_unavailable"

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
    status = ProtagonistMacroStatus(
        project_id=project_id,
        as_of_chapter=int(as_of_chapter or 0),
    )
    fact_status = _derive_from_fact_rows(
        session,
        project_id=project_id,
        as_of_chapter=int(as_of_chapter or 0),
        status=status,
    )
    if fact_status.source == "book_state_macro_fact":
        return fact_status
    return _derive_from_chapter_rows(
        session,
        project_id=project_id,
        as_of_chapter=int(as_of_chapter or 0),
        status=status,
    )


def _derive_from_fact_rows(
    session: Session,
    *,
    project_id: str,
    as_of_chapter: int,
    status: ProtagonistMacroStatus,
) -> ProtagonistMacroStatus:
    rows = session.execute(
        select(FactNodeRow)
        .where(
            FactNodeRow.project_id == project_id,
            FactNodeRow.truth_value == "true",
            FactNodeRow.created_at_chapter <= int(as_of_chapter or 0),
        )
        .order_by(FactNodeRow.created_at_chapter.asc(), FactNodeRow.id.asc())
    ).scalars().all()
    for row in rows:
        payload = _macro_payload_from_fact(row)
        update = _macro_update(payload)
        if not update:
            continue
        refs = _loads(row.source_refs_json, [])
        if not isinstance(refs, list) or not refs:
            refs = [f"fact_node:{row.id}"]
        update["evidence_refs"] = [*status.evidence_refs, *[str(item) for item in refs]][-8:]
        update["source"] = "book_state_macro_fact"
        status = status.model_copy(update=update)
    return status


def _derive_from_chapter_rows(
    session: Session,
    *,
    project_id: str,
    as_of_chapter: int,
    status: ProtagonistMacroStatus,
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
    for row in rows:
        payload = _loads(row.experience_plan_json, {})
        macro = payload.get("macro_status") if isinstance(payload, dict) else None
        if not isinstance(macro, dict):
            continue
        update = _macro_update(macro)
        if not update:
            continue
        explicit_refs = macro.get("evidence_refs")
        refs = list(status.evidence_refs)
        if isinstance(explicit_refs, list) and explicit_refs:
            refs.extend(str(item) for item in explicit_refs if str(item).strip())
            source = "accepted_chapter_macro_evidence"
        else:
            refs.append(f"chapter_plan:{int(row.chapter_number or 0)}")
            source = "accepted_chapter_macro_legacy_projection"
        update["evidence_refs"] = refs[-8:]
        update["source"] = source
        status = status.model_copy(update=update)
    return status


def _macro_payload_from_fact(row: FactNodeRow) -> dict[str, Any]:
    candidates = [
        _loads(row.state_json, {}),
        _loads(row.metadata_json, {}),
    ]
    for payload in candidates:
        if not isinstance(payload, dict):
            continue
        macro = payload.get("macro_status")
        if isinstance(macro, dict) and _macro_update(macro):
            return macro
        if _macro_update(payload):
            return payload
    if str(row.fact_type or "") in {"macro_status", "protagonist_macro_status"}:
        return {}
    return {}


def _macro_update(payload: dict[str, Any]) -> dict[str, Any]:
    update: dict[str, Any] = {}
    for key in _MACRO_KEYS:
        if key in payload and payload[key] not in (None, ""):
            update[key] = payload[key]
    return update


def _loads(value: str, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback


__all__ = ["ProtagonistMacroStatus", "derive_protagonist_macro_status"]
