from __future__ import annotations

from dataclasses import replace
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.models import new_id
from forwin.models.narrative_obligation import NarrativeObligationRow
from forwin.models.phase import BandExperiencePlan
from forwin.models.project import ArcPlanVersion, ChapterPlan
from forwin.narrative_obligations.budget import evaluate_obligation_budget
from forwin.narrative_obligations.transaction import DeferAcceptanceTransaction
from forwin.narrative_obligations.types import NarrativeObligation, NarrativePlanPatch
from forwin.observability.pipeline_trace import PipelineTraceRecorder
from forwin.planning.arc_patch_validator import ArcPatchValidator
from forwin.planning.arc_plan_patcher import ArcPlanPatcher
from forwin.planning.band_plan_patcher import BandPlanPatcher
from forwin.planning.book_patch_validator import BookPatchValidator
from forwin.planning.book_plan_patcher import BookPlanPatcher
from forwin.protocol.review import ReviewVerdict
from forwin.review.decision.engine import AutoDecisionEngine
from forwin.review.decision.rules.commit_with_obligation import (
    decide_commit_with_obligation,
)
from forwin.review.decision.rules.obligation_scope import (
    BandScopeCandidate,
    decide_obligation_scope,
)
from forwin.review.decision.rules.review_outcome import (
    build_review_outcome_rules,
    review_action_from_decision,
)
from forwin.review.decision.rules.structural_patch import decide_structural_patch
from forwin.review.decision.types import Decision, DecisionInput, PlanLayerHealth
from forwin.runtime.policy import RuntimePolicy
from forwin.state.updater import StateUpdater


def _priority_for_deferred_issue(issue_type: str) -> str:
    normalized = str(issue_type or "").strip()
    if normalized in {"style_repetition_pressure"}:
        return "P3"
    if normalized in {"foreshadowing_payoff", "transition_bridge_needed"}:
        return "P2"
    return "P1"


def _summary_for_deferred_issue(
    *, verdict: ReviewVerdict, issue_type: str, outcome_reason: str
) -> str:
    for issue in verdict.issues:
        if (
            str(
                getattr(issue, "issue_type", "")
                or getattr(issue, "rule_name", "")
                or ""
            )
            == issue_type
        ):
            return str(
                getattr(issue, "description", "") or outcome_reason or issue_type
            )
    return str(outcome_reason or issue_type)


def _payoff_test_for_deferred_issue(
    *,
    verdict: ReviewVerdict,
    issue_type: str,
    deadline_chapter: int,
    summary: str,
) -> str:
    for issue in verdict.issues:
        if (
            str(
                getattr(issue, "issue_type", "")
                or getattr(issue, "rule_name", "")
                or ""
            )
            != issue_type
        ):
            continue
        suggested = str(getattr(issue, "suggested_fix", "") or "").strip()
        if suggested:
            return suggested
    return f"第{int(deadline_chapter or 0)}章前必须偿还：{summary}"


_ENGINE_OUTCOME_TO_REVIEW_ACTION = {
    "accept": "commit_clean",
    "local_repair": "local_rewrite",
    "chapter_patch": "defer_with_chapter_plan_patch",
    "band_patch": "defer_with_band_plan_patch",
    "arc_patch": "defer_with_arc_plan_patch",
    "book_patch": "book_replan_required",
    "commit_with_obligation": "commit_with_obligation",
    "manual_review": "manual_review",
    "system_block": "block",
}


def _review_action_for_engine_decision(decision: Decision) -> str:
    fallback_action = str(decision.sub_action.get("review_action") or "").strip()
    review_action = review_action_from_decision(decision, fallback_action)
    if review_action:
        return review_action
    return _ENGINE_OUTCOME_TO_REVIEW_ACTION.get(
        str(decision.outcome or "").strip(),
        fallback_action,
    )


