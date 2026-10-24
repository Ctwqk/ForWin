from __future__ import annotations

import pytest

from forwin.generation.continue_workset import ContinueWorkset, build_continue_generation_workset
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from tests.postgres import postgres_test_url


def _session_factory(name: str):
    engine = get_engine(postgres_test_url(name))
    init_db(engine)
    return engine, get_session_factory(engine)


def _project(session, project_id: str = "project-workset") -> Project:
    project = Project(
        id=project_id,
        title="Workset Book",
        premise="premise",
        genre="玄幻",
        creation_status="writing",
    )
    session.add(project)
    return project


def _arc(
    session,
    *,
    project_id: str,
    arc_id: str,
    arc_number: int,
    status: str,
    chapter_start: int,
    chapter_end: int,
) -> ArcPlanVersion:
    arc = ArcPlanVersion(
        id=arc_id,
        project_id=project_id,
        arc_number=arc_number,
        status=status,
        chapter_start=chapter_start,
        chapter_end=chapter_end,
        arc_synopsis=f"arc {arc_number}",
    )
    session.add(arc)
    return arc


def _chapter(session, *, project_id: str, arc_id: str, number: int, status: str) -> None:
    session.add(
        ChapterPlan(
            id=f"plan-{arc_id}-{number}",
            project_id=project_id,
            arc_plan_id=arc_id,
            chapter_number=number,
            title=f"第{number}章",
            status=status,
        )
    )


def test_workset_counts_unwritten_chapters_without_plan_rows() -> None:
    engine, Session = _session_factory("continue-workset-unwritten")
    try:
        with Session.begin() as session:
            project = _project(session)
            project.target_total_chapters = 7
            session.flush()
            active = _arc(
                session,
                project_id=project.id,
                arc_id="arc-active",
                arc_number=1,
                status="active",
                chapter_start=1,
                chapter_end=4,
            )
            _arc(
                session,
                project_id=project.id,
                arc_id="arc-future",
                arc_number=2,
                status="planned",
                chapter_start=5,
                chapter_end=8,
            )
            _chapter(session, project_id=project.id, arc_id=active.id, number=1, status="accepted")
            _chapter(session, project_id=project.id, arc_id=active.id, number=2, status="accepted")
            _chapter(session, project_id=project.id, arc_id=active.id, number=3, status="failed")

        with Session() as session:
            workset = build_continue_generation_workset(session, "project-workset")
            capped = build_continue_generation_workset(session, "project-workset", max_chapters=2)

        # From the first chapter not accepted up to min(target_total_chapters, the last arc's end),
        # whether or not a chapter has a plan row yet.
        assert workset == ContinueWorkset(chapter_numbers=(3, 4, 5, 6, 7), reason="chapters_pending")
        assert workset.requested_chapters == 5
        assert capped == ContinueWorkset(chapter_numbers=(3, 4), reason="chapters_pending")
    finally:
        engine.dispose()


def test_workset_starts_at_the_first_unaccepted_chapter_and_ends_at_the_last_arc() -> None:
    engine, Session = _session_factory("continue-workset-first-unaccepted")
    try:
        with Session.begin() as session:
            project = _project(session)
            session.flush()
            previous = _arc(
                session,
                project_id=project.id,
                arc_id="arc-previous",
                arc_number=1,
                status="completed",
                chapter_start=1,
                chapter_end=4,
            )
            _arc(
                session,
                project_id=project.id,
                arc_id="arc-superseded",
                arc_number=2,
                status="superseded",
                chapter_start=5,
                chapter_end=20,
            )
            _arc(
                session,
                project_id=project.id,
                arc_id="arc-active",
                arc_number=2,
                status="active",
                chapter_start=5,
                chapter_end=6,
            )
            for number in (1, 3, 4):
                _chapter(session, project_id=project.id, arc_id=previous.id, number=number, status="accepted")
            _chapter(session, project_id=project.id, arc_id=previous.id, number=2, status="planned")

        with Session() as session:
            workset = build_continue_generation_workset(session, "project-workset")

        # A superseded arc does not extend the book; accepted chapters after the gap are skipped.
        assert workset.chapter_numbers == (2, 5, 6)
    finally:
        engine.dispose()


