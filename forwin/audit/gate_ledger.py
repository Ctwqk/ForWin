from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from forwin.audience.feedback import derive_action_effectiveness
from forwin.models.audit import DecisionEvent
from forwin.models.phase import BandExperiencePlan
from forwin.models.planning_control import BandCheckpoint
from forwin.review.issue_groups import issue_group_for_issue

from .events import DecisionEventType
from .gate_outcome import GateOutcome, parse_gate_outcome

if TYPE_CHECKING:
    from forwin.api_schema.observability import AuditInsightsResponse

GateLedgerScope = Literal["project", "band", "cross_project"]

GATE_ORDER: tuple[str, ...] = (
    "hard_floor",
    "canon_quality",
    "future_plan_audit",
    "band_checkpoint",
    "manual_checkpoint",
    "generation_audit",
    "delegation",
)

_DEFAULT_RESPONSIBILITY_DOMAINS: dict[str, str] = {
    "hard_floor": "draft_quality",
    "canon_quality": "canon_admission",
    "future_plan_audit": "future_plan_integrity",
    "band_checkpoint": "band_integrity",
    "manual_checkpoint": "operator_control",
    "generation_audit": "generation_operations",
    "delegation": "delegated_gate_resolution",
}

_PRIMARY_DENOMINATOR_EVENTS: dict[str, set[str]] = {
    "hard_floor": {DecisionEventType.PULP_BEAT_EVALUATED},
    "canon_quality": {DecisionEventType.CANON_COMMIT_STARTED},
    "future_plan_audit": {DecisionEventType.FUTURE_PLAN_AUDIT_RUN},
    "band_checkpoint": {DecisionEventType.BAND_CHECKPOINT_CREATED},
    "manual_checkpoint": {DecisionEventType.MANUAL_CHECKPOINT_CREATED},
    "generation_audit": {
        DecisionEventType.GENERATION_AUDIT_CHECKPOINT_REACHED
    },
    "delegation": {DecisionEventType.GATE_DELEGATION_REQUESTED},
}

_LEGACY_EVENT_GATES: dict[str, str] = {
    DecisionEventType.PULP_BEAT_EVALUATED: "hard_floor",
    DecisionEventType.CANON_COMMIT_STARTED: "canon_quality",
    DecisionEventType.CANON_COMMIT_BLOCKED: "canon_quality",
    DecisionEventType.FUTURE_PLAN_AUDIT_RUN: "future_plan_audit",
    DecisionEventType.BAND_CHECKPOINT_CREATED: "band_checkpoint",
    DecisionEventType.BAND_CHECKPOINT_HIT: "band_checkpoint",
    DecisionEventType.CHECKPOINT_EVALUATOR_ERROR: "band_checkpoint",
    DecisionEventType.BAND_CHECKPOINT_APPROVED: "band_checkpoint",
    DecisionEventType.BAND_CHECKPOINT_OVERRIDDEN: "band_checkpoint",
    DecisionEventType.MANUAL_CHECKPOINT_CREATED: "manual_checkpoint",
    DecisionEventType.MANUAL_CHECKPOINT_HIT: "manual_checkpoint",
    DecisionEventType.GENERATION_AUDIT_CHECKPOINT_REACHED: "generation_audit",
    DecisionEventType.GATE_DELEGATION_REQUESTED: "delegation",
    DecisionEventType.GATE_DELEGATION_DECIDED: "delegation",
    DecisionEventType.GATE_DELEGATION_FAILED: "delegation",
    DecisionEventType.GATE_DELEGATION_APPROVED: "delegation",
}

_INCIDENT_EVENT_TYPES: set[str] = {
    DecisionEventType.REPAIR_STARTED,
    DecisionEventType.REPAIR_FAILED,
    DecisionEventType.CANON_COMMIT_BLOCKED,
    DecisionEventType.CANON_COMMIT_FAILED,
    DecisionEventType.HARD_GATE_HIT,
    DecisionEventType.CHECKPOINT_EVALUATOR_ERROR,
}

_INCIDENT_DOMAIN_BY_TYPE: dict[str, str] = {
    DecisionEventType.REPAIR_STARTED: "draft_quality",
    DecisionEventType.REPAIR_FAILED: "draft_quality",
    DecisionEventType.CANON_COMMIT_BLOCKED: "canon_admission",
    DecisionEventType.CANON_COMMIT_FAILED: "canon_admission",
    DecisionEventType.HARD_GATE_HIT: "draft_quality",
    DecisionEventType.CHECKPOINT_EVALUATOR_ERROR: "band_integrity",
}


