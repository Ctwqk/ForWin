from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from forwin.config import InfrastructureConfig
from forwin.runtime.container import RuntimeContainer
from forwin.planning.amend import PlanAmender
from forwin.planning.plan_step import PlanStepService
from forwin.runtime.policy import RuntimePolicy
from forwin.runtime.policy_store import ProjectPolicyStore


def _container(role):
    infrastructure = InfrastructureConfig(
        minimax_api_key="default-test-key",
        llm_env_profiles=[{
            "id": "project-model", "name": "Project model", "api_key": "project-test-key",
            "base_url": "https://example.invalid/v1", "model": "project-model",
        }],
    )
    policy = RuntimePolicy.for_profile("standard")
    container = RuntimeContainer.from_config(infrastructure, policy=policy, role=role)
    container._core_services = SimpleNamespace(
        infrastructure=infrastructure, policy=policy, artifact_store=None,
        session_factory=lambda: None, engine=SimpleNamespace(dispose=lambda: None),
    )
    return container


def _session(policy):
    project = SimpleNamespace(runtime_policy_json=policy.model_dump_json(), runtime_policy_version=1)
    return SimpleNamespace(get=lambda *_args: project), project


@pytest.mark.parametrize("profile,ceiling", [("standard", 3), ("pulp", 2)])
def test_http_amender_selects_the_stored_project_policy(monkeypatch, profile, ceiling):
    from forwin.http import app as app_module
    from forwin.http.runtime import HttpRuntime

    container = _container("api")
    policy = RuntimePolicy.for_profile(profile, model_profile_id="project-model")
    session, project = _session(policy)
    stored = ProjectPolicyStore(session).load(project).policy

    def amend(amender, actual_session, **kwargs):
        assert actual_session is session
        assert kwargs["policy"] == stored
        assert amender.band_planner.trope_cost_ceiling == ceiling
        assert amender.band_planner.llm_client.model == "project-model"
        assert amender.beat_sheet_service.llm_client is amender.band_planner.llm_client
        return "amended"

    monkeypatch.setattr(PlanAmender, "amend", amend)
    captured = []
    monkeypatch.setattr(app_module, "register_api_routes", lambda app, *, deps: captured.append(deps) or {})
    app_module.create_app(HttpRuntime(container=container))
    try:
        amender = captured[0].core.provide_plan_amender()
        assert amender.amend(session, conflict=object(), decision=object(), policy=stored) == "amended"
        assert container._generation_services is None
    finally:
        container.close()


def test_outbox_plan_step_selects_each_projects_current_policy(monkeypatch):
    container = _container("outbox_worker")
    seen = []

    def run(step, session, commit, *, policy):
        seen.append((policy.quality_profile, step.band_planner.trope_cost_ceiling,
                     step.band_planner.llm_client.model))
        return {"planned_band_id": commit.project_id}

    monkeypatch.setattr(PlanStepService, "run", run)
    try:
        maintenance = container._provide_outbox_post_canon_maintenance()
        for profile, model_id in (("standard", "project-model"), ("pulp", "project-model"), ("standard", "")):
            policy = RuntimePolicy.for_profile(profile, model_profile_id=model_id)
            session, _project = _session(policy)
            commit = SimpleNamespace(project_id=profile)
            assert maintenance._run_plan_step(session, commit) == {"planned_band_id": profile}
        assert seen == [("standard", 3, "project-model"), ("pulp", 2, "project-model"),
                        ("standard", 3, "")]
        assert container._generation_services is None
    finally:
        container.close()


@pytest.mark.parametrize("profiles", [("standard", "pulp"), ("pulp", "standard")])
def test_lazy_plan_step_does_not_reuse_the_first_projects_ceiling(monkeypatch, profiles):
    from forwin.maintenance.post_canon import PostCanonMaintenanceService

    seen = []

    def run(step, session, commit, *, policy):
        seen.append(step.band_planner.trope_cost_ceiling)
        return {}

    monkeypatch.setattr(PlanStepService, "run", run)
    maintenance = PostCanonMaintenanceService(
        session_factory=lambda: None, comment_analyzer=None, artifact_store=None, llm_client=None,
    )
    for profile in profiles:
        session, _project = _session(RuntimePolicy.for_profile(profile))
        maintenance._run_plan_step(session, SimpleNamespace(project_id=profile))
    assert seen == [2 if profile == "pulp" else 3 for profile in profiles]
    assert maintenance.plan_step is None


def test_project_planning_clients_follow_policy_changes_and_close_once(monkeypatch):
    container = _container("api")
    clients = []

    def build_client(_infrastructure, policy):
        client = Mock(model=policy.model_profile_id)
        clients.append(client)
        return client

    monkeypatch.setattr(RuntimeContainer, "_build_llm_client", staticmethod(build_client))
    first_policy = RuntimePolicy.for_profile("pulp", model_profile_id="project-model")
    changed_policy = RuntimePolicy.for_profile("standard")
    first = container._project_planning_llm_client(first_policy)
    assert container._project_planning_llm_client(first_policy) is first
    changed = container._project_planning_llm_client(changed_policy)
    assert changed is not first
    assert len(clients) == 2
    container.close()
    container.close()
    for client in clients:
        client.close.assert_called_once()
    with pytest.raises(RuntimeError, match="closed"):
        container._project_planning_llm_client(first_policy)


def test_project_policy_without_a_model_keeps_planning_off(monkeypatch):
    container = RuntimeContainer.for_api(
        InfrastructureConfig(minimax_api_key=""),
        policy=RuntimePolicy.for_profile("standard"),
    )
    build_client = Mock(side_effect=AssertionError("no client should be constructed"))
    monkeypatch.setattr(RuntimeContainer, "_build_llm_client", staticmethod(build_client))
    assert container._project_planning_llm_client(RuntimePolicy.for_profile("pulp")) is None
    build_client.assert_not_called()
    container.close()
