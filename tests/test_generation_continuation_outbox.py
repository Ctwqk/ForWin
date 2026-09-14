"""PostgreSQL failure boundaries for the application/worker continuation handoff."""

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import event, select

from forwin.application.errors import GenerationTaskLeaseLost
from forwin.application.generation_execution import (
    _build_task_progress_changes,
    execute_pipeline_task,
)
from forwin.generation.pipeline_core.result import RunResult
from forwin.generation.task_lease import claim_generation_task
from forwin.generation.task_payload import payload_from_json
from forwin.models.audit import DecisionEvent
from forwin.models.outbox import OutboxEvent
from forwin.models.project import ChapterPlan, Project
from forwin.models.task import GenerationTask
from forwin.outbox.handlers import build_default_outbox_handlers
from forwin.outbox.worker import run_one_outbox_event
from forwin.runtime.policy import RuntimePolicy
from forwin.runtime.policy_store import ProjectPolicyStore
from tests import test_generation_application_service as application_fixtures
from tests import test_generation_worker_canon_recovery as recovery_fixtures
from tests.test_generation_application_service import (
    _command,
    _create_project,
    _service,
)

session_factory = application_fixtures.session_factory
recovery_fixture = recovery_fixtures.recovery_fixture


def setup_parent(factory, *, review=False):
    _create_project(factory)
    service = _service(factory)
    handle = service.enqueue(
        replace(
            _command(),
            requested_chapters=1,
            max_chapters=1,
            long_run_mode="soak_test",
            isolated=True,
        )
    )
    with factory.begin() as session:
        from forwin.state.updater import StateUpdater

        arc = StateUpdater(session).create_arc_plan("project-1", "Arc")
        session.add_all(
            [
                ChapterPlan(
                    id="chapter-1",
                    arc_plan_id=arc.id,
                    project_id="project-1",
                    chapter_number=1,
                    status="needs_review" if review else "accepted",
                    canon_risk_level="high" if review else "",
                ),
                ChapterPlan(
                    id="chapter-2",
                    arc_plan_id=arc.id,
                    project_id="project-1",
                    chapter_number=2,
                    status="planned",
                ),
            ]
        )
        claim = claim_generation_task(session, worker_id="worker", lease_seconds=300)
    result = RunResult(
        project_id="project-1",
        requested_chapters=1,
        completed_chapters=[] if review else [1],
        paused_chapters=[1] if review else [],
        system_block_chapters=[1] if review else [],
    )
    return service, handle.task_id, claim.task.lease_epoch, result


def finish(service, task_id, epoch, result):
    service.finish_claimed_task(task_id, result, worker_id="worker", lease_epoch=epoch)


def consume(factory):
    return run_one_outbox_event(
        session_factory=factory,
        worker_id="outbox",
        handlers=build_default_outbox_handlers(
            session_factory=factory, config=_service(factory).infrastructure
        ),
    )


def decision(factory, task_id):
    with factory() as session:
        row = session.scalar(
            select(DecisionEvent).where(
                DecisionEvent.task_id == task_id,
                DecisionEvent.event_type == "auto_continue_decision",
            )
        )
        return json.loads(row.payload_json) if row else None


