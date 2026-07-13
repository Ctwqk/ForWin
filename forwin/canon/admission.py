from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from forwin.book_state.compiler import BookStateCompiler
from forwin.candidate_drafts import (
    CandidateDraftRepository,
    candidate_body_hash,
    candidate_plan_revision,
    candidate_writer_output_admission_fingerprint,
)
from forwin.models.base import new_id
from forwin.models.book_state import GraphDeltaRow
from forwin.models.canon import CanonCommitRecord
from forwin.models.draft import CandidateDraftRecord, ChapterDraft
from forwin.models.audit import DecisionEvent
from forwin.models.knowledge import KnowledgeEditProposalRow
from forwin.models.project import ChapterPlan, Project
from forwin.narrative_obligations.repository import NarrativeObligationRepository
from forwin.outbox.store import enqueue_outbox_event
from forwin.protocol.book_state import ApprovedGraphDeltaSet, BookStateCompileResult

from .entity_admission import EntityAdmissionCommitter
from .plan import CanonCommitPlan
from .types import CanonAdmissionOutcome, CanonWorldEditOutcome


logger = logging.getLogger(__name__)


class CanonStaleVersion(RuntimeError):
    """The prepared plan no longer matches locked authoritative state."""


class CanonWriteFailure(RuntimeError):
    """An authoritative Canon write could not be completed."""


