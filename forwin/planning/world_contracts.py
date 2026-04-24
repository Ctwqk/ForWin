from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.models.world_v4 import (
    ArcWorldContractRow,
    BandWorldContractRow,
    ChapterWorldDeltaIntentRow,
)


class RevealLadderStep(BaseModel):
    gap_id: str
    chapter_hint: int = 0
    from_state: str = ""
    to_state: str = ""
    method: str = ""
    fairness_evidence: list[str] = Field(default_factory=list)
    must_not_reveal_before: int | None = None


class ReaderCognitionTransition(BaseModel):
    chapter_hint: int = 0
    observer_id: str = "reader"
    from_state: str = ""
    to_state: str = ""
    intended_effect: str = ""
    payoff_type: str = ""


class ArcWorldContract(BaseModel):
    contract_id: str
    project_id: str
    arc_id: str
    arc_number: int = 1
    title: str = ""
    primary_world_line_ids: list[str] = Field(default_factory=list)
    hidden_world_line_ids: list[str] = Field(default_factory=list)
    antagonist_world_line_ids: list[str] = Field(default_factory=list)
    institutional_world_line_ids: list[str] = Field(default_factory=list)
    environmental_world_line_ids: list[str] = Field(default_factory=list)
    major_gap_ids: list[str] = Field(default_factory=list)
    false_belief_ids: list[str] = Field(default_factory=list)
    reveal_ladder: list[RevealLadderStep] = Field(default_factory=list)
    reader_cognition_trajectory: list[ReaderCognitionTransition] = Field(default_factory=list)
    short_term_payoff_promises: list[str] = Field(default_factory=list)
    medium_term_payoff_promises: list[str] = Field(default_factory=list)
    long_term_payoff_promises: list[str] = Field(default_factory=list)
    arc_exit_objective_state: str = ""
    arc_exit_reader_state: str = ""
    arc_exit_character_cognition_state: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class BandWorldContract(BaseModel):
    contract_id: str
    project_id: str
    arc_id: str
    band_id: str
    chapter_start: int
    chapter_end: int
    foreground_world_line_ids: list[str] = Field(default_factory=list)
    hidden_world_line_ids: list[str] = Field(default_factory=list)
    required_hints: list[str] = Field(default_factory=list)
    gap_transitions: dict[str, str] = Field(default_factory=dict)
    false_belief_adjustments: dict[str, str] = Field(default_factory=dict)
    payoff_commitments: list[str] = Field(default_factory=list)
    band_exit_reader_state: str = ""
    band_exit_hidden_line_state: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class ChapterWorldDeltaIntent(BaseModel):
    intent_id: str
    project_id: str
    chapter_plan_id: str = ""
    chapter_number: int
    visible_delta_intents: list[str] = Field(default_factory=list)
    offscreen_delta_intents: list[str] = Field(default_factory=list)
    hint_delta_intents: list[str] = Field(default_factory=list)
    knowledge_delta_intents: list[str] = Field(default_factory=list)
    reveal_delta_intents: list[str] = Field(default_factory=list)
    false_belief_delta_intents: list[str] = Field(default_factory=list)
    reader_experience_intents: list[str] = Field(default_factory=list)
    must_not_reveal: list[str] = Field(default_factory=list)
    delta_sources: list[str] = Field(default_factory=list)
    expected_observer_state_changes: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


def _dump_model(model: BaseModel) -> str:
    return json.dumps(model.model_dump(mode="json"), ensure_ascii=False)