def test_finish_and_intent_rollback_together(session_factory):
    service, task_id, epoch, result = setup_parent(session_factory)

    def fail_insert(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO outbox_events"):
            raise RuntimeError("injected intent failure")

    engine = session_factory.kw["bind"]
    event.listen(engine, "before_cursor_execute", fail_insert)
    try:
        with pytest.raises(RuntimeError, match="intent failure"):
            finish(service, task_id, epoch, result)
    finally:
        event.remove(engine, "before_cursor_execute", fail_insert)
    with session_factory() as session:
        assert session.get(GenerationTask, task_id).status == "running"
        assert not list(session.scalars(select(OutboxEvent)))
    finish(service, task_id, epoch, result)
    assert consume(session_factory).processed


def test_fresh_consumer_after_completion_and_ack_loss_replays_once(session_factory):
    service, task_id, epoch, result = setup_parent(session_factory)
    parent_payload = None
    with session_factory() as session:
        parent_payload = payload_from_json(
            session.get(GenerationTask, task_id).execution_payload_json
        )
    finish(service, task_id, epoch, result)
    del service  # No callback or process-local continuation state is retained.
    assert consume(session_factory).processed
    with session_factory.begin() as session:
        child = session.scalar(
            select(GenerationTask).where(
                GenerationTask.continuation_parent_task_id == task_id
            )
        )
        child_id = child.id
        payload = payload_from_json(child.execution_payload_json)
        assert payload.policy_snapshot == parent_payload.policy_snapshot
        assert payload.policy_version == parent_payload.policy_version
        assert (payload.run_until_chapter, payload.long_run_mode, payload.isolated) == (
            8,
            "soak_test",
            True,
        )
        child.status = "completed"
        child.deleted_at = datetime.now(UTC)
        session.scalar(
            select(OutboxEvent)
        ).status = "pending"  # child commit survived a lost ack
    assert consume(session_factory).processed
    with session_factory() as session:
        assert [
            x.id
            for x in session.scalars(
                select(GenerationTask).where(
                    GenerationTask.continuation_parent_task_id == task_id
                )
            )
        ] == [child_id]
    assert decision(session_factory, task_id)["next_task_id"] == child_id


def test_policy_change_stops_old_snapshot(session_factory):
    service, task_id, epoch, result = setup_parent(session_factory)
    finish(service, task_id, epoch, result)
    with session_factory.begin() as session:
        project = session.get(Project, "project-1")
        ProjectPolicyStore(session).save(
            project, RuntimePolicy.for_profile("pulp"), expected_version=1
        )
    assert consume(session_factory).processed
    assert decision(session_factory, task_id)["reason"] == "policy_changed"
    with session_factory() as session:
        parent = payload_from_json(
            session.get(GenerationTask, task_id).execution_payload_json
        )
        assert parent.policy_version == 1
        assert parent.policy_snapshot == RuntimePolicy.for_profile("standard")
        assert len(list(session.scalars(select(GenerationTask)))) == 1


def test_two_consumers_share_one_decision(session_factory):
    service, task_id, epoch, result = setup_parent(session_factory)
    finish(service, task_id, epoch, result)
    from forwin.generation.continuation_events import (
        GenerationContinuationEvent,
        consume_continuation,
    )

    with session_factory() as session:
        payload = GenerationContinuationEvent.model_validate_json(
            session.scalar(select(OutboxEvent)).payload_json
        )

    def run():
        with session_factory.begin() as session:
            return consume_continuation(session, payload, service)

    with ThreadPoolExecutor(max_workers=2) as pool:
        answers = list(pool.map(lambda _: run(), range(2)))
    assert answers[0] == answers[1]
    with session_factory() as session:
        assert len(list(session.scalars(select(GenerationTask)))) == 2


def test_review_reset_rolls_back_if_enqueue_fails(session_factory, monkeypatch):
    service, task_id, epoch, result = setup_parent(session_factory, review=True)
    finish(service, task_id, epoch, result)
    from forwin.generation.task_repository import GenerationTaskRepository

    original = GenerationTaskRepository.create

    def fail(*args, **kwargs):
        raise RuntimeError("enqueue failure")

    monkeypatch.setattr(GenerationTaskRepository, "create", fail)
    assert not consume(session_factory).processed
    with session_factory() as session:
        assert session.get(ChapterPlan, "chapter-1").status == "needs_review"
        assert not list(
            session.scalars(
                select(DecisionEvent).where(DecisionEvent.event_type == "retry_attempt")
            )
        )
    assert decision(session_factory, task_id) is None
    monkeypatch.setattr(GenerationTaskRepository, "create", original)
    with session_factory.begin() as session:
        session.scalar(select(OutboxEvent)).available_at = None
    assert consume(session_factory).processed
    assert decision(session_factory, task_id)["reason"] == "auto_retry_review_blocker"


def test_stale_lease_cannot_finalize(session_factory):
    service, task_id, epoch, result = setup_parent(session_factory)
    with pytest.raises(GenerationTaskLeaseLost):
        finish(service, task_id, epoch - 1, result)
    with session_factory() as session:
        assert session.get(GenerationTask, task_id).status == "running"
        assert not list(session.scalars(select(OutboxEvent)))


@pytest.mark.parametrize(
    "stage", ["completed", "failed", "paused", "cancelled", "paused_for_review"]
)
def test_progress_never_publishes_terminal_status(stage):
    assert "status" not in _build_task_progress_changes("progress", {"stage": stage})


def runtime_for(factory):
    from forwin.http.runtime import HttpRuntime

    return HttpRuntime(
        config=_service(factory).infrastructure,
        engine=factory.kw["bind"],
        session_factory=factory,
    )


@pytest.mark.parametrize("action", ["pause", "terminate"])
def test_pending_completed_parent_exposes_stop_and_stops_intent(
    session_factory, action
):
    from forwin.http.project_support import _mutate_generation_task
    from forwin.http.tasks import (
        _get_task_center_service,
        _task_is_pausable,
        _task_is_terminable,
    )

    service, task_id, epoch, result = setup_parent(session_factory)
    finish(service, task_id, epoch, result)
    runtime = runtime_for(session_factory)
    task = _get_task_center_service(runtime).load_generation_task(task_id)
    assert _task_is_pausable(task)
    assert _task_is_terminable(task)
    response = _mutate_generation_task(runtime, task_id, action)
    assert response.ok
    assert response.status == "completed"
    assert consume(session_factory).processed
    assert decision(session_factory, task_id)["reason"] == (
        "user_pause_requested" if action == "pause" else "cancel_requested"
    )
    with session_factory() as session:
        assert len(list(session.scalars(select(GenerationTask)))) == 1


@pytest.mark.parametrize("action", ["pause", "terminate"])
def test_stop_racing_consumer_never_claims_success_after_child_created(
    session_factory, action
):
    from fastapi import HTTPException

    from forwin.generation.continuation_events import (
        GenerationContinuationEvent,
        consume_continuation,
    )
    from forwin.http.project_support import _mutate_generation_task

    service, task_id, epoch, result = setup_parent(session_factory)
    finish(service, task_id, epoch, result)
    with session_factory() as session:
        payload = GenerationContinuationEvent.model_validate_json(
            session.scalar(select(OutboxEvent)).payload_json
        )
    from threading import Event

    child_created, release = Event(), Event()

    def consumer():
        with session_factory.begin() as session:
            answer = consume_continuation(session, payload, service)
            child_created.set()
            assert release.wait(5)
            return answer

    def stop():
        try:
            return _mutate_generation_task(
                runtime_for(session_factory), task_id, action
            )
        except HTTPException as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        consumer_future = pool.submit(consumer)
        assert child_created.wait(5)
        stop_future = pool.submit(stop)
        release.set()
        answer = consumer_future.result(timeout=5)
        response = stop_future.result(timeout=5)
    assert answer.next_task_id
    assert isinstance(response, HTTPException)
    with session_factory() as session:
        assert not session.get(GenerationTask, task_id).pause_requested
        assert not session.get(GenerationTask, task_id).cancel_requested


def test_new_run_supersedes_pending_intent_permanently(session_factory):
    service, task_id, epoch, result = setup_parent(session_factory)
    finish(service, task_id, epoch, result)
    new = service.enqueue(_command())
    assert consume(session_factory).processed
    assert decision(session_factory, task_id)["reason"] == "superseded"
    with session_factory.begin() as session:
        session.get(GenerationTask, new.task_id).status = "completed"
        session.scalar(select(OutboxEvent)).status = "pending"
    assert consume(session_factory).processed
    assert decision(session_factory, task_id)["reason"] == "superseded"


def test_committed_completion_survives_display_exception(session_factory):
    service, task_id, epoch, result = setup_parent(session_factory)

    class Pipeline:
        def close(self):
            pass

    def display(result):
        raise RuntimeError("display failed")

    execute_pipeline_task(
        task_id,
        Pipeline(),
        lambda: result,
        update_task=service._task_updater(worker_id="worker", lease_epoch=epoch),
        logger=logging.getLogger(__name__),
        error_message="failed",
        default_project_id="project-1",
        progress_handler=display,
        finish_task=lambda result: finish(service, task_id, epoch, result),
    )
    with session_factory() as session:
        assert session.get(GenerationTask, task_id).status == "completed"
        assert session.scalar(select(OutboxEvent)) is not None
    assert consume(session_factory).processed


def test_worker_finishing_transaction_failure_remains_recoverable(session_factory):
    service, task_id, epoch, result = setup_parent(session_factory)

    class Pipeline:
        def close(self):
            pass

    def failed_finish(result):
        raise RuntimeError("commit failed")

    with pytest.raises(RuntimeError, match="commit failed"):
        execute_pipeline_task(
            task_id,
            Pipeline(),
            lambda: result,
            update_task=service._task_updater(worker_id="worker", lease_epoch=epoch),
            logger=logging.getLogger(__name__),
            error_message="failed",
            finish_task=failed_finish,
        )
    with session_factory() as session:
        assert session.get(GenerationTask, task_id).status == "running"
        assert session.scalar(select(OutboxEvent)) is None


def test_task_updater_cannot_publish_terminal_completion(session_factory):
    service, task_id, epoch, _result = setup_parent(session_factory)
    with pytest.raises(ValueError, match="finalization"):
        service._task_updater(worker_id="worker", lease_epoch=epoch)(
            task_id, status="completed"
        )
    with session_factory() as session:
        assert session.get(GenerationTask, task_id).status == "running"


@pytest.mark.parametrize("daily", [False, True])
def test_superseded_maintenance_cannot_block_current_ready_continuation(
    recovery_fixture, daily
):
    from forwin.canon.admission import CanonAdmissionService
    from forwin.generation.continuation_events import (
        GenerationContinuationEvent,
        consume_continuation,
    )
    from forwin.models.canon import CanonCommitRecord
    from forwin.models.maintenance import PostCanonMaintenanceRun

    fixture = recovery_fixture
    outcome = CanonAdmissionService(session_factory=fixture.Session).commit_plan(
        fixture.plan
    )
    assert not outcome.blocked
    with fixture.Session.begin() as session:
        project = session.get(Project, fixture.project_id)
        project.target_total_chapters = 20 if daily else 100
        project.creation_status = "writing"
        task = session.get(GenerationTask, fixture.task_id)
        payload = payload_from_json(task.execution_payload_json).model_copy(
            update={
                "auto_continue": True,
                "run_until_chapter": 10,
                "long_run_mode": "daily_serial" if daily else "soak_test",
                "isolated": not daily,
            }
        )
        task.execution_payload_json = payload.model_dump_json()
        task.status, task.lease_owner, task.lease_epoch = "running", "worker", 1
        plan = session.get(ChapterPlan, fixture.chapter_plan_id)
        session.add(
            ChapterPlan(
                id="next-plan",
                project_id=project.id,
                arc_plan_id=plan.arc_plan_id,
                chapter_number=2,
                status="planned",
            )
        )
        session.add(
            CanonCommitRecord(
                id="old-commit",
                idempotency_key="old-key",
                candidate_id=fixture.candidate_id,
                project_id=project.id,
                chapter_plan_id=plan.id,
                chapter_number=1,
                acceptance_revision=2,
            )
        )
        session.flush()
        for commit_id in (outcome.commit_id, "old-commit"):
            for step in ("planning", "arc", "world", "feedback"):
                session.add(
                    PostCanonMaintenanceRun(
                        canon_commit_id=commit_id,
                        project_id=project.id,
                        chapter_number=1,
                        candidate_id=fixture.candidate_id,
                        step_name=step,
                        idempotency_key=f"{commit_id}:{step}",
                        status="succeeded"
                        if commit_id == outcome.commit_id
                        else "failed",
                        result_json='{"order_controls":{"status":"succeeded","result":{}}}',
                    )
                )
    service = _service(fixture.Session)
    finish(
        service,
        fixture.task_id,
        1,
        RunResult(
            project_id=fixture.project_id, requested_chapters=1, completed_chapters=[1]
        ),
    )
    with fixture.Session.begin() as session:
        intent = session.scalar(
            select(OutboxEvent).where(OutboxEvent.aggregate_type == "generation_task")
        )
        answer = consume_continuation(
            session,
            GenerationContinuationEvent.model_validate_json(intent.payload_json),
            service,
        )
    assert answer.decision == "continue"
    with fixture.Session() as session:
        child = session.get(GenerationTask, answer.next_task_id)
        assert child.status == "queued"
        if daily:
            from forwin.production.capacity import SerialCapacityService

            capacity = SerialCapacityService(session).snapshot(fixture.project_id)
            assert capacity.limit == 3
            assert capacity.accepted == 1
            assert child.requested_chapters <= capacity.available == 2
        assert child.resume_from_chapter == 2


def _finish_then_exit(database_url, task_id, epoch, result_json):
    import os

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from forwin.generation.continuation_events import GenerationCompletionResult

    factory = sessionmaker(bind=create_engine(database_url), expire_on_commit=False)
    finish(
        _service(factory),
        task_id,
        epoch,
        GenerationCompletionResult.model_validate_json(result_json),
    )
    os._exit(0)


def test_process_exit_after_completion_needs_no_in_memory_callback(session_factory):
    import multiprocessing
    from dataclasses import asdict

    _service_instance, task_id, epoch, result = setup_parent(session_factory)
    process = multiprocessing.get_context("spawn").Process(
        target=_finish_then_exit,
        args=(
            session_factory.kw["bind"].url.render_as_string(hide_password=False),
            task_id,
            epoch,
            json.dumps(asdict(result)),
        ),
    )
    process.start()
    process.join(15)
    assert process.exitcode == 0
    assert consume(session_factory).processed
    assert decision(session_factory, task_id)["next_task_id"]


def test_child_commit_before_ack_failure_replays_without_second_child(
    session_factory, monkeypatch
):
    from datetime import timedelta

    from forwin.outbox import store

    service, task_id, epoch, result = setup_parent(session_factory)
    finish(service, task_id, epoch, result)
    original = store.mark_outbox_event_processed

    def ack_failure(*args, **kwargs):
        raise RuntimeError("ack connection lost")

    monkeypatch.setattr(store, "mark_outbox_event_processed", ack_failure)
    with pytest.raises(RuntimeError, match="ack connection lost"):
        consume(session_factory)
    first = decision(session_factory, task_id)["next_task_id"]
    with session_factory.begin() as session:
        row = session.scalar(select(OutboxEvent))
        row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    monkeypatch.setattr(store, "mark_outbox_event_processed", original)
    assert consume(session_factory).processed
    assert decision(session_factory, task_id)["next_task_id"] == first
    with session_factory() as session:
        assert len(list(session.scalars(select(GenerationTask)))) == 2


def test_runtime_outbox_continuation_does_not_boot_pipeline_or_model(
    session_factory, monkeypatch
):
    from forwin.config import InfrastructureConfig
    from forwin.runtime.container import RuntimeContainer

    service, task_id, epoch, result = setup_parent(session_factory)
    finish(service, task_id, epoch, result)

    def forbidden(*args, **kwargs):
        raise AssertionError("continuation must not construct model services")

    monkeypatch.setattr(RuntimeContainer, "_build_generation_services", forbidden)
    monkeypatch.setattr(
        RuntimeContainer, "_provide_outbox_post_canon_maintenance", forbidden
    )
    config = InfrastructureConfig(
        database_url=session_factory.kw["bind"].url.render_as_string(
            hide_password=False
        )
    )
    container = RuntimeContainer.for_outbox_worker(
        config, policy=RuntimePolicy.for_profile("standard")
    )
    try:
        answer = run_one_outbox_event(
            session_factory=session_factory,
            worker_id="outbox-runtime",
            handlers=container.build_outbox_handlers(),
        )
        assert answer.processed
    finally:
        container.close()


def test_migration_preserves_existing_task_rows_and_null_history():
    from alembic import command
    from sqlalchemy import MetaData, Table, create_engine, text
    from sqlalchemy.exc import IntegrityError

    from tests.postgres import postgres_empty_test_url
    from tests.test_v5_live_migration import _alembic_config

    url = postgres_empty_test_url("continuation-migration")
    engine = create_engine(url)
    config = _alembic_config(url)
    command.upgrade(config, "0007_feedback_actions")
    try:
        with engine.begin() as connection:
            table = Table("generation_tasks", MetaData(), autoload_with=connection)
            defaults = {
                c.name: c.default.arg
                for c in GenerationTask.__table__.columns
                if c.name in table.c
                and c.default is not None
                and (c.default.is_scalar or c.default.is_clause_element)
            }
            for identity in ("old-one", "old-two"):
                connection.execute(
                    table.insert().values(
                        **(
                            defaults
                            | {"id": identity, "status": "completed", "project_id": ""}
                        )
                    )
                )
            before = list(
                connection.execute(
                    text("SELECT * FROM generation_tasks ORDER BY id")
                ).mappings()
            )
        command.upgrade(config, "head")
        with engine.begin() as connection:
            after = list(
                connection.execute(
                    text("SELECT * FROM generation_tasks ORDER BY id")
                ).mappings()
            )
            assert [{key: row[key] for key in before[0]} for row in after] == before
            assert all(row["continuation_parent_task_id"] is None for row in after)
            assert connection.scalar(text("SELECT count(*) FROM outbox_events")) == 0
            connection.execute(
                text(
                    "UPDATE generation_tasks SET continuation_parent_task_id='parent' WHERE id='old-one'"
                )
            )
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE generation_tasks SET continuation_parent_task_id='parent' WHERE id='old-two'"
                )
            )
    finally:
        engine.dispose()


