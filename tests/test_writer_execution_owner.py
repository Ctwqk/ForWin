from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import import_module
from importlib.util import find_spec
from types import SimpleNamespace

import pytest

from forwin.audit.events import DecisionEventType
from forwin.protocol.writer import WriterOutput
from forwin.runtime.policy import RuntimePolicy


class _Client:
    profile_id = "writer-client-profile"
    model = "writer-client-model"
    base_url = "https://writer.invalid/v1"

    def drain_model_fallback_events(self):
        return []

    def drain_llm_attempt_events(self):
        return []


class _ScriptedWriter:
    single_call_timeout_seconds = 37
    writer_mode = "split"

    def __init__(self, main, preview=()):
        self.llm_client = _Client()
        self.main = iter(main)
        self.preview = iter(preview)
        self.calls = []

    def write_chapter(self, context, **kwargs):
        self.calls.append(("main", context, kwargs))
        return self._resolve(next(self.main))

    def write_preview_chapter(self, context, **kwargs):
        self.calls.append(("preview", context, kwargs))
        return self._resolve(next(self.preview))

    @staticmethod
    def _resolve(value):
        if isinstance(value, BaseException):
            raise value
        return value


class _SkillRouter:
    def __init__(self):
        self.calls = []

    def select(self, **kwargs):
        self.calls.append(kwargs)
        return ["selected-writer-skill"]


class _SkillLayerBuilder:
    def build(self, selections):
        assert selections == ["selected-writer-skill"]
        return ["writer-skill-layer"]


class _EventUpdater:
    def __init__(self):
        self.events = []

    def save_decision_event(self, info):
        event_id = f"event-{len(self.events) + 1}"
        row = SimpleNamespace(
            id=event_id,
            causal_root_id=info.causal_root_id or event_id,
            info=info,
        )
        self.events.append(row)
        return row


@dataclass
class _ArtifactStore:
    path: str = "artifact://writer-failed.json"
    error: Exception | None = None
    frozen: list[dict] = field(default_factory=list)

    def save_frozen_candidate(self, **kwargs):
        self.frozen.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.path


class _Observability:
    def __init__(self):
        self.spans = []

    def _record_span(self, span):
        self.spans.append(span)


def _output(**generation_meta):
    return WriterOutput(
        project_id="project-writer-owner",
        chapter_number=7,
        title="第七章",
        body="完整正文",
        char_count=4,
        end_of_chapter_summary="本章摘要",
        generation_meta=generation_meta,
    )


def _harness(
    main,
    preview=(),
    *,
    retries=3,
    hard_floor=True,
    artifact_store=None,
    should_abort: Callable[[], bool] | None = None,
    task_id="task-writer-owner",
    root_event_id="root-writer-owner",
    **request_options,
):
    for module_name in (
        "forwin.writer.execution",
        "forwin.writer.execution_telemetry",
        "forwin.observability.pipeline_trace",
    ):
        assert find_spec(module_name) is not None, (
            f"A0 requires the independent owner module {module_name}"
        )
    execution = import_module("forwin.writer.execution")
    telemetry_module = import_module("forwin.writer.execution_telemetry")
    trace_module = import_module("forwin.observability.pipeline_trace")
    policy = RuntimePolicy.for_profile("standard")
    policy = policy.model_copy(
        update={
            "writer_attention_retries": retries,
            "canon": policy.canon.model_copy(update={"hard_floor": hard_floor}),
        }
    )
    writer = _ScriptedWriter(main, preview)
    store = artifact_store if artifact_store is not None else _ArtifactStore()
    updater = _EventUpdater()
    router = _SkillRouter()
    audit = trace_module.PipelineAuditContext(
        task_id=task_id, root_event_id=root_event_id
    )
    recorder = trace_module.PipelineTraceRecorder(
        audit=audit, artifact_store=store, observability=_Observability()
    )
    telemetry = telemetry_module.WriterExecutionTelemetry(
        recorder=recorder,
        writer=writer,
        model_client=SimpleNamespace(
            profile_id="active-profile",
            model="active-model",
            base_url="https://model.invalid/v1",
        ),
    )
    owner = execution.WriterExecution(
        policy=policy,
        writer=writer,
        skill_router=router,
        skill_prompt_layer_builder=_SkillLayerBuilder(),
        artifact_store=store,
        telemetry=telemetry,
        should_abort=should_abort,
    )
    context = SimpleNamespace(chapter_number=7)
    request = execution.WriterExecutionRequest(
        context=context,
        project_id="project-writer-owner",
        chapter_number=7,
        updater=updater,
        **request_options,
    )
    return SimpleNamespace(
        owner=owner,
        request=request,
        writer=writer,
        store=store,
        updater=updater,
        router=router,
        audit=audit,
    )


def _events(harness, event_type):
    return [row for row in harness.updater.events if row.info.event_type == event_type]


