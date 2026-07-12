from __future__ import annotations

from types import SimpleNamespace

from forwin.protocol.experience import ChapterExperiencePlan
from forwin.protocol.review import RepairInstruction


def test_api_genesis_service_uses_runtime_container_when_available() -> None:
    from forwin.http import HttpRuntime

    service = SimpleNamespace(
        llm_client=SimpleNamespace(client=SimpleNamespace(close=lambda: None))
    )

    class FakeContainer:
        def services(self):
            return SimpleNamespace(book_genesis=service)

    runtime = HttpRuntime(container=FakeContainer())
    built = runtime.build_genesis_service()

    assert built is service
    assert getattr(built, "_forwin_runtime_owned") is True
    runtime.close_genesis_service(built)


def test_api_automation_can_use_runtime_production_scheduler_factory() -> None:
    from datetime import datetime, timezone

    from forwin import api_automation

    calls: list[str] = []

    class FakeFactory:
        def build(self, **kwargs):
            calls.append("build")
            assert kwargs["generation_terminal_statuses"] == {"completed"}
            assert "create_generation_task" not in kwargs
            assert "create_continue_generation_task" not in kwargs
            return SimpleNamespace(
                run_due_projects=lambda *, now: calls.append(now.isoformat())
            )

    api_automation.run_automation_scheduler_pass(
        session_factory=object(),
        config=object(),
        generation_application=SimpleNamespace(),
        utcnow=lambda: datetime(2026, 5, 6, tzinfo=timezone.utc),
        display_tz=timezone.utc,
        display_datetime=lambda value: "",
        get_session=lambda: None,
        persist_project_automation=lambda *args, **kwargs: None,
        terminal_statuses={"completed"},
        production_scheduler_factory=FakeFactory(),
    )

    assert calls == ["build", "2026-05-06T00:00:00+00:00"]


def test_reviewer_does_not_mutate_chapter_experience_plan() -> None:
    from forwin.protocol.context import ReviewContextPack
    from forwin.protocol.writer import WriterOutput
    from forwin.review.experience import ExperienceReviewer

    plan = ChapterExperiencePlan(
        planned_reward_tags=["mystery"], progress_markers=["找到线索"]
    )
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


def test_draft_review_repair_merge_preserves_existing_scope_order() -> None:
    from forwin.review.draft_service import DraftReviewService

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

    merged = DraftReviewService._merge_repair_instructions(
        continuity_instruction=base,
        plan_instruction=None,
        webnovel_instruction=webnovel,
    )

    assert merged is not None
    assert merged.repair_scope == "band_plan"
    assert merged.failure_type == "mixed"
    assert merged.must_fix == ["修正文内断裂", "补回报"]
    assert merged.evidence_refs == ["continuity", "experience"]
