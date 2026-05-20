from __future__ import annotations

import json
from dataclasses import dataclass

import forwin.api_core.generation as generation_api
from forwin.config import Config as RuntimeConfig
from forwin.generation.auto_continue import (
    AutoContinueDecision,
    GenerationAutoContinueController,
)
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.governance import DecisionEvent
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from tests.postgres import postgres_test_url


@dataclass
class ResultStub:
    project_id: str
    status: str = "completed"
    completed_chapters: list[int] | None = None
    failed_chapters: list[int] | None = None
    paused_chapters: list[int] | None = None
    paused: bool = False
    cancelled: bool = False


def test_completion_handler_schedules_next_task_after_success(monkeypatch) -> None:
    observed: list[dict[str, object]] = []
    runtime_config = RuntimeConfig()

    class DummySession:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def commit(self) -> None:
            observed.append({"commit": True})

    class ControllerStub:
        def __init__(self, *, session_factory, create_continue_generation_task):
            observed.append(
                {
                    "session_factory": session_factory,
                    "create_continue_generation_task": create_continue_generation_task,
                }
            )
            self.create_continue_generation_task = create_continue_generation_task

        def after_task_completion(self, result, **kwargs):
            observed.append({"result": result, **kwargs})
            self.create_continue_generation_task(
                project_id=result.project_id,
                run_until_chapter=kwargs["run_until_chapter"],
                runtime_config=kwargs["runtime_config"],
            )

    class Result:
        project_id = "project-auto"
        status = "completed"
        completed_chapters = [1]
        failed_chapters: list[int] = []
        paused_chapters: list[int] = []

    def fake_create_continue_generation_task(**kwargs):
        observed.append({"scheduled": kwargs})
        return "task-next"

    monkeypatch.setattr(generation_api, "_get_session", lambda: DummySession())
    monkeypatch.setattr(generation_api, "_log_decision_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(generation_api, "GenerationAutoContinueController", ControllerStub)

    handler = generation_api._make_generation_completion_handler(
        task_id="task-prev",
        root_event_id="root-event",
        runtime_config=runtime_config,
        auto_continue=True,
        run_until_chapter=2,
        max_chapters=None,
        create_continue_generation_task=fake_create_continue_generation_task,
    )

    handler(Result())

    scheduled = next(item["scheduled"] for item in observed if "scheduled" in item)
    call = next(item for item in observed if item.get("parent_task_id") == "task-prev")
    assert scheduled["project_id"] == "project-auto"
    assert scheduled["run_until_chapter"] == 2
    assert scheduled["runtime_config"] is runtime_config
    assert call["run_until_chapter"] == 2
    assert call["runtime_config"] is runtime_config


def _session_factory(name: str):
    engine = get_engine(postgres_test_url(name))
    init_db(engine)
    return engine, get_session_factory(engine)


def _project(session, project_id: str = "project-auto", total: int = 6) -> Project:
    project = Project(
        id=project_id,
        title="Auto Book",
        premise="premise",
        genre="玄幻",
        creation_status="writing",
        target_total_chapters=total,
    )
    session.add(project)
    session.flush()
    return project


def _arc(session, *, project_id: str, arc_id: str, number: int, status: str, start: int, end: int) -> None:
    session.add(
        ArcPlanVersion(
            id=arc_id,
            project_id=project_id,
            arc_number=number,
            status=status,
            chapter_start=start,
            chapter_end=end,
            planned_target_size=end - start + 1,
            arc_synopsis=f"arc {number}",
        )
    )


def _chapter(session, *, project_id: str, arc_id: str, number: int, status: str) -> None:
    session.add(
        ChapterPlan(
            id=f"plan-{number}",
            project_id=project_id,
            arc_plan_id=arc_id,
            chapter_number=number,
            title=f"第{number}章",
            status=status,
        )
    )


def test_controller_continues_to_future_arc_when_no_blocker() -> None:
    engine, Session = _session_factory("auto-continue-future-arc")
    calls: list[dict[str, object]] = []
    runtime_config = object()
    try:
        with Session.begin() as session:
            project = _project(session)
            _arc(session, project_id=project.id, arc_id="arc-1", number=1, status="active", start=1, end=3)
            _arc(session, project_id=project.id, arc_id="arc-2", number=2, status="planned", start=4, end=6)
            for number in range(1, 4):
                _chapter(session, project_id=project.id, arc_id="arc-1", number=number, status="accepted")

        controller = GenerationAutoContinueController(
            session_factory=Session,
            create_continue_generation_task=lambda **kwargs: calls.append(kwargs) or "task-next",
        )
        decision = controller.after_task_completion(
            ResultStub(project_id="project-auto", completed_chapters=[1, 2, 3]),
            parent_task_id="task-prev",
            run_until_chapter=6,
            max_chapters=None,
            auto_continue=True,
            runtime_config=runtime_config,
        )

        assert decision == AutoContinueDecision(
            decision="continue",
            reason="future_arc_materialized",
            next_task_id="task-next",
            next_chapter=4,
            run_until_chapter=6,
            target_total_chapters=6,
            requested_chapters=3,
            workset_reason="future_arc_materialization_required",
        )
        assert calls[0]["project_id"] == "project-auto"
        assert calls[0]["requested_chapters"] == 3
        assert calls[0]["max_chapters"] == 3
        assert calls[0]["run_until_chapter"] == 6
        assert calls[0]["auto_continue"] is True
        assert calls[0]["runtime_config"] is runtime_config
        assert calls[0]["title"] == "Auto Book"
        assert calls[0]["subtitle"] == "自动续跑 · 玄幻"
        assert calls[0]["message"] == "前一批完成，无阻断，自动继续生成。"
    finally:
        engine.dispose()


def test_controller_filters_task_kwargs_for_current_strict_factory_signature() -> None:
    engine, Session = _session_factory("auto-continue-strict-factory")
    calls: list[dict[str, object]] = []
    runtime_config = object()

    def create_task(
        *,
        project_id,
        runtime_config,
        requested_chapters,
        max_chapters,
        title,
        subtitle,
        message,
    ):
        calls.append(
            {
                "project_id": project_id,
                "runtime_config": runtime_config,
                "requested_chapters": requested_chapters,
                "max_chapters": max_chapters,
                "title": title,
                "subtitle": subtitle,
                "message": message,
            }
        )
        return "task-strict"

    try:
        with Session.begin() as session:
            project = _project(session)
            _arc(session, project_id=project.id, arc_id="arc-1", number=1, status="active", start=1, end=3)
            _arc(session, project_id=project.id, arc_id="arc-2", number=2, status="planned", start=4, end=6)
            for number in range(1, 4):
                _chapter(session, project_id=project.id, arc_id="arc-1", number=number, status="accepted")

        controller = GenerationAutoContinueController(
            session_factory=Session,
            create_continue_generation_task=create_task,
        )
        decision = controller.after_task_completion(
            ResultStub(project_id="project-auto", completed_chapters=[1, 2, 3]),
            parent_task_id="task-prev",
            run_until_chapter=6,
            max_chapters=None,
            auto_continue=True,
            runtime_config=runtime_config,
        )

        assert decision.next_task_id == "task-strict"
        assert calls == [
            {
                "project_id": "project-auto",
                "runtime_config": runtime_config,
                "requested_chapters": 3,
                "max_chapters": 3,
                "title": "Auto Book",
                "subtitle": "自动续跑 · 玄幻",
                "message": "前一批完成，无阻断，自动继续生成。",
            }
        ]
    finally:
        engine.dispose()


def test_controller_stops_when_run_until_reached() -> None:
    engine, Session = _session_factory("auto-continue-until-reached")
    try:
        with Session.begin() as session:
            project = _project(session)
            _arc(session, project_id=project.id, arc_id="arc-1", number=1, status="active", start=1, end=3)
            for number in range(1, 4):
                _chapter(session, project_id=project.id, arc_id="arc-1", number=number, status="accepted")

        controller = GenerationAutoContinueController(
            session_factory=Session,
            create_continue_generation_task=lambda **kwargs: "unexpected",
        )
        decision = controller.after_task_completion(
            ResultStub(project_id="project-auto", completed_chapters=[1, 2, 3]),
            parent_task_id="task-prev",
            run_until_chapter=3,
            max_chapters=None,
            auto_continue=True,
        )

        assert decision.decision == "stop"
        assert decision.reason == "run_until_reached"
        assert decision.next_task_id == ""
    finally:
        engine.dispose()


def test_controller_audit_payload_contains_target_fields() -> None:
    engine, Session = _session_factory("auto-continue-audit-payload")
    try:
        with Session.begin() as session:
            project = _project(session, total=3)
            _arc(session, project_id=project.id, arc_id="arc-1", number=1, status="active", start=1, end=3)
            for number in range(1, 4):
                _chapter(session, project_id=project.id, arc_id="arc-1", number=number, status="accepted")

        controller = GenerationAutoContinueController(
            session_factory=Session,
            create_continue_generation_task=lambda **kwargs: "unexpected",
        )
        controller.after_task_completion(
            ResultStub(project_id="project-auto", completed_chapters=[1, 2, 3]),
            parent_task_id="task-prev",
            run_until_chapter=3,
            max_chapters=None,
            auto_continue=True,
        )

        with Session() as session:
            event = session.query(DecisionEvent).filter_by(event_type="auto_continue_decision").one()
            payload = json.loads(event.payload_json)

        assert payload["decision"] == "stop"
        assert payload["reason"] == "target_total_reached"
        assert payload["run_until_chapter"] == 3
        assert payload["target_total_chapters"] == 3
    finally:
        engine.dispose()


def test_controller_stops_on_pending_review_and_records_audit_event() -> None:
    engine, Session = _session_factory("auto-continue-review-block")
    try:
        with Session.begin() as session:
            project = _project(session)
            _arc(session, project_id=project.id, arc_id="arc-1", number=1, status="active", start=1, end=3)
            _chapter(session, project_id=project.id, arc_id="arc-1", number=1, status="accepted")
            _chapter(session, project_id=project.id, arc_id="arc-1", number=2, status="needs_review")

        controller = GenerationAutoContinueController(
            session_factory=Session,
            create_continue_generation_task=lambda **kwargs: "unexpected",
        )
        decision = controller.after_task_completion(
            ResultStub(project_id="project-auto", completed_chapters=[1]),
            parent_task_id="task-prev",
            run_until_chapter=6,
            max_chapters=None,
            auto_continue=True,
        )

        with Session() as session:
            events = session.query(DecisionEvent).filter_by(project_id="project-auto").all()

        assert decision.decision == "stop"
        assert decision.reason == "pending_review_blocker"
        assert any(event.event_type == "auto_continue_decision" for event in events)
    finally:
        engine.dispose()
