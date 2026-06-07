from __future__ import annotations

import json
from typing import Protocol

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.canon_quality.signals import CanonQualitySignal
from forwin.models.canon_quality import CanonQualitySignalRow


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


class ActiveRulePatch(BaseModel):
    rule: ActiveRule
    trigger_quote: TriggerQuote


class RegistrationResult(BaseModel):
    applied: bool = False
    rule_key: str = ""
    reason: str = ""


class RevocationResult(BaseModel):
    applied: bool = False
    rule_key: str = ""
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

    def revoke_rule(
        self,
        *,
        project_id: str,
        rule_key: str,
        revoke_chapter: int,
        reason: str,
    ) -> RevocationResult: ...


class CanonQualityActiveRuleStore:
    """Persist active-rule events through the canon-quality signal ledger."""

    REGISTERED = "active_rule_registered"
    REVOKED = "active_rule_revoked"

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
        if self._has_overlapping_active_interval(project_id=project_id, rule=rule, trigger_quote=trigger_quote):
            return RegistrationResult(applied=False, rule_key=key, reason="active_rule_conflict")
        signal = CanonQualitySignal(
            signal_id=f"active_rule:{project_id}:{key}",
            project_id=project_id,
            chapter_number=int(rule.valid_from_chapter or trigger_quote.chapter_number or 0),
            signal_type=self.REGISTERED,
            severity="info",
            target_scope="book",
            subject_key=key,
            description=rule.summary or key,
            evidence_refs=[trigger_quote.source_ref] if trigger_quote.source_ref else [],
            payload={
                "active_rule": rule.model_dump(mode="json"),
                "trigger_quote": trigger_quote.model_dump(mode="json"),
                "source": "ActiveRuleStore",
            },
            status="open",
        )
        row = CanonQualitySignalRow(
            project_id=project_id,
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
        self.session.add(row)
        self.session.flush()
        return RegistrationResult(applied=True, rule_key=key)

    def query_active_as_of(
        self,
        *,
        project_id: str,
        chapter_number: int,
    ) -> list[ActiveRule]:
        as_of = int(chapter_number or 0)
        rows = self.session.execute(
            select(CanonQualitySignalRow).where(
                CanonQualitySignalRow.project_id == project_id,
                CanonQualitySignalRow.signal_type.in_((self.REGISTERED, self.REVOKED)),
                CanonQualitySignalRow.chapter_number <= as_of,
            )
            .order_by(CanonQualitySignalRow.chapter_number.asc(), CanonQualitySignalRow.created_at.asc())
        ).scalars().all()
        revokes_by_key = _revokes_by_key(rows)
        result: list[ActiveRule] = []
        for row in rows:
            if row.signal_type != self.REGISTERED:
                continue
            rule = _rule_from_row(row)
            if rule is not None and _rule_active_at(rule, row=row, as_of=as_of, revokes_by_key=revokes_by_key):
                result.append(rule)
        return result

    def revoke_rule(
        self,
        *,
        project_id: str,
        rule_key: str,
        revoke_chapter: int,
        reason: str,
    ) -> RevocationResult:
        key = str(rule_key or "").strip()
        row = self._active_row(project_id=project_id, rule_key=key, chapter_number=int(revoke_chapter or 0))
        if row is None:
            return RevocationResult(applied=False, rule_key=key, reason="active_rule_not_found")
        chapter = int(revoke_chapter or 0)
        signal = CanonQualitySignalRow(
            project_id=project_id,
            signal_id=f"active_rule_revoked:{project_id}:{key}:{chapter}",
            chapter_number=chapter,
            signal_type=self.REVOKED,
            severity="info",
            target_scope="book",
            subject_key=key,
            description=str(reason or "active rule revoked"),
            evidence_refs_json="[]",
            payload_json=json.dumps(
                {
                    "rule_key": key,
                    "revoke_chapter": chapter,
                    "reason": str(reason or ""),
                    "source": "ActiveRuleStore",
                },
                ensure_ascii=False,
            ),
            status="resolved",
        )
        self.session.add(signal)
        self.session.flush()
        return RevocationResult(applied=True, rule_key=key)

    def _active_row(self, *, project_id: str, rule_key: str, chapter_number: int) -> CanonQualitySignalRow | None:
        rows = self.session.execute(
            select(CanonQualitySignalRow).where(
                CanonQualitySignalRow.project_id == project_id,
                CanonQualitySignalRow.signal_type.in_((self.REGISTERED, self.REVOKED)),
                CanonQualitySignalRow.chapter_number <= int(chapter_number or 0),
            )
            .order_by(CanonQualitySignalRow.chapter_number.asc(), CanonQualitySignalRow.created_at.asc())
        ).scalars().all()
        revokes_by_key = _revokes_by_key(rows)
        for row in rows:
            if row.signal_type != self.REGISTERED or row.subject_key != rule_key:
                continue
            rule = _rule_from_row(row)
            if rule is not None and _rule_active_at(
                rule,
                row=row,
                as_of=int(chapter_number or 0),
                revokes_by_key=revokes_by_key,
            ):
                return row
        return None

    def _has_overlapping_active_interval(
        self,
        *,
        project_id: str,
        rule: ActiveRule,
        trigger_quote: TriggerQuote,
    ) -> bool:
        key = str(rule.rule_key or "").strip()
        if not key:
            return False
        rows = self.session.execute(
            select(CanonQualitySignalRow).where(
                CanonQualitySignalRow.project_id == project_id,
                CanonQualitySignalRow.signal_type.in_((self.REGISTERED, self.REVOKED)),
                CanonQualitySignalRow.subject_key == key,
            )
            .order_by(CanonQualitySignalRow.chapter_number.asc(), CanonQualitySignalRow.created_at.asc())
        ).scalars().all()
        revokes_by_key = _revokes_by_key(rows)
        candidate_start = int(rule.valid_from_chapter or trigger_quote.chapter_number or 0)
        candidate_end = _interval_end_exclusive(rule.valid_until_chapter, None)
        for row in rows:
            if row.signal_type != self.REGISTERED:
                continue
            existing = _rule_from_row(row)
            if existing is None:
                continue
            existing_start = _rule_start(existing, row)
            existing_end = _interval_end_exclusive(existing.valid_until_chapter, _first_revoke_at_or_after(existing.rule_key, existing_start, revokes_by_key))
            if _intervals_overlap(candidate_start, candidate_end, existing_start, existing_end):
                return True
        return False


def _json_object(raw: str) -> dict:
    try:
        value = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _rule_from_row(row: CanonQualitySignalRow) -> ActiveRule | None:
    payload = _json_object(row.payload_json)
    raw_rule = payload.get("active_rule") if isinstance(payload, dict) else {}
    if not isinstance(raw_rule, dict):
        return None
    return ActiveRule.model_validate(raw_rule)


def _revokes_by_key(rows: list[CanonQualitySignalRow]) -> dict[str, list[int]]:
    result: dict[str, list[int]] = {}
    for row in rows:
        payload = _json_object(row.payload_json)
        if row.signal_type == CanonQualityActiveRuleStore.REVOKED:
            key = str(payload.get("rule_key") or row.subject_key or "").strip()
            chapter = int(payload.get("revoke_chapter") or row.chapter_number or 0)
        elif row.signal_type == CanonQualityActiveRuleStore.REGISTERED:
            key = str(row.subject_key or "").strip()
            chapter = int(payload.get("revoked_at_chapter") or 0)
        else:
            continue
        if key and chapter > 0:
            result.setdefault(key, []).append(chapter)
    for chapters in result.values():
        chapters.sort()
    return result


def _rule_active_at(
    rule: ActiveRule,
    *,
    row: CanonQualitySignalRow,
    as_of: int,
    revokes_by_key: dict[str, list[int]],
) -> bool:
    start = _rule_start(rule, row)
    if start > as_of:
        return False
    if rule.valid_until_chapter is not None and int(rule.valid_until_chapter) < as_of:
        return False
    revoke_chapter = _first_revoke_at_or_after(rule.rule_key, start, revokes_by_key)
    return revoke_chapter is None or revoke_chapter > as_of


def _rule_start(rule: ActiveRule, row: CanonQualitySignalRow) -> int:
    return int(rule.valid_from_chapter or row.chapter_number or 0)


def _first_revoke_at_or_after(
    rule_key: str,
    start: int,
    revokes_by_key: dict[str, list[int]],
) -> int | None:
    for chapter in revokes_by_key.get(str(rule_key or "").strip(), []):
        if chapter >= start:
            return chapter
    return None


def _interval_end_exclusive(valid_until_chapter: int | None, revoke_chapter: int | None) -> int:
    ends: list[int] = []
    if valid_until_chapter is not None:
        ends.append(int(valid_until_chapter) + 1)
    if revoke_chapter is not None:
        ends.append(int(revoke_chapter))
    return min(ends) if ends else 1_000_000_000


def _intervals_overlap(left_start: int, left_end: int, right_start: int, right_end: int) -> bool:
    return max(left_start, right_start) < min(left_end, right_end)


__all__ = [
    "ActiveRule",
    "ActiveRulePatch",
    "ActiveRuleStore",
    "CanonQualityActiveRuleStore",
    "RegistrationResult",
    "RevocationResult",
    "TriggerQuote",
]