def _load_json(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


class WorldContractRepository:
    """Persistence API for v4 planning contracts."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def save_arc_contract(self, contract: ArcWorldContract) -> ArcWorldContractRow:
        row = self.session.execute(
            select(ArcWorldContractRow)
            .where(
                ArcWorldContractRow.project_id == contract.project_id,
                ArcWorldContractRow.arc_id == contract.arc_id,
            )
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            row = ArcWorldContractRow(
                project_id=contract.project_id,
                arc_id=contract.arc_id,
                arc_number=contract.arc_number,
            )
            self.session.add(row)
        row.arc_number = contract.arc_number
        row.contract_json = _dump_model(contract)
        row.status = "active"
        self.session.flush()
        return row

    def get_arc_contract(self, project_id: str, arc_id: str) -> ArcWorldContract | None:
        row = self.session.execute(
            select(ArcWorldContractRow)
            .where(
                ArcWorldContractRow.project_id == project_id,
                ArcWorldContractRow.arc_id == arc_id,
            )
            .order_by(ArcWorldContractRow.updated_at.desc(), ArcWorldContractRow.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        return ArcWorldContract.model_validate(_load_json(row.contract_json)) if row else None

    def save_band_contract(self, contract: BandWorldContract) -> BandWorldContractRow:
        row = self.session.execute(
            select(BandWorldContractRow)
            .where(
                BandWorldContractRow.project_id == contract.project_id,
                BandWorldContractRow.arc_id == contract.arc_id,
                BandWorldContractRow.band_id == contract.band_id,
            )
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            row = BandWorldContractRow(
                project_id=contract.project_id,
                arc_id=contract.arc_id,
                band_id=contract.band_id,
            )
            self.session.add(row)
        row.chapter_start = contract.chapter_start
        row.chapter_end = contract.chapter_end
        row.contract_json = _dump_model(contract)
        row.status = "active"
        self.session.flush()
        return row

    def get_band_contract(self, project_id: str, band_id: str) -> BandWorldContract | None:
        row = self.session.execute(
            select(BandWorldContractRow)
            .where(
                BandWorldContractRow.project_id == project_id,
                BandWorldContractRow.band_id == band_id,
            )
            .order_by(BandWorldContractRow.updated_at.desc(), BandWorldContractRow.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        return BandWorldContract.model_validate(_load_json(row.contract_json)) if row else None

    def get_band_contract_for_chapter(
        self,
        project_id: str,
        chapter_number: int,
    ) -> BandWorldContract | None:
        row = self.session.execute(
            select(BandWorldContractRow)
            .where(
                BandWorldContractRow.project_id == project_id,
                BandWorldContractRow.chapter_start <= chapter_number,
                BandWorldContractRow.chapter_end >= chapter_number,
            )
            .order_by(
                BandWorldContractRow.updated_at.desc(),
                BandWorldContractRow.id.desc(),
            )
            .limit(1)
        ).scalar_one_or_none()
        return BandWorldContract.model_validate(_load_json(row.contract_json)) if row else None

    def save_chapter_intent(
        self,
        intent: ChapterWorldDeltaIntent,
    ) -> ChapterWorldDeltaIntentRow:
        row = self.session.execute(
            select(ChapterWorldDeltaIntentRow)
            .where(
                ChapterWorldDeltaIntentRow.project_id == intent.project_id,
                ChapterWorldDeltaIntentRow.chapter_number == intent.chapter_number,
            )
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            row = ChapterWorldDeltaIntentRow(
                project_id=intent.project_id,
                chapter_plan_id=intent.chapter_plan_id,
                chapter_number=intent.chapter_number,
            )
            self.session.add(row)
        row.chapter_plan_id = intent.chapter_plan_id
        row.intent_json = _dump_model(intent)
        row.status = "planned"
        self.session.flush()
        return row

    def get_chapter_intent(
        self,
        project_id: str,
        chapter_number: int,
    ) -> ChapterWorldDeltaIntent | None:
        row = self.session.execute(
            select(ChapterWorldDeltaIntentRow)
            .where(
                ChapterWorldDeltaIntentRow.project_id == project_id,
                ChapterWorldDeltaIntentRow.chapter_number == chapter_number,
            )
            .order_by(
                ChapterWorldDeltaIntentRow.updated_at.desc(),
                ChapterWorldDeltaIntentRow.id.desc(),
            )
            .limit(1)
        ).scalar_one_or_none()
        return ChapterWorldDeltaIntent.model_validate(_load_json(row.intent_json)) if row else None
