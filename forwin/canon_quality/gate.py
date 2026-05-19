from __future__ import annotations

from typing import Literal

from forwin.narrative_obligations.types import NarrativeObligation, NarrativePlanPatch

from .signals import CanonAdmissionGateResult, CanonQualitySignal


GateMode = Literal["off", "shadow", "fatal_only", "strict"]

_FATAL_ONLY_SIGNAL_TYPES = {
    "character_dead_alive",
    "character_teleport",
    "closed_thread_reopened",
    "final_dangling",
    "final_denied",
    "countdown_inconsistent",
    "countdown_non_monotonic",
    "terminal_state_active_conflict",
    "form_countdown_inconsistency",
    "form_final_chapter_unresolved",
}


def normalize_gate_mode(value: str | None, *, default: GateMode = "strict") -> GateMode:
    normalized = str(value or default).strip().lower()
    if normalized in {"off", "shadow", "fatal_only", "strict"}:
        return normalized  # type: ignore[return-value]
    return default


def _fatal_only_blocking(signals: list[CanonQualitySignal]) -> list[CanonQualitySignal]:
    return [
        signal
        for signal in signals
        if signal.status == "open"
        and signal.severity == "error"
        and str(signal.signal_type) in _FATAL_ONLY_SIGNAL_TYPES
        and bool(signal.evidence_refs)
    ]


def _fatal_only_residual_refs(signals: list[CanonQualitySignal]) -> list[str]:
    return [
        signal.signal_id
        for signal in signals
        if signal.status == "open"
        and signal.severity == "error"
        and str(signal.signal_type) in _FATAL_ONLY_SIGNAL_TYPES
        and not signal.evidence_refs
    ]


