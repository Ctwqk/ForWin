from __future__ import annotations

import json
from typing import Literal, Protocol

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from forwin.canon_quality.signals import CanonQualitySignal
from forwin.models.canon_quality import CanonQualitySignalRow

ActiveRuleStatus = Literal["observing", "active", "suspended", "retired"]
_ALLOWED_STATUS_TRANSITIONS: dict[ActiveRuleStatus, frozenset[ActiveRuleStatus]] = {
    "observing": frozenset({"active"}),
    "active": frozenset({"suspended"}),
    "suspended": frozenset({"active", "retired"}),
    "retired": frozenset(),
}


class TriggerQuote(BaseModel):
    chapter_number: int
    quote: str
    source_ref: str = ""


class ActiveRule(BaseModel):
    rule_key: str
    summary: str = ""
    valid_from_chapter: int = 0
    valid_until_chapter: int | None = None
    payload: dict = Field(default_factory=dict)
    origin_event_id: str = ""
    origin_project_id: str = ""
    status: ActiveRuleStatus = "observing"
    promotion_evidence: list[str] = Field(default_factory=list)


class ActiveRulePatch(BaseModel):
    rule: ActiveRule
    trigger_quote: TriggerQuote


class RegistrationResult(BaseModel):
    applied: bool = False
    rule_key: str = ""
    reason: str = ""


class StatusTransitionResult(BaseModel):
    applied: bool = False
    rule_key: str = ""
    from_status: ActiveRuleStatus | None = None
    to_status: ActiveRuleStatus | None = None
    reason: str = ""


class ActiveRuleStore(Protocol):
    def register_rule(
        self,
        *,
        project_id: str,
        rule: ActiveRule,
        trigger_quote: TriggerQuote,
    ) -> RegistrationResult: ...

    def query_active_as_of(
        self,
        *,
        project_id: str,
        chapter_number: int,
    ) -> list[ActiveRule]: ...

    def query_rules_as_of(
        self,
        *,
        project_id: str,
        chapter_number: int,
    ) -> list[ActiveRule]: ...

    def list_rules(self, *, project_id: str) -> list[ActiveRule]: ...

    def transition_status(
        self,
        *,
        project_id: str,
        rule_key: str,
        chapter_number: int,
        status: ActiveRuleStatus,
        reason: str,
        evidence_refs: list[str] | None = None,
    ) -> StatusTransitionResult: ...