def prepare_deferred_acceptance(
    *,
    policy: RuntimePolicy,
    recorder: PipelineTraceRecorder,
    session: Session,
    project_id: str,
    chapter_number: int,
    draft_id: str,
    review_id: str,
    verdict: ReviewVerdict,
    signals: list[Any],
    target_total_chapters: int,
) -> list[str]:
    decision_input = DecisionInput(
        project_id=project_id,
        chapter_number=chapter_number,
        review=verdict,
        signals=list(signals),
        open_obligations=[],
        attempts_completed=0,
        prior_scope_history=[],
        budget=None,
        target_total_chapters=target_total_chapters,
        plan_layer_health=PlanLayerHealth(),
    )
    engine_decision = AutoDecisionEngine(build_review_outcome_rules()).decide(
        decision_input
    )
    selected_review_action = _review_action_for_engine_decision(engine_decision)
    selected_review_reason = str(engine_decision.reason or "")
    selected_primary_issue_class = str(
        engine_decision.sub_action.get("primary_issue_class") or ""
    ).strip()
    record_rule_decision = recorder.record_rule_decision
    if callable(record_rule_decision):
        record_rule_decision(
            updater=StateUpdater(session),
            decision=engine_decision,
            decision_input=decision_input,
            related_object_type="chapter_review",
            related_object_id=review_id,
        )
    decision_input = DecisionInput(
        project_id=decision_input.project_id,
        chapter_number=decision_input.chapter_number,
        review=decision_input.review,
        signals=decision_input.signals,
        open_obligations=decision_input.open_obligations,
        attempts_completed=decision_input.attempts_completed,
        prior_scope_history=decision_input.prior_scope_history,
        budget=decision_input.budget,
        target_total_chapters=decision_input.target_total_chapters,
        plan_layer_health=PlanLayerHealth(
            active_chapter_patch_count=(
                1 if selected_review_action == "defer_with_chapter_plan_patch" else 0
            ),
            active_band_patch_count=(
                1 if selected_review_action == "defer_with_band_plan_patch" else 0
            ),
        ),
    )
    structural_decision = decide_structural_patch(
        input=decision_input,
        arc_patcher_enabled=policy.review.allows_repair_scope("arc"),
        book_patcher_enabled=policy.review.allows_repair_scope("book"),
    )
    if structural_decision.outcome in {"arc_patch", "book_patch"}:
        return _persist_structural_patch_outcome(
            session=session,
            project_id=project_id,
            chapter_number=chapter_number,
            draft_id=draft_id,
            review_id=review_id,
            verdict=verdict,
            signals=signals,
            target_total_chapters=target_total_chapters,
            decision=structural_decision,
            outcome_reason=selected_review_reason,
            arc_book_budget_enabled=(
                policy.review.allows_repair_scope("arc")
                and policy.review.allows_repair_scope("book")
            ),
            updater=StateUpdater(session),
            decision_input=decision_input,
            recorder=recorder,
        )
    if structural_decision.rule_id in {"arc_patcher_disabled", "book_patcher_disabled"}:
        return [structural_decision.reason]
    if selected_review_action not in {
        "defer_with_chapter_plan_patch",
        "defer_with_band_plan_patch",
    }:
        return []
    issue_type = selected_primary_issue_class
    if not issue_type:
        return []
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

    bands = _band_scope_candidates(
        session=session,
        project_id=project_id,
        current_chapter=chapter_number,
    )
    scope_decision = decide_obligation_scope(
        issue_type=issue_type,
        priority=_priority_for_deferred_issue(issue_type),
        current_chapter=chapter_number,
        target_total_chapters=target_total_chapters,
        bands=bands,
    )
    if scope_decision.action not in {
        "defer_with_chapter_plan_patch",
        "defer_with_band_plan_patch",
    }:
        return [
            scope_decision.reason
            or f"deferred_acceptance_scope_unavailable:{issue_type}"
        ]
    if policy.review.allows_repair_scope("obligation"):
        commit_decision_input = replace(
            decision_input,
            plan_layer_health=PlanLayerHealth(
                active_chapter_patch_count=(
                    1 if scope_decision.action == "defer_with_chapter_plan_patch" else 0
                ),
                active_band_patch_count=(
                    1 if scope_decision.action == "defer_with_band_plan_patch" else 0
                ),
            ),
        )
        commit_decision = decide_commit_with_obligation(commit_decision_input)
        record_rule = recorder.record_rule_decision
        if callable(record_rule):
            record_rule(
                updater=StateUpdater(session),
                decision=commit_decision,
                decision_input=commit_decision_input,
                related_object_type="chapter_review",
                related_object_id=review_id,
            )
        if commit_decision.outcome == "system_block":
            return list(
                commit_decision.sub_action.get("budget_reasons")
                or [commit_decision.reason]
            )
        if commit_decision.outcome == "manual_review":
            return [commit_decision.reason]

    obligation_id = new_id()
    summary = _summary_for_deferred_issue(
        verdict=verdict,
        issue_type=issue_type,
        outcome_reason=selected_review_reason,
    )
    payoff_test = _payoff_test_for_deferred_issue(
        verdict=verdict,
        issue_type=issue_type,
        deadline_chapter=scope_decision.deadline_chapter,
        summary=summary,
    )
    obligation = NarrativeObligation(
        id=obligation_id,
        project_id=project_id,
        origin_chapter_number=int(chapter_number or 0),
        origin_draft_id=draft_id,
        origin_review_id=review_id,
        origin_signal_ids=[
            str(getattr(signal, "signal_id", "") or "")
            for signal in signals
            if str(getattr(signal, "signal_type", "") or "") == issue_type
            and str(getattr(signal, "signal_id", "") or "")
        ],
        obligation_type=issue_type,
        priority=_priority_for_deferred_issue(issue_type),
        status="proposed",
        summary=summary,
        deferral_reason=scope_decision.reason or selected_review_reason,
        hardness="design_debt",
        deadline_chapter=int(scope_decision.deadline_chapter or 0),
        payoff_test=payoff_test,
        evidence_refs=[f"review:{review_id}"] if review_id else [],
        metadata={"minimum_scope": scope_decision.target_scope},
    )

    if scope_decision.action == "defer_with_band_plan_patch":
        band_row = _band_row_by_id(
            session=session,
            project_id=project_id,
            band_id=scope_decision.target_band_id,
        )
        if band_row is None:
            return [f"target_band_not_found:{scope_decision.target_band_id}"]
        plan_patch = BandPlanPatcher().build_obligation_patch(
            project_id=project_id,
            band_row=band_row,
            obligations=[obligation],
            current_chapter=chapter_number,
            patch_type="band_defer_acceptance",
        )
    else:
        target_plan = session.execute(
            select(ChapterPlan)
            .where(
                ChapterPlan.project_id == project_id,
                ChapterPlan.chapter_number == int(scope_decision.deadline_chapter or 0),
                ChapterPlan.status.in_(("planned", "failed")),
            )
            .limit(1)
        ).scalar_one_or_none()
        if target_plan is None:
            return [f"target_chapter_plan_not_found:{scope_decision.deadline_chapter}"]
        plan_patch = NarrativePlanPatch(
            id=new_id(),
            project_id=project_id,
            patch_type="defer_acceptance",
            target_scope="chapter",
            target_plan_id=str(target_plan.id or ""),
            target_arc_id=str(target_plan.arc_plan_id or ""),
            affected_chapters=[int(scope_decision.deadline_chapter or 0)],
            source_obligation_ids=[obligation_id],
            new_contract={
                "obligations_to_resolve": [obligation_id],
                "payoff_test": payoff_test,
                "summary": summary,
            },
            diff_summary=f"Bind deferred obligation {obligation_id} to chapter {scope_decision.deadline_chapter}.",
            writer_context_injections=[
                {
                    "type": "narrative_obligation",
                    "obligation_id": obligation_id,
                    "priority": obligation.priority,
                    "summary": summary,
                    "payoff_test": payoff_test,
                    "deadline_chapter": obligation.deadline_chapter,
                }
            ],
            reviewer_context_injections=[
                {
                    "type": "narrative_obligation",
                    "obligation_id": obligation_id,
                    "payoff_test": payoff_test,
                    "must_resolve_now": True,
                }
            ],
            expected_resolution_tests=[payoff_test],
        )
    result = DeferAcceptanceTransaction(session).run(
        obligation=obligation,
        plan_patch=plan_patch,
        current_chapter=chapter_number,
        target_total_chapters=target_total_chapters,
    )
    return [] if result.success else list(result.errors)


