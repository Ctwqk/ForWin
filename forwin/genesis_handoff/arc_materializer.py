from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.models.genesis import BookGenesisRevision
from forwin.models.project import ArcPlanVersion, Project
from forwin.state.updater import StateUpdater


class GenesisArcMaterializer:
    def __init__(self, owner: Any) -> None:
        self.owner = owner

    def materialize_book_arcs(
        self,
        *,
        session: Session,
        updater: StateUpdater,
        project: Project,
        revision: BookGenesisRevision,
    ) -> list[ArcPlanVersion]:
        pack = self.owner.load_pack(revision)
        blueprint = pack.get("book_arc_blueprint") if isinstance(pack.get("book_arc_blueprint"), dict) else {}
        arc_items = [item for item in (blueprint.get("arcs") or []) if isinstance(item, dict)]
        if not arc_items:
            raise ValueError("Genesis blueprint 尚未生成 arcs。")
        existing_rows = session.execute(
            select(ArcPlanVersion)
            .where(ArcPlanVersion.project_id == project.id)
            .order_by(ArcPlanVersion.arc_number.asc(), ArcPlanVersion.created_at.asc())
        ).scalars().all()
        if existing_rows:
            return existing_rows
        created: list[ArcPlanVersion] = []
        for index, arc_payload in enumerate(arc_items, start=1):
            created.append(
                updater.create_arc_plan(
                    project_id=project.id,
                    arc_synopsis=str(arc_payload.get("arc_synopsis", "")).strip() or f"Arc {index}",
                    version=index,
                    status="active" if index == 1 else "planned",
                    arc_number=int(arc_payload.get("arc_number", index) or index),
                    chapter_start=int(arc_payload.get("chapter_start", 1) or 1),
                    chapter_end=int(arc_payload.get("chapter_end", 0) or 0),
                    planned_target_size=int(arc_payload.get("target_size", 0) or 0),
                    planned_soft_min=int(arc_payload.get("soft_min", 0) or 0),
                    planned_soft_max=int(arc_payload.get("soft_max", 0) or 0),
                )
            )
        session.flush()
        return created

