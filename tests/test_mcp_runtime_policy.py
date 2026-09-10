"""Canonical policy over MCP.

FastMCP compresses titles and additionalProperties:false when publishing tool
schemas. Assert public fields/types/limits/enums separately from actual rejection
of extra fields by the unchanged Pydantic/API owner; do not patch SDK schemas.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from sqlalchemy import select

from forwin.api_schema.policy import RuntimePolicyResponse, RuntimePolicyUpdateRequest
from forwin.config import InfrastructureConfig
from forwin.mcp.client import ForWinAPIClient
from forwin.mcp.http import build_mcp_server
from forwin.models.audit import DecisionEvent
from forwin.models.base import get_engine, get_session_factory
from forwin.runtime.policy import RuntimePolicy
from forwin.state.updater import StateUpdater
from tests.http_runtime_harness import HttpRuntimeHarness
from tests.postgres import postgres_test_url


def call(server, name, arguments):
    async def run():
        async with Client(server) as client:
            result = await client.call_tool(name, arguments)
            return result.structured_content

    return asyncio.run(run())


def tools(server):
    async def run():
        async with Client(server) as client:
            return {tool.name: tool for tool in await client.list_tools()}

    return asyncio.run(run())


@pytest.fixture
def runtime():
    url = postgres_test_url("mcp-runtime-policy")
    engine = get_engine(url)
    factory = get_session_factory(engine)
    policy = RuntimePolicy.for_profile(
        "standard", model_profile_id="env-kimi"
    ).with_user_settings(
        min_chars=2600,
        target_chars=3000,
        max_chars=3500,
        review_interval_chapters=7,
    )
    # Preserve a valid, non-default internal choice as well as user settings.
    policy = policy.model_copy(update={"writer_attention_retries": 2})
    with factory.begin() as session:
        project = StateUpdater(session).create_project(
            title="Isolated MCP policy test",
            premise="No generation or publishing",
            genre="test",
            runtime_policy=policy,
        )
        project_id = project.id
    app = HttpRuntimeHarness(
        session_factory=factory,
        engine=engine,
        config=InfrastructureConfig(
            database_url=url,
            minimax_api_key="must-not-appear-in-policy",
            minimax_base_url="http://example.invalid",
        ),
    )
    api_client = ForWinAPIClient(
        base_url="http://forwin.test", transport=httpx.ASGITransport(app=app.app)
    )
    yield SimpleNamespace(
        project_id=project_id,
        policy=policy,
        factory=factory,
        client=api_client,
        server=build_mcp_server(api_client=api_client),
    )
    engine.dispose()


def read(runtime):
    return RuntimePolicyResponse.model_validate(
        call(
            runtime.server,
            "project_get_runtime_policy",
            {"project_id": runtime.project_id},
        )
    )


def request_for(policy, version=1, **changes):
    request = {
        "expected_version": version,
        "quality_profile": policy.quality_profile,
        "model_profile_id": policy.model_profile_id,
        "min_chapter_chars": policy.chapter_length.min_chars,
        "target_chapter_chars": policy.chapter_length.target_chars,
        "max_chapter_chars": policy.chapter_length.max_chars,
        "review_interval_chapters": policy.pause.review_interval_chapters,
        "manual_checkpoints": policy.pause.manual_checkpoints,
        "band_checkpoint_action": policy.pause.band_checkpoint_action,
        "gate_delegate": policy.pause.gate_delegate,
        "reason": "isolated acceptance run policy",
    }
    return {**request, **changes}


def update(runtime, request):
    return RuntimePolicyResponse.model_validate(
        call(
            runtime.server,
            "project_update_runtime_policy",
            {
                "project_id": runtime.project_id,
                "request": request,
            },
        )
    )


def policy_events(runtime):
    with runtime.factory() as session:
        return list(
            session.scalars(
                select(DecisionEvent).where(
                    DecisionEvent.project_id == runtime.project_id,
                    DecisionEvent.event_type == "runtime_policy_updated",
                )
            )
        )


def resolve(schema, field):
    value = schema["properties"][field]
    if "$ref" in value:
        return schema["$defs"][value["$ref"].rsplit("/", 1)[-1]]
    return value


def test_mcp_exposes_canonical_policy_request_and_complete_output_schema():
    catalog = tools(
        build_mcp_server(api_client=ForWinAPIClient(base_url="http://forwin.invalid"))
    )
    assert {
        "project_get_runtime_policy",
        "project_update_runtime_policy",
    } <= catalog.keys()
    get = catalog["project_get_runtime_policy"]
    put = catalog["project_update_runtime_policy"]
    assert get.annotations.readOnlyHint is True
    assert put.annotations.readOnlyHint is False
    request = resolve(put.inputSchema, "request")
    canonical = RuntimePolicyUpdateRequest.model_json_schema()
    # FastMCP omits presentation titles, but must retain every API constraint.
    canonical_properties = {
        name: {key: value for key, value in field.items() if key != "title"}
        for name, field in canonical["properties"].items()
    }
    assert request["properties"] == canonical_properties
    assert request["required"] == canonical["required"]
    for tool in (get, put):
        assert {"policy", "version", "project_id"} <= tool.outputSchema[
            "properties"
        ].keys()
        assert (
            resolve(tool.outputSchema, "policy")["properties"].keys()
            == RuntimePolicy.model_fields.keys()
        )
        assert "api_key" not in json.dumps(tool.outputSchema)


def test_get_returns_complete_stored_policy_and_version_without_credentials(runtime):
    result = read(runtime)
    assert result.project_id == runtime.project_id
    assert result.version == 1
    assert result.policy == runtime.policy
    assert "must-not-appear-in-policy" not in result.model_dump_json()
    client_result = asyncio.run(
        runtime.client.project_get_runtime_policy(project_id=runtime.project_id)
    )
    assert isinstance(client_result, RuntimePolicyResponse)
    assert client_result == result
    assert policy_events(runtime) == []


def test_update_pause_choices_preserves_other_policy_and_records_reason(runtime):
    changed = update(
        runtime,
        request_for(
            runtime.policy,
            manual_checkpoints=False,
            band_checkpoint_action="continue",
            gate_delegate="spark",
            reason="  isolated autonomous acceptance  ",
        ),
    )
    expected = runtime.policy.with_user_settings(
        manual_checkpoints=False,
        band_checkpoint_action="continue",
        gate_delegate="spark",
    )
    assert changed.version == 2
    assert changed.policy == expected
    assert read(runtime).policy == expected
    assert read(runtime).version == 2
    events = policy_events(runtime)
    assert len(events) == 1
    assert events[0].reason == "isolated autonomous acceptance"
    assert json.loads(events[0].payload_json) == {
        "previous_version": 1,
        "new_version": 2,
    }


def test_stale_expected_version_is_rejected_without_another_mutation_or_audit(runtime):
    saved = update(runtime, request_for(runtime.policy, gate_delegate="spark"))
    with pytest.raises(ToolError, match="409.*runtime policy version conflict"):
        update(runtime, request_for(runtime.policy, manual_checkpoints=False))
    assert read(runtime).version == saved.version == 2
    assert read(runtime).policy == saved.policy
    assert len(policy_events(runtime)) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"expected_version": 0},
        {"quality_profile": "premium"},
        {"band_checkpoint_action": "skip_all_checks"},
        {"gate_delegate": "unrestricted"},
        {"min_chapter_chars": 499},
        {"reason": "   "},
        {"quality_gate": "disabled"},
    ],
)
def test_invalid_update_is_rejected_by_existing_schema_and_owner(runtime, changes):
    with pytest.raises(ToolError):
        update(runtime, request_for(runtime.policy, **changes))
    assert read(runtime).version == 1
    assert read(runtime).policy == runtime.policy
    assert policy_events(runtime) == []


def test_existing_gate_delegate_tool_keeps_full_policy_choices(runtime):
    result = call(
        runtime.server,
        "project_set_gate_delegate",
        {
            "project_id": runtime.project_id,
            "delegate": "spark",
            "reason": "existing delegate compatibility",
        },
    )
    assert result["ok"] is True
    assert result["project"]["gate_delegate"] == "spark"
    assert read(runtime).policy == runtime.policy.with_user_settings(
        gate_delegate="spark"
    )
    assert len(policy_events(runtime)) == 1