@pytest.mark.parametrize("action", ["pause", "terminate"])
def test_parent_stop_holds_project_lock_until_commit_before_consumer(
    session_factory, action
):
    from threading import Event, current_thread

    from forwin.http.project_support import _mutate_generation_task

    service, task_id, epoch, result = setup_parent(session_factory)
    finish(service, task_id, epoch, result)
    stop_inserted, release_stop = Event(), Event()
    engine = session_factory.kw["bind"]

    def hold_stop(conn, cursor, statement, parameters, context, executemany):
        if current_thread().name.startswith("stop-control") and statement.startswith(
            "INSERT INTO decision_events"
        ):
            stop_inserted.set()
            assert release_stop.wait(5)

    event.listen(engine, "after_cursor_execute", hold_stop)
    try:
        with (
            ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="stop-control"
            ) as stops,
            ThreadPoolExecutor(max_workers=1) as consumers,
        ):
            stop_future = stops.submit(
                _mutate_generation_task, runtime_for(session_factory), task_id, action
            )
            assert stop_inserted.wait(5)
            consume_future = consumers.submit(consume, session_factory)
            release_stop.set()
            assert stop_future.result(timeout=5).ok
            assert consume_future.result(timeout=5).processed
    finally:
        release_stop.set()
        event.remove(engine, "after_cursor_execute", hold_stop)
    assert decision(session_factory, task_id)["reason"] == (
        "user_pause_requested" if action == "pause" else "cancel_requested"
    )
    with session_factory() as session:
        assert len(list(session.scalars(select(GenerationTask)))) == 1


