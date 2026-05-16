from __future__ import annotations

from forwin.canon_quality.countdown_ledger import analyze_countdowns, parse_countdown_minutes


def test_chinese_countdown_mentions_are_normalized() -> None:
    assert parse_countdown_minutes("五十九分钟") == 59
    assert parse_countdown_minutes("47小时") == 47 * 60
    assert parse_countdown_minutes("三个多小时") == 3 * 60
    assert parse_countdown_minutes("三十多天") == 30 * 24 * 60


def test_countdown_increase_without_reset_is_error() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=60,
        draft_id="d1",
        body="陆明看见倒计时还有三十多天。",
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
        body="陆明冲进地下检修线，只找到新的档案室，没有关闭系统重置危机。",
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
            "重置周期还剩最后一天。陆明把后门芯片插入读取器，核心系统核心系统后门激活中。"
            "核心系统顶端的蓝光骤然熄灭，被抹除的记忆数据一页一页滚动播放。"
            "陆明低声说：重置周期结束了，但这一次，没有人会忘记。旧城，终于自由了。"
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
            "记忆重置周期还剩不到二十四小时。陆明按下红色按钮，将陆远舟留下的记录广播给全城。"
            "从这一刻起，核心系统的记忆重置系统已经失效。"
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
            "陆明切断核心系统核心供电。父亲的信写着：重置周期将被永久终止。"
            "机房内所有蓝光熄灭，核心系统被关停。"
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
        body="核心系统终端跳出72小时倒计时。陆明继续追查，另一处屏幕又显示35天14小时。",
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
        body="城市即将进入记忆重置周期。核心系统终端跳出72小时倒计时。随后屏幕又显示35天14小时。",
        previous_entries=[],
        is_final_chapter=False,
    )

    assert any(signal.signal_type == "countdown_non_monotonic" for signal in signals)


def test_non_countdown_dates_and_offsets_are_ignored() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=1,
        draft_id="d1",
        body="抹除时间是十九时四十三分。四天前系统更新。三天后，钟塔见。",
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


def test_hypothetical_one_day_letter_phrase_is_not_countdown() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=33,
        draft_id="d33",
        body=(
            "终端屏幕显示重置倒计时：00:05:00。"
            "父亲的笔记写着：如果有一天你看到这段话，说明周砚已经控制了核心系统。"
            "陆明重新看向屏幕，重置倒计时还在继续：00:04:32。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 32,
                "normalized_remaining_minutes": 5,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert "一天" not in [entry.raw_mention for entry in ledger_entries]
    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]


def test_wrist_countdown_after_terminal_audit_stays_memory_reset() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=33,
        draft_id="d33",
        body="终端审计窗口只有八分钟了。腕表上的倒计时跳了一格：00:35:00。",
        previous_entries=[
            {
                "countdown_key": "terminal_audit_window",
                "chapter_number": 32,
                "normalized_remaining_minutes": 0,
                "status": "closed",
            },
            {
                "countdown_key": "memory_reset",
                "chapter_number": 32,
                "normalized_remaining_minutes": 38,
                "status": "consistent",
            },
        ],
        is_final_chapter=False,
    )

    assert [(entry.raw_mention, entry.countdown_key) for entry in ledger_entries] == [
        ("八分钟", "terminal_audit_window"),
        ("00:35:00", "memory_reset"),
    ]
    assert not [
        signal
        for signal in signals
        if signal.subject_key == "countdown:terminal_audit_window" and "00:35:00" in signal.description
    ]


def test_narrative_last_day_is_not_countdown_duration() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=12,
        draft_id="d12",
        body="重置前最后一天，核心系统审计窗口还剩不到四个小时，陆明赶往钟塔。",
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
            "陆明问：重置不是还有七天吗？"
            "周砚说，那是公开数据，给普通市民的心理缓冲。"
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


