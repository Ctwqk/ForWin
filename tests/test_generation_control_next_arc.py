from __future__ import annotations

from forwin.application.read_models import (
    build_generation_control,
    build_project_detail,
    build_project_summaries,
    load_next_planned_arc_starts,
)
from forwin.application.task_center import TaskCenterService
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from forwin.runtime.policy import RuntimePolicy
from forwin.runtime.policy_store import ProjectPolicyStore
from tests.postgres import postgres_test_url


def _plan(chapter_number: int, status: str) -> ChapterPlan:
    return ChapterPlan(
        id=f"plan-{chapter_number}",
        project_id="project-next-arc",
        arc_plan_id="arc-1",
        chapter_number=chapter_number,
        title=f"第{chapter_number}章",
        one_line="",
        goals_json="[]",
        status=status,
    )


def test_fully_accepted_book_with_next_arc_is_resumable_from_arc_start() -> None:
    control = build_generation_control(
        plans=[_plan(1, "accepted"), _plan(2, "accepted")],
        latest_replan=None,
        next_arc_chapter_start=3,
    )

    assert control.next_chapter == 3
    assert control.can_resume is True
    assert control.plan_state == "in_progress"
    assert control.next_gate == "next_arc_ready"
    assert control.planned_chapters == []


def test_fully_accepted_book_without_next_arc_stays_completed() -> None:
    control = build_generation_control(
        plans=[_plan(1, "accepted"), _plan(2, "accepted")],
        latest_replan=None,
    )

    assert control.next_chapter == 0
    assert control.can_resume is False
    assert control.plan_state == "completed"


def test_pending_plans_take_precedence_over_next_arc_start() -> None:
    control = build_generation_control(
        plans=[_plan(1, "accepted"), _plan(2, "planned")],
        latest_replan=None,
        next_arc_chapter_start=9,
    )

    assert control.next_chapter == 2
    assert control.can_resume is True
    assert control.plan_state == "in_progress"


def test_review_and_acceptance_blockers_still_block_next_arc_resume() -> None:
    needs_review = build_generation_control(
        plans=[_plan(1, "accepted"), _plan(2, "needs_review")],
        latest_replan=None,
        next_arc_chapter_start=3,
    )
    drafted = build_generation_control(
        plans=[_plan(1, "accepted"), _plan(2, "drafted")],
        latest_replan=None,
        next_arc_chapter_start=3,
    )

    assert needs_review.can_resume is False
    assert needs_review.plan_state == "needs_review"
    assert drafted.can_resume is False
    assert drafted.plan_state == "pending_acceptance"


def _task_center_service(session_factory=None) -> TaskCenterService:
    return TaskCenterService(
        get_session=session_factory,
        display_datetime=str,
        new_stage_history_entry=lambda *_args, **_kwargs: {},
        generation_task_from_row=lambda _row: {},
        config_provider=lambda: None,
        terminal_statuses=set(),
        terminal_stage_by_status={},
    )


def _project_item(plans: list[ChapterPlan], *, next_arc_chapter_start: int):
    project = Project(id="project-next-arc", title="Item", premise="前提", genre="玄幻")
    return _task_center_service()._build_project_task_center_item(
        project,
        plans,
        latest_band_checkpoint=None,
        decision_events=[],
        next_arc_chapter_start=next_arc_chapter_start,
    )


def test_task_center_drafted_chapter_blocks_next_arc_resume() -> None:
    item = _project_item(
        [_plan(1, "accepted"), _plan(2, "drafted")], next_arc_chapter_start=3
    )

    assert item.generation_control.can_resume is False
    assert item.resumable is False
    assert item.generation_control.plan_state == "pending_acceptance"


def test_task_center_pending_review_blocks_next_arc_resume() -> None:
    item = _project_item(
        [_plan(1, "accepted"), _plan(2, "needs_review")], next_arc_chapter_start=3
    )

    assert item.generation_control.can_resume is False
    assert item.resumable is False


def test_task_center_accepted_book_with_next_arc_is_resumable() -> None:
    item = _project_item(
        [_plan(1, "accepted"), _plan(2, "accepted")], next_arc_chapter_start=3
    )

    assert item.generation_control.can_resume is True
    assert item.resumable is True
    assert item.generation_control.next_chapter == 3


