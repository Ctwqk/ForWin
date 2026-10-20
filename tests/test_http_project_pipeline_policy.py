from __future__ import annotations

from dataclasses import fields
from types import SimpleNamespace

from forwin.api_schema import ChapterReviewApproveRequest
from forwin.application.projects.service import (
    ProjectApplicationDeps,
    ProjectApplicationService,
)
from forwin.config import InfrastructureConfig
from forwin.http import project_pipelines
from forwin.http.runtime import HttpRuntime
from forwin.runtime.policy import RuntimePolicy


class _Session:
    def __init__(self, projects: dict[str, object]) -> None:
        self.projects = projects

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def get(self, _model, project_id: str):
        return self.projects.get(project_id)

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


class _Container:
    def __init__(self, policy: RuntimePolicy, closed: list[str]) -> None:
        self.policy = policy
        self.closed = closed

    def build_chapter_pipeline(self):
        return SimpleNamespace(
            policy=self.policy,
            arc_envelope_manager=SimpleNamespace(
                backfill_missing_resolutions=lambda *, session: 0
            ),
        )

    def close(self) -> None:
        self.closed.append(self.policy.quality_profile)


def _project(project_id: str, policy: RuntimePolicy):
    return SimpleNamespace(
        id=project_id,
        runtime_policy_json=policy.model_dump_json(),
        runtime_policy_version=1,
    )


def test_project_pipeline_runs_under_the_projects_runtime_policy(monkeypatch) -> None:
    standard = RuntimePolicy.for_profile("standard")
    pulp = RuntimePolicy.for_profile("pulp")
    closed: list[str] = []
    built: list[str] = []

    def for_api(_config, *, policy: RuntimePolicy) -> _Container:
        built.append(policy.quality_profile)
        return _Container(policy, closed)

    monkeypatch.setattr(
        project_pipelines, "RuntimeContainer", SimpleNamespace(for_api=for_api)
    )
    monkeypatch.setattr(
        "forwin.http.automation.stop_automation_scheduler", lambda _runtime: None
    )
    projects = {"std": _project("std", standard), "pulp": _project("pulp", pulp)}
    runtime = HttpRuntime(
        config=InfrastructureConfig(minimax_api_key=""),
        container=_Container(standard, closed),  # type: ignore[arg-type]
        session_factory=lambda: _Session(projects),  # type: ignore[arg-type]
    )

    shared = runtime.get_pipeline()
    assert runtime.get_project_pipeline("std") is shared
    assert runtime.get_project_pipeline("missing") is shared

    pulp_pipeline = runtime.get_project_pipeline("pulp")
    assert pulp_pipeline.policy == pulp
    assert runtime.get_project_pipeline("pulp") is pulp_pipeline
    assert built == ["pulp"]

    runtime.shutdown()

    assert sorted(closed) == ["pulp", "standard"]


def test_chapter_review_approval_uses_the_project_pipeline() -> None:
    requested: list[str] = []
    pipeline = SimpleNamespace(
        accept_review=lambda project_id, chapter_number, **_kwargs: {
            "status": "accepted",
            "message": "ok",
            "frozen_artifact": "",
        }
    )

    def unused(*_args, **_kwargs):
        raise AssertionError("unexpected dependency call")

    overrides = {
        "get_config": lambda: object(),
        "get_project_pipeline": lambda project_id: requested.append(project_id)
        or pipeline,
        "require_reason": lambda reason, *, action: reason,
    }
    deps = ProjectApplicationDeps(
        **{item.name: overrides.get(item.name, unused) for item in fields(ProjectApplicationDeps)}
    )

    response = ProjectApplicationService(deps).approve_chapter_review(
        "pulp-project",
        3,
        ChapterReviewApproveRequest(reason="looks good", continue_generation=False),
    )

    assert requested == ["pulp-project"]
    assert response.status == "accepted"
