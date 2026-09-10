"""Independent boundary review; model fixtures exercise control flow, not semantics."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from forwin.canon.revision_evaluator import historical_prefix_context
from forwin.canon.revision_service import (
    RevisionValidationService,
    save_revision_proposal,
)
from forwin.canon_quality.chapter_review_form.service import ChapterReviewFormResult
from forwin.canon_quality.repository import CanonQualityRepository
from forwin.canon_quality.signals import CanonQualitySignal
from forwin.models.canon import CanonRevisionValidationRecord
from forwin.runtime.policy import RuntimePolicy
from forwin.writer import ChapterWriter
from tests import test_canon_atomic_transaction as atomic
from tests import test_canon_quality_acceptance_evidence as quality
from tests.test_revision_full_suffix import BodyModel, _book

prepared_canon = atomic.prepared_canon
quality_session = quality.quality_session


def test_historical_prefix_does_not_cap_snapshot_signals_by_raw_table_row_count(
    quality_session,
):
    session = quality_session
    signals = [
        CanonQualitySignal(
            signal_id=f"fresh-{number}",
            project_id="book",
            chapter_number=1,
            signal_type="review_warning",
            severity="warning",
            target_scope="chapter",
            description="Open concern",
            payload={"draft_id": "draft-1"},
        )
        for number in range(25)
    ]
    form = ChapterReviewFormResult(
        project_id="book", chapter_number=1, draft_id="draft-1", signals=signals
    )
    CanonQualityRepository(session).activate_candidate_projection(
        project_id="book",
        chapter_number=1,
        draft_id="draft-1",
        body_sha256=hashlib.sha256(b"Body 1").hexdigest(),
        historical_form=form,
        projection_id="reviewed-1",
    )
    prefix = historical_prefix_context(session, project_id="book", chapter_number=2)
    assert prefix["complete"] is True
    assert len(prefix["open_signal_rows"]) == 25


@pytest.mark.parametrize(
    "problem,expected", [("form_timeout", "unknown"), ("late_graph_failure", "fail")]
)
def test_revision_preserves_known_failure_and_does_not_turn_missing_form_into_failure(
    prepared_canon, monkeypatch, problem, expected
):
    fixture = prepared_canon
    ids, body, old_ids, _ = _book(fixture)
    with fixture.Session.begin() as session:
        candidate = save_revision_proposal(
            session,
            project_id=ids[0],
            chapter_number=1,
            body=body.replace("安静地", "静静地"),
        )
        candidate_id = candidate.id

    class ProblemModel(BodyModel):
        def chat(self, messages, **kwargs):
            response = super().chat(messages, **kwargs)
            value = json.loads(response)
            if "coverage" in value:
                if problem == "form_timeout":
                    raise TimeoutError("controlled form transport timeout")
                value["coverage"][0]["status"] = "fail"
                value["coverage"][0]["explanation"] = (
                    "Controlled known possession contradiction."
                )
            return json.dumps(value)

    if problem == "late_graph_failure":

        def unavailable(*args, **kwargs):
            raise RuntimeError("controlled downstream extraction failure")

        monkeypatch.setattr(
            "forwin.canon.revision_evaluator.BookStateGraphDeltaExtractor.extract",
            unavailable,
        )
    prepared = RevisionValidationService(
        session_factory=fixture.Session,
        writer=ChapterWriter(ProblemModel()),
        policy=RuntimePolicy.for_profile("standard"),
    ).prepare(project_id=ids[0], candidate_id=candidate_id)
    assert prepared.blocked
    with fixture.Session() as session:
        record = session.get(CanonRevisionValidationRecord, prepared.blocked_path)
        assert record.status == expected, record.result_json
        from forwin.models.project import ChapterPlan

        assert [
            session.get(ChapterPlan, chapter_id).active_commit_id
            for chapter_id in ids[1:]
        ] == old_ids


def test_review_approve_continue_accepts_historical_success_without_artifact_path(
    quality_session, monkeypatch
):
    from forwin.api_schema import ChapterReviewApproveRequest
    from forwin.application.projects import reviews

    session = quality_session
    from forwin.models.project import Project

    project = session.get(Project, "book")
    project.runtime_policy_json = RuntimePolicy.for_profile(
        "standard"
    ).model_dump_json()
    project.runtime_policy_version = 1
    monkeypatch.setattr(
        reviews,
        "build_project_detail",
        lambda **kwargs: SimpleNamespace(blocking_reason=SimpleNamespace(code="")),
    )
    monkeypatch.setattr(
        reviews,
        "build_continue_generation_workset",
        lambda *args, **kwargs: SimpleNamespace(requested_chapters=1),
    )
    # Shape returned by AcceptanceStage's successful historical branch.
    pipeline = SimpleNamespace(
        accept_review=lambda *args, **kwargs: {
            "status": "accepted",
            "message": "Revision accepted",
            "canon_commit_id": "new-acceptance",
        }
    )
    tasks = []
    monkeypatch.setattr(session, "close", lambda: None)
    result = reviews.approve_chapter_review(
        "book",
        1,
        ChapterReviewApproveRequest(
            reason="Reviewed revision", continue_generation=True
        ),
        config=object(),
        pipeline=pipeline,
        get_session=lambda: session,
        display_datetime=str,
        active_generation_task_error_cls=RuntimeError,
        require_reason=lambda value, **kwargs: value,
        project_has_active_generation_task=lambda *args, **kwargs: False,
        generation_task_conflict_message=lambda *args: "busy",
        log_decision_event=lambda *args, **kwargs: None,
        create_continue_generation_task=lambda **kwargs: "follow-up-task",
        update_task=lambda *args, **kwargs: tasks.append((args, kwargs)),
    )
    assert result.ok
    assert result.task_id == "follow-up-task"
    assert tasks[0][1]["frozen_artifacts"] == []


def test_quality_signal_owner_supports_explicit_complete_reads(quality_session):
    repo = CanonQualityRepository(quality_session)
    repo.save_signals(
        [
            CanonQualitySignal(
                signal_id=f"raw-{number}",
                project_id="book",
                chapter_number=1,
                signal_type="review_warning",
                severity="warning",
                target_scope="chapter",
                description="Concern",
            )
            for number in range(130)
        ]
    )
    assert len(repo.list_open_signals("book")) == 100
    assert len(repo.list_open_signals("book", limit=None)) == 130


def test_isolated_writer_preserves_explicit_transport_timeout_and_stage_contract():
    from forwin.canon.revision_model import isolated_writer, model_identity

    class ExplicitTransport:
        provider = "fixture"
        model = "frozen"

        def __init__(self):
            self.llm_attempt_events = []
            self.calls = []

        def chat(
            self,
            messages,
            *,
            temperature=0.2,
            max_tokens=1000,
            response_format=None,
            timeout_seconds=None,
            retry_on_timeout=True,
            task_family="",
            stage_key="",
        ):
            self.calls.append(
                (
                    timeout_seconds,
                    retry_on_timeout,
                    task_family,
                    stage_key,
                    response_format,
                )
            )
            self.llm_attempt_events.append(
                {
                    "provider": self.provider,
                    "model": self.model,
                    "finish_reason": "stop",
                }
            )
            return "{}"

    client = ExplicitTransport()
    original = ChapterWriter(client)
    writer = isolated_writer(original, model_identity(original))
    writer._chat_json(
        [{"role": "user", "content": "Full body"}],
        temperature=0.2,
        max_tokens=1000,
        timeout_seconds=31,
        retry_on_timeout=False,
        max_attempts=1,
        stage_key="state_event_extraction",
    )
    assert client.calls == [
        (31, False, "writer", "state_event_extraction", {"type": "json_object"})
    ]


def test_late_duplicate_validation_cannot_demote_an_already_accepted_candidate(
    prepared_canon,
):
    from forwin.canon.admission import CanonAdmissionService
    from forwin.canon.revision_service import revision_model_identity
    from forwin.models.draft import CandidateDraftRecord
    from forwin.models.project import Project

    fixture = prepared_canon
    ids, body, _old_ids, _ = _book(fixture)
    with fixture.Session.begin() as session:
        candidate = save_revision_proposal(
            session,
            project_id=ids[0],
            chapter_number=1,
            body=body.replace("安静地", "静静地"),
        )
        candidate_id = candidate.id
    first_writer = ChapterWriter(BodyModel())
    first = RevisionValidationService(
        session_factory=fixture.Session,
        writer=first_writer,
        policy=RuntimePolicy.for_profile("standard"),
    ).prepare(project_id=ids[0], candidate_id=candidate_id)
    assert not first.blocked
    winner = []

    class InterleavedModel(BodyModel):
        def chat(self, messages, **kwargs):
            response = super().chat(messages, **kwargs)
            parsed = json.loads(response)
            if (
                "coverage" in parsed
                and parsed["answers"]["chapter_number"] == 2
                and not winner
            ):
                outcome = CanonAdmissionService(
                    session_factory=fixture.Session
                ).commit_plan(
                    first.plan,
                    revision_model_identity=revision_model_identity(first_writer),
                )
                assert not outcome.blocked, outcome
                winner.append(outcome.commit_id)
            return response

    RevisionValidationService(
        session_factory=fixture.Session,
        writer=ChapterWriter(InterleavedModel()),
        policy=RuntimePolicy.for_profile("standard"),
    ).prepare(project_id=ids[0], candidate_id=candidate_id)
    assert winner
    with fixture.Session() as session:
        accepted = session.get(CandidateDraftRecord, candidate_id)
        assert session.get(Project, ids[0]).book_revision == 3
        assert accepted.canon_commit_id == winner[0]
        assert accepted.status == "accepted"


def test_entity_classifier_timeout_is_missing_identity_evidence_not_content_conflict(prepared_canon, monkeypatch):
    fixture = prepared_canon
    ids, body, _old_ids, _ = _book(fixture)
    with fixture.Session.begin() as session:
        candidate = save_revision_proposal(session, project_id=ids[0], chapter_number=1, body="林青查看档案。\n" + body)
        candidate_id = candidate.id

    class NamedModel(BodyModel):
        def chat(self, messages, **kwargs):
            value = json.loads(super().chat(messages, **kwargs))
            if "coverage" not in value:
                value["entity_mentions"] = [{"entity_name": "林青", "entity_kind": "character", "is_named": True,
                    "is_on_stage": True, "evidence_refs": ["body:林青查看档案"]}]
            return json.dumps(value)

    calls = []

    def unavailable(*args, **kwargs):
        calls.append(True)
        raise TimeoutError("controlled entity classification timeout")

    monkeypatch.setattr("forwin.naming.entity_registrar.LLMEntityAdmissionClassifier.classify", unavailable)
    prepared = RevisionValidationService(session_factory=fixture.Session, writer=ChapterWriter(NamedModel()),
        policy=RuntimePolicy.for_profile("standard")).prepare(project_id=ids[0], candidate_id=candidate_id)
    assert calls, "the actual entity owner must reach the unavailable classifier"
    assert prepared.blocked
    with fixture.Session() as session:
        record = session.get(CanonRevisionValidationRecord, prepared.blocked_path)
        assert record.status == "unknown", record.result_json
