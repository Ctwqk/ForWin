from __future__ import annotations

import json
from typing import Any, Callable

from fastapi import FastAPI, HTTPException
from sqlalchemy import select

from forwin.api_schemas import (
    WorldModelV4DebugResponse,
    WorldModelV4ExportResponse,
    WorldModelV4GapInfo,
    WorldModelV4LineInfo,
    WorldModelV4RevealInfo,
)
from forwin.models.project import Project
from forwin.models.world_v4 import (
    ArcWorldContractRow,
    BeliefRow,
    KnowledgeGapRow,
    ReaderExperienceDeltaRow,
    RevealEventRow,
    WorldDeltaRow,
    WorldLineRow,
)
from forwin.planning.world_contracts import ArcWorldContract


def _load_json(raw: str, default: Any) -> Any:
    try:
        return json.loads(raw or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _planned_reveals(session, project_id: str) -> list[dict[str, Any]]:
    rows = list(
        session.execute(
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
    reveals: list[dict[str, Any]] = []
    for row in rows:
        payload = _load_json(row.contract_json, {})
        if not isinstance(payload, dict):
            continue
        contract = ArcWorldContract.model_validate(payload)
        reveals.extend(step.model_dump(mode="json") for step in contract.reveal_ladder)
    return reveals


def _require_project(session, project_id: str) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return project


def build_lines_response(session, project_id: str) -> list[WorldModelV4LineInfo]:
    _require_project(session, project_id)
    rows = list(
        session.execute(
            select(WorldLineRow)
            .where(WorldLineRow.project_id == project_id)
            .order_by(
                WorldLineRow.is_visible_onstage.desc(),
                WorldLineRow.created_at.asc(),
                WorldLineRow.id.asc(),
            )
        )
        .scalars()
        .all()
    )
    return [
        WorldModelV4LineInfo(
            world_line_id=row.world_line_id,
            line_type=row.line_type,
            title=row.title,
            objective_state_summary=row.objective_state_summary,
            is_visible_onstage=bool(row.is_visible_onstage),
            planned_reveal_chapter=row.planned_reveal_chapter,
            long_term_promise=row.long_term_promise,
            source_refs=_load_json(row.source_refs_json, []),
            metadata=_load_json(row.metadata_json, {}),
        )
        for row in rows
    ]


def build_gaps_response(session, project_id: str) -> list[WorldModelV4GapInfo]:
    _require_project(session, project_id)
    rows = list(
        session.execute(
            select(KnowledgeGapRow)
            .where(KnowledgeGapRow.project_id == project_id)
            .order_by(KnowledgeGapRow.created_at.asc(), KnowledgeGapRow.id.asc())
        )
        .scalars()
        .all()
    )
    return [
        WorldModelV4GapInfo(
            gap_id=row.gap_id,
            status=row.status,
            objective_truth=row.objective_truth,
            related_world_line_id=row.related_world_line_id,
            happened_at_story_time=row.happened_at_story_time,
            observer_states=_load_json(row.observer_states_json, {}),
            planned_closure=row.planned_closure,
            fairness_requirements=_load_json(row.fairness_requirements_json, []),
            source_refs=_load_json(row.source_refs_json, []),
            metadata=_load_json(row.metadata_json, {}),
        )
        for row in rows
    ]


def build_reveals_response(session, project_id: str) -> list[WorldModelV4RevealInfo]:
    _require_project(session, project_id)
    planned = [
        WorldModelV4RevealInfo(
            source="planned",
            gap_id=str(item.get("gap_id", "")),
            chapter_hint=item.get("chapter_hint"),
            from_state=str(item.get("from_state", "")),
            to_state=str(item.get("to_state", "")),
            method=str(item.get("method", "")),
            metadata={
                key: value
                for key, value in item.items()
                if key
                not in {
                    "gap_id",
                    "chapter_hint",
                    "from_state",
                    "to_state",
                    "method",
                }
            },
        )
        for item in _planned_reveals(session, project_id)
        if isinstance(item, dict)
    ]
    actual_rows = list(
        session.execute(
            select(RevealEventRow)
            .where(RevealEventRow.project_id == project_id)
            .order_by(RevealEventRow.created_at.asc(), RevealEventRow.id.asc())
        )
        .scalars()
        .all()
    )
    actual = [
        WorldModelV4RevealInfo(
            source="actual",
            gap_id=row.related_gap_id,
            reveal_event_id=row.reveal_event_id,
            from_state=row.from_state,
            to_state=row.to_state,
            method=row.reveal_method,
            reveal_to_reader=bool(row.reveal_to_reader),
            reveal_to_characters=_load_json(row.reveal_to_characters_json, []),
            fairness_evidence=_load_json(row.fairness_evidence_json, []),
            metadata=_load_json(row.metadata_json, {}),
        )
        for row in actual_rows
    ]
    return [*planned, *actual]


def build_debug_response(session, project_id: str) -> WorldModelV4DebugResponse:
    _require_project(session, project_id)

    lines = list(
        session.execute(
            select(WorldLineRow)
            .where(WorldLineRow.project_id == project_id)
            .order_by(WorldLineRow.created_at.asc(), WorldLineRow.id.asc())
        )
        .scalars()
        .all()
    )
    lines = sorted(lines, key=lambda line: (not bool(line.is_visible_onstage), line.world_line_id))
    gaps = list(
        session.execute(
            select(KnowledgeGapRow)
            .where(
                KnowledgeGapRow.project_id == project_id,
                KnowledgeGapRow.status.in_(("open", "hinted", "partially_closed")),
            )
            .order_by(KnowledgeGapRow.created_at.asc(), KnowledgeGapRow.id.asc())
        )
        .scalars()
        .all()
    )
    deltas = list(
        session.execute(
            select(WorldDeltaRow)
            .where(WorldDeltaRow.project_id == project_id)
            .order_by(
                WorldDeltaRow.narrative_chapter.asc(),
                WorldDeltaRow.created_at.asc(),
                WorldDeltaRow.id.asc(),
            )
        )
        .scalars()
        .all()
    )
    beliefs = list(
        session.execute(
            select(BeliefRow)
            .where(BeliefRow.project_id == project_id)
            .order_by(BeliefRow.created_at.asc(), BeliefRow.id.asc())
        )
        .scalars()
        .all()
    )
    reader_experience = list(
        session.execute(
            select(ReaderExperienceDeltaRow)
            .where(ReaderExperienceDeltaRow.project_id == project_id)
            .order_by(
                ReaderExperienceDeltaRow.chapter_number.asc(),
                ReaderExperienceDeltaRow.created_at.asc(),
                ReaderExperienceDeltaRow.id.asc(),
            )
        )
        .scalars()
        .all()
    )

    visible_lines = [line.world_line_id for line in lines if line.is_visible_onstage]
    hidden_lines = [
        line.world_line_id
        for line in lines
        if line.world_line_id not in visible_lines
        or any(token in line.line_type for token in ("hidden", "secret", "antagonist"))
    ]
    reader_cognition = {
        belief.belief_id: {
            "proposition": belief.proposition,
            "truth_relation": belief.truth_relation,
            "belief_status": belief.belief_status,
        }
        for belief in beliefs
        if belief.holder_type == "reader"
    }
    protagonist_beliefs = [
        belief.proposition
        for belief in beliefs
        if belief.holder_type == "character" and belief.holder_id == "protagonist"
    ]

    return WorldModelV4DebugResponse(
        project_id=project_id,
        active_world_lines=[line.world_line_id for line in lines],
        visible_world_lines=visible_lines,
        hidden_world_lines=hidden_lines,
        open_gaps=[gap.gap_id for gap in gaps],
        planned_reveals=_planned_reveals(session, project_id),
        accepted_delta_ids=[
            delta.delta_id for delta in deltas if bool(delta.allowed_for_canon)
        ],
        rejected_delta_ids=[
            delta.delta_id for delta in deltas if not bool(delta.allowed_for_canon)
        ],
        reader_cognition=reader_cognition,
        protagonist_beliefs=protagonist_beliefs,
        promise_debts=[
            item.next_desire or item.cognition_transition
            for item in reader_experience
            if int(item.promise_debt_change or 0) > 0
        ],
    )


def build_handlers(*, get_session: Callable[[], Any]) -> dict[str, Callable[..., Any]]:
    def get_world_model_v4_debug(project_id: str):
        with get_session() as session:
            return build_debug_response(session, project_id)

    def get_world_model_v4_lines(project_id: str):
        with get_session() as session:
            return build_lines_response(session, project_id)

    def get_world_model_v4_gaps(project_id: str):
        with get_session() as session:
            return build_gaps_response(session, project_id)

    def get_world_model_v4_reveals(project_id: str):
        with get_session() as session:
            return build_reveals_response(session, project_id)

    def get_world_model_v4_export(project_id: str):
        with get_session() as session:
            return WorldModelV4ExportResponse(
                project_id=project_id,
                lines=build_lines_response(session, project_id),
                gaps=build_gaps_response(session, project_id),
                reveals=build_reveals_response(session, project_id),
                debug=build_debug_response(session, project_id),
            )

    return {
        "get_world_model_v4_debug": get_world_model_v4_debug,
        "get_world_model_v4_lines": get_world_model_v4_lines,
        "get_world_model_v4_gaps": get_world_model_v4_gaps,
        "get_world_model_v4_reveals": get_world_model_v4_reveals,
        "get_world_model_v4_export": get_world_model_v4_export,
    }


def register_world_model_v4_routes(
    app: FastAPI,
    *,
    get_session: Callable[[], Any],
) -> dict[str, Callable[..., Any]]:
    handlers = build_handlers(get_session=get_session)
    app.add_api_route(
        "/api/projects/{project_id}/world-model/v4/debug",
        handlers["get_world_model_v4_debug"],
        methods=["GET"],
        response_model=WorldModelV4DebugResponse,
    )
    app.add_api_route(
        "/api/projects/{project_id}/world-model/v4/lines",
        handlers["get_world_model_v4_lines"],
        methods=["GET"],
        response_model=list[WorldModelV4LineInfo],
    )
    app.add_api_route(
        "/api/projects/{project_id}/world-model/v4/gaps",
        handlers["get_world_model_v4_gaps"],
        methods=["GET"],
        response_model=list[WorldModelV4GapInfo],
    )
    app.add_api_route(
        "/api/projects/{project_id}/world-model/v4/reveals",
        handlers["get_world_model_v4_reveals"],
        methods=["GET"],
        response_model=list[WorldModelV4RevealInfo],
    )
    app.add_api_route(
        "/api/projects/{project_id}/world-model/v4/export",
        handlers["get_world_model_v4_export"],
        methods=["GET"],
        response_model=WorldModelV4ExportResponse,
    )
    return handlers
