"""Execution failures do not create content evidence or spend its repair budget."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from forwin.audit.events import DecisionEventType
from forwin.book_state import BookStateRepository
from forwin.canon import quality_preparation
from forwin.canon_quality.types import CanonQualityAnalysisResult
from forwin.models.audit import DecisionEvent
from forwin.models.base import get_session_factory
from forwin.models.draft import CandidateDraftRecord, ChapterDraft
from forwin.models.narrative_obligation import NarrativeObligationRow
from forwin.models.phase import ChapterRewriteAttempt
from forwin.models.project import ChapterPlan
from forwin.narrative_obligations.repository import NarrativeObligationRepository
from forwin.protocol.book_state import WorldNode
from forwin.protocol.review import ReviewVerdict
from forwin.writer.execution import WriterExecutionResult
from tests.postgres import postgres_test_url
from tests.test_canon_repair_stage import (
    _build_pipeline,
    _draft_blocking_review,
    _isolate_canon_repair_from_hard_floor,  # noqa: F401 - pytest autouse fixture
    _one_chapter_arc,
    _writer_output,
)
from tests.test_obligation_bounded_repair import _obligation, _patch


@pytest.mark.parametrize("prior_content_attempts", [0, 1])
@pytest.mark.parametrize("failure_kind", ["transport", "format", "empty", "semantic"])
def test_repair_execution_failure_retains_budget_and_diagnostic_across_new_run(
    monkeypatch, failure_kind, prior_content_attempts
):
    database_url = postgres_test_url("obligation-repair-execution-failure")
    pipeline = _build_pipeline(database_url, max_rewrites=2)
    pipeline.arc_director.plan_arc = lambda *_: _one_chapter_arc("repair execution")
    initial_bodies = []

    def write(context):
        output = _writer_output(
            context.chapter_number, marker=f"initial-{len(initial_bodies)}"
        )
        initial_bodies.append(output.body)
        return output

    pipeline.writer.write_chapter = write
    review_calls = []

    def review(**kwargs):
        review_calls.append(kwargs["writer_output"].body)
        if prior_content_attempts and len(review_calls) == 1:
            return _draft_blocking_review()
        return ReviewVerdict(verdict="pass", issues=[])

    pipeline.candidate_review.draft_review = SimpleNamespace(review=review)

    repair_calls = []
    error = (
        TimeoutError("provider 429 rate limit")
        if failure_kind == "transport"
        else ValueError("invalid JSON schema response")
    )

    def execute(request):
        repair_calls.append(request)
        if (
            prior_content_attempts and len(repair_calls) == 1
        ) or failure_kind == "semantic":
            return WriterExecutionResult(
                output=_writer_output(1, marker=f"still-unknown-{len(repair_calls)}")
            )
        # Prove saving the diagnostic cannot commit partial work made by the
        # failing executor. The existing chapter failure owner must roll it back.
        chapter = request.updater.session.scalar(select(ChapterPlan))
        chapter.title = "uncommitted failed execution"
        request.updater.session.flush()
        if failure_kind == "empty":
            return WriterExecutionResult()
        return WriterExecutionResult(error=error)

    pipeline.repair_execution = replace(
        pipeline.repair_execution, writer_execution=SimpleNamespace(execute=execute)
    )
    analyzed_drafts = []

    def analyze(**kwargs):
        if not analyzed_drafts:
            # The persisted obligation references an existing object, including
            # when the next run assembles its required accepted context.
            BookStateRepository(kwargs["session"]).create_world_node(
                WorldNode(
                    id="copper-key",
                    project_id=kwargs["project_id"],
                    node_type="item",
                    name="铜钥匙",
                )
            )
            repo = NarrativeObligationRepository(kwargs["session"])
            repo.create_obligation(_obligation(kwargs["project_id"]))
            repo.create_plan_patch(_patch(kwargs["project_id"]))
        analyzed_drafts.append(kwargs["draft_id"])
        # No content signal, form failure or resolution claim: the real quality
        # gate must independently request repair of the due obligation.
        return CanonQualityAnalysisResult(
            project_id=kwargs["project_id"],
            chapter_number=1,
            draft_id=kwargs["draft_id"],
            signals=[],
        )

    monkeypatch.setattr(quality_preparation, "analyze_writer_output_quality", analyze)
    Session = get_session_factory(pipeline.engine)
    try:
        first = pipeline.run("p", "g", 1)
        if failure_kind == "semantic":
            assert first.status == "needs_review"
            assert len(repair_calls) == 2
            with Session() as session:
                assert session.scalar(select(ChapterPlan)).repair_attempt_count == 2
                assert len(session.scalars(select(ChapterRewriteAttempt)).all()) == 2
                assert session.get(NarrativeObligationRow, "promise").status == "active"
            return

        assert first.status == "failed"
        assert first.failed_chapters == [1]
        assert len(repair_calls) == prior_content_attempts + 1
        for run_number in [1, 2]:
            if run_number == 2:
                # Fresh continue invocation/session generates a new candidate;
                # neither a prior failure nor that candidate changes spent count.
                # Isolate pre-write future-plan model validation, which would
                # otherwise stop at the existing due debt before Writer runs.
                pipeline._audit_current_plan_before_write = lambda **kw: kw["context"]
                second = pipeline.continue_project(first.project_id, max_chapters=1)
                assert second.status == "failed"
                assert len(repair_calls) == prior_content_attempts + 2
            with Session() as session:
                chapter = session.scalar(select(ChapterPlan))
                candidates = session.scalars(
                    select(CandidateDraftRecord).order_by(CandidateDraftRecord.version)
                ).all()
                drafts = session.scalars(
                    select(ChapterDraft).order_by(ChapterDraft.version)
                ).all()
                attempts = session.scalars(select(ChapterRewriteAttempt)).all()
                assert chapter.repair_attempt_count == prior_content_attempts
                assert chapter.title != "uncommitted failed execution"
                assert len(attempts) == prior_content_attempts
                assert (
                    len(candidates)
                    == len(drafts)
                    == prior_content_attempts + run_number
                )
                assert candidates[-1].repair_attempt_count == prior_content_attempts
                assert all(not item.canon_commit_id for item in candidates)
                assert all(
                    not json.loads(item.canon_commit_plan_json or "{}")
                    for item in candidates
                )
                assert not chapter.active_commit_id
                debt = session.get(NarrativeObligationRow, "promise")
                assert (debt.status, debt.deadline_chapter, debt.waive_reason) == (
                    "active",
                    1,
                    "",
                )
                events = session.scalars(
                    select(DecisionEvent)
                    .where(DecisionEvent.event_type == DecisionEventType.REPAIR_FAILED)
                    .order_by(DecisionEvent.created_at, DecisionEvent.id)
                ).all()
                diagnostics = [json.loads(event.payload_json) for event in events]
                assert len(diagnostics) == run_number
                diagnostic = diagnostics[-1]
                assert diagnostic["failure_domain"] == "infrastructure"
                assert diagnostic["content_rewrite_spent"] is False
                assert diagnostic["repair_attempt_count"] == prior_content_attempts
                assert (
                    diagnostic["source_draft_id"] == candidates[-1].candidate_draft_id
                )
                assert (
                    diagnostic["error_category"]
                    == {
                        "transport": "rate_limit",
                        "format": "parse_or_schema",
                        "empty": "unknown",
                    }[failure_kind]
                )
                assert events[-1].reason == (
                    "writer-returned-none" if failure_kind == "empty" else str(error)
                )
                assert not any(attempt.forced_accept_applied for attempt in attempts)
    finally:
        pipeline.llm_client.close()
        pipeline.engine.dispose()
