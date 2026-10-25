from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from forwin.config import InfrastructureConfig, ModelProfileConfig
from forwin.runtime import container as runtime_container
from forwin.runtime import policy as runtime_policy
from forwin.writer.llm import LLMClient, OpenAICompatibleAdapter


def profiles():
    return {
        f"env-{name}": ModelProfileConfig(
            id=f"env-{name}", name=name, api_key=f"{name}-key",
            base_url=f"https://{name}.example/v1", model=f"{name}-model",
        )
        for name in ("deepseek", "kimi", "minimax")
    }


@pytest.fixture
def make_client():
    clients = []

    def make(script=None, *, configured=None, pin="", chains=None):
        client = LLMClient(
            profiles=profiles() if configured is None else configured,
            chains=runtime_policy.DEFAULT_LLM_CHAINS if chains is None else chains,
            pinned_profile_id=pin, retry_attempts=1,
            retry_initial_delay_seconds=0,
        )
        client.client.close()
        calls = []

        def respond(request):
            name = request.url.host.split(".")[0]
            calls.append(name)
            status, content = (script or {}).get(name, (200, "ok"))
            return httpx.Response(status, json={"choices": [{"message": {"content": content}}]})

        client.client = httpx.Client(transport=httpx.MockTransport(respond))
        clients.append(client)
        return client, calls

    yield make
    for client in clients:
        client.close()


def test_review_route_tries_deepseek_then_kimi_then_minimax(make_client):
    client, calls = make_client({"deepseek": (503, "unavailable")})
    meta = client.chat_with_meta([], task_family="review")
    assert meta.content == "ok"
    assert meta.profile_id == "env-kimi"
    assert meta.model == "kimi-model"
    assert calls == ["deepseek", "kimi"]
    assert [event["profile_id"] for event in client.llm_attempt_events] == ["env-deepseek", "env-kimi"]


def test_gate_resolves_on_whichever_chain_profile_answers(make_client):
    from forwin.generation.gate_delegation import LLMGateDelegate
    from tests.test_gate_delegation import (
        RecordingUpdater, _approval_json, _delegation_request, _recording_writer,
    )

    client, calls = make_client({"deepseek": (503, "unavailable"), "kimi": (200, _approval_json())})
    client.profiles["env-kimi"] = client.profiles["env-kimi"].model_copy(update={"model": "kimi-k2.5"})
    updater = RecordingUpdater()
    outcome = LLMGateDelegate(llm_client=client).resolve(
        request=_delegation_request(), audit_writer=_recording_writer(updater),
    )
    assert outcome.approved
    assert calls == ["deepseek", "kimi"]
    decided = next(event for event in updater.events if event.event_type == "gate_delegation_decided")
    assert decided.payload["profile_id"] == "env-kimi"
    assert decided.payload["model"] == "kimi-k2.5"
    trace = updater.traces[0]
    assert trace["stage_key"] == trace["template_id"] == "pause_gate"
    assert trace["fallback_used"] is True
    assert "permission_profile" not in trace
    assert "codex_job_id" not in trace


@pytest.mark.parametrize("content", ["not-json", '{"decision":"approve"}'])
def test_gate_parse_failure_pauses_for_human(make_client, content):
    from forwin.generation.gate_delegation import LLMGateDelegate
    from tests.test_gate_delegation import RecordingUpdater, _delegation_request, _recording_writer

    client, calls = make_client({"deepseek": (200, content)})
    updater = RecordingUpdater()
    outcome = LLMGateDelegate(llm_client=client).resolve(
        request=_delegation_request(), audit_writer=_recording_writer(updater),
    )
    assert not outcome.resolved and not outcome.approved
    assert outcome.failure_reason == "parse_or_schema"
    assert calls == ["deepseek"]
    assert updater.events[-1].event_type == "gate_delegation_failed"
    attempts = json.loads(updater.traces[0]["attempts_json"])
    assert attempts[-1]["parse_ok"] is False


