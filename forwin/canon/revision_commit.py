"""Atomic historical branch of CanonAdmissionService; no other acceptance owner."""

from __future__ import annotations

import json

from sqlalchemy import delete, select, update

from forwin.book_state.compiler import BookStateCompiler
from forwin.models.base import Base
from forwin.models.canon import CanonCommitRecord, CanonRevisionValidationRecord
from forwin.models.draft import CandidateDraftRecord, ChapterDraft
from forwin.models.project import ChapterPlan, Project
from forwin.outbox import enqueue_outbox_event
from forwin.publisher_runtime.protection import (
    lock_project_chapters,
    require_revision_unprotected,
)

from .entity_admission import EntityAdmissionCommitter
from .revision_replica import CandidateReplica, capture_revision
from .revision_service import revision_commit_plan, revision_policy_fingerprint
from .revision_validation import (
    RevisionValidationResult,
    assess_revision,
    revision_digest,
)
from .types import CanonAdmissionOutcome


def _load_result(session, record):
    payload = json.loads(record.result_json)
    for chapter in payload["manifest"]["chapters"]:
        draft = session.get(ChapterDraft, chapter["draft_id"])
        if draft is None:
            raise ValueError("validated draft is missing")
        chapter["body"] = draft.body_text
    result = RevisionValidationResult.model_validate(payload)
    assessed = assess_revision(
        result.manifest, result.chapters, uncovered=result.uncovered
    )
    if (
        assessed != result
        or result.validation_id != record.id
        or record.status != "pass"
    ):
        raise ValueError("historical validation identity or complete coverage changed")
    if not all(c.prepared_changes for c in result.chapters):
        raise ValueError("historical validation lacks freshly extracted changes")
    return result


