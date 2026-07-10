from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from forwin.models.draft import ChapterDraft, ChapterReview


@dataclass(frozen=True)
class ChapterReviewGateOutcome:
    should_apply_canon: bool
    gate_kind: str = ""
    reckless_approved: bool = False
    pause_required: bool = False


def handle_chapter_review_gate(
    self,
    *,
    session,
    updater,
    project_id: str,
    governance,
    chapter_plan,
    writer_output,
    verdict,
    residual_review_issues: list[dict[str, Any]],
    canon_risk_level: str,
    repair_attempt_count: int,
    force_accept_applied: bool,
    chapter_number: int,
    last_requested_chapter: int,
    requested_chapters: int,
    completed_chapters: list[int],
    failed_chapters: list[int],
    paused_chapters: list[int],
) -> ChapterReviewGateOutcome:
    operation_mode = "blackbox"
    should_apply_canon = (
        verdict.verdict == "pass"
        or (operation_mode == "blackbox" and verdict.verdict == "warn")
        or force_accept_applied
    )
    review_interval = max(0, int(self.policy.pause.review_interval_chapters))
    gate_kind, gate_reason = _review_gate_details(
        operation_mode=operation_mode,
        verdict=str(verdict.verdict or ""),
        force_accept_applied=force_accept_applied,
        should_apply_canon=should_apply_canon,
        review_interval=review_interval,
        chapter_number=chapter_number,
        last_requested_chapter=last_requested_chapter,
    )
    if not gate_kind:
        return ChapterReviewGateOutcome(should_apply_canon=should_apply_canon)

    reckless_approved = False
    if _is_reckless(governance):
        reckless_approved = _delegate_chapter_gate(
            self,
            session=session,
            updater=updater,
            project_id=project_id,
            governance=governance,
            gate_kind=gate_kind,
            gate_reason=gate_reason,
            chapter_plan=chapter_plan,
            writer_output=writer_output,
            verdict=verdict,
            residual_review_issues=residual_review_issues,
            canon_risk_level=canon_risk_level,
            repair_attempt_count=repair_attempt_count,
            force_accept_applied=force_accept_applied,
            review_interval=review_interval,
            chapter_number=chapter_number,
        )
    if reckless_approved:
        return ChapterReviewGateOutcome(
            should_apply_canon=True,
            gate_kind=gate_kind,
            reckless_approved=True,
        )

    updater.mark_chapter_status(
        project_id,
        chapter_number,
        "needs_review",
        repair_attempt_count=repair_attempt_count,
        residual_review_issues=residual_review_issues,
        canon_risk_level=canon_risk_level,
    )
    session.commit()
    paused_chapters.append(chapter_number)
    self._emit_progress(
        "stage_changed",
        stage="paused_for_review",
        project_id=project_id,
        requested_chapters=requested_chapters,
        current_chapter=chapter_number,
        completed_chapters=completed_chapters,
        failed_chapters=failed_chapters,
        paused_chapters=paused_chapters,
    )
    return ChapterReviewGateOutcome(
        should_apply_canon=should_apply_canon,
        gate_kind=gate_kind,
        pause_required=True,
    )


def _review_gate_details(
    *,
    operation_mode: str,
    verdict: str,
    force_accept_applied: bool,
    should_apply_canon: bool,
    review_interval: int,
    chapter_number: int,
    last_requested_chapter: int,
) -> tuple[str, str]:
    if operation_mode == "checkpoint":
        return "chapter_operation_checkpoint", "checkpoint operation mode requires approval"
    if operation_mode == "copilot" and verdict != "pass":
        return "chapter_copilot_verdict", f"copilot verdict is {verdict}"
    if operation_mode == "blackbox" and verdict == "fail" and not force_accept_applied:
        return "chapter_blackbox_failure", "blackbox repair exhausted with fail verdict"
    if not should_apply_canon:
        return "chapter_acceptance_gate", f"verdict {verdict} is not automatically applicable"
    if (
        review_interval
        and chapter_number % review_interval == 0
        and chapter_number != last_requested_chapter
    ):
        return "chapter_review_interval", f"review interval {review_interval} reached"
    return "", ""


def _delegate_chapter_gate(
    self,
    *,
    session,
    updater,
    project_id: str,
    governance,
    gate_kind: str,
    gate_reason: str,
    chapter_plan,
    writer_output,
    verdict,
    residual_review_issues: list[dict[str, Any]],
    canon_risk_level: str,
    repair_attempt_count: int,
    force_accept_applied: bool,
    review_interval: int,
    chapter_number: int,
) -> bool:
    latest_draft = (
        session.query(ChapterDraft)
        .filter(ChapterDraft.chapter_plan_id == chapter_plan.id)
        .order_by(ChapterDraft.version.desc(), ChapterDraft.id.desc())
        .first()
    )
    latest_review = None
    if latest_draft is not None:
        latest_review = (
            session.query(ChapterReview)
            .filter(ChapterReview.draft_id == latest_draft.id)
            .order_by(ChapterReview.created_at.desc(), ChapterReview.id.desc())
            .first()
        )
    outcome = self._delegate_reckless_review(
        updater=updater,
        project_id=project_id,
        governance=governance,
        gate_kind=gate_kind,
        scope="chapter",
        chapter_number=chapter_number,
        related_object_type="chapter_review" if latest_review is not None else "chapter_plan",
        related_object_id=(
            str(latest_review.id) if latest_review is not None else str(chapter_plan.id)
        ),
        input_snapshot={
            "gate_reason": gate_reason,
            "chapter_plan": {
                "id": str(chapter_plan.id or ""),
                "chapter_number": chapter_number,
                "title": str(chapter_plan.title or ""),
                "one_line": str(chapter_plan.one_line or ""),
                "status": str(chapter_plan.status or ""),
            },
            "draft_id": str(getattr(latest_draft, "id", "") or ""),
            "review_id": str(getattr(latest_review, "id", "") or ""),
            "writer_output": writer_output.model_dump(mode="json"),
            "review_verdict": verdict.model_dump(mode="json"),
            "residual_review_issues": residual_review_issues,
            "canon_risk_level": canon_risk_level,
            "repair_attempt_count": repair_attempt_count,
            "force_accept_applied": force_accept_applied,
            "review_interval_chapters": review_interval,
            "governance": governance.model_dump(mode="json"),
        },
    )
    return bool(outcome is not None and outcome.approved)


def _is_reckless(governance) -> bool:
    return (
        str(getattr(governance, "review_delegation_mode", "human") or "human")
        == "reckless"
    )


__all__ = ["ChapterReviewGateOutcome", "handle_chapter_review_gate"]
