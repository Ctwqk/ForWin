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