def commit_revision(owner, plan, *, model_identity, failure_injector):
    from .admission import _outcome_from_record

    try:
        with owner.session_factory.begin() as session:
            lock_project_chapters(session, plan.project_id)
            project = session.get(Project, plan.project_id)
            record = session.scalar(
                select(CanonRevisionValidationRecord)
                .where(CanonRevisionValidationRecord.id == plan.revision_validation_id)
                .with_for_update()
            )
            if (
                project is None
                or record is None
                or record.project_id != project.id
                or record.candidate_id != plan.candidate_id
            ):
                raise ValueError("historical validation ownership missing")
            result = _load_result(session, record)
            plans = [
                revision_commit_plan(session, result, i)
                for i in range(len(result.chapters))
            ]
            if plan != plans[0]:
                raise ValueError("historical plan differs from durable validation")
            if record.accepted_book_revision is not None:
                if not all(
                    session.get(ChapterPlan, p.chapter_plan_id).active_commit_id
                    == prepared.canon_commit_id
                    for p, prepared in zip(result.manifest.chapters, plans)
                ):
                    raise ValueError("historical acceptance was superseded")
                return _outcome_from_record(
                    session.get(CanonCommitRecord, plan.canon_commit_id),
                    idempotent=True,
                )
            if owner.transaction_guard is not None and not owner.transaction_guard(
                session
            ):
                raise ValueError("generation lease lost before historical acceptance")
            if model_identity != result.manifest.model_identity:
                raise ValueError(
                    "historical validation model configuration changed or unavailable"
                )
            if project.book_revision != result.manifest.base_book_revision:
                raise ValueError("historical validation book revision is stale")
            require_revision_unprotected(
                session,
                project_id=project.id,
                from_chapter=result.manifest.from_chapter,
            )
            candidate = session.get(CandidateDraftRecord, plan.candidate_id)
            review = json.loads(candidate.review_result_json or "{}")
            if (
                candidate.status != "reviewed"
                or review.get("revision_validation_id") != record.id
                or review.get("verdict") != "pass"
            ):
                raise ValueError("historical candidate lacks its completed review")
            current = capture_revision(
                session,
                project_id=project.id,
                candidate_id=candidate.id,
                model_identity=model_identity,
                policy_fingerprint=revision_policy_fingerprint(project),
            )
            if current.manifest != result.manifest:
                raise ValueError("historical baseline identities or policy changed")
        # Re-materialize only the already reviewed fresh changes in scratch;
        # no LLM calls or acceptance records are created in this database.
        with CandidateReplica(current) as replica:
            compiled = []
            for prepared in plans:
                item = BookStateCompiler(replica.session).compile(
                    prepared.approved_book_state_changes,
                    compiler_run_id=f"canon-commit-{prepared.canon_commit_id}",
                    acceptance_identity=prepared.canon_commit_id,
                )
                if not item.committed:
                    raise ValueError(
                        "validated projection no longer compiles: "
                        + "; ".join(item.blocked_reasons)
                    )
                EntityAdmissionCommitter(replica.session).apply(
                    project_id=project.id,
                    plan=prepared.entity_admission_plan,
                    acceptance_id=prepared.canon_commit_id,
                )
                from forwin.narrative_obligations.repository import (
                    NarrativeObligationRepository,
                )

                chapter = result.manifest.chapters[
                    prepared.chapter_number - result.manifest.from_chapter
                ]
                form = result.chapters[
                    prepared.chapter_number - result.manifest.from_chapter
                ].prepared_changes["historical_form"]
                obligations = NarrativeObligationRepository(replica.session)
                obligations.set_candidate_acceptance(
                    project_id=plan.project_id,
                    chapter_number=chapter.chapter_number,
                    acceptance_id=prepared.canon_commit_id,
                    draft_id=chapter.draft_id,
                )
                obligations.apply_reviewed_resolutions(
                    project_id=plan.project_id,
                    chapter_number=chapter.chapter_number,
                    chapter_body=chapter.body,
                    historical_form=form,
                )
                obligations.activate_planned_for_chapter(
                    plan.project_id,
                    origin_chapter_number=chapter.chapter_number,
                    acceptance_id=prepared.canon_commit_id,
                    draft_id=chapter.draft_id,
                )
                replica.session.flush()
                compiled.append(item)
            failure_injector("materialized")
            with owner.session_factory.begin() as session:
                lock_project_chapters(session, plan.project_id)
                project = session.get(Project, plan.project_id)
                record = session.get(
                    CanonRevisionValidationRecord, plan.revision_validation_id
                )
                candidate = session.get(CandidateDraftRecord, plan.candidate_id)
                if (
                    record is None
                    or record.accepted_book_revision is not None
                    or _load_result(session, record) != result
                ):
                    raise ValueError(
                        "historical validation changed while materializing"
                    )
                if owner.transaction_guard is not None and not owner.transaction_guard(
                    session
                ):
                    raise ValueError(
                        "generation lease lost before historical acceptance"
                    )
                if project.book_revision != result.manifest.base_book_revision:
                    raise ValueError("historical validation book revision is stale")
                require_revision_unprotected(
                    session,
                    project_id=project.id,
                    from_chapter=result.manifest.from_chapter,
                )
                review = json.loads(candidate.review_result_json or "{}")
                if (
                    candidate.status != "reviewed"
                    or review.get("revision_validation_id") != record.id
                    or review.get("verdict") != "pass"
                ):
                    raise ValueError(
                        "historical candidate review changed while materializing"
                    )
                final_snapshot = capture_revision(
                    session,
                    project_id=project.id,
                    candidate_id=candidate.id,
                    model_identity=model_identity,
                    policy_fingerprint=revision_policy_fingerprint(project),
                )
                if final_snapshot.manifest != result.manifest:
                    raise ValueError("historical baseline changed while materializing")
                _copy_materialization(session, replica.session, current, project.id)
                failure_injector("book_state")
                failure_injector("entity")
                for chapter, prepared, item in zip(
                    result.manifest.chapters, plans, compiled
                ):
                    stable = session.get(ChapterPlan, chapter.chapter_plan_id)
                    previous = session.get(CanonCommitRecord, chapter.base_commit_id)
                    commit = CanonCommitRecord(
                        id=prepared.canon_commit_id,
                        idempotency_key=prepared.idempotency_key,
                        candidate_id=prepared.candidate_id,
                        project_id=project.id,
                        chapter_number=chapter.chapter_number,
                        chapter_plan_id=stable.id,
                        chapter_title=chapter.title,
                        acceptance_revision=previous.acceptance_revision + 1,
                        base_book_revision=result.manifest.base_book_revision,
                        production_mode=previous.production_mode,
                        expected_previous_accepted_chapter=chapter.chapter_number - 1,
                        expected_book_state_chapter=chapter.chapter_number - 1,
                        graph_delta_ids_json=json.dumps(item.graph_delta_ids),
                        world_snapshot_id=item.world_snapshot_id,
                        map_snapshot_id=item.map_snapshot_id,
                        status="committed",
                        result_json=json.dumps(
                            {
                                "commit_id": prepared.canon_commit_id,
                                "compile_result": item.model_dump(mode="json"),
                                "revision_validation_id": record.id,
                                "entity_plan_sha256": revision_digest(
                                    prepared.entity_admission_plan.model_dump(
                                        mode="json"
                                    )
                                ),
                            },
                            sort_keys=True,
                        ),
                    )
                    session.add(commit)
                    session.flush()
                    from forwin.canon_quality.repository import CanonQualityRepository

                    CanonQualityRepository(session).bind_acceptance(
                        project_id=project.id,
                        chapter_number=chapter.chapter_number,
                        draft_id=chapter.draft_id,
                        acceptance_id=commit.id,
                        historical_form=result.chapters[
                            chapter.chapter_number - result.manifest.from_chapter
                        ].prepared_changes["historical_form"],
                    )
                    stable.active_commit_id = commit.id
                    stable.status = "accepted"
                    stable.title = chapter.title
                    for event in prepared.outbox_events:
                        enqueue_outbox_event(
                            session,
                            aggregate_type=event.aggregate_type,
                            aggregate_id=event.aggregate_id,
                            event_type=event.event_type,
                            payload=event.payload,
                            event_id=event.event_id,
                        )
                NarrativeObligationRepository(session).install_candidate_projection(
                    source_session=replica.session,
                    project_id=project.id,
                    acceptance_ids={p.chapter_number: p.canon_commit_id for p in plans},
                )
                failure_injector("obligation")
                failure_injector("outbox")
                candidate.status = "accepted"
                candidate.canon_status = "canon"
                candidate.canon_commit_id = plans[0].canon_commit_id
                candidate.canon_commit_plan_json = plans[0].model_dump_json()
                project.book_revision += 1
                from forwin.novel_export.events import enqueue_book_export

                enqueue_book_export(session, project)
                record.accepted_book_revision = project.book_revision
                session.flush()
                failure_injector("chapter")
                return CanonAdmissionOutcome(
                    commit_id=plan.canon_commit_id, compile_result=compiled[0]
                )
    except Exception as exc:  # noqa: BLE001 — owner boundary preserves evidence and rejects incomplete validation.
        # Evidence and the accepted mainline survive any failure; proposal can be
        # re-reviewed against a fresh baseline, never demote the active chapter.
        return CanonAdmissionOutcome(
            blocked_path=str(exc),
            block_kind="revision_commit_blocked",
            failure_reason=str(exc),
        )


