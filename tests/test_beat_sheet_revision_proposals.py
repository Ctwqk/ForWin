"""Revision normalization and SQLite publication checks without PostgreSQL or model access."""

from copy import deepcopy

import pytest

from forwin.planning.beat_sheet.service import ArcBeatSheetService
from tests.test_arc_beat_sheet import StubLLM, _OFFERED, _RUNTIME, _VALID


@pytest.mark.parametrize("bands", [
    [{"band_index": 0, "chapter_start": 1, "chapter_end": 4},
     {"band_index": 1, "chapter_start": 5, "chapter_end": 6}],
    [{"band_index": 0, "chapter_start": 1, "chapter_end": 99}],
], ids=["resplit", "outside_arc"])
def test_revision_restores_current_bands_without_retry(bands):
    answer = deepcopy(_VALID)
    answer["band_breakdown"] = bands
    answer["milestones"][0]["description"] = "revised description"
    client = StubLLM(answer)
    service = ArcBeatSheetService(client, save_trace=lambda *a: "trace")
    call = service._call(
        {"current_beat_sheet": deepcopy(_VALID), "revision": {"reason": "conflict"}}, "",
        start=1, end=6, runtime=_RUNTIME, actor_ids=_OFFERED, reveal_chapters={}, reader_known=frozenset(),
    )
    assert call.data is not None
    assert call.data.model_dump()["band_breakdown"] == _VALID["band_breakdown"]
    assert call.data.milestones[0].description == "revised description"
    assert len(client.calls) == 1 and not call.business_retries and not call.issues
    assert "band_breakdown_restored" in call.normalizations


@pytest.mark.parametrize("audit_fails", [False, True], ids=["clean_audit", "failing_audit"])
def test_revision_publication_and_future_planning_without_postgres(local_session_factory, audit_fails):
    from tests.test_chapter_amendment_postgres import (
        test_milestone_revision_preserves_bands_through_future_planning as check,
    )

    check(local_session_factory, audit_fails)


@pytest.fixture
def local_session_factory():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from forwin.models.base import Base

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.mark.parametrize("winner", ["rewrite_chapter", "active_task"])
def test_revalidation_rejects_changed_state_before_any_publication(local_session_factory, winner):
    from fastapi import HTTPException
    from forwin.api_schema.world_conflicts import WorldConflictResolveRequest
    from forwin.http.adapters.api_world_conflict_routes import build_handlers
    from forwin.models.task import GenerationTask
    from tests.test_band_planner import HookedLLM
    from tests.test_chapter_amendment_postgres import _paused_book, _snapshot
    from tests.test_arc_beat_sheet import _sheet
    from tests.test_world_conflict_resolution import _amender

    Session = local_session_factory
    world, _, _, conflict_id = _paused_book(Session)
    before = _snapshot(Session, world.project_id)
    def win():
        if winner == "rewrite_chapter":
            handler(world.project_id, conflict_id, WorldConflictResolveRequest(
                option="rewrite_chapter", rationale="rewrite", reason="decision"))
        else:
            with Session.begin() as session:
                session.add(GenerationTask(id="racing-task", project_id=world.project_id, status="running"))
    client = HookedLLM(win, _sheet(world))
    handler = build_handlers(get_session=Session, get_plan_amender=lambda: _amender(StubLLM(), client))[
        "resolve_project_world_conflict"]
    with pytest.raises(HTTPException) as error:
        handler(world.project_id, conflict_id, WorldConflictResolveRequest(
            option="amend_plan", rationale="amend", reason="decision"))
    assert error.value.status_code == 409
    assert _snapshot(Session, world.project_id) == before


def test_resolution_failure_rolls_back_sheet_and_constraints(local_session_factory, monkeypatch):
    from forwin.canon.admission import CanonAdmissionService
    from forwin.world_engine.conflict_repository import WorldConflictRepository
    from forwin.world_engine.conflict_resolution import ConflictResolutionService
    from tests.test_arc_beat_sheet import POLICY, _sheet
    from tests.test_chapter_amendment_postgres import _paused_book, _snapshot
    from tests.test_world_conflict_resolution import _amender, _decision

    Session = local_session_factory
    world, _, _, conflict_id = _paused_book(Session)
    before = _snapshot(Session, world.project_id)
    def fail(*args, **kwargs):
        raise RuntimeError("resolution failed after sheet publication")
    monkeypatch.setattr(WorldConflictRepository, "mark_resolved", fail)
    with Session() as session:
        with pytest.raises(RuntimeError, match="resolution failed"):
            ConflictResolutionService(session, admission=CanonAdmissionService(), policy=POLICY,
                plan_amender=_amender(StubLLM(), StubLLM(_sheet(world)))).resolve(
                    project_id=world.project_id, conflict_id=conflict_id, decision=_decision("amend_plan"))
        # Committing the outer transaction must not retain any of the failed resolution's canon effects.
        session.commit()
    assert _snapshot(Session, world.project_id) == before


@pytest.mark.parametrize("scenario", ["one_band", "two_bands", "first_failure", "second_failure"])
def test_existing_amender_callers_keep_working(local_session_factory, monkeypatch, scenario):
    from tests import test_world_conflict_resolution as checks

    monkeypatch.setattr(checks, "_session_factory", lambda: (local_session_factory.kw["bind"], local_session_factory))
    if scenario == "one_band":
        checks.test_amendment_keeps_the_band_when_the_revision_resplits_it()
    elif scenario == "two_bands":
        checks.test_resplit_amendment_preserves_existing_band_ownership()
    else:
        checks.test_amendment_succeeds_only_when_every_band_is_planned(1 if scenario == "first_failure" else 2)


def test_immediate_revise_wrapper_preserves_spans(local_session_factory):
    from tests.test_arc_beat_sheet import test_revise_creates_new_version_and_supersedes_old as check

    check(local_session_factory)
