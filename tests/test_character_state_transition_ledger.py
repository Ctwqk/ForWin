from __future__ import annotations

from forwin.canon_quality.character_state import analyze_character_state_transitions


def test_terminal_character_active_without_bridge_is_error() -> None:
    previous = [
        {
            "character_name": "韩砚",
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
        body="韩砚推门而入，拔枪协助陆明突围。",
        previous_transitions=previous,
        central_characters={"韩砚"},
    )

    assert any(item.signal_type == "terminal_state_active_conflict" for item in signals)
    assert signals[0].severity == "error"
    assert any(item.character_name == "韩砚" and item.transition_type == "participation" for item in transitions)


def test_terminal_character_with_mistaken_death_bridge_passes() -> None:
    previous = [
        {
            "character_name": "韩砚",
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
        body="韩砚推门而入。陆明这才明白，之前的死亡证明是伪造的，处决只是伪装。",
        previous_transitions=previous,
        central_characters={"韩砚"},
    )

    assert not [item for item in signals if item.severity == "error"]
    assert any(item.transition_type == "bridge_event" for item in transitions)


def test_terminal_keywords_do_not_apply_to_every_character_globally() -> None:
    signals, transitions = analyze_character_state_transitions(
        project_id="p1",
        chapter_number=8,
        draft_id="d8",
        body=(
            "陆明刚从档案署侧门出来，周砚最后那句话仍在耳边。"
            "韩青从广场雕像的阴影里冲出来，拉住陆明逃入地下检修线。"
            "陆明发现韩青受伤，袖口下有一枚皮下追踪器。"
        ),
        previous_transitions=[],
        central_characters={"陆明", "周砚", "韩青"},
    )

    assert not signals
    assert not [
        item
        for item in transitions
        if item.character_name in {"陆明", "周砚"} and item.terminality != "none"
    ]


def test_passive_clearance_of_memory_traces_does_not_make_nearby_character_terminal() -> None:
    signals, transitions = analyze_character_state_transitions(
        project_id="p1",
        chapter_number=19,
        draft_id="d19",
        body=(
            "韩青说，如果不能找到完整真相，核心系统会启动全面覆盖，"
            "所有被标记为异常的记忆痕迹都会被清除。"
            "陆明感到后背一阵发凉，看向解码器屏幕上跳动的数据流。"
        ),
        previous_transitions=[],
        central_characters={"陆明"},
    )

    assert not signals
    assert not [
        item
        for item in transitions
        if item.character_name == "陆明" and item.terminality != "none"
    ]


def test_future_memory_clearance_threat_is_not_life_terminal() -> None:
    signals, transitions = analyze_character_state_transitions(
        project_id="p1",
        chapter_number=29,
        draft_id="d29",
        body=(
            "周砚说：陆明会在48分钟内失去所有关于家族的近期记忆。"
            "如果他继续追查，韩青的记忆会被清除。"
        ),
        previous_transitions=[],
        central_characters={"陆明", "周砚", "韩青"},
    )

    assert not signals
    assert not [
        item
        for item in transitions
        if item.character_name in {"陆明", "周砚", "韩青"} and item.terminality != "none"
    ]


def test_legacy_auto_terminal_transition_without_trigger_is_ignored() -> None:
    previous = [
        {
            "character_name": "陆明",
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
        body="陆明推门而入，冷静观察巡检员制服细节后带路突围。",
        previous_transitions=previous,
        central_characters={"陆明"},
    )

    assert not [item for item in signals if item.signal_type == "terminal_state_active_conflict"]
    assert any(item.character_name == "陆明" and item.transition_type == "participation" for item in transitions)


def test_previous_passive_clearance_transition_before_character_name_is_ignored() -> None:
    previous = [
        {
            "character_name": "陆明",
            "chapter_number": 19,
            "transition_type": "life_state",
            "to_state": "terminally_wounded",
            "terminality": "soft_terminal",
            "can_participate": False,
            "payload": {
                "draft_id": "d19",
                "analyzer_version": 2,
                "trigger_keyword": "被清除",
                "context_excerpt": "所有被标记为异常的记忆痕迹都会被清除。陆明感到后背一阵发凉。",
            },
        }
    ]

    signals, transitions = analyze_character_state_transitions(
        project_id="p1",
        chapter_number=20,
        draft_id="d20",
        body="陆明推门而入，和韩青一起行动，取走设计图。",
        previous_transitions=previous,
        central_characters={"陆明"},
    )

    assert not [item for item in signals if item.signal_type == "terminal_state_active_conflict"]
    assert any(item.character_name == "陆明" and item.transition_type == "participation" for item in transitions)


def test_previous_memory_clearance_transition_is_ignored() -> None:
    previous = [
        {
            "character_name": "周砚",
            "chapter_number": 29,
            "transition_type": "life_state",
            "to_state": "terminally_wounded",
            "terminality": "soft_terminal",
            "can_participate": False,
            "payload": {
                "draft_id": "d29",
                "analyzer_version": 2,
                "trigger_keyword": "被清除",
                "context_excerpt": "周砚说：陆明会在48分钟内失去记忆。如果他继续追查，韩青的记忆会被清除。",
            },
        }
    ]

    signals, transitions = analyze_character_state_transitions(
        project_id="p1",
        chapter_number=30,
        draft_id="d30",
        body="周砚的广播声音从头顶传来，陆明推门而入，继续行动。",
        previous_transitions=previous,
        central_characters={"陆明", "周砚"},
    )

    assert not [item for item in signals if item.signal_type == "terminal_state_active_conflict"]
    assert any(item.character_name == "周砚" and item.transition_type == "participation" for item in transitions)


def test_recent_rescue_then_unbridged_captivity_is_error() -> None:
    signals, transitions = analyze_character_state_transitions(
        project_id="p1",
        chapter_number=31,
        draft_id="d31",
        body=(
            "韩青被关在底层牢房里。"
            "她双手被束缚带固定在管道上，看到陆明时只是抬了抬下巴。"
        ),
        previous_transitions=[],
        central_characters={"陆明", "韩青"},
        recent_canon_text="第30章：陆明利用父亲留下的硬件后门救出被关押的韩青。",
        recent_canon_chapter_number=30,
    )

    assert any(item.signal_type == "custody_state_regression" for item in signals)
    regression = [item for item in signals if item.signal_type == "custody_state_regression"][0]
    assert regression.severity == "error"
    assert regression.subject_key == "character:韩青"
    assert any(item.character_name == "韩青" and item.transition_type == "custody_state" for item in transitions)


def test_recent_rescue_then_explicit_recapture_is_allowed() -> None:
    signals, transitions = analyze_character_state_transitions(
        project_id="p1",
        chapter_number=31,
        draft_id="d31",
        body=(
            "韩青再次被捕。她刚跟陆明冲出审讯室，就在走廊尽头被巡检员截住。"
            "巡检员把她重新关押进底层牢房，陆明只能先撤向核心机房。"
        ),
        previous_transitions=[],
        central_characters={"陆明", "韩青"},
        recent_canon_text="第30章：陆明利用父亲留下的硬件后门救出被关押的韩青。",
        recent_canon_chapter_number=30,
    )

    assert not [item for item in signals if item.signal_type == "custody_state_regression"]
    assert any(item.character_name == "韩青" and item.to_state == "captured" for item in transitions)


def test_unattributed_previous_terminal_transition_without_trigger_is_ignored() -> None:
    previous = [
        {
            "character_name": "陆明",
            "chapter_number": 29,
            "transition_type": "life_state",
            "to_state": "terminally_wounded",
            "terminality": "soft_terminal",
            "can_participate": False,
            "payload": {},
        },
        {
            "character_name": "周砚",
            "chapter_number": 29,
            "transition_type": "life_state",
            "to_state": "terminally_wounded",
            "terminality": "soft_terminal",
            "can_participate": False,
            "payload": {},
        },
    ]

    signals, transitions = analyze_character_state_transitions(
        project_id="p1",
        chapter_number=30,
        draft_id="d30",
        body=(
            "陆明推门而入，继续行动。"
            "周砚的声音从扩音器里传出，命令巡检员封锁旧档案库。"
        ),
        previous_transitions=previous,
        central_characters={"陆明", "周砚"},
    )

    assert not [item for item in signals if item.signal_type == "terminal_state_active_conflict"]
    assert any(item.character_name == "陆明" and item.transition_type == "participation" for item in transitions)
    assert any(item.character_name == "周砚" and item.transition_type == "participation" for item in transitions)
