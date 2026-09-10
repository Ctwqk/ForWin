from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

from sqlalchemy import event, select

from forwin.audience.actions import ActionMapper
from forwin.audience.feedback import FeedbackCooldown
from forwin.models import FeedbackActionRecord
from forwin.observability.pipeline_trace import (
    PipelineAuditContext,
    PipelineTraceRecorder,
)
from forwin.state.updater import StateUpdater
from forwin.writer.chapter_writer import ChapterWriter
from tests import test_comment_consumption_contract as comments
from tests.test_feedback_action_lifecycle import aggregate_view
from tests.test_feedback_prompt_evidence import CapturingModel, hint_context

comments_runtime = comments.comments_runtime


def test_prompt_evidence_locks_project_before_trace_fk_write(comments_runtime):
    runtime, project_id = comments_runtime
    mapper = ActionMapper()
    with runtime.session_factory() as session:
        records = mapper.record_actions(
            session,
            project_id=project_id,
            chapter_number=4,
            actions=mapper.map_actions([aggregate_view(project_id=project_id)]),
            cooldown=FeedbackCooldown(),
        )
        hints = mapper.select_actions(
            session,
            project_id=project_id,
            chapter_number=4,
            action_ids=[row.id for row in records],
            cooldown=FeedbackCooldown(),
        )
        session.commit()
        output = ChapterWriter(
            CapturingModel(), writer_mode="single", min_chapter_chars=200
        ).write_chapter(
            hint_context().model_copy(
                update={"project_id": project_id, "audience_hints": hints}
            )
        )
        statements = []

        def observe(_conn, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement.lower())

        event.listen(session.bind, "before_cursor_execute", observe)
        try:
            recorder = PipelineTraceRecorder(
                audit=PipelineAuditContext(),
                artifact_store=None,
                observability=SimpleNamespace(),
            )
            recorder.save_prompt_trace(
                session=session,
                updater=StateUpdater(session),
                project_id=project_id,
                prompt_trace=output.generation_meta["prompt_trace"],
            )
        finally:
            event.remove(session.bind, "before_cursor_execute", observe)
        project_lock = next(
            index
            for index, sql in enumerate(statements)
            if "projects" in sql and "for update" in sql
        )
        trace_write = next(
            index
            for index, sql in enumerate(statements)
            if "insert into prompt_traces" in sql
        )
        assert project_lock < trace_write


def test_real_concurrent_selection_starts_only_one_cooldown(comments_runtime):
    runtime, project_id = comments_runtime
    barrier = Barrier(2)

    def choose(number):
        mapper = ActionMapper()
        view = aggregate_view(project_id=project_id) | {
            "aggregate_id": f"aggregate-{number}"
        }
        barrier.wait(timeout=5)
        with runtime.session_factory.begin() as session:
            records = mapper.record_actions(
                session,
                project_id=project_id,
                chapter_number=4,
                actions=mapper.map_actions([view]),
                cooldown=FeedbackCooldown(),
            )
            mapper.select_actions(
                session,
                project_id=project_id,
                chapter_number=4,
                action_ids=[row.id for row in records],
                cooldown=FeedbackCooldown(),
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(choose, number) for number in range(2)]
        for future in futures:
            future.result(timeout=10)
    with runtime.session_factory() as session:
        rows = list(
            session.scalars(
                select(FeedbackActionRecord).where(
                    FeedbackActionRecord.project_id == project_id
                )
            )
        )
        assert sorted(row.status for row in rows) == ["proposed", "selected"]
        assert sorted(row.cooldown_until_chapter for row in rows) == [0, 7]
