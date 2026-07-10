from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException

from forwin.book_state.reviewer import BookStateReviewGate
from forwin.canon import (
    CanonAdmissionService,
    CanonStaleVersion,
    CanonWriteFailure,
)
from forwin.models.knowledge import KnowledgeEditProposalRow
from forwin.obsidian.structured_patch import proposal_to_graph_delta
from forwin.protocol.book_state import ApprovedGraphDeltaSet


@dataclass(frozen=True)
class ProposalReviewResult:
    row: KnowledgeEditProposalRow
    projection_refresh: dict[str, Any] = field(default_factory=dict)


def proposal_chapter(row: KnowledgeEditProposalRow) -> int:
    payload = _load_patch_json(row.proposed_patch_json)
    frontmatter = (
        payload.get("frontmatter")
        if isinstance(payload.get("frontmatter"), dict)
        else {}
    )
    try:
        return int(
            frontmatter.get("as_of_chapter") or payload.get("as_of_chapter") or 0
        )
    except (TypeError, ValueError):
        return 0


def approve_world_edit_proposal(
    session,
    *,
    project_id: str,
    proposal_id: str,
    reason: str = "",
    forced_accept_reason: str = "",
    trigger: str = "obsidian_proposal_approve",
) -> ProposalReviewResult:
    row = _load_pending_proposal(session, project_id, proposal_id)
    try:
        delta = proposal_to_graph_delta(session, row, reason=reason)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    changes = ApprovedGraphDeltaSet(
        project_id=project_id,
        chapter_number=proposal_chapter(row),
        graph_deltas=[delta],
        approved_by=[f"{trigger}_approval"],
        review_verdict_id=f"obsidian_proposal_review_{proposal_id}",
        forced_accept_reason=forced_accept_reason,
    )
    verdict = BookStateReviewGate(session).review(changes)
    if not verdict.accepted or verdict.approved_changes is None:
        message = "; ".join(
            f"{issue.code}: {issue.message}" for issue in verdict.issues
        )
        raise HTTPException(
            status_code=409,
            detail=message or "proposal rejected by BookStateReviewGate",
        )

    try:
        outcome = CanonAdmissionService().commit_world_edit(
            session=session,
            project_id=project_id,
            proposal_id=proposal_id,
            approved_changes=verdict.approved_changes,
            reason=reason,
            trigger=trigger,
        )
    except (CanonStaleVersion, CanonWriteFailure) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ProposalReviewResult(
        row=row,
        projection_refresh={
            "deferred": True,
            "outbox_event_id": outcome.outbox_event_id,
            "as_of_chapter": outcome.compile_result.chapter_number,
        },
    )


def _load_pending_proposal(
    session, project_id: str, proposal_id: str
) -> KnowledgeEditProposalRow:
    row = session.get(KnowledgeEditProposalRow, proposal_id)
    if row is None or row.project_id != project_id:
        raise HTTPException(status_code=404, detail="proposal not found")
    if row.status not in {"pending", "proposed"}:
        raise HTTPException(status_code=409, detail=f"proposal already {row.status}")
    return row


def _load_patch_json(raw: str) -> dict[str, Any]:
    import json

    try:
        payload = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}
