from __future__ import annotations

import json

from forwin.checker.hard_floor import HardFloorResult
from forwin.checker.pulp_policy import evaluate_pulp_beat_policy
from forwin.checker.pulp_beat import verify_pulp_beats
from forwin.config import Config
from forwin.governance import DecisionEventType
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.governance import DecisionEvent
from forwin.models.project import Project
from tests.postgres import postgres_test_url


def test_verify_pulp_beats_detects_core_payoff() -> None:
    body = "众人嘲笑他没资格。林远当场拿出合同，老板脸色大变，当众道歉，还赔偿三十万。门外忽然传来新的威胁。"

    result = verify_pulp_beats(body)

    assert result.pressure_present is True
    assert result.protagonist_action_present is True
    assert result.visible_payoff_present is True
    assert result.audience_reaction_present is True
    assert result.enemy_or_obstacle_damage_present is True
    assert result.new_gain_or_status_shift_present is True
    assert result.next_hook_present is True


def test_verify_pulp_beats_flags_missing_payoff() -> None:
    result = verify_pulp_beats("他走在路上，想了很多前情，夜色越来越深。")

    assert result.visible_payoff_present is False
    assert "visible_payoff_present" in result.missing_fields


def test_pulp_policy_blocks_consecutive_missing_payoff() -> None:
    engine = get_engine(postgres_test_url("pulp-policy-consecutive"))
    init_db(engine)
    Session = get_session_factory(engine)
    try:
        with Session.begin() as session:
            session.add(Project(id="project-1", title="P", premise="p", genre="都市"))
            session.flush()
            session.add(
                DecisionEvent(
                    project_id="project-1",
                    chapter_number=1,
                    event_type=DecisionEventType.PULP_BEAT_EVALUATED,
                    payload_json=json.dumps(
                        {"pulp_beat": {"visible_payoff_present": False}},
                        ensure_ascii=False,
                    ),
                )
            )

        with Session.begin() as session:
            decision = evaluate_pulp_beat_policy(
                session=session,
                project_id="project-1",
                chapter_number=2,
                hard_floor_result=HardFloorResult(
                    passed=True,
                    warning_reasons=["pulp_visible_payoff"],
                    metadata={"pulp_beat": {"visible_payoff_present": False}},
                ),
                config=Config(quality_profile="pulp"),
            )

        assert decision.fatal is True
        assert decision.reason == "pulp_visible_payoff_consecutive_missing"
        assert decision.consecutive_missing_payoff == 2
    finally:
        engine.dispose()


def test_pulp_policy_is_warning_only_for_standard_profile() -> None:
    decision = evaluate_pulp_beat_policy(
        session=object(),
        project_id="project-1",
        chapter_number=1,
        hard_floor_result=HardFloorResult(
            passed=True,
            warning_reasons=["pulp_visible_payoff"],
            metadata={"pulp_beat": {"visible_payoff_present": False}},
        ),
        config=Config(quality_profile="standard"),
    )

    assert decision.fatal is False
    assert decision.consecutive_missing_payoff == 1
