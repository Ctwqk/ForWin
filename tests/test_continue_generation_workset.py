from __future__ import annotations

import pytest

from forwin.generation.continue_workset import build_continue_generation_workset
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project


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
    planned_target_size: int = 0,
) -> ArcPlanVersion:
    arc = ArcPlanVersion(
        id=arc_id,
        project_id=project_id,
        arc_number=arc_number,
        status=status,
        chapter_start=chapter_start,
        chapter_end=chapter_end,
        planned_target_size=planned_target_size,
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


def test_workset_uses_active_arc_pending_chapters_and_max_chapters() -> None:
    engine, Session = _session_factory("continue-workset-active")
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
                chapter_end=4,
            )
            future = _arc(
                session,
                project_id=project.id,
                arc_id="arc-future",
                arc_number=2,
                status="planned",
                chapter_start=5,
                chapter_end=6,
            )
            _chapter(session, project_id=project.id, arc_id=active.id, number=1, status="accepted")
            _chapter(session, project_id=project.id, arc_id=active.id, number=2, status="planned")
            _chapter(session, project_id=project.id, arc_id=active.id, number=3, status="failed")
            _chapter(session, project_id=project.id, arc_id=active.id, number=4, status="planned")
            _chapter(session, project_id=project.id, arc_id=future.id, number=5, status="planned")

        with Session() as session:
            workset = build_continue_generation_workset(
                session,
                "project-workset",
                max_chapters=2,
                source="direct_continue",
            )

        assert workset.chapter_numbers == (2, 3)
        assert workset.requested_chapters == 2
        assert workset.materialized_plan_count == 3
        assert workset.active_arc_id == "arc-active"
        assert workset.active_arc_number == 1
        assert workset.source == "direct_continue"
        assert workset.reason == "active_arc_pending"
    finally:
        engine.dispose()


def test_workset_blocks_pending_review_before_continue() -> None:
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
            _chapter(session, project_id=project.id, arc_id=active.id, number=1, status="needs_review")
            _chapter(session, project_id=project.id, arc_id=active.id, number=2, status="planned")

        with Session() as session:
            workset = build_continue_generation_workset(session, "project-workset")

        assert workset.chapter_numbers == ()
        assert workset.requested_chapters == 0
        assert workset.reason == "pending_review_blocker"
    finally:
        engine.dispose()


def test_workset_predicts_future_arc_without_materializing() -> None:
    engine, Session = _session_factory("continue-workset-future-arc")
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
            future = _arc(
                session,
                project_id=project.id,
                arc_id="arc-future",
                arc_number=2,
                status="planned",
                chapter_start=4,
                chapter_end=6,
                planned_target_size=3,
            )
            for number in (1, 2, 3):
                _chapter(session, project_id=project.id, arc_id=active.id, number=number, status="accepted")

        with Session() as session:
            workset = build_continue_generation_workset(
                session,
                "project-workset",
                max_chapters=2,
                source="scheduler_continue",
            )
            future_plan_count = session.query(ChapterPlan).filter(ChapterPlan.arc_plan_id == future.id).count()

        assert workset.chapter_numbers == (4, 5)
        assert workset.requested_chapters == 2
        assert workset.materialized_plan_count == 0
        assert workset.active_arc_id == "arc-future"
        assert workset.active_arc_number == 2
        assert workset.source == "scheduler_continue"
        assert workset.reason == "future_arc_materialization_required"
        assert future_plan_count == 0
    finally:
        engine.dispose()


def test_workset_prefers_materialized_retry_chapter_when_no_active_arc() -> None:
    engine, Session = _session_factory("continue-workset-accepted-retry")
    try:
        with Session.begin() as session:
            project = _project(session)
            session.flush()
            arc = _arc(
                session,
                project_id=project.id,
                arc_id="arc-completed",
                arc_number=1,
                status="planned",
                chapter_start=1,
                chapter_end=12,
            )
            for number in range(1, 12):
                _chapter(session, project_id=project.id, arc_id=arc.id, number=number, status="accepted")
            _chapter(session, project_id=project.id, arc_id=arc.id, number=12, status="planned")

        with Session() as session:
            workset = build_continue_generation_workset(
                session,
                "project-workset",
                source="review_retry_continue",
            )

        assert workset.chapter_numbers == (12,)
        assert workset.requested_chapters == 1
        assert workset.materialized_plan_count == 1
        assert workset.active_arc_id == "arc-completed"
        assert workset.active_arc_number == 1
        assert workset.reason == "materialized_pending"
    finally:
        engine.dispose()


def test_workset_does_not_skip_earlier_pending_chapter_outside_active_arc() -> None:
    engine, Session = _session_factory("continue-workset-earlier-pending")
    try:
        with Session.begin() as session:
            project = _project(session)
            session.flush()
            previous = _arc(
                session,
                project_id=project.id,
                arc_id="arc-previous",
                arc_number=5,
                status="completed",
                chapter_start=25,
                chapter_end=31,
            )
            active = _arc(
                session,
                project_id=project.id,
                arc_id="arc-active",
                arc_number=6,
                status="active",
                chapter_start=32,
                chapter_end=36,
            )
            for number in range(25, 31):
                _chapter(session, project_id=project.id, arc_id=previous.id, number=number, status="accepted")
            _chapter(session, project_id=project.id, arc_id=previous.id, number=31, status="planned")
            for number in range(32, 37):
                _chapter(session, project_id=project.id, arc_id=active.id, number=number, status="planned")

        with Session() as session:
            workset = build_continue_generation_workset(
                session,
                "project-workset",
                max_chapters=3,
                source="review_retry_continue",
            )

        assert workset.chapter_numbers == (31, 32, 33)
        assert workset.requested_chapters == 3
        assert workset.materialized_plan_count == 6
        assert workset.active_arc_id == "arc-previous"
        assert workset.active_arc_number == 5
        assert workset.reason == "materialized_pending"
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
