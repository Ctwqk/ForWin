from __future__ import annotations

import importlib
import importlib.util
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from forwin.audit.events import DecisionEventType
from forwin.candidate_drafts import CandidateDraftRepository, candidate_plan_revision
from forwin.models.audit import DecisionEvent
from forwin.models.base import Base
from forwin.models.canon_quality import QualityAnalysisRunRow
from forwin.models.draft import CandidateDraftRecord, ChapterDraft, ChapterReview
from forwin.models.genesis import PromptTrace
from forwin.models.project import Project
from forwin.observability.pipeline_trace import (
    PipelineAuditContext,
    PipelineTraceRecorder,
)
from forwin.protocol.review import ReviewVerdict
from forwin.protocol.writer import WriterOutput
from forwin.runtime.policy import RuntimePolicy
from forwin.state.updater import StateUpdater
from forwin.storage import ArtifactStore


def _quality_owner():
    assert importlib.util.find_spec("forwin.canon.quality_preparation") is not None, (
        "Canon quality preparation must run without a Pipeline or callback context"
    )
    return importlib.import_module("forwin.canon.quality_preparation")


@pytest.fixture
def quality_case(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Project(id="book", title="Book", premise="An archive mystery"))
        session.commit()
        store = ArtifactStore(str(tmp_path))
        recorder = PipelineTraceRecorder(
            audit=PipelineAuditContext(task_id="task", root_event_id="root"),
            artifact_store=store,
            observability=SimpleNamespace(_record_span=lambda _span: None),
        )
        yield SimpleNamespace(
            session=session,
            updater=StateUpdater(session),
            store=store,
            recorder=recorder,
            output=WriterOutput(
                project_id="book",
                chapter_number=1,
                title="Archive",
                body="The archivist checked the register and closed the archive.",
                end_of_chapter_summary="The archive was closed.",
            ),
        )
    engine.dispose()


def _evaluate(module, case, *, policy=None, llm_client=None):
    return module.CanonQualityPreparer().evaluate(
        session=case.session,
        updater=case.updater,
        policy=policy or RuntimePolicy.for_profile("pulp"),
        llm_client=llm_client,
        artifact_store=case.store,
        recorder=case.recorder,
        project_id="book",
        chapter_number=1,
        writer_output=case.output,
        verdict=ReviewVerdict(verdict="pass"),
        candidate_id="candidate",
        policy_version=7,
    )


def test_quality_owner_uses_real_gate_and_transaction_bound_audit(quality_case):
    module = _quality_owner()
    outcome = _evaluate(module, quality_case)
    assert not outcome.blocked
    assert outcome.quality_admission_run_id
    events = list(quality_case.session.scalars(select(DecisionEvent)))
    gate_event = next(
        e for e in events if e.event_type == DecisionEventType.REVIEW_VERDICT_RECORDED
    )
    assert gate_event.task_id == "task"
    assert gate_event.causal_root_id == "root"
    gate = json.loads(gate_event.payload_json)["gate_outcome"]
    assert gate["candidate_id"] == "candidate"
    assert gate["policy_version"] == 7
    quality_case.session.rollback()
    assert list(quality_case.session.scalars(select(DecisionEvent))) == []


@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt])
def test_analysis_original_error_still_records_same_transaction_trace(
    quality_case,
    monkeypatch,
    error_type,
):
    module = _quality_owner()
    error = error_type("analysis failed")

    class AttemptClient:
        count = 0

        def drain_llm_attempt_events(self):
            self.count += 1
            return [
                {
                    "attempt_no": 1,
                    "model": "actual-model",
                    "stage_key": "chapter_review_form",
                    "error_class": error_type.__name__,
                }
            ]

    client = AttemptClient()

    def analyze(**kwargs):
        assert kwargs["persist"] is True
        assert kwargs["return_raw_analyzer_results"] is True
        assert kwargs["mode"] == "primary"
        assert kwargs["llm_client"] is client
        raise error

    monkeypatch.setattr(module, "analyze_writer_output_quality", analyze)
    with pytest.raises(error_type) as caught:
        _evaluate(
            module,
            quality_case,
            policy=RuntimePolicy.for_profile("standard"),
            llm_client=client,
        )
    assert caught.value is error
    assert client.count == 1
    trace = quality_case.session.scalar(select(PromptTrace))
    assert trace is not None
    assert trace.trace_scope == "canon_quality"
    assert trace.stage_key == "chapter_review_form"
    assert json.loads(trace.attempts_json)[0]["model"] == "actual-model"
    assert json.loads(trace.output_summary_json)["error_class"] == error_type.__name__
    events = list(quality_case.session.scalars(select(DecisionEvent)))
    assert events
    assert {e.task_id for e in events} == {"task"}
    assert {e.causal_root_id for e in events} == {"root"}
    quality_case.session.rollback()
    assert list(quality_case.session.scalars(select(PromptTrace))) == []
    assert list(quality_case.session.scalars(select(DecisionEvent))) == []


