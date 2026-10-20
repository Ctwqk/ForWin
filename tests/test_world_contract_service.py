from __future__ import annotations

from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.planning.world_contract_service import WorldContractPlanningService
from forwin.planning.world_contracts import WorldContractRepository
from forwin.runtime.policy import RuntimePolicy
from forwin.state.updater import StateUpdater
from tests.postgres import postgres_test_url


_GENERIC_PREMISES = [
    "台前建设殖民地，幕后母星危机推进。",
    "一位年轻的账房先生在边陲小镇经营一家当铺。",
]


def _run_service_for_premise(premise: str, db_name: str):
    engine = get_engine(postgres_test_url(db_name))
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        updater = StateUpdater(session)
        project = updater.create_project(
            title="任意书名",
            premise=premise,
            genre="科幻",
            runtime_policy=RuntimePolicy.for_profile("standard"),
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
        chapter_intents = {
            number: repo.get_chapter_intent(project.id, number) for number in range(21, 29)
        }
        return (
            project.id,
            arc.id,
            arc.arc_synopsis,
            [(plan.id, plan.chapter_number) for plan in chapter_plans],
            arc_contract,
            band_contract,
            chapter_intents,
        )


def test_world_contract_service_emits_generic_contracts_for_any_premise() -> None:
    for index, premise in enumerate(_GENERIC_PREMISES):
        (
            project_id,
            arc_id,
            synopsis,
            plan_ids,
            arc_contract,
            band_contract,
            chapter_intents,
        ) = _run_service_for_premise(premise, f"world-contract-service-{index}")

        assert arc_contract is not None
        assert arc_contract.contract_id == f"arc_world_contract_{arc_id}"
        assert arc_contract.arc_number == 2
        assert arc_contract.title == synopsis
        assert arc_contract.primary_world_line_ids == ["primary_visible_line"]
        assert arc_contract.hidden_world_line_ids == []
        assert arc_contract.major_gap_ids == []
        assert arc_contract.reveal_ladder == []
        assert arc_contract.reader_cognition_trajectory == []
        assert arc_contract.medium_term_payoff_promises == []
        assert arc_contract.long_term_payoff_promises == []
        assert arc_contract.arc_exit_objective_state == synopsis
        assert arc_contract.arc_exit_reader_state == "aware"

        assert band_contract is not None
        assert band_contract.contract_id == f"band_world_contract_{arc_id}_21_23"
        assert band_contract.band_id == "band:21:23"
        assert (band_contract.chapter_start, band_contract.chapter_end) == (21, 23)
        assert band_contract.foreground_world_line_ids == ["primary_visible_line"]
        assert band_contract.hidden_world_line_ids == []
        assert band_contract.required_hints == []
        assert band_contract.gap_transitions == {}
        assert band_contract.payoff_commitments == []
        assert band_contract.band_exit_reader_state == "aware"
        assert band_contract.band_exit_hidden_line_state == ""

        assert [n for n, intent in chapter_intents.items() if intent is not None] == [21, 22, 23]
        plan_id_by_number = {number: plan_id for plan_id, number in plan_ids}
        for number in (21, 22, 23):
            intent = chapter_intents[number]
            assert intent.intent_id == f"chapter_{number}_world_intent"
            assert intent.chapter_plan_id == plan_id_by_number[number]
            assert intent.visible_delta_intents == ["推进殖民地防线"]
            assert intent.offscreen_delta_intents == []
            assert intent.hint_delta_intents == []
            assert intent.knowledge_delta_intents == []
            assert intent.reader_experience_intents == []
            assert intent.must_not_reveal == []
            assert intent.delta_sources == []
            assert intent.expected_observer_state_changes == {}
