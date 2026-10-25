from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from forwin.audit.events import DecisionEventType
from forwin.book_state.projection import BookStateProjection
from forwin.models.audit import DecisionEvent
from forwin.models.project import Project
from forwin.world_engine.conflict_repository import WorldConflictRepository
from tests.http_runtime_harness import HttpRuntimeHarness
from tests.test_world_conflict_resolution import _book, _conflict, _slip
from tests.test_world_gate_v6 import _session_factory


@pytest.fixture
def conflict_api(monkeypatch):
    engine, Session = _session_factory()
    try:
        book = _book(Session, monkeypatch)
        conflict_id = _conflict(
            Session, book, proposed_change=_slip(book, book.onstage),
            options=["veto_world", "rewrite_chapter"], intent_ref=book.onstage,
            actor_ref=book.world.hero,
        )
        client = TestClient(HttpRuntimeHarness(session_factory=Session, engine=engine).app)
        yield client, Session, book, conflict_id
        client.close()
    finally:
        engine.dispose()


def test_list_and_get_world_conflicts(conflict_api):
    client, Session, book, conflict_id = conflict_api
    root = f"/api/projects/{book.project_id}/world-conflicts"
    response = client.get(root, params={"status": "open"})
    assert response.status_code == 200
    row = response.json()[0]
    assert row["id"] == conflict_id
    assert row["involved_refs"] == [book.onstage, book.world.hero]
    assert row["proposed_change"] == _slip(book, book.onstage)
    assert row["decision"] == {} and row["result_refs"] == []
    assert row["blocks_from_chapter"] == 2 and row["story_time"] == book.now
    assert client.get(f"{root}/{conflict_id}").json() == row
    assert client.get(root, params={"status": "resolved"}).json() == []
    assert client.get(f"{root}/missing").status_code == 404
    with Session.begin() as session:
        other = Project(title="Other", premise="Other", genre="test")
        session.add(other)
        session.flush()
        other_id = other.id
    other_root = f"/api/projects/{other_id}/world-conflicts/{conflict_id}"
    assert client.get(other_root).status_code == 404
    assert client.post(f"{other_root}/resolve", json={
        "option": "veto_world", "rationale": "plan", "reason": "review",
    }).status_code == 404


def test_resolve_route_applies_option_and_records_human_decision(conflict_api):
    client, Session, book, conflict_id = conflict_api
    path = f"/api/projects/{book.project_id}/world-conflicts/{conflict_id}/resolve"
    payload = {"option": "veto_world", "rationale": "preserve plan", "reason": "human review"}
    assert client.post(path, json={k: v for k, v in payload.items() if k != "reason"}).status_code == 422
    assert client.post(path, json={**payload, "reason": " "}).status_code == 422
    response = client.post(path, json=payload)
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "resolved"
    assert result["decision"]["decided_by"] == "human"
    assert result["decision"]["option"] == "veto_world"
    assert result["decision"]["rationale"] == "preserve plan"
    assert result["decision"]["reason"] == "human review"
    assert result["decision"]["decided_at"]
    assert result["result_refs"]
    assert client.post(path, json=payload).status_code == 409
    with Session() as session:
        row = WorldConflictRepository(session).get(book.project_id, conflict_id)
        assert row.status == "resolved"
        runtime = BookStateProjection(session).load_runtime_as_of(book.project_id, as_of_chapter=1)
        assert runtime.world.get_state(book.onstage)["status"] == "vetoed"
        events = session.scalars(select(DecisionEvent).where(
            DecisionEvent.project_id == book.project_id,
            DecisionEvent.event_type == DecisionEventType.WORLD_CONFLICT_RESOLVED,
            DecisionEvent.related_object_id == conflict_id,
        )).all()
        assert len(events) == 1
        [event] = events
        assert event.actor_type == "manual_ui"
        assert event.reason == "human review"
        assert json.loads(event.payload_json)["rationale"] == "preserve plan"


@pytest.mark.parametrize("suffix", ["", "/missing"], ids=["list", "get"])
def test_unknown_project_list_and_get_return_404(conflict_api, suffix):
    client, _, _, _ = conflict_api
    assert client.get(f"/api/projects/unknown/world-conflicts{suffix}").status_code == 404