def test_trace_save_failure_cannot_replace_analysis_error(quality_case, monkeypatch):
    module = _quality_owner()
    primary = RuntimeError("primary analysis failure")

    def analyze(**_kwargs):
        raise primary

    def save(**_kwargs):
        raise OSError("trace storage failure")

    monkeypatch.setattr(module, "analyze_writer_output_quality", analyze)
    monkeypatch.setattr(quality_case.recorder, "save_prompt_trace", save)
    client = SimpleNamespace(drain_llm_attempt_events=lambda: [{"attempt_no": 1}])
    with pytest.raises(RuntimeError) as caught:
        _evaluate(
            module,
            quality_case,
            policy=RuntimePolicy.for_profile("standard"),
            llm_client=client,
        )
    assert caught.value is primary


def test_pulp_quality_does_not_call_or_drain_model(quality_case):
    module = _quality_owner()

    class ForbiddenClient:
        def drain_llm_attempt_events(self):
            pytest.fail("deterministic Canon quality must not drain the model")

        def __getattr__(self, name):
            pytest.fail(f"deterministic Canon quality accessed model method {name}")

    result = _evaluate(module, quality_case, llm_client=ForbiddenClient())
    assert not result.blocked
    assert list(quality_case.session.scalars(select(PromptTrace))) == []


def test_quality_owner_reuses_existing_analysis_cache(quality_case):
    module = _quality_owner()
    first = _evaluate(module, quality_case)
    cached = list(quality_case.session.scalars(select(QualityAnalysisRunRow)))
    assert len(cached) == 1
    second = _evaluate(module, quality_case)
    assert not first.blocked and not second.blocked
    assert first.quality_admission_run_id != second.quality_admission_run_id
    assert [
        row.id for row in quality_case.session.scalars(select(QualityAnalysisRunRow))
    ] == [cached[0].id]


@pytest.mark.parametrize("artifact_failure", [False, True])
def test_quality_block_retains_frozen_evidence_or_original_artifact_failure(
    quality_case,
    monkeypatch,
    artifact_failure,
):
    from forwin.canon_quality.signals import CanonQualitySignal
    from forwin.canon_quality.types import CanonQualityAnalysisResult

    module = _quality_owner()
    signal = CanonQualitySignal(
        signal_id="countdown-signal",
        project_id="book",
        chapter_number=1,
        signal_type="countdown_non_monotonic",
        severity="error",
        description="Countdown increased",
        evidence_refs=["body:register"],
    )
    analysis = CanonQualityAnalysisResult(
        project_id="book", chapter_number=1, signals=[signal]
    )
    monkeypatch.setattr(
        module, "analyze_writer_output_quality", lambda **_kwargs: analysis
    )
    error = OSError("frozen artifact unavailable")
    if artifact_failure:

        def fail(**_kwargs):
            raise error

        monkeypatch.setattr(quality_case.store, "save_frozen_candidate", fail)
        with pytest.raises(OSError) as caught:
            _evaluate(module, quality_case)
        assert caught.value is error
    else:
        outcome = _evaluate(module, quality_case)
        assert outcome.blocked
        assert outcome.gate_result.commit_allowed is False
        payload = json.loads(
            quality_case.store.object_store.read_text(outcome.blocked_path)
        )
        assert payload["writer_output"]["body"] == quality_case.output.body
        assert payload["canon_quality_signals"][0]["signal_id"] == "countdown-signal"
        assert payload["canon_quality_gate"]["commit_allowed"] is False


def _request_case(case, *, verdict="pass"):
    module = importlib.import_module("forwin.canon.preparation")
    request_type = getattr(module, "CanonPreparationRequest", None)
    assert request_type is not None, (
        "preparation must accept a finite request, without Pipeline callbacks"
    )
    arc = case.updater.create_arc_plan("book", "Arc")
    chapter = case.updater.create_chapter_plan(
        project_id="book",
        arc_plan_id=arc.id,
        chapter_number=1,
        title="Archive",
        one_line="A register was checked",
        goals=[],
    )
    draft = ChapterDraft(
        chapter_plan_id=chapter.id, version=1, body_text=case.output.body
    )
    case.session.add(draft)
    case.session.flush()
    review = ChapterReview(draft_id=draft.id, verdict=verdict)
    case.session.add(review)
    case.session.flush()
    candidate = CandidateDraftRepository(case.session).create_reviewed_version(
        project_id="book",
        chapter_plan=chapter,
        draft=draft,
        review=review,
        writer_output=case.output,
        plan_revision=candidate_plan_revision(chapter),
        policy_version=1,
    )
    case.session.commit()
    return module, request_type(
        candidate_id=candidate.id,
        project_id="book",
        chapter_number=1,
        writer_output=case.output,
        verdict=ReviewVerdict(verdict=verdict),
        acceptance_mode="normal",
        repair_attempt_count=2,
        residual_review_issues=[],
        canon_risk_level="low",
    )