@pytest.mark.parametrize("option, escalates", [("veto_world", False), ("amend_plan", True)])
def test_world_conflict_review_uses_chain_fallback(make_client, option, escalates):
    from forwin.models.world_conflict import WorldPlanConflictRow
    from forwin.world_engine.conflict_delegate import WorldConflictDelegate
    from tests.test_world_conflict_delegate import RecordingUpdater, _answer

    client, calls = make_client({"deepseek": (503, "unavailable"), "kimi": (200, _answer(option))})
    conflict = WorldPlanConflictRow(
        id="conflict-1", project_id="project-1", source="world_tick", detected_after_chapter=3,
        options_json='["veto_world", "amend_plan"]',
    )
    updater = RecordingUpdater()
    outcome = WorldConflictDelegate(client).decide(
        conflict=conflict, context_snapshot={}, policy=runtime_policy.RuntimePolicy.for_profile("standard"),
        audit_writer=updater.writer(),
    )
    assert outcome.option == option
    assert outcome.escalate is escalates
    assert calls == ["deepseek", "kimi"]
    assert updater.events[0].payload["profile_id"] == "env-kimi"
    assert updater.events[0].payload["model"] == "kimi-model"
    if escalates:
        assert updater.events[-1].event_type == "world_conflict_escalated"
        assert updater.events[-1].payload["profile_id"] == "env-kimi"


def test_attempt_annotations_stay_with_the_calling_thread(make_client):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from forwin.observability.llm_trace import (
        mark_latest_attempt_parse_failure, mark_latest_attempt_workflow,
    )

    client, _ = make_client()
    first_done, second_done = Event(), Event()

    def first():
        client.chat([], task_family="review", stage_key="same_stage")
        first_done.set()
        assert second_done.wait(5)
        mark_latest_attempt_parse_failure(client, parser_name="first", stage_key="same_stage", error="invalid")
        mark_latest_attempt_workflow(client, attempt_no=2, stage_key="same_stage")
        return client.llm_attempt_group_id, client.drain_llm_attempt_events()[0]

    def second():
        assert first_done.wait(5)
        client.chat([], task_family="review", stage_key="same_stage")
        second_done.set()
        return client.llm_attempt_group_id, client.drain_llm_attempt_events()[0]

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future, second_future = pool.submit(first), pool.submit(second)
        (first_group, first_attempt), (second_group, second_attempt) = first_future.result(), second_future.result()
    assert first_group != second_group
    assert client.llm_attempt_group_id == ""
    assert client.llm_attempt_events == []
    assert first_attempt["attempt_group_id"] == first_group
    assert first_attempt["parse_ok"] is False
    assert first_attempt["workflow_attempt_no"] == 2
    assert second_attempt["attempt_group_id"] == second_group
    assert "parse_ok" not in second_attempt
    assert "workflow_attempt_no" not in second_attempt


def test_prose_route_starts_with_kimi_and_never_uses_minimax(make_client):
    client, _ = make_client()
    client.chat([], stage_key="chapter_draft")
    assert [item["profile_id"] for item in client.llm_attempt_events[0]["candidate_chain"]] == ["env-kimi", "env-deepseek"]


def test_world_tick_route_is_deepseek_first(make_client):
    client, calls = make_client()
    client.chat([], task_family="world_engine", stage_key="world.adjudicate")
    assert calls == ["deepseek"]


@pytest.mark.parametrize("status", [401, 402, 403, 404, 408, 409, 425, 429, 500, 501, 503, 529])
def test_http_402_falls_back_with_auth_or_balance_reason(make_client, status):
    client, calls = make_client({"deepseek": (status, "failed")})
    assert client.chat([], task_family="review") == "ok"
    assert calls == ["deepseek", "kimi"]
    if status in {401, 402, 403}:
        assert client.llm_attempt_events[0]["error_category"] == "auth_or_balance"
    elif status == 404:
        assert client.llm_attempt_events[0]["error_category"] == "model_not_found"


def test_http_400_does_not_fall_back(make_client):
    client, calls = make_client({"deepseek": (400, "bad request")})
    with pytest.raises(httpx.HTTPStatusError):
        client.chat([], task_family="review")
    assert calls == ["deepseek"]


