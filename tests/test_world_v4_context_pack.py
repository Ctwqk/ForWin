from __future__ import annotations

from forwin.context.assembler import assemble_context
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.planning.world_contracts import (
    ArcWorldContract,
    BandWorldContract,
    ChapterWorldDeltaIntent,
    WorldContractRepository,
)
from forwin.retrieval.broker import RetrievalBroker
from forwin.state.repo import StateRepository
from forwin.state.updater import StateUpdater


def test_writer_context_includes_v4_hint_intent_without_hidden_truth() -> None:
    engine = get_engine(postgres_test_url())
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        updater = StateUpdater(session)
        project = updater.create_project(
            title="新星殖民",
            premise="主角团建设新星殖民地防线，并处理越来越异常的远程通讯。",
            genre="科幻",
            setting_summary="新星殖民地与远程通讯网络",
        )
        arc = updater.create_arc_plan(
            project.id,
            "Arc 2：新星殖民与远程通讯危机",
            arc_number=2,
            chapter_start=21,
            chapter_end=28,
        )
        chapter = updater.create_chapter_plan(
            project_id=project.id,
            arc_plan_id=arc.id,
            chapter_number=23,
            title="乱码呼号",
            one_line="修复殖民地防线时收到异常通讯",
            goals=["修复殖民地防线", "处理乱码通讯", "维持 mystery hint"],
        )
        contract_repo = WorldContractRepository(session)
        contract_repo.save_arc_contract(
            ArcWorldContract(
                contract_id="arc_contract",
                project_id=project.id,
                arc_id=arc.id,
                arc_number=2,
                primary_world_line_ids=["line_colony_defense"],
                hidden_world_line_ids=["line_homeworld_siege"],
                major_gap_ids=["gap_homeworld_siege"],
            )
        )
        contract_repo.save_band_contract(
            BandWorldContract(
                contract_id="band_contract",
                project_id=project.id,
                arc_id=arc.id,
                band_id="band:21:23",
                chapter_start=21,
                chapter_end=23,
                foreground_world_line_ids=["line_colony_defense"],
                hidden_world_line_ids=["line_homeworld_siege"],
                required_hints=["乱码通讯", "父亲旧部呼号"],
                gap_transitions={"gap_homeworld_siege": "hidden -> hinted"},
            )
        )
        contract_repo.save_chapter_intent(
            ChapterWorldDeltaIntent(
                intent_id="chapter_23_intent",
                project_id=project.id,
                chapter_plan_id=chapter.id,
                chapter_number=23,
                visible_delta_intents=["殖民地防线修复"],
                hint_delta_intents=["乱码通讯", "父亲旧部呼号"],
                must_not_reveal=["father_sieged"],
                expected_observer_state_changes={
                    "reader": "hidden -> hinted",
                    "protagonist": "unknown -> suspected",
                },
            )
        )
        project_id = project.id
        chapter_id = chapter.id

    with Session() as session:
        chapter_plan = session.get(type(chapter), chapter_id)
        assert chapter_plan is not None
        raw_pack = assemble_context(StateRepository(session), project_id, chapter_plan)
        writer_pack = RetrievalBroker(
            memory_index=type("FakeMemoryIndex", (), {"search": lambda self, **_kwargs: []})(),
            include_world_v4_compat=True,
        ).build_chapter_context(
            StateRepository(session),
            project_id,
            chapter_plan,
        )

    assert raw_pack.chapter_world_delta_intent is not None
    assert raw_pack.chapter_world_delta_intent.hint_delta_intents == [
        "乱码通讯",
        "父亲旧部呼号",
    ]
    assert writer_pack.must_not_reveal == ["father_sieged"]
    assert writer_pack.active_knowledge_gaps == ["gap_homeworld_siege"]
    dumped = str(writer_pack.model_dump(mode="json"))
    assert "父亲已被围" not in dumped
    assert "父亲在母星被围" not in dumped