def test_ordinary_success_preserves_output_and_writer_call_contract():
    output = _output(mode="split", prompt_trace={"attempts": []})
    harness = _harness(
        [output],
        trace_stage_key="chapter_rewrite",
        llm_preferred_provider_kind="deepseek",
        llm_preferred_model="deepseek-reasoner",
    )

    result = harness.owner.execute(harness.request)

    assert result.output is output
    assert result.unwrap() is output
    assert result.error is None
    assert result.aborted is False
    assert result.frozen_artifacts == ()
    assert harness.store.frozen == []
    assert output.generation_meta == {"mode": "split", "prompt_trace": {"attempts": []}}
    assert harness.writer.calls == [
        (
            "main",
            harness.request.context,
            {
                "skill_layers": ["writer-skill-layer"],
                "trace_stage_key": "chapter_rewrite",
                "llm_preferred_provider_kind": "deepseek",
                "llm_preferred_model": "deepseek-reasoner",
            },
        )
    ]
    assert harness.router.calls == [
        {
            "scope": "writer",
            "stage_key": "chapter_rewrite",
            "task_family": "write_chapter",
        }
    ]
    assert [row.info.event_type for row in harness.updater.events] == [
        DecisionEventType.LLM_REQUEST_STARTED,
        DecisionEventType.LLM_REQUEST_SUCCEEDED,
        DecisionEventType.STAGE_DURATION_SUMMARY,
        DecisionEventType.WRITER_OUTPUT_BUILT,
    ]
    for row in harness.updater.events:
        assert row.info.task_id == "task-writer-owner"
        assert row.info.causal_root_id == "root-writer-owner"
        assert row.info.payload["operation_id"] == "task-writer-owner"
    started = harness.updater.events[0].info.payload
    assert started["model_profile_id"] == "active-profile"
    assert started["model"] == "active-model"
    assert started["preferred_provider_kind"] == "deepseek"
    assert started["preferred_model"] == "deepseek-reasoner"


def test_timeout_goes_directly_to_preview_with_original_metadata_and_event_chain(
    monkeypatch,
):
    sleeps = []
    monkeypatch.setattr("time.sleep", sleeps.append)
    error = TimeoutError("The read operation timed out")
    output = _output(
        mode="writer_preview",
        prompt_trace={
            "attempts": [
                {
                    "attempt_group_id": "preview-attempts",
                    "attempt_no": 2,
                    "model": "preview-model",
                    "profile_id": "preview-profile",
                    "output_chars": 4,
                }
            ]
        },
    )
    harness = _harness(
        [error],
        [output],
        retries=3,
        llm_preferred_provider_kind="deepseek",
        llm_preferred_model="deepseek-reasoner",
    )

    result = harness.owner.execute(harness.request)

    assert result.unwrap() is output
    assert result.error is None
    assert result.aborted is False
    assert result.frozen_artifacts == ()
    assert sleeps == []
    assert [call[0] for call in harness.writer.calls] == ["main", "preview"]
    assert harness.writer.calls[1][2] == {
        "skill_layers": ["writer-skill-layer"],
        "trace_stage_key": "writer_preview_fallback",
        "timeout_seconds": 37,
        "max_attempts": 3,
        "retry_on_timeout": False,
    }
    assert output.generation_meta["mode"] == "writer_preview"
    assert output.generation_meta["fallback_from_writer_error"] is True
    assert output.generation_meta["writer_fallback_error"] == str(error)
    failed = _events(harness, DecisionEventType.LLM_REQUEST_FAILED)[0]
    started = _events(harness, DecisionEventType.WRITER_PREVIEW_FALLBACK_STARTED)[0]
    succeeded = _events(harness, DecisionEventType.WRITER_PREVIEW_FALLBACK_SUCCEEDED)[0]
    assert started.info.parent_event_id == failed.id
    assert succeeded.info.parent_event_id == failed.id
    assert started.info.causal_root_id == "root-writer-owner"
    assert succeeded.info.causal_root_id == "root-writer-owner"
    assert succeeded.info.payload["effective_model"] == "preview-model"
    assert succeeded.info.payload["effective_profile_id"] == "preview-profile"
    assert succeeded.info.payload["successful_attempt_no"] == 2
    assert succeeded.info.payload["source_attempt_no"] == 1
    assert succeeded.info.payload["output_chars"] == 4
    assert harness.store.frozen == []