@pytest.mark.parametrize("content", ["", "  ", None])
def test_empty_completion_falls_back(make_client, content):
    client, calls = make_client({"deepseek": (200, content)})
    assert client.chat([], task_family="review") == "ok"
    assert calls == ["deepseek", "kimi"]
    assert client.llm_attempt_events[0]["error_category"] == "empty_response"


def test_unconfigured_profile_is_skipped_and_recorded(make_client):
    configured = profiles()
    del configured["env-kimi"]
    client, _ = make_client(configured=configured)
    client.chat([], task_family="review")
    assert {"profile_id": "env-kimi", "reason": "profile_unconfigured"} in client.llm_attempt_events[0]["skipped_profiles"]


def test_no_configured_profile_names_the_route(make_client):
    client, calls = make_client(configured={})
    with pytest.raises(RuntimeError, match="no configured LLM profile for route review_json"):
        client.chat([], task_family="review")
    assert calls == []


def test_pinned_profile_goes_first_on_every_route(make_client):
    client, calls = make_client(pin="env-minimax")
    client.chat([], stage_key="chapter_draft")
    assert calls == ["minimax"]
    client.chat([], task_family="review")
    assert [item["profile_id"] for item in client.llm_attempt_events[-1]["candidate_chain"]] == ["env-minimax", "env-deepseek", "env-kimi"]


def test_llm_configured_true_with_only_deepseek_key():
    config = InfrastructureConfig(llm_env_profiles=[profiles()["env-deepseek"].model_dump()])
    assert runtime_container.llm_configured(config, runtime_policy.RuntimePolicy.for_profile("standard"))
    assert config.resolve_model_profile("") is None


@pytest.mark.parametrize("field", ["api_key", "base_url", "model"])
@pytest.mark.parametrize("value", ["", "   "])
def test_availability_and_routing_agree_on_unconfigured_profiles(make_client, field, value):
    profile = profiles()["env-deepseek"].model_dump()
    profile[field] = value
    config = InfrastructureConfig(llm_env_profiles=[profile])
    policy = runtime_policy.RuntimePolicy.for_profile("standard")
    assert not runtime_container.llm_configured(config, policy)
    client, calls = make_client(configured=config.llm_profiles)
    with pytest.raises(RuntimeError, match="no configured LLM profile for route review_json"):
        client.chat([], task_family="review")
    assert calls == []


def test_cover_client_uses_minimax_env_not_pinned_profile(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = InfrastructureConfig(
        minimax_api_key="cover-key", minimax_base_url="https://cover.example/v1",
        llm_env_profiles=[profiles()["env-deepseek"].model_dump()],
    )
    core = SimpleNamespace(
        infrastructure=config, session_factory=None, observability=None,
        policy=runtime_policy.RuntimePolicy.for_profile("standard", model_profile_id="env-deepseek"),
    )
    service = runtime_container.RuntimeContainer._build_publisher_runtime(None, core)
    image_client = service.cover_service.image_client
    assert image_client.api_key == "cover-key"
    assert image_client.base_url == "https://cover.example/v1"


def test_no_caller_pins_a_provider_or_model():
    violations = []
    for path in Path("forwin").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg in {"preferred_provider_kind", "preferred_model", "llm_preferred_provider_kind", "llm_preferred_model"}:
                    violations.append(f"{path}:{node.lineno}:{kw.arg}")
                if (kw.arg == "model" and isinstance(kw.value, ast.Constant)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr in {"chat", "chat_with_meta"}):
                    violations.append(f"{path}:{node.lineno}:model")
    assert violations == []


def _chat_call_site_violations(path: Path, source: str) -> list[str]:
    if path.is_relative_to(Path("forwin/writer/llm")):
        return []
    tree = ast.parse(source)
    violations = []
    injected_receivers = {"llm_client", "self.llm_client"}
    if path == Path("forwin/llm_eval/runner.py"):
        # The eval-local adapter must come from the injected adapter factory.
        for node in ast.walk(tree):
            if (isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "adapter" for target in node.targets
            ) and isinstance(node.value, ast.Call)
                    and ast.unparse(node.value.func) == "self.adapter_factory"):
                injected_receivers.add("adapter")
    for node in ast.walk(tree):
        # Covers httpx/requests/session POSTs even when the URL is assigned
        # separately from the call. No completion endpoint belongs in a caller.
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and "chat/completions" in node.value:
            violations.append(f"{path}:{node.lineno}:direct completion endpoint")
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"chat", "chat_with_meta"}):
            receiver = ast.unparse(node.func.value)
            if receiver not in injected_receivers:
                violations.append(f"{path}:{node.lineno}:non-injected receiver {receiver}")
    return violations


