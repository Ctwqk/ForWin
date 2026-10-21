from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.orm import Session

from forwin.book_state.extraction.chapter_delta_schema import ChapterDeltaExtraction, PlanFulfillment
from forwin.book_state.repository import BookStateRepository
from forwin.narrative_obligations.repository import NarrativeObligationRepository
from forwin.narrative_obligations.types import (
    NarrativeObligation,
    ObligationResolutionCandidate,
)


class ObligationResolutionVerifier:
    def verify(
        self,
        *,
        obligation: NarrativeObligation,
        plan_fulfillment: Sequence[PlanFulfillment],
    ) -> ObligationResolutionCandidate:
        entries = [item for item in plan_fulfillment if item.target_id == f"obligation:{obligation.id}"]
        evidence = list(dict.fromkeys(item.evidence for item in entries if item.evidence.strip()))
        resolved = any(item.status == "done" and item.evidence.strip() for item in entries)
        result = "pass" if resolved else "warn"
        explanation = (
            "obligation has completed plan fulfillment with evidence"
            if resolved else "obligation has no completed plan fulfillment with evidence"
        )
        return ObligationResolutionCandidate(
            obligation_id=obligation.id,
            chapter_number=obligation.deadline_chapter,
            resolution_type=obligation.obligation_type,
            explanation=explanation,
            verifier_result=result,
            status=result,
            matched_markers=evidence,
            reason=explanation,
        )


def verify_active_obligations_after_acceptance(
    *,
    session: Session,
    project_id: str,
    chapter_number: int,
    verifier: ObligationResolutionVerifier | None = None,
) -> dict[str, list[str]]:
    repo = NarrativeObligationRepository(session)
    deltas = BookStateRepository(session).list_graph_deltas(
        project_id, after_chapter=chapter_number - 1, through_chapter=chapter_number,
    )
    plan_fulfillment = [
        PlanFulfillment.model_validate(item)
        for delta in deltas
        if delta.source_type == "chapter_extraction"
        for item in delta.metadata.get("plan_fulfillment", [])
    ]
    resolved_ids: list[str] = []
    warned_ids: list[str] = []
    failed_ids: list[str] = []
    checker = verifier or ObligationResolutionVerifier()
    for obligation in repo.list_active_for_context(project_id, chapter_number=chapter_number):
        result = checker.verify(
            obligation=obligation,
            plan_fulfillment=plan_fulfillment,
        ).model_copy(update={"chapter_number": chapter_number, "evidence_refs": [f"chapter:{chapter_number}"]})
        if result.status == "pass":
            repo.mark_obligation_resolved(
                obligation.id,
                verifier_result=result.model_dump(mode="json"),
                evidence_refs=result.evidence_refs,
                resolution_chapter=chapter_number,
            )
            resolved_ids.append(obligation.id)
        elif result.status == "warn":
            warned_ids.append(obligation.id)
        else:
            failed_ids.append(obligation.id)
    return {
        "resolved_obligation_ids": resolved_ids,
        "warned_obligation_ids": warned_ids,
        "failed_obligation_ids": failed_ids,
    }


def verify_due_obligations_for_draft(
    *,
    obligations: list[NarrativeObligation],
    chapter_number: int,
    chapter_delta: ChapterDeltaExtraction | None,
    evidence_ref: str = "",
    verifier: ObligationResolutionVerifier | None = None,
) -> list[str]:
    if chapter_delta is None:
        return []
    current_chapter = int(chapter_number or 0)
    checker = verifier or ObligationResolutionVerifier()
    resolved_ids: list[str] = []
    for obligation in obligations:
        obligation_id = str(obligation.id or "").strip()
        if not obligation_id or obligation.status != "active":
            continue
        if int(obligation.deadline_chapter or 0) > current_chapter:
            continue
        result = checker.verify(
            obligation=obligation,
            plan_fulfillment=chapter_delta.plan_fulfillment,
        )
        if result.status == "pass":
            resolved_ids.append(obligation_id)
    return resolved_ids


def expire_unresolved_obligations_after_acceptance(
    *,
    session: Session,
    project_id: str,
    chapter_number: int,
) -> dict[str, list[str]]:
    repo = NarrativeObligationRepository(session)
    expired_ids: list[str] = []
    blocked_ids: list[str] = []
    for obligation in repo.list_active_for_context(project_id, chapter_number=chapter_number + 1):
        if obligation.status != "active":
            continue
        if int(obligation.deadline_chapter or 0) > int(chapter_number or 0):
            continue
        expired = repo.expire_obligation(
            obligation.id,
            reason="deadline passed after accepted chapter",
        )
        if expired is None:
            continue
        expired_ids.append(expired.id)
        if expired.blocking_policy == "block_at_deadline":
            blocked = repo.block_expired_obligation(expired.id)
            if blocked is not None:
                blocked_ids.append(blocked.id)
    return {
        "expired_obligation_ids": expired_ids,
        "blocked_obligation_ids": blocked_ids,
    }

