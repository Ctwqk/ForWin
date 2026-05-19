from __future__ import annotations

from dataclasses import asdict, is_dataclass
import hashlib
import json
from typing import Any, Iterable, Mapping

from .types import Decision, DecisionInput

LEGACY_COMPATIBILITY_REGISTRY: dict[str, dict[str, str]] = {
    "book_state.state.location_fallback": {
        "compat_layer": "book_state",
        "default_assessment": "must_migrate_if_used",
        "description": "Fallback from BookState runtime location to legacy state.location.",
    },
    "book_state.state.location_patch_warning": {
        "compat_layer": "book_state",
        "default_assessment": "must_migrate_if_used",
        "description": "Legacy state.location patches downgraded to warnings.",
    },
    "projection.legacy_world_model_projection": {
        "compat_layer": "projection",
        "default_assessment": "must_migrate_if_used",
        "description": "Legacy world model projection compatibility path.",
    },
    "subworld.legacy_entity_id_bridge": {
        "compat_layer": "subworld",
        "default_assessment": "must_migrate_if_used",
        "description": "Bridge from SubWorld node metadata legacy_entity_id to canonical entity.",
    },
    "subworld.create_legacy_entity": {
        "compat_layer": "subworld",
        "default_assessment": "must_migrate_if_used",
        "description": "Create legacy entity rows for SubWorld compatibility.",
    },
    "governance.legacy_relaxed_fallback": {
        "compat_layer": "governance",
        "default_assessment": "candidate_if_unused",
        "description": "Project governance fallback to legacy_relaxed mode.",
    },
    "api.legacy_checkpoint_status": {
        "compat_layer": "api",
        "default_assessment": "candidate_if_unused",
        "description": "Normalize legacy checkpoint status strings in API responses.",
    },
    "migration.legacy_book_state_import": {
        "compat_layer": "migration",
        "default_assessment": "keep_for_import_only",
        "description": "Legacy BookState import/migration compatibility.",
    },
}


def build_decision_event_payload(
    *,
    decision: Decision,
    input_digest: str,
    shadow_mismatch: bool,
    live_or_shadow: str = "shadow",
    legacy_outcome: str = "",
    engine_outcome: str = "",
    live_source: str = "",
    shadow_source: str = "",
    engine_live: bool = False,
    legacy_shadow_evaluated: bool = False,
    legacy_safety_net_used: bool = False,
    severe_shadow_mismatch: bool = False,
) -> dict[str, object]:
    return {
        "rule_id": decision.rule_id,
        "outcome": decision.outcome,
        "reason": decision.reason,
        "missing_evidence": list(decision.missing_evidence),
        "routed_from": decision.routed_from,
        "sub_action": dict(decision.sub_action),
        "input_digest": input_digest,
        "shadow_mismatch": bool(shadow_mismatch),
        "live_or_shadow": str(live_or_shadow or "shadow"),
        "legacy_outcome": str(legacy_outcome or ""),
        "engine_outcome": str(engine_outcome or ""),
        "live_source": str(live_source or ""),
        "shadow_source": str(shadow_source or ""),
        "engine_live": bool(engine_live),
        "legacy_shadow_evaluated": bool(legacy_shadow_evaluated),
        "legacy_safety_net_used": bool(legacy_safety_net_used),
        "severe_shadow_mismatch": bool(severe_shadow_mismatch),
    }


def build_legacy_compatibility_payload(
    *,
    compat_layer: str,
    compat_feature: str,
    usage_kind: str,
    source_module: str,
    usage_reason: str,
    compat_key: str = "",
    legacy_identifier: str = "",
    canonical_identifier: str = "",
    related_stage: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "compat_layer": str(compat_layer or "").strip(),
        "compat_feature": str(compat_feature or "").strip(),
        "usage_kind": str(usage_kind or "").strip(),
        "source_module": str(source_module or "").strip(),
        "usage_reason": str(usage_reason or "").strip(),
    }
    optional = {
        "compat_key": compat_key,
        "legacy_identifier": legacy_identifier,
        "canonical_identifier": canonical_identifier,
        "related_stage": related_stage,
    }
    for key, value in optional.items():
        text = str(value or "").strip()
        if text:
            payload[key] = text
    if metadata:
        payload["metadata"] = dict(metadata)
    return payload