@pytest.mark.parametrize("source", [
    "provider.chat(messages)",
    "self.provider.chat_with_meta(messages)",
    "httpx.post('https://api.example/v1/chat/completions', json=payload)",
    "url = 'https://api.example/v1/chat/completions'\nrequests.post(url, json=payload)",
])
def test_chat_call_site_scan_rejects_bypasses(source):
    assert _chat_call_site_violations(Path("forwin/example.py"), source)


def test_chat_call_site_scan_accepts_injected_clients():
    assert _chat_call_site_violations(
        Path("forwin/example.py"),
        "llm_client.chat(messages)\nself.llm_client.chat_with_meta(messages)",
    ) == []


def test_every_chat_call_goes_through_llm_client_chain():
    config = InfrastructureConfig(llm_env_profiles=[profiles()["env-deepseek"].model_dump()])
    client = runtime_container.RuntimeContainer._build_llm_client(config, runtime_policy.RuntimePolicy.for_profile("standard"))
    try:
        assert isinstance(client, OpenAICompatibleAdapter)
        assert client.chains["review_json"][0] == "env-deepseek"
    finally:
        client.close()
    violations = []
    for path in Path("forwin").rglob("*.py"):
        violations.extend(_chat_call_site_violations(path, path.read_text()))
    assert violations == []


def test_embed_does_not_depend_on_a_pinned_profile():
    config = InfrastructureConfig(
        embedding_api_key="embedding-key", embedding_base_url="https://embedding.example/v1",
        embedding_model="embed-model", llm_env_profiles=[],
    )
    client = runtime_container.RuntimeContainer._build_llm_client(config, runtime_policy.RuntimePolicy.for_profile("standard"))
    client.client.close()
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2]}]})

    client.client = httpx.Client(transport=httpx.MockTransport(respond))
    try:
        assert client.embed(["text"]) == [[0.1, 0.2]]
        assert str(requests[0].url) == "https://embedding.example/v1/embeddings"
        assert requests[0].headers["Authorization"] == "Bearer embedding-key"
        assert json.loads(requests[0].content)["model"] == "embed-model"
    finally:
        client.close()


def test_chain_order_is_policy_data(make_client):
    client, calls = make_client(chains={"default": ("env-minimax", "env-deepseek")})
    assert client.chat([], task_family="review") == "ok"
    assert calls == ["minimax"]


@pytest.mark.parametrize(
    "stage_key, task_family, json_output, expected",
    [
        ("chapter_review", "review", True, ["env-deepseek", "env-kimi", "env-minimax"]),
        ("comment_analysis", "phase4", True, ["env-deepseek", "env-kimi", "env-minimax"]),
        ("scene_breakdown", "writer", True, ["env-deepseek", "env-kimi", "env-minimax"]),
        ("brief", "genesis", False, ["env-deepseek", "env-kimi", "env-minimax"]),
        ("chapter_delta_extraction", "writer", True, ["env-deepseek", "env-kimi"]),
        ("world.adjudicate", "world_engine", True, ["env-deepseek", "env-kimi"]),
        ("unknown", "", False, ["env-deepseek", "env-kimi"]),
        ("chapter_draft", "writer", False, ["env-kimi", "env-deepseek"]),
        ("chapter_rewrite", "writer", False, ["env-kimi", "env-deepseek"]),
        ("writer_preview", "writer", False, ["env-kimi", "env-deepseek", "env-minimax"]),
    ],
)
def test_default_route_chains(make_client, stage_key, task_family, json_output, expected):
    client, _ = make_client()
    client.chat([], stage_key=stage_key, task_family=task_family,
                response_format={"type": "json_object"} if json_output else None)
    event = client.llm_attempt_events[0]
    assert [item["profile_id"] for item in event["candidate_chain"]] == expected
    assert event["route_policy_version"] == "v5.0-policy-chains"