@pytest.mark.parametrize(
    ("completed", "failure_reason", "expected_status", "expected_error"),
    [
        ([], "", "failed", "以下章节生成失败: 2, 3"),
        ([1], "", "partial_failed", "以下章节生成失败: 2, 3"),
        ([1], "provider unavailable", "failed", "provider unavailable"),
    ],
)
def test_failed_chapter_diagnostic_is_durable_before_late_display_updates(
    session_factory, completed, failure_reason, expected_status, expected_error
):
    from forwin.generation.continuation_events import GenerationCompletionResult

    service, task_id, epoch, _ = setup_parent(session_factory)
    result = RunResult(
        project_id="project-1",
        requested_chapters=3,
        completed_chapters=completed,
        failed_chapters=[2, 3],
    )
    if failure_reason:
        result = GenerationCompletionResult.from_result(result).model_copy(
            update={"failure_reason": failure_reason}
        )

    finish(service, task_id, epoch, result)
    with session_factory() as session:
        parent = session.get(GenerationTask, task_id)
        assert parent.status == expected_status
        assert parent.error_message == expected_error
        assert json.loads(parent.failed_chapters_json) == [2, 3]
        assert session.scalar(select(OutboxEvent)) is not None

    service._task_updater(worker_id="worker", lease_epoch=epoch)(
        task_id,
        message="display callback finished",
        error="late callback must not replace the durable failure",
        failed_chapters=[],
    )
    with session_factory() as session:
        parent = session.get(GenerationTask, task_id)
        assert parent.status == expected_status
        assert parent.message == "display callback finished"
        assert parent.error_message == expected_error
        assert json.loads(parent.failed_chapters_json) == [2, 3]


