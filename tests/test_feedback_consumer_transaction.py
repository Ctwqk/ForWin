from __future__ import annotations

import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from forwin.maintenance.post_canon import PostCanonMaintenanceService
from forwin.models.canon import CanonCommitRecord
from forwin.models.outbox import OutboxEvent
from forwin.models.project import Project
from forwin.models.publisher import CommentAnalysisRecord
from tests.test_comment_consumption_contract import EmptyAnalyzerModel, _comment
from tests.test_comment_source_identity import _published_chapter

pytest_plugins = ["tests.test_comment_consumption_contract"]


class TracedCommentModel(EmptyAnalyzerModel):
    def __init__(self, *, fail=False):
        super().__init__()
        self.fail = fail
        self.events = []

    def chat(self, messages, **kwargs):
        self.events.append({"stage_key": "comment_analysis", "total_tokens": 7})
        result = super().chat(messages, **kwargs)
        if self.fail:
            raise TimeoutError("bounded comment model failure")
        return result

    def drain_llm_attempt_events(self):
        events, self.events = self.events, []
        return events


def _service(runtime, model):
    return PostCanonMaintenanceService(
        session_factory=runtime.session_factory,
        stage_analyzer=object(),
        pacing_strategist=object(),
        replan_governor=object(),
        arc_envelope_manager=object(),
        world_simulator=object(),
        artifact_store=object(),
        llm_client=model,
    )


def test_comment_completion_and_usage_survive_later_feedback_rollback(
    comments_runtime,
    monkeypatch,
):
    runtime, project_id = comments_runtime
    _, _, commit_id, _ = _published_chapter(runtime, project_id)
    with runtime.session_factory.begin() as session:
        _comment(session, project_id, 1)
    model = TracedCommentModel()
    service = _service(runtime, model)

    def fail_aggregation(*args, **kwargs):
        raise RuntimeError("later aggregate failure")

    monkeypatch.setattr(
        "forwin.maintenance.post_canon.run_feedback_aggregation_pass", fail_aggregation
    )
    for _ in range(2):
        with (
            pytest.raises(RuntimeError, match="later aggregate failure"),
            runtime.session_factory.begin() as session,
        ):
            session.get(Project, project_id).premise = "must rollback"
            service._run_feedback_step(
                session, session.get(CanonCommitRecord, commit_id)
            )

    with runtime.session_factory() as session:
        analysis = session.scalars(select(CommentAnalysisRecord)).one()
        assert analysis.status == "completed" and analysis.signal_count == 0
        assert analysis.attempt_count == 1
        assert session.get(Project, project_id).premise == "测试"
        traces = session.scalars(select(OutboxEvent)).all()
        assert len(traces) == 1
        trace = json.loads(json.loads(traces[0].payload_json)["content"])
        assert trace["attempts"][0]["total_tokens"] == 7
        assert trace["step_name"] == "feedback"
    assert len(model.bodies) == 1


def test_comment_failures_exhaust_even_if_later_consumer_rolls_back(
    comments_runtime,
    monkeypatch,
):
    runtime, project_id = comments_runtime
    _, _, commit_id, _ = _published_chapter(runtime, project_id)
    with runtime.session_factory.begin() as session:
        _comment(session, project_id, 1)
    model = TracedCommentModel(fail=True)
    service = _service(runtime, model)

    def fail_aggregation(*args, **kwargs):
        raise RuntimeError("later aggregate failure")

    monkeypatch.setattr(
        "forwin.maintenance.post_canon.run_feedback_aggregation_pass", fail_aggregation
    )
    for attempt in range(4):
        with (
            pytest.raises(RuntimeError, match="later aggregate failure"),
            runtime.session_factory.begin() as session,
        ):
            service._run_feedback_step(
                session, session.get(CanonCommitRecord, commit_id)
            )
        with runtime.session_factory.begin() as session:
            record = session.scalars(select(CommentAnalysisRecord)).one()
            assert record.attempt_count == min(attempt + 1, 3)
            if record.next_retry_at:
                record.next_retry_at -= timedelta(minutes=2)
    with runtime.session_factory() as session:
        record = session.scalars(select(CommentAnalysisRecord)).one()
        assert record.status == "exhausted"
        assert "TimeoutError" in record.last_error
        assert len(session.scalars(select(OutboxEvent)).all()) == 3
    assert len(model.bodies) == 3


def test_no_comments_do_not_call_model_or_add_trace(comments_runtime):
    runtime, project_id = comments_runtime
    _, _, commit_id, _ = _published_chapter(runtime, project_id)
    model = TracedCommentModel()
    service = _service(runtime, model)
    with runtime.session_factory.begin() as session:
        result = service._run_feedback_step(
            session, session.get(CanonCommitRecord, commit_id)
        )
    assert model.bodies == []
    assert result["analysis"]["selected_count"] == 0
    with runtime.session_factory() as session:
        assert session.scalars(select(OutboxEvent)).all() == []