class GateLedgerMetric(BaseModel):
    model_config = ConfigDict(frozen=True)

    gate_id: str
    gate_versions: list[str] = Field(default_factory=list)
    responsibility_domains: list[str] = Field(default_factory=list)
    opportunities: int | Literal["unknown"] = 0
    evaluations: int = 0
    fires: int = 0
    blocks: int = 0
    pauses: int = 0
    approvals: int = 0
    overrides: int = 0
    post_override_incident_proxy: int = 0
    post_pass_incident_proxy: int = 0
    unknown_legacy_count: int = 0
    fire_rate: float | None = None
    block_rate: float | None = None
    override_rate: float | None = None


class GateLedgerReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    scope: GateLedgerScope
    project_id: str = ""
    band_id: str = ""
    project_count: int = 0
    event_count: int = 0
    checkpoint_count: int = 0
    metrics: list[GateLedgerMetric] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _LedgerRecord:
    row: DecisionEvent
    payload: dict[str, Any]
    outcome: GateOutcome | None
    gate_id: str
    candidate_id: str
    chapter_number: int
    band_id: str
    responsibility_domain: str
    issue_groups: frozenset[str]


@dataclass(frozen=True, slots=True)
class _History:
    events: list[DecisionEvent]
    checkpoints: list[BandCheckpoint]
    band_ranges: dict[tuple[str, str], tuple[int, int]]