def test_operation_exception_finalizes_without_inventing_a_failed_chapter(
    session_factory,
):
    service, task_id, epoch, _result = setup_parent(session_factory)

    class Pipeline:
        def close(self):
            pass

    def operation():
        raise RuntimeError("model service unavailable before chapter")

    execute_pipeline_task(
        task_id,
        Pipeline(),
        operation,
        update_task=service._task_updater(worker_id="worker", lease_epoch=epoch),
        logger=logging.getLogger(__name__),
        error_message="failed",
        default_project_id="project-1",
        finish_task=lambda result: finish(service, task_id, epoch, result),
    )
    with session_factory() as session:
        parent = session.get(GenerationTask, task_id)
        assert parent.status == "failed"
        assert json.loads(parent.failed_chapters_json) == []
        assert "model service unavailable" in parent.error_message
        assert session.scalar(select(OutboxEvent))


def test_deleted_parent_intent_stops_without_creating_a_child(session_factory):
    from forwin.http.project_support import _mutate_generation_task

    service, task_id, epoch, result = setup_parent(session_factory)
    finish(service, task_id, epoch, result)
    assert _mutate_generation_task(runtime_for(session_factory), task_id, "delete").ok
    assert consume(session_factory).processed
    assert decision(session_factory, task_id)["reason"] == "parent_deleted"


def test_expired_same_worker_epoch_cannot_finalize(session_factory):
    from datetime import timedelta

    service, task_id, epoch, result = setup_parent(session_factory)
    with session_factory.begin() as session:
        session.get(GenerationTask, task_id).lease_expires_at = datetime.now(
            UTC
        ) - timedelta(seconds=1)
    with pytest.raises(GenerationTaskLeaseLost):
        finish(service, task_id, epoch, result)
    with session_factory() as session:
        assert session.scalar(select(OutboxEvent)) is None
        assert session.get(GenerationTask, task_id).status == "running"


