from __future__ import annotations

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
from forwin.reviewer_v4 import V4ReviewGateVerdict, V4ReviewIssue
from forwin.world_model_v4.compiler import WorldModelCompiler
from forwin.world_model_v4.export import WorldModelExporter, WorldModelExportPage
from forwin.world_model_v4.repository import WorldModelRepository


def test_export_pages_cover_v4_debug_views_with_required_metadata() -> None:
    engine = get_engine(postgres_test_url())
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        project = Project(
            title="V4 Export",
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
                world_line_id="line_homeworld_siege",
                project_id=project_id,
                line_type="hidden_parallel_line",
                objective_state_summary="父亲在母星被围",
                source_refs=["genesis:world"],
            )
        )
        repo.create_or_update_gap(
            KnowledgeGap(
                gap_id="gap_homeworld_siege",
                project_id=project_id,
                objective_truth="父亲在母星被围",
                related_world_line_id="line_homeworld_siege",
                status=GapStatus.HINTED,
                fairness_requirements=["第22章通讯延迟", "第23章旧部呼号"],
                source_refs=["chapter:21"],
            )
        )
        repo.append_world_delta(
            WorldDelta(
                delta_id="delta_hint_callsign",
                project_id=project_id,
                world_line_id="line_homeworld_siege",
                delta_kind=DeltaKind.HINT,
                summary="乱码通讯与父亲旧部呼号",
                objective_story_time="Day 32",
                narrative_chapter=23,
                source=DeltaSource(
                    source_type=DeltaSourceType.INFORMATION_SPREAD,
                    evidence_refs=["chapter:23:comm"],
                ),
                source_refs=["chapter:23"],
            )
        )
        repo.append_belief(
            Belief(
                belief_id="belief_reader_homeworld_abnormal",
                holder_type=ObserverType.READER,
                holder_id="reader",
                proposition="母星通讯异常",
                truth_relation=TruthRelation.PARTIAL,
                belief_status=BeliefStatus.SUSPECTED,
                evidence_sources=["chapter:23"],
            ),
            project_id=project_id,
        )
        repo.append_belief(
            Belief(
                belief_id="belief_protagonist_distance_delay",
                holder_type=ObserverType.CHARACTER,
                holder_id="protagonist",
                proposition="通讯异常只是距离导致",
                truth_relation=TruthRelation.FALSE,
                belief_status=BeliefStatus.SUSPECTED,
                evidence_sources=["chapter:23"],
            ),
            project_id=project_id,
        )
        repo.append_reader_experience_delta(
            ReaderExperienceDelta(
                reader_experience_delta_id="reader_exp_hint",
                project_id=project_id,
                chapter_number=23,
                cognition_transition="hidden -> hinted",
                payoff_type="short_term_hint",
                reward_tags=["mystery"],
                next_desire="母星到底发生了什么？",
                source_refs=["chapter:23"],
            )
        )
        WorldContractRepository(session).save_arc_contract(
            ArcWorldContract(
                contract_id="arc2",
                project_id=project_id,
                arc_id="arc2",
                arc_number=2,
                hidden_world_line_ids=["line_homeworld_siege"],
                major_gap_ids=["gap_homeworld_siege"],
                reveal_ladder=[
                    RevealLadderStep(
                        gap_id="gap_homeworld_siege",
                        chapter_hint=25,
                        from_state="hinted",
                        to_state="partially_revealed",
                        method="残缺求援",
                        fairness_evidence=["第22章通讯延迟", "第23章旧部呼号"],
                    )
                ],
            )
        )
        WorldModelCompiler(session).compile_gate_verdict(
            project_id=project_id,
            chapter_number=23,
            verdict=V4ReviewGateVerdict(
                passed=False,
                issues=[
                    V4ReviewIssue(
                        reviewer="RevealReviewer",
                        severity="fail",
                        failure_type="early_reveal",
                        message="提前 reveal",
                    )
                ],
            ),
            compiler_run_id="compile-blocked-23",
        )

        pages = WorldModelExporter(session).export_project(project_id, as_of_chapter=23)

    assert all(isinstance(page, WorldModelExportPage) for page in pages)
    titles = {page.title for page in pages}
    assert {
        "Actual World State",
        "Objective Timeline",
        "World Lines",
        "World Delta Sources",
        "Reader Cognition",
        "Character Cognition",
        "Knowledge Gaps",
        "Reveal Ladder",
        "Fair Misdirection",
        "Review Checks",
    }.issubset(titles)

    for page in pages:
        assert page.state_layer
        assert page.as_of_chapter == 23
        assert page.world_line_id is not None
        assert page.as_of_story_time is not None
        assert page.visibility is not None
        assert page.truth_relation is not None
        assert isinstance(page.source_refs, list)

    reveal_page = next(page for page in pages if page.title == "Reveal Ladder")
    assert "残缺求援" in reveal_page.body
    fair_page = next(page for page in pages if page.title == "Fair Misdirection")
    assert "第23章旧部呼号" in fair_page.body
    review_page = next(page for page in pages if page.title == "Review Checks")
    assert "early_reveal" in review_page.body
