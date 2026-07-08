from __future__ import annotations


SIGNAL_PRODUCERS: dict[str, str] = {
    "closed_thread_reopened": "forwin.canon_quality.continuity_adapter",
    "dead_character_resurrection": "forwin.canon_quality.continuity_adapter",
    "impossible_location_teleport": "forwin.canon_quality.continuity_adapter",
    "countdown_non_monotonic": "forwin.canon_quality.active_rule_store",
    "terminal_state_active_conflict": "forwin.canon_quality.chapter_review_form.canon_projector",
    "form_countdown_inconsistency": "forwin.canon_quality.chapter_review_form.canon_projector",
    "form_invariant_drift": "forwin.canon_quality.chapter_review_form.canon_projector",
    "form_final_chapter_unresolved": "forwin.canon_quality.chapter_review_form.canon_projector",
    "appellation_referent_conflict": "forwin.canon_quality.readability",
    "internal_key_leakage_v2": "forwin.canon_quality.readability",
    "protagonist_name_missing": "forwin.canon_quality.readability",
    "protagonist_name_diluted": "forwin.canon_quality.readability",
    "chapter_title_mismatch": "forwin.canon_quality.readability",
    "chapter_summary_empty": "forwin.canon_quality.readability",
    "level_rollback": "forwin.canon_quality.chapter_review_form",
    "power_level_rollback": "forwin.canon_quality.chapter_review_form",
    "duplicate_artifact": "forwin.canon_quality.chapter_review_form",
    "duplicate_resource": "forwin.canon_quality.chapter_review_form",
    "duplicate_artifact_resource": "forwin.canon_quality.chapter_review_form",
    "faction_relation_reversal": "forwin.canon_quality.chapter_review_form",
    "protagonist_resource_debt_mismatch": "forwin.canon_quality.chapter_review_form",
    "location_teleport": "forwin.canon_quality.chapter_review_form",
}


def producer_for_signal(signal_type: str) -> str:
    return SIGNAL_PRODUCERS.get(str(signal_type or "").strip(), "")


__all__ = ["SIGNAL_PRODUCERS", "producer_for_signal"]