def test_workset_of_a_book_written_to_its_last_arc_is_completed() -> None:
    engine, Session = _session_factory("continue-workset-completed")
    try:
        with Session.begin() as session:
            project = _project(session)
            session.flush()
            arc = _arc(
                session,
                project_id=project.id,
                arc_id="arc-active",
                arc_number=1,
                status="active",
                chapter_start=1,
                chapter_end=2,
            )
            for number in (1, 2):
                _chapter(session, project_id=project.id, arc_id=arc.id, number=number, status="accepted")

        with Session() as session:
            workset = build_continue_generation_workset(session, "project-workset")

        assert workset == ContinueWorkset(chapter_numbers=(), reason="project_completed")
    finally:
        engine.dispose()


def test_continue_workset_filters_chapters_before_resume_point() -> None:
    engine, Session = _session_factory("continue-workset-resume")
    try:
        with Session.begin() as session:
            project = _project(session, project_id="project-workset-resume")
            session.flush()
            _arc(
                session,
                project_id=project.id,
                arc_id="arc-workset-resume",
                arc_number=1,
                status="active",
                chapter_start=1,
                chapter_end=5,
            )

        with Session() as session:
            workset = build_continue_generation_workset(
                session,
                "project-workset-resume",
                resume_from_chapter=3,
                max_chapters=2,
            )

        assert workset.chapter_numbers == (3, 4)
        assert workset.requested_chapters == 2
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("status", "reason"),
    [("needs_review", "pending_review_blocker"), ("drafted", "pending_acceptance_blocker")],
)
def test_workset_blocks_pending_review_and_acceptance_before_continue(status: str, reason: str) -> None:
    engine, Session = _session_factory("continue-workset-review-block")
    try:
        with Session.begin() as session:
            project = _project(session)
            session.flush()
            active = _arc(
                session,
                project_id=project.id,
                arc_id="arc-active",
                arc_number=1,
                status="active",
                chapter_start=1,
                chapter_end=3,
            )
            _chapter(session, project_id=project.id, arc_id=active.id, number=1, status=status)

        with Session() as session:
            workset = build_continue_generation_workset(session, "project-workset")

        assert workset == ContinueWorkset(chapter_numbers=(), reason=reason)
        assert workset.requested_chapters == 0
    finally:
        engine.dispose()


def test_workset_rejects_invalid_max_chapters() -> None:
    engine, Session = _session_factory("continue-workset-invalid-max")
    try:
        with Session.begin() as session:
            _project(session)
            session.flush()

        with Session() as session:
            with pytest.raises(ValueError, match="max_chapters must be positive"):
                build_continue_generation_workset(session, "project-workset", max_chapters=0)
    finally:
        engine.dispose()


def test_workset_with_degenerate_arc_range_writes_nothing_and_warns(caplog):
    engine, Session = _session_factory("continue-workset-rangeless")
    try:
        with Session.begin() as session:
            project = _project(session)
            project.target_total_chapters = 3
            session.flush()
            arc = _arc(session, project_id=project.id, arc_id="rangeless", arc_number=1,
                       status="active", chapter_start=1, chapter_end=0)
            _chapter(session, project_id=project.id, arc_id=arc.id, number=1, status="accepted")
            _chapter(session, project_id=project.id, arc_id=arc.id, number=2, status="planned")
            _chapter(session, project_id=project.id, arc_id=arc.id, number=4, status="planned")
        with Session() as session, caplog.at_level("WARNING", logger="forwin.generation.continue_workset"):
            result = build_continue_generation_workset(session, "project-workset")
        assert result == ContinueWorkset((), "project_completed")
        assert "empty arc chapter range" in caplog.text
    finally:
        engine.dispose()