def test_resolve_route_returns_409_when_not_open(conflict_api):
    client, Session, book, conflict_id = conflict_api
    with Session.begin() as session:
        WorldConflictRepository(session).get(book.project_id, conflict_id).status = "expired"
    response = client.post(f"/api/projects/{book.project_id}/world-conflicts/{conflict_id}/resolve", json={
        "option": "veto_world", "rationale": "plan", "reason": "review",
    })
    assert response.status_code == 409


@pytest.mark.parametrize("option", ["plot_responds", "rewrite_chapter", "unknown"])
def test_resolve_route_returns_400_for_unavailable_option(conflict_api, option):
    client, Session, book, conflict_id = conflict_api
    response = client.post(f"/api/projects/{book.project_id}/world-conflicts/{conflict_id}/resolve", json={
        "option": option, "rationale": "plan", "reason": "review",
    })
    assert response.status_code == 400
    with Session() as session:
        assert WorldConflictRepository(session).get(book.project_id, conflict_id).status == "open"


@pytest.fixture
def mock_conflict_api(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from fastapi import FastAPI

    from forwin.http.adapters import api_world_conflict_routes as routes

    session = MagicMock()
    session.__enter__.return_value = session
    monkeypatch.setattr(routes, "require_project", lambda *_: SimpleNamespace(id="p1"))
    policy_store = MagicMock()
    repo = MagicMock()
    monkeypatch.setattr(routes, "ProjectPolicyStore", lambda _: policy_store)
    monkeypatch.setattr(routes, "WorldConflictRepository", lambda _: repo)
    service = MagicMock()
    monkeypatch.setattr(routes, "ConflictResolutionService", lambda *args, **kwargs: service)
    app = FastAPI()
    handlers = routes.build_handlers(get_session=lambda: session)
    app.add_api_route(
        "/api/projects/{project_id}/world-conflicts",
        handlers["list_project_world_conflicts"], methods=["GET"],
    )
    app.add_api_route(
        "/api/projects/{project_id}/world-conflicts/{conflict_id}/resolve",
        handlers["resolve_project_world_conflict"],
        methods=["POST"],
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, session, service, policy_store, repo


@pytest.mark.parametrize("error_name,status", [
    ("ConflictNotOpen", 409),
    ("ConflictStale", 409),
    ("ChapterNotAwaitingConflict", 409),
    ("ConflictOptionUnavailable", 400),
    ("ValueError", 400),
    ("LookupError", 500),
    ("KeyError", 500),
    ("IndexError", 500),
    ("JSONDecodeError", 500),
    ("ValidationError", 500),
])
def test_resolve_route_maps_service_errors_without_committing(mock_conflict_api, error_name, status):
    from pydantic import ValidationError

    from forwin.http.adapters import api_world_conflict_routes as routes

    client, session, service, _, _ = mock_conflict_api
    error_type = {
        "ValueError": ValueError, "LookupError": LookupError, "KeyError": KeyError, "IndexError": IndexError,
    }.get(error_name)
    if error_name == "JSONDecodeError":
        error = json.JSONDecodeError("invalid stored JSON", "", 0)
    elif error_name == "ValidationError":
        try:
            routes.ConflictDecision.model_validate({})
        except ValidationError as exc:
            error = exc
    else:
        error = (error_type or getattr(routes, error_name))("resolution refused")
    service.resolve.side_effect = error
    response = client.post("/api/projects/p1/world-conflicts/c1/resolve", json={
        "option": "veto_world", "rationale": "preserve plan", "reason": "human review",
    })
    assert response.status_code == status
    if status != 500:
        assert response.json()["detail"] == "resolution refused"
    session.commit.assert_not_called()
    decision = service.resolve.call_args.kwargs["decision"]
    assert decision.decided_by == "human"
    assert decision.actor_id == "human"
    assert decision.reason == "human review"


def test_resolve_route_rejects_invalid_decision_before_service(mock_conflict_api):
    client, session, service, _, _ = mock_conflict_api
    response = client.post("/api/projects/p1/world-conflicts/c1/resolve", json={
        "option": "unknown", "rationale": "preserve plan", "reason": "human review",
    })
    assert response.status_code == 400
    service.resolve.assert_not_called()
    session.commit.assert_not_called()


def test_resolve_route_maps_canon_write_failure_to_409(mock_conflict_api):
    from forwin.canon.admission import CanonWriteFailure

    client, session, service, _, _ = mock_conflict_api
    service.resolve.side_effect = CanonWriteFailure("held change violates current canon")
    response = client.post("/api/projects/p1/world-conflicts/c1/resolve", json={
        "option": "veto_world", "rationale": "preserve plan", "reason": "human review",
    })
    assert response.status_code == 409
    assert response.json()["detail"] == "canon refused the change: held change violates current canon"
    session.commit.assert_not_called()


def test_resolve_route_reports_missing_policy(mock_conflict_api):
    from forwin.runtime.policy_store import ProjectPolicyMissing

    client, session, service, policy_store, _ = mock_conflict_api
    policy_store.load.side_effect = ProjectPolicyMissing("p1")
    response = client.post("/api/projects/p1/world-conflicts/c1/resolve", json={
        "option": "veto_world", "rationale": "preserve plan", "reason": "human review",
    })
    assert response.status_code == 500
    assert response.json()["detail"] == "project runtime policy is missing"
    service.resolve.assert_not_called()
    session.commit.assert_not_called()


def test_list_route_accepts_expired(mock_conflict_api):
    from types import SimpleNamespace
    from forwin.api_schema.world_conflicts import WorldConflictInfo

    client, _, _, _, repo = mock_conflict_api
    info = WorldConflictInfo(
        id="c1", project_id="p1", source="world_tick", detected_after_chapter=3, story_time=0,
        violated="impact_review", impact_level="major", required_decider="human",
        blocks_from_chapter=4, status="expired", summary="Expired change", origin_key="tick-1",
    )
    values = info.model_dump()
    for field in ("involved_refs", "proposed_change", "options", "decision", "result_refs"):
        values[field + "_json"] = json.dumps(values.pop(field))
    repo.list.return_value = [SimpleNamespace(**values)]
    response = client.get("/api/projects/p1/world-conflicts", params={"status": "expired"})
    assert response.status_code == 200
    assert response.json()[0]["origin_key"] == "tick-1"
    assert response.json()[0]["status"] == "expired"
    repo.list.assert_called_once_with("p1", status="expired")


def test_mcp_list_preserves_expired_filter_and_origin():
    import asyncio
    import httpx
    from forwin.mcp.client import ForWinAPIClient

    def handler(request):
        assert request.url.params["status"] == "expired"
        return httpx.Response(200, json=[{
            "id": "c1", "project_id": "p1", "source": "world_tick", "origin_key": "tick-1",
            "detected_after_chapter": 3, "story_time": 0, "violated": "impact_review",
            "impact_level": "major", "required_decider": "human", "blocks_from_chapter": 4,
            "status": "expired", "summary": "Expired change",
        }])

    client = ForWinAPIClient(base_url="http://forwin.invalid", transport=httpx.MockTransport(handler))
    [row] = asyncio.run(client.world_conflict_list(project_id="p1", status="expired"))
    assert row.status == "expired" and row.origin_key == "tick-1"


@pytest.mark.parametrize("status", ["typo", ""])
def test_list_route_rejects_invalid_status(mock_conflict_api, status):
    client, _, _, _, repo = mock_conflict_api
    response = client.get("/api/projects/p1/world-conflicts", params={"status": status})
    assert response.status_code == 400
    repo.list.assert_not_called()


@pytest.mark.parametrize(("option", "handed"), [("amend_plan", True), ("veto_world", False)])
def test_resolve_route_hands_the_plan_amender_only_to_amend_plan(monkeypatch, option, handed):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from fastapi import FastAPI

    from forwin.http.adapters import api_world_conflict_routes as routes

    session = MagicMock()
    session.__enter__.return_value = session
    monkeypatch.setattr(routes, "require_project", lambda *_: SimpleNamespace(id="p1"))
    monkeypatch.setattr(routes, "ProjectPolicyStore", lambda _: MagicMock())
    monkeypatch.setattr(routes, "WorldConflictRepository", lambda _: MagicMock())
    from forwin.api_schema.world_conflicts import WorldConflictInfo
    from forwin.world_engine.conflict_resolution import ConflictResolutionResult

    monkeypatch.setattr(routes, "_conflict_info", lambda row: WorldConflictInfo(
        id="c1", project_id="p1", source="world_tick", detected_after_chapter=3, story_time=0, violated="impact_review",
        impact_level="major", required_decider="human", blocks_from_chapter=4, status="resolved", summary="s",
    ))
    constructed: list[dict] = []

    def service(*args, **kwargs):
        constructed.append(kwargs)
        resolved = MagicMock()
        resolved.resolve.return_value = ConflictResolutionResult(graph_delta_ids=(), obligation_id="", plan_patch_id="")
        return resolved

    monkeypatch.setattr(routes, "ConflictResolutionService", service)
    amender, builds = object(), []
    handlers = routes.build_handlers(get_session=lambda: session,
                                     get_plan_amender=lambda: builds.append(1) or amender)
    app = FastAPI()
    app.add_api_route("/resolve/{project_id}/{conflict_id}", handlers["resolve_project_world_conflict"],
                      methods=["POST"])
    with TestClient(app) as client:
        response = client.post("/resolve/p1/c1", json={"option": option, "rationale": "r", "reason": "why"})

    assert response.status_code == 200
    assert constructed[0]["plan_amender"] is (amender if handed else None)
    assert len(builds) == (1 if handed else 0)
    session.commit.assert_called_once()


@pytest.mark.parametrize("option", ["plot_responds", "accept_with_obligation"])
def test_response_without_next_plan_is_pending_not_escalated_over_http(conflict_api, option):
    from forwin.models import ChapterPlan, NarrativeObligationRow, NarrativePlanPatchRow
    from tests.test_world_conflict_resolution import _held

    client, Session, book, _ = conflict_api
    with Session.begin() as session:
        for number in (2, 3):
            session.get(ChapterPlan, book.plans[number]).status = "accepted"
    conflict_id = _conflict(Session, book, proposed_change=_held(book).model_dump(mode="json"))
    response = client.post(f"/api/projects/{book.project_id}/world-conflicts/{conflict_id}/resolve", json={
        "option": option, "rationale": "future response", "reason": "reviewed",
    })
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "resolved"
    with Session() as session:
        runtime = BookStateProjection(session).load_runtime_as_of(book.project_id, as_of_chapter=1)
        assert runtime.world.get_state(book.later)["status"] == ("held" if option == "plot_responds" else "resolved")
        assert session.scalars(select(NarrativePlanPatchRow).where(
            NarrativePlanPatchRow.project_id == book.project_id,
        )).all() == []
        if option == "plot_responds":
            assert response.json()["decision"]["pending_plan_response"] is True
        else:
            [obligation] = session.scalars(select(NarrativeObligationRow).where(
                NarrativeObligationRow.project_id == book.project_id,
            )).all()
            assert obligation.status == "active"


def test_resolve_route_reports_a_failed_amendment_beside_the_standing_resolution(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from fastapi import FastAPI

    from forwin.api_schema.world_conflicts import WorldConflictInfo
    from forwin.http.adapters import api_world_conflict_routes as routes
    from forwin.world_engine.conflict_resolution import ConflictResolutionResult

    session = MagicMock()
    session.__enter__.return_value = session
    monkeypatch.setattr(routes, "require_project", lambda *_: SimpleNamespace(id="p1"))
    monkeypatch.setattr(routes, "ProjectPolicyStore", lambda _: MagicMock())
    monkeypatch.setattr(routes, "WorldConflictRepository", lambda _: MagicMock())
    monkeypatch.setattr(routes, "_conflict_info", lambda row: WorldConflictInfo(
        id="c1", project_id="p1", source="world_tick", detected_after_chapter=3, story_time=0, violated="impact_review",
        impact_level="major", required_decider="human", blocks_from_chapter=4, status="resolved", summary="s",
    ))
    warning = {"code": "replan_failed", "message": "the resolution stands, but its chapters could not be planned"}
    service = MagicMock()
    service.resolve.return_value = ConflictResolutionResult(
        graph_delta_ids=("delta-1",), obligation_id="", plan_patch_id="", warnings=(warning,),
        followup_conflict_id="c2",
    )
    monkeypatch.setattr(routes, "ConflictResolutionService", lambda *args, **kwargs: service)
    handlers = routes.build_handlers(get_session=lambda: session, get_plan_amender=lambda: object())
    app = FastAPI()
    app.add_api_route("/resolve/{project_id}/{conflict_id}", handlers["resolve_project_world_conflict"],
                      methods=["POST"])
    with TestClient(app) as client:
        response = client.post("/resolve/p1/c1", json={"option": "amend_plan", "rationale": "r", "reason": "why"})

    assert response.status_code == 200
    body = response.json()
    assert (body["status"], body["warnings"], body["followup_conflict_id"]) == ("resolved", [warning], "c2")
    session.commit.assert_called_once()


def test_planless_obligation_budget_rejection_over_http_rolls_back(conflict_api):
    from forwin.models import ChapterPlan, NarrativeObligationRow
    from tests.test_world_conflict_resolution import _held

    client, Session, book, _ = conflict_api
    with Session.begin() as session:
        for number in (2, 3):
            session.get(ChapterPlan, book.plans[number]).status = "accepted"
        for index in range(3):
            session.add(NarrativeObligationRow(
                id=f"existing-{book.project_id}-{index}", project_id=book.project_id, origin_chapter_number=1,
                obligation_type="motivation_gap", priority="P1", status="active", summary="existing",
                hardness="design_debt", deadline_chapter=5, payoff_test="payoff",
            ))
    conflict_id = _conflict(Session, book, proposed_change=_held(book).model_dump(mode="json"))
    response = client.post(f"/api/projects/{book.project_id}/world-conflicts/{conflict_id}/resolve", json={
        "option": "accept_with_obligation", "rationale": "future response", "reason": "reviewed",
    })
    assert response.status_code == 400, response.text
    assert "obligation_budget_exceeded" in response.text
    with Session() as session:
        assert WorldConflictRepository(session).get(book.project_id, conflict_id).status == "open"
        assert len(session.scalars(select(NarrativeObligationRow).where(
            NarrativeObligationRow.project_id == book.project_id,
        )).all()) == 3
        runtime = BookStateProjection(session).load_runtime_as_of(book.project_id, as_of_chapter=1)
        assert runtime.world.get_state(book.later)["status"] == "planned"



def test_plot_responds_holds_the_intent_and_commits_no_world_change(conflict_api):
    from tests.test_world_conflict_resolution import _held, _resolution_deltas
    from tests.test_world_tick_delta import _patches

    client, Session, book, _ = conflict_api
    conflict_id = _conflict(Session, book, proposed_change=_held(book).model_dump(mode="json"))
    response = client.post(f"/api/projects/{book.project_id}/world-conflicts/{conflict_id}/resolve", json={
        "option": "plot_responds", "rationale": "respond first", "reason": "reviewed",
    })
    assert response.status_code == 200, response.text
    [delta] = _resolution_deltas(Session, book, conflict_id)
    assert _patches(delta) == [(book.later, "set", "state.status", "planned", "held")]
    assert not delta.fact_patches and not delta.cognition_patches
    assert all(str(patch.op) != "create" for patch in delta.node_patches)
    with Session() as session:
        from forwin.models.project import ChapterPlan
        bindings = json.loads(session.get(ChapterPlan, book.plans[2]).world_bindings_json)
        assert [(item["kind"], item["ref"]) for item in bindings] == [("intent", book.later)]



def test_http_refuses_second_plot_response_even_for_stale_offered_option(conflict_api):
    from tests.test_world_conflict_resolution import _held, _resolution_deltas

    client, Session, book, _ = conflict_api
    first, second = [_conflict(Session, book, proposed_change=_held(book).model_dump(mode="json")) for _ in range(2)]
    payload = {"option": "plot_responds", "rationale": "respond first", "reason": "reviewed"}
    root = f"/api/projects/{book.project_id}/world-conflicts"
    assert client.post(f"{root}/{first}/resolve", json=payload).status_code == 200
    assert client.post(f"{root}/{second}/resolve", json=payload).status_code == 400
    assert _resolution_deltas(Session, book, second) == []
    with Session() as session:
        assert WorldConflictRepository(session).get(book.project_id, second).status == "open"


def test_concurrent_http_plot_responses_consume_one_intent_once(conflict_api, monkeypatch):
    """Both conflict rows are locked before either session competes for the project lock."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Lock
    from sqlalchemy import text
    from forwin.models import NarrativePlanPatchRow
    from tests.test_world_conflict_resolution import _held, _resolution_deltas
    from tests.test_world_tick_delta import _patches

    client, Session, book, _ = conflict_api
    ids = [_conflict(Session, book, proposed_change=_held(book).model_dump(mode="json"),
                     violated=violation) for violation in ("impact_review", "onstage_slip")]
    barrier = Barrier(2, timeout=8)
    backend_ids = set()
    guard = Lock()
    original = WorldConflictRepository.get

    def lock_both(repo, project_id, conflict_id, *, for_update=False):
        if for_update and conflict_id in ids:
            repo.session.execute(text("SET LOCAL lock_timeout = '5s'"))
            repo.session.execute(text("SET LOCAL statement_timeout = '10s'"))
        row = original(repo, project_id, conflict_id, for_update=for_update)
        if for_update and conflict_id in ids:
            assert row.status == "open"
            assert "plot_responds" in json.loads(row.options_json)
            with guard:
                backend_ids.add(repo.session.scalar(text("SELECT pg_backend_pid()")))
            barrier.wait()
        return row

    monkeypatch.setattr(WorldConflictRepository, "get", lock_both)
    root = f"/api/projects/{book.project_id}/world-conflicts"
    def resolve(conflict_id):
        return client.post(f"{root}/{conflict_id}/resolve", json={
            "option": "plot_responds", "rationale": "respond first", "reason": "concurrent review",
        })

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(resolve, conflict_id) for conflict_id in ids]
        responses = [future.result(timeout=20) for future in futures]
    assert len(backend_ids) == 2
    assert sorted(response.status_code for response in responses) == [200, 400], [r.text for r in responses]
    winner, loser = sorted(zip(ids, responses), key=lambda pair: pair[1].status_code)
    [delta] = _resolution_deltas(Session, book, winner[0])
    assert _patches(delta) == [(book.later, "set", "state.status", "planned", "held")]
    assert not delta.fact_patches and not delta.cognition_patches
    assert _resolution_deltas(Session, book, loser[0]) == []
    with Session() as session:
        assert WorldConflictRepository(session).get(book.project_id, loser[0]).status == "open"
        patches = session.scalars(select(NarrativePlanPatchRow).where(
            NarrativePlanPatchRow.project_id == book.project_id,
            NarrativePlanPatchRow.patch_type == "world_conflict_response",
        )).all()
        assert len(patches) == 1
        resolved = session.scalars(select(DecisionEvent).where(
            DecisionEvent.project_id == book.project_id,
            DecisionEvent.event_type == DecisionEventType.WORLD_CONFLICT_RESOLVED,
        )).all()
        assert len(resolved) == 1


def test_http_response_between_tick_staging_and_snapshot_lock_rolls_back_tick(monkeypatch):
    """A staged response loses to committed HTTP canon, then retry cannot offer it again."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from sqlalchemy import text
    from forwin.book_state.repository import BookStateRepository
    from forwin.canon import CanonStaleVersion
    from forwin.config import InfrastructureConfig
    from forwin.models import NarrativePlanPatchRow
    from forwin.world_engine import tick as tick_module
    from forwin.world_engine.conflict_types import ConflictDraft
    from tests.test_world_conflict_resolution import _Book, _held, _resolution_deltas
    from tests.test_world_tick_service import (
        _ambushing, _canon_commit, _chapter, _later, _plan, _seed_tick_world, _service,
        _tick, _tick_deltas, _WorldLLM, _quiet_turns, POLICY,
    )
    from tests.test_world_tick_delta import _patches

    engine, Session = _session_factory()
    # Lifespan builds its own container engine; share the URL created by postgres_test_url().
    database_url = engine.url.render_as_string(hide_password=False)
    monkeypatch.setenv("FORWIN_DATABASE_URL", database_url)
    config = InfrastructureConfig(database_url=database_url)
    staged, resume = Event(), Event()
    try:
        world = _seed_tick_world(Session, monkeypatch)
        _plan(Session, world, 2)
        chapter = _chapter(Session, world, 1)
        book = _Book(world, chapter, {})
        conflict_id = _conflict(Session, book, proposed_change=_held(book).model_dump(mode="json"),
                                violated="onstage_slip", actor_ref=world.hero)
        original = tick_module._Tick._lock_fresh_canon
        staged_ids = []

        def pause_before_lock(tick):
            if tick.context.project_id == world.project_id and not staged.is_set():
                assert len(tick.conflicts.resolutions) == 1
                resolution = tick.conflicts.resolutions[0]
                assert resolution.decision.option == "plot_responds"
                staged_ids.append(resolution.conflict_id)
                staged.set()
                assert resume.wait(12), "HTTP response never completed while the tick was staged"
            return original(tick)

        monkeypatch.setattr(tick_module._Tick, "_lock_fresh_canon", pause_before_lock)
        # Bound both the blocked database statements and the Python synchronization.
        original_get = WorldConflictRepository.get
        def bounded_get(repo, project_id, row_id, *, for_update=False):
            if for_update:
                repo.session.execute(text("SET LOCAL lock_timeout = '5s'"))
                repo.session.execute(text("SET LOCAL statement_timeout = '10s'"))
            return original_get(repo, project_id, row_id, for_update=for_update)
        monkeypatch.setattr(WorldConflictRepository, "get", bounded_get)

        def run_tick():
            with Session.begin() as session:
                session.execute(text("SET LOCAL lock_timeout = '5s'"))
                session.execute(text("SET LOCAL statement_timeout = '10s'"))
                return _service(Session, _ambushing(world, chapter, "plot_responds")).run(
                    session, _canon_commit(chapter), policy=POLICY,
                )

        with TestClient(HttpRuntimeHarness(session_factory=Session, engine=engine, config=config).app) as client:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(run_tick)
                try:
                    assert staged.wait(12), "Tick never reached its staged response"
                    response = client.post(
                        f"/api/projects/{world.project_id}/world-conflicts/{conflict_id}/resolve",
                        json={"option": "plot_responds", "rationale": "respond first", "reason": "competing HTTP"},
                    )
                    assert response.status_code == 200, response.text
                finally:
                    resume.set()
                with pytest.raises(CanonStaleVersion, match="Canon moved"):
                    future.result(timeout=20)
        [delta] = _resolution_deltas(Session, book, conflict_id)
        assert _patches(delta) == [(_later(world), "set", "state.status", "planned", "held")]
        assert not delta.fact_patches and not delta.cognition_patches
        assert [row.id for row in _tick_deltas(Session, world)] == [delta.id]
        with Session() as session:
            assert WorldConflictRepository(session).get(world.project_id, staged_ids[0]) is None
            runtime = BookStateProjection(session).load_runtime_as_of(world.project_id, as_of_chapter=1)
            assert runtime.world.get_state(world.rival)["alive_status"] == "alive"
            assert len(session.scalars(select(NarrativePlanPatchRow).where(
                NarrativePlanPatchRow.project_id == world.project_id,
                NarrativePlanPatchRow.patch_type == "world_conflict_response",
            )).all()) == 1
            assert len(session.scalars(select(DecisionEvent).where(
                DecisionEvent.project_id == world.project_id,
                DecisionEvent.event_type == DecisionEventType.WORLD_CONFLICT_RESOLVED,
            )).all()) == 1
            assert all(not d.fact_patches and not d.cognition_patches for d in
                       BookStateRepository(session).list_graph_deltas(world.project_id, after_chapter=0,
                                                                      through_chapter=1)
                       if d.source_type == "world_engine")

        # Observe routing in the retry itself: a fresh conflict for this held intent must omit the option.
        retry_options = []
        original_collect = tick_module._Tick._collect
        def collect_with_conflict(tick):
            candidates = original_collect(tick)
            draft = ConflictDraft(project_id=world.project_id, source="world_tick", detected_after_chapter=1,
                story_time=chapter.story_time_end, involved_refs=[_later(world)], violated="impact_review",
                impact_level="major", summary="retry", proposed_change=_held(book).model_dump(mode="json"),
                intent_ref=_later(world), actor_ref=world.hero, blocks_from_chapter=2)
            tick.conflicts.hold(draft, extra={})
            row = tick.conflicts.repo.get(world.project_id, tick.conflicts.conflict_ids[-1])
            retry_options.extend(json.loads(row.options_json))
            return candidates
        monkeypatch.setattr(tick_module._Tick, "_collect", collect_with_conflict)
        from tests.test_world_tick_service import _policy
        _tick(Session, _WorldLLM(actor_turn=_quiet_turns), chapter, policy=_policy(conflict_delegate="human"))
        assert retry_options == ["veto_world", "amend_plan"]
        with Session() as session:
            assert BookStateProjection(session).load_runtime_as_of(
                world.project_id, as_of_chapter=1).world.get_state(_later(world))["status"] == "held"
            assert len(session.scalars(select(NarrativePlanPatchRow).where(
                NarrativePlanPatchRow.project_id == world.project_id,
                NarrativePlanPatchRow.patch_type == "world_conflict_response",
            )).all()) == 1
    finally:
        resume.set()
        engine.dispose()


def test_resolve_refused_while_generation_active(conflict_api):
    from forwin.models import ChapterPlan
    from forwin.models.task import GenerationTask
    from tests.test_world_conflict_resolution import _chapter_conflict, _chapter_violation

    client, Session, book, _ = conflict_api
    conflict_id = _chapter_conflict(Session, book.project_id, _chapter_violation(ref=book.world.hero))
    task_id = f"task-{book.project_id}"
    with Session.begin() as session:
        session.add(GenerationTask(id=task_id, project_id=book.project_id, status="running"))
    path = f"/api/projects/{book.project_id}/world-conflicts/{conflict_id}/resolve"
    payload = {"option": "rewrite_chapter", "rationale": "repair the text", "reason": "operator review"}

    response = client.post(path, json=payload)

    assert response.status_code == 409 and "generation task" in response.json()["detail"]
    with Session() as session:
        assert WorldConflictRepository(session).get(book.project_id, conflict_id).status == "open"
        assert session.get(ChapterPlan, book.plans[2]).status == "needs_review"
    with Session.begin() as session:
        session.get(GenerationTask, task_id).status = "paused"
    response = client.post(path, json=payload)
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["status"], body["source"], body["decision"]["option"], body["result_refs"]) == (
        "resolved", "chapter_extraction", "rewrite_chapter", [],
    )
    assert body["proposed_change"]["boundary"]["kind"] == "must_be_alive"
    with Session() as session:
        assert session.get(ChapterPlan, book.plans[2]).status == "planned"


def test_resolve_refused_once_the_chapter_left_needs_review(conflict_api):
    from forwin.models import ChapterPlan
    from tests.test_world_conflict_resolution import _chapter_conflict, _chapter_violation

    client, Session, book, _ = conflict_api
    conflict_id = _chapter_conflict(Session, book.project_id, _chapter_violation(ref=book.world.hero), status="drafted")
    response = client.post(f"/api/projects/{book.project_id}/world-conflicts/{conflict_id}/resolve", json={
        "option": "rewrite_chapter", "rationale": "repair the text", "reason": "operator review",
    })
    assert response.status_code == 409 and "not needs_review" in response.json()["detail"]
    with Session() as session:
        assert WorldConflictRepository(session).get(book.project_id, conflict_id).status == "open"
        assert session.get(ChapterPlan, book.plans[2]).status == "drafted"
