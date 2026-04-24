from __future__ import annotations

import json

from sqlalchemy import select

from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.governance import DecisionEvent
from forwin.models.phase import BandExperiencePlan
from forwin.models.project import ArcPlanVersion, ChapterPlan
from forwin.models.subworld import SubWorld
from forwin.models.world_v4 import ScenarioRehearsalRunRow
from forwin.governance import DecisionEventType
from forwin.planning.scenario_rehearsal import ScenarioRehearsalRunner
from forwin.planning.scenario_rehearsal_resolution import ScenarioRehearsalCoordinator
from forwin.planning.world_contracts import (
    ArcWorldContract,
    BandWorldContract,
    ChapterWorldDeltaIntent,
    RevealLadderStep,
    WorldContractRepository,
)
from forwin.protocol.scenario_rehearsal import ScenarioRehearsalRecommendation
from forwin.state.updater import StateUpdater


def _setup_project(session, *, chapter_start: int = 21, chapter_end: int = 24):
    updater = StateUpdater(session)
    project = updater.create_project(
        title="Scenario Resolution",
        premise="母星围城与殖民地误判",
        genre="科幻",
    )
    arc = updater.create_arc_plan(
        project.id,
        "Arc：母星围城",
        arc_number=1,
        chapter_start=chapter_start,
        chapter_end=chapter_end,
    )
    chapters = [
        updater.create_chapter_plan(
            project_id=project.id,
            arc_plan_id=arc.id,
            chapter_number=chapter_number,
            title=f"第{chapter_number}章",
            one_line=f"推进第{chapter_number}章",
            goals=["推进危机", "保持信息差"],
        )
        for chapter_number in range(chapter_start, chapter_end + 1)
    ]
    return project, arc, chapters


def test_patch_resolution_adds_reveal_ladder_and_rehearses_again() -> None:
    engine = get_engine(":memory:")
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        project, arc, chapters = _setup_project(session)
        contracts = WorldContractRepository(session)
        contracts.save_arc_contract(
            ArcWorldContract(
                contract_id="arc_contract",
                project_id=project.id,
                arc_id=arc.id,
                hidden_world_line_ids=["line_homeworld_siege"],
                major_gap_ids=["gap_homeworld_siege"],
                reveal_ladder=[],
            )
        )
        contracts.save_band_contract(
            BandWorldContract(
                contract_id="band_contract",
                project_id=project.id,
                arc_id=arc.id,
                band_id="band:21:24",
                chapter_start=21,
                chapter_end=24,
                hidden_world_line_ids=["line_homeworld_siege"],
                required_hints=["乱码通讯"],
            )
        )
        contracts.save_chapter_intent(
            ChapterWorldDeltaIntent(
                intent_id="chapter_23_intent",
                project_id=project.id,
                chapter_plan_id=chapters[2].id,
                chapter_number=23,
                hint_delta_intents=["乱码通讯"],
                must_not_reveal=["father_sieged"],
                expected_observer_state_changes={"reader": "hidden -> hinted"},
            )
        )

        outcome = ScenarioRehearsalCoordinator(session).run_for_band(
            project_id=project.id,
            arc_id=arc.id,
            band_id="band:21:24",
            chapter_numbers=[chapter.chapter_number for chapter in chapters],
        )

        updated_contract = contracts.get_arc_contract(project.id, arc.id)
        rows = session.execute(
            select(ScenarioRehearsalRunRow)
            .where(ScenarioRehearsalRunRow.project_id == project.id)
            .order_by(ScenarioRehearsalRunRow.created_at.asc(), ScenarioRehearsalRunRow.id.asc())
        ).scalars().all()

        assert outcome.status == "patched_passed"
        assert outcome.report.recommendation == ScenarioRehearsalRecommendation.PASS
        assert updated_contract is not None
        assert updated_contract.reveal_ladder
        assert json.loads(rows[-1].report_json)["resolution_status"] == "patched_passed"
        assert json.loads(rows[-1].report_json)["patch_attempt_count"] == 1


def test_subworld_without_roster_is_a_patchable_rehearsal_risk() -> None:
    engine = get_engine(":memory:")
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        project, arc, chapters = _setup_project(session, chapter_start=1, chapter_end=4)
        subworld = SubWorld(
            project_id=project.id,
            origin_arc_id=arc.id,
            name="灰港",
            purpose="新地图与势力入口",
            scope="arc_local",
            status="active",
        )
        session.add(subworld)
        session.flush()
        session.add(
            BandExperiencePlan(
                project_id=project.id,
                arc_id=arc.id,
                band_id="band:1:4",
                chapter_start=1,
                chapter_end=4,
                schedule_json=json.dumps(
                    {
                        "band_id": "band:1:4",
                        "chapter_start": 1,
                        "chapter_end": 4,
                        "active_subworld_ids": [subworld.id],
                        "chapter_entry_targets": [],
                    },
                    ensure_ascii=False,
                ),
            )
        )

        report = ScenarioRehearsalRunner(session).run_for_band(
            project_id=project.id,
            arc_id=arc.id,
            band_id="band:1:4",
            chapter_numbers=[chapter.chapter_number for chapter in chapters],
        )

        assert report.recommendation == ScenarioRehearsalRecommendation.PATCH
        assert any(finding.risk_type == "subworld_roster_empty" for finding in report.risk_findings)
        assert any(patch.patch_type == "add_subworld_roster_slot" for patch in report.required_plan_patches)