class CanonQualityActiveRuleStore:
    """Project-scoped runtime rules persisted in the canon-quality signal ledger."""

    REGISTERED = "active_rule_registered"
    STATUS_CHANGED = "active_rule_status_changed"

    def __init__(self, session: Session) -> None:
        self.session = session

    def register_rule(
        self,
        *,
        project_id: str,
        rule: ActiveRule,
        trigger_quote: TriggerQuote,
    ) -> RegistrationResult:
        key = str(rule.rule_key or "").strip()
        if not key:
            return RegistrationResult(applied=False, reason="missing_rule_key")
        origin_project_id = str(rule.origin_project_id or "").strip()
        if origin_project_id and origin_project_id != project_id:
            return RegistrationResult(
                applied=False,
                rule_key=key,
                reason="origin_project_mismatch",
            )
        normalized = rule.model_copy(
            update={
                "rule_key": key,
                "origin_project_id": project_id,
                "origin_event_id": str(
                    rule.origin_event_id or trigger_quote.source_ref or ""
                ).strip(),
                "promotion_evidence": _dedupe(rule.promotion_evidence),
            }
        )
        event_chapter = int(
            normalized.valid_from_chapter or trigger_quote.chapter_number or 0
        )
        latest_event_chapter = self._latest_event_chapter(
            project_id=project_id,
            rule_key=key,
        )
        if (
            latest_event_chapter is not None
            and event_chapter < latest_event_chapter
        ):
            return RegistrationResult(
                applied=False,
                rule_key=key,
                reason="out_of_order_rule_event",
            )
        if self._has_overlapping_rule(
            project_id=project_id,
            rule=normalized,
            trigger_quote=trigger_quote,
        ):
            return RegistrationResult(
                applied=False,
                rule_key=key,
                reason="active_rule_conflict",
            )
        status_sequence = self._next_status_sequence(
            project_id=project_id,
            rule_key=key,
        )
        signal = CanonQualitySignal(
            signal_id=(
                f"active_rule:{project_id}:{key}:"
                f"{event_chapter}:{status_sequence}"
            ),
            project_id=project_id,
            chapter_number=event_chapter,
            signal_type=self.REGISTERED,
            severity="info",
            target_scope="book",
            subject_key=key,
            description=normalized.summary or key,
            evidence_refs=[trigger_quote.source_ref]
            if trigger_quote.source_ref
            else [],
            payload={
                "active_rule": normalized.model_dump(mode="json"),
                "trigger_quote": trigger_quote.model_dump(mode="json"),
                "status_sequence": status_sequence,
                "source": "ActiveRuleStore",
            },
            status="resolved",
        )
        self.session.add(_signal_row(signal))
        self.session.flush()
        return RegistrationResult(applied=True, rule_key=key)

    def query_active_as_of(
        self,
        *,
        project_id: str,
        chapter_number: int,
    ) -> list[ActiveRule]:
        return [
            rule
            for rule in self.query_rules_as_of(
                project_id=project_id,
                chapter_number=chapter_number,
            )
            if rule.status == "active"
        ]

    def query_rules_as_of(
        self,
        *,
        project_id: str,
        chapter_number: int,
    ) -> list[ActiveRule]:
        as_of = int(chapter_number or 0)
        rows = list(
            self.session.scalars(
                select(CanonQualitySignalRow)
                .where(
                    CanonQualitySignalRow.project_id == project_id,
                    CanonQualitySignalRow.signal_type.in_(
                        (self.REGISTERED, self.STATUS_CHANGED)
                    ),
                    CanonQualitySignalRow.chapter_number <= as_of,
                )
                .order_by(
                    CanonQualitySignalRow.chapter_number.asc(),
                    CanonQualitySignalRow.created_at.asc(),
                    CanonQualitySignalRow.id.asc(),
                )
            ).all()
        )
        rows.sort(key=_rule_row_order)
        states = _materialize_rules(rows)
        return sorted(
            (
                rule
                for rule in states.values()
                if int(rule.valid_from_chapter or 0) <= as_of
                and (
                    rule.valid_until_chapter is None
                    or int(rule.valid_until_chapter) >= as_of
                )
            ),
            key=lambda item: item.rule_key,
        )

    def list_rules(self, *, project_id: str) -> list[ActiveRule]:
        rows = list(
            self.session.scalars(
                select(CanonQualitySignalRow)
                .where(
                    CanonQualitySignalRow.project_id == project_id,
                    CanonQualitySignalRow.signal_type.in_(
                        (self.REGISTERED, self.STATUS_CHANGED)
                    ),
                )
                .order_by(
                    CanonQualitySignalRow.chapter_number.asc(),
                    CanonQualitySignalRow.created_at.asc(),
                    CanonQualitySignalRow.id.asc(),
                )
            ).all()
        )
        rows.sort(key=_rule_row_order)
        return sorted(_materialize_rules(rows).values(), key=lambda item: item.rule_key)

    def transition_status(
        self,
        *,
        project_id: str,
        rule_key: str,
        chapter_number: int,
        status: ActiveRuleStatus,
        reason: str,
        evidence_refs: list[str] | None = None,
    ) -> StatusTransitionResult:
        key = str(rule_key or "").strip()
        chapter = int(chapter_number or 0)
        latest_event_chapter = self._latest_event_chapter(
            project_id=project_id,
            rule_key=key,
        )
        if latest_event_chapter is not None and chapter < latest_event_chapter:
            return StatusTransitionResult(
                applied=False,
                rule_key=key,
                to_status=status,
                reason="out_of_order_rule_event",
            )
        current = next(
            (
                rule
                for rule in self.query_rules_as_of(
                    project_id=project_id,
                    chapter_number=chapter,
                )
                if rule.rule_key == key
            ),
            None,
        )
        if current is None:
            return StatusTransitionResult(
                applied=False,
                rule_key=key,
                to_status=status,
                reason="active_rule_not_found",
            )
        if status not in _ALLOWED_STATUS_TRANSITIONS[current.status]:
            return StatusTransitionResult(
                applied=False,
                rule_key=key,
                from_status=current.status,
                to_status=status,
                reason="invalid_status_transition",
            )
        evidence = _dedupe([*current.promotion_evidence, *(evidence_refs or [])])
        status_sequence = self._next_status_sequence(
            project_id=project_id,
            rule_key=key,
        )
        updated = current.model_copy(
            update={"status": status, "promotion_evidence": evidence}
        )
        self.session.add(
            CanonQualitySignalRow(
                project_id=project_id,
                signal_id=(
                    f"active_rule_status:{project_id}:{key}:"
                    f"{chapter}:{status}:{status_sequence}"
                ),
                chapter_number=chapter,
                signal_type=self.STATUS_CHANGED,
                severity="info",
                target_scope="book",
                subject_key=key,
                description=str(reason or f"active rule status changed to {status}"),
                evidence_refs_json=json.dumps(evidence_refs or [], ensure_ascii=False),
                payload_json=json.dumps(
                    {
                        "active_rule": updated.model_dump(mode="json"),
                        "from_status": current.status,
                        "to_status": status,
                        "status_sequence": status_sequence,
                        "reason": str(reason or ""),
                        "source": "ActiveRuleStore",
                    },
                    ensure_ascii=False,
                ),
                status="resolved",
            )
        )
        self.session.flush()
        return StatusTransitionResult(
            applied=True,
            rule_key=key,
            from_status=current.status,
            to_status=status,
        )

    def _next_status_sequence(self, *, project_id: str, rule_key: str) -> int:
        rows = list(
            self.session.scalars(
                select(CanonQualitySignalRow).where(
                    CanonQualitySignalRow.project_id == project_id,
                    CanonQualitySignalRow.subject_key == rule_key,
                    CanonQualitySignalRow.signal_type.in_(
                        (self.REGISTERED, self.STATUS_CHANGED)
                    ),
                )
            ).all()
        )
        return max(
            (
                int(_json_object(row.payload_json).get("status_sequence") or 0)
                for row in rows
            ),
            default=-1,
        ) + 1

    def _latest_event_chapter(
        self,
        *,
        project_id: str,
        rule_key: str,
    ) -> int | None:
        value = self.session.scalar(
            select(func.max(CanonQualitySignalRow.chapter_number)).where(
                CanonQualitySignalRow.project_id == project_id,
                CanonQualitySignalRow.subject_key == rule_key,
                CanonQualitySignalRow.signal_type.in_(
                    (self.REGISTERED, self.STATUS_CHANGED)
                ),
            )
        )
        return int(value) if value is not None else None

    def _has_overlapping_rule(
        self,
        *,
        project_id: str,
        rule: ActiveRule,
        trigger_quote: TriggerQuote,
    ) -> bool:
        candidate_start = int(
            rule.valid_from_chapter or trigger_quote.chapter_number or 0
        )
        current = next(
            (
                item
                for item in self.query_rules_as_of(
                    project_id=project_id,
                    chapter_number=candidate_start,
                )
                if item.rule_key == rule.rule_key
            ),
            None,
        )
        if current is None or current.status == "retired":
            return False
        current_end = (
            int(current.valid_until_chapter)
            if current.valid_until_chapter is not None
            else 1_000_000_000
        )
        candidate_end = (
            int(rule.valid_until_chapter)
            if rule.valid_until_chapter is not None
            else 1_000_000_000
        )
        return candidate_start <= current_end and int(
            current.valid_from_chapter or 0
        ) <= candidate_end


