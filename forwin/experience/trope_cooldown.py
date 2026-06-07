from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.models.phase import TropeUsageRecord
from forwin.protocol.trope_library import TropeTemplate


class TropeCooldownPolicy(BaseModel):
    template_band_gap: int = Field(default=3, ge=0, le=20)
    category_band_gap: int = Field(default=1, ge=0, le=20)


def _normalize_usage_stage(value: str | None) -> str:
    stage = str(value or "accepted").strip()
    return stage if stage in {"planned", "accepted"} else "accepted"


def select_available_templates(
    templates: list[TropeTemplate],
    *,
    recent_template_ids: list[str],
    recent_categories: list[str],
    policy: TropeCooldownPolicy,
) -> list[TropeTemplate]:
    blocked_templates = set(recent_template_ids[: policy.template_band_gap])
    blocked_categories = set(recent_categories[: policy.category_band_gap])
    available = [
        template
        for template in templates
        if template.template_id not in blocked_templates
        and str(template.category) not in blocked_categories
    ]
    return (
        available
        or [template for template in templates if template.template_id not in blocked_templates]
        or list(templates)
    )


def recent_trope_usage(
    session: Session,
    *,
    project_id: str,
    limit: int = 24,
    usage_stage: str = "accepted",
) -> tuple[list[str], list[str]]:
    stage = _normalize_usage_stage(usage_stage)
    rows = (
        session.execute(
            select(TropeUsageRecord)
            .where(
                TropeUsageRecord.project_id == project_id,
                TropeUsageRecord.usage_stage == stage,
            )
            .order_by(TropeUsageRecord.created_at.desc(), TropeUsageRecord.id.desc())
            .limit(max(1, int(limit or 24)))
        )
        .scalars()
        .all()
    )
    return [row.template_id for row in rows], [row.category for row in rows]


def save_trope_usage(
    session: Session,
    *,
    project_id: str,
    arc_id: str,
    band_id: str,
    chapter_number: int,
    template_id: str,
    category: str,
    usage_stage: str = "accepted",
) -> TropeUsageRecord:
    stage = _normalize_usage_stage(usage_stage)
    normalized_template_id = str(template_id or "").strip()
    existing = session.execute(
        select(TropeUsageRecord).where(
            TropeUsageRecord.project_id == project_id,
            TropeUsageRecord.chapter_number == int(chapter_number or 0),
            TropeUsageRecord.template_id == normalized_template_id,
            TropeUsageRecord.usage_stage == stage,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    row = TropeUsageRecord(
        project_id=project_id,
        arc_id=arc_id,
        band_id=band_id,
        chapter_number=int(chapter_number or 0),
        template_id=normalized_template_id,
        category=category,
        usage_stage=stage,
    )
    session.add(row)
    return row


def save_accepted_trope_usage_for_chapter(
    session: Session,
    *,
    project_id: str,
    arc_id: str,
    band_id: str,
    chapter_number: int,
    experience_plan_json: str,
) -> list[TropeUsageRecord]:
    plan = _json_loads(experience_plan_json, {})
    if not isinstance(plan, dict):
        return []
    template_ids = _plan_values(
        plan,
        "selected_template_ids",
        "selected_trope_ids",
        "template_ids",
        "trope_ids",
        "active_band_template_ids",
    )
    categories = _plan_values(
        plan,
        "planned_reward_tags",
        "selected_trope_categories",
        "reward_tags",
    )
    rows: list[TropeUsageRecord] = []
    for index, template_id in enumerate(template_ids):
        if not template_id:
            continue
        category = categories[index] if index < len(categories) else ""
        rows.append(
            save_trope_usage(
                session,
                project_id=project_id,
                arc_id=arc_id,
                band_id=band_id,
                chapter_number=chapter_number,
                template_id=template_id,
                category=category,
                usage_stage="accepted",
            )
        )
    return rows


def _plan_values(plan: dict[str, Any], *keys: str) -> list[str]:
    candidates: list[Any] = []
    for key in keys:
        value = plan.get(key)
        if isinstance(value, list):
            candidates.extend(value)
        elif isinstance(value, str) and value.strip():
            candidates.append(value)
    return [str(item).strip() for item in candidates if str(item).strip()]


def _json_loads(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback
