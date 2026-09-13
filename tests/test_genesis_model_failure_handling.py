"""Formal Genesis failures cannot replace locked content or claim success."""

import json
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from forwin.api_schema import BookGenesisRefineRequest
from forwin.application.projects import genesis as application
from forwin.genesis import GENESIS_STAGE_ORDER
from forwin.models import Project
from forwin.models.audit import DecisionEvent
from forwin.models.base import get_engine, get_session_factory
from forwin.models.genesis import BookGenesisRevision, PromptTrace
from forwin.runtime.policy import RuntimePolicy
from forwin.runtime.policy_store import ProjectPolicyStore
from forwin.state.updater import StateUpdater
from tests.postgres import postgres_test_url
from tests.test_genesis_map_completeness import complete_atlas, model_service
from tests.test_genesis_map_failure_handling import unavailable_service

FAILURES = ["transport", "invalid_json", "empty_response", "missing_credentials"]
TARGETS = {
    "brief": "one_line",
    "world": "world_bible.overview",
    "map": "edges[0]",
    "story_engine": "core_cast[0]",
    "book_blueprint": "arcs[0]",
    "bootstrap": "start_policy",
}


@pytest.fixture
def locked_workspace():
    engine = get_engine(postgres_test_url("genesis-model-failure"))
    factory = get_session_factory(engine)
    service = model_service({})
    with factory() as session:
        project = Project(
            id="authored", title="河谷", premise="陆砚调查旧档", genre="悬疑"
        )
        session.add(project)
        session.flush()
        ProjectPolicyStore(session).initialize(
            project, RuntimePolicy.for_profile("standard")
        )
        updater = StateUpdater(session)
        revision = service.create_initial_revision(
            session=session, updater=updater, project=project
        )
        pack = service.load_pack(revision)
        pack["book_brief"]["one_line"] = "陆砚调查旧档"
        pack["world"]["world_bible"] = {"overview": "河谷旧档"}
        pack["world"]["map_atlas"] = complete_atlas()
        pack["world"]["story_engine"] = {
            "core_cast": [{"id": "lu-yan", "name": "陆砚"}]
        }
        pack["book_arc_blueprint"] = {"arcs": [{"id": "arc-1", "title": "旧档"}]}
        pack["execution_bootstrap"] = {"start_policy": "从河谷开始"}
        trace = updater.save_prompt_trace(
            project_id=project.id,
            genesis_revision_id=revision.id,
            stage_key="story_engine",
            attempts_json='[{"status":"success"}]',
            output_summary_json='{"mode":"success","payload":{"name":"陆砚"}}',
        )
        for state in pack["stage_states"].values():
            state["last_trace_id"] = trace.id
        revision.pack_json = json.dumps(pack, ensure_ascii=False)
        for stage in GENESIS_STAGE_ORDER:
            revision = service.lock_stage(
                session=session,
                updater=updater,
                project=project,
                revision=revision,
                stage_key=stage,
            )
        session.commit()
    try:
        yield factory
    finally:
        engine.dispose()


def snapshot(session):
    """Read persisted identities and payloads, including immutable old revisions."""
    project = session.get(Project, "authored")
    return {
        "project": (project.active_genesis_revision_id, project.creation_status),
        "revisions": sorted(
            (row.id, row.pack_json, row.status)
            for row in session.scalars(select(BookGenesisRevision))
        ),
        "events": sorted(row.id for row in session.scalars(select(DecisionEvent))),
        "traces": sorted(
            (row.id, row.attempts_json, row.output_summary_json)
            for row in session.scalars(select(PromptTrace))
        ),
    }


