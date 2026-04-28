from __future__ import annotations

import json
from types import SimpleNamespace

from sqlalchemy import func, select

from forwin.config import Config
from forwin.models import ProvisionalBandExecution
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.world_v4 import ScenarioRehearsalRunRow
from forwin.orchestrator.phase24 import ArcEnvelopeManager
from forwin.orchestrator.loop import WritingOrchestrator
from forwin.planning.scenario_rehearsal import ScenarioRehearsalRepository, ScenarioRehearsalRunner
from forwin.planning.world_contracts import (
    ArcWorldContract,
    BandWorldContract,
    ChapterWorldDeltaIntent,
    RevealLadderStep,
    WorldContractRepository,
)
from forwin.protocol.scenario_rehearsal import ScenarioRehearsalRecommendation
from forwin.state.updater import StateUpdater


def test_project_arc_snapshot_payload_exposes_scenario_rehearsal_fields() -> None:
    from forwin.api_project_payloads import project_arc_snapshot_payload

    payload = project_arc_snapshot_payload(
        None,
        None,
        None,
        latest_scenario_rehearsal=SimpleNamespace(
            band_id="band:21:24",
            recommendation="patch",
            risk_count=2,
            blocker_count=0,
            required_patch_count=1,
            report_json=json.dumps(
                {
                    "resolution_status": "manual_patch_required",
                    "trigger_reasons": ["must_not_reveal_guard"],
                    "patch_attempt_count": 1,
                    "checkpoint_id": "checkpoint_1",
                    "replan_event_id": "",
                }
            ),
        ),
    )

    assert payload["scenario_rehearsal_band_id"] == "band:21:24"
    assert payload["scenario_rehearsal_recommendation"] == "patch"
    assert payload["scenario_rehearsal_risk_count"] == 2
    assert payload["scenario_rehearsal_blocker_count"] == 0
    assert payload["scenario_rehearsal_required_patch_count"] == 1
    assert payload["scenario_rehearsal_resolution_status"] == "manual_patch_required"
    assert payload["scenario_rehearsal_trigger_reasons"] == ["must_not_reveal_guard"]
    assert payload["scenario_rehearsal_patch_attempt_count"] == 1
    assert payload["scenario_rehearsal_checkpoint_id"] == "checkpoint_1"


def _seed_project_with_chapters(session, *, chapter_start: int = 1, chapter_end: int = 4):
    updater = StateUpdater(session)
    project = updater.create_project(
        title="Scenario Rehearsal",
        premise="殖民地防线与母星通讯危机",
        genre="科幻",
    )
    arc = updater.create_arc_plan(
        project.id,
        "Arc：通讯危机",
        arc_number=1,
        chapter_start=chapter_start,
        chapter_end=chapter_end,
    )
    chapters = []
    for chapter_number in range(chapter_start, chapter_end + 1):
        chapters.append(
            updater.create_chapter_plan(
                project_id=project.id,
                arc_plan_id=arc.id,
                chapter_number=chapter_number,
                title=f"第{chapter_number}章",
                one_line=f"推进第{chapter_number}章危机",
                goals=["推进", "保持悬念"],
            )
        )
    return project, arc, chapters


def test_scenario_rehearsal_records_patch_when_visibility_plan_lacks_reveal_ladder() -> None:
    engine = get_engine(postgres_test_url())
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        project, arc, chapters = _seed_project_with_chapters(session, chapter_start=21, chapter_end=24)
        contracts = WorldContractRepository(session)
        contracts.save_arc_contract(
            ArcWorldContract(
                contract_id="arc_contract",
                project_id=project.id,
                arc_id=arc.id,
                arc_number=1,
                hidden_world_line_ids=["line_homeworld_siege"],
                major_gap_ids=["gap_homeworld_siege"],
                reveal_ladder=[],
                long_term_payoff_promises=["母星危机最终兑现"],
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
                payoff_commitments=["本 band 只给 mystery hint"],
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

        report = ScenarioRehearsalRunner(session).run_for_band(
            project_id=project.id,
            arc_id=arc.id,
            band_id="band:21:24",
            chapter_numbers=[chapter.chapter_number for chapter in chapters],
        )

        assert report.recommendation == ScenarioRehearsalRecommendation.PATCH
        assert "must_not_reveal_guard" in report.trigger_reasons
        assert any(finding.risk_type == "missing_reveal_ladder" for finding in report.risk_findings)
        assert any(patch.patch_type == "add_reveal_ladder" for patch in report.required_plan_patches)

        latest = ScenarioRehearsalRepository(session).latest_for_project(project.id)
        assert latest is not None
        assert latest.recommendation == ScenarioRehearsalRecommendation.PATCH
        assert session.scalar(select(func.count()).select_from(ScenarioRehearsalRunRow)) == 1


def test_scenario_rehearsal_low_risk_band_records_pass_skip() -> None:
    engine = get_engine(postgres_test_url())
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        project, arc, chapters = _seed_project_with_chapters(session)

        report = ScenarioRehearsalRunner(session).run_for_band(
            project_id=project.id,
            arc_id=arc.id,
            band_id="band:1:4",
            chapter_numbers=[chapter.chapter_number for chapter in chapters],
        )

        assert report.recommendation == ScenarioRehearsalRecommendation.PASS
        assert report.trigger_reasons == ["low_risk_skip"]
        assert report.risk_findings == []


def test_arc_envelope_prefers_scenario_rehearsal_over_legacy_preview() -> None:
    engine = get_engine(postgres_test_url())
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        project, _arc, _chapters = _seed_project_with_chapters(session)

        def legacy_preview_should_not_run(**_kwargs):
            raise AssertionError("legacy provisional preview should not run when scenario rehearsal is available")

        manager = ArcEnvelopeManager(
            director=None,
            provisional_executor=legacy_preview_should_not_run,
        )
        envelope = manager.ensure_active_arc_resolution(
            session=session,
            project_id=project.id,
            activation_chapter=1,
        )

        assert envelope is not None
        assert session.scalar(select(func.count()).select_from(ScenarioRehearsalRunRow)) == 1
        assert session.scalar(select(func.count()).select_from(ProvisionalBandExecution)) == 0


def test_legacy_provisional_failure_no_longer_blocks_by_default_but_switch_can_restore() -> None:
    engine = get_engine(postgres_test_url())
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        project, arc, _chapters = _seed_project_with_chapters(session)
        session.add(
            ProvisionalBandExecution(
                project_id=project.id,
                arc_id=arc.id,
                band_id="band:1:4",
                chapter_numbers_json=json.dumps([1, 2, 3, 4]),
                aggregate_verdict="fail",
                failure_count=1,
            )
        )
        project_id = project.id

    with Session() as session:
        default_orchestrator = WritingOrchestrator(Config(database_url=postgres_test_url()))
        try:
            assert default_orchestrator._new_failed_provisional_gate(
                session,
                project_id=project_id,
                previous_snapshot=None,
            ) is None
        finally:
            default_orchestrator.llm_client.close()
            default_orchestrator.engine.dispose()

        legacy_orchestrator = WritingOrchestrator(
            Config(database_url=postgres_test_url(), legacy_provisional_blocking=True)
        )
        try:
            gate = legacy_orchestrator._new_failed_provisional_gate(
                session,
                project_id=project_id,
                previous_snapshot=None,
            )
            assert gate is not None
            assert gate.aggregate_verdict == "fail"
        finally:
            legacy_orchestrator.llm_client.close()
            legacy_orchestrator.engine.dispose()
