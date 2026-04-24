from __future__ import annotations

from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.orchestrator.phase24 import ArcEnvelopeManager
from forwin.state.updater import StateUpdater
from forwin.planning.world_contracts import (
    ArcWorldContract,
    BandWorldContract,
    ChapterWorldDeltaIntent,
    ReaderCognitionTransition,
    RevealLadderStep,
    WorldContractRepository,
)


def test_world_contract_repository_persists_arc_band_and_chapter_contracts() -> None:
    engine = get_engine(":memory:")
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
        chapter = updater.create_chapter_plan(
            project_id=project.id,
            arc_plan_id=arc.id,
            chapter_number=23,
            title="乱码呼号",
            one_line="主角修复防线时收到异常通讯",
            goals=["修复殖民地防线", "公平 hint 母星异常"],
        )
        repo = WorldContractRepository(session)
        repo.save_arc_contract(
            ArcWorldContract(
                contract_id="arc_world_contract_2",
                project_id=project.id,
                arc_id=arc.id,
                arc_number=2,
                title="新星殖民与母星危机",
                primary_world_line_ids=["line_colony_defense"],
                hidden_world_line_ids=["line_homeworld_siege"],
                major_gap_ids=["gap_homeworld_siege"],
                reveal_ladder=[
                    RevealLadderStep(
                        gap_id="gap_homeworld_siege",
                        chapter_hint=22,
                        from_state="hidden",
                        to_state="hinted",
                        method="通讯延迟",
                    ),
                    RevealLadderStep(
                        gap_id="gap_homeworld_siege",
                        chapter_hint=25,
                        from_state="hinted",
                        to_state="partially_revealed",
                        method="残缺求援",
                    ),
                ],
                reader_cognition_trajectory=[
                    ReaderCognitionTransition(
                        chapter_hint=22,
                        observer_id="reader",
                        from_state="hidden",
                        to_state="hinted",
                        intended_effect="不安",
                    )
                ],
                arc_exit_objective_state="殖民地成为反攻母星基础",
                arc_exit_reader_state="partially_revealed",
            )
        )
        repo.save_band_contract(
            BandWorldContract(
                contract_id="band_world_contract_21_23",
                project_id=project.id,
                arc_id=arc.id,
                band_id="band_21_23",
                chapter_start=21,
                chapter_end=23,
                foreground_world_line_ids=["line_colony_defense"],
                hidden_world_line_ids=["line_homeworld_siege"],
                required_hints=["乱码通讯", "父亲旧部呼号"],
                gap_transitions={"gap_homeworld_siege": "hidden -> hinted"},
                band_exit_reader_state="hinted",
                band_exit_hidden_line_state="母星通讯被进一步切断",
            )
        )
        repo.save_chapter_intent(
            ChapterWorldDeltaIntent(
                intent_id="chapter_23_world_intent",
                project_id=project.id,
                chapter_plan_id=chapter.id,
                chapter_number=23,
                visible_delta_intents=["殖民地防线修复"],
                offscreen_delta_intents=["敌方切断第三通讯阵列"],
                hint_delta_intents=["乱码通讯", "父亲旧部呼号"],
                knowledge_delta_intents=["主角进入 suspected 状态"],
                reader_experience_intents=["mystery hint"],
                must_not_reveal=["father_sieged"],
                expected_observer_state_changes={
                    "reader": "hidden -> hinted",
                    "protagonist": "unknown -> suspected",
                },
            )
        )

        loaded_arc = repo.get_arc_contract(project.id, arc.id)
        loaded_band = repo.get_band_contract(project.id, "band_21_23")
        loaded_chapter = repo.get_chapter_intent(project.id, 23)

    assert loaded_arc is not None
    assert loaded_arc.hidden_world_line_ids == ["line_homeworld_siege"]
    assert loaded_arc.reveal_ladder[1].to_state == "partially_revealed"
    assert loaded_band is not None
    assert loaded_band.required_hints == ["乱码通讯", "父亲旧部呼号"]
    assert loaded_chapter is not None
    assert loaded_chapter.must_not_reveal == ["father_sieged"]
    assert loaded_chapter.expected_observer_state_changes["protagonist"] == (
        "unknown -> suspected"
    )


def test_phase24_persists_homeworld_crisis_contracts_for_arc_plan() -> None:
    engine = get_engine(":memory:")
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        updater = StateUpdater(session)
        project = updater.create_project(
            title="新星殖民与母星危机",
            premise="主角团建设新星殖民地防线，幕后父亲所在母星被敌方围困。",
            genre="科幻",
            target_total_chapters=40,
        )
        arc = updater.create_arc_plan(
            project.id,
            "Arc 2：新星殖民与母星危机。台前线是殖民地站稳，幕后线是父亲母星被围。",
            arc_number=2,
            chapter_start=21,
            chapter_end=28,
        )
        chapter_plans = []
        for chapter_number in range(21, 29):
            chapter_plans.append(
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
            )

        manager = ArcEnvelopeManager(director=None)
        manager._persist_world_contracts(  # noqa: SLF001
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
    assert arc_contract.major_gap_ids == ["gap_homeworld_siege"]
    assert [(step.chapter_hint, step.to_state) for step in arc_contract.reveal_ladder] == [
        (22, "hinted"),
        (25, "partially_revealed"),
        (28, "closed"),
    ]
    assert band_contract is not None
    assert band_contract.required_hints == ["乱码通讯", "父亲旧部呼号"]
    assert band_contract.band_exit_reader_state == "hinted"
    assert chapter_intent is not None
    assert chapter_intent.offscreen_delta_intents == ["敌方切断第三通讯阵列"]
    assert chapter_intent.must_not_reveal == ["father_sieged"]
    assert chapter_intent.expected_observer_state_changes == {
        "reader": "hidden -> hinted",
        "protagonist": "unknown -> suspected",
    }