@pytest.mark.parametrize("stage", GENESIS_STAGE_ORDER)
@pytest.mark.parametrize("failure", FAILURES)
@pytest.mark.parametrize("operation", ["generate", "refine", "refine_item"])
def test_failed_formal_operation_preserves_locked_workspace(
    locked_workspace, stage, failure, operation
):
    service = unavailable_service(failure)
    with locked_workspace() as session:
        before = snapshot(session)
        project = session.get(Project, "authored")
        revision = service.active_revision(session, project)
        assert service.load_pack(revision)["stage_states"][stage]["locked"] is True
        common = {
            "session": session,
            "updater": StateUpdater(session),
            "project": project,
            "revision": revision,
            "stage_key": stage,
        }
        with pytest.raises(RuntimeError, match="Genesis") as caught:
            if operation == "generate":
                service.generate_stage(**common)
            else:
                service.refine_stage(
                    **common,
                    instruction="更新原稿",
                    target_path=TARGETS[stage] if operation == "refine_item" else "",
                )
        if failure != "missing_credentials":
            assert caught.value.__cause__ is not None
        # Deliberately commit: a rollback must not mask writes before failure.
        session.commit()
        assert snapshot(session) == before


@pytest.mark.parametrize("stage", GENESIS_STAGE_ORDER)
@pytest.mark.parametrize("failure", FAILURES)
@pytest.mark.parametrize("operation", ["generate", "rerun", "refine", "refine_item"])
def test_application_reports_model_failure_without_persisting_success(
    locked_workspace, stage, failure, operation
):
    service = unavailable_service(failure)
    with locked_workspace() as session:
        before = snapshot(session)
    kwargs = {
        "get_session": locked_workspace,
        "build_genesis_service": lambda **kwargs: service,
        "close_genesis_service": lambda service: None,
        "require_genesis_project": lambda project: None,
        "active_genesis_revision": service.active_revision,
    }
    with pytest.raises(HTTPException) as caught:
        if operation in {"generate", "rerun"}:
            getattr(application, f"{operation}_project_genesis_stage")(
                "authored", stage, **kwargs
            )
        else:
            application.refine_project_genesis_stage(
                "authored",
                stage,
                BookGenesisRefineRequest(
                    instruction="更新原稿",
                    target_path=TARGETS[stage] if operation == "refine_item" else "",
                ),
                **kwargs,
            )
    assert caught.value.status_code == 503
    assert "Genesis" in caught.value.detail
    assert caught.value.__cause__ is not None
    with locked_workspace() as session:
        assert snapshot(session) == before


@pytest.mark.parametrize("stage", GENESIS_STAGE_ORDER)
@pytest.mark.parametrize("suffix", ["", ":refine", ":refine_item"])
def test_valid_model_object_is_still_accepted(stage, suffix):
    source = complete_atlas() if stage == "map" else {"authored": "保留模型内容"}
    service = model_service(source)
    service.llm_client.api_key = ""
    service.llm_client.codex_enabled = True
    payload, trace = service._call_json_with_trace(
        messages=[], fallback={"wrong": "替代内容"}, stage_key=stage + suffix
    )
    assert payload == source
    assert trace["output_summary"]["mode"] == "success"


@pytest.mark.parametrize("stage", ["launch_arc_1", "launch_arc_2"])
@pytest.mark.parametrize("failure", FAILURES)
def test_internal_materialization_retains_existing_fallback(stage, failure):
    payload, trace = unavailable_service(failure)._call_json_with_trace(
        messages=[], fallback={"internal": "plan"}, stage_key=stage
    )
    assert payload == {"internal": "plan"}
    assert trace["output_summary"]["mode"] == "fallback"


@pytest.mark.parametrize(
    "failure,expected_calls",
    [
        ("transport", 2),
        ("invalid_json", 2),
        ("empty_response", 1),
        ("missing_credentials", 0),
    ],
)
def test_formal_failure_keeps_existing_bounded_attempt_count(failure, expected_calls):
    service = unavailable_service(failure)
    with (
        patch.object(service.llm_client, "chat", wraps=service.llm_client.chat) as chat,
        pytest.raises(RuntimeError, match="Genesis"),
    ):
        service._call_json_with_trace(
            messages=[], fallback={}, stage_key="story_engine"
        )
    assert chat.call_count == expected_calls


