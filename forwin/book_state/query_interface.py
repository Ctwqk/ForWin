from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from forwin.canon_quality.active_rule_store import ActiveRule, CanonQualityActiveRuleStore
from forwin.canon_quality.invariants import CanonInvariant, invariant_from_active_rule


class InvariantStateSnapshot(BaseModel):
    project_id: str
    as_of_chapter: int
    invariants: dict[str, CanonInvariant] = Field(default_factory=dict)
    active_rules: list[ActiveRule] = Field(default_factory=list)


class BookStateQueryInterface(Protocol):
    def get_current_invariant_state(
        self,
        *,
        project_id: str,
        as_of_chapter: int,
    ) -> InvariantStateSnapshot: ...

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
        active_rules = self.get_active_rules(project_id=project_id, as_of_chapter=as_of_chapter)
        return InvariantStateSnapshot(
            project_id=project_id,
            as_of_chapter=int(as_of_chapter or 0),
            invariants=self.get_current_invariants(
                project_id=project_id,
                as_of_chapter=as_of_chapter,
            ),
            active_rules=active_rules,
        )

    def get_current_invariants(
        self,
        *,
        project_id: str,
        as_of_chapter: int,
    ) -> dict[str, CanonInvariant]:
        invariants: dict[str, CanonInvariant] = {}
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


__all__ = [
    "BookStateQueryInterface",
    "InvariantStateSnapshot",
    "SqlBookStateQueryInterface",
]
