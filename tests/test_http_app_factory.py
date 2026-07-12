from __future__ import annotations

from pathlib import Path

from forwin.http import HttpRuntime, create_app


ROOT = Path(__file__).resolve().parents[1]


def _route_contract(app) -> set[tuple[str, tuple[str, ...]]]:
    return {
        (route.path, tuple(sorted(route.methods or ())))
        for route in app.routes
        if hasattr(route, "path")
    }


def test_create_app_owns_isolated_runtime_state() -> None:
    first_runtime = HttpRuntime()
    second_runtime = HttpRuntime()

    first = create_app(runtime=first_runtime)
    second = create_app(runtime=second_runtime)

    assert first.state.forwin_runtime is first_runtime
    assert second.state.forwin_runtime is second_runtime
    assert first_runtime is not second_runtime
    assert first_runtime.tasks is not second_runtime.tasks
    assert first_runtime.tasks_lock is not second_runtime.tasks_lock
    assert first_runtime.automation_stop is not second_runtime.automation_stop
    assert _route_contract(first) == _route_contract(second)


def test_http_contract_has_one_project_generation_workflow() -> None:
    app = create_app(runtime=HttpRuntime())
    paths = {route.path for route in app.routes if hasattr(route, "path")}

    assert "/api/generate" not in paths
    assert "/api/projects" in paths
    assert "/api/projects/{project_id}/start-writing" in paths
    assert "/api/projects/{project_id}/continue-generation" in paths


def test_cli_does_not_construct_or_run_chapter_pipeline() -> None:
    source = (ROOT / "forwin/cli.py").read_text(encoding="utf-8")

    assert "RuntimeContainer" not in source
    assert "build_chapter_pipeline" not in source
    assert "pipeline.run(" not in source


def test_legacy_api_core_namespace_is_deleted() -> None:
    assert not (ROOT / "forwin/api_core").exists()
