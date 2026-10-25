"""Stable identity for the chapter-plan inputs used by writing and review."""

from __future__ import annotations

import hashlib
import json

from forwin.models.project import ChapterPlan


def chapter_plan_fingerprint(plan: ChapterPlan | None) -> str:
    """The chapter plan revision, or the explicit no-plan fingerprint."""
    return plan_fingerprint(plan) if plan is not None else ""


def plan_fingerprint(plan: ChapterPlan) -> str:
    """Stable revision of every chapter-plan input used by writing and review."""
    payload = {
        "chapter_plan_id": str(plan.id or ""),
        "arc_plan_id": str(plan.arc_plan_id or ""),
        "chapter_number": int(plan.chapter_number or 0),
        "title": str(plan.title or ""),
        "one_line": str(plan.one_line or ""),
        "band_plan_id": plan.band_plan_id,
        "pov_ref": str(plan.pov_ref or ""),
        "expected_span_minutes": plan.expected_span_minutes,
        "hook": str(plan.hook or ""),
    }
    for field in (
        "goals_json", "experience_plan_json", "onstage_refs_json",
        "location_refs_json", "world_bindings_json", "reveal_directives_json",
        "hint_directives_json", "intended_changes_json", "milestone_ids_json",
    ):
        raw = getattr(plan, field) or ("{}" if field == "experience_plan_json" else "[]")
        try:
            payload[field] = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            payload[field] = raw
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
