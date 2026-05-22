from __future__ import annotations

from forwin.orchestrator_loop_core.common import *
from forwin.planning.arc_patch_validator import ArcPatchValidator
from forwin.planning.arc_plan_patcher import ArcPlanPatcher
from forwin.planning.book_patch_validator import BookPatchValidator
from forwin.planning.book_plan_patcher import BookPlanPatcher
from forwin.narrative_obligations.budget import evaluate_obligation_budget
from forwin.review_engine.types import Decision, DecisionInput


def _persist_structural_patch_outcome(
    *,
    session: Session,
    project_id: str,
    chapter_number: int,
    draft_id: str,
    review_id: str,
    verdict: ReviewVerdict,
    signals: list[Any],
    target_total_chapters: int,
    decision,
    outcome_reason: str,
    arc_book_budget_enabled: bool = False,
    updater: StateUpdater | None = None,
    decision_input: DecisionInput | None = None,
    record_engine_decision_event: Any | None = None,
) -> list[str]:
    issue_type = str(decision.sub_action.get("issue_kind") or "").strip()
    if not issue_type:
        return ["missing_structural_issue_kind"]
    existing = session.execute(
        select(NarrativeObligationRow)
        .where(
            NarrativeObligationRow.project_id == project_id,
            NarrativeObligationRow.origin_chapter_number == int(chapter_number or 0),
            NarrativeObligationRow.obligation_type == issue_type,
            NarrativeObligationRow.status.in_(("planned", "active")),
        )
        .limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        return []

    affected: list[int]
    target_arc_id = ""
    if decision.outcome == "arc_patch":
        target_arc = _arc_for_chapter(
            session=session,
            project_id=project_id,
            chapter_number=chapter_number,
        )
        if target_arc is None:
            return [f"target_arc_not_found:{chapter_number}"]
        target_arc_id = str(target_arc.id or "")
        affected = _future_chapters_for_arc(
            session=session,
            project_id=project_id,
            target_arc_id=target_arc_id,
            current_chapter=chapter_number,
            arc_end=int(target_arc.chapter_end or 0),
        )
    else:
        affected = _future_chapters_for_book(
            session=session,
            project_id=project_id,
            current_chapter=chapter_number,
            target_total_chapters=target_total_chapters,
        )
    if not affected:
        return [f"no_future_structural_patch_chapters:{issue_type}"]

    obligation_id = new_id()
    deadline_chapter = max(affected)
    summary = _summary_for_deferred_issue(
        verdict=verdict,
        issue_type=issue_type,
        outcome_reason=outcome_reason or decision.reason,
    )
    payoff_test = _payoff_test_for_deferred_issue(
        verdict=verdict,
        issue_type=issue_type,
        deadline_chapter=deadline_chapter,
        summary=summary,
    )
    source_signal_ids = _source_signal_ids_for_issue(signals=signals, issue_type=issue_type)
    target_scope = "arc" if decision.outcome == "arc_patch" else "book"
    obligation = NarrativeObligation(
        id=obligation_id,
        project_id=project_id,
        origin_chapter_number=int(chapter_number or 0),
        origin_draft_id=draft_id,
        origin_review_id=review_id,
        origin_signal_ids=source_signal_ids,
        obligation_type=issue_type,
        priority=_priority_for_deferred_issue(issue_type),
        status="proposed",
        summary=summary,
        deferral_reason=decision.reason,
        hardness="design_debt",
        deadline_chapter=deadline_chapter,
        payoff_test=payoff_test,
        evidence_refs=[f"review:{review_id}"] if review_id else [],
        metadata={"minimum_scope": target_scope, "review_engine_rule_id": decision.rule_id},
    )
    if arc_book_budget_enabled:
        budget = evaluate_obligation_budget(
            open_obligations=_open_obligations_for_project(
                session=session,
                project_id=project_id,
            ),
            new_obligations=[obligation],
            current_chapter=chapter_number,
            band_start=chapter_number,
            band_end=deadline_chapter,
            arc_start=(
                int(getattr(target_arc, "chapter_start", 0) or 0)
                if decision.outcome == "arc_patch"
                else 1
            ),
            arc_end=(
                int(getattr(target_arc, "chapter_end", 0) or 0)
                if decision.outcome == "arc_patch"
                else target_total_chapters
            ),
        )
        if budget.over_budget:
            if (
                callable(record_engine_decision_event)
                and updater is not None
                and decision_input is not None
            ):
                record_engine_decision_event(
                    updater=updater,
                    decision=Decision(
                        outcome="system_block",
                        reason=";".join(budget.reasons),
                        rule_id="arc_book_obligation_budget_exceeded",
                        missing_evidence=[],
                        routed_from="AutoDecisionEngine",
                        sub_action={
                            "budget_reasons": list(budget.reasons),
                            "scope": target_scope,
                            "arc_id": target_arc_id,
                            "threshold_source": "ObligationBudgetPolicy",
                        },
                    ),
                    decision_input=decision_input,
                    live_or_shadow="live",
                    baseline_outcome=decision.outcome,
                    engine_outcome="system_block",
                    live_source="engine",
                    shadow_source="",
                    engine_live=True,
                    baseline_shadow_evaluated=False,
                    baseline_safety_net_used=False,
                    related_object_type="chapter_review",
                    related_object_id=review_id,
                )
            return list(budget.reasons)
    if decision.outcome == "arc_patch":
        plan_patch = ArcPlanPatcher().build_patch(
            project_id=project_id,
            origin_chapter_number=chapter_number,
            target_arc_id=target_arc_id,
            issue_kind=issue_type,
            summary=summary,
            source_signal_ids=source_signal_ids,
            source_obligation_ids=[obligation_id],
            payoff_test=payoff_test,
            affected_chapters=affected,
        )
        validation = ArcPatchValidator().validate(plan_patch)
    else:
        plan_patch = BookPlanPatcher().build_patch(
            project_id=project_id,
            origin_chapter_number=chapter_number,
            issue_kind=issue_type,
            summary=summary,
            source_signal_ids=source_signal_ids,
            source_obligation_ids=[obligation_id],
            payoff_test=payoff_test,
            affected_chapters=affected,
        )
        validation = BookPatchValidator().validate(plan_patch)
    if not validation.passed:
        return list(validation.errors)
    result = DeferAcceptanceTransaction(session).run(
        obligation=obligation,
        plan_patch=plan_patch,
        current_chapter=chapter_number,
        target_total_chapters=target_total_chapters,
    )
    return [] if result.success else list(result.errors)


def _open_obligations_for_project(
    *,
    session: Session,
    project_id: str,
) -> list[NarrativeObligation]:
    rows = session.execute(
        select(NarrativeObligationRow).where(
            NarrativeObligationRow.project_id == project_id,
            NarrativeObligationRow.status.in_(("proposed", "planned", "active", "expired")),
        )
    ).scalars().all()
    return [
        NarrativeObligation(
            id=str(row.id or ""),
            project_id=str(row.project_id or ""),
            origin_chapter_number=int(row.origin_chapter_number or 0),
            origin_draft_id=str(row.origin_draft_id or ""),
            origin_review_id=str(row.origin_review_id or ""),
            obligation_type=str(row.obligation_type or ""),
            priority=str(row.priority or "P1"),  # type: ignore[arg-type]
            status=str(row.status or "active"),  # type: ignore[arg-type]
            summary=str(row.summary or ""),
            hardness=str(row.hardness or "design_debt"),
            deadline_chapter=int(row.deadline_chapter or 0),
            payoff_test=str(row.payoff_test or ""),
        )
        for row in rows
    ]


def _arc_for_chapter(
    *,
    session: Session,
    project_id: str,
    chapter_number: int,
) -> ArcPlanVersion | None:
    return session.execute(
        select(ArcPlanVersion)
        .where(
            ArcPlanVersion.project_id == project_id,
            ArcPlanVersion.chapter_start <= int(chapter_number or 0),
            ArcPlanVersion.chapter_end >= int(chapter_number or 0),
        )
        .order_by(ArcPlanVersion.created_at.desc(), ArcPlanVersion.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def _future_chapters_for_arc(
    *,
    session: Session,
    project_id: str,
    target_arc_id: str,
    current_chapter: int,
    arc_end: int,
) -> list[int]:
    rows = session.execute(
        select(ChapterPlan.chapter_number)
        .where(
            ChapterPlan.project_id == project_id,
            ChapterPlan.arc_plan_id == target_arc_id,
            ChapterPlan.chapter_number > int(current_chapter or 0),
            ChapterPlan.status.in_(("planned", "failed")),
        )
        .order_by(ChapterPlan.chapter_number.asc())
    ).scalars().all()
    if rows:
        return [int(chapter) for chapter in rows]
    end = int(arc_end or 0)
    current = int(current_chapter or 0)
    return list(range(current + 1, end + 1)) if end > current else []


def _future_chapters_for_book(
    *,
    session: Session,
    project_id: str,
    current_chapter: int,
    target_total_chapters: int,
) -> list[int]:
    rows = session.execute(
        select(ChapterPlan.chapter_number)
        .where(
            ChapterPlan.project_id == project_id,
            ChapterPlan.chapter_number > int(current_chapter or 0),
            ChapterPlan.status.in_(("planned", "failed")),
        )
        .order_by(ChapterPlan.chapter_number.asc())
    ).scalars().all()
    if rows:
        return [int(chapter) for chapter in rows]
    total = int(target_total_chapters or 0)
    current = int(current_chapter or 0)
    return list(range(current + 1, total + 1)) if total > current else []


def _source_signal_ids_for_issue(*, signals: list[Any], issue_type: str) -> list[str]:
    return [
        str(getattr(signal, "signal_id", "") or "")
        for signal in signals
        if str(getattr(signal, "signal_type", "") or "") == issue_type
        and str(getattr(signal, "signal_id", "") or "")
    ]


def evaluate_structural_patch_completion_debt(
    *,
    project_id: str,
    chapter_number: int,
    is_arc_final_chapter: bool,
    is_book_final_chapter: bool,
    active_patch_debt: list[dict[str, Any]],
) -> dict[str, Any]:
    del project_id
    reasons: list[str] = []
    for item in active_patch_debt:
        patch_id = str(item.get("patch_id") or item.get("id") or "").strip()
        target_scope = str(item.get("target_scope") or item.get("scope") or "").strip()
        if target_scope == "arc" and is_arc_final_chapter:
            reasons.append(f"unresolved_arc_patch_debt:{patch_id or chapter_number}")
        if target_scope == "book" and is_book_final_chapter:
            reasons.append(f"unresolved_book_patch_debt:{patch_id or chapter_number}")
    return {
        "commit_allowed": not reasons,
        "blocking_reasons": reasons,
    }


__all__ = [
    "_persist_structural_patch_outcome",
    "evaluate_structural_patch_completion_debt",
]