def test_early_reveal_before_visibility_guard_blocks_rehearsal() -> None:
    engine = get_engine(":memory:")
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        project, arc, chapters = _setup_project(session, chapter_start=10, chapter_end=12)
        contracts = WorldContractRepository(session)
        contracts.save_arc_contract(
            ArcWorldContract(
                contract_id="arc_contract",
                project_id=project.id,
                arc_id=arc.id,
                hidden_world_line_ids=["line_homeworld_siege"],
                major_gap_ids=["father_sieged"],
                reveal_ladder=[
                    RevealLadderStep(
                        gap_id="father_sieged",
                        chapter_hint=10,
                        from_state="hidden",
                        to_state="revealed",
                        must_not_reveal_before=20,
                    )
                ],
            )
        )
        contracts.save_chapter_intent(
            ChapterWorldDeltaIntent(
                intent_id="chapter_10_intent",
                project_id=project.id,
                chapter_plan_id=chapters[0].id,
                chapter_number=10,
                reveal_delta_intents=["father_sieged"],
                must_not_reveal=["father_sieged"],
            )
        )

        report = ScenarioRehearsalRunner(session).run_for_band(
            project_id=project.id,
            arc_id=arc.id,
            band_id="band:10:12",
            chapter_numbers=[chapter.chapter_number for chapter in chapters],
        )

        assert report.recommendation == ScenarioRehearsalRecommendation.BLOCK
        assert any(finding.risk_type == "early_reveal_blocker" for finding in report.risk_findings)


def test_replan_resolution_creates_new_plan_version_and_governance_events() -> None:
    engine = get_engine(":memory:")
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        project, arc, chapters = _setup_project(session, chapter_start=31, chapter_end=34)
        contracts = WorldContractRepository(session)
        contracts.save_arc_contract(
            ArcWorldContract(
                contract_id="arc_contract",
                project_id=project.id,
                arc_id=arc.id,
                hidden_world_line_ids=["line_false_signal"],
                major_gap_ids=["gap_false_signal"],
                false_belief_ids=["false_signal"],
                reveal_ladder=[
                    RevealLadderStep(
                        gap_id="gap_false_signal",
                        chapter_hint=31,
                        from_state="hidden",
                        to_state="hinted",
                    )
                ],
                reader_cognition_trajectory=[],
            )
        )
        contracts.save_band_contract(
            BandWorldContract(
                contract_id="band_contract",
                project_id=project.id,
                arc_id=arc.id,
                band_id="band:31:34",
                chapter_start=31,
                chapter_end=34,
                required_hints=["失真信号"],
                false_belief_adjustments={"false_signal": "reader accepts a decoy"},
            )
        )

        outcome = ScenarioRehearsalCoordinator(session).run_for_band(
            project_id=project.id,
            arc_id=arc.id,
            band_id="band:31:34",
            chapter_numbers=[chapter.chapter_number for chapter in chapters],
        )

        old_arc = session.get(ArcPlanVersion, arc.id)
        active_arc = session.execute(
            select(ArcPlanVersion)
            .where(ArcPlanVersion.project_id == project.id, ArcPlanVersion.status == "active")
            .order_by(ArcPlanVersion.version.desc())
            .limit(1)
        ).scalar_one()
        updated_chapter_plans = session.execute(
            select(ChapterPlan)
            .where(ChapterPlan.project_id == project.id)
            .order_by(ChapterPlan.chapter_number.asc())
        ).scalars().all()
        updated_contract = contracts.get_arc_contract(project.id, active_arc.id)
        event_types = [
            row.event_type
            for row in session.execute(
                select(DecisionEvent)
                .where(DecisionEvent.project_id == project.id)
                .order_by(DecisionEvent.created_at.asc(), DecisionEvent.id.asc())
            ).scalars().all()
        ]

        assert outcome.status == "replanned_passed"
        assert outcome.report.recommendation == ScenarioRehearsalRecommendation.PASS
        assert old_arc is not None
        assert old_arc.status == "superseded"
        assert active_arc.id != arc.id
        assert active_arc.version == arc.version + 1
        assert all(plan.arc_plan_id == active_arc.id for plan in updated_chapter_plans)
        assert updated_contract is not None
        assert updated_contract.reader_cognition_trajectory
        assert json.loads(outcome.report.metadata["replan"])["new_arc_id"] == active_arc.id
        assert DecisionEventType.SCENARIO_REHEARSAL_EVALUATED in event_types
        assert DecisionEventType.SCENARIO_REHEARSAL_REPLAN_REQUIRED in event_types