@pytest.mark.parametrize(
    ("errors", "expected_sleeps", "preview_budget"),
    [
        ([ValueError("invalid draft"), ValueError("invalid draft again")], [], 2),
        ([RuntimeError("HTTP 503"), ValueError("invalid draft")], [3.0], 3),
    ],
    ids=[
        "nontransient-retries-without-delay",
        "transient-history-survives-later-error",
    ],
)
def test_main_error_kind_preserves_retry_delay_and_preview_budget(
    monkeypatch, errors, expected_sleeps, preview_budget
):
    sleeps = []
    monkeypatch.setattr("time.sleep", sleeps.append)
    output = _output(mode="writer_preview")
    harness = _harness(errors, [output], retries=2)

    result = harness.owner.execute(harness.request)

    assert result.unwrap() is output
    assert [call[0] for call in harness.writer.calls] == ["main", "main", "preview"]
    assert sleeps == expected_sleeps
    assert harness.writer.calls[-1][2]["max_attempts"] == preview_budget
    assert output.generation_meta["writer_fallback_error"] == str(errors[-1])
    failures = _events(harness, DecisionEventType.LLM_REQUEST_FAILED)
    assert [row.info.payload["attempt_no"] for row in failures] == [1, 2]
    preview = _events(harness, DecisionEventType.WRITER_PREVIEW_FALLBACK_STARTED)[0]
    assert preview.info.parent_event_id == failures[-1].id
    retries = _events(harness, DecisionEventType.RETRY_ATTEMPT)
    assert len(retries) == len(expected_sleeps)
    if retries:
        assert retries[0].info.payload["attempt_no"] == 2
        assert retries[0].info.payload["previous_attempt"] == 1
        assert retries[0].info.payload["delay_seconds"] == 3.0


@pytest.mark.parametrize("hard_floor", [True, False])
@pytest.mark.parametrize("main_is_transient", [True, False])
def test_preview_failure_preserves_exception_identity_cause_and_frozen_result(
    hard_floor, main_is_transient
):
    main_error = (
        RuntimeError("HTTP 529 Unknown Status Code")
        if main_is_transient
        else ValueError("invalid writer output")
    )
    preview_error = RuntimeError("HTTP 503 preview unavailable")
    harness = _harness([main_error], [preview_error], retries=1, hard_floor=hard_floor)

    result = harness.owner.execute(harness.request)

    assert result.output is None
    assert result.aborted is False
    if main_is_transient:
        from forwin.generation.pipeline_core.common import TransientLLMChapterFailure

        assert isinstance(result.error, TransientLLMChapterFailure)
        assert result.error.cause is preview_error
        assert result.error.__cause__ is preview_error
    else:
        assert result.error is preview_error
    with pytest.raises(type(result.error)) as raised:
        result.unwrap()
    assert raised.value is result.error
    assert result.frozen_artifacts == (
        ("artifact://writer-failed.json",) if hard_floor else ()
    )
    assert harness.store.frozen == (
        [
            {
                "project_id": "project-writer-owner",
                "chapter_number": 7,
                "payload": {
                    "reason": "writer-failed-without-draft",
                    "chapter_number": 7,
                    "project_id": "project-writer-owner",
                    "error": "HTTP 503 preview unavailable",
                },
            }
        ]
        if hard_floor
        else []
    )
    started = _events(harness, DecisionEventType.WRITER_PREVIEW_FALLBACK_STARTED)[0]
    failed = _events(harness, DecisionEventType.WRITER_PREVIEW_FALLBACK_FAILED)[0]
    assert failed.info.parent_event_id == started.id
    assert failed.info.payload["source_error_class"] == type(main_error).__name__
    assert failed.info.payload["error_class"] == "RuntimeError"


def test_empty_frozen_artifact_path_is_not_returned_as_an_artifact():
    error = ValueError("preview invalid")
    harness = _harness(
        [ValueError("main invalid")],
        [error],
        retries=1,
        artifact_store=_ArtifactStore(path=""),
    )

    result = harness.owner.execute(harness.request)

    assert result.error is error
    assert result.frozen_artifacts == ()
    assert len(harness.store.frozen) == 1


def test_frozen_artifact_failure_is_not_replaced_by_a_writer_failure():
    artifact_error = OSError("artifact storage unavailable")
    harness = _harness(
        [RuntimeError("HTTP 503")],
        [ValueError("preview invalid")],
        retries=1,
        artifact_store=_ArtifactStore(error=artifact_error),
    )

    result = harness.owner.execute(harness.request)

    assert result.error is artifact_error
    assert result.output is None
    assert result.aborted is False
    assert result.frozen_artifacts == ()
    with pytest.raises(OSError) as raised:
        result.unwrap()
    assert raised.value is artifact_error


@pytest.mark.parametrize("abort_after_first_attempt", [False, True])
def test_abort_returns_no_output_without_preview_or_frozen_artifact(
    abort_after_first_attempt,
):
    decisions = iter([False, True] if abort_after_first_attempt else [True])
    harness = _harness(
        [ValueError("invalid draft")],
        retries=2,
        should_abort=lambda: next(decisions),
    )

    result = harness.owner.execute(harness.request)

    assert result.output is None
    assert result.unwrap() is None
    assert result.error is None
    assert result.aborted is True
    assert result.frozen_artifacts == ()
    assert harness.store.frozen == []
    assert [call[0] for call in harness.writer.calls] == (
        ["main"] if abort_after_first_attempt else []
    )
    assert _events(harness, DecisionEventType.WRITER_PREVIEW_FALLBACK_STARTED) == []