def maintenance_parent(fixture, *, state="pending", result_changes=None):
    """Real Canon admission and application completion; deterministic maintenance runners."""
    from datetime import timedelta
    from types import SimpleNamespace

    from forwin.canon.admission import CanonAdmissionService
    from forwin.maintenance.post_canon import PostCanonMaintenanceService

    outcome = CanonAdmissionService(session_factory=fixture.Session).commit_plan(
        fixture.plan
    )
    assert not outcome.blocked
    with fixture.Session.begin() as session:
        project = session.get(Project, fixture.project_id)
        project.target_total_chapters = 10
        project.creation_status = "writing"
        task = session.get(GenerationTask, fixture.task_id)
        task.execution_payload_json = (
            payload_from_json(task.execution_payload_json)
            .model_copy(
                update={
                    "auto_continue": True,
                    "run_until_chapter": 10,
                    "long_run_mode": "soak_test",
                    "isolated": True,
                }
            )
            .model_dump_json()
        )
        task.status, task.lease_owner, task.lease_epoch = "running", "worker", 1
        plan = session.get(ChapterPlan, fixture.chapter_plan_id)
        session.add(
            ChapterPlan(
                id="next-maintenance-plan",
                project_id=project.id,
                arc_plan_id=plan.arc_plan_id,
                chapter_number=2,
                status="planned",
            )
        )
        # Keep real phase3 intent durable but isolate this worker's continuation delivery.
        for row in session.scalars(select(OutboxEvent)):
            row.available_at = datetime.now(UTC) + timedelta(days=1)
    maintenance = PostCanonMaintenanceService(
        session_factory=fixture.Session,
        stage_analyzer=None,
        pacing_strategist=None,
        replan_governor=None,
        arc_envelope_manager=None,
        world_simulator=None,
        artifact_store=None,
        llm_client=SimpleNamespace(drain_llm_attempt_events=list),
    )
    maintenance._step_runners = {
        step: lambda session, commit: {}
        for step in ("planning", "arc", "world", "feedback")
    }
    if state != "missing":
        maintenance._ensure_runs(outcome.commit_id)
    if state.startswith("controls_"):
        maintenance.run(canon_commit_id=outcome.commit_id, worker_id="maintenance")
        if state == "controls_failed":
            from forwin.maintenance.post_canon import PostCanonMaintenanceError

            def fail_controls(session, commit):
                raise RuntimeError("temporary controls failure")

            with pytest.raises(PostCanonMaintenanceError):
                maintenance.run_order_controls(
                    canon_commit_id=outcome.commit_id, runner=fail_controls
                )
    elif state in {"running", "failed"}:
        claim, _ = maintenance._claim_step(
            canon_commit_id=outcome.commit_id,
            step_name="planning",
            worker_id="maintenance",
        )
        if state == "failed":
            maintenance._fail_claim(claim, RuntimeError("temporary step failure"))
    service = _service(fixture.Session)
    result = RunResult(
        project_id=fixture.project_id,
        requested_chapters=1,
        completed_chapters=[1],
        paused_chapters=[1],
        **(result_changes or {}),
    )
    finish(service, fixture.task_id, 1, result)
    return maintenance, outcome.commit_id


def continuation_row(session, parent_id):
    from forwin.generation.continuation_events import continuation_event_id

    return session.scalar(
        select(OutboxEvent).where(
            OutboxEvent.event_id == continuation_event_id(parent_id)
        )
    )


def child_count(factory, parent_id):
    with factory() as session:
        return len(
            list(
                session.scalars(
                    select(GenerationTask).where(
                        GenerationTask.continuation_parent_task_id == parent_id
                    )
                )
            )
        )


@pytest.mark.parametrize(
    "state",
    ["missing", "pending", "running", "failed", "controls_missing", "controls_failed"],
)
def test_maintenance_wait_survives_worker_replay_and_ack_loss(recovery_fixture, state):
    from forwin.models.maintenance import PostCanonMaintenanceRun

    fixture = recovery_fixture
    maintenance, commit_id = maintenance_parent(fixture, state=state)
    first = consume(fixture.Session)
    assert not first.processed
    assert first.message == "deferred"
    assert decision(fixture.Session, fixture.task_id) is None
    assert child_count(fixture.Session, fixture.task_id) == 0
    with fixture.Session.begin() as session:
        row = continuation_row(session, fixture.task_id)
        assert row.status == "pending"
        assert row.attempts == 1
        assert row.available_at.replace(tzinfo=UTC) > datetime.now(UTC)
        # Expire an interrupted lease / retry deadline; the owner still executes success.
        for run in session.scalars(select(PostCanonMaintenanceRun)):
            run.lease_expires_at = datetime.now(UTC)
            run.available_at = None
    maintenance.run(canon_commit_id=commit_id, worker_id="recovered-maintenance")
    maintenance.run_order_controls(
        canon_commit_id=commit_id, runner=lambda session, commit: {}
    )
    with fixture.Session.begin() as session:
        continuation_row(session, fixture.task_id).available_at = None
    assert consume(fixture.Session).processed
    assert child_count(fixture.Session, fixture.task_id) == 1
    with fixture.Session.begin() as session:
        continuation_row(session, fixture.task_id).status = "pending"
    assert consume(fixture.Session).processed
    assert child_count(fixture.Session, fixture.task_id) == 1


@pytest.mark.parametrize(
    "control,reason",
    [
        ("pause", "user_pause_requested"),
        ("cancel", "cancel_requested"),
        ("policy", "policy_changed"),
        ("target", "target_total_reached"),
    ],
)
def test_controls_during_maintenance_wait_permanently_stop(
    recovery_fixture, control, reason
):
    fixture = recovery_fixture
    maintenance_parent(fixture)
    assert not consume(fixture.Session).processed
    with fixture.Session.begin() as session:
        task = session.get(GenerationTask, fixture.task_id)
        project = session.get(Project, fixture.project_id)
        if control == "pause":
            task.pause_requested = True
        elif control == "cancel":
            task.cancel_requested = True
        elif control == "policy":
            ProjectPolicyStore(session).save(
                project, RuntimePolicy.for_profile("pulp"), expected_version=1
            )
        else:
            project.target_total_chapters = 1
        continuation_row(session, fixture.task_id).available_at = None
    assert consume(fixture.Session).processed
    assert decision(fixture.Session, fixture.task_id)["reason"] == reason
    assert child_count(fixture.Session, fixture.task_id) == 0


def test_failed_chapter_is_not_hidden_by_pending_maintenance(recovery_fixture):
    fixture = recovery_fixture
    maintenance_parent(fixture, result_changes={"failed_chapters": [2]})
    assert consume(fixture.Session).processed
    assert (
        decision(fixture.Session, fixture.task_id)["reason"]
        == "failed_chapters_blocker"
    )


