from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from forwin.generation.gate_delegation import GateResolution
from forwin.models.draft import ChapterDraft, ChapterReview
from forwin.runtime.policy import RuntimePolicy


@dataclass(frozen=True)
class ChapterReviewGateOutcome:
    should_apply_canon: bool
    gate_kind: str = ""
    gate_approved: bool = False
    pause_required: bool = False


def evaluate_candidate_gate(
    *,
    policy: RuntimePolicy,
    verdict,
    eligible: bool,
    delegate: Callable[[], GateResolution],
) -> ChapterReviewGateOutcome:
    if not eligible or verdict.verdict not in {"pass", "warn"}:
        return ChapterReviewGateOutcome(
            should_apply_canon=False,
            pause_required=True,
        )
    if policy.pause.gate_delegate == "human":
        return ChapterReviewGateOutcome(
            should_apply_canon=True,
            pause_required=True,
        )
    resolution = delegate()
    if resolution.approved:
        return ChapterReviewGateOutcome(
            should_apply_canon=True,
            gate_approved=True,
        )
    return ChapterReviewGateOutcome(
        should_apply_canon=True,
        pause_required=True,
    )


def handle_chapter_review_gate(
    self,
    *,
    session,
    updater,
    project_id: str,
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
    eligible = verdict.verdict in {"pass", "warn"}
    review_interval = max(0, int(self.policy.pause.review_interval_chapters))
    gate_kind, gate_reason = _review_gate_details(
        verdict=str(verdict.verdict or ""),
        eligible=eligible,
        review_interval=review_interval,
        chapter_number=chapter_number,
        last_requested_chapter=last_requested_chapter,
    )
    if not gate_kind:
        return ChapterReviewGateOutcome(should_apply_canon=eligible)

    evaluation = evaluate_candidate_gate(
        policy=self.policy,
        verdict=verdict,
        eligible=eligible,
        delegate=lambda: _delegate_chapter_gate(
            self,
            session=session,
            updater=updater,
            project_id=project_id,
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
    )
    if evaluation.gate_approved:
        return ChapterReviewGateOutcome(
            should_apply_canon=True,
            gate_kind=gate_kind,
            gate_approved=True,
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
        should_apply_canon=evaluation.should_apply_canon,
        gate_kind=gate_kind,
        pause_required=True,
    )


def _review_gate_details(
    *,
    verdict: str,
    eligible: bool,
    review_interval: int,
    chapter_number: int,
    last_requested_chapter: int,
) -> tuple[str, str]:
    if not eligible:
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
) -> GateResolution:
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
    return self._resolve_gate_delegation(
        updater=updater,
        project_id=project_id,
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
            "pause_policy": self.policy.pause.model_dump(mode="json"),
        },
    )


__all__ = [
    "ChapterReviewGateOutcome",
    "evaluate_candidate_gate",
    "handle_chapter_review_gate",
]
