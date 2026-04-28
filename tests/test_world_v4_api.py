from __future__ import annotations

import inspect

from fastapi import FastAPI
from fastapi.testclient import TestClient

from forwin import api_route_registry
from forwin.api_schemas import WorldModelV4DebugResponse
from forwin.api_world_model_v4_routes import register_world_model_v4_routes
from forwin.models import Project
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.planning.world_contracts import (
    ArcWorldContract,
    RevealLadderStep,
    WorldContractRepository,
)
from forwin.protocol.world_v4 import (
    Belief,
    BeliefStatus,
    DeltaKind,
    DeltaSource,
    DeltaSourceType,
    GapStatus,
    KnowledgeGap,
    ObserverType,
    ReaderExperienceDelta,
    TruthRelation,
    WorldDelta,
    WorldLine,
)
from forwin.world_model_v4.repository import WorldModelRepository


def test_v4_debug_endpoint_exposes_world_model_diagnostics() -> None:
    engine = get_engine(postgres_test_url())
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        project = Project(
            title="V4 API",
            premise="新星殖民与母星危机",
            genre="科幻",
            setting_summary="殖民地与母星",
        )
        session.add(project)
        session.flush()
        project_id = project.id
        repo = WorldModelRepository(session)
        repo.create_world_line(
            WorldLine(
                world_line_id="line_colony_defense",
                project_id=project_id,
                line_type="primary_visible_line",
                is_visible_onstage=True,
            )
        )
        repo.create_world_line(
            WorldLine(
                world_line_id="line_homeworld_siege",
                project_id=project_id,
                line_type="hidden_parallel_line",
                objective_state_summary="父亲在母星被围",
            )
        )
        repo.create_or_update_gap(
            KnowledgeGap(
                gap_id="gap_homeworld_siege",
                project_id=project_id,
                objective_truth="父亲在母星被围",
                related_world_line_id="line_homeworld_siege",
                status=GapStatus.OPEN,
            )
        )
        repo.append_world_delta(
            WorldDelta(
                delta_id="delta_hint_callsign",
                project_id=project_id,
                world_line_id="line_homeworld_siege",
                delta_kind=DeltaKind.HINT,
                summary="乱码通讯与父亲旧部呼号",
                narrative_chapter=23,
                source=DeltaSource(source_type=DeltaSourceType.INFORMATION_SPREAD),
            )
        )
        repo.append_world_delta(
            WorldDelta(
                delta_id="delta_rejected_early_reveal",
                project_id=project_id,
                world_line_id="line_homeworld_siege",
                delta_kind=DeltaKind.REVEAL,
                summary="提前揭示父亲被围",
                narrative_chapter=23,
                source=DeltaSource(source_type=DeltaSourceType.INFORMATION_SPREAD),
                allowed_for_canon=False,
            )
        )
        repo.append_belief(
            Belief(
                belief_id="belief_protagonist_homeworld_safe",
                holder_type=ObserverType.CHARACTER,
                holder_id="protagonist",
                proposition="母星仍然安全",
                truth_relation=TruthRelation.FALSE,
                belief_status=BeliefStatus.SUSPECTED,
            ),
            project_id=project_id,
        )
        repo.append_reader_experience_delta(
            ReaderExperienceDelta(
                reader_experience_delta_id="reader_exp_ch23",
                project_id=project_id,
                chapter_number=23,
                cognition_transition="hidden -> hinted",
                payoff_type="short_term_hint",
                promise_debt_change=1,
                next_desire="旧时代坐标真正价值是什么？",
            )
        )
        WorldContractRepository(session).save_arc_contract(
            ArcWorldContract(
                contract_id="arc2",
                project_id=project_id,
                arc_id="arc2",
                arc_number=2,
                primary_world_line_ids=["line_colony_defense"],
                hidden_world_line_ids=["line_homeworld_siege"],
                major_gap_ids=["gap_homeworld_siege"],
                reveal_ladder=[
                    RevealLadderStep(
                        gap_id="gap_homeworld_siege",
                        chapter_hint=25,
                        from_state="hinted",
                        to_state="partially_revealed",
                        method="残缺求援",
                    )
                ],
            )
        )

    app = FastAPI()
    register_world_model_v4_routes(app, get_session=Session)
    response = TestClient(app).get(f"/api/projects/{project_id}/world-model/v4/debug")

    assert response.status_code == 200
    payload = WorldModelV4DebugResponse.model_validate(response.json())
    assert payload.active_world_lines == ["line_colony_defense", "line_homeworld_siege"]
    assert payload.hidden_world_lines == ["line_homeworld_siege"]
    assert payload.open_gaps == ["gap_homeworld_siege"]
    assert payload.planned_reveals[0]["gap_id"] == "gap_homeworld_siege"
    assert payload.accepted_delta_ids == ["delta_hint_callsign"]
    assert payload.rejected_delta_ids == ["delta_rejected_early_reveal"]
    assert payload.protagonist_beliefs == ["母星仍然安全"]
    assert payload.promise_debts == ["旧时代坐标真正价值是什么？"]


def test_v4_debug_route_is_registered_in_main_route_registry() -> None:
    source = inspect.getsource(api_route_registry.register_api_routes)
    assert "world-model/v4/debug" in source
