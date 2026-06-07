from __future__ import annotations

import json
from typing import Protocol

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.book_state.macro_status import (
    ProtagonistMacroStatus,
    derive_protagonist_macro_status,
)
from forwin.canon_quality.active_rule_store import ActiveRule, CanonQualityActiveRuleStore
from forwin.canon_quality.invariants import (
    CanonInvariant,
    invariant_from_active_rule,
    invariant_from_countdown_state,
)
from forwin.models.canon_quality import CountdownLedgerRow


class CountdownState(BaseModel):
    key: str
    label: str = ""
    remaining_minutes: int | None = None
    status: str = "active"
    chapter_number: int = 0
    source: str = "book_state_query_interface"
    raw_mention: str = ""
    evidence_refs: list[str] = Field(default_factory=list)
    payload: dict = Field(default_factory=dict)


class InvariantStateSnapshot(BaseModel):
    project_id: str
    as_of_chapter: int
    invariants: dict[str, CanonInvariant] = Field(default_factory=dict)
    countdowns: dict[str, CountdownState] = Field(default_factory=dict)
    active_rules: list[ActiveRule] = Field(default_factory=list)


class BookStateQueryInterface(Protocol):
    def get_current_invariant_state(
        self,
        *,
        project_id: str,
        as_of_chapter: int,
    ) -> InvariantStateSnapshot: ...

    def get_current_countdown_values(
        self,
        *,
        project_id: str,
        as_of_chapter: int,
    ) -> dict[str, CountdownState]: ...

    def get_current_invariants(
        self,
        *,
        project_id: str,
        as_of_chapter: int,
    ) -> dict[str, CanonInvariant]: ...

    def get_active_rules(
        self,
        *,
        project_id: str,
        as_of_chapter: int,
    ) -> list[ActiveRule]: ...

    def get_protagonist_macro_status(
        self,
        *,
        project_id: str,
        as_of_chapter: int,
    ) -> ProtagonistMacroStatus: ...


class SqlBookStateQueryInterface:
    """Stable canon-state query boundary for repair-time consumers.

    The implementation may use compatibility rows internally, but callers depend
    only on this BookState-facing contract.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def get_current_invariant_state(
        self,
        *,
        project_id: str,
        as_of_chapter: int,
    ) -> InvariantStateSnapshot:
        countdowns = self.get_current_countdown_values(project_id=project_id, as_of_chapter=as_of_chapter)
        active_rules = self.get_active_rules(project_id=project_id, as_of_chapter=as_of_chapter)
        return InvariantStateSnapshot(
            project_id=project_id,
            as_of_chapter=int(as_of_chapter or 0),
            invariants=self.get_current_invariants(
                project_id=project_id,
                as_of_chapter=as_of_chapter,
            ),
            countdowns=countdowns,
            active_rules=active_rules,
        )

    def get_current_countdown_values(
        self,
        *,
        project_id: str,
        as_of_chapter: int,
    ) -> dict[str, CountdownState]:
        rows = self.session.execute(
            select(CountdownLedgerRow).where(
                CountdownLedgerRow.project_id == project_id,
                CountdownLedgerRow.chapter_number <= int(as_of_chapter or 0),
            ).order_by(CountdownLedgerRow.countdown_key.asc(), CountdownLedgerRow.chapter_number.asc())
        ).scalars().all()
        latest: dict[str, CountdownState] = {}
        for row in rows:
            latest[row.countdown_key] = CountdownState(
                key=row.countdown_key,
                label=row.label,
                remaining_minutes=row.normalized_remaining_minutes,
                status=row.status,
                chapter_number=row.chapter_number,
                source="countdown_ledger_via_book_state_query_interface",
                raw_mention=row.raw_mention,
                evidence_refs=_json_list(row.evidence_refs_json),
                payload=_json_object(row.payload_json),
            )
        return latest

    def get_current_invariants(
        self,
        *,
        project_id: str,
        as_of_chapter: int,
    ) -> dict[str, CanonInvariant]:
        countdowns = self.get_current_countdown_values(project_id=project_id, as_of_chapter=as_of_chapter)
        invariants: dict[str, CanonInvariant] = {}
        for key, countdown in countdowns.items():
            invariant = invariant_from_countdown_state(
                key=key,
                label=countdown.label,
                remaining_minutes=countdown.remaining_minutes,
                status=countdown.status,
                chapter_number=countdown.chapter_number,
                raw_mention=countdown.raw_mention,
                evidence_refs=countdown.evidence_refs,
                payload=countdown.payload,
            )
            invariants[invariant.invariant_key] = invariant
        for rule in self.get_active_rules(project_id=project_id, as_of_chapter=as_of_chapter):
            invariant = invariant_from_active_rule(rule)
            if invariant.invariant_key:
                invariants[invariant.invariant_key] = invariant
        return invariants

    def get_active_rules(
        self,
        *,
        project_id: str,
        as_of_chapter: int,
    ) -> list[ActiveRule]:
        return CanonQualityActiveRuleStore(self.session).query_active_as_of(
            project_id=project_id,
            chapter_number=int(as_of_chapter or 0),
        )

    def get_protagonist_macro_status(
        self,
        *,
        project_id: str,
        as_of_chapter: int,
    ) -> ProtagonistMacroStatus:
        return derive_protagonist_macro_status(
            self.session,
            project_id=project_id,
            as_of_chapter=int(as_of_chapter or 0),
        )


def _json_list(raw: str) -> list[str]:
    try:
        value = json.loads(str(raw or "[]"))
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _json_object(raw: str) -> dict:
    try:
        value = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


__all__ = [
    "BookStateQueryInterface",
    "CountdownState",
    "InvariantStateSnapshot",
    "ProtagonistMacroStatus",
    "SqlBookStateQueryInterface",
]
