from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from forwin.application.projects import genesis
from forwin.api_schema.genesis import BookGenesisNameGenerateRequest, BookGenesisRefineRequest
from forwin.config import InfrastructureConfig
from forwin.http.runtime import HttpRuntime
from forwin.models.project import Project
from forwin.runtime.container import RuntimeContainer
from forwin.runtime.policy import RuntimePolicy


@pytest.fixture
def runtime(tmp_path):
    config = InfrastructureConfig(
        minimax_api_key="test-key", skill_runtime_enabled=False,
        artifact_backend="local", artifact_root=str(tmp_path / "artifacts"),
    )
    runtime = HttpRuntime(config=config)
    yield runtime
    runtime.shutdown()


def custom_policy():
    data = RuntimePolicy.for_profile("standard", model_profile_id="env-kimi").model_dump(mode="json")
    data["models"]["chains"]["planning_json_general"] = ["env-minimax", "env-kimi"]
    return RuntimePolicy.model_validate(data)


@pytest.mark.parametrize("path", ["project", "generation", "refinement", "name", "handoff"])
def test_project_genesis_preserves_chains_and_pin(runtime, monkeypatch, path):
    policy = custom_policy()
    project = Project(
        id="project-1", creation_status="genesis_ready",
        runtime_policy_json=policy.model_dump_json(), runtime_policy_version=3,
    )
    if path == "project":
        service = genesis._build_project_genesis_service(None, project, runtime.build_genesis_service)
    else:
        # Stop after the real factory constructs its client, before LLM/DB work.
        class FactoryReached(Exception):
            pass

        built = []

        def build(*args, **kwargs):
            built.append(runtime.build_genesis_service(*args, **kwargs))
            raise FactoryReached

        monkeypatch.setattr(genesis, "_lock_project_for_handoff", lambda *args: project)
        dependencies = dict(
            get_session=lambda: SimpleNamespace(get=lambda *args: project, close=lambda: None),
            build_genesis_service=build,
            close_genesis_service=runtime.close_genesis_service,
            require_genesis_project=lambda project: None,
            active_genesis_revision=lambda *args: SimpleNamespace(id="revision-1"),
        )
        with pytest.raises(FactoryReached):
            if path == "generation":
                genesis.generate_project_genesis_stage(project.id, "world", **dependencies)
            elif path == "refinement":
                genesis.refine_project_genesis_stage(
                    project.id, "world", BookGenesisRefineRequest(instruction="expand"),
                    **dependencies,
                )
            elif path == "name":
                genesis.generate_project_genesis_name(
                    project.id, BookGenesisNameGenerateRequest(stage_key="world"),
                    **dependencies,
                )
            else:
                genesis.start_project_writing(
                    project.id, config=runtime.config,
                    project_has_active_generation_task=lambda *args, **kwargs: False,
                    generation_task_conflict_message=lambda *args: "busy",
                    create_continue_generation_task=Mock(side_effect=AssertionError("no writes")),
                    **dependencies,
                )
        service = built[0]
    assert service.llm_client.chains["planning_json_general"] == ("env-minimax", "env-kimi")
    assert service.llm_client.pinned_profile_id == "env-kimi"


def test_genesis_service_cache_uses_full_policy_and_closes_at_shutdown(runtime, monkeypatch):
    policy = custom_policy()
    first = runtime.build_genesis_service(policy=policy)
    assert runtime.build_genesis_service(policy=RuntimePolicy.model_validate_json(policy.model_dump_json())) is first
    changed_chain = policy.model_dump(mode="json")
    changed_chain["models"]["chains"]["planning_json_general"] = ["env-kimi"]
    second = runtime.build_genesis_service(policy=RuntimePolicy.model_validate(changed_chain))
    assert second is not first
    changed_pause = policy.with_user_settings(review_interval_chapters=17)
    assert runtime.build_genesis_service(policy=changed_pause) is not first
    assert runtime.build_genesis_service(policy=policy.with_user_settings(model_profile_id="")) is not first
    close = Mock(wraps=first.llm_client.close)
    monkeypatch.setattr(first.llm_client, "close", close)
    runtime.close_genesis_service(first)
    close.assert_not_called()
    runtime.shutdown()
    close.assert_called_once()


def test_custom_unpinned_policy_does_not_reuse_default_container(runtime):
    policy = custom_policy().with_user_settings(model_profile_id="")
    runtime.container = RuntimeContainer.for_api(
        runtime.config, policy=RuntimePolicy.for_profile("standard")
    )
    service = runtime.build_genesis_service(policy=policy)
    assert service.llm_client.chains["planning_json_general"] == ("env-minimax", "env-kimi")
    assert service.llm_client.pinned_profile_id == ""
