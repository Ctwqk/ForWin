from __future__ import annotations

from typing import Literal

from forwin.narrative_obligations.types import NarrativeObligation, NarrativePlanPatch

from .signals import CanonAdmissionBlocker, CanonAdmissionGateResult, CanonQualitySignal

GateMode = Literal["off", "shadow", "fatal_only", "pulp_fatal", "serial_fatal", "strict"]

_FATAL_ONLY_SIGNAL_TYPES = {
    "closed_thread_reopened",
    "dead_character_resurrection",
    "impossible_location_teleport",
    "countdown_non_monotonic",
    "terminal_state_active_conflict",
    "form_countdown_inconsistency",
    "form_invariant_drift",
    "form_final_chapter_unresolved",
    "appellation_referent_conflict",
    "internal_key_leakage_v2",
    "protagonist_name_missing",
    "protagonist_name_diluted",
    "chapter_title_mismatch",
    "chapter_summary_empty",
}
_EXPANDED_FATAL_SIGNAL_TYPES = _FATAL_ONLY_SIGNAL_TYPES | {
    "level_rollback",
    "power_level_rollback",
    "duplicate_artifact",
    "duplicate_resource",
    "duplicate_artifact_resource",
    "faction_relation_reversal",
    "protagonist_resource_debt_mismatch",
    "location_teleport",
    "impossible_location_teleport",
}
_FATAL_PROFILE_MODES = {"fatal_only", "pulp_fatal", "serial_fatal"}


def normalize_gate_mode(value: str | None, *, default: GateMode = "strict") -> GateMode:
    normalized = str(value or default).strip().lower()
    if normalized in {"off", "shadow", "fatal_only", "pulp_fatal", "serial_fatal", "strict"}:
        return normalized  # type: ignore[return-value]
    return default


def _fatal_signal_types_for_mode(mode: str) -> set[str]:
    if mode in {"pulp_fatal", "serial_fatal"}:
        return set(_EXPANDED_FATAL_SIGNAL_TYPES)
    return set(_FATAL_ONLY_SIGNAL_TYPES)


def _fatal_only_blocking(
    signals: list[CanonQualitySignal],
    *,
    fatal_signal_types: set[str],
) -> list[CanonQualitySignal]:
    return [
        signal
        for signal in signals
        if signal.status == "open"
        and signal.severity == "error"
        and str(signal.signal_type) in fatal_signal_types
        and bool(signal.evidence_refs)
    ]


