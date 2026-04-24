from __future__ import annotations

import json
from enum import Enum

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.models.phase import WorldProjectionDeltaRow
from forwin.protocol.world_v4 import (
    ApprovedWorldChangeSet,
    ExtractedWorldChangeSet,
    WorldCompileRequest,
    WorldCompileResult,
    WorldDelta,
)
from forwin.world_model_v4.compiler import WorldModelCompiler


class ProjectionLayer(str, Enum):
    ACTUAL_STATE = "actual_state"
    PLANNED_PROJECTION = "planned_projection"
    PROVISIONAL_PROJECTION = "provisional_projection"


class WorldProjectionSnapshot(BaseModel):
    project_id: str
    projection_id: str
    actual_delta_ids: list[str] = Field(default_factory=list)
    planned_delta_ids: list[str] = Field(default_factory=list)
    provisional_delta_ids: list[str] = Field(default_factory=list)


class WorldModelProvisionalStore:
    """Shadow-layer storage for planned/provisional v4 deltas.

    This store never writes actual canon. Promotion delegates to
    `WorldModelCompiler`, preserving the compiler as the only canon writer.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def record_delta(
        self,
        *,
        project_id: str,
        projection_id: str,
        layer: ProjectionLayer,
        world_delta: WorldDelta,
        metadata: dict[str, object] | None = None,
    ) -> WorldProjectionDeltaRow:
        if layer == ProjectionLayer.ACTUAL_STATE:
            raise ValueError("actual_state deltas must be written by WorldModelCompiler")
        world_delta = world_delta.model_copy(update={"project_id": project_id})
        row = WorldProjectionDeltaRow(
            project_id=project_id,
            projection_id=projection_id,
            projection_layer=layer.value,
            delta_id=world_delta.delta_id,
            chapter_number=int(world_delta.narrative_chapter or 0),
            world_delta_json=json.dumps(
                world_delta.model_dump(mode="json"),
                ensure_ascii=False,
            ),
            metadata_json=json.dumps(metadata or {}, ensure_ascii=False),
        )
        self.session.add(row)
        self.session.flush()
        return row

    def load_projection(self, project_id: str, projection_id: str) -> WorldProjectionSnapshot:
        rows = list(
            self.session.execute(
                select(WorldProjectionDeltaRow)
                .where(
                    WorldProjectionDeltaRow.project_id == project_id,
                    WorldProjectionDeltaRow.projection_id == projection_id,
                )
                .order_by(
                    WorldProjectionDeltaRow.chapter_number.asc(),
                    WorldProjectionDeltaRow.created_at.asc(),
                    WorldProjectionDeltaRow.id.asc(),
                )
            )
            .scalars()
            .all()
        )
        planned = [
            row.delta_id
            for row in rows
            if row.projection_layer == ProjectionLayer.PLANNED_PROJECTION.value
        ]
        provisional = [
            row.delta_id
            for row in rows
            if row.projection_layer == ProjectionLayer.PROVISIONAL_PROJECTION.value
        ]
        actual = [
            row.delta_id
            for row in rows
            if row.promoted_compile_run_id
        ]
        return WorldProjectionSnapshot(
            project_id=project_id,
            projection_id=projection_id,
            actual_delta_ids=actual,
            planned_delta_ids=planned,
            provisional_delta_ids=provisional,
        )

    def promote_delta(
        self,
        projection_delta_row_id: str,
        *,
        compiler: WorldModelCompiler,
        approved_by: list[str],
        review_verdict_id: str = "",
        promotion_reason: str = "",
    ) -> WorldCompileResult:
        row = self.session.get(WorldProjectionDeltaRow, projection_delta_row_id)
        if row is None:
            raise ValueError(f"unknown projection delta: {projection_delta_row_id}")
        if row.promoted_compile_run_id:
            raise ValueError(f"projection delta already promoted: {projection_delta_row_id}")

        world_delta = WorldDelta.model_validate(json.loads(row.world_delta_json or "{}"))
        extracted = ExtractedWorldChangeSet(
            project_id=row.project_id,
            chapter_number=row.chapter_number,
            world_deltas=[world_delta],
        )
        approved = ApprovedWorldChangeSet.from_extracted(
            extracted,
            approved_by=approved_by,
            review_verdict_id=review_verdict_id,
        )
        result = compiler.compile(
            WorldCompileRequest(
                project_id=row.project_id,
                chapter_number=row.chapter_number,
                approved_changes=approved,
                review_verdict_id=review_verdict_id,
                compiler_run_id=f"promote_{row.id}",
            )
        )
        row.promoted_compile_run_id = result.compiler_run_id
        row.promotion_review_verdict_id = review_verdict_id
        row.promotion_reason = promotion_reason
        self.session.flush()
        return result