def test_task_center_writing_project_without_plans_resumes_from_next_arc() -> None:
    item = _project_item([], next_arc_chapter_start=1)

    assert item.requested_chapters == 0
    assert item.generation_control.plan_state == "none"
    assert item.generation_control.next_chapter == 1
    assert item.generation_control.can_resume is True
    assert item.resumable is True


def test_task_center_book_without_plans_or_next_arc_is_not_resumable() -> None:
    item = _project_item([], next_arc_chapter_start=0)

    assert item.generation_control.can_resume is False
    assert item.resumable is False


def _seed_genesis_book_between_arcs(session, *, project_id: str, status: str) -> Project:
    project = Project(
        id=project_id,
        title="Between Arcs",
        premise="前提",
        genre="玄幻",
        creation_status=status,
        target_total_chapters=20,
    )
    ProjectPolicyStore(session).initialize(
        project, RuntimePolicy.for_profile("standard")
    )
    session.add(project)
    session.flush()
    session.add(
        ArcPlanVersion(
            id=f"{project_id}-arc-1",
            project_id=project_id,
            arc_number=1,
            chapter_start=1,
            chapter_end=10,
            arc_synopsis="第一卷",
            status="active",
        )
    )
    for number in range(1, 11):
        session.add(
            ChapterPlan(
                id=f"{project_id}-plan-{number}",
                project_id=project_id,
                arc_plan_id=f"{project_id}-arc-1",
                chapter_number=number,
                title=f"第{number}章",
                status="accepted",
            )
        )
    # Genesis future arcs are skeleton rows without chapter plans; the lowest
    # arc_number is the next one to activate.
    session.add(
        ArcPlanVersion(
            id=f"{project_id}-arc-3",
            project_id=project_id,
            arc_number=3,
            chapter_start=21,
            chapter_end=30,
            arc_synopsis="第三卷",
            status="planned",
        )
    )
    session.add(
        ArcPlanVersion(
            id=f"{project_id}-arc-2",
            project_id=project_id,
            arc_number=2,
            chapter_start=11,
            chapter_end=20,
            arc_synopsis="第二卷",
            status="planned",
        )
    )
    session.flush()
    return project


def test_genesis_book_between_arcs_is_resumable_in_every_read_model() -> None:
    engine = get_engine(postgres_test_url("generation_control_next_arc"))
    init_db(engine)
    Session = get_session_factory(engine)
    try:
        with Session() as session:
            project = _seed_genesis_book_between_arcs(
                session, project_id="proj-between-arcs", status="writing"
            )
            session.commit()

            assert load_next_planned_arc_starts(session, [project.id]) == {
                project.id: 11
            }

            detail = build_project_detail(
                session=session, project=project, display_datetime=str
            )
            summary = build_project_summaries(
                session=session, projects=[project], display_datetime=str
            )[0]
            task_service = _task_center_service(Session)
            task_item = task_service.get_project_backed_task_item_or_404(
                task_service.project_task_id(project.id)
            )

        for control in (
            detail.generation_control,
            summary.generation_control,
            task_item.generation_control,
        ):
            assert control.next_chapter == 11
            assert control.can_resume is True
            assert control.plan_state == "in_progress"
            assert control.planned_chapters == []
    finally:
        engine.dispose()


def test_next_arc_start_is_ignored_for_projects_that_are_not_writing() -> None:
    engine = get_engine(postgres_test_url("generation_control_next_arc_genesis"))
    init_db(engine)
    Session = get_session_factory(engine)
    try:
        with Session() as session:
            project = _seed_genesis_book_between_arcs(
                session, project_id="proj-genesis-stage", status="genesis_ready"
            )
            session.commit()

            assert load_next_planned_arc_starts(session, [project.id]) == {}
            detail = build_project_detail(
                session=session, project=project, display_datetime=str
            )

        assert detail.generation_control.next_chapter == 0
        assert detail.generation_control.can_resume is False
    finally:
        engine.dispose()
