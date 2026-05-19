from __future__ import annotations

from dataclasses import asdict, is_dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from .types import Decision, DecisionInput

LEGACY_COMPATIBILITY_REGISTRY: dict[str, dict[str, Any]] = {
    "book_state.state.location_fallback": {
        "compat_layer": "book_state",
        "default_assessment": "candidate_if_unused",
        "removal_mode": "candidate_if_unused",
        "instrumentation_status": "instrumented",
        "static_patterns": "book_state.state.location_fallback",
        "description": "Fallback from BookState runtime location to legacy state.location.",
    },
    "book_state.state.location_patch_warning": {
        "compat_layer": "book_state",
        "default_assessment": "candidate_if_unused",
        "removal_mode": "candidate_if_unused",
        "instrumentation_status": "instrumented",
        "static_patterns": "book_state.state.location_patch_warning",
        "description": "Legacy state.location patches downgraded to warnings.",
    },
    "projection.legacy_world_model_projection": {
        "compat_layer": "projection",
        "default_assessment": "must_migrate_if_used",
        "removal_mode": "must_migrate_if_used",
        "instrumentation_status": "instrumented",
        "static_patterns": "projection.legacy_world_model_projection",
        "description": "Legacy world model projection compatibility path.",
    },
    "subworld.legacy_entity_id_bridge": {
        "compat_layer": "subworld",
        "default_assessment": "candidate_if_unused",
        "removal_mode": "candidate_if_unused",
        "instrumentation_status": "instrumented",
        "static_patterns": "subworld.legacy_entity_id_bridge",
        "description": "Bridge from SubWorld node metadata legacy_entity_id to canonical entity.",
    },
    "subworld.create_legacy_entity": {
        "compat_layer": "subworld",
        "default_assessment": "must_migrate_if_used",
        "removal_mode": "must_migrate_if_used",
        "instrumentation_status": "instrumented",
        "static_patterns": "subworld.create_legacy_entity",
        "description": "Create legacy entity rows for SubWorld compatibility.",
    },
    "governance.legacy_relaxed_fallback": {
        "compat_layer": "governance",
        "default_assessment": "candidate_if_unused",
        "removal_mode": "candidate_if_unused",
        "instrumentation_status": "instrumented",
        "static_patterns": "governance.legacy_relaxed_fallback",
        "description": "Project governance fallback to legacy_relaxed mode.",
    },
    "api.legacy_checkpoint_status": {
        "compat_layer": "api",
        "default_assessment": "must_migrate_if_used",
        "removal_mode": "must_migrate_if_used",
        "instrumentation_status": "instrumented",
        "static_patterns": "api.legacy_checkpoint_status",
        "description": "Normalize legacy checkpoint status strings in API responses.",
    },
    "migration.legacy_book_state_import": {
        "compat_layer": "migration",
        "default_assessment": "keep_for_import_only",
        "removal_mode": "keep_for_import_only",
        "instrumentation_status": "import_contract",
        "static_patterns": "LegacyBookStateImporter",
        "description": "Legacy BookState import/migration compatibility.",
    },
    "project.creation_status_legacy": {
        "compat_layer": "project",
        "default_assessment": "must_migrate_if_used",
        "removal_mode": "must_migrate_if_used",
        "instrumentation_status": "static_only",
        "static_patterns": [
            'creation_status", "legacy',
            'creation_status", "") or "legacy',
            'creation_status == "legacy',
            'creation_status: str = "legacy"',
            'creation_status: Mapped[str] = mapped_column(String, default="legacy")',
            'creation_status=str(raw.get("creation_status", "legacy"))',
            'creation_status=str(getattr(project, "creation_status", "") or "legacy")',
            'getattr(project, "creation_status", "") or "legacy"',
        ],
        "description": "Project-level legacy creation_status compatibility paths.",
    },
    "characters.create_legacy_entity_default_true": {
        "compat_layer": "characters",
        "default_assessment": "must_migrate_if_used",
        "removal_mode": "must_migrate_if_used",
        "instrumentation_status": "instrumented",
        "static_patterns": "characters.create_legacy_entity_default_true",
        "description": "Character creation path that materializes legacy Entity rows.",
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
    registry: Mapping[str, Mapping[str, Any]] = LEGACY_COMPATIBILITY_REGISTRY,
    static_counts: Mapping[str, int] | None = None,
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
        "static_only_needs_targeted_test": [],
        "uninstrumented_no_delete_signal": [],
        "anomalous_runtime_without_static": [],
    }
    for feature, entry in sorted(registry.items()):
        count = int(by_feature.get(feature, 0))
        mode = str(entry.get("removal_mode") or entry.get("default_assessment") or "").strip()
        instrumentation_status = str(entry.get("instrumentation_status") or "instrumented").strip()
        static_count = (
            int(static_counts.get(feature, 0))
            if static_counts is not None
            else None
        )
        common = {
            "compat_feature": feature,
            "events": count,
            "static_callers": static_count,
        }
        if mode == "out_of_scope":
            assessment["out_of_scope"].append(
                {
                    **common,
                    "reason": "outside this removal audit",
                }
            )
        elif mode == "keep_for_import_only":
            assessment["keep_for_import_only"].append(
                {
                    **common,
                    "reason": (
                        "used during audit window"
                        if count
                        else "import compatibility retained even when unused"
                    ),
                }
            )
        elif instrumentation_status == "uninstrumented" and count == 0:
            assessment["uninstrumented_no_delete_signal"].append(
                {
                    **common,
                    "reason": "runtime instrumentation is missing",
                }
            )
        elif static_count == 0 and count > 0:
            assessment["anomalous_runtime_without_static"].append(
                {
                    **common,
                    "reason": "runtime events exist but static scan found no callers",
                }
            )
        elif count > 0:
            assessment["blocking_for_removal"].append(
                {
                    **common,
                    "reason": "used during audit window",
                }
            )
        elif static_count is not None and static_count > 0:
            assessment["static_only_needs_targeted_test"].append(
                {
                    **common,
                    "reason": "static callers exist but runtime audit did not hit this path",
                }
            )
        elif mode == "candidate_if_unused":
            reason = (
                "unused during audit window and no static callers"
                if static_count == 0
                else "unused during audit window"
            )
            assessment["delete_candidates"].append(
                {
                    **common,
                    "reason": reason,
                }
            )
    return {
        "total_events": len(events),
        "by_layer": by_layer,
        "by_feature": by_feature,
        "static_counts": dict(static_counts or {}),
        "removal_assessment": assessment,
    }


def collect_legacy_compatibility_static_counts(
    root: str | Path = "forwin",
    *,
    registry: Mapping[str, Mapping[str, Any]] = LEGACY_COMPATIBILITY_REGISTRY,
) -> dict[str, int]:
    root_path = Path(root)
    counts = {feature: 0 for feature in registry}
    if not root_path.exists():
        return counts
    for path in root_path.rglob("*.py"):
        if _skip_static_compat_scan_path(path):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = path.read_text(encoding="utf-8", errors="ignore")
        for feature, entry in registry.items():
            patterns = _static_patterns(feature, entry)
            if any(pattern and pattern in text for pattern in patterns):
                counts[feature] += 1
    return counts


def _skip_static_compat_scan_path(path: Path) -> bool:
    parts = set(path.parts)
    if "__pycache__" in parts:
        return True
    if path.name.startswith("test_") or path.name.endswith("_test.py"):
        return True
    normalized = path.as_posix()
    return normalized.endswith("forwin/review_engine/audit.py")


def _static_patterns(feature: str, entry: Mapping[str, Any]) -> list[str]:
    raw_value = entry.get("static_patterns") or feature
    if isinstance(raw_value, (list, tuple)):
        return [str(part) for part in raw_value if str(part)]
    raw = str(raw_value)
    return [part for part in raw.split("|") if part]


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
