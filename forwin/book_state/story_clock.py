from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .lexicon import BookLexicon


# Absorb floating-point travel noise before rounding to story minutes.
MINUTE_EPSILON = 1e-6


@dataclass(frozen=True)
class CalendarUnit:
    name: str
    minutes: int


@dataclass(frozen=True)
class StoryCalendar:
    epoch_label: str
    opening_minute: int
    units: tuple[CalendarUnit, ...]


class StoryClockError(ValueError):
    pass


class UnknownTimeUnit(StoryClockError):
    def __init__(self, unit_name: str) -> None:
        self.unit_name = unit_name
        super().__init__(f"Unknown time unit: {unit_name}")


def calendar_from_payload(payload: Mapping[str, Any]) -> StoryCalendar:
    epoch_label = payload.get("epoch_label", "")
    opening_minute = payload.get("opening_minute", 0)
    if not isinstance(epoch_label, str):
        raise StoryClockError("epoch_label must be a string")
    if type(opening_minute) is not int or opening_minute < 0:
        raise StoryClockError("opening_minute must be a nonnegative integer")
    raw_units = payload.get("units", ())
    if not isinstance(raw_units, (list, tuple)):
        raise StoryClockError("units must be a list")
    units: list[CalendarUnit] = []
    names: set[str] = set()
    for item in raw_units:
        if not isinstance(item, Mapping):
            raise StoryClockError("Each unit must be an object")
        name = item.get("name")
        minutes = item.get("minutes")
        if not isinstance(name, str) or not name.strip() or name in names:
            raise StoryClockError("Unit names must be nonempty and unique")
        if type(minutes) is not int or minutes <= 0:
            raise StoryClockError("Unit minutes must be a positive integer")
        names.add(name)
        units.append(CalendarUnit(name, minutes))
    return StoryCalendar(epoch_label, opening_minute, tuple(units))


def calendar_to_payload(calendar: StoryCalendar) -> dict[str, Any]:
    return {
        "epoch_label": calendar.epoch_label,
        "opening_minute": calendar.opening_minute,
        "units": [{"name": unit.name, "minutes": unit.minutes} for unit in calendar.units],
    }


def format_story_time(calendar: StoryCalendar, minutes: int) -> str:
    if minutes < 0:
        raise StoryClockError("Story time must be nonnegative")
    units = sorted(calendar.units, key=lambda unit: unit.minutes, reverse=True)
    remainder = minutes
    parts: list[str] = []
    for unit in units:
        count, remainder = divmod(remainder, unit.minutes)
        if count:
            parts.append(f"{count}{unit.name}")
    if remainder or not units:
        parts.append(f"{remainder}分钟")
    if not parts:
        parts.append(f"0{units[-1].name}")
    return f"{calendar.epoch_label} {''.join(parts)}".strip()


def unit_minutes(calendar: StoryCalendar, lexicon: BookLexicon, unit_name: str) -> int:
    for unit in calendar.units:
        if unit.name == unit_name:
            return unit.minutes
    for alias in lexicon.time_unit_aliases:
        if alias.name == unit_name:
            return alias.minutes
    raise UnknownTimeUnit(unit_name)


def duration_to_minutes(
    calendar: StoryCalendar,
    lexicon: BookLexicon,
    *,
    amount: float,
    unit_name: str,
) -> int:
    try:
        if not math.isfinite(amount) or amount < 0:
            raise StoryClockError("Duration amount must be finite and nonnegative")
        minutes = amount * unit_minutes(calendar, lexicon, unit_name)
        if not math.isfinite(minutes) or minutes > 2**53:
            raise StoryClockError("Duration minutes exceed the safe numeric range")
    except OverflowError as exc:
        raise StoryClockError("Duration minutes exceed the safe numeric range") from exc
    return round(minutes)