def _prepare(module, request, case, **dependencies):
    return module.CanonPreparationService(**dependencies).prepare(
        request=request,
        session=case.session,
        updater=case.updater,
        policy=RuntimePolicy.for_profile("standard"),
        llm_client=None,
        artifact_store=case.store,
        recorder=case.recorder,
    )


@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt])
def test_preparation_preserves_exception_classification_and_caller_rollback(
    quality_case,
    error_type,
):
    module, request = _request_case(quality_case)
    error = error_type("quality unavailable")

    class FailingQuality:
        def evaluate(self, **_kwargs):
            raise error

    if error_type is KeyboardInterrupt:
        with pytest.raises(KeyboardInterrupt) as caught:
            _prepare(module, request, quality_case, quality_preparer=FailingQuality())
        assert caught.value is error
        assert (
            quality_case.session.get(CandidateDraftRecord, request.candidate_id).status
            == "reviewed"
        )
    else:
        result = _prepare(
            module, request, quality_case, quality_preparer=FailingQuality()
        )
        assert result.block_kind == "canon_preparation_error"
        assert result.blocked_path == "quality unavailable"
        assert (
            quality_case.session.get(CandidateDraftRecord, request.candidate_id).status
            == "failed"
        )
    quality_case.session.rollback()
    assert (
        quality_case.session.get(CandidateDraftRecord, request.candidate_id).status
        == "reviewed"
    )
    assert list(quality_case.session.scalars(select(DecisionEvent))) == []


def test_ineligible_request_does_not_enter_quality_or_book_state(quality_case):
    module, request = _request_case(quality_case, verdict="fail")

    class Forbidden:
        def evaluate(self, **_kwargs):
            pytest.fail("ineligible candidate reached quality analysis")

        def prepare(self, **_kwargs):
            pytest.fail("ineligible candidate reached BookState extraction")

    result = _prepare(
        module,
        request,
        quality_case,
        quality_preparer=Forbidden(),
        book_state_preparer=Forbidden(),
    )
    assert result.block_kind == "candidate_ineligible"
    assert (
        quality_case.session.get(CandidateDraftRecord, request.candidate_id).status
        == "needs_review"
    )


def test_preparation_start_event_failure_propagates_before_quality(
    quality_case,
    monkeypatch,
):
    module, request = _request_case(quality_case)
    error = OSError("event store unavailable")

    class ForbiddenQuality:
        def evaluate(self, **_kwargs):
            pytest.fail("preparation continued after its initial event failed")

    def fail_event(**_kwargs):
        raise error

    monkeypatch.setattr(quality_case.recorder, "record_event", fail_event)
    with pytest.raises(OSError) as caught:
        _prepare(module, request, quality_case, quality_preparer=ForbiddenQuality())
    assert caught.value is error
    assert (
        quality_case.session.get(CandidateDraftRecord, request.candidate_id).status
        == "reviewed"
    )


def test_quality_event_failure_still_becomes_audited_preparation_failure(
    quality_case,
    monkeypatch,
):
    module, request = _request_case(quality_case)
    record_event = quality_case.recorder.record_event

    def fail_quality_event(**kwargs):
        if kwargs["event_type"] == DecisionEventType.REVIEW_VERDICT_RECORDED:
            raise OSError("quality event unavailable")
        return record_event(**kwargs)

    monkeypatch.setattr(quality_case.recorder, "record_event", fail_quality_event)
    result = module.CanonPreparationService().prepare(
        request=request,
        session=quality_case.session,
        updater=quality_case.updater,
        policy=RuntimePolicy.for_profile("pulp"),
        llm_client=None,
        artifact_store=quality_case.store,
        recorder=quality_case.recorder,
    )
    assert result.block_kind == "canon_preparation_error"
    assert result.blocked_path == "quality event unavailable"
    assert (
        quality_case.session.get(CandidateDraftRecord, request.candidate_id).status
        == "failed"
    )
    blocked = quality_case.session.scalar(
        select(DecisionEvent).where(
            DecisionEvent.event_type == DecisionEventType.CANON_COMMIT_BLOCKED
        )
    )
    assert blocked is not None
    assert json.loads(blocked.payload_json)["gate_outcome"]["decision"] == "error"
