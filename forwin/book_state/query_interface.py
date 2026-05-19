from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.canon_quality.active_rule_store import ActiveRule, CanonQualityActiveRuleStore
from forwin.models.canon_quality import CountdownLedgerRow


class CountdownState(BaseModel):
    key: str
    remaining_minutes: int | None = None
    status: str = "active"
    chapter_number: int = 0
    source: str = "book_state_query_interface"


class InvariantStateSnapshot(BaseModel):
    project_id: str
    as_of_chapter: int
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

    def get_active_rules(
        self,
        *,
        project_id: str,
        as_of_chapter: int,
    ) -> list[ActiveRule]: ...


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
        return InvariantStateSnapshot(
            project_id=project_id,
            as_of_chapter=int(as_of_chapter or 0),
            countdowns=self.get_current_countdown_values(project_id=project_id, as_of_chapter=as_of_chapter),
            active_rules=self.get_active_rules(project_id=project_id, as_of_chapter=as_of_chapter),
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
                remaining_minutes=row.normalized_remaining_minutes,
                status=row.status,
                chapter_number=row.chapter_number,
                source="countdown_ledger_via_book_state_query_interface",
            )
        return latest

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


__all__ = [
    "BookStateQueryInterface",
    "CountdownState",
    "InvariantStateSnapshot",
    "SqlBookStateQueryInterface",
]
