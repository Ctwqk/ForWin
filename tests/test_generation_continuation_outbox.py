"""PostgreSQL failure boundaries for the application/worker continuation handoff."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
import json
import logging

import pytest
from sqlalchemy import event, select

from forwin.application.errors import GenerationTaskLeaseLost
from forwin.application.generation_execution import (
    execute_pipeline_task,
    _build_task_progress_changes,
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
from tests.test_generation_application_service import (
    session_factory as session_factory,
    _create_project,
    _command,
    _service,
)


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
        consume_continuation,
        GenerationContinuationEvent,
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
    from forwin.http.project_support import _mutate_generation_task
    from forwin.generation.continuation_events import (
        consume_continuation,
        GenerationContinuationEvent,
    )

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
    service, task_id, epoch, result = setup_parent(session_factory)
    with pytest.raises(ValueError, match="finalization"):
        service._task_updater(worker_id="worker", lease_epoch=epoch)(
            task_id, status="completed"
        )
    with session_factory() as session:
        assert session.get(GenerationTask, task_id).status == "running"


from tests.test_generation_worker_canon_recovery import (
    recovery_fixture as recovery_fixture,
)


@pytest.mark.parametrize("daily", [False, True])
def test_superseded_maintenance_cannot_block_current_ready_continuation(
    recovery_fixture, daily
):
    from forwin.canon.admission import CanonAdmissionService
    from forwin.models.canon import CanonCommitRecord
    from forwin.models.maintenance import PostCanonMaintenanceRun
    from forwin.generation.continuation_events import (
        consume_continuation,
        GenerationContinuationEvent,
    )

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

    service, task_id, epoch, result = setup_parent(session_factory)
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
    from forwin.runtime.container import RuntimeContainer
    from forwin.config import InfrastructureConfig

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
    from sqlalchemy import create_engine, MetaData, Table, text
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
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
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
    service, task_id, epoch, result = setup_parent(session_factory)

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
