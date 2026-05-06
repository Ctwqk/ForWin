from __future__ import annotations

from forwin.orchestrator.goals import load_goals_json


def test_load_goals_json_wraps_legacy_string_goal() -> None:
    assert load_goals_json('"引入旧港废墟线索"') == ["引入旧港废墟线索"]


def test_load_goals_json_ignores_non_list_non_string_payload() -> None:
    assert load_goals_json('{"goal":"引入旧港"}') == []