class CanonAdmissionService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session] | None = None,
        transaction_guard: Callable[[Session], bool] | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.transaction_guard = transaction_guard

    def commit_plan(
        self,
        plan: CanonCommitPlan,
        *,
        failure_injector: Callable[[str], None] | None = None,
    ) -> CanonAdmissionOutcome:
        if self.session_factory is None:
            raise RuntimeError("CanonAdmissionService requires a session factory")
        inject = failure_injector or _ignore_failure_stage
        try:
            with self.session_factory.begin() as session:
                project = session.execute(
                    select(Project)
                    .where(Project.id == plan.project_id)
                    .with_for_update()
                ).scalar_one_or_none()
                if project is None:
                    raise CanonStaleVersion("project no longer exists")
                if self.transaction_guard is not None and not self.transaction_guard(
                    session
                ):
                    raise CanonStaleVersion("generation task lease lost before Canon commit")

                prior = session.execute(
                    select(CanonCommitRecord)
                    .where(CanonCommitRecord.idempotency_key == plan.idempotency_key)
                    .with_for_update()
                ).scalar_one_or_none()
                if prior is not None:
                    return _outcome_from_record(prior, idempotent=True)

                chapter = (
                    session.execute(
                        select(ChapterPlan)
                        .where(
                            ChapterPlan.project_id == plan.project_id,
                            ChapterPlan.chapter_number == plan.chapter_number,
                        )
                        .with_for_update()
                    )
                    .scalars()
                    .first()
                )
                candidate = session.execute(
                    select(CandidateDraftRecord)
                    .where(CandidateDraftRecord.id == plan.candidate_id)
                    .with_for_update()
                ).scalar_one_or_none()
                self._revalidate_locked_plan(
                    session=session,
                    project=project,
                    chapter=chapter,
                    candidate=candidate,
                    plan=plan,
                )

                commit_id = new_id()
                candidate_repository = CandidateDraftRepository(session)
                candidate_repository.transition(plan.candidate_id, "committing")

                compile_result = BookStateCompiler(session).compile(
                    plan.approved_book_state_changes,
                    compiler_run_id=f"canon-commit-{commit_id}",
                )
                if not compile_result.committed:
                    raise CanonWriteFailure(
                        "; ".join(compile_result.blocked_reasons)
                        or "BookState compiler rejected the approved changes"
                    )
                if compile_result.metadata.get("idempotent"):
                    raise CanonStaleVersion(
                        "BookState deltas already exist without a Canon commit record"
                    )
                session.flush()
                inject("book_state")

                try:
                    EntityAdmissionCommitter(session).apply(
                        project_id=plan.project_id,
                        plan=plan.entity_admission_plan,
                    )
                except ValueError as exc:
                    raise CanonStaleVersion(str(exc)) from exc
                session.flush()
                inject("entity")

                NarrativeObligationRepository(session).activate_planned_for_chapter(
                    plan.project_id,
                    origin_chapter_number=plan.chapter_number,
                )
                session.flush()
                inject("obligation")

                assert chapter is not None
                chapter.status = "accepted"
                chapter.acceptance_mode = plan.acceptance_mode
                chapter.repair_attempt_count = plan.repair_attempt_count
                chapter.residual_review_issues_json = json.dumps(
                    list(plan.residual_review_issues),
                    ensure_ascii=False,
                    sort_keys=True,
                )
                chapter.canon_risk_level = plan.canon_risk_level
                session.add(chapter)
                candidate_repository.transition(
                    plan.candidate_id,
                    "accepted",
                    canon_commit_id=commit_id,
                )
                session.flush()
                inject("chapter")

                for event in plan.audit_events:
                    session.add(
                        DecisionEvent(
                            id=new_id(),
                            project_id=plan.project_id,
                            chapter_number=plan.chapter_number,
                            scope=event.scope,
                            event_family=event.event_family,
                            event_type=event.event_type,
                            actor_type="system",
                            summary=event.summary,
                            reason=event.reason,
                            payload_json=json.dumps(
                                event.payload,
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                            related_object_type=event.related_object_type,
                            related_object_id=event.related_object_id,
                        )
                    )
                for event in plan.outbox_events:
                    enqueue_outbox_event(
                        session,
                        aggregate_type=event.aggregate_type,
                        aggregate_id=event.aggregate_id,
                        event_type=event.event_type,
                        payload=event.payload,
                        event_id=event.event_id,
                    )
                session.flush()
                inject("outbox")

                result_payload = {
                    "commit_id": commit_id,
                    "compile_result": compile_result.model_dump(mode="json"),
                }
                session.add(
                    CanonCommitRecord(
                        id=commit_id,
                        idempotency_key=plan.idempotency_key,
                        candidate_id=plan.candidate_id,
                        project_id=plan.project_id,
                        chapter_number=plan.chapter_number,
                        expected_previous_accepted_chapter=(
                            plan.expected_previous_accepted_chapter
                        ),
                        expected_book_state_chapter=(plan.expected_book_state_chapter),
                        graph_delta_ids_json=json.dumps(
                            compile_result.graph_delta_ids,
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        world_snapshot_id=compile_result.world_snapshot_id,
                        map_snapshot_id=compile_result.map_snapshot_id,
                        status="committed",
                        result_json=json.dumps(
                            result_payload,
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                    )
                )
                session.flush()
                return CanonAdmissionOutcome(
                    commit_id=commit_id,
                    compile_result=compile_result,
                )
        except CanonStaleVersion as exc:
            self._return_candidate_to_ready(plan.candidate_id)
            return CanonAdmissionOutcome(
                blocked_path=str(exc),
                block_kind="stale_canon_plan",
                stale=True,
                failure_reason=str(exc),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "Atomic Canon commit failed for chapter %d.",
                plan.chapter_number,
            )
            self._mark_candidate_failed(plan.candidate_id, str(exc))
            return CanonAdmissionOutcome(
                blocked_path=str(exc),
                block_kind="canon_write_failed",
                failure_reason=str(exc),
            )

    def _revalidate_locked_plan(
        self,
        *,
        session: Session,
        project: Project,
        chapter: ChapterPlan | None,
        candidate: CandidateDraftRecord | None,
        plan: CanonCommitPlan,
    ) -> None:
        if chapter is None:
            raise CanonStaleVersion("chapter plan no longer exists")
        if str(chapter.status or "") == "accepted":
            raise CanonStaleVersion("chapter is already accepted by another Canon commit")
        if candidate is None:
            raise CanonStaleVersion("candidate no longer exists")
        if candidate.project_id != plan.project_id:
            raise CanonStaleVersion("candidate project changed")
        if int(candidate.chapter_number or 0) != plan.chapter_number:
            raise CanonStaleVersion("candidate chapter changed")
        if candidate.status != "ready_for_canon":
            raise CanonStaleVersion(
                f"candidate is {candidate.status}, expected ready_for_canon"
            )
        if candidate.idempotency_key != plan.idempotency_key:
            raise CanonStaleVersion("candidate idempotency key changed")
        try:
            persisted_plan = CanonCommitPlan.model_validate_json(
                candidate.canon_commit_plan_json
            )
        except Exception as exc:  # noqa: BLE001
            raise CanonStaleVersion("candidate Canon plan is invalid") from exc
        if persisted_plan != plan:
            raise CanonStaleVersion("candidate Canon plan changed")
        if (
            candidate_writer_output_admission_fingerprint(candidate)
            != plan.entity_admission_plan.candidate_fingerprint
        ):
            raise CanonStaleVersion(
                "entity admission candidate fingerprint changed after preparation"
            )

        draft = session.execute(
            select(ChapterDraft)
            .where(ChapterDraft.id == candidate.candidate_draft_id)
            .with_for_update()
        ).scalar_one_or_none()
        if draft is None:
            raise CanonStaleVersion("candidate draft no longer exists")
        current_body_hash = candidate_body_hash(draft.body_text)
        if (
            current_body_hash != plan.candidate_body_hash
            or candidate.body_hash != plan.candidate_body_hash
        ):
            raise CanonStaleVersion("candidate body changed after preparation")
        current_plan_revision = candidate_plan_revision(chapter)
        if (
            current_plan_revision != plan.plan_revision
            or candidate.plan_revision != plan.plan_revision
        ):
            raise CanonStaleVersion("chapter plan changed after preparation")
        if (
            int(project.runtime_policy_version or 0) != plan.policy_version
            or int(candidate.policy_version or 0) != plan.policy_version
        ):
            raise CanonStaleVersion("runtime policy changed after preparation")

        previous_accepted_chapter = int(
            session.scalar(
                select(func.max(ChapterPlan.chapter_number)).where(
                    ChapterPlan.project_id == plan.project_id,
                    ChapterPlan.status == "accepted",
                    ChapterPlan.chapter_number < plan.chapter_number,
                )
            )
            or 0
        )
        if previous_accepted_chapter != plan.expected_previous_accepted_chapter:
            raise CanonStaleVersion("accepted chapter version changed")
        book_state_chapter = int(
            session.scalar(
                select(func.max(GraphDeltaRow.chapter_number)).where(
                    GraphDeltaRow.project_id == plan.project_id,
                    GraphDeltaRow.chapter_number < plan.chapter_number,
                )
            )
            or 0
        )
        if book_state_chapter != plan.expected_book_state_chapter:
            raise CanonStaleVersion("BookState version changed")
        current_chapter_delta_count = int(
            session.scalar(
                select(func.count(GraphDeltaRow.id)).where(
                    GraphDeltaRow.project_id == plan.project_id,
                    GraphDeltaRow.chapter_number == plan.chapter_number,
                )
            )
            or 0
        )
        if current_chapter_delta_count:
            raise CanonStaleVersion("BookState already contains this chapter")

    def commit_world_edit(
        self,
        *,
        session: Session,
        project_id: str,
        proposal_id: str,
        approved_changes: ApprovedGraphDeltaSet,
        reason: str,
        trigger: str,
    ) -> CanonWorldEditOutcome:
        project = session.execute(
            select(Project).where(Project.id == project_id).with_for_update()
        ).scalar_one_or_none()
        if project is None:
            raise CanonStaleVersion("project no longer exists")
        proposal = session.execute(
            select(KnowledgeEditProposalRow)
            .where(KnowledgeEditProposalRow.id == proposal_id)
            .with_for_update()
        ).scalar_one_or_none()
        if proposal is None or proposal.project_id != project_id:
            raise CanonStaleVersion("world edit proposal no longer exists")
        if proposal.status not in {"pending", "proposed"}:
            raise CanonStaleVersion(f"world edit proposal is already {proposal.status}")
        if approved_changes.project_id != project_id:
            raise CanonStaleVersion("world edit project changed")

        compile_result = BookStateCompiler(session).compile(
            approved_changes,
            compiler_run_id=f"canon-world-edit-{proposal.id}",
        )
        if not compile_result.committed:
            raise CanonWriteFailure(
                "; ".join(compile_result.blocked_reasons)
                or "BookState compiler rejected the world edit"
            )
        if compile_result.metadata.get("idempotent"):
            raise CanonStaleVersion(
                "world edit delta already exists without an accepted proposal"
            )

        proposal.status = "accepted"
        proposal.reviewed_at = datetime.now(UTC)
        proposal.review_reason = reason
        proposal.graph_delta_id = (
            compile_result.graph_delta_ids[0] if compile_result.graph_delta_ids else ""
        )
        session.add(proposal)
        event_id = (
            f"canon-world-edit:{proposal.id}:{proposal.graph_delta_id}:projection"
        )
        enqueue_outbox_event(
            session,
            aggregate_type="project",
            aggregate_id=project_id,
            event_type="knowledge.projection.refresh_requested",
            event_id=event_id,
            payload={
                "project_id": project_id,
                "projection_kind": "all",
                "as_of_chapter": compile_result.chapter_number,
                "trigger": trigger,
                "proposal_id": proposal.id,
            },
        )
        session.flush()
        return CanonWorldEditOutcome(
            compile_result=compile_result,
            outbox_event_id=event_id,
        )

    def _return_candidate_to_ready(self, candidate_id: str) -> None:
        if self.session_factory is None:
            return
        try:
            with self.session_factory.begin() as session:
                repository = CandidateDraftRepository(session)
                candidate = repository.get(candidate_id, for_update=True)
                if candidate is not None and candidate.status == "committing":
                    repository.transition(candidate.id, "ready_for_canon")
        except Exception:  # noqa: BLE001
            logger.exception("Could not restore stale candidate %s.", candidate_id)

    def _mark_candidate_failed(
        self,
        candidate_id: str,
        failure_reason: str,
    ) -> None:
        if self.session_factory is None:
            return
        try:
            with self.session_factory.begin() as session:
                repository = CandidateDraftRepository(session)
                candidate = repository.get(candidate_id, for_update=True)
                if candidate is None or candidate.status in {"accepted", "failed"}:
                    return
                if candidate.status == "committing":
                    candidate = repository.transition(
                        candidate.id,
                        "ready_for_canon",
                    )
                repository.transition(
                    candidate.id,
                    "failed",
                    failure_reason=failure_reason,
                )
        except Exception:  # noqa: BLE001
            logger.exception("Could not mark candidate %s failed.", candidate_id)


def _ignore_failure_stage(_stage: str) -> None:
    return None


def _outcome_from_record(
    record: CanonCommitRecord,
    *,
    idempotent: bool,
) -> CanonAdmissionOutcome:
    try:
        payload = json.loads(record.result_json or "{}")
    except (TypeError, json.JSONDecodeError):
        payload = {}
    compile_payload = payload.get("compile_result")
    compile_result = (
        BookStateCompileResult.model_validate(compile_payload)
        if isinstance(compile_payload, dict)
        else None
    )
    return CanonAdmissionOutcome(
        commit_id=record.id,
        idempotent=idempotent,
        compile_result=compile_result,
    )


__all__ = [
    "CanonAdmissionService",
    "CanonStaleVersion",
    "CanonWriteFailure",
]
