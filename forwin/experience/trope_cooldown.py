from __future__ import annotations

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.models.phase import TropeUsageRecord
from forwin.protocol.trope_library import TropeTemplate


class TropeCooldownPolicy(BaseModel):
    template_band_gap: int = Field(default=3, ge=0, le=20)
    category_band_gap: int = Field(default=1, ge=0, le=20)


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
) -> tuple[list[str], list[str]]:
    rows = (
        session.execute(
            select(TropeUsageRecord)
            .where(TropeUsageRecord.project_id == project_id)
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
) -> TropeUsageRecord:
    row = TropeUsageRecord(
        project_id=project_id,
        arc_id=arc_id,
        band_id=band_id,
        chapter_number=chapter_number,
        template_id=template_id,
        category=category,
    )
    session.add(row)
    return row