def test_negated_old_countdown_values_do_not_create_monotonicity_conflicts() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body=(
            "韩青说，记忆校准窗口提前了，现在是九十分钟。"
            "不是七天，不是九天，甚至不是几个小时。"
            "屏幕显示记忆重置周期剩余：00:89:47。"
            "距离重置完成还有不到八十五分钟。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 22,
                "normalized_remaining_minutes": 90,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [entry.raw_mention for entry in ledger_entries] == ["九十分钟", "00:89:47", "八十五分钟"]
    assert [entry.normalized_remaining_minutes for entry in ledger_entries] == [90, 89, 85]


def test_historical_old_countdown_and_retrospective_hours_do_not_override_current_timer() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body=(
            "韩青说，记忆重置周期的倒计时现在只剩九十分钟。"
            "不是之前说的七天，也不是三天。"
            "屏幕显示记忆重置倒计时：87分钟。"
            "陆明盯着数字说：“上周还有七天，怎么会……”"
            "系统日志显示，过去七十二小时内，核心系统处理核心的温度上升了六度。"
            "屏幕上的倒计时跳到86分钟。"
        ),
        previous_entries=[
            {
                "countdown_key": "main",
                "chapter_number": 15,
                "normalized_remaining_minutes": 9,
                "status": "consistent",
            },
            {
                "countdown_key": "memory_reset",
                "chapter_number": 22,
                "normalized_remaining_minutes": 90,
                "status": "consistent",
            },
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [entry.raw_mention for entry in ledger_entries] == ["九十分钟", "87分钟", "86分钟"]
    assert [entry.countdown_key for entry in ledger_entries] == ["memory_reset", "memory_reset", "memory_reset"]
    assert [entry.normalized_remaining_minutes for entry in ledger_entries] == [90, 87, 86]


def test_upper_bound_minute_rounding_does_not_count_as_increase() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body=(
            "记忆重置周期剩余时间：00:89:47。"
            "韩青看了一眼腕表：“距离记忆重置还有不到九十分钟。”"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 22,
                "normalized_remaining_minutes": 90,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [entry.normalized_remaining_minutes for entry in ledger_entries] == [89, 90]


def test_terminal_audit_context_does_not_hijack_later_memory_reset_ticks() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body=(
            "他动用了档案署的权限，把钟塔顶层封闭成终端审计现场。"
            "韩青已经走到床前，将一个便携终端递到他面前。"
            "她低声说，记忆重置周期剩余时间显示为00:89:37。"
            "陆明盯着那个数字，脑子飞快运转。89分12秒。88分51秒。"
        ),
        previous_entries=[
            {
                "countdown_key": "archive_cleanup",
                "chapter_number": 22,
                "normalized_remaining_minutes": 11,
                "status": "consistent",
            },
            {
                "countdown_key": "memory_reset",
                "chapter_number": 22,
                "normalized_remaining_minutes": 90,
                "status": "consistent",
            },
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [entry.countdown_key for entry in ledger_entries] == ["memory_reset", "memory_reset", "memory_reset"]
    assert [entry.normalized_remaining_minutes for entry in ledger_entries] == [89, 89, 88]


def test_static_memory_reset_window_explanation_is_not_current_countdown() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body=(
            "陆明看了看腕上的计时器。记忆重置周期剩余时间已经跳到了87分钟。"
            "韩青说，系统波动，记忆重置周期不是固定90分钟，它会根据外部压力缩短。"
            "屏幕上的倒计时继续下滑到86分钟。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 22,
                "normalized_remaining_minutes": 90,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [entry.raw_mention for entry in ledger_entries] == ["87分钟", "86分钟"]
    assert [entry.normalized_remaining_minutes for entry in ledger_entries] == [87, 86]


def test_minute_style_colon_clock_does_not_expand_to_hours_when_previous_timer_is_short() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body=(
            "手腕上的计时器显示记忆重置周期剩余：87:42:31。"
            "记忆重置周期剩余——87分钟。"
            "陆明看着她的背影消失，计时器又跳了一格：87:01:44。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 22,
                "normalized_remaining_minutes": 90,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [entry.normalized_remaining_minutes for entry in ledger_entries] == [87, 87, 87]
    assert [entry.countdown_key for entry in ledger_entries] == ["memory_reset", "memory_reset", "memory_reset"]


def test_two_part_minute_second_clock_does_not_expand_to_hours_when_previous_timer_is_short() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body="屏幕上的倒计时定格在——89:47。倒计时还在跳动，89:32。当前预估重置窗口约九十分钟。",
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 22,
                "normalized_remaining_minutes": 90,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [entry.normalized_remaining_minutes for entry in ledger_entries] == [89, 89, 90]


def test_two_part_countdown_clock_without_previous_uses_minute_second_when_context_is_explicit() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=35,
        draft_id="d35",
        body="记忆重置倒计时闪烁着猩红的数字：08:12。主角继续奔跑。倒计时：07:03。",
        previous_entries=[],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [entry.normalized_remaining_minutes for entry in ledger_entries] == [8, 7]


def test_policy_threshold_duration_is_not_current_countdown() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body=(
            "记忆重置周期剩余时间：87分钟。"
            "核心系统从未在倒计时少于六个小时时启动过校准程序。"
            "倒计时继续跳动——八十二分钟。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 22,
                "normalized_remaining_minutes": 90,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [entry.raw_mention for entry in ledger_entries] == ["87分钟", "八十二分钟"]


def test_below_ninety_minute_policy_threshold_is_not_current_countdown() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body=(
            "屏幕显示记忆重置周期剩余82分钟。"
            "陆明记得父亲笔记里的规则：每一次重置周期低于九十分钟，就意味着系统进入不可逆加速阶段。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 22,
                "normalized_remaining_minutes": 90,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [entry.raw_mention for entry in ledger_entries] == ["82分钟"]


def test_ordinal_next_day_is_not_countdown_duration() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body=(
            "屏幕显示记忆重置周期剩余82分钟。"
            "第二天就被传唤到系统巡检科的往事，只说明她三年前被校准过。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 22,
                "normalized_remaining_minutes": 90,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [entry.raw_mention for entry in ledger_entries] == ["82分钟"]


def test_frequency_and_delta_durations_do_not_override_short_countdown() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body=(
            "核心运算负载在过去四十分钟内飙升。"
            "记忆校准程序的启动频率从每六小时一次缩短到不足二十分钟一次。"
            "按现在的加速度，剩余时间不会超过九十分钟。"
            "终端震动了一下：记忆重置周期剩余时间：00:85:00。"
            "比刚才又少了五分钟。"
            "终端再次震动：00:84:00。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 22,
                "normalized_remaining_minutes": 90,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [entry.raw_mention for entry in ledger_entries] == ["九十分钟", "00:85:00", "00:84:00"]
    assert [entry.normalized_remaining_minutes for entry in ledger_entries] == [90, 85, 84]


def test_generic_countdown_after_active_memory_reset_uses_memory_key_not_stale_main_key() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body=(
            "陆明盯着腕表——记忆重置周期剩余八十六分钟。"
            + ("顾北解释核心系统真正用途，档案架深处回荡着通风管的低鸣。" * 40)
            + "倒计时继续跳动——八十二分钟。"
        ),
        previous_entries=[
            {
                "countdown_key": "main",
                "chapter_number": 15,
                "normalized_remaining_minutes": 9,
                "status": "consistent",
            },
            {
                "countdown_key": "memory_reset",
                "chapter_number": 22,
                "normalized_remaining_minutes": 90,
                "status": "consistent",
            },
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [entry.countdown_key for entry in ledger_entries] == ["memory_reset", "memory_reset"]


def test_three_plus_hours_memory_reset_backtrack_is_detected() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body="他记得很清楚，自己在钟楼顶层昏迷前看到的倒计时还是三个多小时。重置周期在加速。",
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 22,
                "normalized_remaining_minutes": 90,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert [entry.raw_mention for entry in ledger_entries] == []
    assert any(signal.signal_type == "countdown_stale_retrospective_reference" for signal in signals)


def test_retrospective_pre_unconscious_hours_conflict_with_accepted_short_timer() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body=(
            "韩青说，记忆重置周期现在不到九十分钟。"
            "陆明记得昏迷前倒计时还有将近三小时。"
            "终端显示记忆重置周期剩余：00:87:12。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 22,
                "normalized_remaining_minutes": 90,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert [entry.raw_mention for entry in ledger_entries] == ["九十分钟", "00:87:12"]
    assert any(signal.signal_type == "countdown_stale_retrospective_reference" for signal in signals)


def test_retrospective_days_conflict_with_accepted_short_timer() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body=(
            "屏幕显示记忆重置周期剩余：87分钟。"
            "韩青猛地转过头：“我们之前还有两天时间。”"
            "陆明说：“那是错误计划，真实倒计时必须按九十分钟以下处理。”"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 22,
                "normalized_remaining_minutes": 90,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert [entry.raw_mention for entry in ledger_entries] == ["87分钟"]
    assert any(signal.signal_type == "countdown_stale_retrospective_reference" for signal in signals)


def test_previous_cycle_days_question_is_stale_reference_not_current_countdown() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body=(
            "终端屏幕显示：记忆重置周期剩余89分钟。"
            "陆明低声说：“之前的周期是七天，九十分钟怎么可能？”"
            "韩青回答：“那是旧计划，真实重置窗口已经被核心系统压缩。”"
            "屏幕上的数字继续减少到87分钟。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 22,
                "normalized_remaining_minutes": 90,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert any(signal.signal_type == "countdown_stale_retrospective_reference" for signal in signals)
    assert [entry.raw_mention for entry in ledger_entries if entry.countdown_key == "memory_reset"] == [
        "89分钟",
        "87分钟",
    ]


def test_previous_calibration_notice_days_conflict_with_accepted_short_timer() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body=(
            "屏幕显示记忆重置周期剩余：87分钟。"
            "陆明低声说，上一轮校准预告写的是七天。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 22,
                "normalized_remaining_minutes": 90,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert [entry.raw_mention for entry in ledger_entries if entry.countdown_key == "memory_reset"] == ["87分钟"]
    assert any(signal.signal_type == "countdown_stale_retrospective_reference" for signal in signals)


def test_public_time_explanation_in_next_sentence_is_not_stale_reference() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body=(
            "数据板显示记忆重置周期剩余83分12秒。"
            "韩青低声说，上一次校准预告是七天。"
            "巡检员回答，那是系统对外公布的公共时间，内部加速协议已经启动。"
            "屏幕继续跳到82分钟。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 22,
                "normalized_remaining_minutes": 90,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_stale_retrospective_reference"]
    assert [entry.raw_mention for entry in ledger_entries if entry.countdown_key == "memory_reset"] == ["83分"]


def test_yesterday_old_hour_reference_conflicts_with_accepted_short_timer() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body=(
            "屏幕显示记忆重置周期剩余：89:47。"
            "韩青看到屏幕，低声说，不对，昨天还是十二小时。"
            "倒计时数字继续跳动，89:32。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 22,
                "normalized_remaining_minutes": 90,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert [entry.raw_mention for entry in ledger_entries] == ["89:47", "89:32"]
    assert [entry.normalized_remaining_minutes for entry in ledger_entries] == [89, 89]
    assert any(signal.signal_type == "countdown_stale_retrospective_reference" for signal in signals)


def test_old_belief_day_sequence_conflicts_with_accepted_short_timer() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body=(
            "屏幕显示记忆重置周期剩余：87分钟。"
            "七天、五天、三天，他一直以为还有时间。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 22,
                "normalized_remaining_minutes": 90,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert [entry.raw_mention for entry in ledger_entries if entry.countdown_key == "memory_reset"] == ["87分钟"]
    assert any(signal.signal_type == "countdown_stale_retrospective_reference" for signal in signals)


def test_public_decoy_future_time_is_not_stale_retrospective_reference() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body=(
            "屏幕显示记忆重置周期剩余：87分钟。"
            "核心系统公开数据伪称还有两天，但韩青确认那只是给市民看的假时间。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 22,
                "normalized_remaining_minutes": 90,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert [
        entry.raw_mention for entry in ledger_entries if entry.countdown_key == "memory_reset"
    ] == ["87分钟"]
    assert [entry.raw_mention for entry in ledger_entries if entry.countdown_key == "public_countdown"] == ["两天"]
    assert not [signal for signal in signals if signal.signal_type == "countdown_stale_retrospective_reference"]


def test_memory_coverage_timer_is_memory_reset_not_terminal_audit() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body=(
            "终端审计窗口突然跳了一个红色警报。"
            "曲线下方标注着一行小字：记忆覆盖进程加速，预计剩余时间——00:87:00。"
            "这不是预设的校准周期，有人在强行缩短重置窗口。"
        ),
        previous_entries=[
            {
                "countdown_key": "archive_cleanup",
                "chapter_number": 22,
                "normalized_remaining_minutes": 11,
                "status": "consistent",
            },
            {
                "countdown_key": "memory_reset",
                "chapter_number": 22,
                "normalized_remaining_minutes": 90,
                "status": "consistent",
            },
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [(entry.countdown_key, entry.raw_mention) for entry in ledger_entries] == [("memory_reset", "00:87:00")]


def test_forced_memory_calibration_timer_is_memory_reset_not_archive_cleanup() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body=(
            "顾北的终端弹出红字：核心系统提示：强制记忆校准已启动。"
            "所有区域——非授权人员将被清除。"
            "校准倒计时：83分钟。"
        ),
        previous_entries=[
            {
                "countdown_key": "archive_cleanup",
                "chapter_number": 22,
                "normalized_remaining_minutes": 11,
                "status": "consistent",
            },
            {
                "countdown_key": "memory_reset",
                "chapter_number": 22,
                "normalized_remaining_minutes": 90,
                "status": "consistent",
            },
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [(entry.countdown_key, entry.raw_mention) for entry in ledger_entries] == [("memory_reset", "83分钟")]


def test_authorized_terminal_status_panel_does_not_hijack_memory_reset_clock() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body=(
            "屏幕上是核心系统的内部监控界面，档案署的授权终端才能调取实时状态面板。"
            "界面左上角显示着红色倒计时数字：00:87:23。"
        ),
        previous_entries=[
            {
                "countdown_key": "archive_cleanup",
                "chapter_number": 22,
                "normalized_remaining_minutes": 11,
                "status": "consistent",
            },
            {
                "countdown_key": "memory_reset",
                "chapter_number": 22,
                "normalized_remaining_minutes": 90,
                "status": "consistent",
            },
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [(entry.countdown_key, entry.raw_mention) for entry in ledger_entries] == [("memory_reset", "00:87:23")]


def test_memory_reset_protocol_completion_timer_is_not_archive_cleanup() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body=(
            "控制台屏幕弹出：记忆重置协议·执行中。"
            "当前阶段：档案索引覆盖，预计完成倒计时：00:53:17。"
        ),
        previous_entries=[
            {
                "countdown_key": "archive_cleanup",
                "chapter_number": 22,
                "normalized_remaining_minutes": 11,
                "status": "consistent",
            },
            {
                "countdown_key": "memory_reset",
                "chapter_number": 22,
                "normalized_remaining_minutes": 87,
                "status": "consistent",
            },
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [(entry.countdown_key, entry.raw_mention) for entry in ledger_entries] == [("memory_reset", "00:53:17")]


def test_patrol_contact_countdown_is_local_tactical_window_not_ledger() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body=(
            "控制台警告：系统巡检部队已派遣至本区域。"
            "预计接触时间：00:04:20。"
            "记忆重置协议预计完成倒计时：00:53:17。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 22,
                "normalized_remaining_minutes": 87,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [(entry.countdown_key, entry.raw_mention) for entry in ledger_entries] == [("memory_reset", "00:53:17")]


def test_authorized_acceleration_window_is_memory_reset_not_archive_cleanup() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body=(
            "巡检员按下通讯器，请求确认是否启动记忆重置加速程序。"
            "通讯器回复：确认，加速程序授权已通过。剩余窗口：78分整。"
        ),
        previous_entries=[
            {
                "countdown_key": "archive_cleanup",
                "chapter_number": 22,
                "normalized_remaining_minutes": 11,
                "status": "consistent",
            },
            {
                "countdown_key": "memory_reset",
                "chapter_number": 22,
                "normalized_remaining_minutes": 83,
                "status": "consistent",
            },
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [(entry.countdown_key, entry.raw_mention) for entry in ledger_entries] == [("memory_reset", "78分")]


def test_previous_short_timer_reference_does_not_reopen_current_countdown() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body=(
            "屏幕显示记忆重置周期剩余：00:87:12。"
            "上一回他看到的数字还是九十分钟，而昏迷期间已经过去了三分钟。"
            "倒计时继续跳动到八十六分钟。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 22,
                "normalized_remaining_minutes": 90,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [entry.raw_mention for entry in ledger_entries] == ["00:87:12", "八十六分钟"]


def test_remembered_previous_ninety_minute_display_is_not_current_countdown() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body=(
            "终端上的倒计时还在继续，88分54秒。"
            "他记得很清楚，在钟塔顶层看到的终端显示的是九十分钟。"
            "从他昏迷到现在最多过去了五分钟，也就是说重置周期在加速。"
            "实验室屏幕显示记忆重置周期剩余：85分钟。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 22,
                "normalized_remaining_minutes": 90,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [entry.raw_mention for entry in ledger_entries] == ["88分", "85分钟"]


def test_elapsed_unconscious_duration_does_not_become_memory_reset_countdown() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body=(
            "韩青压低声音说：“凌晨一点三十一分。你昏迷了将近二十分钟。”"
            "陆明问：“记忆重置周期还剩多久？”"
            "“不到九十分钟。”"
            "她接着说，按目前曲线，最多还剩八十七分钟。"
            "终端显示记忆重置周期剩余：00:87:12。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 22,
                "normalized_remaining_minutes": 90,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [entry.raw_mention for entry in ledger_entries] == ["九十分钟", "八十七分钟", "00:87:12"]


def test_medical_observation_duration_does_not_become_memory_reset_countdown() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body=(
            "系统检测到你生命体征稳定，但建议继续观察四小时。"
            "走廊尽头的计时屏显示记忆重置周期剩余：00:89:47。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 22,
                "normalized_remaining_minutes": 90,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [entry.raw_mention for entry in ledger_entries] == ["00:89:47"]


def test_maintenance_minimum_duration_is_not_current_memory_reset_countdown() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body=(
            "计时屏显示记忆重置周期剩余：00:89:47。"
            "陆明意识到，维护周期从来不会短于三个小时，这个数字不是普通维护。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 22,
                "normalized_remaining_minutes": 90,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [entry.raw_mention for entry in ledger_entries] == ["00:89:47"]


def test_threshold_duration_is_not_stale_retrospective_reference() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body=(
            "屏幕显示记忆重置周期剩余八十七分钟。"
            "父亲留下的文件写着：如果记忆重置周期低于两小时，核心系统会启动全域覆盖扫描。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 22,
                "normalized_remaining_minutes": 90,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_stale_retrospective_reference"]
    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [entry.raw_mention for entry in ledger_entries] == ["八十七分钟"]


def test_negated_old_scale_ninety_minute_baseline_is_not_current_increase() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body=(
            "记忆重置周期：剩余87分30秒。"
            "核心系统每一次重置周期都比上一次短。这一次，不是七天，不是三天，是九十分钟。"
            "终端随后显示记忆重置周期剩余：00:82:17。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 22,
                "normalized_remaining_minutes": 90,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [entry.raw_mention for entry in ledger_entries] == ["87分", "00:82:17"]


def test_negated_cycle_extension_explanation_is_not_current_countdown() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=28,
        draft_id="d28",
        body=(
            "陆明看见手腕上的终端读数：记忆重置窗口剩余68分钟。"
            "父亲残影解释，后门会锁定当前分钟级窗口，"
            "倒计时不会归零重启成七天或三十天，也不会触发新的无约束周期。"
            "陆明继续前进，终端显示记忆重置窗口剩余65分钟。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 27,
                "normalized_remaining_minutes": 68,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [entry.raw_mention for entry in ledger_entries if entry.countdown_key == "memory_reset"] == [
        "68分钟",
        "65分钟",
    ]


def test_hypothetical_protocol_compression_is_not_current_countdown() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=28,
        draft_id="d28",
        body=(
            "父亲残影说，如果直接重置，核心系统会检测到异常时间跳跃，"
            "然后强制启动应急协议——把所有剩余时间压缩到一个小时内。"
            "终端显示记忆重置窗口剩余68分钟。"
            "陆明离开机房后，手腕上的数字跳到55分钟。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 27,
                "normalized_remaining_minutes": 68,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [entry.raw_mention for entry in ledger_entries if entry.countdown_key == "memory_reset"] == [
        "68分钟",
        "55分钟",
    ]


def test_activation_threshold_and_future_offset_are_not_current_countdown() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=28,
        draft_id="d28",
        body=(
            "系统时钟显示：记忆重置窗口剩余55分钟。"
            "程序必须在记忆校准的最后三分钟内激活，否则锚点会被覆盖。"
            "最后三分钟窗口，也就是52分钟后的某个时刻。"
            "陆明继续前进，终端显示记忆重置窗口剩余50分钟。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 27,
                "normalized_remaining_minutes": 68,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [entry.raw_mention for entry in ledger_entries if entry.countdown_key == "memory_reset"] == [
        "55分钟",
        "50分钟",
    ]


def test_local_operation_eta_is_not_memory_reset_countdown() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=28,
        draft_id="d28",
        body=(
            "记忆重置窗口剩余68分钟。"
            "屏幕上跳出一个进度条，显示：正在注入伪指令，预计剩余时间：4分32秒。"
            "倒计时在脑海中不断跳动——记忆重置窗口剩余57分钟。"
            "陆明拔出芯片后逃入走廊，记忆重置窗口剩余56分钟。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 27,
                "normalized_remaining_minutes": 68,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [entry.raw_mention for entry in ledger_entries if entry.countdown_key == "memory_reset"] == [
        "68分钟",
        "57分钟",
        "56分钟",
    ]


def test_archive_erasure_clock_is_not_memory_reset_countdown() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=29,
        draft_id="d29",
        body=(
            "记忆重置窗口剩余54分钟。"
            "周砚宣布陆氏家族全部档案记录将被永久抹除。执行时间：三分钟后。"
            "投影屏右下角跳出一个档案抹除倒计时：02:58。"
            "陆明看向屏幕，档案抹除倒计时继续跳动：02:41。"
            "记忆重置窗口剩余48分钟。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 28,
                "normalized_remaining_minutes": 56,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [(entry.countdown_key, entry.raw_mention) for entry in ledger_entries] == [
        ("memory_reset", "54分钟"),
        ("archive_cleanup", "02:58"),
        ("archive_cleanup", "02:41"),
        ("memory_reset", "48分钟"),
    ]


def test_action_deadline_minutes_do_not_extend_terminal_audit_window() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=29,
        draft_id="d29",
        body=(
            "记忆重置窗口剩余44分钟。终端审计窗口剩余9分钟。"
            "终端审计窗口关闭后，核心系统会重新锁定所有地下层通道。"
            "他必须在9分钟内离开档案署，在44分钟内完成血脉验证并进入遗忘之井。"
            "记忆重置窗口剩余42分钟。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 28,
                "normalized_remaining_minutes": 56,
                "status": "consistent",
            },
            {
                "countdown_key": "terminal_audit_window",
                "chapter_number": 28,
                "normalized_remaining_minutes": 11,
                "status": "consistent",
            },
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [(entry.countdown_key, entry.raw_mention) for entry in ledger_entries] == [
        ("memory_reset", "44分钟"),
        ("terminal_audit_window", "9分钟"),
        ("memory_reset", "42分钟"),
    ]


def test_decision_deadline_is_not_memory_reset_countdown() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=29,
        draft_id="d29",
        body="记忆重置窗口剩余53分钟。周砚说：你有五分钟考虑。记忆重置窗口剩余52分钟。",
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 28,
                "normalized_remaining_minutes": 56,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [(entry.countdown_key, entry.raw_mention) for entry in ledger_entries] == [
        ("memory_reset", "53分钟"),
        ("memory_reset", "52分钟"),
    ]


def test_access_token_validity_is_not_terminal_audit_countdown() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=29,
        draft_id="d29",
        body=(
            "终端审计窗口剩余11分钟，记忆重置窗口剩余56分钟。"
            "后门权限已激活，临时访问令牌有效时间——47分钟。"
            "记忆重置窗口剩余52分钟，终端审计窗口剩余3分钟。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 28,
                "normalized_remaining_minutes": 56,
                "status": "consistent",
            },
            {
                "countdown_key": "terminal_audit_window",
                "chapter_number": 28,
                "normalized_remaining_minutes": 11,
                "status": "consistent",
            },
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [(entry.countdown_key, entry.raw_mention) for entry in ledger_entries] == [
        ("terminal_audit_window", "11分钟"),
        ("memory_reset", "56分钟"),
        ("memory_reset", "52分钟"),
        ("terminal_audit_window", "3分钟"),
    ]


def test_wait_until_next_authorization_window_is_not_countdown() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body=(
            "核心系统核心层的授权窗口会在十一分钟后关闭。"
            "一旦关闭，下一次打开至少要等七十二小时。"
            "而记忆重置只剩八十二分钟。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 22,
                "normalized_remaining_minutes": 90,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [entry.raw_mention for entry in ledger_entries] == ["十一分钟", "八十二分钟"]
    assert [entry.countdown_key for entry in ledger_entries] == ["core_access_window", "memory_reset"]


def test_core_entry_and_memory_reset_clocks_in_same_sentence_keep_separate_keys() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body="终端上的计时器还在跳动：00:10:43——核心层入口关闭倒计时；00:81:58——记忆重置倒计时。",
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 22,
                "normalized_remaining_minutes": 90,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [entry.raw_mention for entry in ledger_entries] == ["00:10:43", "00:81:58"]
    assert [entry.countdown_key for entry in ledger_entries] == ["core_access_window", "memory_reset"]
    assert [entry.status for entry in ledger_entries] == ["consistent", "consistent"]


def test_countdown_deadline_before_phrase_does_not_resolve_timer() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body="记忆重置周期剩余八十二分钟。如果不能在倒计时结束前公开档案，它们会被锁定。",
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 22,
                "normalized_remaining_minutes": 90,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert signals == []
    assert [entry.status for entry in ledger_entries] == ["consistent"]


def test_terminal_audit_and_core_access_windows_keep_separate_keys() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body=(
            "终端审计窗口剩余时间：8分钟47秒。"
            "核心系统核心层的授权窗口会在十一分钟后关闭。"
            "一旦关闭，下一次打开至少要等七十二小时。"
            "记忆重置只剩八十二分钟。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 22,
                "normalized_remaining_minutes": 90,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [entry.raw_mention for entry in ledger_entries] == ["8分钟", "十一分钟", "八十二分钟"]
    assert [entry.countdown_key for entry in ledger_entries] == [
        "terminal_audit_window",
        "core_access_window",
        "memory_reset",
    ]


def test_terminal_audit_and_archive_cleanup_windows_keep_separate_keys() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body=(
            "终端审计窗口剩余时间：8分钟47秒。"
            "档案清理窗口倒计时：00:10:52。"
            "记忆重置只剩八十二分钟。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 22,
                "normalized_remaining_minutes": 90,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [entry.raw_mention for entry in ledger_entries] == ["8分钟", "00:10:52", "八十二分钟"]
    assert [entry.countdown_key for entry in ledger_entries] == [
        "terminal_audit_window",
        "archive_cleanup",
        "memory_reset",
    ]


def test_elapsed_system_instability_duration_is_not_countdown() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body="核心系统异常波动已经持续了三天。记忆重置只剩八十二分钟。",
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 22,
                "normalized_remaining_minutes": 90,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [entry.raw_mention for entry in ledger_entries] == ["八十二分钟"]
    assert [entry.countdown_key for entry in ledger_entries] == ["memory_reset"]


def test_countdown_summary_enumeration_keeps_nearest_prefix_key() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body="陆明得知记忆重置只剩九十分钟、终端审计窗口九分钟、档案清理倒计时十一分钟。",
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 22,
                "normalized_remaining_minutes": 90,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [entry.raw_mention for entry in ledger_entries] == ["九十分钟", "九分钟", "十一分钟"]
    assert [entry.countdown_key for entry in ledger_entries] == [
        "memory_reset",
        "terminal_audit_window",
        "archive_cleanup",
    ]


def test_clock_seconds_and_rounded_minute_restatement_are_equivalent() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body="档案清理倒计时：00:10:52。窗口关闭后，被标记的档案只有十一分钟的时间重新验证。",
        previous_entries=[],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [entry.raw_mention for entry in ledger_entries] == ["00:10:52", "十一分钟"]
    assert [entry.status for entry in ledger_entries] == ["consistent", "consistent"]


def test_prior_ninety_minute_baseline_is_not_current_countdown_after_acceleration() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body=(
            "记忆重置周期剩余：88分钟。"
            "陆明说：“上次看到还有九十——”"
            "韩青说，重置倒计时从九十分钟直接降到八十八分钟，而且还在持续下降。"
            "核心系统推送显示当前剩余时间：83分钟。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 22,
                "normalized_remaining_minutes": 90,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [entry.raw_mention for entry in ledger_entries] == ["88分钟", "83分钟"]
    assert [entry.normalized_remaining_minutes for entry in ledger_entries] == [88, 83]


def test_hypothetical_completion_duration_is_not_current_countdown() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body=(
            "记忆重置周期剩余：88分钟。"
            "如果加速继续，重置可能在不到一个小时内完成。"
            "核心系统推送显示当前剩余时间：83分钟。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 22,
                "normalized_remaining_minutes": 90,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [entry.raw_mention for entry in ledger_entries] == ["88分钟", "83分钟"]


def test_protocol_result_duration_is_not_current_countdown() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body=(
            "核心系统推送显示当前剩余时间：83分钟。"
            "紧急重置协议写明。激活后，系统将强制重启，重置周期缩短至七十二小时，以清除累积错误。"
            "副屏随后显示记忆重置周期剩余：81分钟47秒。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 22,
                "normalized_remaining_minutes": 90,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [entry.raw_mention for entry in ledger_entries] == ["83分钟", "81分钟"]


def test_rounded_ninety_minute_threshold_after_exact_short_timer_is_not_regression() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body=(
            "终端显示记忆重置周期剩余：00:87:12。"
            "核心系统刚才又做了一次记忆校准，终端显示重置周期已经缩短到九十分钟。"
            "屏幕上的倒计时继续跳动到八十六分钟。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 22,
                "normalized_remaining_minutes": 90,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [entry.raw_mention for entry in ledger_entries] == ["00:87:12", "九十分钟", "八十六分钟"]


def test_emergency_acceleration_policy_durations_are_not_current_countdown() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body=(
            "终端记录显示，记忆重置周期从昨天的七天急剧压缩到了现在的九十分钟。"
            "父亲称它为应急加速协议：一旦触发，重置周期会从标准七天逐级缩减，最终压缩到九十分钟以内。"
            "屏幕上的倒计时显示72分钟。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 22,
                "normalized_remaining_minutes": 90,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [entry.raw_mention for entry in ledger_entries] == ["72分钟"]


def test_memory_clearance_response_timer_is_distinct_from_memory_reset() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=23,
        draft_id="d23",
        body=(
            "韩青看向终端屏幕上的倒计时——72分钟，正以秒为单位跳动。"
            "警告框弹出：系统级威胁识别，建议执行记忆清除。倒计时：3分钟。"
            "陆明撤离后低头看向手表，记忆重置周期剩余：67分钟。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 22,
                "normalized_remaining_minutes": 90,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [entry.countdown_key for entry in ledger_entries] == [
        "memory_reset",
        "threat_response",
        "memory_reset",
    ]


def test_disproved_public_countdown_does_not_override_revealed_real_timer() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=12,
        draft_id="d12",
        body=(
            "屏幕显示记忆重置周期剩余时间：00天 23时 59分 48秒。"
            "陆明以为还有七天，但档案署确认的七天只是公开数据。"
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
            "这段记忆如果公开，核心系统的合法性会在二十四小时内崩塌。"
            "重置前最后一天。韩青说，你有二十四小时，决定怎么用。"
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
            "韩青说，旧城的记忆重置周期还有七天。"
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
            "核心系统终端审计窗口跳出四小时倒计时。"
            "老档案员说，城市每十年重置一次记忆。陆明问现在还剩多久？"
            "“七天。”"
        ),
        previous_entries=[],
        is_final_chapter=False,
    )

    assert signals == []
    assert [entry.countdown_key for entry in ledger_entries] == ["terminal_audit_window", "memory_reset"]


def test_memory_forging_timer_is_memory_reset_not_main_countdown() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=22,
        draft_id="d1",
        body="凌晨零点三十分，距离核心系统最后一次记忆熔铸还有不到两小时。",
        previous_entries=[
            {
                "countdown_key": "main",
                "chapter_number": 20,
                "normalized_remaining_minutes": 9,
                "status": "consistent",
            },
            {
                "countdown_key": "memory_reset",
                "chapter_number": 19,
                "normalized_remaining_minutes": 120,
                "status": "consistent",
            },
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [entry.countdown_key for entry in ledger_entries] == ["memory_reset"]


def test_memory_reset_answer_keeps_key_when_next_sentence_mentions_terminal_audit() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=2,
        draft_id="d1",
        body=(
            "老档案员说，城市每十年一次会重置记忆。陆明问这次还剩几天？"
            "“七天。”从你今天触发终端审计倒计时开始算，正好七天。"
            "七天之后，核心系统会启动一次全域记忆归零。"
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


def test_countdown_cost_deduction_is_not_remaining_countdown_value() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=26,
        draft_id="d26",
        body=(
            "手腕上的倒计时显示77分钟。"
            "救援触发条件：手动切断隔离仓电源将触发全区警报，剩余倒计时扣除至少15分钟。"
            "屏幕显示记忆重置周期剩余：76分钟。"
            "他逃出核心系统后，手腕上的倒计时显示73分钟。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 25,
                "normalized_remaining_minutes": 79,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [entry.raw_mention for entry in ledger_entries] == ["77分钟", "76分钟", "73分钟"]


def test_authorization_window_does_not_override_memory_reset_countdown() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=26,
        draft_id="d26",
        body=(
            "当前授权窗口剩余时间：11分钟。"
            "陆明迅速计算，记忆重置倒计时还剩77分钟，授权窗口只有11分钟。"
            "他逃出终端室时，记忆重置倒计时还在跳动，74分钟。"
            "离开档案署时，倒计时已缩至70分钟。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 25,
                "normalized_remaining_minutes": 79,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [(entry.countdown_key, entry.raw_mention) for entry in ledger_entries] == [
        ("archive_cleanup", "11分钟"),
        ("memory_reset", "77分钟"),
        ("archive_cleanup", "11分钟"),
        ("memory_reset", "74分钟"),
        ("memory_reset", "70分钟"),
    ]


def test_bare_countdown_after_access_log_keeps_memory_reset_key() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=26,
        draft_id="d26",
        body=(
            "屏幕提示：检测到未授权访问，系统巡检员正在赶往底层终端室。"
            "核心系统底层走廊的应急灯投下昏黄的光线。"
            "他低头看了一眼倒计时——77分钟。"
            "他抵达地下检修线第一层时，倒计时变成73分钟。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 25,
                "normalized_remaining_minutes": 79,
                "status": "consistent",
            },
            {
                "countdown_key": "archive_cleanup",
                "chapter_number": 25,
                "normalized_remaining_minutes": 11,
                "status": "consistent",
            },
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [(entry.countdown_key, entry.raw_mention) for entry in ledger_entries] == [
        ("memory_reset", "77分钟"),
        ("memory_reset", "73分钟"),
    ]


def test_detention_review_window_is_not_memory_reset_countdown() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=26,
        draft_id="d26",
        body=(
            "陆明盯着屏幕左侧的倒计时——76分23秒。"
            "日志显示，韩青被捕后关押在地下检修线第三层B区，预计审查窗口：剩余3小时。"
            "他拔出存储器，倒计时：75分11秒。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 25,
                "normalized_remaining_minutes": 77,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [(entry.countdown_key, entry.raw_mention) for entry in ledger_entries] == [
        ("memory_reset", "76分"),
        ("memory_reset", "75分"),
    ]


def test_wrist_countdown_after_audit_window_keeps_memory_reset_key() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=26,
        draft_id="d26",
        body=(
            "屏幕提示：后门访问已被锁定，剩余审计窗口：9分钟。"
            "他看了一眼手腕上的倒计时——76分钟。两个计时器同时在走。"
            "进入维修通道后，手腕上的倒计时数字变成73分钟。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 25,
                "normalized_remaining_minutes": 77,
                "status": "consistent",
            },
            {
                "countdown_key": "archive_cleanup",
                "chapter_number": 25,
                "normalized_remaining_minutes": 11,
                "status": "consistent",
            },
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [(entry.countdown_key, entry.raw_mention) for entry in ledger_entries] == [
        ("archive_cleanup", "9分钟"),
        ("memory_reset", "76分钟"),
        ("memory_reset", "73分钟"),
    ]


def test_travel_duration_after_audit_window_is_not_countdown_value() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=26,
        draft_id="d26",
        body=(
            "审计窗口只剩下不到十分钟。"
            "而他现在所处的位置，距离档案署核心机房至少需要十五分钟。"
            "手腕上的倒计时已经跳到76分钟。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 25,
                "normalized_remaining_minutes": 77,
                "status": "consistent",
            },
            {
                "countdown_key": "archive_cleanup",
                "chapter_number": 25,
                "normalized_remaining_minutes": 9,
                "status": "consistent",
            },
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [(entry.countdown_key, entry.raw_mention) for entry in ledger_entries] == [
        ("archive_cleanup", "十分钟"),
        ("memory_reset", "76分钟"),
    ]


def test_memory_fragment_unlock_cost_is_not_remaining_countdown_value() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=26,
        draft_id="d26",
        body=(
            "手腕上的倒计时数字跳动着——74分钟13秒。"
            "屏幕显示：单次解锁消耗：约15分钟记忆片段。"
            "如果用它来解锁隔离间，他会失去十五分钟记忆。"
            "倒计时还在跳动：73分51秒。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 25,
                "normalized_remaining_minutes": 76,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [(entry.countdown_key, entry.raw_mention) for entry in ledger_entries] == [
        ("memory_reset", "74分钟"),
        ("memory_reset", "73分"),
    ]


def test_local_backend_operation_window_does_not_pollute_memory_reset_countdown() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=26,
        draft_id="d26",
        body=(
            "他低头看了一眼手腕上的数字：77分钟。记忆重置剩余时间。"
            "屏幕上闪过一行字：后门已启用，剩余操作窗口：4分钟。"
            "一个新的倒计时出现了。"
            "陆明看向数据板上的倒计时：3分42秒。又看向手腕上的计时器：76分48秒。"
            "那个四分钟的局部倒计时已经归零，后门关闭。"
            "手腕上的标记显示记忆重置剩余75分钟。"
            "他的操作窗口还剩不到2分钟，必须立刻撤离。"
            "通道里只剩手腕上的倒计时微光在跳动，记忆重置剩余74分钟。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 25,
                "normalized_remaining_minutes": 77,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [
        (entry.countdown_key, entry.raw_mention)
        for entry in ledger_entries
        if entry.countdown_key == "memory_reset"
    ] == [
        ("memory_reset", "77分钟"),
        ("memory_reset", "76分"),
        ("memory_reset", "75分钟"),
        ("memory_reset", "74分钟"),
    ]


def test_rescue_window_duration_does_not_pollute_memory_reset_countdown() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=26,
        draft_id="d26",
        body=(
            "手腕上的倒计时显示记忆重置剩余79分钟。"
            "韩青的隔离舱救援窗口剩余4分47秒，刷新间隙只有十七秒。"
            "陆明再次确认手腕上的数字：记忆重置剩余78分钟。"
            "救援窗口剩余3分48秒，他必须赶到地下检修线入口。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 25,
                "normalized_remaining_minutes": 80,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [
        (entry.countdown_key, entry.raw_mention)
        for entry in ledger_entries
        if entry.countdown_key == "memory_reset"
    ] == [
        ("memory_reset", "79分钟"),
        ("memory_reset", "78分钟"),
    ]


def test_internal_audit_duration_does_not_reset_memory_countdown_context() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=26,
        draft_id="d26",
        body=(
            "倒计时在脑海中跳动——76分钟11秒。"
            "屏幕显示：任何后门操作将在3分47秒后触发核心系统内部审计程序。"
            "关联查询：预计救援窗口：倒计时71分钟。"
            "他贴着核心系统底层墙壁移动，左腕的计时器显示74:12。"
            "他抵达档案区时，手腕上的倒计时只剩七十二分钟。"
            "终端审计窗口的倒计时在余光里跳动——九分钟。"
        ),
        previous_entries=[
            {
                "countdown_key": "main",
                "chapter_number": 15,
                "normalized_remaining_minutes": 9,
                "status": "consistent",
            },
            {
                "countdown_key": "memory_reset",
                "chapter_number": 25,
                "normalized_remaining_minutes": 77,
                "status": "consistent",
            },
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [(entry.countdown_key, entry.raw_mention) for entry in ledger_entries] == [
        ("memory_reset", "76分钟"),
        ("memory_reset", "74:12"),
        ("memory_reset", "七十二分钟"),
        ("terminal_audit_window", "九分钟"),
    ]


def test_local_memory_erosion_thresholds_do_not_advance_memory_reset_ledger() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=26,
        draft_id="d26",
        body=(
            "陆明低头看着手腕上的倒计时：74分钟。"
            "如果他在第三层停留超过三十分钟，短期记忆会全部被剥离。"
            "超过四十五分钟，他连自己是谁都不会记得。"
            "老档案员说：你现在剩下的时间已经不到八十分钟。"
            "陆明看向手腕，倒计时数字跳到73分钟。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 25,
                "normalized_remaining_minutes": 77,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [(entry.countdown_key, entry.raw_mention, entry.normalized_remaining_minutes) for entry in ledger_entries] == [
        ("memory_reset", "74分钟", 74),
        ("memory_reset", "八十分钟", 74),
        ("memory_reset", "73分钟", 73),
    ]


def test_interception_eta_does_not_pollute_archive_cleanup_window() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=26,
        draft_id="d26",
        body=(
            "后门访问已被标记，当前访问已进入监控队列，预计拦截时间：剩余74分钟。"
            "他低头确认手腕上的记忆重置倒计时：73分钟。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 25,
                "normalized_remaining_minutes": 75,
                "status": "consistent",
            },
            {
                "countdown_key": "archive_cleanup",
                "chapter_number": 25,
                "normalized_remaining_minutes": 11,
                "status": "consistent",
            },
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [(entry.countdown_key, entry.raw_mention) for entry in ledger_entries] == [
        ("memory_reset", "73分钟"),
    ]


def test_elapsed_to_clause_does_not_treat_baseline_as_current_countdown() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=26,
        draft_id="d26",
        body=(
            "陆明先确认手腕上的倒计时：75分钟。"
            "七十九分钟的倒计时已经流逝到七十一分钟。"
            "记忆重置周期还剩七十分钟。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 25,
                "normalized_remaining_minutes": 77,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [(entry.countdown_key, entry.raw_mention, entry.normalized_remaining_minutes) for entry in ledger_entries] == [
        ("memory_reset", "75分钟", 75),
        ("memory_reset", "七十一分钟", 71),
        ("memory_reset", "七十分钟", 70),
    ]


def test_elapsed_delta_phrase_is_not_current_countdown_value() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=26,
        draft_id="d26",
        body=(
            "陆明低头确认腕表显示77分钟。"
            "终端提示下载完成，剩余时间75分钟。"
            "倒计时又流逝了两分钟。"
            "他冲出走廊时，腕表的数字还在跳动——74分钟。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 25,
                "normalized_remaining_minutes": 77,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [(entry.countdown_key, entry.raw_mention, entry.normalized_remaining_minutes) for entry in ledger_entries] == [
        ("memory_reset", "77分钟", 77),
        ("memory_reset", "75分钟", 75),
        ("memory_reset", "74分钟", 74),
    ]


def test_detention_transfer_window_is_not_memory_reset_countdown() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=26,
        draft_id="d26",
        body=(
            "陆明看向腕表，记忆重置倒计时还剩73分钟。"
            "巡检员低声说，韩青将在三十分钟后被转移至核心系统审讯层。"
            "陆明继续逃离，倒计时显示71分钟。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 25,
                "normalized_remaining_minutes": 77,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [(entry.countdown_key, entry.raw_mention, entry.normalized_remaining_minutes) for entry in ledger_entries] == [
        ("memory_reset", "73分钟", 73),
        ("memory_reset", "71分钟", 71),
    ]


def test_initial_countdown_recall_is_not_current_remaining_time() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=26,
        draft_id="d26",
        body=(
            "陆明确认手腕上的倒计时还剩72分钟。"
            "他记得自己启动紧急重置协议时看到的时间——七十九分钟。"
            "现在还剩多少？他不知道。"
            "档案署门口的备用终端亮起：记忆重置周期还剩70分钟。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 25,
                "normalized_remaining_minutes": 77,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [(entry.countdown_key, entry.raw_mention, entry.normalized_remaining_minutes) for entry in ledger_entries] == [
        ("memory_reset", "72分钟", 72),
        ("memory_reset", "70分钟", 70),
    ]


def test_approximate_window_and_remaining_threshold_are_not_current_countdown_values() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=26,
        draft_id="d26",
        body=(
            "陆明的手腕上显示73:21。"
            "密钥有效期与记忆重置倒计时挂钩，剩余窗口期约70分钟。"
            "记忆重置倒计时显示为七十一分钟。"
            "渡鸦将在倒计时剩余四十分钟时于档案区东南角留下下一步指示。"
            "倒计时在手腕上跳动：70:18。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 25,
                "normalized_remaining_minutes": 77,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [(entry.countdown_key, entry.raw_mention, entry.normalized_remaining_minutes) for entry in ledger_entries] == [
        ("memory_reset", "73:21", 73),
        ("memory_reset", "七十一分钟", 71),
        ("memory_reset", "70:18", 70),
    ]


def test_short_mm_ss_terminal_audit_clock_does_not_expand_to_hours() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=30,
        draft_id="d30",
        body=(
            "终端审计窗口的倒计时数字在屏幕右下角跳动：02:47。"
            "陆明继续导出证据，倒计时跳到01:12。"
            "下载完成前，屏幕显示00:03。"
            "终端审计窗口倒计时归于00:00。"
        ),
        previous_entries=[
            {
                "countdown_key": "terminal_audit_window",
                "chapter_number": 29,
                "normalized_remaining_minutes": 2,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [
        (entry.countdown_key, entry.raw_mention, entry.normalized_remaining_minutes)
        for entry in ledger_entries
    ] == [
        ("terminal_audit_window", "02:47", 2),
        ("terminal_audit_window", "01:12", 1),
        ("terminal_audit_window", "00:03", 0),
        ("terminal_audit_window", "00:00", 0),
    ]


def test_short_mm_ss_memory_reset_clock_does_not_expand_to_hours() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=30,
        draft_id="d30",
        body="陆明摸到手腕上的计时器，记忆重置窗口剩余47:12。",
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 29,
                "normalized_remaining_minutes": 52,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [(entry.countdown_key, entry.raw_mention, entry.normalized_remaining_minutes) for entry in ledger_entries] == [
        ("memory_reset", "47:12", 47),
    ]


def test_wrist_clock_with_following_terminal_audit_label_uses_terminal_key() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=30,
        draft_id="d30",
        body=(
            "陆明的手指悬在键盘上方，腕表上的倒计时数字清晰跳动：02:47。"
            "终端审计窗口剩余不到三分钟。"
            "剩余操作时间：00:02:31。"
            "终端锁定后，他冲进旧轨。记忆重置窗口剩余46分53秒。"
        ),
        previous_entries=[
            {
                "countdown_key": "terminal_audit_window",
                "chapter_number": 29,
                "normalized_remaining_minutes": 2,
                "status": "consistent",
            },
            {
                "countdown_key": "memory_reset",
                "chapter_number": 29,
                "normalized_remaining_minutes": 47,
                "status": "consistent",
            },
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [
        (entry.countdown_key, entry.raw_mention, entry.normalized_remaining_minutes)
        for entry in ledger_entries
    ] == [
        ("terminal_audit_window", "02:47", 2),
        ("terminal_audit_window", "三分钟", 3),
        ("terminal_audit_window", "00:02:31", 2),
        ("memory_reset", "46分", 46),
    ]


def test_bare_terminal_screen_short_clock_does_not_pollute_memory_reset() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=30,
        draft_id="d30",
        body=(
            "终端屏幕上跳动的倒计时数字像一把刀——02:47，02:46，02:45。"
            "这条通道只有在终端审计窗口开启时才能激活，且每次使用不超过三分钟。"
            "陆明继续导出证据，倒计时跳到02:12。"
            "记忆重置窗口在视野角落闪烁——47分12秒。"
        ),
        previous_entries=[
            {
                "countdown_key": "terminal_audit_window",
                "chapter_number": 29,
                "normalized_remaining_minutes": 3,
                "status": "consistent",
            },
            {
                "countdown_key": "memory_reset",
                "chapter_number": 29,
                "normalized_remaining_minutes": 47,
                "status": "consistent",
            },
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.subject_key == "countdown:memory_reset"]
    assert [
        (entry.countdown_key, entry.raw_mention, entry.normalized_remaining_minutes)
        for entry in ledger_entries
    ] == [
        ("terminal_audit_window", "02:47", 2),
        ("terminal_audit_window", "02:46", 2),
        ("terminal_audit_window", "02:45", 2),
        ("terminal_audit_window", "三分钟", 3),
        ("terminal_audit_window", "02:12", 2),
        ("memory_reset", "47分", 47),
    ]


def test_markdown_terminal_label_and_memory_stripping_countdown_keep_separate_keys() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=30,
        draft_id="d30",
        body=(
            "手腕上的终端计时器跳了一下：**终端审计窗口剩余2分47秒**。"
            "**终端审计窗口剩余2分11秒。**"
            "屏幕上的数字跳到1分07秒。"
            "终端审计窗口显示0分59秒。"
            "记忆剥离倒计时开始。你还有46分钟，陆明。"
            "记忆重置窗口的倒计时还在继续：44分18秒。"
        ),
        previous_entries=[
            {
                "countdown_key": "terminal_audit_window",
                "chapter_number": 29,
                "normalized_remaining_minutes": 2,
                "status": "consistent",
            },
            {
                "countdown_key": "memory_reset",
                "chapter_number": 29,
                "normalized_remaining_minutes": 47,
                "status": "consistent",
            },
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [
        (entry.countdown_key, entry.raw_mention, entry.normalized_remaining_minutes)
        for entry in ledger_entries
    ] == [
        ("terminal_audit_window", "2分", 2),
        ("terminal_audit_window", "2分", 2),
        ("terminal_audit_window", "1分", 1),
        ("terminal_audit_window", "0分", 0),
        ("memory_reset", "46分钟", 46),
        ("memory_reset", "44分", 44),
    ]


def test_gate_crack_eta_clock_is_not_memory_reset_countdown() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=30,
        draft_id="d30",
        body=(
            "陆明看了一眼腕表：记忆重置窗口剩余44分31秒。"
            "屏幕提示：正在破解门禁系统……预计时间：00:01:12。"
            "一分钟。陆明低声说，我们得等一分钟。"
            "最终记忆熔铸协议倒计时：00:43:51。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 29,
                "normalized_remaining_minutes": 47,
                "status": "consistent",
            }
        ],
        is_final_chapter=False,
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [(entry.countdown_key, entry.raw_mention, entry.normalized_remaining_minutes) for entry in ledger_entries] == [
        ("memory_reset", "44分", 44),
        ("memory_reset", "00:43:51", 43),
    ]


def test_inline_archive_cleanup_does_not_reset_memory_baseline() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=32,
        draft_id="d32",
        body=(
            "屏幕上的倒计时还在跳动——终端审计窗口剩余0分钟，"
            "记忆重置周期剩余44分钟，档案清理剩余11分钟。"
            "记忆重置周期剩余40分钟。"
        ),
        previous_entries=[
            {
                "countdown_key": "terminal_audit_window",
                "chapter_number": 31,
                "normalized_remaining_minutes": 0,
                "status": "resolved",
                "is_resolution_event": True,
            },
            {
                "countdown_key": "memory_reset",
                "chapter_number": 31,
                "normalized_remaining_minutes": 44,
                "status": "consistent",
            },
        ],
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [
        (entry.countdown_key, entry.raw_mention, entry.normalized_remaining_minutes)
        for entry in ledger_entries
    ] == [
        ("terminal_audit_window", "0分钟", 0),
        ("memory_reset", "44分钟", 44),
        ("archive_cleanup", "11分钟", 11),
        ("memory_reset", "40分钟", 40),
    ]


def test_prior_countdown_quote_does_not_create_current_backtrack() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=32,
        draft_id="d32",
        body=(
            "屏幕右下角跳出一行小字：“当前终端审计窗口已关闭，记忆重置倒计时：41分23秒。”"
            "周砚说过，记忆重置会在四十四分钟内触发。现在已经过去了不到三分钟。"
            "右手腕上的临时终端显示着记忆重置倒计时——35分22秒。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 31,
                "normalized_remaining_minutes": 44,
                "status": "consistent",
            },
            {
                "countdown_key": "terminal_audit_window",
                "chapter_number": 31,
                "normalized_remaining_minutes": 0,
                "status": "resolved",
                "is_resolution_event": True,
            },
        ],
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [
        (entry.countdown_key, entry.raw_mention, entry.normalized_remaining_minutes)
        for entry in ledger_entries
    ] == [
        ("memory_reset", "41分", 41),
        ("memory_reset", "35分", 35),
    ]


def test_patrol_interval_does_not_override_memory_countdown() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=32,
        draft_id="d32",
        body=(
            "陆明看了一眼终端——35分02秒。"
            "巡检站的值班人员刚换岗，巡逻间隔大约七分钟。"
            "终端发出轻微的震动——倒计时剩余34分47秒。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 31,
                "normalized_remaining_minutes": 44,
                "status": "consistent",
            }
        ],
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [
        (entry.countdown_key, entry.raw_mention, entry.normalized_remaining_minutes)
        for entry in ledger_entries
    ] == [
        ("memory_reset", "35分", 35),
        ("memory_reset", "34分", 34),
    ]


def test_tracker_unlock_windows_do_not_backtrack_memory_countdown() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=32,
        draft_id="d32",
        body=(
            "终端顶部显示记忆重置剩余41分22秒，档案清理剩余8分47秒。"
            "系统弹出警告框：“解除操作需管理员授权。当前窗口剩余6分12秒。”"
            "屏幕随后显示“追踪器解除窗口已打开，剩余操作时间：5分48秒”。"
            "韩青问：“我还有不到六分钟，对吗？”"
            "陆明关掉通讯，看了一眼终端顶部的倒计时——记忆重置剩余38分15秒。"
            "追踪器解除成功，目标追踪器已离线。"
            "腕表上的记忆重置倒计时还在跳动：00:28:41。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 31,
                "normalized_remaining_minutes": 44,
                "status": "consistent",
            },
            {
                "countdown_key": "archive_cleanup",
                "chapter_number": 31,
                "normalized_remaining_minutes": 11,
                "status": "consistent",
            },
        ],
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [
        (entry.countdown_key, entry.raw_mention, entry.normalized_remaining_minutes, entry.status)
        for entry in ledger_entries
    ] == [
        ("memory_reset", "41分", 41, "consistent"),
        ("archive_cleanup", "8分", 8, "consistent"),
        ("memory_reset", "38分", 38, "consistent"),
        ("memory_reset", "00:28:41", 28, "consistent"),
    ]


def test_local_search_window_does_not_backtrack_memory_countdown() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=33,
        draft_id="d33",
        body=(
            "控制台上方的倒计时器跳了一秒。00:38:41。"
            "他们的搜索范围在扩大，我们最多还有十分钟的窗口。"
            "陆明看了一眼腕上的倒计时：00:35:12。"
            "终端屏幕倒计时：00:30:12。"
            "倒计时：00:27:38。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 32,
                "normalized_remaining_minutes": 39,
                "status": "consistent",
            }
        ],
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [
        (entry.countdown_key, entry.raw_mention, entry.normalized_remaining_minutes, entry.status)
        for entry in ledger_entries
    ] == [
        ("memory_reset", "00:38:41", 38, "consistent"),
        ("memory_reset", "00:35:12", 35, "consistent"),
        ("memory_reset", "00:30:12", 30, "consistent"),
        ("memory_reset", "00:27:38", 27, "consistent"),
    ]


def test_tracker_unlock_success_does_not_resolve_main_countdown() -> None:
    _signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=32,
        draft_id="d32",
        body="追踪器解除成功，目标追踪器已离线。腕表上的记忆重置倒计时还在跳动：00:28:41。",
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 31,
                "normalized_remaining_minutes": 44,
                "status": "consistent",
            }
        ],
    )

    assert [(entry.countdown_key, entry.status) for entry in ledger_entries] == [
        ("memory_reset", "consistent")
    ]


def test_generic_remaining_time_after_explicit_memory_and_archive_clock_is_not_archive_conflict() -> None:
    signals, ledger_entries = analyze_countdowns(
        project_id="p1",
        chapter_number=32,
        draft_id="d32",
        body=(
            "腕表上的记忆重置倒计时还在跳动：00:28:41。"
            "档案清理倒计时：00:02:19。"
            "他还有不到三十分钟，而周砚的脚步声正在身后追赶。"
        ),
        previous_entries=[
            {
                "countdown_key": "memory_reset",
                "chapter_number": 31,
                "normalized_remaining_minutes": 44,
                "status": "consistent",
            },
            {
                "countdown_key": "archive_cleanup",
                "chapter_number": 31,
                "normalized_remaining_minutes": 11,
                "status": "consistent",
            },
        ],
    )

    assert not [signal for signal in signals if signal.signal_type == "countdown_non_monotonic"]
    assert [
        (entry.countdown_key, entry.raw_mention, entry.normalized_remaining_minutes)
        for entry in ledger_entries
    ] == [
        ("memory_reset", "00:28:41", 28),
        ("archive_cleanup", "00:02:19", 2),
    ]
