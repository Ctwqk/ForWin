from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.models.world_v4 import (
    ArcWorldContractRow,
    BeliefRow,
    KnowledgeGapRow,
    ReaderExperienceDeltaRow,
    WorldCompileRunV4Row,
    WorldDeltaRow,
    WorldLineRow,
)
from forwin.planning.world_contracts import ArcWorldContract


class WorldModelExportPage(BaseModel):
    title: str
    body: str = ""
    state_layer: str
    world_line_id: str = ""
    as_of_chapter: int = 0
    as_of_story_time: str = ""
    visibility: str = ""
    truth_relation: str = ""
    source_refs: list[str] = Field(default_factory=list)


def _load_json(raw: str, default: Any) -> Any:
    try:
        return json.loads(raw or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _source_refs(raw: str) -> list[str]:
    payload = _load_json(raw, [])
    return [str(item) for item in payload] if isinstance(payload, list) else []


class WorldModelExporter:
    """Build derived human/debug views from v4 ledgers."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def export_project(
        self,
        project_id: str,
        *,
        as_of_chapter: int,
    ) -> list[WorldModelExportPage]:
        lines = self._world_lines(project_id)
        deltas = self._world_deltas(project_id, as_of_chapter)
        gaps = self._knowledge_gaps(project_id)
        beliefs = self._beliefs(project_id)
        reader_experience = self._reader_experience(project_id, as_of_chapter)
        reveal_ladder = self._reveal_ladder(project_id)
        compile_runs = self._compile_runs(project_id, as_of_chapter)

        pages = [
            self._page(
                "Actual World State",
                _dump(
                    {
                        "source_delta_ids": [delta.delta_id for delta in deltas],
                        "objective_state": [delta.summary for delta in deltas],
                    }
                ),
                state_layer="actual_state",
                as_of_chapter=as_of_chapter,
                source_refs=[ref for delta in deltas for ref in _source_refs(delta.source_refs_json)],
            ),
            self._page(
                "Objective Timeline",
                _dump(
                    [
                        {
                            "story_time": delta.objective_story_time,
                            "chapter": delta.narrative_chapter,
                            "summary": delta.summary,
                        }
                        for delta in deltas
                    ]
                ),
                state_layer="actual_state",
                as_of_chapter=as_of_chapter,
                as_of_story_time=self._latest_story_time(deltas),
            ),
            self._page(
                "World Lines",
                _dump(
                    [
                        {
                            "world_line_id": line.world_line_id,
                            "line_type": line.line_type,
                            "visible": line.is_visible_onstage,
                            "objective_state_summary": line.objective_state_summary,
                        }
                        for line in lines
                    ]
                ),
                state_layer="world_line_projection",
                world_line_id=lines[0].world_line_id if lines else "",
                as_of_chapter=as_of_chapter,
                source_refs=[ref for line in lines for ref in _source_refs(line.source_refs_json)],
            ),
            self._page(
                "World Delta Sources",
                _dump(
                    [
                        {
                            "delta_id": delta.delta_id,
                            "source_type": delta.source_type,
                            "source_actor_id": delta.source_actor_id,
                            "source_evidence_refs": _load_json(
                                delta.source_evidence_refs_json,
                                [],
                            ),
                        }
                        for delta in deltas
                    ]
                ),
                state_layer="delta_source_projection",
                as_of_chapter=as_of_chapter,
            ),
            self._page(
                "Reader Cognition",
                _dump(
                    [
                        {
                            "belief_id": belief.belief_id,
                            "proposition": belief.proposition,
                            "truth_relation": belief.truth_relation,
                            "status": belief.belief_status,
                        }
                        for belief in beliefs
                        if belief.holder_type == "reader"
                    ]
                ),
                state_layer="reader_cognition",
                as_of_chapter=as_of_chapter,
                truth_relation="mixed",
            ),
            self._page(
                "Character Cognition",
                _dump(
                    [
                        {
                            "holder_id": belief.holder_id,
                            "belief_id": belief.belief_id,
                            "proposition": belief.proposition,
                            "truth_relation": belief.truth_relation,
                            "status": belief.belief_status,
                        }
                        for belief in beliefs
                        if belief.holder_type == "character"
                    ]
                ),
                state_layer="character_cognition",
                as_of_chapter=as_of_chapter,
                truth_relation="mixed",
            ),
            self._page(
                "Knowledge Gaps",
                _dump(
                    [
                        {
                            "gap_id": gap.gap_id,
                            "objective_truth": gap.objective_truth,
                            "status": gap.status,
                            "observer_states": _load_json(gap.observer_states_json, {}),
                        }
                        for gap in gaps
                    ]
                ),
                state_layer="knowledge_gap",
                world_line_id=gaps[0].related_world_line_id if gaps else "",
                as_of_chapter=as_of_chapter,
                visibility=gaps[0].status if gaps else "",
                truth_relation="true",
                source_refs=[ref for gap in gaps for ref in _source_refs(gap.source_refs_json)],
            ),
            self._page(
                "Reveal Ladder",
                _dump(reveal_ladder),
                state_layer="reveal_projection",
                as_of_chapter=as_of_chapter,
            ),
            self._page(
                "Fair Misdirection",
                _dump(
                    {
                        "gap_requirements": [
                            {
                                "gap_id": gap.gap_id,
                                "requirements": _load_json(
                                    gap.fairness_requirements_json,
                                    [],
                                ),
                            }
                            for gap in gaps
                        ],
                        "reveal_fairness": [
                            step.get("fairness_evidence", [])
                            for step in reveal_ladder
                        ],
                    }
                ),
                state_layer="fair_misdirection",
                as_of_chapter=as_of_chapter,
            ),
            self._page(
                "Short / Medium / Long Term Delight",
                _dump(
                    [
                        {
                            "chapter": item.chapter_number,
                            "transition": item.cognition_transition,
                            "payoff_type": item.payoff_type,
                            "reward_tags": _load_json(item.reward_tags_json, []),
                            "next_desire": item.next_desire,
                        }
                        for item in reader_experience
                    ]
                ),
                state_layer="reader_experience",
                as_of_chapter=as_of_chapter,
            ),
            self._page(
                "Review Checks",
                _dump(
                    [
                        {
                            "compiler_run_id": run.compiler_run_id,
                            "chapter": run.chapter_number,
                            "committed": run.committed,
                            "blocked_reasons": _load_json(
                                run.blocked_reasons_json,
                                [],
                            ),
                        }
                        for run in compile_runs
                    ]
                ),
                state_layer="review_checks",
                as_of_chapter=as_of_chapter,
            ),
        ]
        return pages

    def _page(
        self,
        title: str,
        body: str,
        *,
        state_layer: str,
        as_of_chapter: int,
        world_line_id: str = "",
        as_of_story_time: str = "",
        visibility: str = "",
        truth_relation: str = "",
        source_refs: list[str] | None = None,
    ) -> WorldModelExportPage:
        return WorldModelExportPage(
            title=title,
            body=body,
            state_layer=state_layer,
            world_line_id=world_line_id,
            as_of_chapter=as_of_chapter,
            as_of_story_time=as_of_story_time,
            visibility=visibility,
            truth_relation=truth_relation,
            source_refs=source_refs or [],
        )

    def _world_lines(self, project_id: str) -> list[WorldLineRow]:
        return list(
            self.session.execute(
                select(WorldLineRow)
                .where(WorldLineRow.project_id == project_id)
                .order_by(WorldLineRow.created_at.asc(), WorldLineRow.id.asc())
            )
            .scalars()
            .all()
        )

    def _world_deltas(self, project_id: str, as_of_chapter: int) -> list[WorldDeltaRow]:
        return list(
            self.session.execute(
                select(WorldDeltaRow)
                .where(
                    WorldDeltaRow.project_id == project_id,
                    WorldDeltaRow.narrative_chapter <= as_of_chapter,
                )
                .order_by(
                    WorldDeltaRow.narrative_chapter.asc(),
                    WorldDeltaRow.created_at.asc(),
                    WorldDeltaRow.id.asc(),
                )
            )
            .scalars()
            .all()
        )

    def _knowledge_gaps(self, project_id: str) -> list[KnowledgeGapRow]:
        return list(
            self.session.execute(
                select(KnowledgeGapRow)
                .where(KnowledgeGapRow.project_id == project_id)
                .order_by(KnowledgeGapRow.created_at.asc(), KnowledgeGapRow.id.asc())
            )
            .scalars()
            .all()
        )

    def _beliefs(self, project_id: str) -> list[BeliefRow]:
        return list(
            self.session.execute(
                select(BeliefRow)
                .where(BeliefRow.project_id == project_id)
                .order_by(BeliefRow.created_at.asc(), BeliefRow.id.asc())
            )
            .scalars()
            .all()
        )

    def _reader_experience(
        self,
        project_id: str,
        as_of_chapter: int,
    ) -> list[ReaderExperienceDeltaRow]:
        return list(
            self.session.execute(
                select(ReaderExperienceDeltaRow)
                .where(
                    ReaderExperienceDeltaRow.project_id == project_id,
                    ReaderExperienceDeltaRow.chapter_number <= as_of_chapter,
                )
                .order_by(
                    ReaderExperienceDeltaRow.chapter_number.asc(),
                    ReaderExperienceDeltaRow.created_at.asc(),
                    ReaderExperienceDeltaRow.id.asc(),
                )
            )
            .scalars()
            .all()
        )

    def _reveal_ladder(self, project_id: str) -> list[dict[str, Any]]:
        rows = list(
            self.session.execute(
                select(ArcWorldContractRow)
                .where(
                    ArcWorldContractRow.project_id == project_id,
                    ArcWorldContractRow.status == "active",
                )
                .order_by(
                    ArcWorldContractRow.arc_number.asc(),
                    ArcWorldContractRow.updated_at.desc(),
                    ArcWorldContractRow.id.desc(),
                )
            )
            .scalars()
            .all()
        )
        steps: list[dict[str, Any]] = []
        for row in rows:
            payload = _load_json(row.contract_json, {})
            if isinstance(payload, dict):
                contract = ArcWorldContract.model_validate(payload)
                steps.extend(step.model_dump(mode="json") for step in contract.reveal_ladder)
        return steps

    def _compile_runs(
        self,
        project_id: str,
        as_of_chapter: int,
    ) -> list[WorldCompileRunV4Row]:
        return list(
            self.session.execute(
                select(WorldCompileRunV4Row)
                .where(
                    WorldCompileRunV4Row.project_id == project_id,
                    WorldCompileRunV4Row.chapter_number <= as_of_chapter,
                )
                .order_by(
                    WorldCompileRunV4Row.chapter_number.asc(),
                    WorldCompileRunV4Row.created_at.asc(),
                    WorldCompileRunV4Row.id.asc(),
                )
            )
            .scalars()
            .all()
        )

    @staticmethod
    def _latest_story_time(deltas: list[WorldDeltaRow]) -> str:
        for delta in reversed(deltas):
            if delta.objective_story_time:
                return delta.objective_story_time
        return ""
