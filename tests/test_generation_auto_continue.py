"""Decisions exercised through persisted completion and the real enqueue owner."""

import pytest
from sqlalchemy import select

from forwin.generation.auto_continue import GenerationAutoContinueController
from forwin.generation.task_payload import payload_from_json
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from forwin.models.task import GenerationTask
from tests.test_generation_continuation_outbox import (
    setup_parent,
    finish,
    consume,
    decision,
)
from tests.test_generation_application_service import session_factory as session_factory


@pytest.mark.parametrize("paused", [False, True])
def test_safe_accepted_pause_marker_does_not_block_continuation(
    session_factory, paused
):
    service, task_id, epoch, result = setup_parent(session_factory)
    result.paused_chapters, result.paused = [1], paused
    finish(service, task_id, epoch, result)
    assert consume(session_factory).processed
    assert (
        decision(session_factory, task_id)["reason"] == "chapter_completed_no_blocker"
    )


def test_future_arc_materialization_has_durable_child_and_target_audit(session_factory):
    service, task_id, epoch, result = setup_parent(session_factory)
    with session_factory.begin() as session:
        session.delete(session.get(ChapterPlan, "chapter-2"))
        session.add(
            ArcPlanVersion(
                id="future",
                project_id="project-1",
                arc_number=2,
                status="planned",
                arc_synopsis="Next arc",
                chapter_start=2,
                chapter_end=8,
                planned_target_size=7,
            )
        )
    finish(service, task_id, epoch, result)
    assert consume(session_factory).processed
    answer = decision(session_factory, task_id)
    assert answer["reason"] == "future_arc_materialized"
    assert answer["next_chapter"] == 2
    assert answer["run_until_chapter"] == 8
    assert answer["requested_chapters"] == 1
    with session_factory() as session:
        child = session.get(GenerationTask, answer["next_task_id"])
        assert child.continuation_parent_task_id == task_id


@pytest.mark.parametrize(
    "total,until,expected",
    [(1, 1, "target_total_reached"), (100, 1, "run_until_reached")],
)
def test_run_target_stops_with_audit_fields(session_factory, total, until, expected):
    service, task_id, epoch, result = setup_parent(session_factory)
    with session_factory.begin() as session:
        session.get(Project, "project-1").target_total_chapters = total
        task = session.get(GenerationTask, task_id)
        task.execution_payload_json = (
            payload_from_json(task.execution_payload_json)
            .model_copy(update={"run_until_chapter": until})
            .model_dump_json()
        )
    finish(service, task_id, epoch, result)
    assert consume(session_factory).processed
    answer = decision(session_factory, task_id)
    assert answer["reason"] == expected
    assert answer["run_until_chapter"] == until
    assert answer["target_total_chapters"] == total


@pytest.mark.parametrize("blocker", ["hard", "exhausted", "prior_retry"])
def test_review_retry_preserves_existing_hard_blocks_and_exhaustion(
    session_factory, blocker
):
    service, task_id, epoch, result = setup_parent(session_factory, review=True)
    with session_factory.begin() as session:
        plan = session.get(ChapterPlan, "chapter-1")
        if blocker == "hard":
            plan.residual_review_issues_json = '[{"reviewer":"canon_quality","issue_type":"duplicate_artifact_resource"}]'
        elif blocker == "exhausted":
            plan.repair_attempt_count = 2
        else:
            from forwin.audit.events import DecisionEventInfo, DecisionEventType
            from forwin.state.updater import StateUpdater

            StateUpdater(session).save_decision_event(
                DecisionEventInfo(
                    project_id="project-1",
                    chapter_number=1,
                    event_family="audit_action",
                    event_type=DecisionEventType.RETRY_ATTEMPT,
                    actor_type="system",
                    payload={"source": "auto_continue_review_retry"},
                )
            )
    finish(service, task_id, epoch, result)
    assert consume(session_factory).processed
    assert decision(session_factory, task_id)["reason"] == "pending_review_blocker"
    with session_factory() as session:
        plan = session.get(ChapterPlan, "chapter-1")
        assert plan.status == "needs_review"
        if blocker == "exhausted":
            assert plan.repair_attempt_count == 2
        assert len(list(session.scalars(select(GenerationTask)))) == 1


def test_pending_acceptance_stops_without_creating_child(session_factory):
    service, task_id, epoch, result = setup_parent(session_factory)
    with session_factory.begin() as session:
        session.get(ChapterPlan, "chapter-2").status = "drafted"
    finish(service, task_id, epoch, result)
    assert consume(session_factory).processed
    assert decision(session_factory, task_id)["reason"] == "pending_acceptance_blocker"


def test_legacy_no_rule_matched_maps_to_explicit_manual_review_reason():
    from types import SimpleNamespace

    assert (
        GenerationAutoContinueController._terminal_block_reason(
            SimpleNamespace(status="no_rule_matched")
        )
        == "manual_review_required_blocker"
    )
