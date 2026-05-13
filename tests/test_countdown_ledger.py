from __future__ import annotations

from forwin.canon_quality.countdown_ledger import analyze_countdowns, parse_countdown_minutes


def test_chinese_countdown_mentions_are_normalized() -> None:
    assert parse_countdown_minutes("五十九分钟") == 59
    assert parse_countdown_minutes("47小时") == 47 * 60
    assert parse_countdown_minutes("三十多天") == 30 * 24 * 60


def test_countdown_increase_without_reset_is_error() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=60,
        draft_id="d1",
        body="林澈看见倒计时还有三十多天。",
        previous_entries=[
            {
                "countdown_key": "main",
                "chapter_number": 59,
                "normalized_remaining_minutes": 59,
                "status": "consistent",
            }
        ],
        is_final_chapter=True,
    )

    assert ledger_entries[0].normalized_remaining_minutes == 30 * 24 * 60
    assert any(signal.signal_type == "countdown_non_monotonic" and signal.severity == "error" for signal in signals)
    assert "不要把同一个倒计时" in signals[0].description
    assert "repair_hint" in signals[0].payload


def test_final_chapter_without_new_countdown_still_blocks_previous_open_timer() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=12,
        draft_id="d12",
        body="林澈冲进地下旧轨，只找到新的档案室，没有关闭白塔重置危机。",
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 11,
                "normalized_remaining_minutes": 180,
                "status": "consistent",
                "evidence_refs": ["chapter:11"],
            }
        ],
        is_final_chapter=True,
    )

    assert ledger_entries == []
    assert any(signal.signal_type == "final_countdown_unresolved" for signal in signals)
    assert signals[0].subject_key == "countdown:memory_reset"


def test_final_chapter_closes_memory_reset_with_no_forgetting_resolution() -> None:
    signals, _ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=12,
        draft_id="d12",
        body=(
            "重置周期还剩最后一天。林澈把后门芯片插入读取器，白塔核心系统后门激活中。"
            "白塔顶端的蓝光骤然熄灭，被抹除的记忆数据一页一页滚动播放。"
            "林澈低声说：重置周期结束了，但这一次，没有人会忘记。旧城，终于自由了。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 11,
                "normalized_remaining_minutes": 24 * 60,
                "status": "consistent",
                "evidence_refs": ["chapter:11"],
            }
        ],
        is_final_chapter=True,
    )

    assert not [signal for signal in signals if signal.signal_type == "final_countdown_unresolved"]


def test_final_chapter_closes_memory_reset_when_system_is_disabled() -> None:
    signals, _ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=12,
        draft_id="d12",
        body=(
            "记忆重置周期还剩不到二十四小时。林澈按下红色按钮，将林远舟留下的记录广播给全城。"
            "从这一刻起，白塔的记忆重置系统已经失效。"
            "旧城将不再有记忆重置，每个人都将拥有自己的历史。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 11,
                "normalized_remaining_minutes": 24 * 60,
                "status": "consistent",
                "evidence_refs": ["chapter:11"],
            }
        ],
        is_final_chapter=True,
    )

    assert not [signal for signal in signals if signal.signal_type == "final_countdown_unresolved"]


def test_final_chapter_closes_memory_reset_when_cycle_is_permanently_ended() -> None:
    signals, _ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=12,
        draft_id="d1",
        body=(
            "林澈切断白塔核心供电。父亲的信写着：重置周期将被永久终止。"
            "机房内所有蓝光熄灭，白塔系统被关停。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 11,
                "normalized_remaining_minutes": 24 * 60,
                "status": "consistent",
            }
        ],
        is_final_chapter=True,
    )

    assert not [signal for signal in signals if signal.signal_type == "final_countdown_unresolved"]


def test_countdown_reset_allows_increase() -> None:
    signals, _ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=60,
        draft_id="d1",
        body="系统完成重置，新的倒计时还有三十多天。",
        previous_entries=[
            {
                "countdown_key": "main",
                "chapter_number": 59,
                "normalized_remaining_minutes": 59,
                "status": "consistent",
            }
        ],
        is_final_chapter=True,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]


