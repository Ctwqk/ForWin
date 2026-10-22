from __future__ import annotations

import pytest

from forwin.canon_quality.chapter_review_form.service import review_chapter_with_form
from forwin.models import Project
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.protocol.writer import WriterOutput
from tests.helpers.book_state_seed import character_node, clock_node, commit_chapter, state_change
from tests.postgres import postgres_test_url


def _form(session, project_id: str, chapter_number: int, body: str):
    result = review_chapter_with_form(
        session=session,
        project_id=project_id,
        chapter_number=chapter_number,
        writer_output=WriterOutput(
            project_id=project_id, chapter_number=chapter_number, title="t", body=body, end_of_chapter_summary="",
        ),
        llm_client=None,
    )
    assert result.form is not None
    return result.form


def test_form_character_rows_come_from_book_state() -> None:
    engine = get_engine(postgres_test_url("review_form_book_state_characters"))
    init_db(engine)
    Session = get_session_factory(engine)
    try:
        with Session.begin() as session:
            project = Project(title="t", premise="p", genre="g")
            session.add(project)
            session.flush()
            project_id = project.id
            held, roaming, absent = (f"{kind}-{project_id}" for kind in ("held", "roaming", "absent"))
            commit_chapter(session, project_id, 0, [
                character_node(project_id, held, "甲", aliases=("阿甲",), state={
                    "alive_status": "alive",
                    "restraint": {"state": "imprisoned", "by_ref": "", "since": None},
                    "location_id": "site-1",
                    "last_seen_chapter": 0,
                }),
                character_node(project_id, roaming, "乙", state={"alive_status": "alive", "location_id": "site-2"}),
                character_node(project_id, absent, "丙", state={"alive_status": "alive"}),
            ], story_minute=1000)
            commit_chapter(session, project_id, 1, [state_change(held, "alive_status", "alive", "dead")], story_minute=1100)
            # Chapter 2's own changes are not the prior of chapter 2.
            commit_chapter(session, project_id, 2, [state_change(roaming, "location_id", "site-2", "site-3")], story_minute=1200)

        with Session() as session:
            form = _form(session, project_id, 2, "甲与乙在旧地重逢。")

        assert [
            (ask.name, ask.aliases, ask.prior_life_state, ask.prior_custody_state, ask.prior_location_id)
            for ask in form.characters
        ] == [
            ("乙", [], "alive", "free", "site-2"),
            ("甲", ["阿甲"], "dead", "captured", "site-1"),
        ]
    finally:
        engine.dispose()


def test_form_countdown_rows_come_from_clock_nodes() -> None:
    engine = get_engine(postgres_test_url("review_form_book_state_clocks"))
    init_db(engine)
    Session = get_session_factory(engine)
    try:
        with Session.begin() as session:
            project = Project(title="t", premise="p", genre="g")
            session.add(project)
            session.flush()
            project_id = project.id
            running, expired, resolved = (f"{kind}-{project_id}" for kind in ("running", "expired", "resolved"))
            commit_chapter(session, project_id, 0, [
                clock_node(project_id, running, "钟甲", due_at=1600, scope="secret"),
                clock_node(project_id, expired, "钟乙", due_at=1200, status="expired"),
                clock_node(project_id, resolved, "钟丙", due_at=1500, status="resolved"),
            ], story_minute=1000)
            commit_chapter(session, project_id, 1, [], story_minute=1300)

        with Session() as session:
            form = _form(session, project_id, 2, "钟乙早已敲过。")

        assert [
            (ask.key, ask.label, ask.prior_value_minutes, ask.prior_status, ask.last_updated_chapter)
            for ask in form.countdowns
        ] == [
            (running, "钟甲", 300, "active", 1),
            (expired, "钟乙", -100, "closed", 1),
        ]
        assert [ask.scope for ask in form.countdowns] == ["secret", "public"]
    finally:
        engine.dispose()


@pytest.mark.parametrize("remaining", [-10, 0])
def test_overdue_running_clock_is_not_protected(remaining: int) -> None:
    engine = get_engine(postgres_test_url("review_form_overdue_running_clock"))
    init_db(engine)
    Session = get_session_factory(engine)
    try:
        with Session.begin() as session:
            project = Project(title="t", premise="p", genre="g")
            session.add(project)
            session.flush()
            project_id = project.id
            clock_id = f"overdue-{project_id}"
            commit_chapter(session, project_id, 0, [
                clock_node(project_id, clock_id, "Overdue clock", due_at=1100 + remaining),
            ], story_minute=1000)
            commit_chapter(session, project_id, 1, [], story_minute=1100)

        with Session() as session:
            assert _form(session, project_id, 2, "No clocks here.").countdowns == []
            form = _form(session, project_id, 2, "Overdue clock")

        assert [(ask.key, ask.prior_value_minutes, ask.prior_status) for ask in form.countdowns] == [
            (clock_id, remaining, "closed"),
        ]
    finally:
        engine.dispose()


def test_protected_countdowns_are_capped_by_remaining() -> None:
    engine = get_engine(postgres_test_url("review_form_capped_running_clocks"))
    init_db(engine)
    Session = get_session_factory(engine)
    try:
        with Session.begin() as session:
            project = Project(title="t", premise="p", genre="g")
            session.add(project)
            session.flush()
            project_id = project.id
            remaining = [80, 10, 70, 20, 60, 30, 50, 40]
            commit_chapter(session, project_id, 0, [
                clock_node(project_id, f"clock-{index}-{project_id}", f"Clock {index}", due_at=1100 + minutes)
                for index, minutes in enumerate(remaining)
            ], story_minute=1000)
            commit_chapter(session, project_id, 1, [], story_minute=1100)

        with Session() as session:
            form = _form(session, project_id, 2, "No clocks here.")

        assert [ask.prior_value_minutes for ask in form.countdowns] == [10, 20, 30, 40, 50, 60]
        assert all(ask.prior_status == "active" for ask in form.countdowns)
    finally:
        engine.dispose()