def _fatal_only_residual_refs(
    signals: list[CanonQualitySignal],
    *,
    fatal_signal_types: set[str],
) -> list[str]:
    return [
        signal.signal_id
        for signal in signals
        if signal.status == "open"
        and signal.severity == "error"
        and str(signal.signal_type) in fatal_signal_types
        and not signal.evidence_refs
    ]



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
    resolved_obligation_ids: list[str] | None = None,
) -> CanonAdmissionGateResult:
    from forwin.review.repair_scope_router import (
        blockers_for_signals,
        required_scope_for_blockers,
    )

    resolved_mode = normalize_gate_mode(mode)
    fatal_signal_types = _fatal_signal_types_for_mode(resolved_mode)
    quality_signals = list(signals or [])
    infrastructure_items = [
        item
        for item in blockers_for_signals(quality_signals)
        if item.failure_domain == "infrastructure"
    ]
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
    fatal_blocking = _fatal_only_blocking(
        quality_signals,
        fatal_signal_types=fatal_signal_types,
    )
    fatal_residual_refs = _fatal_only_residual_refs(
        quality_signals,
        fatal_signal_types=fatal_signal_types,
    )
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
        allowed_signal_types=fatal_signal_types,
    )
    llm_issue_refs = form_blocking_refs
    residual_issue_refs: list[str] = []
    required_repair_scope: Literal["draft", "chapter_plan", "band", "arc", "book"] | None = None
    obligation_items = _obligation_blocking_items(
        obligations=active_obligations,
        plan_patches=available_patches,
        current_chapter=int(chapter_number or 0),
        over_budget=over_budget,
        is_final_chapter=is_final_chapter,
        p0_only=resolved_mode in {"pulp_fatal", "serial_fatal"},
        resolved_obligation_ids=resolved_obligation_ids or [],
    )
    obligation_reasons = [item.reason for item in obligation_items]
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
    elif resolved_mode in _FATAL_PROFILE_MODES:
        llm_issue_refs = fatal_form_blocking_refs
        residual_issue_refs = fatal_residual_refs
        commit_allowed = (
            not fatal_blocking
            and not fatal_form_blocking_refs
            and not review_failed
            and open_terminal_obligation_count <= 0
            and not obligation_reasons
            and not infrastructure_items
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
        summary = (
            f"canon quality gate {resolved_mode}: commit_allowed={commit_allowed}, "
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
            and not infrastructure_items
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
    blocking_items = [*obligation_items, *blockers_for_signals(blocking)]
    # An absent payoff alone is content debt; actual form failures stop repair.
    known = {item.reason for item in blocking_items}
    blocking_items.extend(
        item for item in infrastructure_items if item.reason not in known
    )
    required_repair_scope = required_scope_for_blockers(blocking_items)

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
        blocking_items=blocking_items,
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
                if not _issue_is_blocking_severity(issue):
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


def _issue_is_blocking_severity(issue: dict) -> bool:
    severity = str(issue.get("severity") or "").strip().lower()
    if severity:
        return severity in {"error", "fatal", "blocking"}
    status = str(
        issue.get("status")
        or issue.get("verdict")
        or issue.get("result")
        or ""
    ).strip().lower()
    if status:
        return status in {"error", "fail", "failed", "blocking", "conflict"}
    return bool(issue.get("blocking"))


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


def obligation_status_blocking_reason(
    obligation: NarrativeObligation, *, p0_only: bool = False
) -> str:
    if (
        p0_only
        and obligation.priority != "P0"
        and obligation.hardness != "hard_blocker"
    ):
        return ""
    if obligation.status in {"expired", "blocked"}:
        return f"{obligation.status}_obligation:{obligation.id}"
    return ""


def obligation_resolution_required(
    obligation: NarrativeObligation, *, current_chapter: int,
    is_final_chapter: bool = False, p0_only: bool = False,
) -> bool:
    """Shared payoff requirement for the quality gate and locked admission."""
    if obligation.status in {"resolved", "waived"}:
        return False
    if p0_only and obligation.priority != "P0" and obligation.hardness != "hard_blocker":
        return False
    return bool(
        (obligation.status == "active" and obligation.deadline_chapter <= current_chapter)
        or (is_final_chapter and obligation.priority in {"P0", "P1"})
    )


def _obligation_blocking_items(
    *,
    obligations: list[NarrativeObligation],
    plan_patches: list[NarrativePlanPatch],
    current_chapter: int,
    over_budget: bool,
    is_final_chapter: bool,
    p0_only: bool = False,
    resolved_obligation_ids: list[str] | None = None,
) -> list[CanonAdmissionBlocker]:
    items: list[CanonAdmissionBlocker] = []
    patches_by_id = {patch.id: patch for patch in plan_patches if patch.id}

    def add(
        reason: str,
        obligation: NarrativeObligation | None = None,
        *,
        content: bool = False,
    ) -> None:
        related = (
            [
                patches_by_id[key]
                for key in obligation.linked_plan_patch_ids
                if key in patches_by_id
            ]
            if obligation
            else []
        )
        items.append(
            CanonAdmissionBlocker(
                reason=reason,
                obligation_id=obligation.id if obligation else "",
                unmet_conditions=[
                    obligation.payoff_test,
                    *obligation.resolution_conditions,
                ]
                if obligation
                else [],
                source="narrative_obligation",
                scope="draft" if content else "operator",
                failure_domain="content" if content else "unsupported",
                subject_refs=list(obligation.subject_refs) if obligation else [],
                evidence_refs=list(obligation.evidence_refs) if obligation else [],
                must_preserve=list(
                    dict.fromkeys(
                        [
                            "Preserve accepted Canon, obligation subjects, payoff conditions and deadlines; do not waive debt.",
                            *[
                                value
                                for patch in related
                                for value in [
                                    *patch.must_preserve,
                                    *patch.must_not_change,
                                ]
                            ],
                        ]
                    )
                ),
            )
        )

    if over_budget:
        add("obligation_budget_exceeded")
    draft_resolved = {str(item).strip() for item in resolved_obligation_ids or [] if str(item).strip()}
    for obligation in obligations:
        obligation_id = obligation.id or "unknown"
        if obligation.status in {"resolved", "waived"}:
            continue
        if p0_only and obligation.priority != "P0" and obligation.hardness != "hard_blocker":
            continue
        status_reason = obligation_status_blocking_reason(obligation, p0_only=p0_only)
        if status_reason:
            add(status_reason, obligation)
        elif obligation.status == "active":
            if obligation_resolution_required(obligation, current_chapter=current_chapter, p0_only=p0_only) and obligation_id not in draft_resolved:
                add(
                    f"obligation_due_unresolved:{obligation_id}",
                    obligation,
                    content=True,
                )
        elif obligation.status != "planned":
            add(f"obligation_not_planned:{obligation_id}", obligation)
        if obligation.hardness == "hard_blocker":
            add(f"hard_blocker_obligation:{obligation_id}", obligation)
        if not int(obligation.deadline_chapter or 0):
            add(f"missing_deadline:{obligation_id}", obligation)
        if not str(obligation.payoff_test or "").strip():
            add(f"missing_payoff_test:{obligation_id}", obligation)
        if is_final_chapter and obligation.priority in {"P0", "P1"} and obligation_resolution_required(obligation, current_chapter=current_chapter, is_final_chapter=True, p0_only=p0_only) and obligation_id not in draft_resolved:
            add(
                f"final_obligation_not_cleared:{obligation_id}",
                obligation,
                content=obligation.status == "active",
            )
        if not obligation.linked_plan_patch_ids:
            add(f"missing_plan_patch:{obligation_id}", obligation)
            continue
        for patch_id in obligation.linked_plan_patch_ids:
            patch = patches_by_id.get(patch_id)
            if patch is None or patch.validation_status != "passed" or not patch.applied:
                add(f"missing_applied_plan_patch:{patch_id}", obligation)
                continue
            if obligation_id not in patch.source_obligation_ids:
                add(
                    f"plan_patch_missing_obligation:{patch_id}:{obligation_id}",
                    obligation,
                )
    return list({(item.reason, item.obligation_id): item for item in items}.values())


def _review_verdict_to_gate_verdict(value: str) -> Literal["pass", "warn", "fail"]:
    normalized = str(value or "pass").strip().lower()
    if normalized in {"pass", "warn", "fail"}:
        return normalized  # type: ignore[return-value]
    return "pass"
