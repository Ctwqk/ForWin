from __future__ import annotations

from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.planning.world_contract_service import WorldContractPlanningService
from forwin.planning.world_contracts import WorldContractRepository
from forwin.state.updater import StateUpdater


def test_world_contract_service_preserves_arc_band_and_chapter_intent_semantics() -> None:
    engine = get_engine(postgres_test_url("world-contract-service"))
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        updater = StateUpdater(session)
        project = updater.create_project(
            title="殖民地与母星",
            premise="台前建设殖民地，幕后母星危机推进。",
            genre="科幻",
        )
        arc = updater.create_arc_plan(
            project.id,
            "Arc 2：新星殖民与母星危机",
            arc_number=2,
            chapter_start=21,
            chapter_end=28,
        )
        chapter_plans = [
            updater.create_chapter_plan(
                project_id=project.id,
                arc_plan_id=arc.id,
                chapter_number=chapter_number,
                title=f"第{chapter_number}章",
                one_line=(
                    "修复殖民地防线，收到乱码通讯和父亲旧部呼号"
                    if chapter_number == 23
                    else "推进新星殖民地与母星危机双线"
                ),
                goals=[
                    "推进殖民地防线",
                    "幕后推进母星危机",
                    "第25章残缺求援，第28章确认母星被围",
                ],
            )
            for chapter_number in range(21, 29)
        ]

        WorldContractPlanningService().ensure_for_arc_band(
            session=session,
            project_id=project.id,
            arc_id=arc.id,
            chapter_plans=chapter_plans,
            activation_chapter=21,
            detailed_band_size=3,
        )

        repo = WorldContractRepository(session)
        arc_contract = repo.get_arc_contract(project.id, arc.id)
        band_contract = repo.get_band_contract(project.id, "band:21:23")
        chapter_intent = repo.get_chapter_intent(project.id, 23)

    assert arc_contract is not None
    assert arc_contract.primary_world_line_ids == ["line_colony_defense"]
    assert arc_contract.hidden_world_line_ids == ["line_homeworld_siege"]
    assert [(step.chapter_hint, step.to_state) for step in arc_contract.reveal_ladder] == [
        (22, "hinted"),
        (25, "partially_revealed"),
        (28, "closed"),
    ]
    assert band_contract is not None
    assert band_contract.required_hints == ["乱码通讯", "父亲旧部呼号"]
    assert chapter_intent is not None
    assert chapter_intent.offscreen_delta_intents == ["敌方切断第三通讯阵列"]
    assert chapter_intent.must_not_reveal == ["father_sieged"]