def test_countdown_increase_inside_same_chapter_is_error() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=1,
        draft_id="d1",
        body="白塔终端跳出72小时倒计时。林澈继续追查，另一处屏幕又显示35天14小时。",
        previous_entries=[],
        is_final_chapter=False,
    )

    assert [entry.normalized_remaining_minutes for entry in ledger_entries] == [72 * 60, (35 * 24 + 14) * 60]
    assert any(signal.signal_type == "countdown_non_monotonic" and signal.severity == "error" for signal in signals)


def test_memory_reset_word_does_not_allow_countdown_increase() -> None:
    signals, _ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=1,
        draft_id="d1",
        body="城市即将进入记忆重置周期。白塔终端跳出72小时倒计时。随后屏幕又显示35天14小时。",
        previous_entries=[],
        is_final_chapter=False,
    )

    assert any(signal.signal_type == "countdown_non_monotonic" for signal in signals)


def test_non_countdown_dates_and_offsets_are_ignored() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=1,
        draft_id="d1",
        body="抹除时间是十九时四十三分。四天前系统更新。三天后，潮汐钟楼见。",
        previous_entries=[],
        is_final_chapter=False,
    )

    assert signals == []
    assert ledger_entries == []


def test_time_of_day_and_retrospective_days_are_not_countdown_mentions() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=12,
        draft_id="d12",
        body=(
            "重置窗口会在今晚十一点十五分到十一点四十五分之间开启。"
            "重置后，你查到的这七天里发生过什么都会被清掉。"
            "倒计时还剩十五分钟。"
        ),
        previous_entries=[],
        is_final_chapter=False,
    )

    assert signals == []
    assert [entry.raw_mention for entry in ledger_entries] == ["十五分钟"]


def test_narrative_last_day_is_not_countdown_duration() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=12,
        draft_id="d12",
        body="重置前最后一天，白塔系统审计窗口还剩不到四个小时，林澈赶往潮汐钟楼。",
        previous_entries=[
            {
                "countdown_key": "main",
                "chapter_number": 11,
                "normalized_remaining_minutes": 240,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [entry.raw_mention for entry in ledger_entries] == ["四个小时"]


def test_public_decoy_window_does_not_conflict_with_real_countdown() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=12,
        draft_id="d12",
        body=(
            "林澈问：重置不是还有七天吗？"
            "洛庭若说，那是公开数据，给普通市民的心理缓冲。"
            "真正的核心调度窗口只有二十四小时。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 11,
                "normalized_remaining_minutes": 3 * 24 * 60,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert {entry.countdown_key for entry in ledger_entries} == {"public_countdown", "memory_reset"}
    assert [entry.normalized_remaining_minutes for entry in ledger_entries if entry.countdown_key == "memory_reset"] == [24 * 60]


def test_clock_countdown_mentions_are_normalized() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=1,
        draft_id="d1",
        body="倒计时显示72:00:00。倒计时还在跳动，71:12:08。",
        previous_entries=[],
        is_final_chapter=False,
    )

    assert signals == []
    assert [entry.normalized_remaining_minutes for entry in ledger_entries] == [72 * 60, 71 * 60 + 12]


def test_compound_day_hour_minute_countdowns_are_single_mentions() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=1,
        draft_id="d1",
        body=(
            "倒计时显示00:47:12:38:29。"
            "倒计时还在走。四十七天。十二小时。三十五分钟。四秒。"
        ),
        previous_entries=[],
        is_final_chapter=False,
    )

    assert signals == []
    assert [entry.normalized_remaining_minutes for entry in ledger_entries] == [
        47 * 24 * 60 + 12 * 60 + 38,
        47 * 24 * 60 + 12 * 60 + 35,
    ]


def test_chinese_unit_day_hour_minute_second_countdown_is_single_mention() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=12,
        draft_id="d12",
        body=(
            "屏幕自动亮起，上面显示着一串数字："
            "记忆重置周期剩余时间：00天 23时 59分 48秒。"
            "数字还在不断跳动，每一秒都在减少。"
            "距离重置周期结束，还有不到二十三小时。"
        ),
        previous_entries=[],
        is_final_chapter=True,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [entry.raw_mention for entry in ledger_entries] == ["00天 23时 59分 48秒", "二十三小时"]
    assert [entry.normalized_remaining_minutes for entry in ledger_entries] == [23 * 60 + 59, 23 * 60]


def test_disproved_public_countdown_does_not_override_revealed_real_timer() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=12,
        draft_id="d12",
        body=(
            "屏幕显示记忆重置周期剩余时间：00天 23时 59分 48秒。"
            "林澈以为还有七天，但档案公会确认的七天只是公开数据。"
            "真正的核心调度窗口只有不到二十四小时。"
            "距离重置周期结束，还有不到二十三小时。"
        ),
        previous_entries=[],
        is_final_chapter=True,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [entry.countdown_key for entry in ledger_entries] == [
        "memory_reset",
        "public_countdown",
        "public_countdown",
        "memory_reset",
        "memory_reset",
    ]
    assert [entry.normalized_remaining_minutes for entry in ledger_entries if entry.countdown_key == "memory_reset"] == [
        23 * 60 + 59,
        24 * 60,
        23 * 60,
    ]


def test_effect_window_is_not_countdown_but_final_day_timer_is_memory_reset() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=12,
        draft_id="d12",
        body=(
            "这段记忆如果公开，白塔的合法性会在二十四小时内崩塌。"
            "重置前最后一天。沈宴秋说，你有二十四小时，决定怎么用。"
        ),
        previous_entries=[
            {
                "countdown_key": "archive_cleanup",
                "chapter_number": 11,
                "normalized_remaining_minutes": 180,
                "status": "consistent",
            }
        ],
        is_final_chapter=True,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [entry.raw_mention for entry in ledger_entries] == ["二十四小时"]
    assert [entry.countdown_key for entry in ledger_entries] == ["memory_reset"]


def test_distinct_countdown_contexts_do_not_conflict() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=1,
        draft_id="d1",
        body=(
            "档案记录群组开始清除，倒计时1分28秒。倒计时归零。"
            "老档案员说记忆重置周期还有七天。"
        ),
        previous_entries=[],
        is_final_chapter=False,
    )

    assert signals == []
    assert {entry.countdown_key for entry in ledger_entries} == {"archive_cleanup", "memory_reset"}