@pytest.mark.parametrize(
    "blocker", ["checkpoint", "future_plan", "manual", "manual_missing"]
)
def test_content_blockers_stop_even_before_maintenance_completes(
    recovery_fixture, blocker
):
    from forwin.models.maintenance import PostCanonMaintenanceRun
    from forwin.models.planning_control import BandCheckpoint

    fixture = recovery_fixture
    maintenance_parent(
        fixture, state="missing" if blocker == "manual_missing" else "pending"
    )
    with fixture.Session.begin() as session:
        if blocker.startswith("manual"):
            session.add(
                BandCheckpoint(
                    project_id=fixture.project_id,
                    arc_id=session.get(
                        ChapterPlan, fixture.chapter_plan_id
                    ).arc_plan_id,
                    band_id="band-1",
                    boundary_chapter=1,
                    boundary_kind="chapter_accepted",
                    trigger_source="manual_boundary",
                    status="pending",
                )
            )
        else:
            feedback = session.scalar(
                select(PostCanonMaintenanceRun).where(
                    PostCanonMaintenanceRun.step_name == "feedback"
                )
            )
            result = (
                {"checkpoint": {"status": "fail"}}
                if blocker == "checkpoint"
                else {
                    "future_plan_audit": {"blocking_reasons": ["future plan conflict"]}
                }
            )
            feedback.result_json = json.dumps(
                {"order_controls": {"status": "blocked", "result": result}}
            )
    assert consume(fixture.Session).processed
    assert decision(fixture.Session, fixture.task_id)["reason"] == "maintenance_blocker"
    assert child_count(fixture.Session, fixture.task_id) == 0


@pytest.mark.parametrize("mode", ["soak_test", "daily_serial"])
@pytest.mark.parametrize("audit_fail_once", [False, True])
def test_phase3_outbox_recovers_controls_after_parent_exits(
    recovery_fixture, monkeypatch, mode, audit_fail_once
):
    from types import SimpleNamespace

    from forwin.canon.outbox_events import CANON_PHASE3_REQUESTED
    from forwin.maintenance.events import build_post_canon_outbox_handlers
    from forwin.maintenance.order_controls import recover_order_controls
    from forwin.models.draft import CandidateDraftRecord
    from forwin.planning.future_plan_audit import FuturePlanAuditor, FuturePlanAuditRun

    fixture = recovery_fixture
    maintenance, _ = maintenance_parent(fixture, state="failed")
    selected = []
    attempts = 0

    def audit(self, **kw):
        nonlocal attempts
        attempts += 1
        if audit_fail_once and attempts == 1:
            raise RuntimeError("temporary audit provider failure")
        return FuturePlanAuditRun(
            project_id=kw["project_id"],
            current_chapter=kw["current_chapter"],
            trigger_stage=kw["trigger_stage"],
        )

    monkeypatch.setattr(FuturePlanAuditor, "audit_and_apply", audit)
    with fixture.Session.begin() as session:
        task = session.get(GenerationTask, fixture.task_id)
        frozen = payload_from_json(task.execution_payload_json)
        task.execution_payload_json = frozen.model_copy(
            update={"long_run_mode": mode}
        ).model_dump_json()
        candidate = session.get(CandidateDraftRecord, fixture.candidate_id)
        candidate.metadata_json = json.dumps({"generation_task_id": fixture.task_id})
    maintenance.order_controls_recovery = lambda commit_id: recover_order_controls(
        maintenance,
        commit_id=commit_id,
        llm_client_factory=lambda policy: (
            selected.append(policy) or SimpleNamespace(close=lambda: None)
        ),
    )
    assert not consume(fixture.Session).processed
    with fixture.Session.begin() as session:
        from forwin.models.maintenance import PostCanonMaintenanceRun

        for row in session.scalars(select(PostCanonMaintenanceRun)):
            row.available_at = None
        phase3 = session.scalar(
            select(OutboxEvent).where(OutboxEvent.event_type == CANON_PHASE3_REQUESTED)
        )
        phase3.available_at = None
    recovered = run_one_outbox_event(
        session_factory=fixture.Session,
        worker_id="phase3",
        handlers=build_post_canon_outbox_handlers(service_provider=lambda: maintenance),
    )
    if audit_fail_once:
        assert not recovered.processed
        with fixture.Session.begin() as session:
            continuation_row(session, fixture.task_id).available_at = None
        assert not consume(fixture.Session).processed
        assert decision(fixture.Session, fixture.task_id) is None
        with fixture.Session.begin() as session:
            phase3 = session.scalar(
                select(OutboxEvent).where(
                    OutboxEvent.event_type == CANON_PHASE3_REQUESTED
                )
            )
            assert phase3.status == "pending"
            phase3.available_at = None
        recovered = run_one_outbox_event(
            session_factory=fixture.Session,
            worker_id="phase3-retry",
            handlers=build_post_canon_outbox_handlers(
                service_provider=lambda: maintenance
            ),
        )
    assert recovered.processed
    assert selected == [frozen.policy_snapshot] * (2 if audit_fail_once else 1)
    with fixture.Session.begin() as session:
        phase3 = session.scalar(
            select(OutboxEvent).where(OutboxEvent.event_type == CANON_PHASE3_REQUESTED)
        )
        phase3.status = "pending"  # completion persisted but phase3 ack was lost
        phase3.available_at = None
    assert run_one_outbox_event(
        session_factory=fixture.Session,
        worker_id="phase3-replay",
        handlers=build_post_canon_outbox_handlers(service_provider=lambda: maintenance),
    ).processed
    assert attempts == (
        2 if audit_fail_once else 1
    )  # completed checks are not repeated
    with fixture.Session.begin() as session:
        continuation_row(session, fixture.task_id).available_at = None
    assert consume(fixture.Session).processed
    assert child_count(fixture.Session, fixture.task_id) == 1


