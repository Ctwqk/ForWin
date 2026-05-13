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