def test_prose_falls_back_to_deepseek_and_stops_before_minimax(make_client):
    client, calls = make_client({"kimi": (429, "rate limited"), "deepseek": (503, "unavailable")})
    with pytest.raises(httpx.HTTPStatusError):
        client.chat([], stage_key="chapter_draft")
    assert calls == ["kimi", "deepseek"]


def test_review_exhausts_chain_through_minimax(make_client):
    client, calls = make_client({"deepseek": (402, "balance"), "kimi": (503, "unavailable")})
    assert client.chat_with_meta([], task_family="review").profile_id == "env-minimax"
    assert calls == ["deepseek", "kimi", "minimax"]


@pytest.mark.parametrize("payload", [{}, {"choices": []}, {"choices": [{}]}, {"choices": [{"message": {}}]}])
def test_missing_completion_falls_back(make_client, payload):
    client, _ = make_client()
    client.client.close()
    calls = []

    def respond(request):
        calls.append(request.url.host)
        return httpx.Response(200, json=payload if len(calls) == 1 else {"choices": [{"message": {"content": "ok"}}]})

    client.client = httpx.Client(transport=httpx.MockTransport(respond))
    assert client.chat([], task_family="review") == "ok"
    assert calls == ["deepseek.example", "kimi.example"]
    assert client.llm_attempt_events[0]["error_category"] == "empty_response"


def test_invalid_json_does_not_fall_back(make_client):
    client, _ = make_client()
    client.client.close()
    calls = []

    def respond(request):
        calls.append(request.url.host)
        return httpx.Response(200, content="not json")

    client.client = httpx.Client(transport=httpx.MockTransport(respond))
    with pytest.raises(ValueError):
        client.chat([], task_family="review")
    assert calls == ["deepseek.example"]
    assert client.llm_attempt_events[0]["error_category"] == "parse_error"
    assert not client.llm_attempt_events[0]["fallback_eligible"]


def test_embedding_fallback_uses_first_configured_profile_not_pin(make_client):
    client, _ = make_client(pin="env-minimax")
    client.client.close()
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"data": [{"embedding": [1.0]}]})

    client.client = httpx.Client(transport=httpx.MockTransport(respond))
    assert client.embed(["text"], model="embedding-model") == [[1.0]]
    assert requests[0].url.host == "deepseek.example"
    assert requests[0].headers["Authorization"] == "Bearer deepseek-key"


def test_unconfigured_pin_does_not_block_generation_context():
    from forwin.generation.task_payload import build_execution_context, execution_payload

    policy = runtime_policy.RuntimePolicy.for_profile("standard", model_profile_id="env-kimi")
    config = InfrastructureConfig(llm_env_profiles=[profiles()["env-deepseek"].model_dump()])
    payload = execution_payload(mode="continue", policy=policy, policy_version=1)
    context = build_execution_context(config, payload, task_id="task")
    assert context.model_profile is None
    assert runtime_container.llm_configured(config, policy)


def test_genesis_uses_unpinned_chain_instead_of_scaffold(make_client):
    from forwin.genesis import BookGenesisService

    client, calls = make_client({"deepseek": (200, '{"value": "generated"}')})
    service = BookGenesisService(llm_client=client)
    payload, trace = service._call_json_with_trace(
        messages=[{"role": "user", "content": "brief"}],
        fallback={"value": "fallback"}, stage_key="brief",
    )
    assert payload == {"value": "generated"}
    assert calls == ["deepseek"]
    assert trace["attempts"][0]["profile_id"] == "env-deepseek"
    assert trace["model_profile"]["profile_id"] == "env-deepseek"
    assert trace["model_profile"]["model"] == "deepseek-model"


