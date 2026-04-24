from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.models import Project
from forwin.models.world_v4 import CognitionSnapshotRow, WorldLineRow
from forwin.protocol.world_v4 import WorldLine, WorldModelSnapshot
from forwin.world_model_v4.projection import WorldModelProjection
from forwin.world_model_v4.repository import WorldModelRepository


def _initial_objective_summary(project: Project) -> str:
    parts = [
        str(project.setting_summary or "").strip(),
        str(project.premise or "").strip(),
    ]
    return "；".join(part for part in parts if part)


def _ensure_cognition_snapshot(
    session: Session,
    *,
    project_id: str,
    observer_type: str,
    observer_id: str,
) -> CognitionSnapshotRow:
    existing = session.execute(
        select(CognitionSnapshotRow)
        .where(
            CognitionSnapshotRow.project_id == project_id,
            CognitionSnapshotRow.observer_type == observer_type,
            CognitionSnapshotRow.observer_id == observer_id,
            CognitionSnapshotRow.as_of_chapter == 0,
        )
        .limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    row = CognitionSnapshotRow(
        project_id=project_id,
        cognition_state_id=f"cognition_{observer_type}_{observer_id}_ch0",
        observer_type=observer_type,
        observer_id=observer_id,
        as_of_chapter=0,
        as_of_story_time="genesis",
        beliefs_json="[]",
        known_delta_ids_json="[]",
        suspected_gap_ids_json="[]",
        visibility_by_delta_json="{}",
        metadata_json=json.dumps({"bootstrap": True}, ensure_ascii=False),
    )
    session.add(row)
    session.flush()
    return row


def bootstrap_initial_world_model(
    session: Session,
    project_id: str,
) -> WorldModelSnapshot:
    """Create the minimum v4 world model for an existing project."""

    project = session.get(Project, project_id)
    if project is None:
        raise ValueError(f"Project not found: {project_id}")

    repo = WorldModelRepository(session)
    existing_line = session.execute(
        select(WorldLineRow)
        .where(
            WorldLineRow.project_id == project_id,
            WorldLineRow.world_line_id == "primary_visible_line",
        )
        .limit(1)
    ).scalar_one_or_none()
    if existing_line is None:
        repo.create_world_line(
            WorldLine(
                world_line_id="primary_visible_line",
                project_id=project_id,
                line_type="primary_visible_line",
                title="主线台前世界",
                objective_state_summary=_initial_objective_summary(project),
                is_visible_onstage=True,
                source_refs=["bootstrap:project"],
                metadata={"bootstrap": True},
            )
        )

    _ensure_cognition_snapshot(
        session,
        project_id=project_id,
        observer_type="reader",
        observer_id="reader",
    )
    _ensure_cognition_snapshot(
        session,
        project_id=project_id,
        observer_type="character",
        observer_id="protagonist",
    )

    return WorldModelProjection(session).rebuild_snapshot(project_id, as_of_chapter=0)