def _band_scope_candidates(
    *,
    session: Session,
    project_id: str,
    current_chapter: int,
) -> list[BandScopeCandidate]:
    rows = (
        session.execute(
            select(BandExperiencePlan)
            .where(
                BandExperiencePlan.project_id == project_id,
                BandExperiencePlan.chapter_end > int(current_chapter or 0),
            )
            .order_by(
                BandExperiencePlan.chapter_start.asc(),
                BandExperiencePlan.chapter_end.asc(),
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return []
    plans = (
        session.execute(
            select(ChapterPlan).where(
                ChapterPlan.project_id == project_id,
                ChapterPlan.chapter_number > int(current_chapter or 0),
                ChapterPlan.status.in_(("planned", "failed")),
            )
        )
        .scalars()
        .all()
    )
    planned_numbers = [int(plan.chapter_number or 0) for plan in plans]
    result: list[BandScopeCandidate] = []
    for row in rows:
        start = int(row.chapter_start or 0)
        end = int(row.chapter_end or 0)
        result.append(
            BandScopeCandidate(
                band_id=str(row.band_id or ""),
                arc_id=str(row.arc_id or ""),
                chapter_start=start,
                chapter_end=end,
                planned_chapters=[
                    number for number in planned_numbers if start <= number <= end
                ],
            )
        )
    return result


def _band_row_by_id(
    *,
    session: Session,
    project_id: str,
    band_id: str,
) -> BandExperiencePlan | None:
    return session.execute(
        select(BandExperiencePlan)
        .where(
            BandExperiencePlan.project_id == project_id,
            BandExperiencePlan.band_id == band_id,
        )
        .order_by(BandExperiencePlan.created_at.desc(), BandExperiencePlan.id.desc())
        .limit(1)
    ).scalar_one_or_none()


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
    recorder: PipelineTraceRecorder | None = None,
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
    source_signal_ids = _source_signal_ids_for_issue(
        signals=signals, issue_type=issue_type
    )
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
        metadata={"minimum_scope": target_scope, "decision_rule_id": decision.rule_id},
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
                recorder is not None
                and updater is not None
                and decision_input is not None
            ):
                recorder.record_rule_decision(
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
    rows = (
        session.execute(
            select(NarrativeObligationRow).where(
                NarrativeObligationRow.project_id == project_id,
                NarrativeObligationRow.status.in_(
                    ("proposed", "planned", "active", "expired")
                ),
            )
        )
        .scalars()
        .all()
    )
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
    rows = (
        session.execute(
            select(ChapterPlan.chapter_number)
            .where(
                ChapterPlan.project_id == project_id,
                ChapterPlan.arc_plan_id == target_arc_id,
                ChapterPlan.chapter_number > int(current_chapter or 0),
                ChapterPlan.status.in_(("planned", "failed")),
            )
            .order_by(ChapterPlan.chapter_number.asc())
        )
        .scalars()
        .all()
    )
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
    rows = (
        session.execute(
            select(ChapterPlan.chapter_number)
            .where(
                ChapterPlan.project_id == project_id,
                ChapterPlan.chapter_number > int(current_chapter or 0),
                ChapterPlan.status.in_(("planned", "failed")),
            )
            .order_by(ChapterPlan.chapter_number.asc())
        )
        .scalars()
        .all()
    )
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
