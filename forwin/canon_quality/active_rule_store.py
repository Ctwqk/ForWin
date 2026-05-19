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
        if self._active_row(project_id=project_id, rule_key=key) is not None:
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
        rows = self.session.execute(
            select(CanonQualitySignalRow).where(
                CanonQualitySignalRow.project_id == project_id,
                CanonQualitySignalRow.signal_type == self.REGISTERED,
                CanonQualitySignalRow.status == "open",
                CanonQualitySignalRow.chapter_number <= int(chapter_number or 0),
            )
        ).scalars().all()
        result: list[ActiveRule] = []
        for row in rows:
            payload = _json_object(row.payload_json)
            raw_rule = payload.get("active_rule") if isinstance(payload, dict) else {}
            if isinstance(raw_rule, dict):
                result.append(ActiveRule.model_validate(raw_rule))
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
        row = self._active_row(project_id=project_id, rule_key=key)
        if row is None:
            return RevocationResult(applied=False, rule_key=key, reason="active_rule_not_found")
        row.status = "resolved"
        payload = _json_object(row.payload_json)
        payload["revoked_at_chapter"] = int(revoke_chapter or 0)
        payload["revoke_reason"] = str(reason or "")
        row.payload_json = json.dumps(payload, ensure_ascii=False)
        self.session.add(row)
        self.session.flush()
        return RevocationResult(applied=True, rule_key=key)

    def _active_row(self, *, project_id: str, rule_key: str) -> CanonQualitySignalRow | None:
        return self.session.execute(
            select(CanonQualitySignalRow).where(
                CanonQualitySignalRow.project_id == project_id,
                CanonQualitySignalRow.signal_type == self.REGISTERED,
                CanonQualitySignalRow.status == "open",
                CanonQualitySignalRow.subject_key == rule_key,
            )
        ).scalars().first()


def _json_object(raw: str) -> dict:
    try:
        value = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


__all__ = [
    "ActiveRule",
    "ActiveRulePatch",
    "ActiveRuleStore",
    "CanonQualityActiveRuleStore",
    "RegistrationResult",
    "RevocationResult",
    "TriggerQuote",
]
