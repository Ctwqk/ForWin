from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException

from forwin.book_state.compiler import BookStateCompiler
from forwin.book_state.reviewer import BookStateReviewGate
from forwin.knowledge_system import KnowledgeProjectionRefresher
from forwin.models.world_model import WorldEditProposalRow
from forwin.obsidian.structured_patch import proposal_to_graph_delta
from forwin.protocol.book_state import ApprovedGraphDeltaSet


@dataclass(frozen=True)
class ProposalReviewResult:
    row: WorldEditProposalRow
    projection_refresh: dict[str, Any] = field(default_factory=dict)


def proposal_chapter(row: WorldEditProposalRow) -> int:
    payload = _load_patch_json(row.proposed_patch_json)
    frontmatter = payload.get("frontmatter") if isinstance(payload.get("frontmatter"), dict) else {}
    try:
        return int(frontmatter.get("as_of_chapter") or payload.get("as_of_chapter") or 0)
    except (TypeError, ValueError):
        return 0


def approve_world_edit_proposal(
    session,
    *,
    project_id: str,
    proposal_id: str,
    reason: str = "",
    trigger: str = "obsidian_proposal_approve",
    qdrant_url: str | None = None,
    qdrant_collection: str | None = None,
    qdrant_client: Any | None = None,
    qdrant_models: Any | None = None,
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
    )
    verdict = BookStateReviewGate(session).review(changes)
    if not verdict.accepted or verdict.approved_changes is None:
        message = "; ".join(f"{issue.code}: {issue.message}" for issue in verdict.issues)
        raise HTTPException(status_code=409, detail=message or "proposal rejected by BookStateReviewGate")

    result = BookStateCompiler(session).compile(verdict.approved_changes)
    if not result.committed:
        raise HTTPException(status_code=409, detail="; ".join(result.blocked_reasons) or "BookState compile blocked")

    row.status = "accepted"
    row.reviewed_at = datetime.now(UTC)
    row.review_reason = reason
    row.graph_delta_id = delta.id
    session.add(row)
    session.flush()

    projection_refresh = KnowledgeProjectionRefresher(
        session,
        qdrant_url=qdrant_url,
        qdrant_collection=qdrant_collection,
        qdrant_client=qdrant_client,
        qdrant_models=qdrant_models,
    ).refresh(
        project_id,
        as_of_chapter=result.chapter_number,
        trigger=trigger,
    )
    return ProposalReviewResult(row=row, projection_refresh=projection_refresh.as_dict())


def _load_pending_proposal(session, project_id: str, proposal_id: str) -> WorldEditProposalRow:
    row = session.get(WorldEditProposalRow, proposal_id)
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