def _json_object(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _json_list(raw: str | None) -> list[Any]:
    try:
        value = json.loads(str(raw or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def _counter_rows(
    counter: Counter[str], *, limit: int = 5
) -> list[dict[str, Any]]:
    return [
        {"name": name, "count": count}
        for name, count in counter.most_common(max(1, limit))
        if str(name or "").strip()
    ]


def _normalized_strings(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set, frozenset)):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _payload_issue_groups(payload: dict[str, Any]) -> frozenset[str]:
    groups = set(_normalized_strings(payload.get("issue_groups")))
    issue_keys = [
        *_normalized_strings(payload.get("issue_keys")),
        *_normalized_strings(payload.get("issue_types")),
        *_normalized_strings(payload.get("blocking_reasons")),
    ]
    for issue_key in issue_keys:
        group = issue_group_for_issue(code=issue_key, issue_type=issue_key)
        if group:
            groups.add(group)
    return frozenset(groups)


def _legacy_gate_id(
    row: DecisionEvent,
    *,
    payload: dict[str, Any],
    checkpoint_map: dict[str, BandCheckpoint],
) -> str:
    event_type = str(row.event_type or "")
    if event_type == DecisionEventType.HARD_GATE_HIT:
        if (
            "hard floor" in str(row.summary or "").lower()
            or "fail_reasons" in payload
            or "checks" in payload
        ) and not payload.get("blocking_reason"):
            return "hard_floor"
        return ""
    if event_type == DecisionEventType.REVIEW_VERDICT_RECORDED:
        if str(row.related_object_type or "") == "canon_admission_run":
            return "canon_quality"
        return ""
    gate_id = _LEGACY_EVENT_GATES.get(event_type, "")
    if gate_id != "band_checkpoint":
        return gate_id
    checkpoint = checkpoint_map.get(str(row.related_object_id or ""))
    if checkpoint is not None and str(checkpoint.trigger_source or "") == "manual_boundary":
        return "manual_checkpoint"
    return gate_id


def _candidate_id(
    row: DecisionEvent,
    payload: dict[str, Any],
    outcome: GateOutcome | None,
) -> str:
    if outcome is not None and outcome.candidate_id:
        return str(outcome.candidate_id)
    payload_candidate = str(
        payload.get("candidate_id")
        or payload.get("gate_related_object_id")
        or (
            payload.get("related_object_id")
            if str(row.event_type or "").startswith("gate_delegation_")
            else ""
        )
        or ""
    ).strip()
    if payload_candidate:
        return payload_candidate
    if str(row.related_object_type or "") in {
        "band_checkpoint",
        "candidate_draft",
        "future_plan_audit_run",
        "generation_audit_checkpoint",
    }:
        return str(row.related_object_id or "").strip()
    return ""


def _identity(
    *,
    row: DecisionEvent,
    gate_id: str,
    candidate_id: str,
    chapter_number: int,
    band_id: str,
    checkpoint_map: dict[str, BandCheckpoint],
    delegation_request_map: dict[str, str],
) -> tuple[str, str, str]:
    checkpoint = checkpoint_map.get(
        candidate_id or str(row.related_object_id or "")
    )
    if (
        gate_id == "band_checkpoint"
        and checkpoint is not None
        and str(checkpoint.trigger_source or "") != "manual_boundary"
    ):
        value = f"band:{checkpoint.arc_id}:{checkpoint.band_id}"
    elif gate_id == "delegation" and delegation_request_map.get(str(row.id or "")):
        value = f"request:{delegation_request_map[str(row.id or '')]}"
    elif candidate_id:
        value = f"candidate:{candidate_id}"
    elif str(row.event_type or "") in _PRIMARY_DENOMINATOR_EVENTS.get(
        gate_id, set()
    ):
        value = f"event:{row.id}"
    elif str(row.related_object_id or ""):
        value = f"related:{row.related_object_id}"
    elif chapter_number > 0 or band_id:
        value = f"chapter:{chapter_number}:band:{band_id}"
    else:
        value = f"event:{row.id}"
    return (str(row.project_id or ""), gate_id, value)


def _checkpoint_identity(
    checkpoint: BandCheckpoint, gate_id: str
) -> tuple[str, str, str]:
    if gate_id == "band_checkpoint":
        value = f"band:{checkpoint.arc_id}:{checkpoint.band_id}"
    else:
        value = f"candidate:{checkpoint.id}"
    return (str(checkpoint.project_id or ""), gate_id, value)


def _rate(numerator: int, opportunities: int | Literal["unknown"]) -> float | None:
    if opportunities == "unknown":
        return None
    if opportunities <= 0:
        return 0.0
    return round(numerator / opportunities, 4)


class GateLedgerService:
    """Derive gate effectiveness from complete DecisionEvent history."""

    def __init__(self, session) -> None:
        self.session = session

    def report(
        self,
        *,
        scope: GateLedgerScope = "cross_project",
        project_id: str = "",
        band_id: str = "",
    ) -> GateLedgerReport:
        normalized_scope = str(scope or "cross_project").strip()
        normalized_project_id = str(project_id or "").strip()
        normalized_band_id = str(band_id or "").strip()
        if normalized_scope not in {"project", "band", "cross_project"}:
            raise ValueError(f"unsupported gate ledger scope: {normalized_scope}")
        if normalized_scope in {"project", "band"} and not normalized_project_id:
            raise ValueError(f"project_id is required for {normalized_scope} scope")
        if normalized_scope == "band" and not normalized_band_id:
            raise ValueError("band_id is required for band scope")

        history = self._load_history(
            scope=normalized_scope,  # type: ignore[arg-type]
            project_id=normalized_project_id,
            band_id=normalized_band_id,
        )
        checkpoint_map = {str(row.id): row for row in history.checkpoints}
        records = self._records(history.events, checkpoint_map=checkpoint_map)
        delegation_request_map = self._delegation_request_map(records)
        states: dict[
            str, dict[tuple[str, str, str], list[_LedgerRecord]]
        ] = defaultdict(lambda: defaultdict(list))
        opportunity_keys: dict[str, set[tuple[str, str, str]]] = defaultdict(set)
        unresolved_denominators: dict[str, set[tuple[str, str, str]]] = defaultdict(set)
        unknown_legacy_ids: dict[str, set[str]] = defaultdict(set)
        responsibility_domains: dict[str, set[str]] = defaultdict(set)
        gate_versions: dict[str, set[str]] = defaultdict(set)

        for checkpoint in history.checkpoints:
            gate_id = (
                "manual_checkpoint"
                if str(checkpoint.trigger_source or "") == "manual_boundary"
                else "band_checkpoint"
            )
            opportunity_keys[gate_id].add(_checkpoint_identity(checkpoint, gate_id))

        for record in records:
            gate_id = record.gate_id
            if not gate_id:
                continue
            identity = _identity(
                row=record.row,
                gate_id=gate_id,
                candidate_id=record.candidate_id,
                chapter_number=record.chapter_number,
                band_id=record.band_id,
                checkpoint_map=checkpoint_map,
                delegation_request_map=delegation_request_map,
            )
            outcome = record.outcome
            if outcome is None:
                unknown_legacy_ids[gate_id].add(str(record.row.id or ""))
                if str(record.row.event_type or "") in _PRIMARY_DENOMINATOR_EVENTS.get(
                    gate_id, set()
                ):
                    opportunity_keys[gate_id].add(identity)
                else:
                    unresolved_denominators[gate_id].add(identity)
                continue
            responsibility_domains[gate_id].add(outcome.responsibility_domain)
            gate_versions[gate_id].add(outcome.gate_version)
            states[gate_id][identity].append(record)
            if self._is_denominator_event(record):
                opportunity_keys[gate_id].add(identity)

        for gate_id, gate_states in states.items():
            for identity, state_records in gate_states.items():
                if not any(item.outcome and item.outcome.evaluated for item in state_records):
                    continue
                if identity not in opportunity_keys[gate_id]:
                    unresolved_denominators[gate_id].add(identity)
        for gate_id, identities in unresolved_denominators.items():
            identities.difference_update(opportunity_keys.get(gate_id, set()))

        proxy_counts = self._proxy_counts(states=states, records=records)
        discovered_gates = sorted(
            (
                set(states)
                | set(opportunity_keys)
                | set(unknown_legacy_ids)
                | set(unresolved_denominators)
            )
            - set(GATE_ORDER)
        )
        metrics: list[GateLedgerMetric] = []
        for gate_id in (*GATE_ORDER, *discovered_gates):
            gate_states = states.get(gate_id, {})
            evaluated_states = {
                identity: rows
                for identity, rows in gate_states.items()
                if any(item.outcome and item.outcome.evaluated for item in rows)
            }
            evaluations = len(evaluated_states)
            fires = sum(
                any(
                    item.outcome
                    and item.outcome.evaluated
                    and item.outcome.fired
                    for item in rows
                )
                for rows in evaluated_states.values()
            )
            blocks = sum(
                any(
                    item.outcome
                    and item.outcome.evaluated
                    and (item.outcome.blocked or item.outcome.decision == "block")
                    for item in rows
                )
                for rows in evaluated_states.values()
            )
            pauses = sum(
                any(
                    item.outcome
                    and item.outcome.evaluated
                    and item.outcome.decision == "pause"
                    for item in rows
                )
                for rows in evaluated_states.values()
            )
            approvals = sum(
                any(
                    item.outcome
                    and item.outcome.evaluated
                    and item.outcome.decision == "approve"
                    for item in rows
                )
                for rows in evaluated_states.values()
            )
            overrides = sum(
                any(
                    item.outcome
                    and item.outcome.evaluated
                    and bool(item.outcome.overridden_by)
                    for item in rows
                )
                for rows in evaluated_states.values()
            )
            opportunities: int | Literal["unknown"]
            if unresolved_denominators.get(gate_id):
                opportunities = "unknown"
            else:
                opportunities = len(opportunity_keys.get(gate_id, set()))
            domains = sorted(responsibility_domains.get(gate_id, set()))
            if not domains:
                domains = [_DEFAULT_RESPONSIBILITY_DOMAINS.get(gate_id, "unknown")]
            versions = sorted(gate_versions.get(gate_id, set())) or ["v1"]
            unknown_legacy_count = len(unknown_legacy_ids.get(gate_id, set()))
            rates_known = unknown_legacy_count == 0
            metrics.append(
                GateLedgerMetric(
                    gate_id=gate_id,
                    gate_versions=versions,
                    responsibility_domains=domains,
                    opportunities=opportunities,
                    evaluations=evaluations,
                    fires=fires,
                    blocks=blocks,
                    pauses=pauses,
                    approvals=approvals,
                    overrides=overrides,
                    post_override_incident_proxy=proxy_counts[gate_id][0],
                    post_pass_incident_proxy=proxy_counts[gate_id][1],
                    unknown_legacy_count=unknown_legacy_count,
                    fire_rate=_rate(fires, opportunities) if rates_known else None,
                    block_rate=_rate(blocks, opportunities) if rates_known else None,
                    override_rate=(
                        _rate(overrides, opportunities) if rates_known else None
                    ),
                )
            )

        project_ids = {
            str(row.project_id or "")
            for row in [*history.events, *history.checkpoints]
            if str(row.project_id or "")
        }
        return GateLedgerReport(
            scope=normalized_scope,  # type: ignore[arg-type]
            project_id=normalized_project_id,
            band_id=normalized_band_id,
            project_count=(
                len(project_ids) if normalized_scope == "cross_project" else 1
            ),
            event_count=len(history.events),
            checkpoint_count=len(history.checkpoints),
            metrics=metrics,
        )

    def audit_insights(self, *, project_id: str) -> AuditInsightsResponse:
        """Preserve the old UI response while reading complete ledger history."""

        from forwin.api_schema.observability import AuditInsightsResponse

        history = self._load_history(
            scope="project",
            project_id=str(project_id or "").strip(),
            band_id="",
        )
        event_rows = list(reversed(history.events))
        checkpoint_rows = list(reversed(history.checkpoints))[:20]
        override_counter: Counter[str] = Counter()
        override_reason_counter: Counter[str] = Counter()
        warn_allowed_counter: Counter[str] = Counter()
        constraint_counter: Counter[str] = Counter()
        blocking_counter: Counter[str] = Counter()
        issue_group_counter: Counter[str] = Counter()
        forced_accept_frequency = 0
        recent_examples: list[dict[str, Any]] = []
        checkpoint_status_counter: Counter[str] = Counter(
            str(row.status or "")
            for row in checkpoint_rows
            if str(row.status or "").strip()
        )
        checkpoint_map = {str(row.id): row for row in history.checkpoints}
        for checkpoint in history.checkpoints:
            for issue in _json_list(checkpoint.issues_json):
                if not isinstance(issue, dict):
                    continue
                code = str(issue.get("code") or "").strip()
                group = str(
                    issue.get("issue_group") or issue_group_for_issue(code=code)
                ).strip()
                if group:
                    issue_group_counter[group] += 1
        for row in event_rows:
            payload = _json_object(row.payload_json)
            if row.event_type == DecisionEventType.FORCED_ACCEPT_APPLIED:
                forced_accept_frequency += 1
                override_counter["forced_accept"] += 1
                reason = str(payload.get("reason") or row.reason or "").strip()
                if reason:
                    override_reason_counter[reason] += 1
                recent_examples.append(self._example(row))
            if row.event_type == DecisionEventType.HARD_GATE_HIT:
                blocking_counter[
                    str(payload.get("blocking_reason") or "hard_gate_hit")
                ] += 1
                recent_examples.append(
                    {
                        **self._example(row),
                        "blocking_reason": str(payload.get("blocking_reason") or ""),
                    }
                )
            if row.event_type in {
                DecisionEventType.BAND_CHECKPOINT_HIT,
                DecisionEventType.BAND_CHECKPOINT_CREATED,
            }:
                status = str(payload.get("status") or "")
                if status in {"warn", "fail", "error"}:
                    blocking_counter[f"band_checkpoint_{status}"] += 1
            if row.event_type == DecisionEventType.BAND_CHECKPOINT_OVERRIDDEN:
                override_counter["band_checkpoint_override"] += 1
                reason = str(payload.get("reason") or row.reason or "").strip()
                if reason:
                    override_reason_counter[reason] += 1
                checkpoint = checkpoint_map.get(str(row.related_object_id or ""))
                issues = _json_list(checkpoint.issues_json) if checkpoint else []
                for issue in issues:
                    if not isinstance(issue, dict):
                        continue
                    code = str(
                        issue.get("code")
                        or issue.get("severity")
                        or "checkpoint_issue"
                    )
                    issue_group = str(
                        issue.get("issue_group") or issue_group_for_issue(code=code)
                    ).strip()
                    if issue_group:
                        issue_group_counter[issue_group] += 1
                    warn_allowed_counter[code] += 1
                    if code in {
                        "future_constraint",
                        "future_resource_preservation",
                        "next_band_compatibility",
                    }:
                        constraint_counter[code] += 1
                    category = str(issue.get("category") or "").strip()
                    if category:
                        warn_allowed_counter[category] += 1
                recent_examples.append(
                    {
                        **self._example(row),
                        "related_object_id": str(row.related_object_id or ""),
                    }
                )
            issue_types = payload.get("issue_types") or []
            issue_groups = payload.get("issue_groups") or []
            if row.event_type == DecisionEventType.REVIEW_APPROVED:
                reason = str(payload.get("reason") or row.reason or "").strip()
                if reason:
                    override_reason_counter[reason] += 1
                for issue_type in (
                    issue_types if isinstance(issue_types, list) else []
                ):
                    normalized_issue = str(issue_type or "")
                    warn_allowed_counter[normalized_issue] += 1
                    if "constraint" in normalized_issue:
                        constraint_counter[normalized_issue] += 1
                    group = issue_group_for_issue(issue_type=normalized_issue)
                    if group:
                        issue_group_counter[group] += 1
                for group in issue_groups if isinstance(issue_groups, list) else []:
                    normalized_group = str(group or "").strip()
                    if normalized_group:
                        issue_group_counter[normalized_group] += 1
                recent_examples.append(self._example(row))

        recommendations = self._recommendations(
            override_counter=override_counter,
            warn_allowed_counter=warn_allowed_counter,
            constraint_counter=constraint_counter,
            issue_group_counter=issue_group_counter,
            forced_accept_frequency=forced_accept_frequency,
        )
        return AuditInsightsResponse(
            top_override_rule_types=_counter_rows(override_counter),
            top_override_reasons=_counter_rows(override_reason_counter),
            top_warn_but_allowed_issue_types=_counter_rows(warn_allowed_counter),
            top_constraint_false_positive_types=_counter_rows(constraint_counter),
            forced_accept_frequency=forced_accept_frequency,
            most_common_blocking_reasons=_counter_rows(blocking_counter),
            recent_band_checkpoint_distribution=_counter_rows(
                checkpoint_status_counter
            ),
            issue_group_distribution=_counter_rows(issue_group_counter),
            recent_action_effectiveness=derive_action_effectiveness(
                self.session, project_id=project_id, limit=8
            ),
            recommended_adjustments=recommendations[:5],
            recent_examples=recent_examples[:8],
        )

    def _load_history(
        self,
        *,
        scope: GateLedgerScope,
        project_id: str,
        band_id: str,
    ) -> _History:
        event_stmt = select(DecisionEvent)
        checkpoint_stmt = select(BandCheckpoint)
        band_stmt = select(BandExperiencePlan)
        if scope != "cross_project":
            event_stmt = event_stmt.where(DecisionEvent.project_id == project_id)
            checkpoint_stmt = checkpoint_stmt.where(
                BandCheckpoint.project_id == project_id
            )
            band_stmt = band_stmt.where(BandExperiencePlan.project_id == project_id)
        band_rows = self.session.execute(band_stmt).scalars().all()
        band_ranges: dict[tuple[str, str], tuple[int, int]] = {}
        for row in band_rows:
            key = (str(row.project_id or ""), str(row.band_id or ""))
            start = int(row.chapter_start or 0)
            end = int(row.chapter_end or 0)
            if key in band_ranges:
                previous = band_ranges[key]
                start = min(
                    (value for value in (previous[0], start) if value > 0),
                    default=0,
                )
                end = max(previous[1], end)
            band_ranges[key] = (start, end)
        events = (
            self.session.execute(
                event_stmt.order_by(DecisionEvent.created_at.asc(), DecisionEvent.id.asc())
            )
            .scalars()
            .all()
        )
        checkpoints = (
            self.session.execute(
                checkpoint_stmt.order_by(
                    BandCheckpoint.created_at.asc(), BandCheckpoint.id.asc()
                )
            )
            .scalars()
            .all()
        )
        if scope == "band":
            events = [
                row
                for row in events
                if self._row_matches_band(
                    project_id=str(row.project_id or ""),
                    explicit_band_id=str(row.band_id or ""),
                    chapter_number=int(row.chapter_number or 0),
                    target_band_id=band_id,
                    band_ranges=band_ranges,
                )
            ]
            checkpoints = [
                row for row in checkpoints if str(row.band_id or "") == band_id
            ]
        return _History(
            events=list(events),
            checkpoints=list(checkpoints),
            band_ranges=band_ranges,
        )

    @staticmethod
    def _row_matches_band(
        *,
        project_id: str,
        explicit_band_id: str,
        chapter_number: int,
        target_band_id: str,
        band_ranges: dict[tuple[str, str], tuple[int, int]],
    ) -> bool:
        if explicit_band_id:
            return explicit_band_id == target_band_id
        start, end = band_ranges.get((project_id, target_band_id), (0, 0))
        return bool(start > 0 and start <= chapter_number <= end)

    def _records(
        self,
        events: list[DecisionEvent],
        *,
        checkpoint_map: dict[str, BandCheckpoint],
    ) -> list[_LedgerRecord]:
        records: list[_LedgerRecord] = []
        for row in events:
            payload = _json_object(row.payload_json)
            outcome = parse_gate_outcome(payload)
            gate_id = (
                str(outcome.gate_id or "")
                if outcome is not None
                else _legacy_gate_id(
                    row,
                    payload=payload,
                    checkpoint_map=checkpoint_map,
                )
            )
            candidate_id = _candidate_id(row, payload, outcome)
            chapter_number = int(
                (outcome.chapter_number if outcome is not None else 0)
                or row.chapter_number
                or 0
            )
            band_id = str(
                (outcome.band_id if outcome is not None else "")
                or row.band_id
                or ""
            )
            domain = str(
                (outcome.responsibility_domain if outcome is not None else "")
                or payload.get("responsibility_domain")
                or _INCIDENT_DOMAIN_BY_TYPE.get(str(row.event_type or ""), "")
            ).strip()
            issue_groups = (
                frozenset(outcome.issue_groups)
                if outcome is not None
                else _payload_issue_groups(payload)
            )
            records.append(
                _LedgerRecord(
                    row=row,
                    payload=payload,
                    outcome=outcome,
                    gate_id=gate_id,
                    candidate_id=candidate_id,
                    chapter_number=chapter_number,
                    band_id=band_id,
                    responsibility_domain=domain,
                    issue_groups=issue_groups,
                )
            )
        return records

    @staticmethod
    def _delegation_request_map(records: list[_LedgerRecord]) -> dict[str, str]:
        by_id = {str(record.row.id or ""): record for record in records}
        request_ids = {
            event_id
            for event_id, record in by_id.items()
            if str(record.row.event_type or "")
            == DecisionEventType.GATE_DELEGATION_REQUESTED
        }
        resolved: dict[str, str] = {}
        for event_id, record in by_id.items():
            current = record
            visited: set[str] = set()
            while True:
                current_id = str(current.row.id or "")
                if current_id in request_ids:
                    resolved[event_id] = current_id
                    break
                if current_id in visited:
                    break
                visited.add(current_id)
                parent_id = str(current.row.parent_event_id or "")
                parent = by_id.get(parent_id)
                if parent is None:
                    causal_root_id = str(current.row.causal_root_id or "")
                    if causal_root_id in request_ids:
                        resolved[event_id] = causal_root_id
                    break
                current = parent
        return resolved

    @staticmethod
    def _is_denominator_event(record: _LedgerRecord) -> bool:
        outcome = record.outcome
        if outcome is None:
            return False
        if outcome.gate_id == "hard_floor" and outcome.evaluated:
            return True
        return str(record.row.event_type or "") in _PRIMARY_DENOMINATOR_EVENTS.get(
            outcome.gate_id, set()
        )

    def _proxy_counts(
        self,
        *,
        states: dict[str, dict[tuple[str, str, str], list[_LedgerRecord]]],
        records: list[_LedgerRecord],
    ) -> dict[str, tuple[int, int]]:
        counts: dict[str, tuple[int, int]] = {}
        for gate_id, gate_states in states.items():
            counts[gate_id] = (
                self._proxy_match_count(
                    gate_states=gate_states,
                    records=records,
                    origin_kind="override",
                ),
                self._proxy_match_count(
                    gate_states=gate_states,
                    records=records,
                    origin_kind="pass",
                ),
            )
        return defaultdict(lambda: (0, 0), counts)

    def _proxy_match_count(
        self,
        *,
        gate_states: dict[tuple[str, str, str], list[_LedgerRecord]],
        records: list[_LedgerRecord],
        origin_kind: Literal["override", "pass"],
    ) -> int:
        origins: dict[tuple[str, str, str], list[_LedgerRecord]] = {}
        record_state: dict[str, tuple[str, str, str]] = {}
        for identity, state_records in gate_states.items():
            for record in state_records:
                record_state[str(record.row.id or "")] = identity
            eligible = [
                record
                for record in state_records
                if self._is_proxy_origin(record, origin_kind=origin_kind)
            ]
            if eligible:
                origins[identity] = eligible
        by_id = {str(record.row.id or ""): record for record in records}
        matched: set[tuple[str, str, str]] = set()
        for incident in records:
            if not self._is_incident(incident):
                continue
            candidates = {
                identity
                for identity, origin_records in origins.items()
                if any(
                    self._happens_after(
                        origin=origin,
                        incident=incident,
                        by_id=by_id,
                    )
                    and self._identity_matches(origin, incident)
                    and self._responsibility_matches(origin, incident)
                    for origin in origin_records
                )
            }
            same_state = record_state.get(str(incident.row.id or ""))
            if same_state in candidates:
                matched.add(same_state)
            elif len(candidates) == 1:
                matched.update(candidates)
        return len(matched)

    @staticmethod
    def _is_proxy_origin(
        record: _LedgerRecord,
        *,
        origin_kind: Literal["override", "pass"],
    ) -> bool:
        outcome = record.outcome
        if outcome is None or not outcome.evaluated:
            return False
        if origin_kind == "override":
            return bool(outcome.overridden_by)
        return bool(
            not outcome.blocked
            and not outcome.overridden_by
            and outcome.decision in {"pass", "warn", "approve"}
        )

    @staticmethod
    def _happens_after(
        *,
        origin: _LedgerRecord,
        incident: _LedgerRecord,
        by_id: dict[str, _LedgerRecord],
    ) -> bool:
        current = incident
        visited: set[str] = set()
        while True:
            parent_id = str(current.row.parent_event_id or "")
            if not parent_id or parent_id in visited:
                break
            if parent_id == str(origin.row.id or ""):
                return True
            visited.add(parent_id)
            parent = by_id.get(parent_id)
            if parent is None:
                break
            current = parent
        origin_time = origin.row.created_at
        incident_time = incident.row.created_at
        return bool(
            origin_time is not None
            and incident_time is not None
            and incident_time > origin_time
        )

    @staticmethod
    def _is_incident(record: _LedgerRecord) -> bool:
        if record.outcome is not None and record.outcome.evaluated:
            return bool(
                record.outcome.blocked
                or record.outcome.decision in {"block", "reject", "error"}
            )
        return str(record.row.event_type or "") in _INCIDENT_EVENT_TYPES

    @staticmethod
    def _identity_matches(origin: _LedgerRecord, incident: _LedgerRecord) -> bool:
        if str(origin.row.project_id or "") != str(incident.row.project_id or ""):
            return False
        if origin.candidate_id and incident.candidate_id:
            return origin.candidate_id == incident.candidate_id
        if origin.band_id and incident.band_id:
            return origin.band_id == incident.band_id
        return bool(
            origin.chapter_number > 0
            and incident.chapter_number > 0
            and origin.chapter_number == incident.chapter_number
        )

    @staticmethod
    def _responsibility_matches(
        origin: _LedgerRecord, incident: _LedgerRecord
    ) -> bool:
        if (
            origin.responsibility_domain
            and incident.responsibility_domain
            and origin.responsibility_domain == incident.responsibility_domain
        ):
            return True
        return bool(origin.issue_groups & incident.issue_groups)

    @staticmethod
    def _example(row: DecisionEvent) -> dict[str, Any]:
        return {
            "event_id": str(row.id or ""),
            "event_type": str(row.event_type or ""),
            "chapter_number": int(row.chapter_number or 0),
            "band_id": str(row.band_id or ""),
            "summary": str(row.summary or ""),
        }

    @staticmethod
    def _recommendations(
        *,
        override_counter: Counter[str],
        warn_allowed_counter: Counter[str],
        constraint_counter: Counter[str],
        issue_group_counter: Counter[str],
        forced_accept_frequency: int,
    ) -> list[dict[str, Any]]:
        recommendations: list[dict[str, Any]] = []
        if override_counter.get("band_checkpoint_override", 0) >= 2:
            recommendations.append(
                {
                    "type": "review_band_checkpoint_policy",
                    "target": "band_checkpoint",
                    "reason": "band checkpoint override 次数偏高，建议复查 warn 阈值和 issue 口径。",
                    "count": override_counter["band_checkpoint_override"],
                }
            )
        future_resource_keys = {
            "character_locked_out",
            "thread_closed_too_early",
            "relationship_closed_too_early",
            "secret_over_explained",
            "growth_arc_completed_too_early",
        }
        if warn_allowed_counter.get("future_resource_preservation", 0) or any(
            key in warn_allowed_counter for key in future_resource_keys
        ):
            recommendations.append(
                {
                    "type": "review_future_preservation_warns",
                    "target": "future_resource_preservation",
                    "reason": "未来资源保留 warn 多次被人工放行，建议复查风险分类和证据阈值。",
                    "count": warn_allowed_counter.get(
                        "future_resource_preservation", 0
                    ),
                }
            )
        if forced_accept_frequency:
            recommendations.append(
                {
                    "type": "review_forced_accept_frequency",
                    "target": "chapter_review",
                    "reason": "forced accept 已出现，建议复查 reviewer 规则或 repair 链是否过严。",
                    "count": forced_accept_frequency,
                }
            )
        if constraint_counter:
            target, count = constraint_counter.most_common(1)[0]
            recommendations.append(
                {
                    "type": "review_constraint_quality",
                    "target": target,
                    "reason": "future constraint 相关问题频繁进入人工放行，建议检查 hard/soft 边界。",
                    "count": count,
                }
            )
        for group, recommendation_type, reason in (
            (
                "director_imbalance",
                "review_director_imbalance_rules",
                "导演失衡类问题较多，建议复查 task contract、payoff 和 future preservation 口径。",
            ),
            (
                "fact_conflict",
                "review_fact_conflict_rules",
                "事实冲突类问题较多，建议复查 hard/soft constraint 与 continuity 判定证据。",
            ),
        ):
            if issue_group_counter.get(group, 0) >= 2:
                recommendations.append(
                    {
                        "type": recommendation_type,
                        "target": group,
                        "reason": reason,
                        "count": issue_group_counter[group],
                    }
                )
        return recommendations


__all__ = [
    "GATE_ORDER",
    "GateLedgerMetric",
    "GateLedgerReport",
    "GateLedgerScope",
    "GateLedgerService",
]
