from __future__ import annotations

import json

from sqlalchemy import select

from forwin.api_governance_ops import approve_scenario_plan_patch, rerun_scenario_rehearsal
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.subworld import SubWorld, SubWorldRosterItem
from forwin.models.world_v4 import ScenarioPlanPatchRow, ScenarioRehearsalRunRow
from forwin.planning.scenario_rehearsal import ScenarioRehearsalRunner
from forwin.planning.scenario_rehearsal_resolution import ScenarioRehearsalCoordinator
from forwin.planning.scenario_triggers import ScenarioTriggerEvaluator
from forwin.planning.world_contracts import (
    ArcWorldContract,
    BandWorldContract,
    ChapterWorldDeltaIntent,
    WorldContractRepository,
)
from forwin.protocol.scenario_rehearsal import ScenarioRehearsalRecommendation
from forwin.state.updater import StateUpdater


def _setup_project(session, *, chapter_start: int = 1, chapter_end: int = 4):
    updater = StateUpdater(session)
    project = updater.create_project(title="完整预检", premise="误会与新地图", genre="玄幻")
    arc = updater.create_arc_plan(
        project.id,
        "Arc：误会入局",
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
            one_line="推进",
            goals=["推进主线"],
        )
        for chapter_number in range(chapter_start, chapter_end + 1)
    ]
    return project, arc, chapters


def test_trigger_evaluator_skips_low_risk_and_triggers_review_repair_future_dependencies() -> None:
    engine = get_engine(":memory:")
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        project, arc, chapters = _setup_project(session)
        evaluator = ScenarioTriggerEvaluator(session)

        low_risk = evaluator.evaluate_for_band(
            project_id=project.id,
            arc_id=arc.id,
            band_id="band:1:4",
            chapter_numbers=[chapter.chapter_number for chapter in chapters],
            boundary_kind="chapter_start",
        )
        high_risk = evaluator.evaluate_for_band(
            project_id=project.id,
            arc_id=arc.id,
            band_id="band:1:4",
            chapter_numbers=[chapter.chapter_number for chapter in chapters],
            boundary_kind="chapter_start",
            consecutive_review_failures=2,
            repair_escalated=True,
            future_dependency_refs=["secret:皇陵钥匙"],
        )

        assert low_risk.should_run is False
        assert low_risk.reasons == ["low_risk_skip"]
        assert high_risk.should_run is True
        assert "consecutive_review_fail" in high_risk.reasons
        assert "repair_escalation" in high_risk.reasons
        assert "future_dependency" in high_risk.reasons


class _DirectorPassesDanger:
    def rehearse_scenario(self, **_kwargs):
        return {
            "recommendation": "pass",
            "risk_findings": [],
            "future_conflicts": [],
            "required_plan_patches": [],
        }


class _DirectorFindsFutureLock:
    def rehearse_scenario(self, **_kwargs):
        return {
            "recommendation": "replan",
            "risk_findings": [
                {
                    "risk_type": "director_future_lock_in",
                    "severity": "warn",
                    "message": "当前误会会锁死后续 arc。",
                    "evidence_refs": ["director"],
                }
            ],
            "future_conflicts": ["future arc dependency locked"],
            "required_plan_patches": [
                {
                    "patch_type": "add_false_belief_exit",
                    "target": "arc_world_contract.reader_cognition_trajectory",
                    "message": "补 reader 退出路径。",
                    "evidence_refs": ["director"],
                }
            ],
        }


class _DirectorFails:
    def rehearse_scenario(self, **_kwargs):
        raise RuntimeError("director unavailable")


def test_hybrid_simulation_uses_director_but_deterministic_block_wins() -> None:
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
                major_gap_ids=["truth"],
                reveal_ladder=[
                    {
                        "gap_id": "truth",
                        "chapter_hint": 10,
                        "from_state": "hidden",
                        "to_state": "revealed",
                        "must_not_reveal_before": 20,
                    }
                ],
            )
        )
        contracts.save_chapter_intent(
            ChapterWorldDeltaIntent(
                intent_id="intent",
                project_id=project.id,
                chapter_plan_id=chapters[0].id,
                chapter_number=10,
                reveal_delta_intents=["truth"],
            )
        )

        report = ScenarioRehearsalRunner(session, director=_DirectorPassesDanger()).run_for_band(
            project_id=project.id,
            arc_id=arc.id,
            band_id="band:10:12",
            chapter_numbers=[chapter.chapter_number for chapter in chapters],
        )

        assert report.recommendation == ScenarioRehearsalRecommendation.BLOCK
        assert report.metadata["simulation_mode"] == "hybrid"
        assert report.metadata["director_used"] is True
        assert any(finding.risk_type == "early_reveal_blocker" for finding in report.risk_findings)


