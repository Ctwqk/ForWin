from __future__ import annotations

import json

import pytest
from sqlalchemy.orm import Session

from forwin.canon_quality.chapter_review_form.replay import ReplayLLMUnavailable, replay_single_chapter
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.canon_quality import CanonQualitySignalRow
from tests.helpers.book_state_seed import clock_node, commit_chapter
from tests.helpers.canon_replay import FakeCountdownClient, seed_project_with_accepted_chapter
from tests.postgres import postgres_test_url


def _seed_countdown_prior(session: Session, project_id: str) -> str:
    clock_key = f"clock-{project_id}"
    commit_chapter(session, project_id, 0, [clock_node(project_id, clock_key, "主倒计时", due_at=60)], story_minute=0)
    return clock_key


def test_replay_single_chapter_dry_run_returns_candidate_signals_without_writing() -> None:
    engine = get_engine(postgres_test_url("canon-replay-single-dry"))
    init_db(engine)
    try:
        session_factory = get_session_factory(engine)
        with session_factory() as session:
            project, _arc, _plan, _draft = seed_project_with_accepted_chapter(session, chapter_number=1)
            clock_key = _seed_countdown_prior(session, project.id)
            session.commit()

        with session_factory() as session:
            result = replay_single_chapter(
                session=session,
                project_id=project.id,
                chapter_number=1,
                llm_client=FakeCountdownClient(project.id, 1, key=clock_key, consistent=False),
                persist=False,
                mode="dry_run",
            )
            session.rollback()

        with session_factory() as session:
            rows = session.query(CanonQualitySignalRow).filter_by(project_id=project.id).all()

        assert result.status == "success"
        assert result.chapter_number == 1
        assert result.mode == "dry_run"
        assert [(item["signal_type"], item["severity"]) for item in result.candidate_rows["signals"]] == [
            ("form_countdown_inconsistency", "warning")
        ]
        assert rows == []
    finally:
        engine.dispose()


def test_replay_single_chapter_persist_writes_form_signals() -> None:
    engine = get_engine(postgres_test_url("canon-replay-single-persist"))
    init_db(engine)
    try:
        session_factory = get_session_factory(engine)
        with session_factory() as session:
            project, _arc, _plan, _draft = seed_project_with_accepted_chapter(session, chapter_number=1)
            clock_key = _seed_countdown_prior(session, project.id)
            result = replay_single_chapter(
                session=session,
                project_id=project.id,
                chapter_number=1,
                llm_client=FakeCountdownClient(project.id, 1, key=clock_key, consistent=False),
                persist=True,
                mode="primary",
            )
            session.commit()

        with session_factory() as session:
            rows = session.query(CanonQualitySignalRow).filter_by(project_id=project.id, chapter_number=1).all()

        assert result.status == "success"
        assert [row.signal_type for row in rows] == ["form_countdown_inconsistency"]
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
            assert session.query(CanonQualitySignalRow).filter_by(project_id=project.id).count() == 0
    finally:
        engine.dispose()