def test_unpinned_genesis_trace_records_served_fallback_profile(make_client):
    from forwin.genesis import BookGenesisService

    client, calls = make_client({"deepseek": (402, "balance"), "kimi": (200, '{"value": "generated"}')})
    payload, trace = BookGenesisService(llm_client=client)._call_json_with_trace(
        messages=[{"role": "user", "content": "brief"}],
        fallback={"value": "fallback"}, stage_key="brief",
    )
    assert payload == {"value": "generated"}
    assert calls == ["deepseek", "kimi"]
    assert trace["model_profile"]["profile_id"] == "env-kimi"
    assert trace["model_profile"]["model"] == "kimi-model"
    assert trace["model_profile"]["base_url"] == "https://kimi.example/v1"


def test_unpinned_writer_trace_records_served_fallback_profile(make_client):
    from forwin.writer.chapter_writer import ChapterWriter

    client, calls = make_client({"kimi": (402, "balance")})
    writer = ChapterWriter(client)
    assert writer._call_chat([], temperature=0.5, max_tokens=100) == "ok"
    trace = writer._build_prompt_trace(
        base_messages=[], skill_layers=[], template_id="writer:test",
        stage_key="chapter_draft", input_snapshot={}, output_summary={},
    )
    assert calls == ["kimi", "deepseek"]
    assert trace["model_profile"]["profile_id"] == "env-deepseek"
    assert trace["model_profile"]["model"] == "deepseek-model"
    assert trace["model_profile"]["base_url"] == "https://deepseek.example/v1"


def test_quality_gate_identity_uses_attempts_even_after_drain(make_client):
    from forwin.generation.pipeline_core.quality_gates import QualityDiagnosticsStage

    client, _ = make_client({"deepseek": (402, "balance")})
    stage = QualityDiagnosticsStage()
    stage.llm_client = client
    client.chat([], task_family="review")
    assert stage._current_model_identity() == ("env-kimi", "kimi-model")
    attempts = client.drain_llm_attempt_events()
    assert stage._current_model_identity(attempts) == ("env-kimi", "kimi-model")


@pytest.mark.parametrize("fails", [False, True])
def test_writer_decision_events_identify_actual_attempt_after_call(make_client, fails):
    from forwin.audit.events import DecisionEventType
    from forwin.generation.pipeline_core.quality_gates import QualityDiagnosticsStage
    from forwin.generation.pipeline_core.writer_attention import WriterExecutionStage
    from forwin.protocol.writer import WriterOutput
    from forwin.writer.chapter_writer import ChapterWriter

    client, _ = make_client({
        "kimi": (402, "balance"), "deepseek": (400 if fails else 200, "ok"),
    })
    writer = ChapterWriter(client)

    def write(context, **kwargs):
        body = writer._call_chat([], temperature=0.5, max_tokens=100)
        trace = writer._build_prompt_trace(
            base_messages=[], skill_layers=[], template_id="writer:test",
            stage_key="chapter_draft", input_snapshot={}, output_summary={},
        )
        return WriterOutput(
            project_id="project", chapter_number=1, title="Test", body=body,
            char_count=len(body), end_of_chapter_summary="Test",
            generation_meta={"prompt_trace": trace},
        )

    class Stage(WriterExecutionStage, QualityDiagnosticsStage):
        # Replace only external orchestration/persistence; calls, traces, event
        # construction, draining and identity resolution are production code.
        _select_skill_layers = staticmethod(lambda **kwargs: [])
        _abort_requested = staticmethod(lambda: False)
        _call_with_compatible_kwargs = staticmethod(lambda func, *args, **kwargs: func(*args, **kwargs))
        _record_model_fallback_payloads = staticmethod(lambda **kwargs: None)
        _is_transient_llm_like = staticmethod(lambda exc: False)
        _is_timeout_like = staticmethod(lambda exc: False)

        def _record_decision_event(self, **kwargs):
            self.events.append(kwargs)
            return SimpleNamespace(id=str(len(self.events)))

    stage = Stage()
    stage.llm_client = client
    stage.writer = SimpleNamespace(
        llm_client=client, write_chapter=write, write_preview_chapter=write,
        single_call_timeout_seconds=10,
    )
    stage.policy = SimpleNamespace(writer_attention_retries=1, canon=SimpleNamespace(hard_floor=False))
    stage._audit_task_id = "task"
    stage.events = []
    kwargs = dict(context=None, project_id="project", chapter_number=1,
                  updater=SimpleNamespace(), paused_chapters=[], frozen_artifacts=[])
    if fails:
        with pytest.raises(httpx.HTTPStatusError):
            stage._write_chapter_with_attention_fallback(**kwargs)
    else:
        assert stage._write_chapter_with_attention_fallback(**kwargs).body == "ok"
    event_type = DecisionEventType.LLM_REQUEST_FAILED if fails else DecisionEventType.LLM_REQUEST_SUCCEEDED
    payload = next(event["payload"] for event in stage.events if event["event_type"] == event_type)
    assert payload["model_profile_id"] == "env-deepseek"
    assert payload["model"] == "deepseek-model"
    started = next(event["payload"] for event in stage.events if event["event_type"] == DecisionEventType.LLM_REQUEST_STARTED)
    assert not started.get("model")  # A pre-call event cannot know the served model.


