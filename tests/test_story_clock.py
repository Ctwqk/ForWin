from __future__ import annotations

import pytest

from forwin.book_state.lexicon import BookLexicon, TimeUnitAlias
from forwin.book_state.story_clock import (
    CalendarUnit,
    StoryCalendar,
    StoryClockError,
    UnknownTimeUnit,
    calendar_from_payload,
    calendar_to_payload,
    duration_to_minutes,
    format_story_time,
)


@pytest.fixture
def cal() -> StoryCalendar:
    return StoryCalendar("开元", 0, (CalendarUnit("日", 1440), CalendarUnit("时辰", 120)))


def test_format_story_time_decomposes_units(cal: StoryCalendar) -> None:
    assert format_story_time(cal, 3 * 1440 + 2 * 120 + 15) == "开元 3日2时辰15分钟"
    assert format_story_time(cal, 0) == "开元 0时辰"
    assert format_story_time(StoryCalendar("开元", 0, ()), 95) == "开元 95分钟"
    assert format_story_time(StoryCalendar("", 0, ()), 0) == "0分钟"
    unordered = StoryCalendar("开元", 100, tuple(reversed(cal.units)))
    assert format_story_time(unordered, 1440) == "开元 1日"
    assert format_story_time(unordered, 0) == "开元 0时辰"


def test_duration_uses_calendar_then_lexicon_alias(cal: StoryCalendar) -> None:
    lex = BookLexicon(time_unit_aliases=(TimeUnitAlias("一炷香", 30), TimeUnitAlias("时辰", 5)))
    assert duration_to_minutes(cal, lex, amount=2, unit_name="时辰") == 240
    assert duration_to_minutes(cal, lex, amount=1, unit_name="一炷香") == 30
    with pytest.raises(UnknownTimeUnit) as exc:
        duration_to_minutes(cal, lex, amount=1, unit_name="天")
    assert exc.value.unit_name == "天"


def test_duration_rounds_minutes_and_rejects_negative_amount(cal: StoryCalendar) -> None:
    lex = BookLexicon(time_unit_aliases=(TimeUnitAlias("tick", 1),))
    assert duration_to_minutes(cal, lex, amount=2.5, unit_name="tick") == 2
    assert duration_to_minutes(cal, lex, amount=3.5, unit_name="tick") == 4
    assert duration_to_minutes(cal, lex, amount=0, unit_name="tick") == 0
    with pytest.raises(StoryClockError):
        duration_to_minutes(cal, lex, amount=-1, unit_name="tick")


def test_calendar_round_trip(cal: StoryCalendar) -> None:
    payload = calendar_to_payload(cal)
    assert payload == {
        "epoch_label": "开元",
        "opening_minute": 0,
        "units": [{"name": "日", "minutes": 1440}, {"name": "时辰", "minutes": 120}],
    }
    assert calendar_from_payload(payload) == cal


@pytest.mark.parametrize(
    "change",
    [
        {"units": [{"name": "日", "minutes": 0}]},
        {"units": [{"name": "日", "minutes": -1}]},
        {"units": [{"name": "日", "minutes": 1.5}]},
        {"units": [{"name": "日", "minutes": True}]},
        {"units": [{"name": "", "minutes": 1}]},
        {"units": [{"name": "  ", "minutes": 1}]},
        {"units": [{"name": "日", "minutes": 1440}, {"name": "日", "minutes": 120}]},
        {"opening_minute": -1},
        {"opening_minute": 0.5},
        {"opening_minute": True},
    ],
)
def test_calendar_rejects_bad_units(change: dict) -> None:
    payload = {"epoch_label": "开元", "opening_minute": 0, "units": []}
    payload.update(change)
    with pytest.raises(StoryClockError):
        calendar_from_payload(payload)
