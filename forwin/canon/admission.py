from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from forwin.audit.events import DecisionEventType
from forwin.book_state.compiler import BookStateCompiler
from forwin.book_state.repository import BookStateRepository
from forwin.book_state.narrative import PLAN_CONSTRAINT_SOURCE_TYPE, PROTECTION_STATUS_PATH, RELEASED_PROTECTION_STATUS
from forwin.book_state.projection import BookStateProjection
from forwin.book_state.reviewer import BookStateReviewGate
from forwin.planning.beat_sheet.types import ProtectionSpec
from forwin.candidate_drafts import (
    CandidateDraftRepository,
    candidate_body_hash,
    candidate_plan_revision,
    candidate_writer_output_admission_fingerprint,
)
from forwin.models.base import new_id
from forwin.models.book_state import GraphDeltaRow, WorldSnapshotRow
from forwin.models.canon import CanonCommitRecord
from forwin.models.draft import CandidateDraftRecord, ChapterDraft
from forwin.models.audit import DecisionEvent
from forwin.models.knowledge import KnowledgeEditProposalRow
from forwin.models.maintenance import PostCanonMaintenanceRun
from forwin.models.project import ChapterPlan, Project
from forwin.maintenance.state import post_canon_barrier_ready
from forwin.narrative_obligations.repository import NarrativeObligationRepository
from forwin.outbox.store import enqueue_outbox_event
from forwin.protocol.book_state import ApprovedGraphDeltaSet, BookStateCompileResult, GraphDelta, NarrativePatch
from forwin.runtime.policy_store import ProjectPolicyStore
from forwin.world_engine.types import WORLD_ENGINE_SOURCE_TYPE

from .entity_admission import EntityAdmissionCommitter
from .historical_rewrite import HistoricalCanonRewriteService, HistoricalRewriteInvalid
from .plan import CanonCommitPlan
from .types import CanonAdmissionOutcome, CanonWorldEditOutcome, CanonWorldTickOutcome


logger = logging.getLogger(__name__)

GENESIS_SEED_SOURCE_TYPE = "genesis_seed"
# How BookStateCompiler reports a patch whose old_value no longer matches: a failed CAS.
_COMPILER_CAS_MISMATCH = "old_value mismatch"


class CanonStaleVersion(RuntimeError):
    """The prepared plan no longer matches locked authoritative state."""


class CanonWriteFailure(RuntimeError):
    """An authoritative Canon write could not be completed."""


