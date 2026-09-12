"""The generation Session passes live hydration authority through the real Writer owner."""

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from forwin.models import Project
from forwin.models.base import get_session_factory
from forwin.models.draft import CandidateDraftRecord, ChapterDraft
from forwin.models.project import ChapterPlan
from forwin.protocol.review import ReviewVerdict
from forwin.protocol.scene import ScenePlan
from forwin.retrieval.requirements import RequiredContextError
from forwin.retrieval.source_identity import CanonBaselineChanged
from tests.postgres import postgres_test_url
from tests.test_canon_repair_stage import (
    _build_pipeline,
    _isolate_canon_repair_from_hard_floor,  # noqa: F401 - autouse fixture
    _one_chapter_arc,
    _writer_output,
)
from tests.test_writer_execution_owner import _terminal_input_error


@pytest.mark.parametrize(
    "kind", ["success", "input_limit", "required_context", "canon_baseline_changed"]
)
def test_generation_pipeline_preserves_terminal_failure_and_session_authority(kind):
    pipeline = _build_pipeline(postgres_test_url())
    pipeline.arc_director.plan_arc = lambda *_: _one_chapter_arc("live input")
    pipeline.candidate_review.draft_review = SimpleNamespace(
        review=lambda **_: ReviewVerdict(verdict="pass", issues=[])
    )
    calls, captured, results, failures = [], [], [], []
    execute = pipeline.writer_execution.execute

    def capture(request):
        captured.append(request)
        result = execute(request)
        results.append(result)
        return result

    pipeline.writer_execution = SimpleNamespace(execute=capture)

    def write(context, **_):
        calls.append("main")
        request = captured[-1]
        session = request.updater.session
        assert session.get_transaction().is_active
        copied = context.model_copy(deep=True)
        hydrated = copied.required_context_hydrator(copied, [])
        assert hydrated.canon_read_baseline == context.canon_read_baseline
        assert hydrated.accepted_cognition == context.accepted_cognition
        try:
            if kind == "canon_baseline_changed":
                project = session.get(Project, context.project_id)
                project.book_revision += 1
                session.flush()
                hydrated.required_context_hydrator(hydrated, [])
            elif kind == "required_context":
                hydrated.required_context_hydrator(
                    hydrated,
                    [
                        ScenePlan(
                            scene_no=1,
                            objective="verify",
                            involved_entities=["node:missing"],
                        )
                    ],
                )
            elif kind == "input_limit":
                raise _terminal_input_error(kind)
        except Exception as exc:
            failures.append(exc)
            raise
        return _writer_output(context.chapter_number, marker="live authority")

    pipeline.writer.write_chapter = write
    pipeline.writer.write_preview_chapter = lambda *_a, **_k: (
        calls.append("preview") or _writer_output(1)
    )
    try:
        run = pipeline.run("p", "g", 1)
        assert calls == ["main"]
        assert len(results) == 1
        original = captured[0].context
        with pytest.raises(RequiredContextError, match="reassembl"):
            original.required_context_hydrator(original, [])
        if kind == "success":
            assert results[0].output is not None
            assert results[0].error is None
        else:
            assert run.status == "failed"
            assert results[0].output is None
            assert results[0].error is failures[0]
            expected = (
                CanonBaselineChanged
                if kind == "canon_baseline_changed"
                else RequiredContextError
                if kind == "required_context"
                else type(_terminal_input_error(kind))
            )
            assert isinstance(results[0].error, expected)
            with pytest.raises(expected) as raised:
                results[0].unwrap()
            assert raised.value is failures[0]
            with get_session_factory(pipeline.engine)() as session:
                assert session.scalars(select(ChapterDraft)).all() == []
                assert session.scalars(select(CandidateDraftRecord)).all() == []
                chapter = session.scalar(select(ChapterPlan))
                assert chapter.status == "failed"
                assert chapter.active_commit_id is None
    finally:
        pipeline.engine.dispose()
