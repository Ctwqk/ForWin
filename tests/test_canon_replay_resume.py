from __future__ import annotations

import json
from pathlib import Path

import pytest

from forwin.canon_quality.chapter_review_form.replay import replay_chapter_range
from forwin.canon_quality.chapter_review_form.replay_state import (
    ReplayRangeOptions,
    ReplayState,
    state_file_path,
    write_state_atomic,
)
from forwin.models.base import get_engine, get_session_factory, init_db
from tests.helpers.canon_replay import (
    FakeCountdownClient,
    seed_accepted_chapter,
    seed_project_with_accepted_chapter,
)
from tests.postgres import postgres_test_url


def test_state_file_path_is_range_scoped(tmp_path: Path) -> None:
    path = state_file_path(root=tmp_path, project_id="p1", from_chapter=1, to_chapter=3)

    assert path == tmp_path / "canon_replay" / "p1" / "1-3.state.json"


def test_write_state_atomic_creates_valid_json(tmp_path: Path) -> None:
    path = state_file_path(root=tmp_path, project_id="p1", from_chapter=1, to_chapter=2)
    state = ReplayState(project_id="p1", from_chapter=1, to_chapter=2)

    write_state_atomic(path, state)

    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == "canon_replay.v1"
    assert not path.with_suffix(path.suffix + ".tmp").exists()


def test_range_replay_resumes_after_completed_chapter(tmp_path: Path) -> None:
    engine = get_engine(postgres_test_url("canon-replay-range-resume"))
    init_db(engine)
    try:
        session_factory = get_session_factory(engine)
        with session_factory() as session:
            project, arc, _plan, _draft = seed_project_with_accepted_chapter(session, chapter_number=1)
            seed_accepted_chapter(session, project=project, arc=arc, chapter_number=2)
            session.commit()

        state_path = state_file_path(root=tmp_path, project_id=project.id, from_chapter=1, to_chapter=2)
        write_state_atomic(
            state_path,
            ReplayState(project_id=project.id, from_chapter=1, to_chapter=2).mark_completed(
                1,
                {"status": "success"},
            ),
        )

        results = replay_chapter_range(
            session_factory=session_factory,
            project_id=project.id,
            from_chapter=1,
            to_chapter=2,
            llm_client_factory=lambda chapter: FakeCountdownClient(project.id, chapter),
            state_root=tmp_path,
            options=ReplayRangeOptions(
                persist=False,
                mode="dry_run",
                resume=True,
                force_restart=False,
                force_rerun=False,
                abort_on_error=True,
            ),
        )

        assert [result.chapter_number for result in results] == [2]
        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert state["chapters"]["1"]["status"] == "completed"
        assert state["chapters"]["2"]["status"] == "completed"
    finally:
        engine.dispose()


def test_range_replay_refuses_existing_state_without_resume_or_force_restart(tmp_path: Path) -> None:
    path = state_file_path(root=tmp_path, project_id="p1", from_chapter=1, to_chapter=2)
    write_state_atomic(path, ReplayState(project_id="p1", from_chapter=1, to_chapter=2))

    with pytest.raises(RuntimeError, match="state file already exists"):
        ReplayState.prepare_existing_state(
            path=path,
            project_id="p1",
            from_chapter=1,
            to_chapter=2,
            resume=False,
            force_restart=False,
        )
