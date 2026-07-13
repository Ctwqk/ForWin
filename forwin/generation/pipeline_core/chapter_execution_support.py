from __future__ import annotations

import json

from forwin.audit.events import DecisionEventType
from forwin.audit.gate_outcome import GateOutcome, attach_gate_outcome
from forwin.maintenance.deferred import (
    DeferredMaintenanceRecord,
    record_deferred_maintenance,
)
from forwin.review.issue_groups import issue_group_for_issue
from forwin.state.updater import StateUpdater

_STRUCTURED_EXTRACTION_PARTS = (
    "state_event_extraction",
    "thread_time_extraction",
    "lore_timeline_notes_extraction",
)


def hard_floor_gate_outcome(
    hard_floor,
    *,
    candidate_id: str,
    chapter_number: int,
    policy_version: int,
) -> GateOutcome:
    fail_reasons = [str(item) for item in hard_floor.fail_reasons if str(item)]
    warning_reasons = [
        str(item) for item in hard_floor.warning_reasons if str(item)
    ]
    issue_keys = list(dict.fromkeys([*fail_reasons, *warning_reasons]))
    if not hard_floor.passed:
        decision = "block"
    elif warning_reasons:
        decision = "warn"
    else:
        decision = "pass"
    return GateOutcome(
        gate_id="hard_floor",
        responsibility_domain="draft_quality",
        scope="chapter",
        candidate_id=candidate_id,
        chapter_number=chapter_number,
        policy_version=policy_version,
        fired=bool(issue_keys),
        decision=decision,
        blocked=not bool(hard_floor.passed),
        issue_keys=issue_keys,
        issue_groups=list(
            dict.fromkeys(
                issue_group_for_issue(code=issue_key) for issue_key in issue_keys
            )
        ),
        evidence_refs=[f"hard_floor:{issue_key}" for issue_key in issue_keys],
    )


def checkpoint_event_gate_outcome(
    checkpoint,
    *,
    chapter_number: int,
    policy_version: int,
    decision: str,
    blocked: bool,
) -> GateOutcome:
    try:
        issues = json.loads(str(getattr(checkpoint, "issues_json", "[]") or "[]"))
    except (json.JSONDecodeError, TypeError):
        issues = []
    if not isinstance(issues, list):
        issues = []
    issue_rows = [item for item in issues if isinstance(item, dict)]
    is_manual = str(getattr(checkpoint, "trigger_source", "") or "") == "manual_boundary"
    scope = (
        "band"
        if str(getattr(checkpoint, "boundary_kind", "") or "") == "band_end"
        else "chapter"
    )
    return GateOutcome(
        gate_id="manual_checkpoint" if is_manual else "band_checkpoint",
        responsibility_domain="operator_control" if is_manual else "band_integrity",
        scope=scope,
        candidate_id=str(getattr(checkpoint, "id", "") or ""),
        chapter_number=chapter_number,
        band_id=str(getattr(checkpoint, "band_id", "") or ""),
        policy_version=policy_version,
        fired=True,
        decision=decision,
        blocked=blocked,
        issue_keys=list(
            dict.fromkeys(
                str(item.get("code") or "")
                for item in issue_rows
                if str(item.get("code") or "")
            )
        ),
        issue_groups=list(
            dict.fromkeys(
                str(item.get("issue_group") or "")
                or issue_group_for_issue(code=str(item.get("code") or ""))
                for item in issue_rows
                if str(item.get("code") or "")
            )
        ),
        evidence_refs=list(
            dict.fromkeys(
                str(item.get("detail") or "")
                for item in issue_rows
                if str(item.get("detail") or "")
            )
        ),
    )


def record_pulp_beat_evaluation(
    stage,
    *,
    updater: StateUpdater,
    project_id: str,
    chapter_number: int,
    hard_floor,
    gate_outcome: GateOutcome,
) -> None:
    metadata = getattr(hard_floor, "metadata", {}) or {}
    pulp_beat = metadata.get("pulp_beat") if isinstance(metadata, dict) else None
    if not isinstance(pulp_beat, dict):
        return
    stage._record_decision_event(
        updater=updater,
        project_id=project_id,
        chapter_number=chapter_number,
        event_family="evaluation_verdict",
        event_type=DecisionEventType.PULP_BEAT_EVALUATED,
        scope="chapter",
        summary=f"第{chapter_number}章 pulp beat 已评估。",
        payload=attach_gate_outcome(
            {
                "passed": bool(getattr(hard_floor, "passed", False)),
                "warning_reasons": list(
                    getattr(hard_floor, "warning_reasons", []) or []
                ),
                "pulp_beat": pulp_beat,
            },
            gate_outcome,
        ),
    )


def defer_structured_extraction_if_needed(
    *,
    updater: StateUpdater,
    project_id: str,
    chapter_number: int,
    writer_output,
) -> None:
    meta = dict(getattr(writer_output, "generation_meta", {}) or {})
    status = str(meta.get("structured_extraction", "") or "").strip()
    degraded_parts = [
        part
        for part in _STRUCTURED_EXTRACTION_PARTS
        if str(meta.get(part, "") or "").strip() == "degraded"
    ]
    if (
        status not in {"degraded", "partial_degraded", "deferred"}
        and not degraded_parts
    ):
        return
    record_deferred_maintenance(
        updater,
        DeferredMaintenanceRecord(
            project_id=project_id,
            chapter_number=chapter_number,
            task_type="structured_extraction",
            reason=status or "structured_extraction_degraded",
            payload={
                "structured_extraction": status,
                "degraded_parts": degraded_parts,
            },
        ),
    )


__all__ = [
    "checkpoint_event_gate_outcome",
    "defer_structured_extraction_if_needed",
    "hard_floor_gate_outcome",
    "record_pulp_beat_evaluation",
]
