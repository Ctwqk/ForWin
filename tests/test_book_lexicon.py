from __future__ import annotations

import pytest
from sqlalchemy import func, select

from forwin.book_state.book_frame_repository import BookLexiconRepository, StoryCalendarRepository
from forwin.book_state.lexicon import (
    EMPTY_LEXICON,
    BookLexicon,
    Currency,
    LexiconError,
    RankLadder,
    TimeUnitAlias,
    TravelMode,
    lexicon_from_payload,
    lexicon_to_payload,
)
from forwin.book_state.story_clock import CalendarUnit, StoryCalendar
from forwin.models import BookLexiconRow, StoryCalendarRow
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.runtime.policy import RuntimePolicy
from forwin.state.updater import StateUpdater
from tests.postgres import postgres_test_url


def test_lexicon_round_trip() -> None:
    lexicon = BookLexicon(
        time_unit_aliases=(TimeUnitAlias("一炷香", 30),),
        rank_ladders=(RankLadder("rank", "阶位", ("初阶", "高阶")),),
        currencies=(Currency("coin", "铜钱"),),
        appellations=("先生",),
        generic_character_refs=("旅人",),
        travel_modes=(TravelMode("walk", "步行", 1.5, ("road", "path")),),
        news_speed_multiplier=2.0,
    )
    payload = lexicon_to_payload(lexicon)
    assert payload["travel_modes"][0]["allowed_edge_types"] == ["road", "path"]
    assert payload["rank_ladders"][0]["ranks"] == ["初阶", "高阶"]
    assert lexicon_from_payload(payload) == lexicon
    assert lexicon_from_payload({}) == EMPTY_LEXICON
    assert lexicon_from_payload(lexicon_to_payload(EMPTY_LEXICON)) == EMPTY_LEXICON


@pytest.mark.parametrize(
    "change",
    [{"allowed_edge_types": ["teleport"]}, {"speed_multiplier": 0}, {"allowed_edge_types": []}],
)
def test_lexicon_rejects_unknown_edge_type_and_zero_speed(change: dict) -> None:
    mode = {"id": "walk", "name": "步行", "speed_multiplier": 1.0, "allowed_edge_types": ["road"]}
    mode.update(change)
    with pytest.raises(LexiconError):
        lexicon_from_payload({"travel_modes": [mode]})


@pytest.mark.parametrize(
    "payload",
    [
        {"time_unit_aliases": [{"name": "tick", "minutes": 0}]},
        {"time_unit_aliases": [{"name": "tick", "minutes": -1}]},
        {"news_speed_multiplier": 0},
        {"news_speed_multiplier": -1},
        {"news_speed_multiplier": float("nan")},
        {"rank_ladders": [{"id": "r", "name": "r", "ranks": []}] * 2},
        {"currencies": [{"id": "c", "name": "c"}] * 2},
        {"travel_modes": [{"id": "m", "name": "m", "speed_multiplier": 1, "allowed_edge_types": ["road"]}] * 2},
    ],
)
def test_lexicon_rejects_invalid_values_and_duplicate_ids(payload: dict) -> None:
    with pytest.raises(LexiconError):
        lexicon_from_payload(payload)


def test_repositories_upsert_per_project() -> None:
    engine = get_engine(postgres_test_url())
    init_db(engine)
    Session = get_session_factory(engine)
    first_calendar = StoryCalendar("开元", 0, (CalendarUnit("日", 1440),))
    final_calendar = StoryCalendar("新元", 2**40, (CalendarUnit("时辰", 120),))
    first_lexicon = BookLexicon(currencies=(Currency("coin", "铜钱"),))
    final_lexicon = BookLexicon(time_unit_aliases=(TimeUnitAlias("一炷香", 30),))

    with Session.begin() as session:
        project = StateUpdater(session).create_project(
            title="Book frame test",
            premise="Book frame persistence",
            genre="test",
            setting_summary="",
            runtime_policy=RuntimePolicy.for_profile("standard"),
        )
        project_id = project.id
        calendars = StoryCalendarRepository(session)
        lexicons = BookLexiconRepository(session)
        assert calendars.get(project_id) is None
        assert lexicons.get(project_id) == EMPTY_LEXICON
        calendars.save(project_id, first_calendar)
        lexicons.save(project_id, first_lexicon)

    with Session.begin() as session:
        StoryCalendarRepository(session).save(project_id, final_calendar)
        BookLexiconRepository(session).save(project_id, final_lexicon)

    with Session() as session:
        assert StoryCalendarRepository(session).get(project_id) == final_calendar
        assert BookLexiconRepository(session).get(project_id) == final_lexicon
        assert BookLexiconRepository(session).get("missing-project") == EMPTY_LEXICON
        for row_type in (StoryCalendarRow, BookLexiconRow):
            assert session.scalar(
                select(func.count()).select_from(row_type).where(row_type.project_id == project_id)
            ) == 1
    engine.dispose()