def test_hybrid_simulation_fallback_records_director_error() -> None:
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
                reader_cognition_trajectory=[],
            )
        )
        contracts.save_band_contract(
            BandWorldContract(
                contract_id="band_contract",
                project_id=project.id,
                arc_id=arc.id,
                band_id="band:1:4",
                chapter_start=1,
                chapter_end=4,
                required_hints=["假信物"],
                false_belief_adjustments={"fake_token": "reader trusts a false token"},
            )
        )

        director_report = ScenarioRehearsalRunner(session, director=_DirectorFindsFutureLock()).run_for_band(
            project_id=project.id,
            arc_id=arc.id,
            band_id="band:1:4",
            chapter_numbers=[chapter.chapter_number for chapter in chapters],
        )
        fallback_report = ScenarioRehearsalRunner(session, director=_DirectorFails()).run_for_band(
            project_id=project.id,
            arc_id=arc.id,
            band_id="band:1:4",
            chapter_numbers=[chapter.chapter_number for chapter in chapters],
        )

        assert director_report.recommendation == ScenarioRehearsalRecommendation.REPLAN
        assert any(finding.risk_type == "director_future_lock_in" for finding in director_report.risk_findings)
        assert fallback_report.metadata["director_used"] is False
        assert "director unavailable" in fallback_report.metadata["director_error"]
        assert fallback_report.recommendation == ScenarioRehearsalRecommendation.REPLAN


def test_subworld_resource_rehearsal_checks_region_node_culture_and_key_roles() -> None:
    engine = get_engine(":memory:")
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        project, arc, chapters = _setup_project(session)
        subworld = SubWorld(
            project_id=project.id,
            origin_arc_id=arc.id,
            name="灰港",
            purpose="新地图",
            scope="arc_local",
            status="active",
            metadata_json=json.dumps({"culture_profile_id": "culture-a"}, ensure_ascii=False),
        )
        session.add(subworld)
        session.flush()
        session.add(
            SubWorldRosterItem(
                project_id=project.id,
                subworld_id=subworld.id,
                display_name="灰港向导",
                role_hint="helper",
                status="planned_slot",
                metadata_json=json.dumps({"culture_profile_id": "culture-b"}, ensure_ascii=False),
            )
        )
        session.flush()
        from forwin.models.phase import BandExperiencePlan

        session.add(
            BandExperiencePlan(
                project_id=project.id,
                arc_id=arc.id,
                band_id="band:1:4",
                chapter_start=1,
                chapter_end=4,
                schedule_json=json.dumps(
                    {
                        "active_subworld_ids": [subworld.id],
                        "chapter_entry_targets": [{"subworld_id": subworld.id, "entity_name": "灰港向导"}],
                        "critical_role_slots": [
                            {"subworld_id": subworld.id, "role": "boss"},
                            {"subworld_id": subworld.id, "role": "rival"},
                        ],
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

        risk_types = {finding.risk_type for finding in report.risk_findings}
        patch_types = {patch.patch_type for patch in report.required_plan_patches}
        assert "missing_subworld_region_node" in risk_types
        assert "subworld_culture_mismatch" in risk_types
        assert "missing_critical_role_slot" in risk_types
        assert "add_subworld_anchor" in patch_types
        assert "align_subworld_culture_profile" in patch_types
        assert "add_subworld_roster_slot" in patch_types


def test_scenario_patch_rows_can_be_approved_and_rehearsal_can_rerun() -> None:
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
                hidden_world_line_ids=["hidden_line"],
                major_gap_ids=["hidden_gap"],
            )
        )
        contracts.save_band_contract(
            BandWorldContract(
                contract_id="band_contract",
                project_id=project.id,
                arc_id=arc.id,
                band_id="band:1:4",
                chapter_start=1,
                chapter_end=4,
                hidden_world_line_ids=["hidden_line"],
                required_hints=[],
            )
        )
        outcome = ScenarioRehearsalCoordinator(session).run_for_band(
            project_id=project.id,
            arc_id=arc.id,
            band_id="band:1:4",
            chapter_numbers=[chapter.chapter_number for chapter in chapters],
            max_patch_attempts=0,
        )
        run = session.execute(select(ScenarioRehearsalRunRow)).scalar_one()
        patch = session.execute(select(ScenarioPlanPatchRow)).scalar_one()
        project_id = project.id
        run_id = run.id
        patch_id = patch.id

        assert outcome.status == "manual_patch_required"
        assert patch.status == "proposed"

    detail = approve_scenario_plan_patch(
        project_id,
        patch_id,
        reason="批准自动补 hint",
        get_session=Session,
        display_datetime=lambda _value: "",
    )

    assert detail.report["applied_patch_id"] == patch_id
    assert detail.report["patch_status"] == "applied"

    rerun_detail = rerun_scenario_rehearsal(
        project_id,
        run_id,
        get_session=Session,
        display_datetime=lambda _value: "",
    )

    assert rerun_detail.project_id == project_id
    assert rerun_detail.resolution_status in {"passed", "patched_passed"}