@pytest.mark.parametrize(
    "provenance",
    ["missing", "unknown_task", "wrong_policy", "wrong_project", "malformed"],
)
def test_control_recovery_requires_original_policy_provenance(
    recovery_fixture, provenance
):
    from forwin.maintenance.order_controls import recover_order_controls
    from forwin.maintenance.state import (
        post_canon_control_blockers,
        post_canon_control_result,
    )
    from forwin.models.draft import CandidateDraftRecord
    from forwin.models.maintenance import PostCanonMaintenanceRun

    fixture = recovery_fixture
    maintenance, commit_id = maintenance_parent(fixture, state="controls_missing")
    with fixture.Session.begin() as session:
        candidate = session.get(CandidateDraftRecord, fixture.candidate_id)
        candidate.metadata_json = json.dumps({"generation_task_id": fixture.task_id})
        if provenance == "missing":
            candidate.metadata_json = "{}"
        elif provenance == "unknown_task":
            candidate.metadata_json = '{"generation_task_id":"absent"}'
        elif provenance == "wrong_policy":
            candidate.policy_version += 1
        elif provenance == "wrong_project":
            other = Project(
                id="other-provenance-project", title="Other", premise="Other"
            )
            session.add(other)
            session.flush()
            session.get(GenerationTask, fixture.task_id).project_id = other.id
        else:
            candidate.metadata_json = "["
    recover_order_controls(
        maintenance,
        commit_id=commit_id,
        llm_client_factory=lambda policy: pytest.fail(
            "unknown policy must not build a model client"
        ),
    )
    with fixture.Session() as session:
        rows = list(session.scalars(select(PostCanonMaintenanceRun)))
        assert post_canon_control_blockers(post_canon_control_result(rows)) == [
            "post_canon_policy_provenance_missing"
        ]


@pytest.mark.parametrize(
    "corruption",
    [
        "missing_intent",
        "processed_intent",
        "unknown_step",
        "bad_json",
        "bad_control_result",
    ],
)
def test_corrupt_maintenance_state_stops_explainably(recovery_fixture, corruption):
    from forwin.canon.outbox_events import CANON_PHASE3_REQUESTED
    from forwin.models.maintenance import PostCanonMaintenanceRun

    fixture = recovery_fixture
    maintenance_parent(
        fixture, state="missing" if corruption == "missing_intent" else "pending"
    )
    with fixture.Session.begin() as session:
        if corruption == "missing_intent":
            session.delete(
                session.scalar(
                    select(OutboxEvent).where(
                        OutboxEvent.event_type == CANON_PHASE3_REQUESTED
                    )
                )
            )
        elif corruption == "processed_intent":
            session.scalar(
                select(OutboxEvent).where(
                    OutboxEvent.event_type == CANON_PHASE3_REQUESTED
                )
            ).status = "processed"
        else:
            feedback = session.scalar(
                select(PostCanonMaintenanceRun).where(
                    PostCanonMaintenanceRun.step_name == "feedback"
                )
            )
            if corruption == "unknown_step":
                feedback.step_name = "unknown"
            elif corruption == "bad_json":
                feedback.result_json = "["
            else:
                feedback.result_json = (
                    '{"order_controls":{"status":"succeeded","result":[]}}'
                )
    assert consume(fixture.Session).processed
    assert (
        decision(fixture.Session, fixture.task_id)["reason"]
        == "maintenance_state_invalid"
    )


def test_concurrent_consumers_after_maintenance_wait_create_one_child(recovery_fixture):
    from forwin.generation.continuation_events import (
        GenerationContinuationEvent,
        consume_continuation,
    )

    fixture = recovery_fixture
    maintenance, commit_id = maintenance_parent(fixture)
    assert not consume(fixture.Session).processed
    maintenance.run(canon_commit_id=commit_id, worker_id="maintenance")
    maintenance.run_order_controls(
        canon_commit_id=commit_id, runner=lambda session, commit: {}
    )
    with fixture.Session() as session:
        event = GenerationContinuationEvent.model_validate_json(
            continuation_row(session, fixture.task_id).payload_json
        )

    def replay():
        with fixture.Session.begin() as session:
            return consume_continuation(session, event, _service(fixture.Session))

    with ThreadPoolExecutor(max_workers=2) as pool:
        answers = list(pool.map(lambda _: replay(), range(2)))
    assert answers[0] == answers[1]
    assert child_count(fixture.Session, fixture.task_id) == 1


def test_completed_phase3_rereads_stale_maintenance_snapshot(recovery_fixture):
    from forwin.canon.outbox_events import CANON_PHASE3_REQUESTED
    from forwin.maintenance.state import post_canon_continuation_state
    from forwin.models.canon import CanonCommitRecord
    from forwin.models.maintenance import PostCanonMaintenanceRun

    fixture = recovery_fixture
    maintenance, commit_id = maintenance_parent(fixture)
    with fixture.Session() as stale_session:
        rows = list(stale_session.scalars(select(PostCanonMaintenanceRun)))
        assert all(row.status == "pending" for row in rows)
        commit = stale_session.get(CanonCommitRecord, commit_id)
        maintenance.run(canon_commit_id=commit_id, worker_id="maintenance")
        maintenance.run_order_controls(
            canon_commit_id=commit_id, runner=lambda session, commit: {}
        )
        with fixture.Session.begin() as session:
            session.scalar(
                select(OutboxEvent).where(
                    OutboxEvent.event_type == CANON_PHASE3_REQUESTED
                )
            ).status = "processed"
        assert (
            post_canon_continuation_state(rows, session=stale_session, commit=commit)
            == "ready"
        )