@dataclass(frozen=True)
class CanonPlanConstraintOutcome:
    graph_delta_ids: tuple[str, ...]
    idempotent: bool


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
                    raise CanonStaleVersion(
                        "generation task lease lost before Canon commit"
                    )

                prior = session.execute(
                    select(CanonCommitRecord)
                    .where(CanonCommitRecord.idempotency_key == plan.idempotency_key)
                    .with_for_update()
                ).scalar_one_or_none()
                if prior is not None:
                    if prior.status == "superseded":
                        raise CanonStaleVersion("Canon commit has been superseded")
                    return _outcome_from_record(prior, idempotent=True)

                self._require_previous_post_canon_barrier(
                    session=session,
                    plan=plan,
                    band_checkpoint_action=(
                        ProjectPolicyStore(session)
                        .load(project)
                        .policy.pause.band_checkpoint_action
                    ),
                )
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
                rewrite = None
                try:
                    if chapter is not None and chapter.status != "accepted":
                        rewrite = HistoricalCanonRewriteService(
                            session
                        ).prepare_replacement(plan)
                except HistoricalRewriteInvalid as exc:
                    raise CanonStaleVersion(str(exc)) from exc
                if rewrite is not None:
                    rewrite.retire_old_contribution()
                self._revalidate_locked_plan(
                    session=session,
                    project=project,
                    chapter=chapter,
                    candidate=candidate,
                    plan=plan,
                    retained_chapter_delta_ids=frozenset(rewrite.retained_delta_ids)
                    if rewrite
                    else frozenset(),
                )

                commit_id = plan.canon_commit_id
                candidate_repository = CandidateDraftRepository(session)
                candidate_repository.transition(plan.candidate_id, "committing")

                # A chapter's contribution builds on the canon before it; world
                # edits retained at this chapter are replayed after it on rewrite.
                compile_result = BookStateCompiler(session).compile(
                    plan.approved_book_state_changes,
                    compiler_run_id=f"canon-commit-{commit_id}",
                    base_chapter=max(plan.chapter_number - 1, 0),
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
                if rewrite is not None:
                    try:
                        compile_result = rewrite.rebuild_successor_projections(
                            compile_result, compiler=BookStateCompiler(session)
                        )
                    except HistoricalRewriteInvalid as exc:
                        raise CanonStaleVersion(str(exc)) from exc
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
                if rewrite is not None:
                    rewrite.mark_prior_commit_superseded()
                    result_payload["historical_rewrite"] = {
                        "superseded_commit_id": rewrite.previous.id,
                        "retired_delta_ids": rewrite.retired_delta_ids,
                        "replayed_chapters": rewrite.replayed_chapters,
                        # The world step expires these ticks' open conflicts and ticks the chapter again.
                        "retired_tick_ids": rewrite.retired_tick_ids,
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

    @staticmethod
    def _require_previous_post_canon_barrier(
        *,
        session: Session,
        plan: CanonCommitPlan,
        band_checkpoint_action: str = "pause_on_warn",
    ) -> None:
        if int(plan.chapter_number or 0) <= 1:
            return
        previous_chapter = int(plan.chapter_number) - 1
        previous = session.execute(
            select(CanonCommitRecord).where(
                CanonCommitRecord.project_id == plan.project_id,
                CanonCommitRecord.chapter_number == previous_chapter,
                CanonCommitRecord.status == "committed",
            )
        ).scalar_one_or_none()
        if previous is None:
            raise CanonStaleVersion(
                f"previous Canon chapter {previous_chapter} is not committed"
            )
        runs = list(
            session.execute(
                select(PostCanonMaintenanceRun).where(
                    PostCanonMaintenanceRun.canon_commit_id == previous.id
                )
            ).scalars()
        )
        if not post_canon_barrier_ready(
            runs,
            session=session,
            band_checkpoint_action=band_checkpoint_action,
        ):
            raise CanonStaleVersion(
                f"previous Canon chapter {previous_chapter} has an incomplete "
                "post-Canon maintenance/order-control barrier"
            )

    def _revalidate_locked_plan(
        self,
        *,
        session: Session,
        project: Project,
        chapter: ChapterPlan | None,
        candidate: CandidateDraftRecord | None,
        plan: CanonCommitPlan,
        retained_chapter_delta_ids: frozenset[str] = frozenset(),
    ) -> None:
        if chapter is None:
            raise CanonStaleVersion("chapter plan no longer exists")
        if str(chapter.status or "") == "accepted":
            raise CanonStaleVersion(
                "chapter is already accepted by another Canon commit"
            )
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
        current_chapter_delta_ids = set(
            session.scalars(
                select(GraphDeltaRow.id).where(
                    GraphDeltaRow.project_id == plan.project_id,
                    GraphDeltaRow.chapter_number == plan.chapter_number,
                )
            )
        )
        if current_chapter_delta_ids != retained_chapter_delta_ids:
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
        # The caller picked the chapter before this lock; a chapter accepted since
        # would leave the edit out of its snapshot.
        latest_chapter = BookStateRepository(session).latest_canon_chapter(project_id)
        if latest_chapter is not None and approved_changes.chapter_number < latest_chapter:
            raise CanonStaleVersion(
                f"world edit targets chapter {approved_changes.chapter_number} "
                f"but BookState is at chapter {latest_chapter}"
            )

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

    def commit_world_seed(
        self,
        *,
        session: Session,
        project_id: str,
        revision_id: str,
        approved_changes: ApprovedGraphDeltaSet,
    ) -> BookStateCompileResult:
        """Compile a Genesis world seed as chapter 0; the caller owns the transaction."""
        # FOR NO KEY UPDATE still serializes canon writers, but lets other sessions insert
        # rows referencing the project; start-writing takes the same lock before its handoff.
        project = session.execute(
            select(Project).where(Project.id == project_id).with_for_update(key_share=True)
        ).scalar_one_or_none()
        if project is None:
            raise CanonStaleVersion("project no longer exists")
        if approved_changes.project_id != project_id:
            raise CanonStaleVersion("world seed project changed")
        if approved_changes.chapter_number != 0:
            raise ValueError("world seed must compile into chapter 0")
        if any(
            delta.source_type != GENESIS_SEED_SOURCE_TYPE
            for delta in approved_changes.graph_deltas
        ):
            raise ValueError(f"world seed deltas must have source_type {GENESIS_SEED_SOURCE_TYPE!r}")
        non_seed_delta_id = session.scalar(
            select(GraphDeltaRow.id)
            .where(
                GraphDeltaRow.project_id == project_id,
                GraphDeltaRow.source_type != GENESIS_SEED_SOURCE_TYPE,
            )
            .limit(1)
        )
        latest_chapter = BookStateRepository(session).latest_canon_chapter(project_id)
        if non_seed_delta_id is not None or (latest_chapter or 0) > 0:
            raise CanonStaleVersion("BookState already holds canon beyond the world seed")

        compile_result = BookStateCompiler(session).compile(
            approved_changes,
            compiler_run_id=f"genesis-seed-{revision_id}",
        )
        if not compile_result.committed:
            raise CanonWriteFailure(
                "; ".join(compile_result.blocked_reasons)
                or "BookState compiler rejected the world seed"
            )
        if compile_result.metadata.get("idempotent"):
            return compile_result

        enqueue_outbox_event(
            session,
            aggregate_type="project",
            aggregate_id=project_id,
            event_type="knowledge.projection.refresh_requested",
            event_id=f"genesis-seed:{project_id}:projection",
            payload={
                "project_id": project_id,
                "projection_kind": "all",
                "as_of_chapter": compile_result.chapter_number,
                "trigger": GENESIS_SEED_SOURCE_TYPE,
                "revision_id": revision_id,
            },
        )
        session.flush()
        return compile_result

    def commit_plan_constraints(
        self, *, session: Session, project_id: str, constraint_set_id: str | None = None,
        creates: list[ProtectionSpec], releases: list[str], release_sheet_id: str | None = None,
        beat_sheet_id: str | None = None,
    ) -> CanonPlanConstraintOutcome:
        """Commit a constraint set's protections and releases after the latest canon delta, idempotently by
        ``constraint_set_id``; the caller owns the transaction.

        The id is a beat sheet's id, or ``conflict:<id>`` for a chapter conflict a user settled by amending the
        plan. ``beat_sheet_id`` is the same parameter under its earlier name. The ledger stores the id under the
        delta's and protection nodes' ``beat_sheet_id`` metadata key.
        """
        if (constraint_set_id is None) == (beat_sheet_id is None):
            raise TypeError("commit_plan_constraints takes exactly one of constraint_set_id and beat_sheet_id")
        constraint_set_id = constraint_set_id if constraint_set_id is not None else beat_sheet_id
        project = session.scalar(select(Project).where(Project.id == project_id).with_for_update(key_share=True))
        if project is None:
            raise CanonStaleVersion("project no longer exists")
        existing = tuple(session.scalars(select(GraphDeltaRow.id).where(
            GraphDeltaRow.project_id == project_id, GraphDeltaRow.source_type == PLAN_CONSTRAINT_SOURCE_TYPE,
            GraphDeltaRow.beat_sheet_id == constraint_set_id,
        ).order_by(GraphDeltaRow.created_at, GraphDeltaRow.id)))
        if existing:
            return CanonPlanConstraintOutcome(existing, True)
        chapter = BookStateRepository(session).latest_canon_chapter(project_id) or 0
        runtime = BookStateProjection(session).load_runtime_as_of(project_id, as_of_chapter=chapter)
        releases = list(releases)
        if release_sheet_id is not None:
            releases.extend(node.id for node in runtime.narrative.nodes_by_id.values()
                            if node.node_type == "review_constraint"
                            and node.metadata.get("beat_sheet_id") == release_sheet_id
                            and node.payload.get("status") != RELEASED_PROTECTION_STATUS)
        patches = []
        created_ids, released_ids = [], []
        for ref in dict.fromkeys(releases):
            node = runtime.narrative.nodes_by_id.get(ref)
            if node is None or node.node_type != "review_constraint":
                raise CanonWriteFailure(f"protection {ref!r} is not a review_constraint node")
            released_ids.append(ref)
            patches.append(NarrativePatch(target_ref=f"review_constraint:{ref}", op="set",
                field_path=PROTECTION_STATUS_PATH, old_value=node.payload.get("status"),
                new_value=RELEASED_PROTECTION_STATUS))
        for spec in creates:
            ref = new_id()
            created_ids.append(ref)
            payload = {"ref": spec.ref, "kind": spec.kind, "until_chapter": spec.until_chapter, "source": "beat_sheet"}
            if spec.kind == "must_not_know":
                payload.update(observer_ref=spec.observer_ref, fact_ref=spec.fact_ref)
            if spec.kind == "must_hold_state":
                payload.update(field=spec.field, value=spec.value)
            patches.append(NarrativePatch(target_ref=f"review_constraint:{ref}", op="create", new_value={
                "id": ref, "project_id": project_id, "node_type": "review_constraint", "payload": payload,
                "metadata": {"beat_sheet_id": constraint_set_id},
            }))
        delta = GraphDelta(id=new_id(), project_id=project_id, chapter_number=chapter,
            source_type=PLAN_CONSTRAINT_SOURCE_TYPE, source_id=constraint_set_id, narrative_patches=patches,
            metadata={"beat_sheet_id": constraint_set_id})
        changes = ApprovedGraphDeltaSet(project_id=project_id, chapter_number=chapter,
                                       graph_deltas=[delta], approved_by=[PLAN_CONSTRAINT_SOURCE_TYPE])
        verdict = BookStateReviewGate(session).review(changes, base_chapter=chapter)
        if not verdict.accepted:
            raise CanonWriteFailure("; ".join(issue.message for issue in verdict.issues))
        result = BookStateCompiler(session).compile(
            changes, compiler_run_id=f"canon-plan-constraints-{constraint_set_id}", base_chapter=chapter,
        )
        if not result.committed:
            raise CanonWriteFailure("; ".join(result.blocked_reasons))
        # PostgreSQL now() is transaction-stable. Preserve order after the seed and after any
        # earlier sheet in this transaction, both in the ledger and the narrative snapshot.
        last_delta_time = session.scalar(select(func.max(GraphDeltaRow.created_at)).where(
            GraphDeltaRow.project_id == project_id, GraphDeltaRow.chapter_number == chapter,
        ))
        session.get(GraphDeltaRow, delta.id).created_at = last_delta_time + timedelta(microseconds=1)
        last_snapshot_time = session.scalar(select(func.max(WorldSnapshotRow.built_at)).where(
            WorldSnapshotRow.project_id == project_id, WorldSnapshotRow.as_of_chapter == chapter,
        ))
        session.get(WorldSnapshotRow, result.world_snapshot_id).built_at = last_snapshot_time + timedelta(microseconds=1)
        session.flush()
        session.add(DecisionEvent(
            id=new_id(), project_id=project_id, chapter_number=chapter, scope="chapter",
            event_family="business_event", event_type=DecisionEventType.PLAN_CONSTRAINTS_COMMITTED,
            actor_type="system", summary=f"Plan constraints {constraint_set_id} committed into chapter {chapter}.",
            payload_json=json.dumps({"beat_sheet_id": constraint_set_id, "graph_delta_ids": list(result.graph_delta_ids),
                                     "created_ids": created_ids, "released_ids": released_ids,
                                     "chapter_number": chapter}, ensure_ascii=False, sort_keys=True),
            related_object_type="beat_sheet", related_object_id=constraint_set_id,
        ))
        session.flush()
        return CanonPlanConstraintOutcome(tuple(result.graph_delta_ids), False)

    def commit_world_tick(
        self,
        *,
        session: Session,
        project_id: str,
        chapter_number: int,
        approved_changes: ApprovedGraphDeltaSet,
        tick_id: str,
    ) -> CanonWorldTickOutcome:
        """Compile a world tick into the latest canon chapter; the caller owns the transaction.

        The tick extends the chapter it ran after, so it lands after that chapter's own delta.
        """
        # FOR NO KEY UPDATE, as the world seed takes: serializes canon writers.
        project = session.execute(
            select(Project).where(Project.id == project_id).with_for_update(key_share=True)
        ).scalar_one_or_none()
        if project is None:
            raise CanonStaleVersion("project no longer exists")
        if approved_changes.project_id != project_id or approved_changes.chapter_number != chapter_number:
            raise CanonWriteFailure(
                f"world tick changes target project {approved_changes.project_id} chapter "
                f"{approved_changes.chapter_number}, not project {project_id} chapter {chapter_number}"
            )
        latest_chapter = BookStateRepository(session).latest_canon_chapter(project_id)
        if chapter_number != latest_chapter:
            raise CanonWriteFailure(
                f"world tick targets chapter {chapter_number} but BookState is at chapter {latest_chapter}"
            )
        if not approved_changes.graph_deltas:
            raise CanonWriteFailure("world tick has no deltas to commit")
        for delta in approved_changes.graph_deltas:
            if delta.source_type != WORLD_ENGINE_SOURCE_TYPE:
                raise CanonWriteFailure(
                    f"world tick delta {delta.id} has source_type {delta.source_type!r}, "
                    f"expected {WORLD_ENGINE_SOURCE_TYPE!r}"
                )
            # The tick id in metadata is what makes a retry find this commit.
            if delta.metadata.get("tick_id") != tick_id:
                raise CanonWriteFailure(f"world tick delta {delta.id} belongs to another tick")
        committed_ids = _world_tick_delta_ids(session, project_id, tick_id)
        if committed_ids:
            return CanonWorldTickOutcome(graph_delta_ids=committed_ids, idempotent=True)

        compile_result = BookStateCompiler(session).compile(
            approved_changes,
            compiler_run_id=f"canon-world-tick-{tick_id}",
            base_chapter=chapter_number,
        )
        if not compile_result.committed:
            reasons = "; ".join(compile_result.blocked_reasons) or "BookState compiler rejected the world tick"
            if any(_COMPILER_CAS_MISMATCH in reason for reason in compile_result.blocked_reasons):
                raise CanonStaleVersion(reasons)
            raise CanonWriteFailure(reasons)
        if compile_result.metadata.get("idempotent"):
            raise CanonStaleVersion("world tick delta already exists under another tick")

        graph_delta_ids = tuple(compile_result.graph_delta_ids)
        session.add(
            DecisionEvent(
                id=new_id(),
                project_id=project_id,
                chapter_number=chapter_number,
                scope="chapter",
                event_family="business_event",
                event_type=DecisionEventType.WORLD_TICK_COMMITTED,
                actor_type="system",
                summary=(
                    f"World tick {tick_id} committed {len(graph_delta_ids)} BookState "
                    f"delta(s) into chapter {chapter_number}."
                ),
                payload_json=json.dumps(
                    {"tick_id": tick_id, "graph_delta_ids": list(graph_delta_ids)},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                related_object_type="world_tick",
                related_object_id=tick_id,
            )
        )
        for delta_id in graph_delta_ids:
            enqueue_outbox_event(
                session,
                aggregate_type="project",
                aggregate_id=project_id,
                event_type="knowledge.projection.refresh_requested",
                event_id=f"canon-world-tick:{tick_id}:{delta_id}:projection",
                payload={
                    "project_id": project_id,
                    "projection_kind": "all",
                    "as_of_chapter": compile_result.chapter_number,
                    "trigger": WORLD_ENGINE_SOURCE_TYPE,
                    "tick_id": tick_id,
                },
            )
        session.flush()
        return CanonWorldTickOutcome(graph_delta_ids=graph_delta_ids, idempotent=False)

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


def _world_tick_delta_ids(session: Session, project_id: str, tick_id: str) -> tuple[str, ...]:
    """Ids of the world_engine deltas a tick committed, in commit order."""
    return tuple(session.scalars(
        select(GraphDeltaRow.id).where(
            GraphDeltaRow.project_id == project_id,
            GraphDeltaRow.source_type == WORLD_ENGINE_SOURCE_TYPE,
            GraphDeltaRow.tick_id == tick_id,
        ).order_by(GraphDeltaRow.created_at, GraphDeltaRow.id)
    ))


def _json_object(raw: str | None) -> dict:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


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
    "CanonPlanConstraintOutcome",
    "CanonStaleVersion",
    "CanonWriteFailure",
    "GENESIS_SEED_SOURCE_TYPE",
]
