from __future__ import annotations

from forwin.models import Project
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.planning.world_contracts import (
    ArcWorldContract,
    ChapterWorldDeltaIntent,
    RevealLadderStep,
    WorldContractRepository,
)
from forwin.protocol.context import (
    CognitionPack,
    CompilerPack,
    PlanningPack,
    ReaderExperiencePack,
    RevealPack,
    ReviewPack,
    WritingPack,
)
from forwin.protocol.world_v4 import (
    GapObserverState,
    GapStatus,
    KnowledgeGap,
    ObserverType,
    VisibilityState,
    WorldLine,
)
from forwin.retrieval.broker import RetrievalBroker
from forwin.state.repo import StateRepository
from forwin.world_model_v4.repository import WorldModelRepository


def test_role_specific_retrieval_packs_filter_hidden_truth_for_writer_only() -> None:
    engine = get_engine(":memory:")
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        project = Project(
            title="Arc 2",
            premise="新星殖民与母星危机",
            genre="科幻",
            setting_summary="殖民地与母星双线",
        )
        session.add(project)
        session.flush()
        project_id = project.id

        world_repo = WorldModelRepository(session)
        world_repo.create_world_line(
            WorldLine(
                world_line_id="line_colony_defense",
                project_id=project_id,
                line_type="primary_visible_line",
                title="殖民地防线",
                is_visible_onstage=True,
            )
        )
        world_repo.create_world_line(
            WorldLine(
                world_line_id="line_homeworld_siege",
                project_id=project_id,
                line_type="hidden_parallel_line",
                title="母星围困线",
                objective_state_summary="Day 30 父亲在母星被围",
            )
        )
        world_repo.create_or_update_gap(
            KnowledgeGap(
                gap_id="gap_homeworld_siege",
                project_id=project_id,
                objective_truth="Day 30 父亲在母星被围",
                related_world_line_id="line_homeworld_siege",
                status=GapStatus.OPEN,
                observer_states={
                    "reader": GapObserverState(
                        observer_type=ObserverType.READER,
                        observer_id="reader",
                        visibility=VisibilityState.HIDDEN,
                    )
                },
            )
        )
        contracts = WorldContractRepository(session)
        contracts.save_arc_contract(
            ArcWorldContract(
                contract_id="arc2_contract",
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
                        must_not_reveal_before=25,
                    )
                ],
            )
        )
        contracts.save_chapter_intent(
            ChapterWorldDeltaIntent(
                intent_id="ch23",
                project_id=project_id,
                chapter_number=23,
                hint_delta_intents=["乱码通讯", "父亲旧部呼号"],
                must_not_reveal=["father_sieged"],
            )
        )

    with Session() as session:
        broker = RetrievalBroker()
        repo = StateRepository(session)
        packs = {
            kind: broker.build_world_model_pack(repo, project_id, 23, kind)
            for kind in (
                "planning",
                "writing",
                "review",
                "compiler",
                "reader_experience",
                "cognition",
                "reveal",
            )
        }

    assert isinstance(packs["planning"], PlanningPack)
    assert isinstance(packs["writing"], WritingPack)
    assert isinstance(packs["review"], ReviewPack)
    assert isinstance(packs["compiler"], CompilerPack)
    assert isinstance(packs["reader_experience"], ReaderExperiencePack)
    assert isinstance(packs["cognition"], CognitionPack)
    assert isinstance(packs["reveal"], RevealPack)

    writer_dump = str(packs["writing"].model_dump(mode="json"))
    assert "Day 30 父亲在母星被围" not in writer_dump
    assert packs["writing"].hidden_objective_truths == []
    assert packs["writing"].must_not_reveal == ["father_sieged"]

    assert packs["review"].hidden_objective_truths == ["Day 30 父亲在母星被围"]
    assert packs["compiler"].hidden_objective_truths == ["Day 30 父亲在母星被围"]
    assert packs["review"].planned_reveal_ladder[0].gap_id == "gap_homeworld_siege"
    assert packs["compiler"].planned_reveal_ladder[0].chapter_hint == 25