def _signal_row(signal: CanonQualitySignal) -> CanonQualitySignalRow:
    return CanonQualitySignalRow(
        project_id=signal.project_id,
        signal_id=signal.signal_id,
        chapter_number=signal.chapter_number,
        signal_type=signal.signal_type,
        severity=signal.severity,
        target_scope=signal.target_scope,
        subject_key=signal.subject_key,
        description=signal.description,
        evidence_refs_json=json.dumps(signal.evidence_refs, ensure_ascii=False),
        payload_json=json.dumps(signal.payload, ensure_ascii=False),
        status=signal.status,
    )


def _json_object(raw: str) -> dict:
    try:
        value = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _rule_from_row(row: CanonQualitySignalRow) -> ActiveRule | None:
    raw_rule = _json_object(row.payload_json).get("active_rule")
    if not isinstance(raw_rule, dict):
        return None
    try:
        return ActiveRule.model_validate(raw_rule)
    except ValueError:
        return None


def _materialize_rules(
    rows: list[CanonQualitySignalRow],
) -> dict[str, ActiveRule]:
    states: dict[str, ActiveRule] = {}
    for row in rows:
        rule = _rule_from_row(row)
        if rule is not None:
            states[rule.rule_key] = rule
    return states


def _rule_row_order(row: CanonQualitySignalRow) -> tuple[int, int, str]:
    payload = _json_object(row.payload_json)
    return (
        int(row.chapter_number or 0),
        int(payload.get("status_sequence") or 0),
        str(row.id or ""),
    )


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


__all__ = [
    "ActiveRule",
    "ActiveRulePatch",
    "ActiveRuleStatus",
    "ActiveRuleStore",
    "CanonQualityActiveRuleStore",
    "RegistrationResult",
    "StatusTransitionResult",
    "TriggerQuote",
]
