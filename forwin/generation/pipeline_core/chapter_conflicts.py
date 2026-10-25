"""The chapter stage's side of chapter conflicts.

After draft review, and again when canon preparation's review gate blocks the candidate, the stage records the
candidate's residual boundary violations. A Codex rewrite_chapter buys one more repair pass per chapter run, after
which every conflict goes to a human; a conflict a human decides pauses the chapter mid-way, with its draft kept as
the latest candidate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, NamedTuple

from sqlalchemy.orm import Session

from forwin.book_state.reviewer import BookStateReviewVerdict
from forwin.candidate_drafts import CandidateDraftRepository
from forwin.checker.rules import ContinuityChecker
from forwin.maintenance.post_canon import own_transaction_audit_writer
from forwin.models.project import ChapterPlan
from forwin.protocol.review import ReviewVerdict
from forwin.protocol.writer import WriterOutput
from forwin.runtime.policy import RuntimePolicy
from forwin.state.repo import StateRepository
from forwin.state.updater import StateUpdater
from forwin.world_engine.chapter_conflicts import (
    CANON_GATE_DETECTION,
    DRAFT_REVIEW_DETECTION,
    ChapterConflictRecorder,
    violations_from_gate,
    violations_from_verdict,
)
from forwin.world_engine.conflict_delegate import WorldConflictDelegate
from forwin.world_engine.conflict_repository import WorldConflictRepository


class ChapterConflictBlocked(RuntimeError):
    """A chapter conflict a human decides blocks the chapter; the chapter is already marked needs_review."""

    def __init__(self, conflict_id: str) -> None:
        super().__init__(f"world_conflict:{conflict_id}")
        self.conflict_id = conflict_id


class ChapterConflictStage:
    """Builds the recorder of a chapter's boundary conflicts."""

    def _chapter_conflict_recorder(self, session: Session, policy: RuntimePolicy) -> ChapterConflictRecorder:
        """Its Codex reviews keep their audit trail in transactions of their own."""
        return ChapterConflictRecorder(
            session,
            policy=policy,
            delegate=WorldConflictDelegate(self.llm_client),
            audit_writer_factory=own_transaction_audit_writer(self._SessionFactory),
        )


class RecordedChapterConflicts(NamedTuple):
    writer_output: WriterOutput
    verdict: ReviewVerdict
    force_accept_applied: bool
    # Whether a Codex rewrite_chapter ran one more repair pass, which changed the candidate and its verdict.
    repaired: bool


@dataclass
class ChapterConflicts:
    """One chapter run's conflict recording, bound to the stage's helpers for that chapter."""

    stage: Any
    session: Session
    repo: StateRepository
    updater: StateUpdater
    checker: ContinuityChecker
    policy: RuntimePolicy
    chapter_plan: ChapterPlan
    context: Any
    # Codex decides once per chapter run: after its extra repair pass every conflict goes to a human.
    codex_pass_used: bool = False

    def record(
        self,
        writer_output: WriterOutput,
        verdict: ReviewVerdict,
        force_accept_applied: bool,
        *,
        gate: BookStateReviewVerdict | None = None,
    ) -> RecordedChapterConflicts:
        """Record the latest candidate's residual boundary violations, from ``verdict`` or from the blocking
        ``gate`` alone, then commit.

        Codex choosing rewrite_chapter buys one more repair pass unless a human conflict already blocks the
        chapter. After the pass the repaired draft's own boundary violations are recorded. On the gate path, a
        repaired draft without one goes back to the canon loop, which prepares again so the gate records what is
        left. Every conflict recorded after the pass goes to a human. A conflict a human decides marks the chapter
        needs_review and raises ChapterConflictBlocked.
        """
        session, project_id = self.session, self.chapter_plan.project_id
        chapter_number = int(self.chapter_plan.chapter_number)
        # Lock order: conflict rows come before the chapter plan, which this transaction may already have written.
        session.commit()
        recorder = self.stage._chapter_conflict_recorder(session, self.policy)
        candidates = CandidateDraftRepository(session)
        candidate = candidates.latest_for_chapter(project_id=project_id, chapter_number=chapter_number)
        outcome = recorder.record(
            project_id=project_id, chapter_number=chapter_number, candidate_id=candidate.id,
            violations=violations_from_gate(gate) if gate is not None else violations_from_verdict(verdict),
            detected_by=CANON_GATE_DETECTION if gate is not None else DRAFT_REVIEW_DETECTION,
            allow_codex=not self.codex_pass_used,
        )
        repaired = bool(outcome.codex_rewrite_ids) and not outcome.blocking_conflict_id
        if repaired:
            self.codex_pass_used = True
            # The repair pass calls the writer: no conflict row stays locked across it.
            session.commit()
            conflicts = WorldConflictRepository(session)
            rows = [conflicts.get(project_id, conflict_id) for conflict_id in outcome.codex_rewrite_ids]
            writer_output, verdict, repair_force_accept = self.stage.repair.repair_world_conflict(
                execution=self.stage.repair_execution, session=session, repo=self.repo, updater=self.updater,
                checker=self.checker, project_id=project_id, chapter_plan=self.chapter_plan, context=self.context,
                writer_output=writer_output, verdict=verdict, conflicts=rows,
            )
            force_accept_applied = force_accept_applied or repair_force_accept
            candidate = candidates.latest_for_chapter(project_id=project_id, chapter_number=chapter_number)
            # The repair the decision asked for ran on this candidate, so the decision is no pending resume.
            for row in rows:
                conflicts.attach_result_refs(row, [f"candidate:{candidate.id}"])
            # A draft row never closes a gate row, so on the gate path the repaired draft's violations are recorded
            # too: canon preparation would refuse its failing review before the gate runs.
            draft_violations = violations_from_verdict(verdict)
            if gate is None or draft_violations:
                outcome = recorder.record(
                    project_id=project_id, chapter_number=chapter_number, candidate_id=candidate.id,
                    violations=draft_violations, allow_codex=False,
                )
        if outcome.blocking_conflict_id:
            repair_attempt_count, residual_review_issues, _risk = self.review_state(verdict)
            self.updater.mark_chapter_status(
                project_id, chapter_number, "needs_review", repair_attempt_count=repair_attempt_count,
                residual_review_issues=residual_review_issues, canon_risk_level="high",
            )
            session.commit()
            raise ChapterConflictBlocked(outcome.blocking_conflict_id)
        session.commit()
        return RecordedChapterConflicts(writer_output, verdict, force_accept_applied, repaired)

    def review_state(self, verdict: ReviewVerdict) -> tuple[int, list[dict[str, Any]], str]:
        """The chapter's repair attempt count, residual review issues and canon risk after ``verdict``."""
        return (
            int(self.chapter_plan.repair_attempt_count or 0),
            self.stage._review_issue_payloads(verdict),
            self.stage._review_canon_risk(verdict),
        )


__all__ = ["ChapterConflictBlocked", "ChapterConflictStage", "ChapterConflicts", "RecordedChapterConflicts"]
