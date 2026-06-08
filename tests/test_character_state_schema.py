from __future__ import annotations

from forwin.orchestrator_loop_core.governance import _filter_supported_state_changes
from forwin.protocol.state_change import StateChangeCandidate
from forwin.state.schema import prepare_state_change, validate_state_payload


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


def test_character_state_accepts_strong_state_ledger_fields() -> None:
    payload = validate_state_payload(
        "character",
        {
            "role_state": "第三代守门人",
            "knowledge_state": "知道通风井备份光盘位置",
            "possession_state": "持有馆员备份光盘",
        },
    )

    assert payload["role_state"] == "第三代守门人"
    assert payload["knowledge_state"] == "知道通风井备份光盘位置"
    assert payload["possession_state"] == "持有馆员备份光盘"


def test_prepare_state_change_normalizes_common_extraction_aliases() -> None:
    normalized_role, role_state = prepare_state_change(
        "character",
        {},
        "role",
        "临时证人",
    )
    normalized_knowledge, knowledge_state = prepare_state_change(
        "character",
        {},
        "认知状态",
        "确认周隐仍在通风井内",
    )
    normalized_possession, possession_state = prepare_state_change(
        "character",
        {},
        "持有物品",
        "馆员备份光盘",
    )

    assert normalized_role == "role_state"
    assert role_state["role_state"] == "临时证人"
    assert normalized_knowledge == "knowledge_state"
    assert knowledge_state["knowledge_state"] == "确认周隐仍在通风井内"
    assert normalized_possession == "possession_state"
    assert possession_state["possession_state"] == "馆员备份光盘"


def test_filter_supported_state_changes_normalizes_aliases_before_filtering() -> None:
    changes = [
        StateChangeCandidate(
            entity_name="林陈",
            entity_kind="character",
            field="knowledge",
            old_value="",
            new_value="知道备份光盘位置",
            reason="周隐告知",
        ),
        StateChangeCandidate(
            entity_name="林陈",
            entity_kind="character",
            field="possession",
            old_value="",
            new_value="备份光盘",
            reason="取得证物",
        ),
    ]

    filtered = _filter_supported_state_changes(changes)

    assert [change.field for change in filtered] == [
        "knowledge_state",
        "possession_state",
    ]
