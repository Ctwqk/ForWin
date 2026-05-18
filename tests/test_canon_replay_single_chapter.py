from __future__ import annotations

import json

import pytest

from forwin.canon_quality.chapter_review_form.replay import ReplayLLMUnavailable, replay_single_chapter
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.canon_quality import CountdownLedgerRow
from tests.helpers.canon_replay import FakeCountdownClient, seed_project_with_accepted_chapter
from tests.postgres import postgres_test_url


def test_replay_single_chapter_dry_run_returns_candidate_rows_without_writing() -> None:
    engine = get_engine(postgres_test_url("canon-replay-single-dry"))
    init_db(engine)
    try:
        session_factory = get_session_factory(engine)
        with session_factory() as session:
            project, _arc, _plan, _draft = seed_project_with_accepted_chapter(session, chapter_number=1)
            session.commit()

        with session_factory() as session:
            result = replay_single_chapter(
                session=session,
                project_id=project.id,
                chapter_number=1,
                llm_client=FakeCountdownClient(project.id, 1),
                persist=False,
                mode="dry_run",
            )
            session.rollback()

        with session_factory() as session:
            rows = session.query(CountdownLedgerRow).filter_by(project_id=project.id).all()

        assert result.status == "success"
        assert result.chapter_number == 1
        assert result.mode == "dry_run"
        assert result.candidate_rows["countdowns"][0]["normalized_remaining_minutes"] == 59
        assert rows == []
    finally:
        engine.dispose()


def test_replay_single_chapter_persist_writes_form_sourced_rows() -> None:
    engine = get_engine(postgres_test_url("canon-replay-single-persist"))
    init_db(engine)
    try:
        session_factory = get_session_factory(engine)
        with session_factory() as session:
            project, _arc, _plan, _draft = seed_project_with_accepted_chapter(session, chapter_number=1)
            result = replay_single_chapter(
                session=session,
                project_id=project.id,
                chapter_number=1,
                llm_client=FakeCountdownClient(project.id, 1),
                persist=True,
                mode="primary",
            )
            session.commit()

        with session_factory() as session:
            rows = session.query(CountdownLedgerRow).filter_by(project_id=project.id).all()

        assert result.status == "success"
        assert len(rows) == 1
        assert json.loads(rows[0].payload_json)["source"] == "chapter_review_form"
    finally:
        engine.dispose()


def test_replay_single_chapter_requires_llm_client_before_writes() -> None:
    engine = get_engine(postgres_test_url("canon-replay-single-no-llm"))
    init_db(engine)
    try:
        session_factory = get_session_factory(engine)
        with session_factory() as session:
            project, _arc, _plan, _draft = seed_project_with_accepted_chapter(session, chapter_number=1)
            session.commit()

        with session_factory() as session:
            with pytest.raises(ReplayLLMUnavailable):
                replay_single_chapter(
                    session=session,
                    project_id=project.id,
                    chapter_number=1,
                    llm_client=None,
                    persist=True,
                    mode="primary",
                )
            session.rollback()

        with session_factory() as session:
            assert session.query(CountdownLedgerRow).filter_by(project_id=project.id).count() == 0
    finally:
        engine.dispose()
