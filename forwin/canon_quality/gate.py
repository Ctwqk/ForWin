from __future__ import annotations

from typing import Literal

from forwin.narrative_obligations.types import NarrativeObligation, NarrativePlanPatch

from .signals import CanonAdmissionGateResult, CanonQualitySignal


GateMode = Literal["off", "shadow", "strict"]


def normalize_gate_mode(value: str | None, *, default: GateMode = "strict") -> GateMode:
    normalized = str(value or default).strip().lower()
    if normalized in {"off", "shadow", "strict"}:
        return normalized  # type: ignore[return-value]
    return default


def evaluate_canon_admission(
    *,
    project_id: str,
    chapter_number: int,
    draft_id: str = "",
    review_id: str = "",
    review_verdict: str = "pass",
    signals: list[CanonQualitySignal] | None = None,
    obligations: list[NarrativeObligation] | None = None,
    plan_patches: list[NarrativePlanPatch] | None = None,
    mode: str = "strict",
    open_terminal_obligation_count: int = 0,
    over_budget: bool = False,
    is_final_chapter: bool = False,
) -> CanonAdmissionGateResult:
    resolved_mode = normalize_gate_mode(mode)
    quality_signals = list(signals or [])
    active_obligations = list(obligations or [])
    available_patches = list(plan_patches or [])
    blocking = [
        signal
        for signal in quality_signals
        if signal.status == "open" and signal.severity == "error"
    ]
    warnings = [
        signal
        for signal in quality_signals
        if signal.status == "open" and signal.severity == "warning"
    ]
    deterministic_refs = [signal.signal_id for signal in blocking]
    obligation_reasons = _obligation_blocking_reasons(
        obligations=active_obligations,
        plan_patches=available_patches,
        current_chapter=int(chapter_number or 0),
        over_budget=over_budget,
        is_final_chapter=is_final_chapter,
    )
    obligation_ids = [item.id for item in active_obligations if item.id]
    required_plan_patch_ids = sorted(
        {
            patch_id
            for item in active_obligations
            for patch_id in item.linked_plan_patch_ids
            if patch_id
        }
    )
    expired_obligation_ids = [
        item.id for item in active_obligations if item.id and item.status == "expired"
    ]
    if resolved_mode == "off":
        commit_allowed = True
        verdict = _review_verdict_to_gate_verdict(review_verdict)
        admission_mode: Literal["clean", "with_obligation", "blocked", "manual_required"] = (
            "with_obligation" if active_obligations else "clean"
        )
        summary = "canon quality gate off"
    elif resolved_mode == "shadow":
        commit_allowed = True
        admission_mode = "with_obligation" if active_obligations else "clean"
        verdict = (
            "warn"
            if blocking or warnings or open_terminal_obligation_count or obligation_reasons or active_obligations
            else _review_verdict_to_gate_verdict(review_verdict)
        )
        summary = (
            f"canon quality gate shadow: blocking={len(blocking)}, "
            f"warnings={len(warnings)}, open_obligations={open_terminal_obligation_count}, "
            f"narrative_obligations={len(active_obligations)}"
        )
    else:
        commit_allowed = (
            not blocking
            and open_terminal_obligation_count <= 0
            and not obligation_reasons
        )
        admission_mode = (
            "blocked"
            if not commit_allowed
            else ("with_obligation" if active_obligations else "clean")
        )
        verdict = "fail" if not commit_allowed else (
            "warn" if warnings or active_obligations or str(review_verdict) == "warn" else "pass"
        )
        summary = (
            f"canon quality gate strict: commit_allowed={commit_allowed}, "
            f"blocking={len(blocking)}, warnings={len(warnings)}, "
            f"open_obligations={open_terminal_obligation_count}, "
            f"narrative_obligations={len(active_obligations)}"
        )

    return CanonAdmissionGateResult(
        project_id=project_id,
        chapter_number=int(chapter_number or 0),
        draft_id=draft_id,
        review_id=review_id,
        commit_allowed=commit_allowed,
        verdict=verdict,
        admission_mode=admission_mode,
        obligation_ids=obligation_ids,
        required_plan_patch_ids=required_plan_patch_ids,
        blocking_reasons=obligation_reasons,
        expired_obligation_ids=expired_obligation_ids,
        over_budget=bool(over_budget),
        blocking_issue_count=len(blocking),
        warning_issue_count=len(warnings),
        open_terminal_obligation_count=max(0, int(open_terminal_obligation_count or 0)),
        deterministic_issue_refs=deterministic_refs,
        required_repair_scope="draft" if blocking else None,
        gate_summary=summary,
    )


def _obligation_blocking_reasons(
    *,
    obligations: list[NarrativeObligation],
    plan_patches: list[NarrativePlanPatch],
    current_chapter: int,
    over_budget: bool,
    is_final_chapter: bool,
) -> list[str]:
    reasons: list[str] = []
    if over_budget:
        reasons.append("obligation_budget_exceeded")
    patches_by_id = {patch.id: patch for patch in plan_patches if patch.id}
    for obligation in obligations:
        obligation_id = obligation.id or "unknown"
        if obligation.status in {"resolved", "waived"}:
            continue
        if obligation.status == "expired":
            reasons.append(f"expired_obligation:{obligation_id}")
        elif obligation.status == "active":
            if int(obligation.deadline_chapter or 0) <= int(current_chapter or 0):
                reasons.append(f"obligation_due_unresolved:{obligation_id}")
        elif obligation.status != "planned":
            reasons.append(f"obligation_not_planned:{obligation_id}")
        if obligation.hardness == "hard_blocker":
            reasons.append(f"hard_blocker_obligation:{obligation_id}")
        if not int(obligation.deadline_chapter or 0):
            reasons.append(f"missing_deadline:{obligation_id}")
        if not str(obligation.payoff_test or "").strip():
            reasons.append(f"missing_payoff_test:{obligation_id}")
        if is_final_chapter and obligation.priority in {"P0", "P1"}:
            reasons.append(f"final_obligation_not_cleared:{obligation_id}")
        if not obligation.linked_plan_patch_ids:
            reasons.append(f"missing_plan_patch:{obligation_id}")
            continue
        for patch_id in obligation.linked_plan_patch_ids:
            patch = patches_by_id.get(patch_id)
            if patch is None or patch.validation_status != "passed" or not patch.applied:
                reasons.append(f"missing_applied_plan_patch:{patch_id}")
                continue
            if obligation_id not in patch.source_obligation_ids:
                reasons.append(f"plan_patch_missing_obligation:{patch_id}:{obligation_id}")
    return sorted(set(reasons))


def _review_verdict_to_gate_verdict(value: str) -> Literal["pass", "warn", "fail"]:
    normalized = str(value or "pass").strip().lower()
    if normalized in {"pass", "warn", "fail"}:
        return normalized  # type: ignore[return-value]
    return "pass"
