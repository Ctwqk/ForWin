from __future__ import annotations

from pydantic import BaseModel
from sqlalchemy.orm import Session

from forwin.book_state.compiler import BookStateCompiler
from forwin.book_state.reviewer import BookStateReviewGate, BookStateReviewVerdict
from forwin.protocol.book_state import ApprovedGraphDeltaSet, BookStateCompileResult


class BookStateDirectCommitResult(BaseModel):
    project_id: str
    chapter_number: int
    review_verdict: BookStateReviewVerdict
    compile_result: BookStateCompileResult | None = None

    @property
    def committed(self) -> bool:
        return bool(self.compile_result and self.compile_result.committed)


class BookStateDirectCommitService:
    """Run the direct BookState review/compile path for accepted chapter changes."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def review(self, changes: ApprovedGraphDeltaSet) -> BookStateReviewVerdict:
        return BookStateReviewGate(self.session).review(changes)

    def compile_approved(
        self,
        changes: ApprovedGraphDeltaSet,
        *,
        compiler_run_id: str = "",
    ) -> BookStateCompileResult:
        nested = self.session.begin_nested()
        try:
            result = BookStateCompiler(self.session).compile(
                changes,
                compiler_run_id=compiler_run_id,
            )
            if result.committed:
                nested.commit()
            else:
                nested.rollback()
            return result
        except Exception:
            nested.rollback()
            raise

    def commit(
        self,
        changes: ApprovedGraphDeltaSet,
        *,
        compiler_run_id: str = "",
    ) -> BookStateDirectCommitResult:
        review_verdict = self.review(changes)
        if not review_verdict.accepted or review_verdict.approved_changes is None:
            return BookStateDirectCommitResult(
                project_id=changes.project_id,
                chapter_number=changes.chapter_number,
                review_verdict=review_verdict,
            )
        compile_result = self.compile_approved(
            review_verdict.approved_changes,
            compiler_run_id=compiler_run_id,
        )
        return BookStateDirectCommitResult(
            project_id=changes.project_id,
            chapter_number=changes.chapter_number,
            review_verdict=review_verdict,
            compile_result=compile_result,
        )


__all__ = ["BookStateDirectCommitResult", "BookStateDirectCommitService"]
