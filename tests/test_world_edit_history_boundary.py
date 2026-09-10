"""World edits cannot change accepted history outside full-suffix validation."""

import json

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from forwin.api_schema import WorldEditProposalReviewRequest
from forwin.canon.admission import CanonAdmissionService, CanonWriteFailure
from forwin.http.adapters.api_proposal_routes import build_handlers
from forwin.models.knowledge import KnowledgeEditProposalRow
from forwin.models.project import Project
from forwin.proposals.structured_patch import proposal_to_graph_delta
from forwin.protocol.book_state import ApprovedGraphDeltaSet
from tests import test_canon_atomic_transaction as atomic

historical_rewrite_scenario = atomic.historical_rewrite_scenario
prepared_canon = atomic.prepared_canon


def _proposal(fixture, chapter):
    with fixture.Session.begin() as session:
        proposal = KnowledgeEditProposalRow(
            project_id=fixture.project_id,
            source="world_studio",
            status="pending",
            proposed_patch_json=json.dumps(
                {
                    "as_of_chapter": chapter,
                    "forwin_patch": [
                        {
                            "op": "create_fact",
                            "fact_id": f"world-edit-premise-{chapter}",
                            "proposition": "The archive event never occurred.",
                            "truth_value": "true",
                        }
                    ],
                }
            ),
        )
        session.add(proposal)
        session.flush()
        return proposal.id


@pytest.mark.parametrize("chapter", [0, 2, 3, 4])
def test_proposal_approval_rejects_accepted_history_without_changing_mainline(
    historical_rewrite_scenario, chapter
):
    fixture = historical_rewrite_scenario
    proposal_id = _proposal(fixture, chapter)
    before = atomic._retained_mainline(fixture)
    handlers = build_handlers(get_session=fixture.Session)

    with pytest.raises(HTTPException) as rejected:
        handlers["approve_project_proposal"](
            fixture.project_id,
            proposal_id,
            WorldEditProposalReviewRequest(
                status="accepted",
                reason="Correct a historical premise",
                forced_accept_reason="Human approval cannot replace suffix evidence",
            ),
        )

    assert rejected.value.status_code == 409
    assert "full-suffix" in rejected.value.detail
    assert atomic._retained_mainline(fixture) == before
    with fixture.Session() as session:
        proposal = session.get(KnowledgeEditProposalRow, proposal_id)
        assert proposal.status == "pending"
        assert proposal.reviewed_at is None
        assert not proposal.graph_delta_id


def test_direct_canon_world_edit_cannot_hide_historical_delta_in_future_envelope(
    historical_rewrite_scenario,
):
    fixture = historical_rewrite_scenario
    proposal_id = _proposal(fixture, 2)
    before = atomic._retained_mainline(fixture)
    with fixture.Session.begin() as session:
        proposal = session.get(KnowledgeEditProposalRow, proposal_id)
        delta = proposal_to_graph_delta(session, proposal)
        with pytest.raises(CanonWriteFailure, match="full-suffix"):
            CanonAdmissionService().commit_world_edit(
                session=session,
                project_id=fixture.project_id,
                proposal_id=proposal_id,
                approved_changes=ApprovedGraphDeltaSet(
                    project_id=fixture.project_id,
                    chapter_number=4,
                    graph_deltas=[delta],
                ),
                reason="The delta still changes chapter two",
                trigger="test",
            )
        # Even a caller that catches the rejection and commits cannot leak writes.
        assert proposal.status == "pending"
    assert atomic._retained_mainline(fixture) == before


@pytest.mark.parametrize("chapter", [0, 4])
def test_world_edit_without_accepted_history_remains_available(prepared_canon, chapter):
    fixture = prepared_canon
    proposal_id = _proposal(fixture, chapter)
    result = build_handlers(get_session=fixture.Session)["approve_project_proposal"](
        fixture.project_id,
        proposal_id,
        WorldEditProposalReviewRequest(status="accepted", reason="Future"),
    )
    assert result.status == "accepted"
    assert result.projection_refresh["as_of_chapter"] == chapter
    with fixture.Session() as session:
        assert session.get(Project, fixture.project_id).book_revision == 1


def test_world_edit_rechecks_new_acceptance_after_stale_project_read(prepared_canon):
    fixture = prepared_canon
    proposal_id = _proposal(fixture, 1)
    with fixture.Session.begin() as session:
        assert session.get(Project, fixture.project_id).book_revision == 0
        outcome = CanonAdmissionService(session_factory=fixture.Session).commit_plan(
            fixture.plan
        )
        assert not outcome.blocked
        proposal = session.get(KnowledgeEditProposalRow, proposal_id)
        changes = ApprovedGraphDeltaSet(
            project_id=fixture.project_id,
            chapter_number=1,
            graph_deltas=[proposal_to_graph_delta(session, proposal)],
        )
        with pytest.raises(CanonWriteFailure, match="full-suffix"):
            CanonAdmissionService().commit_world_edit(
                session=session,
                project_id=fixture.project_id,
                proposal_id=proposal_id,
                approved_changes=changes,
                reason="Prepared before another transaction accepted chapter one",
                trigger="test",
            )
    with fixture.Session() as session:
        assert session.get(Project, fixture.project_id).book_revision == 1
        assert session.get(KnowledgeEditProposalRow, proposal_id).status == "pending"


def test_pre_acceptance_world_edit_increments_current_revision_after_stale_project_read(
    prepared_canon,
):
    fixture = prepared_canon
    first_proposal_id = _proposal(fixture, 0)
    proposal_id = _proposal(fixture, 2)
    with fixture.Session.begin() as session:
        cached_project = session.get(Project, fixture.project_id)
        assert cached_project.book_revision == 0
        result = build_handlers(get_session=fixture.Session)["approve_project_proposal"](
            fixture.project_id,
            first_proposal_id,
            WorldEditProposalReviewRequest(status="accepted", reason="First edit"),
        )
        assert result.status == "accepted"
        proposal = session.get(KnowledgeEditProposalRow, proposal_id)
        outcome = CanonAdmissionService().commit_world_edit(
            session=session,
            project_id=fixture.project_id,
            proposal_id=proposal_id,
            approved_changes=ApprovedGraphDeltaSet(
                project_id=fixture.project_id,
                chapter_number=2,
                graph_deltas=[proposal_to_graph_delta(session, proposal)],
            ),
            reason="A second pre-acceptance edit after another world edit commit",
            trigger="test",
        )
        assert outcome.compile_result.committed
    with fixture.Session() as session:
        assert session.scalar(
            select(Project.book_revision).where(Project.id == fixture.project_id)
        ) == 2