def test_cost_ledger_default_model_uses_served_attempt_over_stale_profile():
    from forwin.models.genesis import PromptTrace
    from forwin.observability.cost_ledger import CostLedgerService

    trace = PromptTrace(
        model_profile_json=json.dumps({"model": "stale-pinned-model"}),
        attempts_json=json.dumps([
            {"profile_id": "env-deepseek", "model": "deepseek-model", "http_status": 402},
            {"profile_id": "env-kimi", "model": "kimi-model", "http_status": 200},
        ]),
    )
    context = CostLedgerService._trace_context(
        trace, event_by_id={}, related_trace_event=None, explicit_trace_event=None,
        parent_context=None, candidate_by_review={},
    )
    assert context.default_model == "kimi-model"


def test_unpinned_extraction_trace_records_served_fallback_profile(make_client):
    from forwin.book_state.extraction.chapter_delta_extractor import ChapterDeltaExtractor
    from forwin.book_state.extraction.chapter_input import ChapterExtractionInput
    from forwin.book_state.lexicon import BookLexicon

    response = json.dumps({"body_sha256": "model-hash", "time": {"elapsed": None, "evidence": "A quiet day."}})
    client, _ = make_client({"deepseek": (402, "balance"), "kimi": (200, response)})
    extraction_input = ChapterExtractionInput(
        project_id="project", chapter_number=1, entities=(), locations=(),
        calendar_units=(), time_aliases=(), fulfillment_targets=(), intents=(),
        secrets=(), travel_modes=(),
    )
    result = ChapterDeltaExtractor(client).extract(
        extraction_input=extraction_input, chapter_title="Test", chapter_body="A quiet day.",
        chapter_summary="Quiet", calendar=None, lexicon=BookLexicon(),
    )
    assert result.extraction is not None
    assert result.prompt_trace["model_profile"]["profile_id"] == "env-kimi"
    assert result.prompt_trace["model_profile"]["model"] == "kimi-model"


def test_unpinned_entity_trace_records_served_fallback_profile(make_client):
    from forwin.naming.entity_registrar import LLMEntityAdmissionClassifier
    from forwin.protocol.writer import WriterOutput

    response = json.dumps({"decisions": [{
        "name": "蔡序", "decision": "register_character", "canonical_name": "蔡序",
        "aliases": [], "role_hint": "复核员",
    }]})
    client, _ = make_client({"deepseek": (402, "balance"), "kimi": (200, response)})
    traces = []
    classifier = LLMEntityAdmissionClassifier(client, save_trace=traces.append)
    decisions = classifier.classify(
        project_id="project", chapter_number=1, names=["蔡序"], existing_entities=[],
        writer_output=WriterOutput(
            project_id="project", chapter_number=1, title="Test", body="蔡序核对签名。",
            end_of_chapter_summary="核对签名。",
        ),
    )
    assert decisions
    assert traces[0]["model_profile"]["profile_id"] == "env-kimi"
    assert traces[0]["model_profile"]["model"] == "kimi-model"


