from __future__ import annotations

from forwin.canon_quality.character_state import analyze_character_state_transitions


def test_terminal_character_active_without_bridge_is_error() -> None:
    previous = [
        {
            "character_name": "沈砚",
            "chapter_number": 23,
            "transition_type": "life_state",
            "to_state": "dead",
            "terminality": "hard_terminal",
            "can_participate": False,
        }
    ]

    signals, transitions = analyze_character_state_transitions(
        project_id="p1",
        chapter_number=24,
        draft_id="d1",
        body="沈砚推门而入，拔枪协助林澈突围。",
        previous_transitions=previous,
        central_characters={"沈砚"},
    )

    assert any(item.signal_type == "terminal_state_active_conflict" for item in signals)
    assert signals[0].severity == "error"
    assert any(item.character_name == "沈砚" and item.transition_type == "participation" for item in transitions)


def test_terminal_character_with_mistaken_death_bridge_passes() -> None:
    previous = [
        {
            "character_name": "沈砚",
            "chapter_number": 23,
            "transition_type": "life_state",
            "to_state": "dead",
            "terminality": "hard_terminal",
            "can_participate": False,
        }
    ]

    signals, transitions = analyze_character_state_transitions(
        project_id="p1",
        chapter_number=24,
        draft_id="d1",
        body="沈砚推门而入。林澈这才明白，之前的死亡证明是伪造的，处决只是伪装。",
        previous_transitions=previous,
        central_characters={"沈砚"},
    )

    assert not [item for item in signals if item.severity == "error"]
    assert any(item.transition_type == "bridge_event" for item in transitions)


def test_terminal_keywords_do_not_apply_to_every_character_globally() -> None:
    signals, transitions = analyze_character_state_transitions(
        project_id="p1",
        chapter_number=8,
        draft_id="d8",
        body=(
            "林澈刚从档案公会侧门出来，洛庭若最后那句话仍在耳边。"
            "沈宴秋从广场雕像的阴影里冲出来，拉住林澈逃入地下旧轨。"
            "林澈发现沈宴秋受伤，袖口下有一枚皮下追踪器。"
        ),
        previous_transitions=[],
        central_characters={"林澈", "洛庭若", "沈宴秋"},
    )

    assert not signals
    assert not [
        item
        for item in transitions
        if item.character_name in {"林澈", "洛庭若"} and item.terminality != "none"
    ]


def test_legacy_auto_terminal_transition_without_trigger_is_ignored() -> None:
    previous = [
        {
            "character_name": "林澈",
            "chapter_number": 8,
            "transition_type": "life_state",
            "to_state": "terminally_wounded",
            "terminality": "soft_terminal",
            "can_participate": False,
            "payload": {"draft_id": "legacy-draft"},
        }
    ]

    signals, transitions = analyze_character_state_transitions(
        project_id="p1",
        chapter_number=10,
        draft_id="d10",
        body="林澈推门而入，冷静观察巡检员制服细节后带路突围。",
        previous_transitions=previous,
        central_characters={"林澈"},
    )

    assert not [item for item in signals if item.signal_type == "terminal_state_active_conflict"]
    assert any(item.character_name == "林澈" and item.transition_type == "participation" for item in transitions)
