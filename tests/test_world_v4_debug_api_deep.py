from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from forwin.api_world_model_v4_routes import register_world_model_v4_routes
from forwin.models import Project
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.planning.world_contracts import (
    ArcWorldContract,
    RevealLadderStep,
    WorldContractRepository,
)
from forwin.protocol.world_v4 import (
    GapObserverState,
    GapStatus,
    KnowledgeGap,
    ObserverType,
    RevealEvent,
    VisibilityState,
    WorldLine,
)
from forwin.world_model_v4.repository import WorldModelRepository


def _build_client():
    engine = get_engine(postgres_test_url())
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        project = Project(
            title="Debug API",
            premise="母星危机",
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
                title="殖民地防线",
                is_visible_onstage=True,
            )
        )
        repo.create_world_line(
            WorldLine(
                world_line_id="line_homeworld_siege",
                project_id=project_id,
                line_type="hidden_parallel_line",
                title="母星围困线",
                objective_state_summary="Day 30 父亲在母星被围",
            )
        )
        repo.create_or_update_gap(
            KnowledgeGap(
                gap_id="gap_homeworld_siege",
                project_id=project_id,
                objective_truth="Day 30 父亲在母星被围",
                related_world_line_id="line_homeworld_siege",
                status=GapStatus.HINTED,
                observer_states={
                    "reader": GapObserverState(
                        observer_type=ObserverType.READER,
                        observer_id="reader",
                        visibility=VisibilityState.HINTED,
                    )
                },
                fairness_requirements=["第22章通讯延迟"],
            )
        )
        repo.append_reveal_event(
            RevealEvent(
                reveal_event_id="reveal_ch25_distress",
                project_id=project_id,
                related_gap_id="gap_homeworld_siege",
                reveal_to_reader=True,
                reveal_to_characters=["protagonist"],
                reveal_method="残缺求援",
                from_state=VisibilityState.HINTED,
                to_state=VisibilityState.PARTIALLY_REVEALED,
            )
        )
        WorldContractRepository(session).save_arc_contract(
            ArcWorldContract(
                contract_id="arc2",
                project_id=project_id,
                arc_id="arc2",
                arc_number=2,
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
    return TestClient(app), project_id


def test_world_model_v4_debug_endpoints_expose_lines_gaps_reveals_and_export() -> None:
    client, project_id = _build_client()

    lines = client.get(f"/api/projects/{project_id}/world-model/v4/lines")
    gaps = client.get(f"/api/projects/{project_id}/world-model/v4/gaps")
    reveals = client.get(f"/api/projects/{project_id}/world-model/v4/reveals")
    export = client.get(f"/api/projects/{project_id}/world-model/v4/export")

    assert lines.status_code == 200
    assert [item["world_line_id"] for item in lines.json()] == [
        "line_colony_defense",
        "line_homeworld_siege",
    ]

    assert gaps.status_code == 200
    gap_payload = gaps.json()[0]
    assert gap_payload["gap_id"] == "gap_homeworld_siege"
    assert gap_payload["observer_states"]["reader"]["visibility"] == "hinted"
    assert gap_payload["fairness_requirements"] == ["第22章通讯延迟"]

    assert reveals.status_code == 200
    reveal_sources = {item["source"] for item in reveals.json()}
    assert reveal_sources == {"planned", "actual"}

    assert export.status_code == 200
    exported = export.json()
    assert exported["debug"]["hidden_world_lines"] == ["line_homeworld_siege"]
    assert exported["reveals"][0]["gap_id"] == "gap_homeworld_siege"


def test_world_model_v4_debug_endpoints_return_404_for_missing_project() -> None:
    client, _ = _build_client()

    response = client.get("/api/projects/missing/world-model/v4/export")

    assert response.status_code == 404
