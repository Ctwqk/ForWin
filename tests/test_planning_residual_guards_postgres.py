"""Transaction regressions for the controller to run on PostgreSQL."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from forwin.runtime.container import RuntimeContainer  # initialize the canon import graph
from forwin.maintenance.post_canon import save_world_prompt_trace
from forwin.planning.amend import AmendResult, PlanAmender
from forwin.planning.band_planner.service import BandPlanner
from forwin.planning.band_planner.types import BandPlanningAuditError, PlanIssue
from tests.test_arc_beat_sheet import StubLLM
from tests.test_band_planner import _band, _book, _plan_band, _plans
from tests.test_world_conflict_resolution import (
    _amend, _band_world, _conflict_row, _session_factory, _world_conflict,
)


def test_aborted_audit_transaction_does_not_mask_the_original_database_error(caplog):
    class DatabaseFailureAudit:
        def audit_plans(self, session, **kwargs):
            session.execute(text("SELECT 1 / 0"))

    engine, Session = _session_factory()
    try:
        world, arc_id, sheet_id = _book(Session)
        planner = BandPlanner(
            StubLLM(_band(world, range(1, 5))), feasibility=DatabaseFailureAudit(),
            save_trace=save_world_prompt_trace,
        )
        with pytest.raises(BandPlanningAuditError) as raised:
            _plan_band(Session, planner, world, arc_id, sheet_id)
        assert isinstance(raised.value.__cause__, DBAPIError)
        assert raised.value.__cause__.orig.sqlstate == "22012"
        assert "could not save the failed band audit trace" in caplog.text
        with Session() as session:
            assert _plans(session, world.project_id) == {}
    finally:
        engine.dispose()


@pytest.mark.parametrize("raises", [False, True], ids=["failed_result", "raised_error"])
def test_failure_recorder_database_error_preserves_the_committed_resolution(raises, caplog):
    class DoubleFailureAmender(PlanAmender):
        def amend(self, session, **kwargs):
            if raises:
                raise RuntimeError("replan unavailable")
            return AmendResult(band_plan_id=None, issues=(
                PlanIssue(code="audit_error", severity="error", message="replan unavailable"),
            ))

        def record_world_amendment_failure(self, session, **kwargs):
            session.execute(text("SELECT 1 / 0"))

    engine, Session = _session_factory()
    try:
        world, _arc_id, _band_id = _band_world(Session)
        conflict_id = _world_conflict(Session, world)
        result = _amend(Session, world.project_id, conflict_id, DoubleFailureAmender(None, None))
        assert len(result.graph_delta_ids) == 1
        assert result.followup_conflict_id == ""
        assert [warning["code"] for warning in result.warnings] == ["replan_failed"]
        assert _conflict_row(Session, world.project_id, conflict_id) == (
            "resolved", list(result.graph_delta_ids),
        )
        assert "recording the failed amendment raised" in caplog.text
    finally:
        engine.dispose()
