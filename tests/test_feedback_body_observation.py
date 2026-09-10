from __future__ import annotations

import importlib
import importlib.util
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from forwin.candidate_drafts import candidate_body_hash
from forwin.models.canon import CanonCommitRecord
from forwin.models.draft import CandidateDraftRecord, ChapterDraft
from forwin.models.genesis import PromptTrace
from forwin.protocol.context import AudienceHintView
from forwin.writer.feedback_input import feedback_input_evidence, render_feedback_hints
from tests import test_feedback_plan_service as plans

plan_case = plans.plan_case


def _owner():
    assert importlib.util.find_spec("forwin.audience.body_observation"), (
        "BODY evidence needs its own grounded observation owner"
    )
    return importlib.import_module(
        "forwin.audience.body_observation"
    ).FeedbackBodyObservationService()


@pytest.fixture
def body_case(plan_case):
    session, chapter = plan_case
    action = plans._selected(session)
    # Real input evidence extraction, not a manually invented hint-included flag.
    payload = json.loads(action.action_payload_json)
    payload["hint"]["category"] = "clarity"
    action.action_payload_json = json.dumps(payload)
    text = payload["hint"]["text"]
    hints = AudienceHintView(
        items=[{"action_id": action.id, "category": "clarity", "text": text}]
    )
    messages = [{"role": "user", "content": render_feedback_hints(hints)}]
    event = feedback_input_evidence(messages, "chapter_write")
    event["adapter_outcome"] = "returned"
    snapshot = {"chapter_number": 3, "feedback_inputs": [event]}
    trace_payload = {
        "trace_scope": "writer",
        "stage_key": "chapter_write",
        "input_snapshot": snapshot,
    }
    trace = PromptTrace(
        id="trace",
        project_id="book",
        trace_scope="writer",
        stage_key="chapter_write",
        input_snapshot_json=json.dumps(snapshot),
    )
    session.add(trace)
    session.flush()
    from forwin.audience.actions import record_prompt_inclusions

    record_prompt_inclusions(
        session,
        project_id="book",
        chapter_number=3,
        prompt_trace_id=trace.id,
        feedback_inputs=[event],
    )
    body = "他举起铜钥匙。门卫解释：只有交还钥匙才能离开。于是他把钥匙放在桌上。"
    digest = candidate_body_hash(body)
    draft = ChapterDraft(
        id="draft", chapter_plan_id=chapter.id, version=1, body_text=body
    )
    candidate = CandidateDraftRecord(
        id="candidate",
        project_id="book",
        chapter_plan_id=chapter.id,
        chapter_number=3,
        candidate_draft_id=draft.id,
        body_hash=digest,
        plan_revision="frozen-plan",
        canon_commit_id="canon",
        status="accepted",
        metadata_json=json.dumps({"prompt_trace": trace_payload}),
        canon_commit_plan_json=json.dumps(
            {
                "canon_commit_id": "canon",
                "candidate_id": "candidate",
                "candidate_body_hash": digest,
                "plan_revision": "frozen-plan",
                "project_id": "book",
                "chapter_number": 3,
            }
        ),
    )
    canon = CanonCommitRecord(
        id="canon",
        idempotency_key="once",
        project_id="book",
        chapter_plan_id=chapter.id,
        chapter_number=3,
        candidate_id=candidate.id,
        created_at=datetime.now(UTC),
    )
    session.add_all([draft, candidate, canon])
    chapter.status = "accepted"
    chapter.active_commit_id = "canon"
    session.commit()
    return SimpleNamespace(
        session=session,
        action=action,
        chapter=chapter,
        draft=draft,
        candidate=candidate,
        canon=canon,
        trace=trace,
        event=event,
        body=body,
        digest=digest,
    )


def _record(case, **kwargs):
    return _owner().record(
        session=case.session,
        project_id="book",
        action_id=case.action.id,
        canon_commit_id=case.canon.id,
        **kwargs,
    )


def _assessment(case, **changes):
    text = "只有交还钥匙才能离开"
    start = case.body.index(text)
    return {
        "assessment": "observed",
        "observer": {
            "kind": "frozen_observer",
            "source_ref": "fixture:independent-rule-label-v1",
        },
        "explanation": "正文明确说明了钥匙与离开的规则；这是关联观察，不证明反馈导致该句。",
        "reviewed_content_sha256": case.digest,
        "quotes": [{"start": start, "end": start + len(text), "text": text}],
        **changes,
    }