def test_abort_predicate_failure_does_not_cancel_the_writer():
    def broken_predicate():
        raise RuntimeError("task state temporarily unreadable")

    output = _output()
    harness = _harness([output], should_abort=broken_predicate)

    result = harness.owner.execute(harness.request)

    assert result.unwrap() is output
    assert result.aborted is False
    assert result.error is None


@pytest.mark.parametrize("error", [KeyboardInterrupt(), SystemExit(2)])
def test_process_control_exceptions_propagate_without_fallback_or_freeze(error):
    harness = _harness([error])

    with pytest.raises(type(error)) as raised:
        harness.owner.execute(harness.request)

    assert raised.value is error
    assert [call[0] for call in harness.writer.calls] == ["main"]
    assert harness.store.frozen == []
    assert _events(harness, DecisionEventType.LLM_REQUEST_FAILED) == []


def test_first_recorded_event_becomes_the_root_for_later_writer_events():
    output = _output()
    harness = _harness([output], task_id="", root_event_id="")

    result = harness.owner.execute(harness.request)

    assert result.unwrap() is output
    first = harness.updater.events[0]
    for row in harness.updater.events[1:]:
        assert row.info.causal_root_id == first.id
        assert row.info.payload["operation_id"] == first.id


def test_zero_retry_budget_still_runs_one_writer_attempt_with_a_narrow_signature():
    output = _output()
    harness = _harness([], retries=0)
    contexts = []

    def write_chapter(context):
        contexts.append(context)
        return output

    harness.writer.write_chapter = write_chapter

    result = harness.owner.execute(harness.request)

    assert result.unwrap() is output
    assert contexts == [harness.request.context]
    started = _events(harness, DecisionEventType.LLM_REQUEST_STARTED)
    assert len(started) == 1
    assert started[0].info.payload["max_attempts"] == 1


def _terminal_input_error(kind):
    import httpx
    from forwin.writer.llm.errors import LLMInputLimitError
    from forwin.retrieval.requirements import RequiredContextError
    from forwin.retrieval.source_identity import CanonBaselineChanged

    if kind == "input_limit":
        return LLMInputLimitError(
            "input has 150029 tokens; limit 50000", response=httpx.Response(400)
        )
    if kind == "required_context":
        return RequiredContextError("required_context: ambiguous scene participant")
    return CanonBaselineChanged("Canon baseline changed")


@pytest.mark.parametrize(
    "kind", ["input_limit", "required_context", "canon_baseline_changed"]
)
@pytest.mark.parametrize("prior_transient", [False, True])
def test_terminal_input_failure_cannot_retry_preview_or_lose_identity(
    monkeypatch, kind, prior_transient
):
    error = _terminal_input_error(kind)
    sleeps = []
    monkeypatch.setattr("time.sleep", sleeps.append)
    main = ([RuntimeError("HTTP 503 unavailable")] if prior_transient else []) + [
        error
    ] * 3
    harness = _harness(main, [_output(mode="writer_preview")], retries=3)
    result = harness.owner.execute(harness.request)
    assert result.output is None
    assert result.error is error
    with pytest.raises(type(error)) as raised:
        result.unwrap()
    assert raised.value is error
    assert [call[0] for call in harness.writer.calls] == ["main"] * (
        2 if prior_transient else 1
    )
    assert sleeps == ([3.0] if prior_transient else [])
    assert not _events(harness, DecisionEventType.WRITER_PREVIEW_FALLBACK_STARTED)
    failure = _events(harness, DecisionEventType.LLM_REQUEST_FAILED)[-1]
    assert failure.info.payload["error_category"] == kind
    assert failure.info.payload["is_transient"] is False
    assert result.frozen_artifacts == ("artifact://writer-failed.json",)
    assert harness.store.frozen[0]["payload"]["error"] == str(error)


@pytest.mark.parametrize(
    "kind", ["input_limit", "required_context", "canon_baseline_changed"]
)
def test_terminal_preview_failure_retains_identity_after_transient_main(kind):
    error = _terminal_input_error(kind)
    harness = _harness([RuntimeError("HTTP 503 unavailable")], [error], retries=1)
    result = harness.owner.execute(harness.request)
    assert result.output is None
    assert result.error is error
    assert [call[0] for call in harness.writer.calls] == ["main", "preview"]
    failure = _events(harness, DecisionEventType.WRITER_PREVIEW_FALLBACK_FAILED)[-1]
    assert failure.info.payload["error_category"] == kind
