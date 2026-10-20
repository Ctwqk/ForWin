from __future__ import annotations

from types import SimpleNamespace

from forwin.director.arc_director import ArcDirector
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.runtime.policy import RuntimePolicy
from forwin.simulation.world import WorldSimulator
from forwin.state.updater import StateUpdater
from tests.postgres import postgres_test_url

_EMPTY_INITIAL_TIME = {"label": "", "description": ""}


def test_arc_director_core_fallback_invents_no_story_content() -> None:
    # A blank api_key makes ArcDirector._call_json return the fallback untouched.
    director = ArcDirector(SimpleNamespace(api_key=""), max_tokens=2048)

    core = director._plan_core("任意前提", "任意类型")

    assert core == {
        "arc_synopsis": "",
        "setting_summary": "",
        "initial_time": _EMPTY_INITIAL_TIME,
    }


def test_arc_director_plan_arc_defaults_initial_time_to_empty() -> None:
    # The model answers with an empty object, so every core field is missing.
    director = ArcDirector(SimpleNamespace(chat=lambda *args, **kwargs: "{}"), max_tokens=2048)

    plan = director.plan_arc("任意前提", "任意类型", 1)

    assert plan["initial_time"] == _EMPTY_INITIAL_TIME
    assert plan["arc_synopsis"] == ""
    assert plan["setting_summary"] == ""
    assert "雨" not in str(plan["initial_time"])


def test_world_simulator_deterministic_turn_has_no_chapter_number_threshold() -> None:
    engine = get_engine(postgres_test_url("world-sim-no-chapter-threshold"))
    init_db(engine)
    session = get_session_factory(engine)()
    try:
        project = StateUpdater(session).create_project(
            title="书",
            premise="p",
            genre="g",
            runtime_policy=RuntimePolicy.for_profile("standard"),
        )
        simulator = WorldSimulator(llm_client=None)

        early = simulator.simulate(session=session, project_id=project.id, chapter_number=1)
        late = simulator.simulate(session=session, project_id=project.id, chapter_number=40)
    finally:
        session.close()
        engine.dispose()

    # No threads, no stale threads and no reader signal: late chapters must not be
    # escalated just because of their number.
    assert late.pressure_level == early.pressure_level == "steady"
    assert late.notable_shifts == early.notable_shifts