def test_actual_input_and_accepted_body_do_not_automatically_claim_semantic_application(
    body_case,
):
    c = body_case
    result = _record(c)
    assert result["assessment"] == "unknown"
    assert result["action_id"] == c.action.id
    assert result["canon_commit_id"] == c.canon.id
    assert result["content_sha256"] == c.digest
    assert result["plan_revision"] == "frozen-plan"
    assert result["prompt_input_ids"] == [c.event["input_id"]]
    assert result["prompt_trace_ids"] == ["trace"]
    assert result["quotes"] == []
    assert result["causal_claim"] is False
    assert "unassessed" in result["reasons"]
    assert json.loads(c.action.body_observation_json)["observations"] == [result]
    assert c.action.plan_application_json == "{}"
    assert c.action.effect_observation_json == "{}"
    assert c.chapter.status == "accepted"
    assert c.draft.body_text == c.body
    assert _record(c) == result
    assert len(json.loads(c.action.body_observation_json)["observations"]) == 1
    c.session.rollback()
    assert c.action.body_observation_json == "{}"


def test_grounded_assessment_appends_after_unknown_and_binds_exact_body(body_case):
    c = body_case
    first = _record(c)
    result = _record(c, assessment=_assessment(c))
    assert result["assessment"] == "observed"
    assert result["quotes"] == _assessment(c)["quotes"]
    assert result["observer"]["source_ref"] == "fixture:independent-rule-label-v1"
    assert result["committed_at"]
    assert result["body_seen"] is True
    assert len(result["evidence_sha256"]) == 64
    assert json.loads(c.action.body_observation_json)["observations"] == [first, result]
    assert _record(c, assessment=_assessment(c)) == result


@pytest.mark.parametrize(
    "change",
    [
        {"reviewed_content_sha256": "0" * 64},
        {"quotes": [{"start": 0, "end": 3, "text": "不存在"}]},
        {"quotes": [{"start": False, "end": 3, "text": "他举起"}]},
        {"quotes": []},
        {
            "observer": {
                "kind": "automatic_hint_inclusion",
                "source_ref": "plan applied",
            }
        },
        {"observer": {"kind": "human", "source_ref": ""}},
        {"explanation": ""},
    ],
)
def test_invalid_assessment_is_rejected_without_mutating_evidence(body_case, change):
    with pytest.raises(ValueError):
        _record(body_case, assessment=_assessment(body_case, **change))
    assert body_case.action.body_observation_json == "{}"


@pytest.mark.parametrize(
    "tamper",
    [
        "missing_trace",
        "other_trace",
        "trace_hash",
        "candidate_input",
        "hint_hash",
        "trace_project",
        "trace_chapter",
    ],
)
def test_unrelated_or_tampered_input_never_supports_body_application(body_case, tamper):
    c = body_case
    if tamper == "missing_trace":
        c.session.delete(c.trace)
    elif tamper == "other_trace":
        c.candidate.metadata_json = "{}"
    elif tamper == "candidate_input":
        meta = json.loads(c.candidate.metadata_json)
        meta["prompt_trace"]["input_snapshot"]["feedback_inputs"][0]["input_id"] = (
            "different-attempt"
        )
        c.candidate.metadata_json = json.dumps(meta)
    elif tamper == "trace_project":
        c.trace.project_id = "other"
    else:
        payload = json.loads(c.trace.input_snapshot_json)
        if tamper == "trace_hash":
            payload["feedback_inputs"][0]["messages_sha256"] = "0" * 64
        elif tamper == "trace_chapter":
            payload["chapter_number"] = 4
        else:
            payload["feedback_inputs"][0]["hints"][0]["hint_sha256"] = "0" * 64
        c.trace.input_snapshot_json = json.dumps(payload)
    c.session.commit()
    result = _record(c)
    assert result["assessment"] == "unknown"
    assert result["prompt_input_ids"] == []
    assert "writer_input_unverified" in result["reasons"]
    with pytest.raises(ValueError, match="input"):
        _record(c, assessment=_assessment(c))


@pytest.mark.parametrize(
    "tamper",
    ["body", "candidate_project", "draft_plan", "uncommitted", "foreign_action"],
)
def test_canon_or_content_identity_failure_refuses_without_new_observation(
    body_case, tamper
):
    c = body_case
    if tamper == "body":
        c.draft.body_text += "Changed after acceptance"
    elif tamper == "candidate_project":
        c.candidate.project_id = "other"
    elif tamper == "draft_plan":
        c.draft.chapter_plan_id = "other-3"
    elif tamper == "uncommitted":
        c.canon.status = "prepared"
    else:
        c.action.project_id = "other"
    c.session.commit()
    with pytest.raises(ValueError):
        _record(c)
    assert c.action.body_observation_json == "{}"


def test_missing_immutable_plan_identity_stays_unknown_instead_of_using_current_plan(
    body_case,
):
    c = body_case
    c.candidate.canon_commit_plan_json = "{}"
    c.chapter.one_line = "This mutable plan is not the accepted version"
    c.session.commit()
    result = _record(c)
    assert result["plan_revision"] is None
    assert "plan_identity_unavailable" in result["reasons"]
    with pytest.raises(ValueError, match="plan"):
        _record(c, assessment=_assessment(c))


