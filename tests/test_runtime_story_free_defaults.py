from __future__ import annotations

from types import SimpleNamespace

from forwin.director.arc_director import ArcDirector

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