def _fatal_only_required_repair_scope(
    signals: list[CanonQualitySignal],
) -> Literal["draft", "chapter_plan", "band", "arc", "book"] | None:
    from forwin.reviewer.repair_scope_router import RepairScopeKind, route_signal_kind

    routed_scopes: list[Literal["draft", "chapter_plan"]] = []
    for signal in signals:
        routed = route_signal_kind(str(signal.signal_type or ""))
        if routed == RepairScopeKind.CHAPTER_PLAN:
            routed_scopes.append("chapter_plan")
        elif routed == RepairScopeKind.DRAFT:
            routed_scopes.append("draft")
    if not routed_scopes:
        return None
    if "chapter_plan" in routed_scopes:
        return "chapter_plan"
    return "draft"


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
    analyzer_results: list[dict] | None = None,
    min_blocking_confidence: float = 0.8,
    require_evidence_for_block: bool = True,
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
    fatal_blocking = _fatal_only_blocking(quality_signals)
    fatal_residual_refs = _fatal_only_residual_refs(quality_signals)
    deterministic_refs = [signal.signal_id for signal in blocking]
    form_blocking_refs = _form_blocking_refs(
        analyzer_results=analyzer_results or [],
        min_blocking_confidence=float(min_blocking_confidence or 0.8),
        require_evidence_for_block=bool(require_evidence_for_block),
    )
    fatal_form_blocking_refs = _form_blocking_refs(
        analyzer_results=analyzer_results or [],
        min_blocking_confidence=float(min_blocking_confidence or 0.8),
        require_evidence_for_block=bool(require_evidence_for_block),
        allowed_signal_types=_FATAL_ONLY_SIGNAL_TYPES,
    )
    llm_issue_refs = form_blocking_refs
    residual_issue_refs: list[str] = []
    required_repair_scope: Literal["draft", "chapter_plan", "band", "arc", "book"] | None = None
    obligation_reasons = _obligation_blocking_reasons(
        obligations=active_obligations,
        plan_patches=available_patches,
        current_chapter=int(chapter_number or 0),
        over_budget=over_budget,
        is_final_chapter=is_final_chapter,
    )
    review_failed = _review_verdict_to_gate_verdict(review_verdict) == "fail"
    blocking_reasons = sorted(
        {
            *obligation_reasons,
            *(["llm_review_fail"] if review_failed else []),
        }
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
    elif resolved_mode == "fatal_only":
        llm_issue_refs = fatal_form_blocking_refs
        residual_issue_refs = fatal_residual_refs
        commit_allowed = (
            not fatal_blocking
            and not fatal_form_blocking_refs
            and not review_failed
            and open_terminal_obligation_count <= 0
            and not obligation_reasons
        )
        admission_mode = (
            "blocked"
            if not commit_allowed
            else ("with_obligation" if active_obligations else "clean")
        )
        verdict = "fail" if not commit_allowed else (
            "warn"
            if warnings or fatal_residual_refs or active_obligations or str(review_verdict) == "warn"
            else "pass"
        )
        blocking = fatal_blocking
        deterministic_refs = [signal.signal_id for signal in fatal_blocking]
        required_repair_scope = _fatal_only_required_repair_scope(fatal_blocking)
        summary = (
            f"canon quality gate fatal_only: commit_allowed={commit_allowed}, "
            f"fatal_blocking={len(fatal_blocking)}, form_blocking={len(fatal_form_blocking_refs)}, "
            f"warnings={len(warnings)}, residual={len(fatal_residual_refs)}, "
            f"open_obligations={open_terminal_obligation_count}, "
            f"narrative_obligations={len(active_obligations)}"
        )
    else:
        commit_allowed = (
            not blocking
            and not form_blocking_refs
            and not review_failed
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
    if resolved_mode != "fatal_only":
        required_repair_scope = "draft" if blocking else None

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
        blocking_reasons=blocking_reasons,
        expired_obligation_ids=expired_obligation_ids,
        over_budget=bool(over_budget),
        blocking_issue_count=len(blocking),
        warning_issue_count=len(warnings),
        open_terminal_obligation_count=max(0, int(open_terminal_obligation_count or 0)),
        deterministic_issue_refs=deterministic_refs,
        llm_issue_refs=llm_issue_refs,
        residual_issue_refs=residual_issue_refs,
        required_repair_scope=required_repair_scope,
        gate_summary=summary,
    )


def _form_blocking_refs(
    *,
    analyzer_results: list[dict],
    min_blocking_confidence: float,
    require_evidence_for_block: bool,
    allowed_signal_types: set[str] | None = None,
) -> list[str]:
    refs: list[str] = []
    for result in analyzer_results:
        if not _result_can_block(result, min_confidence=min_blocking_confidence, require_evidence=require_evidence_for_block):
            continue
        analyzer = str(result.get("analyzer") or "ChapterReviewForm")
        for issue in result.get("issues") or []:
            if not isinstance(issue, dict):
                continue
            if allowed_signal_types is not None:
                signal_type = _issue_signal_type(issue)
                if signal_type not in allowed_signal_types:
                    continue
                if require_evidence_for_block and not _issue_has_evidence(issue):
                    continue
            refs.append(f"{analyzer}:{issue.get('issue_id') or issue.get('type') or 'issue'}")
    return refs


def _issue_signal_type(issue: dict) -> str:
    return str(
        issue.get("signal_type")
        or issue.get("issue_type")
        or issue.get("type")
        or issue.get("kind")
        or issue.get("rule_name")
        or ""
    ).strip()


def _issue_has_evidence(issue: dict) -> bool:
    return bool(issue.get("evidence_quote") or issue.get("evidence_refs"))


def _result_can_block(result: dict, *, min_confidence: float, require_evidence: bool) -> bool:
    if not bool(result.get("blocking", False)):
        return False
    if float(result.get("confidence") or 0.0) < float(min_confidence or 0.0):
        return False
    if not require_evidence:
        return True
    for issue in result.get("issues") or []:
        if not isinstance(issue, dict):
            continue
        if _issue_has_evidence(issue):
            return True
    return False


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