@pytest.mark.parametrize("error_type", [RuntimeError, TypeError, ValueError])
def test_trace_programming_error_is_not_retried_as_model_failure(error_type):
    service = model_service({"one_line": "有效输出"})
    with (
        patch.object(
            service, "_trace_payload", side_effect=error_type("trace programming bug")
        ),
        patch.object(service.llm_client, "chat", wraps=service.llm_client.chat) as chat,
        pytest.raises(error_type, match="trace programming bug"),
    ):
        service._call_json_with_trace(messages=[], fallback={}, stage_key="brief")
    assert chat.call_count == 1


@pytest.mark.parametrize("error_type", [TypeError, RuntimeError])
def test_client_programming_error_is_not_reclassified_or_retried(error_type):
    service = model_service({})
    with (
        patch.object(
            service.llm_client, "chat", side_effect=error_type("client programming bug")
        ) as chat,
        pytest.raises(error_type, match="client programming bug"),
    ):
        service._call_json_with_trace(messages=[], fallback={}, stage_key="brief")
    assert chat.call_count == 1


@pytest.mark.parametrize("failure", ["http_status", "input_limit"])
def test_provider_failures_remain_identifiable_and_keep_their_cause(failure):
    import httpx

    from forwin.genesis.constants import GenesisModelError
    from forwin.writer.llm.errors import LLMInputLimitError

    response = httpx.Response(
        429, request=httpx.Request("POST", "https://model.invalid/chat")
    )
    error = (
        httpx.HTTPStatusError(
            "provider quota exhausted", request=response.request, response=response
        )
        if failure == "http_status"
        else LLMInputLimitError("provider input limit exceeded", response=response)
    )
    service = model_service({})
    with (
        patch.object(service.llm_client, "chat", side_effect=error) as chat,
        pytest.raises(GenesisModelError, match="model call failed") as caught,
    ):
        service._call_json_with_trace(
            messages=[], fallback={}, stage_key="story_engine"
        )
    assert caught.value.__cause__ is error
    assert chat.call_count == 2


@pytest.mark.parametrize("stage", GENESIS_STAGE_ORDER)
@pytest.mark.parametrize("operation", ["generate", "rerun", "refine", "refine_item"])
@pytest.mark.parametrize(
    "failure", ["invalid_http_json", "missing_content", "empty_choices"]
)
def test_adapter_protocol_failure_is_application_model_parse_failure(
    locked_workspace, stage, operation, failure
):
    import httpx

    from forwin.genesis import BookGenesisService
    from forwin.writer.llm import LLMClient

    client = LLMClient(
        api_key="test",
        base_url="https://api.deepseek.com/v1",
        model="deepseek-chat",
        retry_attempts=2,
        retry_initial_delay_seconds=0,
        retry_max_delay_seconds=0,
    )
    service = BookGenesisService(llm_client=client)
    cause_type = {
        "invalid_http_json": json.JSONDecodeError,
        "missing_content": KeyError,
        "empty_choices": IndexError,
    }[failure]

    def respond(url, **kwargs):
        request = httpx.Request("POST", url)
        if failure == "invalid_http_json":
            return httpx.Response(200, text="invalid HTTP JSON", request=request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {}}]}
            if failure == "missing_content"
            else {"choices": []},
            request=request,
        )

    with locked_workspace() as session:
        before = snapshot(session)
    kwargs = {
        "get_session": locked_workspace,
        "build_genesis_service": lambda **kwargs: service,
        "close_genesis_service": lambda service: None,
        "require_genesis_project": lambda project: None,
        "active_genesis_revision": service.active_revision,
    }
    try:
        with (
            patch.object(client.client, "post", side_effect=respond) as post,
            pytest.raises(HTTPException) as caught,
        ):
            if operation in {"generate", "rerun"}:
                getattr(application, f"{operation}_project_genesis_stage")(
                    "authored", stage, **kwargs
                )
            else:
                application.refine_project_genesis_stage(
                    "authored",
                    stage,
                    BookGenesisRefineRequest(
                        instruction="更新",
                        target_path=TARGETS[stage]
                        if operation == "refine_item"
                        else "",
                    ),
                    **kwargs,
                )
        assert caught.value.status_code == 503
        assert "parsing failed" in caught.value.detail
        provider_error = caught.value.__cause__.__cause__
        assert provider_error.error_category == "parse_error"
        assert isinstance(provider_error.__cause__, cause_type)
        assert getattr(provider_error, "_forwin_llm_attempt_recorded", False)
        assert getattr(provider_error.__cause__, "_forwin_llm_attempt_recorded", False)
        # Two bounded Genesis attempts, no adapter retries or duplicate records.
        assert post.call_count == 2
        attempts = client.drain_llm_attempt_events()
        assert len(attempts) == 2
        assert all(row["error_class"] == cause_type.__name__ for row in attempts)
        assert all(row["error_category"] == "parse_error" for row in attempts)
        assert all(row["final_failure"] and not row["retryable"] for row in attempts)
        assert client.drain_model_fallback_events() == []
        with locked_workspace() as session:
            assert snapshot(session) == before
    finally:
        client.close()


