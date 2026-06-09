from __future__ import annotations

from typing import Protocol

from sqlalchemy.orm import Session

from forwin.book_state.review_gate_ext import (
    BookStateDirectCommitResult,
    BookStateDirectCommitService,
)
from forwin.protocol.book_state import ApprovedGraphDeltaSet, BookStateCompileResult


class CanonPort(Protocol):
    def compile(
        self,
        changes: ApprovedGraphDeltaSet,
        *,
        compiler_run_id: str = "",
    ) -> BookStateCompileResult:
        ...

    def commit(
        self,
        changes: ApprovedGraphDeltaSet,
        *,
        compiler_run_id: str = "",
    ) -> BookStateDirectCommitResult:
        ...


class BookStateCanonPort:
    def __init__(self, commit_service) -> None:
        self.commit_service = commit_service

    @classmethod
    def for_session(cls, session: Session) -> "BookStateCanonPort":
        return cls(BookStateDirectCommitService(session))

    def compile(
        self,
        changes: ApprovedGraphDeltaSet,
        *,
        compiler_run_id: str = "",
    ) -> BookStateCompileResult:
        return self.commit_service.compile_approved(
            changes,
            compiler_run_id=compiler_run_id,
        )

    def commit(
        self,
        changes: ApprovedGraphDeltaSet,
        *,
        compiler_run_id: str = "",
    ) -> BookStateDirectCommitResult:
        return self.commit_service.commit(
            changes,
            compiler_run_id=compiler_run_id,
        )


__all__ = ["BookStateCanonPort", "CanonPort"]