def test_genesis_empty_chain_completion_exits_before_business_retry(make_client):
    from forwin.genesis import BookGenesisService

    client, calls = make_client({name: (200, "  ") for name in ("deepseek", "kimi", "minimax")})
    payload, trace = BookGenesisService(llm_client=client)._call_json_with_trace(
        messages=[], fallback={"value": "fallback"}, stage_key="brief",
    )
    assert payload == {"value": "fallback"}
    assert calls == ["deepseek", "kimi", "minimax"]
    business_attempts = trace["output_summary"]["business_attempts"]
    assert [item["status"] for item in business_attempts] == ["failed", "fallback"]


def test_default_llm_chains_match_brief_literal():
    assert runtime_policy.DEFAULT_LLM_CHAINS == {
        "review_json": ("env-deepseek", "env-kimi", "env-minimax"),
        "feedback_analysis": ("env-deepseek", "env-kimi", "env-minimax"),
        "planning_json_low_risk": ("env-deepseek", "env-kimi", "env-minimax"),
        "planning_prose": ("env-deepseek", "env-kimi", "env-minimax"),
        "canon_extraction": ("env-deepseek", "env-kimi"),
        "planning_json_general": ("env-deepseek", "env-kimi"),
        "general": ("env-deepseek", "env-kimi"),
        "default": ("env-deepseek", "env-kimi"),
        "prose_generation": ("env-kimi", "env-deepseek"),
        "repair_generation": ("env-kimi", "env-deepseek"),
        "writer_preview": ("env-kimi", "env-deepseek", "env-minimax"),
    }


def test_unknown_route_uses_default_chain(make_client, monkeypatch):
    client, calls = make_client(chains={"general": ("env-deepseek",), "default": ("env-minimax", "env-kimi")})
    monkeypatch.setattr(client, "_llm_task_route", lambda **kwargs: "future_route")
    assert client.chat_with_meta([]).profile_id == "env-minimax"
    assert calls == ["minimax"]
    assert [item["profile_id"] for item in client.llm_attempt_events[0]["candidate_chain"]] == ["env-minimax", "env-kimi"]


def test_llm_configured_ignores_profiles_outside_chains():
    policy = runtime_policy.RuntimePolicy.for_profile("standard")
    config = InfrastructureConfig(llm_env_profiles=[])
    assert not runtime_container.llm_configured(config, policy)
    config = InfrastructureConfig(minimax_api_key="minimax-key")
    custom = policy.model_copy(update={"models": runtime_policy.ModelsPolicy(chains={"default": ("env-deepseek",)})})
    assert not runtime_container.llm_configured(config, custom)
    assert runtime_container.llm_configured(config, custom.with_user_settings(model_profile_id="env-minimax"))


def test_eval_factory_uses_chain_for_every_profile():
    from forwin.llm_eval.runner import LLMReliabilityRunner
    from forwin.llm_eval.schemas import EvalProfile

    profile = EvalProfile(id="configured", base_url="https://provider.example/v1", model="configured-model", api_key="key", provider="openai_compatible")
    client = LLMReliabilityRunner._default_adapter_factory(profile)
    try:
        assert isinstance(client, OpenAICompatibleAdapter)
        assert client.chains == {"default": ("configured",)}
    finally:
        client.close()


def test_model_adapter_contract_has_no_call_time_provider_override():
    import inspect
    from forwin.model_adapter import ModelAdapter

    assert "preferred_provider_kind" not in inspect.signature(ModelAdapter.chat).parameters
    assert "preferred_model" not in inspect.signature(ModelAdapter.chat).parameters