# Mutable current projections are replaceable; immutable evidence below is only
# appended. Rows missing from the new current graph are retired or removed.
_PROJECTIONS = (
    "world_nodes",
    "world_edges",
    "fact_nodes",
    "map_nodes",
    "map_edges",
    "narrative_nodes",
    "narrative_edges",
    "book_reader_promises",
    "entities",
    "entity_aliases",
    "sub_world_roster_items",
)
_APPEND = (
    "graph_deltas",
    "graph_delta_patches",
    "world_node_states",
    "world_snapshots",
    "map_snapshots",
    "cognition_overlays",
    "cognition_overlay_patches",
    "book_cognition_snapshots",
    "book_reader_experience_deltas",
    "decision_events",
)


def _copy_materialization(destination, scratch, snapshot, project_id):
    for name in _PROJECTIONS:
        table = Base.metadata.tables[name]
        fresh = {
            r["id"]: dict(r)
            for r in scratch.execute(
                select(table).where(table.c.project_id == project_id)
            ).mappings()
        }
        existing = {
            r["id"]: dict(r)
            for r in destination.execute(
                select(table).where(table.c.project_id == project_id)
            ).mappings()
        }
        for row_id, row in existing.items():
            if row_id not in fresh:
                # These are current projections; immutable old deltas/states/
                # snapshots retain history. A deactivated graph node is still
                # a real node and cannot stand in for an absent revision node.
                if name == "entities":
                    destination.execute(
                        update(table)
                        .where(table.c.id == row_id)
                        .values(is_active=False)
                    )
                else:
                    destination.execute(delete(table).where(table.c.id == row_id))
        for row_id, row in fresh.items():
            if row_id in existing:
                if row != existing[row_id]:
                    destination.execute(
                        update(table).where(table.c.id == row_id).values(**row)
                    )
            else:
                destination.execute(table.insert().values(**row))
    for name in _APPEND:
        table = Base.metadata.tables[name]
        before_ids = {r["id"] for r in snapshot.tables.get(name, ())}
        for row in scratch.execute(
            select(table).where(table.c.project_id == project_id)
        ).mappings():
            if (
                name == "decision_events"
                and row["event_type"] == "canon_obligation_before_image"
            ):
                continue
            if row["id"] not in before_ids:
                destination.execute(table.insert().values(**dict(row)))
    destination.flush()
