from __future__ import annotations

from forwin.orchestrator.goals import load_goals_json, normalize_goals_payload


def test_load_goals_json_wraps_legacy_string_goal() -> None:
    assert load_goals_json('"引入旧港废墟线索"') == ["引入旧港废墟线索"]


def test_load_goals_json_ignores_non_list_non_string_payload() -> None:
    assert load_goals_json('{"goal":"引入旧港"}') == []


def test_normalize_goals_payload_skips_single_character_fragments() -> None:
    assert normalize_goals_payload(["揭", "示", "周", "揭示周岚火灾记忆"]) == ["揭示周岚火灾记忆"]