def test_action_selected_after_target_chapter_cannot_be_backfilled_as_observed(
    body_case,
):
    c = body_case
    c.action.selected_at_chapter = c.canon.chapter_number
    c.session.commit()
    with pytest.raises(ValueError, match="scope"):
        _record(c)
    assert c.action.body_observation_json == "{}"


def test_malformed_history_is_preserved(body_case):
    c = body_case
    c.action.body_observation_json = "{broken historical evidence"
    c.session.commit()
    with pytest.raises(ValueError, match="history"):
        _record(c)
    assert c.action.body_observation_json == "{broken historical evidence"


def test_explicit_negative_observation_does_not_become_implementation(body_case):
    c = body_case
    result = _record(
        c,
        assessment=_assessment(
            c,
            assessment="not_observed",
            explanation="冻结独立观察认为该句仍未解释为何必须归还；不把引用存在等同于语义充分。",
        ),
    )
    assert result["assessment"] == "not_observed"
    assert result["causal_claim"] is False
    assert c.action.effect_observation_json == "{}"


def test_actual_writer_trace_and_candidate_repository_supply_body_provenance(plan_case):
    from forwin.audience.actions import action_hint
    from forwin.candidate_drafts import (
        CandidateDraftRepository,
        candidate_plan_revision,
    )
    from forwin.models.draft import ChapterReview
    from forwin.observability.pipeline_trace import (
        PipelineAuditContext,
        PipelineTraceRecorder,
    )
    from forwin.state.updater import StateUpdater
    from forwin.writer.chapter_writer import ChapterWriter
    from tests.test_feedback_prompt_evidence import CapturingModel, hint_context

    session, chapter = plan_case
    action = plans._selected(session)
    payload = json.loads(action.action_payload_json)
    payload["hint"]["category"] = "clarity"
    action.action_payload_json = json.dumps(payload)
    session.flush()
    model = CapturingModel()
    writer = ChapterWriter(model, writer_mode="single", min_chapter_chars=200)
    context = hint_context().model_copy(
        update={
            "chapter_number": 3,
            "audience_hints": AudienceHintView(items=[action_hint(action)]),
        }
    )
    output = writer.write_chapter(context)
    recorder = PipelineTraceRecorder(
        audit=PipelineAuditContext(task_id="task"),
        artifact_store=SimpleNamespace(
            save_observability_diagnostic=lambda **kwargs: {}
        ),
        observability=SimpleNamespace(),
    )
    trace_id = recorder.save_prompt_trace(
        session=session,
        updater=StateUpdater(session),
        project_id="book",
        prompt_trace=output.generation_meta["prompt_trace"],
    )
    draft = ChapterDraft(chapter_plan_id=chapter.id, version=1, body_text=output.body)
    session.add(draft)
    session.flush()
    review = ChapterReview(draft_id=draft.id, verdict="accept")
    session.add(review)
    session.flush()
    revision = candidate_plan_revision(chapter)
    candidate = CandidateDraftRepository(session).create_reviewed_version(
        project_id="book",
        chapter_plan=chapter,
        draft=draft,
        review=review,
        writer_output=output,
        plan_revision=revision,
        policy_version=1,
    )
    # Canon is an explicit accepted fixture here; this test exercises the real
    # Writer/trace/candidate owners, not another acceptance implementation.
    canon = CanonCommitRecord(
        id="accepted-fixture",
        idempotency_key="accepted-fixture",
        candidate_id=candidate.id,
        project_id="book",
        chapter_plan_id=chapter.id,
        chapter_number=3,
        created_at=datetime.now(UTC),
    )
    candidate.canon_commit_plan_json = json.dumps(
        {
            "canon_commit_id": canon.id,
            "candidate_id": candidate.id,
            "candidate_body_hash": candidate.body_hash,
            "plan_revision": revision,
            "project_id": "book",
            "chapter_number": 3,
        }
    )
    session.add(canon)
    chapter.active_commit_id = canon.id
    chapter.status = "accepted"
    session.commit()
    result = _owner().record(
        session=session,
        project_id="book",
        action_id=action.id,
        canon_commit_id=canon.id,
    )
    assert result["prompt_trace_ids"] == [trace_id]
    assert len(result["prompt_input_ids"]) == len(model.messages) == 1
    assert result["content_sha256"] == candidate_body_hash(output.body)
    assert result["plan_revision"] == revision
    assert result["assessment"] == "unknown"


def test_body_observation_retains_action_selection_and_source_identity(body_case):
    result = _record(body_case)
    source = result["source"]
    assert source["selected_at_chapter"] == body_case.action.selected_at_chapter
    assert source["source_qualified"] is True
    assert source["target_chapter_start"] == body_case.action.target_chapter_start
    assert source["hint_expires_at_chapter"] == body_case.action.hint_expires_at_chapter
