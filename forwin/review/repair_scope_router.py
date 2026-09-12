from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

from forwin.canon_quality.signals import (
    CanonAdmissionBlocker,
    CanonQualitySignal,
    SignalKind,
)


class RepairScopeKind(StrEnum):
    """Runtime repair scopes reached directly from signal-kind routing."""

    OPERATOR = "operator"
    ACTIVE_RULES = "active_rules"
    CHAPTER_PLAN = "chapter_plan"
    DRAFT = "draft"


SIGNAL_KIND_TO_SCOPE: dict[str, RepairScopeKind] = {
    SignalKind.form_schema_invalid.value: RepairScopeKind.OPERATOR,
    SignalKind.form_llm_unavailable.value: RepairScopeKind.OPERATOR,
    SignalKind.form_budget_exceeded.value: RepairScopeKind.OPERATOR,
    SignalKind.form_answer_rejected.value: RepairScopeKind.OPERATOR,
    SignalKind.writer_prompt_assembly_error.value: RepairScopeKind.OPERATOR,
    SignalKind.dead_character_resurrection.value: RepairScopeKind.DRAFT,
    SignalKind.closed_thread_reopened.value: RepairScopeKind.CHAPTER_PLAN,
    SignalKind.impossible_location_teleport.value: RepairScopeKind.CHAPTER_PLAN,
    SignalKind.location_teleport.value: RepairScopeKind.CHAPTER_PLAN,
    SignalKind.level_rollback.value: RepairScopeKind.DRAFT,
    SignalKind.power_level_rollback.value: RepairScopeKind.DRAFT,
    SignalKind.duplicate_artifact.value: RepairScopeKind.CHAPTER_PLAN,
    SignalKind.duplicate_resource.value: RepairScopeKind.CHAPTER_PLAN,
    SignalKind.duplicate_artifact_resource.value: RepairScopeKind.CHAPTER_PLAN,
    SignalKind.faction_relation_reversal.value: RepairScopeKind.CHAPTER_PLAN,
    SignalKind.protagonist_resource_debt_mismatch.value: RepairScopeKind.CHAPTER_PLAN,
    SignalKind.form_countdown_inconsistency.value: RepairScopeKind.CHAPTER_PLAN,
    SignalKind.form_invariant_drift.value: RepairScopeKind.CHAPTER_PLAN,
    SignalKind.countdown_non_monotonic.value: RepairScopeKind.ACTIVE_RULES,
    SignalKind.active_rule_missing.value: RepairScopeKind.ACTIVE_RULES,
    SignalKind.form_open_signal_persisting.value: RepairScopeKind.DRAFT,
    SignalKind.personality_drift.value: RepairScopeKind.DRAFT,
    SignalKind.placeholder_leakage.value: RepairScopeKind.DRAFT,
    SignalKind.bare_role_placeholder_leakage.value: RepairScopeKind.DRAFT,
    SignalKind.protagonist_placeholder_leakage.value: RepairScopeKind.DRAFT,
    SignalKind.internal_state_key_leakage.value: RepairScopeKind.DRAFT,
    SignalKind.appellation_referent_conflict.value: RepairScopeKind.DRAFT,
    SignalKind.internal_key_leakage_v2.value: RepairScopeKind.DRAFT,
    SignalKind.protagonist_name_missing.value: RepairScopeKind.DRAFT,
    SignalKind.protagonist_name_diluted.value: RepairScopeKind.DRAFT,
    SignalKind.chapter_title_mismatch.value: RepairScopeKind.DRAFT,
    SignalKind.chapter_summary_empty.value: RepairScopeKind.DRAFT,
    SignalKind.body_duplicate_span.value: RepairScopeKind.DRAFT,
    SignalKind.style_repetition.value: RepairScopeKind.DRAFT,
    SignalKind.form_obligation_unresolved.value: RepairScopeKind.CHAPTER_PLAN,
    SignalKind.form_final_chapter_unresolved.value: RepairScopeKind.CHAPTER_PLAN,
    SignalKind.terminal_state_active_conflict.value: RepairScopeKind.CHAPTER_PLAN,
    SignalKind.artifact_count_conflict.value: RepairScopeKind.CHAPTER_PLAN,
    SignalKind.repeated_reveal_as_new.value: RepairScopeKind.CHAPTER_PLAN,
}

@dataclass(frozen=True)
class RoutedSignal:
    kind: str
    severity: str = "warning"
    subject_key: str = ""
    description: str = ""
    source_signal_id: str = ""
    source: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


def route_signal_kind(kind: str) -> RepairScopeKind:
    return SIGNAL_KIND_TO_SCOPE.get(str(kind or "").strip(), RepairScopeKind.OPERATOR)


__all__ = [
    "RepairScopeKind",
    "RoutedSignal",
    "SIGNAL_KIND_TO_SCOPE",
    "route_signal_kind",
    "blockers_for_signals",
    "required_scope_for_blockers",
]


def blockers_for_signals(
    signals: list[CanonQualitySignal],
) -> list[CanonAdmissionBlocker]:
    items = []
    infrastructure = {
        "form_schema_invalid",
        "form_llm_unavailable",
        "form_budget_exceeded",
        "form_answer_rejected",
        "writer_prompt_assembly_error",
    }
    for signal in signals:
        if signal.status != "open" or signal.severity != "error":
            continue
        scope = route_signal_kind(signal.signal_type)
        domain = (
            "infrastructure"
            if signal.signal_type in infrastructure
            else "content"
            if scope == RepairScopeKind.DRAFT
            else "plan"
            if scope == RepairScopeKind.CHAPTER_PLAN
            else "unsupported"
        )
        items.append(
            CanonAdmissionBlocker(
                reason=signal.signal_id,
                source="canon_quality_signal",
                scope=scope.value,
                failure_domain=domain,
                unmet_conditions=[signal.description or signal.signal_type],
                evidence_refs=signal.evidence_refs,
            )
        )
    return items


def required_scope_for_blockers(items: list[CanonAdmissionBlocker]) -> Literal["draft", "chapter_plan"] | None:
    if not items or any(
        item.failure_domain in {"infrastructure", "unsupported"}
        or item.scope not in {"draft", "chapter_plan"}
        for item in items
    ):
        return None
    return (
        "chapter_plan"
        if any(item.scope == "chapter_plan" for item in items)
        else "draft"
    )