def summarize_legacy_compatibility_audit(
    rows: Iterable[Mapping[str, Any]],
    *,
    registry: Mapping[str, Mapping[str, str]] = LEGACY_COMPATIBILITY_REGISTRY,
) -> dict[str, object]:
    events: list[Mapping[str, Any]] = []
    for row in rows:
        payload = row.get("payload", row)
        if isinstance(payload, Mapping):
            events.append(payload)

    by_layer: dict[str, int] = {}
    by_feature: dict[str, int] = {}
    for payload in events:
        layer = str(payload.get("compat_layer") or "unknown").strip() or "unknown"
        feature = str(payload.get("compat_feature") or "unknown").strip() or "unknown"
        by_layer[layer] = by_layer.get(layer, 0) + 1
        by_feature[feature] = by_feature.get(feature, 0) + 1

    assessment = {
        "delete_candidates": [],
        "blocking_for_removal": [],
        "keep_for_import_only": [],
        "out_of_scope": [],
    }
    for feature, entry in sorted(registry.items()):
        count = int(by_feature.get(feature, 0))
        mode = str(entry.get("default_assessment") or "").strip()
        if mode == "candidate_if_unused" and count == 0:
            assessment["delete_candidates"].append(
                {
                    "compat_feature": feature,
                    "reason": "unused during audit window",
                }
            )
        elif mode == "must_migrate_if_used" and count > 0:
            assessment["blocking_for_removal"].append(
                {
                    "compat_feature": feature,
                    "reason": "used during audit window",
                    "events": count,
                }
            )
        elif mode == "keep_for_import_only":
            assessment["keep_for_import_only"].append(
                {
                    "compat_feature": feature,
                    "reason": (
                        "used during audit window"
                        if count
                        else "import compatibility retained even when unused"
                    ),
                    "events": count,
                }
            )
        elif mode == "out_of_scope":
            assessment["out_of_scope"].append(
                {
                    "compat_feature": feature,
                    "reason": "outside this removal audit",
                    "events": count,
                }
            )

    return {
        "total_events": len(events),
        "by_layer": by_layer,
        "by_feature": by_feature,
        "removal_assessment": assessment,
    }


def summarize_live_cutover_audit(
    rows: Iterable[Mapping[str, Any]],
    *,
    expected_chapters: int = 60,
) -> dict[str, object]:
    expected = max(0, int(expected_chapters or 0))
    by_chapter: dict[int, list[Mapping[str, Any]]] = {}
    for row in rows:
        payload = row.get("payload", row)
        if not isinstance(payload, Mapping):
            payload = {}
        chapter = int(row.get("chapter_number") or payload.get("chapter_number") or 0)
        if chapter <= 0:
            continue
        by_chapter.setdefault(chapter, []).append(payload)

    expected_range = list(range(1, expected + 1)) if expected else sorted(by_chapter)
    missing_chapters = [chapter for chapter in expected_range if chapter not in by_chapter]
    legacy_safety_net_chapters = [
        chapter
        for chapter, payloads in sorted(by_chapter.items())
        if any(_uses_legacy_safety_net(payload) for payload in payloads)
    ]
    severe_mismatch_chapters = [
        chapter
        for chapter, payloads in sorted(by_chapter.items())
        if any(bool(payload.get("severe_shadow_mismatch")) for payload in payloads)
    ]
    non_live_chapters = [
        chapter
        for chapter, payloads in sorted(by_chapter.items())
        if not any(_is_engine_live_payload(payload) for payload in payloads)
    ]
    engine_live_chapters = [
        chapter
        for chapter, payloads in sorted(by_chapter.items())
        if any(_is_engine_live_payload(payload) for payload in payloads)
    ]
    passed = not (
        missing_chapters
        or legacy_safety_net_chapters
        or severe_mismatch_chapters
        or non_live_chapters
    )
    return {
        "passed": passed,
        "expected_chapters": expected,
        "observed_chapters": len(by_chapter),
        "engine_live_chapters": len(engine_live_chapters),
        "missing_chapters": missing_chapters,
        "legacy_safety_net_chapters": legacy_safety_net_chapters,
        "severe_mismatch_chapters": severe_mismatch_chapters,
        "non_live_chapters": non_live_chapters,
    }


def _uses_legacy_safety_net(payload: Mapping[str, Any]) -> bool:
    return (
        bool(payload.get("legacy_safety_net_used"))
        or str(payload.get("live_source") or "") == "legacy"
        or (
            str(payload.get("live_or_shadow") or "") == "live"
            and str(payload.get("routed_from") or "") in {"ReviewOutcomeRouter", "RepairPolicy"}
        )
    )


def _is_engine_live_payload(payload: Mapping[str, Any]) -> bool:
    return (
        str(payload.get("live_or_shadow") or "") == "live"
        and bool(payload.get("engine_live"))
        and str(payload.get("live_source") or "") == "engine"
    )


def digest_decision_input(input: DecisionInput) -> str:
    payload = {
        "project_id": input.project_id,
        "chapter_number": input.chapter_number,
        "review": _jsonable(input.review),
        "signals": [_jsonable(signal) for signal in input.signals],
        "open_obligations": [_jsonable(item) for item in input.open_obligations],
        "operation_mode": input.operation_mode,
        "attempts_completed": input.attempts_completed,
        "prior_scope_history": list(input.prior_scope_history),
        "budget": _jsonable(input.budget),
        "target_total_chapters": input.target_total_chapters,
        "plan_layer_health": _jsonable(input.plan_layer_health),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _jsonable(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if is_dataclass(value):
        return asdict(value)
    return value
