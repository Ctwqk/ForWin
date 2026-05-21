from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import select

from forwin.api_project_payloads import normalize_project_automation
from forwin.api_schemas import (
    BandCheckpointDetail,
    CausalReplayResponse,
    DecisionEventInfo,
    GovernanceInsightsResponse,
    NarrativeConstraintInfo,
    ProjectAutomationSettings,
)
from forwin.config import Config
from forwin.governance import (
    BandCheckpointIssueInfo,
    CONSTRAINT_LEVELS,
    CONSTRAINT_STATUSES,
    CONSTRAINT_TYPES,
    DecisionEventType,
    ensure_decision_event_type,
    issue_group_for_issue,
    normalize_checkpoint_status,
    normalize_project_governance,
)
from forwin.models.governance import BandCheckpoint, DecisionEvent, NarrativeConstraint
from forwin.models.phase import BandExperiencePlan
from forwin.models.project import ArcPlanVersion, Project
from forwin.orchestrator.feedback_aggregator import derive_action_effectiveness

_DISPLAY_TZ = ZoneInfo("America/Los_Angeles")


def _display_datetime(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(_DISPLAY_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")


def _json_load_list(raw: str | None) -> list[Any]:
    try:
        value = json.loads(str(raw or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def _json_load_object(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def validate_constraint_payload(*, constraint_type: str, level: str, status: str) -> tuple[str, str, str]:
    normalized_type = str(constraint_type or "").strip()
    normalized_level = str(level or "hard").strip()
    normalized_status = str(status or "active").strip() or "active"
    if normalized_type not in CONSTRAINT_TYPES:
        raise HTTPException(400, f"未知 constraint_type: {normalized_type or '<empty>'}")
    if normalized_level not in CONSTRAINT_LEVELS:
        raise HTTPException(400, f"未知 constraint level: {normalized_level or '<empty>'}")
    if normalized_status not in CONSTRAINT_STATUSES:
        raise HTTPException(400, f"未知 constraint status: {normalized_status or '<empty>'}")
    return normalized_type, normalized_level, normalized_status


def persist_project_automation(
    session,
    project: Project,
    automation: ProjectAutomationSettings,
) -> ProjectAutomationSettings:
    normalized = normalize_project_automation(automation.model_dump(mode="json"))
    project.automation_json = json.dumps(
        normalized.model_dump(mode="json"),
        ensure_ascii=False,
    )
    session.add(project)
    session.flush()
    return normalized


def governance_request_payload(req: object) -> dict[str, object]:
    if req is None:
        return {}
    payload: dict[str, object] = {}
    for field in (
        "default_operation_mode",
        "operation_mode",
        "review_interval_chapters",
        "progression_mode",
        "auto_band_checkpoint",
        "band_warn_action",
        "manual_checkpoints_enabled",
        "future_constraints_enabled",
        "generation_audit_interval_chapters",
        "generation_audit_pause_enabled",
    ):
        if not hasattr(req, field):
            continue
        value = getattr(req, field)
        if value is None:
            continue
        target_field = "default_operation_mode" if field == "operation_mode" else field
        payload[target_field] = value
    return payload


def resolve_project_governance(
    project: Project | None,
    *,
    overrides: dict[str, object] | None = None,
    base_config: Config | None = None,
) -> object:
    fallback_operation_mode = (
        base_config.operation_mode if base_config is not None else "blackbox"
    )
    fallback_review_interval = (
        max(0, int(base_config.review_interval_chapters or 0))
        if base_config is not None
        else 0
    )
    raw = project.governance_json if project is not None else "{}"
    governance = normalize_project_governance(
        raw,
        fallback_operation_mode=fallback_operation_mode,
        fallback_review_interval=fallback_review_interval,
    )
    merged = governance.model_dump(mode="json")
    for key, value in (overrides or {}).items():
        merged[key] = value
    return normalize_project_governance(
        merged,
        fallback_operation_mode=fallback_operation_mode,
        fallback_review_interval=fallback_review_interval,
    )


def persist_project_governance(
    session,
    project: Project,
    governance,
    *,
    base_config: Config | None = None,
) -> object:
    normalized = resolve_project_governance(project, overrides=governance.model_dump(mode="json"), base_config=base_config)
    project.governance_json = json.dumps(
        normalized.model_dump(mode="json"),
        ensure_ascii=False,
    )
    session.add(project)
    session.flush()
    return normalized


def log_decision_event(
    session,
    *,
    project_id: str,
    event_family: str,
    event_type: str,
    summary: str,
    reason: str = "",
    scope: str = "project",
    actor_type: str = "system",
    actor_id: str = "",
    band_id: str = "",
    chapter_number: int = 0,
    task_id: str = "",
    payload: dict[str, Any] | None = None,
    related_object_type: str = "",
    related_object_id: str = "",
    parent_event_id: str = "",
    causal_root_id: str = "",
) -> DecisionEvent:
    row = DecisionEvent(
        project_id=project_id,
        task_id=task_id,
        band_id=band_id,
        chapter_number=chapter_number,
        scope=scope,
        event_family=event_family,
        event_type=ensure_decision_event_type(event_type),
        actor_type=actor_type,
        actor_id=actor_id,
        summary=summary,
        reason=reason,
        payload_json=json.dumps(payload or {}, ensure_ascii=False),
        related_object_type=related_object_type,
        related_object_id=related_object_id,
        parent_event_id=parent_event_id,
        causal_root_id=causal_root_id,
    )
    session.add(row)
    session.flush()
    if not str(row.causal_root_id or "").strip():
        row.causal_root_id = row.id
        session.add(row)
        session.flush()
    return row


def latest_band_checkpoint_row(
    session,
    *,
    project_id: str,
    band_id: str = "",
) -> BandCheckpoint | None:
    stmt = select(BandCheckpoint).where(BandCheckpoint.project_id == project_id)
    if band_id:
        stmt = stmt.where(BandCheckpoint.band_id == band_id)
    return session.execute(
        stmt.order_by(BandCheckpoint.created_at.desc(), BandCheckpoint.id.desc()).limit(1)
    ).scalar_one_or_none()


def serialize_band_checkpoint(row: BandCheckpoint, *, session=None) -> BandCheckpointDetail:
    issues_payload = _json_load_list(row.issues_json)
    normalized_status = normalize_checkpoint_status(row.status)
    return BandCheckpointDetail(
        id=row.id,
        project_id=row.project_id,
        arc_id=row.arc_id,
        band_id=row.band_id,
        chapter_start=int(row.chapter_start or 0),
        chapter_end=int(row.chapter_end or 0),
        trigger_source=str(row.trigger_source or ""),
        boundary_kind=str(row.boundary_kind or ""),
        boundary_chapter=int(row.boundary_chapter or 0),
        status=normalized_status,
        summary=str(row.summary or ""),
        reason=str(row.reason or ""),
        issues=[
            BandCheckpointIssueInfo.model_validate(item)
            for item in issues_payload
            if isinstance(item, dict)
        ],
        decision_refs=decision_refs_for_checkpoint(session, row) if session is not None else [],
        created_at=_display_datetime(row.created_at),
        updated_at=_display_datetime(row.updated_at),
        resolved_at=_display_datetime(row.resolved_at or (row.updated_at if normalized_status in {"pass", "overridden"} else None)),
    )


def serialize_constraint(row: NarrativeConstraint) -> NarrativeConstraintInfo:
    payload = json.loads(row.payload_json or "{}") if str(row.payload_json or "").strip() else {}
    if not isinstance(payload, dict):
        payload = {}
    return NarrativeConstraintInfo(
        id=row.id,
        project_id=row.project_id,
        arc_id=row.arc_id,
        band_id=row.band_id,
        constraint_type=row.constraint_type,
        level=row.level,
        subject_name=row.subject_name,
        description=row.description,
        payload=payload,
        effective_from_chapter=int(row.effective_from_chapter or 1),
        protect_until_chapter=int(row.protect_until_chapter or 0),
        status=row.status,
        created_at=_display_datetime(row.created_at),
        updated_at=_display_datetime(row.updated_at),
    )


def serialize_decision_event(row: DecisionEvent) -> DecisionEventInfo:
    payload = json.loads(row.payload_json or "{}") if str(row.payload_json or "").strip() else {}
    if not isinstance(payload, dict):
        payload = {}
    return DecisionEventInfo(
        id=row.id,
        project_id=row.project_id,
        task_id=row.task_id,
        band_id=row.band_id,
        chapter_number=int(row.chapter_number or 0),
        scope=row.scope,
        event_family=row.event_family,
        event_type=row.event_type,
        actor_type=row.actor_type,
        actor_id=row.actor_id,
        summary=row.summary,
        reason=row.reason,
        payload=payload,
        related_object_type=row.related_object_type,
        related_object_id=row.related_object_id,
        parent_event_id=str(getattr(row, "parent_event_id", "") or ""),
        causal_root_id=str(getattr(row, "causal_root_id", "") or ""),
        created_at=_display_datetime(row.created_at),
    )


def decision_event_stmt(
    *,
    project_id: str,
    scope: str = "",
    band_id: str = "",
    chapter_number: int = 0,
    task_id: str = "",
    event_family: str = "",
    related_object_type: str = "",
    related_object_id: str = "",
    causal_root_id: str = "",
):
    stmt = select(DecisionEvent).where(DecisionEvent.project_id == project_id)
    if scope:
        stmt = stmt.where(DecisionEvent.scope == scope)
    if band_id:
        stmt = stmt.where(DecisionEvent.band_id == band_id)
    if chapter_number > 0:
        stmt = stmt.where(DecisionEvent.chapter_number == chapter_number)
    if task_id:
        stmt = stmt.where(DecisionEvent.task_id == task_id)
    if event_family:
        stmt = stmt.where(DecisionEvent.event_family == event_family)
    if related_object_type:
        stmt = stmt.where(DecisionEvent.related_object_type == related_object_type)
    if related_object_id:
        stmt = stmt.where(DecisionEvent.related_object_id == related_object_id)
    if causal_root_id:
        stmt = stmt.where(DecisionEvent.causal_root_id == causal_root_id)
    return stmt


def list_decision_event_rows(
    session,
    *,
    project_id: str,
    scope: str = "",
    band_id: str = "",
    chapter_number: int = 0,
    task_id: str = "",
    event_family: str = "",
    related_object_type: str = "",
    related_object_id: str = "",
    causal_root_id: str = "",
    limit: int = 200,
    ascending: bool = False,
) -> list[DecisionEvent]:
    order_clause = (
        (DecisionEvent.created_at.asc(), DecisionEvent.id.asc())
        if ascending
        else (DecisionEvent.created_at.desc(), DecisionEvent.id.desc())
    )
    return session.execute(
        decision_event_stmt(
            project_id=project_id,
            scope=scope,
            band_id=band_id,
            chapter_number=chapter_number,
            task_id=task_id,
            event_family=event_family,
            related_object_type=related_object_type,
            related_object_id=related_object_id,
            causal_root_id=causal_root_id,
        )
        .order_by(*order_clause)
        .limit(max(1, limit))
    ).scalars().all()


def latest_related_decision_event(
    session,
    *,
    project_id: str,
    related_object_type: str = "",
    related_object_id: str = "",
    band_id: str = "",
    chapter_number: int = 0,
) -> DecisionEvent | None:
    rows = list_decision_event_rows(
        session,
        project_id=project_id,
        related_object_type=related_object_type,
        related_object_id=related_object_id,
        band_id=band_id,
        chapter_number=chapter_number,
        limit=1,
        ascending=False,
    )
    if rows:
        return rows[0]
    if related_object_type or related_object_id:
        return None
    rows = list_decision_event_rows(
        session,
        project_id=project_id,
        band_id=band_id,
        chapter_number=chapter_number,
        limit=1,
        ascending=False,
    )
    return rows[0] if rows else None


def decision_refs_for_checkpoint(session, row: BandCheckpoint) -> list[DecisionEventInfo]:
    rows = list_decision_event_rows(
        session,
        project_id=row.project_id,
        related_object_type="band_checkpoint",
        related_object_id=row.id,
        limit=50,
        ascending=True,
    )
    return [serialize_decision_event(item) for item in rows]


def decision_refs_for_chapter_review(
    session,
    *,
    project_id: str,
    chapter_number: int,
    review_id: str,
) -> list[DecisionEventInfo]:
    allowed_types = {
        DecisionEventType.REVIEW_VERDICT_RECORDED,
        DecisionEventType.REPAIR_STARTED,
        DecisionEventType.REPAIR_FAILED,
        DecisionEventType.REPAIR_SUCCEEDED,
        DecisionEventType.FORCED_ACCEPT_APPLIED,
        DecisionEventType.REVIEW_APPROVED,
        DecisionEventType.CANON_COMMIT,
        DecisionEventType.CANON_COMMIT_FAILED,
        DecisionEventType.HARD_GATE_HIT,
    }
    ordered: dict[str, DecisionEventInfo] = {}
    rows = list_decision_event_rows(
        session,
        project_id=project_id,
        related_object_type="chapter_review",
        related_object_id=review_id,
        limit=80,
        ascending=True,
    )
    for row in rows:
        event = serialize_decision_event(row)
        ordered[event.id] = event
    rows = list_decision_event_rows(
        session,
        project_id=project_id,
        chapter_number=chapter_number,
        scope="chapter",
        limit=120,
        ascending=True,
    )
    for row in rows:
        if str(row.event_type or "") not in allowed_types:
            continue
        event = serialize_decision_event(row)
        ordered.setdefault(event.id, event)
    return list(ordered.values())


def counter_rows(counter: Counter[str], *, limit: int = 5) -> list[dict[str, Any]]:
    return [
        {"name": name, "count": count}
        for name, count in counter.most_common(max(1, limit))
        if str(name or "").strip()
    ]


def build_causal_replay(
    session,
    *,
    project_id: str,
    scope: str = "",
    arc_id: str = "",
    band_id: str = "",
    chapter_number: int = 0,
    task_id: str = "",
) -> CausalReplayResponse:
    if str(scope or "").strip() == "arc":
        target_arc_id = str(arc_id or "").strip()
        if not target_arc_id:
            active_arc = session.execute(
                select(ArcPlanVersion)
                .where(ArcPlanVersion.project_id == project_id, ArcPlanVersion.status == "active")
                .order_by(ArcPlanVersion.version.desc(), ArcPlanVersion.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            target_arc_id = str(active_arc.id if active_arc is not None else "")
        if not target_arc_id:
            return CausalReplayResponse(current_outcome="no_active_arc")
        bands = session.execute(
            select(BandExperiencePlan)
            .where(BandExperiencePlan.project_id == project_id, BandExperiencePlan.arc_id == target_arc_id)
            .order_by(BandExperiencePlan.chapter_start.asc(), BandExperiencePlan.created_at.asc())
        ).scalars().all()
        band_ids = {str(row.band_id or "") for row in bands if str(row.band_id or "").strip()}
        chapter_numbers: set[int] = set()
        for band in bands:
            start = int(band.chapter_start or 0)
            end = int(band.chapter_end or 0)
            if start and end >= start:
                chapter_numbers.update(range(start, end + 1))
        rows = list_decision_event_rows(
            session,
            project_id=project_id,
            limit=1000,
            ascending=True,
        )
        scoped_rows: list[DecisionEvent] = []
        checkpoint_ids = set(
            session.execute(
                select(BandCheckpoint.id).where(
                    BandCheckpoint.project_id == project_id,
                    BandCheckpoint.arc_id == target_arc_id,
                )
            ).scalars().all()
        )
        for row in rows:
            payload = _json_load_object(getattr(row, "payload_json", "") or "")
            if str(payload.get("arc_id") or "") == target_arc_id:
                scoped_rows.append(row)
                continue
            if str(getattr(row, "band_id", "") or "") in band_ids:
                scoped_rows.append(row)
                continue
            if int(getattr(row, "chapter_number", 0) or 0) in chapter_numbers:
                scoped_rows.append(row)
                continue
            if (
                str(getattr(row, "related_object_type", "") or "") == "band_checkpoint"
                and str(getattr(row, "related_object_id", "") or "") in checkpoint_ids
            ):
                scoped_rows.append(row)
        items = [serialize_decision_event(row) for row in scoped_rows]
        scope_rank = {"arc": 0, "band": 1, "chapter": 2, "project": 3, "task": 4}
        items.sort(
            key=lambda item: (
                int(item.chapter_number or 0),
                0 if not str(item.parent_event_id or "").strip() else 1,
                scope_rank.get(str(item.scope or ""), 99),
                str(item.created_at or ""),
                str(item.id or ""),
            )
        )
        by_parent: dict[str, list[DecisionEventInfo]] = defaultdict(list)
        for item in items:
            if item.parent_event_id:
                by_parent[str(item.parent_event_id)].append(item)
        linked_review_refs = [item for item in items if item.related_object_type == "chapter_review"]
        linked_checkpoint_refs = [item for item in items if item.related_object_type == "band_checkpoint"]
        current_outcome = "arc_empty"
        if items:
            current_outcome = items[-1].summary or items[-1].event_type
        elif bands:
            current_outcome = f"arc {target_arc_id} has {len(bands)} band(s), no decision events"
        return CausalReplayResponse(
            root_event=next((item for item in items if not item.parent_event_id), items[0] if items else None),
            timeline=items,
            branches=dict(by_parent),
            current_outcome=current_outcome,
            linked_review_refs=linked_review_refs,
            linked_checkpoint_refs=linked_checkpoint_refs,
        )
    scoped_rows = list_decision_event_rows(
        session,
        project_id=project_id,
        scope=scope,
        band_id=band_id,
        chapter_number=chapter_number,
        task_id=task_id,
        limit=400,
        ascending=False,
    )
    if not scoped_rows:
        return CausalReplayResponse()
    pivot = scoped_rows[0]
    root_id = str(getattr(pivot, "causal_root_id", "") or pivot.id or "")
    timeline_rows = list_decision_event_rows(
        session,
        project_id=project_id,
        causal_root_id=root_id,
        limit=400,
        ascending=True,
    )
    if not timeline_rows:
        timeline_rows = [pivot]
    items = [serialize_decision_event(row) for row in timeline_rows]
    by_parent: dict[str, list[DecisionEventInfo]] = defaultdict(list)
    for item in items:
        parent_id = str(item.parent_event_id or "")
        if parent_id:
            by_parent[parent_id].append(item)
    root_event = next((item for item in items if item.id == root_id), items[0] if items else None)
    linked_review_refs = [item for item in items if item.related_object_type == "chapter_review"]
    linked_checkpoint_refs = [item for item in items if item.related_object_type == "band_checkpoint"]
    current_outcome = items[-1].event_type if items else ""
    if items and items[-1].summary:
        current_outcome = items[-1].summary
    return CausalReplayResponse(
        root_event=root_event,
        timeline=items,
        branches=dict(by_parent),
        current_outcome=current_outcome,
        linked_review_refs=linked_review_refs,
        linked_checkpoint_refs=linked_checkpoint_refs,
    )


def build_governance_insights(session, *, project_id: str) -> GovernanceInsightsResponse:
    event_rows = list_decision_event_rows(
        session,
        project_id=project_id,
        limit=1000,
        ascending=False,
    )
    override_counter: Counter[str] = Counter()
    override_reason_counter: Counter[str] = Counter()
    warn_allowed_counter: Counter[str] = Counter()
    constraint_counter: Counter[str] = Counter()
    blocking_counter: Counter[str] = Counter()
    issue_group_counter: Counter[str] = Counter()
    forced_accept_frequency = 0
    recent_examples: list[dict[str, Any]] = []
    checkpoint_rows = (
        session.execute(
            select(BandCheckpoint)
            .where(BandCheckpoint.project_id == project_id)
            .order_by(BandCheckpoint.created_at.desc(), BandCheckpoint.id.desc())
            .limit(20)
        ).scalars().all()
    )
    checkpoint_status_counter: Counter[str] = Counter(
        str(row.status or "") for row in checkpoint_rows if str(row.status or "").strip()
    )
    checkpoint_map = {row.id: row for row in checkpoint_rows}
    for checkpoint in checkpoint_rows:
        for issue in _json_load_list(checkpoint.issues_json):
            if not isinstance(issue, dict):
                continue
            code = str(issue.get("code") or "").strip()
            group = str(issue.get("issue_group") or issue_group_for_issue(code=code)).strip()
            if group:
                issue_group_counter[group] += 1
    for row in event_rows:
        payload = json.loads(row.payload_json or "{}") if str(row.payload_json or "").strip() else {}
        if not isinstance(payload, dict):
            payload = {}
        if row.event_type == DecisionEventType.FORCED_ACCEPT_APPLIED:
            forced_accept_frequency += 1
            override_counter["forced_accept"] += 1
            reason = str(payload.get("reason") or row.reason or "").strip()
            if reason:
                override_reason_counter[reason] += 1
            recent_examples.append(
                {
                    "event_id": row.id,
                    "event_type": row.event_type,
                    "chapter_number": int(row.chapter_number or 0),
                    "band_id": str(row.band_id or ""),
                    "summary": str(row.summary or ""),
                }
            )
        if row.event_type == DecisionEventType.HARD_GATE_HIT:
            blocking_counter[str(payload.get("blocking_reason") or "hard_gate_hit")] += 1
            recent_examples.append(
                {
                    "event_id": row.id,
                    "event_type": row.event_type,
                    "chapter_number": int(row.chapter_number or 0),
                    "band_id": str(row.band_id or ""),
                    "summary": str(row.summary or ""),
                    "blocking_reason": str(payload.get("blocking_reason") or ""),
                }
            )
        if row.event_type in {DecisionEventType.BAND_CHECKPOINT_HIT, DecisionEventType.BAND_CHECKPOINT_CREATED}:
            status = str(payload.get("status") or "")
            if status in {"warn", "fail", "error"}:
                blocking_counter[f"band_checkpoint_{status}"] += 1
        if row.event_type == DecisionEventType.BAND_CHECKPOINT_OVERRIDDEN:
            override_counter["band_checkpoint_override"] += 1
            reason = str(payload.get("reason") or row.reason or "").strip()
            if reason:
                override_reason_counter[reason] += 1
            checkpoint = checkpoint_map.get(str(row.related_object_id or ""))
            issues = _json_load_list(checkpoint.issues_json) if checkpoint is not None else []
            for issue in issues:
                code = str(issue.get("code") or issue.get("severity") or "checkpoint_issue")
                issue_group = str(issue.get("issue_group") or issue_group_for_issue(code=code)).strip()
                if issue_group:
                    issue_group_counter[issue_group] += 1
                warn_allowed_counter[code] += 1
                if code in {"future_constraint", "future_resource_preservation", "next_band_compatibility"}:
                    constraint_counter[code] += 1
                category = str(issue.get("category") or "").strip()
                if category:
                    warn_allowed_counter[category] += 1
            recent_examples.append(
                {
                    "event_id": row.id,
                    "event_type": row.event_type,
                    "chapter_number": int(row.chapter_number or 0),
                    "band_id": str(row.band_id or ""),
                    "summary": str(row.summary or ""),
                    "related_object_id": str(row.related_object_id or ""),
                }
            )
        issue_types = payload.get("issue_types") or []
        issue_groups = payload.get("issue_groups") or []
        if row.event_type == DecisionEventType.REVIEW_APPROVED:
            reason = str(payload.get("reason") or row.reason or "").strip()
            if reason:
                override_reason_counter[reason] += 1
            for issue_type in issue_types if isinstance(issue_types, list) else []:
                warn_allowed_counter[str(issue_type or "")] += 1
                if "constraint" in str(issue_type or ""):
                    constraint_counter[str(issue_type or "")] += 1
                group = issue_group_for_issue(issue_type=str(issue_type or ""))
                if group:
                    issue_group_counter[group] += 1
            for group in issue_groups if isinstance(issue_groups, list) else []:
                normalized_group = str(group or "").strip()
                if normalized_group:
                    issue_group_counter[normalized_group] += 1
            recent_examples.append(
                {
                    "event_id": row.id,
                    "event_type": row.event_type,
                    "chapter_number": int(row.chapter_number or 0),
                    "band_id": str(row.band_id or ""),
                    "summary": str(row.summary or ""),
                }
            )
    recommended_adjustments: list[dict[str, Any]] = []
    if override_counter.get("band_checkpoint_override", 0) >= 2:
        recommended_adjustments.append(
            {
                "type": "review_band_checkpoint_policy",
                "target": "band_checkpoint",
                "reason": "band checkpoint override 次数偏高，建议复查 warn 阈值和 issue 口径。",
                "count": override_counter["band_checkpoint_override"],
            }
        )
    if warn_allowed_counter.get("future_resource_preservation", 0) or any(
        key in warn_allowed_counter
        for key in {
            "character_locked_out",
            "thread_closed_too_early",
            "relationship_closed_too_early",
            "secret_over_explained",
            "growth_arc_completed_too_early",
        }
    ):
        recommended_adjustments.append(
            {
                "type": "review_future_preservation_warns",
                "target": "future_resource_preservation",
                "reason": "未来资源保留 warn 多次被人工放行，建议复查风险分类和证据阈值。",
                "count": warn_allowed_counter.get("future_resource_preservation", 0),
            }
        )
    if forced_accept_frequency:
        recommended_adjustments.append(
            {
                "type": "review_forced_accept_frequency",
                "target": "chapter_review",
                "reason": "forced accept 已出现，建议复查 reviewer 规则或 repair 链是否过严。",
                "count": forced_accept_frequency,
            }
        )
    if constraint_counter:
        top_constraint = constraint_counter.most_common(1)[0]
        recommended_adjustments.append(
            {
                "type": "review_constraint_quality",
                "target": top_constraint[0],
                "reason": "future constraint 相关问题频繁进入人工放行，建议检查 hard/soft 边界。",
                "count": top_constraint[1],
            }
        )
    if issue_group_counter.get("director_imbalance", 0) >= 2:
        recommended_adjustments.append(
            {
                "type": "review_director_imbalance_rules",
                "target": "director_imbalance",
                "reason": "导演失衡类问题较多，建议复查 task contract、payoff 和 future preservation 口径。",
                "count": issue_group_counter["director_imbalance"],
            }
        )
    if issue_group_counter.get("fact_conflict", 0) >= 2:
        recommended_adjustments.append(
            {
                "type": "review_fact_conflict_rules",
                "target": "fact_conflict",
                "reason": "事实冲突类问题较多，建议复查 hard/soft constraint 与 continuity 判定证据。",
                "count": issue_group_counter["fact_conflict"],
            }
        )
    return GovernanceInsightsResponse(
        top_override_rule_types=counter_rows(override_counter),
        top_override_reasons=counter_rows(override_reason_counter),
        top_warn_but_allowed_issue_types=counter_rows(warn_allowed_counter),
        top_constraint_false_positive_types=counter_rows(constraint_counter),
        forced_accept_frequency=forced_accept_frequency,
        most_common_blocking_reasons=counter_rows(blocking_counter),
        recent_band_checkpoint_distribution=counter_rows(checkpoint_status_counter),
        issue_group_distribution=counter_rows(issue_group_counter),
        recent_action_effectiveness=derive_action_effectiveness(session, project_id=project_id, limit=8),
        recommended_adjustments=recommended_adjustments[:5],
        recent_examples=recent_examples[:8],
    )
