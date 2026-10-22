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


@pytest.mark.parametrize("error_name,status", [
    ("ConflictNotOpen", 409),
    ("ConflictStale", 409),
    ("ConflictOptionUnavailable", 400),
    ("ValueError", 400),
    ("LookupError", 404),
])
def test_resolve_route_maps_service_errors_without_committing(monkeypatch, error_name, status):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from fastapi import FastAPI

    from forwin.http.adapters import api_world_conflict_routes as routes

    session = MagicMock()
    session.__enter__.return_value = session
    monkeypatch.setattr(routes, "require_project", lambda *_: SimpleNamespace(id="p1"))
    monkeypatch.setattr(routes, "ProjectPolicyStore", lambda _: MagicMock())
    monkeypatch.setattr(routes, "WorldConflictRepository", lambda _: MagicMock())
    service = MagicMock()
    error_type = {"ValueError": ValueError, "LookupError": LookupError}.get(error_name)
    if error_type is None:
        error_type = getattr(routes, error_name)
    service.resolve.side_effect = error_type("resolution refused")
    monkeypatch.setattr(routes, "ConflictResolutionService", lambda *args, **kwargs: service)
    app = FastAPI()
    app.add_api_route(
        "/api/projects/{project_id}/world-conflicts/{conflict_id}/resolve",
        routes.build_handlers(get_session=lambda: session)["resolve_project_world_conflict"],
        methods=["POST"],
    )
    with TestClient(app) as client:
        response = client.post("/api/projects/p1/world-conflicts/c1/resolve", json={
            "option": "veto_world", "rationale": "preserve plan", "reason": "human review",
        })
    assert response.status_code == status
    assert response.json()["detail"] == "resolution refused"
    session.commit.assert_not_called()
    decision = service.resolve.call_args.kwargs["decision"]
    assert decision.decided_by == "human"
