"""DB-free regressions for scoped collection and persisted chain metadata."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest

from forwin.generation.pipeline_core.quality_gates import (
    QualityDiagnosticsStage, _persist_canon_quality_attempt_trace,
)
from forwin.generation.pipeline_core.runtime_helpers import RuntimeSupportStage
from forwin.models import PromptTrace
from forwin.observability.llm_trace import (
    build_workflow_trace, drain_workflow_attempts, served_model_profile,
)
from forwin.state.updater import StateUpdater
from forwin.storage import ArtifactStore
from tests.test_llm_chain_routing import make_client, profiles  # noqa: F401


class MemorySession:
    """Only replace the database boundary; serialize real PromptTrace rows."""

    def __init__(self):
        self.rows = []

    def add(self, row):
        self.rows.append(row)

    def flush(self):
        pass

    def get(self, model, key):
        return None


class TraceStage(QualityDiagnosticsStage, RuntimeSupportStage):
    def _record_decision_event(self, **kwargs):
        return SimpleNamespace(id="event")


@pytest.fixture
def persistence(tmp_path):
    session = MemorySession()
    stage = TraceStage()
    stage.artifact_store = ArtifactStore(str(tmp_path / "artifacts"))
    stage._audit_task_id = "task"
    stage.writer = SimpleNamespace(writer_mode="test")
    return stage, StateUpdater(session)


def persist_trace(persistence, trace):
    stage, updater = persistence
    stage._save_prompt_trace_payload(
        session=updater.session, updater=updater, project_id="project", prompt_trace=trace,
    )
    return next(row for row in reversed(updater.session.rows) if isinstance(row, PromptTrace))


def workflow_trace(client, attempts):
    return build_workflow_trace(
        client, messages=[], attempts=attempts, trace_scope="world_engine",
        stage_key="same_stage", template_id="test", input_snapshot={}, output_summary={},
    )


def test_concurrent_drains_and_trace_builds_keep_attempts_identity_and_parse_marker(make_client):
    client, _ = make_client()
    first_done, second_done, first_drained = Event(), Event(), Event()

    def first():
        # Both calls belong to A's workflow; neither may be discarded by B.
        for _ in range(2):
            client.chat([{"role": "user", "content": "A"}], task_family="review", stage_key="same_stage")
        first_done.set()
        assert second_done.wait(5)
        identity = served_model_profile(llm_client=client)
        attempts = drain_workflow_attempts(
            client, attempt_no=2, stage_key="same_stage", task_family="review",
            messages=[], raw="A", call_error=None, error_message="",
        )
        trace = workflow_trace(client, attempts)
        fallbacks = client.drain_model_fallback_events()
        first_drained.set()
        return identity, trace, fallbacks

    def second():
        assert first_done.wait(5)
        # This route serves Kimi, while A's review route serves DeepSeek.
        client.chat([{"role": "user", "content": "B"}], task_family="writer", stage_key="same_stage")
        second_done.set()
        assert first_drained.wait(5)
        identity = served_model_profile(llm_client=client)
        attempts = drain_workflow_attempts(
            client, attempt_no=1, stage_key="same_stage", task_family="writer",
            messages=[], raw="B", call_error=None, error_message="",
            parse_error=ValueError("B parse failure"), parser_name="B parser",
        )
        return identity, workflow_trace(client, attempts), client.drain_model_fallback_events()

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(first), pool.submit(second)
        a_identity, a_trace, _ = a.result(timeout=10)
        b_identity, b_trace, _ = b.result(timeout=10)
    for identity, trace, label, profile, count in (
        (a_identity, a_trace, "A", "env-deepseek", 2),
        (b_identity, b_trace, "B", "env-kimi", 1),
    ):
        assert identity["profile_id"] == profile
        assert trace["model_profile"]["profile_id"] == profile
        assert len(trace["attempts"]) == count
        assert {item["_raw_request_payload"]["messages"][0]["content"] for item in trace["attempts"]} == {label}
    assert b_trace["attempts"][0]["parse_error"] == "B parse failure"
    assert all("parse_error" not in item for item in a_trace["attempts"])
    assert client.drain_llm_attempt_events() == []


def test_concurrent_fallback_drains_do_not_clear_another_thread(make_client):
    client, _ = make_client({"deepseek": (402, "balance"), "kimi": (402, "balance")})
    a_done, b_done, a_drained = Event(), Event(), Event()

    def invoke(label):
        if label == "B":
            assert a_done.wait(5)
        client.chat([{"role": "user", "content": label}], task_family="review")
        if label == "A":
            a_done.set()
            assert b_done.wait(5)
        else:
            b_done.set()
            assert a_drained.wait(5)
        attempts = client.drain_llm_attempt_events()
        events = client.drain_model_fallback_events()
        if label == "A":
            a_drained.set()
        return attempts, events

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(invoke, "A"), pool.submit(invoke, "B")
        for future in (a, b):
            attempts, events = future.result(timeout=10)
            assert len(attempts) == 3
            assert len(events) == 2
            assert {event["attempt_group_id"] for event in events} == {attempts[0]["attempt_group_id"]}


def structured_call(client, updater, *, stage_key="business_retry"):
    from pydantic import BaseModel
    from forwin.llm.structured_call import (
        GateAuditWriter, StructuredCallEnvelope, StructuredCallEventTypes, run_structured_llm_call,
    )

    class Answer(BaseModel):
        value: str

    return run_structured_llm_call(
        llm_client=client, recorder=GateAuditWriter(
            save_decision_event=updater.save_decision_event, save_prompt_trace=updater.save_prompt_trace,
        ), gate_kind="test", stage_key=stage_key, messages=[], output_model=Answer,
        output_schema=Answer.model_json_schema(), event_types=StructuredCallEventTypes("", "", ""),
        request_payload={}, envelope=StructuredCallEnvelope(project_id="project"), input_snapshot={},
        verdict=lambda outcome: None, trace_scope="test", template_id="test",
    )


def test_structured_call_keeps_pending_workflow_attempts(make_client):
    client, _ = make_client({"deepseek": (200, '{"value":"ok"}')})
    client.chat([], task_family="review", stage_key="business_retry")
    updater = StateUpdater(MemorySession())
    outcome = structured_call(client, updater)
    assert outcome.ok
    row = next(row for row in updater.session.rows if isinstance(row, PromptTrace))
    assert len(json.loads(row.attempts_json)) == 2
    assert row.fallback_used is False


def test_structured_call_cannot_discard_or_persist_another_threads_attempts(make_client):
    client, _ = make_client({"kimi": (200, "invalid B response")})
    a_done, b_done = Event(), Event()

    def first():
        client.chat([{"role": "user", "content": "private A prompt"}], task_family="review")
        a_done.set()
        assert b_done.wait(5)
        return workflow_trace(client, client.drain_llm_attempt_events())

    def second():
        assert a_done.wait(5)
        updater = StateUpdater(MemorySession())
        # Preview's route begins with Kimi; its schema failure must annotate B.
        outcome = structured_call(client, updater, stage_key="writer_preview")
        row = next(row for row in updater.session.rows if isinstance(row, PromptTrace))
        b_done.set()
        return outcome, row

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(first), pool.submit(second)
        a_trace = a.result(timeout=10)
        b_outcome, b_row = b.result(timeout=10)
    assert len(a_trace["attempts"]) == 1
    assert a_trace["model_profile"]["profile_id"] == "env-deepseek"
    assert b_outcome.failure_reason == "parse_or_schema"
    assert b_outcome.profile_id == "env-kimi"
    b_attempts = json.loads(b_row.attempts_json)
    assert len(b_attempts) == 1
    assert b_attempts[0]["profile_id"] == "env-kimi"
    assert b_attempts[0]["parse_ok"] is False
    assert b_attempts[0]["parser_name"] == "structured_llm_call"
    assert "private A prompt" not in b_row.attempts_json


@pytest.mark.parametrize("pin", ["", "env-deepseek"])
@pytest.mark.parametrize("builder", ["workflow", "genesis", "writer", "extraction", "entity", "reviewer", "repair", "post_canon"])
def test_trace_builders_persist_served_identity_and_fallback_after_402(make_client, persistence, monkeypatch, builder, pin):
    from forwin.book_state.extraction.chapter_delta_extractor import ChapterDeltaExtractor
    from forwin.genesis import BookGenesisService
    from forwin.naming.entity_registrar import LLMEntityAdmissionClassifier
    from forwin.review.llm_webnovel import LLMWebNovelReviewer
    from forwin.writer.chapter_writer import ChapterWriter

    # A configured pin must not override the provider which actually answered.
    client, _ = make_client(
        {"deepseek": (402, "balance")}, pin=pin,
        chains={"default": ("env-deepseek", "env-kimi")},
    )
    client.chat([], task_family="review", stage_key="comment_analysis" if builder == "post_canon" else "test")
    if builder == "workflow":
        trace = workflow_trace(client, client.drain_llm_attempt_events())
    elif builder == "genesis":
        trace = BookGenesisService(llm_client=client)._trace_payload(
            stage_key="brief", effective_system_prompt="", messages=[], attempts=[], output_summary={},
        )
    elif builder == "writer":
        trace = ChapterWriter(client)._build_prompt_trace(
            base_messages=[], skill_layers=[], template_id="test", stage_key="chapter_draft",
            input_snapshot={}, output_summary={},
        )
    elif builder == "extraction":
        trace = ChapterDeltaExtractor(client)._build_prompt_trace(
            messages=[], extraction_input=SimpleNamespace(project_id="project", chapter_number=1),
            chapter_title="test", body_hash="hash", attempts=client.drain_llm_attempt_events(),
            business_retries=[], extraction=None, issues=[], plan_fingerprint="",
        )
    elif builder == "entity":
        traces = []
        LLMEntityAdmissionClassifier(client, save_trace=traces.append)._save_call_trace(
            project_id="project", chapter_number=1, stage_key="entity_registrar",
            messages=[{"role": "system", "content": "test"}], raw="ok", error=None, attempt_index=0,
        )
        trace = traces[0]
    elif builder == "reviewer":
        trace = LLMWebNovelReviewer(llm_client=client)._build_prompt_trace(
            context=SimpleNamespace(project_id="project", chapter_number=1), writer_output=SimpleNamespace(char_count=1),
            messages=[], attempts=client.drain_llm_attempt_events(), output_summary={},
        )
    elif builder == "repair":
        from forwin.generation.pipeline_core.review_autofix import ReviewWorkflowStage
        from forwin.protocol.review import RepairVerification, ReviewVerdict

        stage = ReviewWorkflowStage()
        stage.repair_verifier = SimpleNamespace(llm_client=client, verify=lambda **kwargs: RepairVerification(
            fixed_all_must_fix=True, preserved_all_must_preserve=True,
        ))
        review = stage._review_with_repair_verification(
            original_output=None, repaired_output=SimpleNamespace(chapter_number=1), before_review=None,
            review=ReviewVerdict(verdict="pass", issues=[]), repair_instruction=None,
        )
        trace = review.prompt_trace
    else:
        from forwin.maintenance import post_canon

        stage, updater = persistence
        # Keep actual audience trace persistence, replace only the DB-backed outbox.
        frozen_traces = []
        monkeypatch.setattr(post_canon, "enqueue_trace_upload", lambda *args, trace: frozen_traces.append(trace) or {})
        service = post_canon.PostCanonMaintenanceService(
            session_factory=None, comment_analyzer=None, artifact_store=stage.artifact_store, llm_client=client,
        )
        service._enqueue_step_trace(
            session=updater.session, commit=SimpleNamespace(id="commit", project_id="project", chapter_number=1),
            step_name="feedback", attempts=client.drain_llm_attempt_events(),
        )
        assert frozen_traces[0]["model_profile"]["profile_id"] == "env-kimi"
        assert frozen_traces[0]["backend"] == "openai_compatible"
        assert frozen_traces[0]["fallback_used"] is True
    row = (
        next(row for row in persistence[1].session.rows if isinstance(row, PromptTrace))
        if builder == "post_canon" else persist_trace(persistence, trace)
    )
    identity = json.loads(row.model_profile_json)
    assert identity["profile_id"] == "env-kimi"
    assert identity["model"] == "kimi-model"
    assert row.backend == "openai_compatible"
    assert row.fallback_used is True
    assert [item["http_status"] for item in json.loads(row.attempts_json)] == [402, 200]


@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("path", ["canon_quality", "writer_failure"])
def test_unconfigured_skip_survives_safe_trace_persistence(make_client, persistence, path, fallback):
    configured = profiles()
    del configured["env-deepseek"]
    client, _ = make_client({"kimi": (402, "balance")} if fallback else None, configured=configured)
    client.chat([], task_family="review")
    # Only public routing fields may survive even if a future producer adds secrets.
    attempt = client.llm_attempt_events[0]
    attempt["candidate_chain"][0].update(api_key="secret-key", extra={"authorization": "secret-auth"})
    attempt["skipped_profiles"][0].update(api_key="secret-key", extra={"prompt": "secret-prompt"})
    attempt["candidate_chain"].append({"profile_id": {"api_key": "nested-secret"}})
    stage, updater = persistence
    stage.llm_client = client
    if path == "canon_quality":
        context = SimpleNamespace(
            llm_client=client, drain_llm_attempt_events=client.drain_llm_attempt_events,
            save_prompt_trace=stage._save_prompt_trace_payload,
        )
        trace_id = _persist_canon_quality_attempt_trace(
            context, session=updater.session, updater=updater, project_id="project",
            chapter_number=1, candidate_id="candidate",
        )
    else:
        trace_id = stage._record_failure_prompt_trace(
            updater=updater, project_id="project", chapter_number=1, context=None,
            stage_key="chapter_draft", template_id="writer:failure", source_event_id="event",
            exc=ValueError("invalid content"), duration_ms=1,
            attempts=client.drain_llm_attempt_events(), skill_layers=[],
        )
    row = next(row for row in updater.session.rows if isinstance(row, PromptTrace) and row.id == trace_id)
    saved = json.loads(row.attempts_json)[0]
    assert saved["skipped_profiles"] == [{"profile_id": "env-deepseek", "reason": "profile_unconfigured"}]
    assert saved["candidate_chain"][0] == {"profile_id": "env-deepseek"}
    assert saved["candidate_chain"][1]["profile_id"] == "env-kimi"
    assert saved["route_policy_version"] == "v5.0-policy-chains"
    assert "secret" not in row.attempts_json
    assert "_raw_request_payload" not in row.attempts_json
    assert json.loads(row.model_profile_json)["profile_id"] == ("env-minimax" if fallback else "env-kimi")
    assert row.backend == "openai_compatible"
    assert row.fallback_used is fallback


@pytest.mark.parametrize("fallback", [False, True])
def test_workflow_metadata_distinguishes_business_calls_from_chain_fallback(make_client, fallback):
    client, _ = make_client({"deepseek": (402, "balance")} if fallback else None)
    client.chat([], task_family="review")
    client.chat([], task_family="writer")
    trace = workflow_trace(client, client.drain_llm_attempt_events())
    assert trace["model_profile"]["profile_id"] == "env-kimi"
    assert trace["fallback_used"] is fallback
    assert len(trace["attempts"]) == (3 if fallback else 2)
