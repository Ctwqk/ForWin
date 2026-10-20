from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from forwin.protocol.book_state import MapEdgeType


@dataclass(frozen=True)
class TimeUnitAlias:
    name: str
    minutes: int


@dataclass(frozen=True)
class RankLadder:
    id: str
    name: str
    ranks: tuple[str, ...]


@dataclass(frozen=True)
class Currency:
    id: str
    name: str


@dataclass(frozen=True)
class TravelMode:
    id: str
    name: str
    speed_multiplier: float
    allowed_edge_types: tuple[str, ...]


@dataclass(frozen=True)
class BookLexicon:
    time_unit_aliases: tuple[TimeUnitAlias, ...] = ()
    rank_ladders: tuple[RankLadder, ...] = ()
    currencies: tuple[Currency, ...] = ()
    appellations: tuple[str, ...] = ()
    generic_character_refs: tuple[str, ...] = ()
    travel_modes: tuple[TravelMode, ...] = ()
    news_speed_multiplier: float = 1.0


EMPTY_LEXICON = BookLexicon()


class LexiconError(ValueError):
    pass


def _items(payload: Mapping[str, Any], key: str) -> tuple[Mapping[str, Any], ...]:
    values = payload.get(key, ())
    if not isinstance(values, (list, tuple)) or any(
        not isinstance(value, Mapping) for value in values
    ):
        raise LexiconError(f"{key} must be a list of objects")
    return tuple(values)


def _text(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise LexiconError(f"{key} must be a string")
    return value


def _strings(value: Any, key: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
        raise LexiconError(f"{key} must be a list of strings")
    return tuple(value)


def _positive_number(value: Any, key: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0:
        raise LexiconError(f"{key} must be positive")
    return float(value)


def _check_unique_ids(items: tuple[RankLadder | Currency | TravelMode, ...]) -> None:
    if len({item.id for item in items}) != len(items):
        raise LexiconError("IDs must be unique within each list")


def lexicon_from_payload(payload: Mapping[str, Any]) -> BookLexicon:
    aliases: list[TimeUnitAlias] = []
    for item in _items(payload, "time_unit_aliases"):
        minutes = item.get("minutes")
        if type(minutes) is not int or minutes <= 0:
            raise LexiconError("Alias minutes must be a positive integer")
        aliases.append(TimeUnitAlias(_text(item, "name"), minutes))
    ladders = tuple(
        RankLadder(
            _text(item, "id"), _text(item, "name"),
            _strings(item.get("ranks", ()), "ranks"),
        )
        for item in _items(payload, "rank_ladders")
    )
    currencies = tuple(
        Currency(_text(item, "id"), _text(item, "name"))
        for item in _items(payload, "currencies")
    )
    modes: list[TravelMode] = []
    edge_types = {edge_type.value for edge_type in MapEdgeType}
    for item in _items(payload, "travel_modes"):
        allowed = _strings(item.get("allowed_edge_types", ()), "allowed_edge_types")
        if not allowed or not set(allowed) <= edge_types:
            raise LexiconError("allowed_edge_types must be nonempty and contain only map edge types")
        modes.append(TravelMode(
            _text(item, "id"), _text(item, "name"),
            _positive_number(item.get("speed_multiplier"), "speed_multiplier"),
            allowed,
        ))
    travel_modes = tuple(modes)
    for items in (ladders, currencies, travel_modes):
        _check_unique_ids(items)
    return BookLexicon(
        time_unit_aliases=tuple(aliases),
        rank_ladders=ladders,
        currencies=currencies,
        appellations=_strings(payload.get("appellations", ()), "appellations"),
        generic_character_refs=_strings(payload.get("generic_character_refs", ()), "generic_character_refs"),
        travel_modes=travel_modes,
        news_speed_multiplier=_positive_number(payload.get("news_speed_multiplier", 1.0), "news_speed_multiplier"),
    )


def lexicon_to_payload(lexicon: BookLexicon) -> dict[str, Any]:
    return {
        "time_unit_aliases": [{"name": alias.name, "minutes": alias.minutes} for alias in lexicon.time_unit_aliases],
        "rank_ladders": [
            {"id": ladder.id, "name": ladder.name, "ranks": list(ladder.ranks)}
            for ladder in lexicon.rank_ladders
        ],
        "currencies": [{"id": currency.id, "name": currency.name} for currency in lexicon.currencies],
        "appellations": list(lexicon.appellations),
        "generic_character_refs": list(lexicon.generic_character_refs),
        "travel_modes": [
            {
                "id": mode.id,
                "name": mode.name,
                "speed_multiplier": mode.speed_multiplier,
                "allowed_edge_types": list(mode.allowed_edge_types),
            }
            for mode in lexicon.travel_modes
        ],
        "news_speed_multiplier": lexicon.news_speed_multiplier,
    }