@pytest.mark.parametrize("operation", ["generate", "rerun", "refine", "refine_item"])
def test_adapter_programming_runtime_error_propagates_once_through_application(
    locked_workspace, operation
):
    from forwin.genesis import BookGenesisService
    from forwin.writer.llm import LLMClient

    client = LLMClient(
        api_key="test",
        base_url="https://api.deepseek.com/v1",
        model="deepseek-chat",
        retry_attempts=2,
        retry_initial_delay_seconds=0,
    )
    service = BookGenesisService(llm_client=client)
    error = RuntimeError("client programming bug")
    with locked_workspace() as session:
        before = snapshot(session)
    kwargs = {
        "get_session": locked_workspace,
        "build_genesis_service": lambda **kwargs: service,
        "close_genesis_service": lambda service: None,
        "require_genesis_project": lambda project: None,
        "active_genesis_revision": service.active_revision,
    }
    try:
        with (
            patch.object(client.client, "post", side_effect=error) as post,
            pytest.raises(RuntimeError, match="client programming bug") as caught,
        ):
            if operation in {"generate", "rerun"}:
                getattr(application, f"{operation}_project_genesis_stage")(
                    "authored", "story_engine", **kwargs
                )
            else:
                application.refine_project_genesis_stage(
                    "authored",
                    "story_engine",
                    BookGenesisRefineRequest(
                        instruction="更新",
                        target_path="core_cast[0]"
                        if operation == "refine_item"
                        else "",
                    ),
                    **kwargs,
                )
        assert caught.value is error
        assert post.call_count == 1
        assert len(client.drain_llm_attempt_events()) == 1
        with locked_workspace() as session:
            assert snapshot(session) == before
    finally:
        client.close()


def test_adapter_unavailable_profile_remains_application_failure(locked_workspace):
    from forwin.genesis import BookGenesisService
    from forwin.writer.llm import LLMClient
    from forwin.writer.llm.errors import LLMUnavailableError

    # A configured key with no model reaches the real no-usable-profile owner.
    client = LLMClient(api_key="test", base_url="https://model.invalid/v1", model="")
    service = BookGenesisService(llm_client=client)
    with locked_workspace() as session:
        before = snapshot(session)
    try:
        with (
            patch.object(
                client.client,
                "post",
                side_effect=AssertionError("no usable profile must not send HTTP"),
            ),
            pytest.raises(HTTPException) as caught,
        ):
            application.generate_project_genesis_stage(
                "authored",
                "story_engine",
                get_session=locked_workspace,
                build_genesis_service=lambda **kwargs: service,
                close_genesis_service=lambda service: None,
                require_genesis_project=lambda project: None,
                active_genesis_revision=service.active_revision,
            )
        assert caught.value.status_code == 503
        assert isinstance(caught.value.__cause__.__cause__, LLMUnavailableError)
        attempts = client.drain_llm_attempt_events()
        assert len(attempts) == 2
        assert all(row["error_class"] == "NoUsableLLMProfile" for row in attempts)
        assert all(row["error_category"] == "no_usable_profile" for row in attempts)
        with locked_workspace() as session:
            assert snapshot(session) == before
    finally:
        client.close()
