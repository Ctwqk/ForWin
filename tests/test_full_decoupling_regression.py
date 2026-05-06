from __future__ import annotations

from types import SimpleNamespace

from forwin.config import Config
from forwin.orchestrator.loop import RunResult
from forwin.protocol.experience import ChapterExperiencePlan
from forwin.protocol.review import ContinuityIssue, RepairInstruction, ReviewVerdict


def test_api_runtime_uses_runtime_container_for_generation_tasks(monkeypatch) -> None:
    from forwin import api_runtime

    calls: list[str] = []
    updates: list[dict] = []

    class FakeOrchestrator:
        llm_client = SimpleNamespace(close=lambda: calls.append("llm_closed"))
        engine = SimpleNamespace(dispose=lambda: calls.append("engine_disposed"))
        _SessionFactory = None

        def run(self, *, premise: str, genre: str, num_chapters: int):
            calls.append(f"run:{premise}:{genre}:{num_chapters}")
            return RunResult(project_id="project-1", requested_chapters=num_chapters, completed_chapters=[1])

    class FakeContainer:
        @classmethod
        def from_config(cls, config):
            calls.append("from_config")
            return cls()

        def build_writing_orchestrator(self, **kwargs):
            assert callable(kwargs["progress_callback"])
            calls.append("build_orchestrator")
            return FakeOrchestrator()

    monkeypatch.setattr(api_runtime, "RuntimeContainer", FakeContainer, raising=False)

    api_runtime.run_generation_with_config(
        task_id="task-1",
        premise="premise",
        genre="玄幻",
        num_chapters=1,
        config=Config(database_url="postgresql+psycopg://fake/forwin", minimax_api_key=""),
        update_task=lambda task_id, **changes: updates.append({"task_id": task_id, **changes}),
        logger=SimpleNamespace(
            exception=lambda *args, **kwargs: None,
            debug=lambda *args, **kwargs: None,
        ),
    )

    assert calls[:3] == ["from_config", "build_orchestrator", "from_config"] or calls[:2] == [
        "from_config",
        "build_orchestrator",
    ]
    assert "run:premise:玄幻:1" in calls
    assert any(update.get("status") == "completed" for update in updates)


def test_api_genesis_service_uses_runtime_container_when_available(monkeypatch) -> None:
    from forwin import api as api_module

    service = SimpleNamespace(llm_client=SimpleNamespace(client=SimpleNamespace(close=lambda: None)))

    class FakeContainer:
        def services(self):
            return SimpleNamespace(book_genesis=service)

    old_container = api_module._runtime_container
    try:
        api_module._runtime_container = FakeContainer()

        built = api_module._build_genesis_service()

        assert built is service
        assert getattr(built, "_forwin_runtime_owned") is True
        api_module._close_genesis_service(built)
    finally:
        api_module._runtime_container = old_container


def test_api_automation_can_use_runtime_production_scheduler_factory() -> None:
    from datetime import datetime, timezone

    from forwin import api_automation

    calls: list[str] = []

    class FakeFactory:
        def build(self, **kwargs):
            calls.append("build")
            assert kwargs["runtime_config_provider"]() == "runtime-config"
            assert kwargs["generation_terminal_statuses"] == {"completed"}
            return SimpleNamespace(run_due_projects=lambda *, now: calls.append(now.isoformat()))

    api_automation.run_automation_scheduler_pass(
        session_factory=object(),
        config=object(),
        saved_runtime_config_or_503=lambda: "runtime-config",
        utcnow=lambda: datetime(2026, 5, 6, tzinfo=timezone.utc),
        display_tz=timezone.utc,
        display_datetime=lambda value: "",
        get_session=lambda: None,
        persist_project_automation=lambda *args, **kwargs: None,
        create_generation_task=lambda **kwargs: "task-1",
        create_continue_generation_task=lambda **kwargs: "task-2",
        active_generation_task_error_cls=RuntimeError,
        terminal_statuses={"completed"},
        production_scheduler_factory=FakeFactory(),
    )

    assert calls == ["build", "2026-05-06T00:00:00+00:00"]


def test_reviewer_does_not_mutate_chapter_experience_plan() -> None:
    from forwin.protocol.context import ReviewContextPack
    from forwin.protocol.writer import WriterOutput
    from forwin.reviewer.experience import ExperienceReviewer

    plan = ChapterExperiencePlan(planned_reward_tags=["mystery"], progress_markers=["找到线索"])
    before = plan.model_dump(mode="json")
    context = ReviewContextPack(
        project_id="project-1",
        project_title="测试书",
        chapter_number=1,
        chapter_plan_title="第一章",
        chapter_plan_one_line="主角找到线索",
        chapter_experience_plan=plan,
    )
    writer_output = WriterOutput(
        project_id="project-1",
        chapter_number=1,
        title="第一章",
        body="他找到了线索。下一刻，更大的谜团出现。",
        end_of_chapter_summary="主角找到线索。",
    )

    ExperienceReviewer().review(context, writer_output)

    assert plan.model_dump(mode="json") == before


def test_review_hub_repair_merge_preserves_existing_scope_order() -> None:
    from forwin.reviewer.hub import HistoricalReviewHub

    base = RepairInstruction(
        repair_scope="draft",
        failure_type="continuity",
        must_fix=["修正文内断裂"],
        must_preserve=["章目标"],
        design_patch={"continuity_focus": ["state"]},
        evidence_refs=["continuity"],
    )
    webnovel = RepairInstruction(
        repair_scope="band_plan",
        failure_type="payoff_miss",
        must_fix=["补回报"],
        must_preserve=["章目标"],
        design_patch={"planned_reward_tags": ["mystery"]},
        evidence_refs=["experience"],
    )

    merged = HistoricalReviewHub._merge_repair_instructions(
        continuity_instruction=base,
        governance_instruction=None,
        webnovel_instruction=webnovel,
    )

    assert merged is not None
    assert merged.repair_scope == "band_plan"
    assert merged.failure_type == "mixed"
    assert merged.must_fix == ["修正文内断裂", "补回报"]
    assert merged.evidence_refs == ["continuity", "experience"]
