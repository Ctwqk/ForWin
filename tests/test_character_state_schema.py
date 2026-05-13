from __future__ import annotations

from forwin.state.schema import validate_state_payload


def test_character_state_accepts_life_and_participation_fields() -> None:
    payload = validate_state_payload(
        "character",
        {
            "status": "重伤",
            "life_state": "terminally_wounded",
            "custody_state": "missing",
            "injury_state": "critical",
            "participation_state": "impossible",
            "terminal_event_id": "event-23",
            "terminal_event_chapter": "23",
            "bridge_event_id": "bridge-35",
        },
    )

    assert payload["life_state"] == "terminally_wounded"
    assert payload["participation_state"] == "impossible"