def test_access_authorization_timer_and_memory_reset_timer_are_distinct() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=1,
        draft_id="d1",
        body=(
            "该查询涉及限制级信息，已自动记录至审计日志。如需继续，请输入授权码。"
            "对话框下方还有倒计时图标，数字跳动：03:59:47。"
            "沈宴秋说，旧城的记忆重置周期还有七天。"
        ),
        previous_entries=[],
        is_final_chapter=False,
    )

    assert signals == []
    assert [entry.countdown_key for entry in ledger_entries] == ["archive_cleanup", "memory_reset"]


def test_terminal_audit_timer_and_memory_reset_answer_are_distinct() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=2,
        draft_id="d1",
        body=(
            "白塔终端审计窗口跳出四小时倒计时。"
            "老档案员说，城市每十年重置一次记忆。林澈问现在还剩多久？"
            "“七天。”"
        ),
        previous_entries=[],
        is_final_chapter=False,
    )

    assert signals == []
    assert [entry.countdown_key for entry in ledger_entries] == ["archive_cleanup", "memory_reset"]


def test_memory_reset_answer_keeps_key_when_next_sentence_mentions_terminal_audit() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=2,
        draft_id="d1",
        body=(
            "老档案员说，城市每十年一次会重置记忆。林澈问这次还剩几天？"
            "“七天。”从你今天触发终端审计倒计时开始算，正好七天。"
            "七天之后，白塔会启动一次全域记忆归零。"
        ),
        previous_entries=[
            {
                "countdown_key": "main",
                "chapter_number": 1,
                "normalized_remaining_minutes": 240,
                "status": "consistent",
            },
            {
                "countdown_key": "archive_cleanup",
                "chapter_number": 1,
                "normalized_remaining_minutes": 240,
                "status": "consistent",
            },
        ],
        is_final_chapter=False,
    )

    assert signals == []
    assert [entry.raw_mention for entry in ledger_entries] == ["七天", "七天"]
    assert [entry.countdown_key for entry in ledger_entries] == ["memory_reset", "memory_reset"]


def test_rounded_hour_after_exact_clock_is_not_countdown_increase() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=1,
        draft_id="d1",
        body="终端审计窗口倒计时：03:59:42。四小时，他必须先查出操作人。",
        previous_entries=[],
        is_final_chapter=False,
    )

    assert signals == []
    assert [entry.normalized_remaining_minutes for entry in ledger_entries] == [239, 240]
