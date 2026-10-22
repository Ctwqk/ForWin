from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from forwin.audit.events import DecisionEventType
from forwin.book_state.compiler import BookStateCompiler
from forwin.book_state.knowledge import FACT_REF_PREFIX
from forwin.book_state.repository import BookStateRepository, _dump, _loads
from forwin.book_state.runtime import BookStateRuntime
from forwin.book_state.schema import IMPACT_LEVELS
from forwin.models.audit import DecisionEvent
from forwin.models.book_state import (
    BookCognitionSnapshotRow,
    GraphDeltaRow,
    MapSnapshotRow,
    WorldSnapshotRow,
)
from forwin.models.canon import CanonCommitRecord
from forwin.models.knowledge import KnowledgeEditProposalRow
from forwin.models.maintenance import PostCanonMaintenanceRun
from forwin.models.project import ChapterPlan
from forwin.models.world_conflict import WorldPlanConflictRow
from forwin.protocol.book_state import (
    BookStateCompileResult,
    CognitionPatch,
    EdgePatch,
    FactPatch,
    GraphDelta,
    MapPatch,
    NarrativePatch,
    NodePatch,
)
from forwin.world_engine.conflict_repository import WorldConflictRepository
from forwin.world_engine.conflict_types import ConflictDraft
from forwin.world_engine.types import WORLD_ENGINE_SOURCE_TYPE

from .plan import CanonCommitPlan


class HistoricalRewriteInvalid(ValueError):
    """The retry audit trail does not authorize replacement of this commit."""


def _payload(event: DecisionEvent) -> dict:
    try:
        value = json.loads(event.payload_json or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _delta_manifest(value: object) -> list[str]:
    if (
        not isinstance(value, list)
        or any(not isinstance(item, str) or not item.strip() for item in value)
        or len(set(value)) != len(value)
    ):
        raise HistoricalRewriteInvalid(
            "historical rewrite delta manifest missing or invalid"
        )
    return value


def _string_items(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _highest_candidate_impact(delta: GraphDelta) -> str:
    """The highest impact among the tick's candidates; "moderate" without any."""
    impacts = delta.metadata.get("candidate_impacts")
    ranks = (
        [
            IMPACT_LEVELS.index(level)
            for level in impacts.values()
            if level in IMPACT_LEVELS
        ]
        if isinstance(impacts, dict)
        else []
    )
    return IMPACT_LEVELS[max(ranks)] if ranks else "moderate"


def _patch_ref(patch: object) -> str:
    """The BookState ref a patch targets."""
    if isinstance(patch, NodePatch):
        return patch.node_id
    if isinstance(patch, EdgePatch):
        return patch.edge_id
    if isinstance(patch, FactPatch):
        return f"{FACT_REF_PREFIX}{patch.fact_id}"
    if isinstance(patch, MapPatch):
        return patch.target_id
    if isinstance(patch, CognitionPatch):
        return patch.observer_id
    if isinstance(patch, NarrativePatch):
        return patch.target_ref
    return ""


class HistoricalCanonRewriteRepository:
    """Historical rewrite authorization lives in the existing API retry event."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def committed_records(
        self, project_id: str, chapter_number: int
    ) -> list[CanonCommitRecord]:
        return list(
            self.session.scalars(
                select(CanonCommitRecord)
                .where(
                    CanonCommitRecord.project_id == project_id,
                    CanonCommitRecord.chapter_number == chapter_number,
                    CanonCommitRecord.status == "committed",
                )
                .with_for_update()
            )
        )

    def mark_pending(
        self, *, project_id: str, chapter_number: int, reason: str
    ) -> DecisionEvent:
        records = self.committed_records(project_id, chapter_number)
        if len(records) != 1:
            raise HistoricalRewriteInvalid(
                "historical rewrite marker missing or invalid"
            )
        event = DecisionEvent(
            project_id=project_id,
            chapter_number=chapter_number,
            event_family="audit_action",
            event_type=DecisionEventType.RETRY_ATTEMPT,
            actor_type="api",
            scope="chapter",
            summary=f"第{chapter_number}章 review 候选已重置为 planned，等待重写。",
            reason=reason,
            payload_json=json.dumps(
                {
                    "chapter_number": chapter_number,
                    "previous_status": "accepted",
                    "previous_commit_id": records[0].id,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            related_object_type="chapter",
        )
        self.session.add(event)
        self.session.flush()
        return event

    def validate_contribution(
        self, previous: CanonCommitRecord
    ) -> tuple[list[str], list[str]]:
        """Validate ownership before backfilling a marker or retiring any rows."""
        try:
            delta_ids = _delta_manifest(json.loads(previous.graph_delta_ids_json))
            result = json.loads(previous.result_json)
            compiled_ids = _delta_manifest(result["compile_result"]["graph_delta_ids"])
        except (ValueError, TypeError, KeyError) as exc:
            raise HistoricalRewriteInvalid(
                "historical rewrite delta manifest missing or invalid"
            ) from exc
        if delta_ids != compiled_ids:
            raise HistoricalRewriteInvalid(
                "historical rewrite delta manifest missing or invalid"
            )
        chapter_rows = self.session.execute(
            select(GraphDeltaRow.id, GraphDeltaRow.source_type)
            .where(
                GraphDeltaRow.project_id == previous.project_id,
                GraphDeltaRow.chapter_number == previous.chapter_number,
            )
            .order_by(GraphDeltaRow.created_at, GraphDeltaRow.id)
            .with_for_update()
        ).all()
        chapter_ids = [delta_id for delta_id, _ in chapter_rows]
        owned_ids = set(delta_ids)
        if not owned_ids.issubset(chapter_ids):
            raise HistoricalRewriteInvalid(
                "historical rewrite delta manifest missing or invalid"
            )
        retained_ids = [
            delta_id for delta_id in chapter_ids if delta_id not in owned_ids
        ]
        # A retained delta is an accepted world edit or a world tick's delta.
        tick_ids = {
            delta_id
            for delta_id, source_type in chapter_rows
            if delta_id not in owned_ids and source_type == WORLD_ENGINE_SOURCE_TYPE
        }
        edit_ids = set(retained_ids) - tick_ids
        if edit_ids:
            world_edit_ids = set(
                self.session.scalars(
                    select(KnowledgeEditProposalRow.graph_delta_id).where(
                        KnowledgeEditProposalRow.project_id == previous.project_id,
                        KnowledgeEditProposalRow.status == "accepted",
                        KnowledgeEditProposalRow.graph_delta_id.in_(edit_ids),
                    )
                )
            )
            if world_edit_ids != edit_ids:
                raise HistoricalRewriteInvalid(
                    "historical rewrite delta manifest missing or invalid"
                )
        if tick_ids and self._unlisted_world_tick_deltas(previous, tick_ids):
            raise HistoricalRewriteInvalid(
                "historical rewrite delta manifest missing or invalid"
            )
        return delta_ids, retained_ids

    def _unlisted_world_tick_deltas(
        self, record: CanonCommitRecord, tick_ids: set[str]
    ) -> set[str]:
        """Tick deltas that no chapter world step or decided conflict lists."""
        unlisted = set(tick_ids)
        # Every world step of the chapter counts: a retained tick ran after the
        # commit that an earlier rewrite of the chapter superseded.
        for result_json in self.session.scalars(
            select(PostCanonMaintenanceRun.result_json).where(
                PostCanonMaintenanceRun.project_id == record.project_id,
                PostCanonMaintenanceRun.chapter_number == record.chapter_number,
                PostCanonMaintenanceRun.step_name == "world",
                PostCanonMaintenanceRun.status == "succeeded",
            )
        ):
            result = _loads(result_json, {})
            if isinstance(result, dict):
                unlisted.difference_update(_string_items(result.get("graph_delta_ids")))
        if unlisted:
            for refs_json in self.session.scalars(
                select(WorldPlanConflictRow.result_refs_json).where(
                    WorldPlanConflictRow.project_id == record.project_id,
                    WorldPlanConflictRow.status == "resolved",
                )
            ):
                unlisted.difference_update(_string_items(_loads(refs_json, [])))
        return unlisted

    def pending_marker(
        self, previous: CanonCommitRecord, *, persist_backfill: bool = True
    ) -> DecisionEvent:
        events = list(
            self.session.scalars(
                select(DecisionEvent)
                .where(
                    DecisionEvent.project_id == previous.project_id,
                    DecisionEvent.chapter_number == previous.chapter_number,
                    DecisionEvent.event_family == "audit_action",
                    DecisionEvent.event_type == DecisionEventType.RETRY_ATTEMPT,
                    DecisionEvent.actor_type == "api",
                )
                .with_for_update()
            )
        )
        pending = []
        for event in events:
            payload = _payload(event)
            if payload.get("previous_status") != "accepted":
                continue
            if "replacement_commit_id" in payload:
                replacement_id = payload["replacement_commit_id"]
                if not isinstance(replacement_id, str) or not replacement_id.strip():
                    raise HistoricalRewriteInvalid(
                        "historical rewrite marker missing or invalid"
                    )
                continue
            pending.append(event)
        if len(pending) != 1:
            raise HistoricalRewriteInvalid(
                "historical rewrite marker missing or invalid"
            )
        event = pending[0]
        payload = _payload(event)
        if payload.get("chapter_number") != previous.chapter_number:
            raise HistoricalRewriteInvalid(
                "historical rewrite marker missing or invalid"
            )
        if "previous_commit_id" not in payload:
            # Older retries predate the durable marker. A single accepted API
            # retry plus exactly one committed record is the only backfill path.
            if (
                len(
                    [
                        item
                        for item in events
                        if _payload(item).get("previous_status") == "accepted"
                    ]
                )
                != 1
            ):
                raise HistoricalRewriteInvalid(
                    "historical rewrite marker missing or invalid"
                )
            records = self.committed_records(
                previous.project_id, previous.chapter_number
            )
            if len(records) != 1 or records[0].id != previous.id:
                raise HistoricalRewriteInvalid(
                    "historical rewrite marker missing or invalid"
                )
            payload["previous_commit_id"] = previous.id
            if persist_backfill:
                event.payload_json = json.dumps(
                    payload, ensure_ascii=False, sort_keys=True
                )
                self.session.flush()
        if payload.get("previous_commit_id") != previous.id:
            raise HistoricalRewriteInvalid(
                "historical rewrite marker missing or invalid"
            )
        return event


@dataclass
class HistoricalCanonReplacement:
    session: Session
    plan: CanonCommitPlan
    previous: CanonCommitRecord
    marker: DecisionEvent
    through_chapter: int
    retired_delta_ids: list[str]
    retained_delta_ids: list[str]
    ordered_deltas: list[GraphDelta]
    replayed_chapters: list[int] = field(default_factory=list)

    def retire_old_contribution(self) -> None:
        repo = BookStateRepository(self.session)
        result = json.loads(self.previous.result_json or "{}")
        result["retired_graph_delta_ids"] = self.retired_delta_ids
        self.previous.result_json = json.dumps(
            result, ensure_ascii=False, sort_keys=True
        )
        self.session.flush()
        repo.invalidate_project_range(
            self.plan.project_id,
            from_chapter=self.plan.chapter_number,
            through_chapter=self.through_chapter,
            ordered_deltas=self.ordered_deltas,
        )
        repo.retire_chapter_deltas(
            self.plan.project_id,
            self.plan.chapter_number,
            delta_ids=self.retired_delta_ids,
        )

    def rebuild_successor_projections(
        self,
        compile_result: BookStateCompileResult,
        *,
        compiler: BookStateCompiler,
    ) -> BookStateCompileResult:
        if self.retained_delta_ids:
            compile_result = self._replay_retained_chapter_deltas(
                compile_result,
                delta_ids=self.retained_delta_ids,
                compiler=compiler,
            )
        self.replayed_chapters = self._rebuild_project_range(
            self.plan.project_id,
            from_chapter=self.plan.chapter_number + 1,
            through_chapter=self.through_chapter,
            compiler=compiler,
            ordered_deltas=[
                delta
                for delta in self.ordered_deltas
                if delta.chapter_number > self.plan.chapter_number
            ],
        )
        return compile_result

    def _replay_retained_chapter_deltas(
        self,
        compile_result: BookStateCompileResult,
        *,
        delta_ids: list[str],
        compiler: BookStateCompiler,
    ) -> BookStateCompileResult:
        """Restore retained edits and ticks after the replacement contribution."""
        repo = BookStateRepository(self.session)
        project_id = compile_result.project_id
        chapter_number = compile_result.chapter_number
        cognition_views = repo.load_cognition_views(
            project_id, as_of_chapter=chapter_number
        )
        cognition_after = max(
            (int(view.as_of_chapter or 0) for view in cognition_views.values()),
            default=-1,
        )
        runtime = compiler.projection.load_runtime_as_of(
            project_id, as_of_chapter=chapter_number
        )
        retained_ids = set(delta_ids)
        if cognition_after < chapter_number:
            # The loader replays cognition beyond its checkpoint, these retained
            # deltas' included. Rebuild it without them, so each one is checked
            # against, and applied after, the replacement contribution.
            for views in (
                runtime.cognition_by_observer,
                runtime.map.cognition_by_observer,
            ):
                views.clear()
                views.update(cognition_views)
            for delta in repo.list_graph_deltas(
                project_id,
                after_chapter=cognition_after,
                through_chapter=chapter_number,
            ):
                if delta.id not in retained_ids:
                    compiler.projection.apply_delta_to_runtime(
                        runtime,
                        delta,
                        apply_world=False,
                        apply_map=False,
                        apply_narrative=False,
                    )
        deltas = [
            delta
            for delta in repo.list_graph_deltas(
                project_id,
                after_chapter=chapter_number - 1,
                through_chapter=chapter_number,
            )
            if delta.id in retained_ids
        ]
        if {delta.id for delta in deltas} != retained_ids:
            raise ValueError("historical rewrite delta manifest missing or invalid")
        deltas = self._replay(runtime, deltas, compiler=compiler)
        provisional_world = self.session.get(
            WorldSnapshotRow, compile_result.world_snapshot_id
        )
        if provisional_world is None:
            raise ValueError("historical rewrite replacement snapshot is missing")
        story_time = provisional_world.as_of_story_time
        world_line_ids = list(
            dict.fromkeys(
                [
                    *_loads(provisional_world.active_world_line_ids_json, []),
                    *(delta.world_line_id for delta in deltas if delta.world_line_id),
                ]
            )
        )
        # Supersede the provisional compile snapshots; two snapshots created in
        # one transaction otherwise have the same timestamp and random ID order.
        for model, snapshot_ids in (
            (WorldSnapshotRow, [compile_result.world_snapshot_id]),
            (MapSnapshotRow, [compile_result.map_snapshot_id]),
            (BookCognitionSnapshotRow, compile_result.cognition_snapshot_ids),
        ):
            self.session.execute(
                delete(model).where(
                    model.project_id == project_id,
                    model.id.in_(snapshot_ids),
                )
            )
        world, map_snapshot, cognition = compiler.projection.persist_snapshots(
            runtime,
            as_of_chapter=chapter_number,
            source_delta_ids=[
                *compile_result.graph_delta_ids,
                *(delta.id for delta in deltas),
            ],
            active_world_line_ids=world_line_ids,
            as_of_story_time=next(
                (delta.story_time for delta in reversed(deltas) if delta.story_time),
                story_time,
            ),
        )
        return compile_result.model_copy(
            update={
                "world_snapshot_id": world.id,
                "map_snapshot_id": map_snapshot.id,
                "cognition_snapshot_ids": [snapshot.id for snapshot in cognition],
            }
        )

    def _rebuild_project_range(
        self,
        project_id: str,
        *,
        from_chapter: int,
        through_chapter: int,
        compiler: BookStateCompiler,
        ordered_deltas: list[GraphDelta] | None = None,
    ) -> list[int]:
        """Replay retained accepted successors without re-inserting their ledger or drafts."""
        repo = BookStateRepository(self.session)

        runtime = compiler.projection.load_runtime_as_of(
            project_id,
            as_of_chapter=max(from_chapter - 1, 0),
        )
        deltas_by_chapter: dict[int, list[GraphDelta]] = {}
        deltas = ordered_deltas if ordered_deltas is not None else repo.list_graph_deltas(
            project_id,
            after_chapter=from_chapter - 1,
            through_chapter=through_chapter,
        )
        for delta in deltas:
            deltas_by_chapter.setdefault(delta.chapter_number, []).append(delta)
        commits = list(
            self.session.scalars(
                select(CanonCommitRecord)
                .join(
                    ChapterPlan,
                    (ChapterPlan.project_id == CanonCommitRecord.project_id)
                    & (ChapterPlan.chapter_number == CanonCommitRecord.chapter_number),
                )
                .where(
                    CanonCommitRecord.project_id == project_id,
                    CanonCommitRecord.chapter_number >= from_chapter,
                    CanonCommitRecord.chapter_number <= through_chapter,
                    CanonCommitRecord.status == "committed",
                    ChapterPlan.status == "accepted",
                )
                .order_by(CanonCommitRecord.chapter_number)
            )
        )
        if set(deltas_by_chapter) - {commit.chapter_number for commit in commits}:
            raise ValueError(
                "historical rewrite successor deltas lack an accepted Canon commit"
            )
        replayed = []
        for commit in commits:
            deltas = self._replay(
                runtime,
                deltas_by_chapter.get(commit.chapter_number, []),
                compiler=compiler,
            )
            world, map_snapshot, cognition = compiler.projection.persist_snapshots(
                runtime,
                as_of_chapter=commit.chapter_number,
                as_of_story_time=next(
                    (
                        delta.story_time
                        for delta in reversed(deltas)
                        if delta.story_time
                    ),
                    "",
                ),
                source_delta_ids=[delta.id for delta in deltas],
                active_world_line_ids=list(
                    dict.fromkeys(
                        delta.world_line_id for delta in deltas if delta.world_line_id
                    )
                ),
            )
            commit.world_snapshot_id = world.id
            commit.map_snapshot_id = map_snapshot.id
            result = _loads(commit.result_json, {})
            if isinstance(result.get("compile_result"), dict):
                result["compile_result"].update(
                    {
                        "world_snapshot_id": world.id,
                        "map_snapshot_id": map_snapshot.id,
                        "cognition_snapshot_ids": [
                            snapshot.id for snapshot in cognition
                        ],
                    }
                )
                commit.result_json = _dump(result)
            runtime.as_of_chapter = commit.chapter_number
            replayed.append(commit.chapter_number)
        self.session.flush()
        return replayed

    def _replay(
        self,
        runtime: BookStateRuntime,
        deltas: list[GraphDelta],
        *,
        compiler: BookStateCompiler,
    ) -> list[GraphDelta]:
        """Re-apply and re-materialize deltas as recorded; return those still in effect.

        A world tick's delta read the canon it ran on, so it is checked against
        the replayed runtime the way the compiler checks a new delta.
        """
        replayed = []
        for delta in deltas:
            if delta.source_type == WORLD_ENGINE_SOURCE_TYPE:
                mismatches = compiler.cas_mismatches(runtime, delta)
                if mismatches:
                    self._drop_world_tick_delta(delta, mismatches)
                    continue
            compiler.projection.apply_delta_to_runtime(runtime, delta)
            replayed.append(delta)
        for delta in replayed:
            compiler._persist_delta_side_effects(runtime, delta)
        return replayed

    def _drop_world_tick_delta(
        self, delta: GraphDelta, mismatches: list[tuple[str, Any, Any]]
    ) -> None:
        """Take the tick's delta out of effect; a human decides how the world responds.

        The range rewind already undid its rows, so leaving it out of the replay
        is enough; removing it from the ledger keeps any later replay or rewrite
        from finding it again.
        """
        BookStateRepository(self.session).retire_chapter_deltas(
            self.plan.project_id, delta.chapter_number, delta_ids=[delta.id]
        )
        tick_id = str(delta.metadata.get("tick_id") or "")
        labels = [label for label, _, _ in mismatches]
        summary = (
            f"Rewriting chapter {self.plan.chapter_number} changed what world tick "
            f"{tick_id} read ({', '.join(labels)}); its delta {delta.id} no longer "
            "applies."
        )
        conflict = WorldConflictRepository(self.session).create(
            ConflictDraft(
                project_id=self.plan.project_id,
                source="historical_rewrite",
                detected_after_chapter=delta.chapter_number,
                story_time=delta.story_time_end or 0,
                involved_refs=list(
                    dict.fromkeys(_patch_ref(patch) for _, patch, _ in mismatches)
                ),
                violated="replay_cas",
                impact_level=_highest_candidate_impact(delta),
                summary=summary,
                proposed_change={"graph_delta_id": delta.id, "tick_id": tick_id},
                blocks_from_chapter=self.through_chapter + 1,
            ),
            required_decider="human",
            options=("veto_world",),
        )
        self.session.add(
            DecisionEvent(
                project_id=self.plan.project_id,
                chapter_number=delta.chapter_number,
                event_family="business_event",
                event_type=DecisionEventType.WORLD_CONFLICT_DETECTED,
                actor_type="system",
                scope="chapter",
                summary=summary,
                payload_json=_dump(
                    {
                        "conflict_id": conflict.id,
                        "rewritten_chapter": self.plan.chapter_number,
                        "graph_delta_id": delta.id,
                        "tick_id": tick_id,
                        "cas_mismatches": [
                            {
                                "patch": label,
                                "expected": patch.old_value,
                                "current": current,
                            }
                            for label, patch, current in mismatches
                        ],
                        # The ledger no longer holds the delta; this is its record.
                        "graph_delta": delta,
                    }
                ),
                related_object_type="world_conflict",
                related_object_id=conflict.id,
            )
        )
        self.session.flush()

    def mark_prior_commit_superseded(self) -> None:
        # Project locking in admission serializes allocation. Keep the original
        # chapter in the audit result while freeing the existing unique key.
        minimum = self.session.scalar(
            select(func.min(CanonCommitRecord.chapter_number)).where(
                CanonCommitRecord.project_id == self.plan.project_id,
            )
        )
        archive_chapter = min(int(minimum or 0), 0) - 1
        result = json.loads(self.previous.result_json or "{}")
        result.update(
            {
                "original_chapter_number": self.previous.chapter_number,
                "superseded_by_commit_id": self.plan.canon_commit_id,
                "replayed_chapters": self.replayed_chapters,
            }
        )
        self.previous.result_json = json.dumps(
            result, ensure_ascii=False, sort_keys=True
        )
        self.previous.chapter_number = archive_chapter
        self.previous.status = "superseded"
        payload = _payload(self.marker)
        payload["replacement_commit_id"] = self.plan.canon_commit_id
        self.marker.payload_json = json.dumps(
            payload, ensure_ascii=False, sort_keys=True
        )
        self.session.flush()


class HistoricalCanonRewriteService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def prepare_replacement(
        self, plan: CanonCommitPlan, *, persist_marker_backfill: bool = True
    ) -> HistoricalCanonReplacement | None:
        records = HistoricalCanonRewriteRepository(self.session).committed_records(
            plan.project_id,
            plan.chapter_number,
        )
        if not records:
            return None
        if len(records) != 1 or records[0].candidate_id == plan.candidate_id:
            raise HistoricalRewriteInvalid(
                "historical rewrite marker missing or invalid"
            )
        previous = records[0]
        repository = HistoricalCanonRewriteRepository(self.session)
        retired_delta_ids, retained_delta_ids = repository.validate_contribution(
            previous
        )
        through_chapter = BookStateRepository(self.session).latest_available_chapter(
            plan.project_id
        )
        successors = list(
            self.session.execute(
                select(CanonCommitRecord, ChapterPlan.status)
                .join(
                    ChapterPlan,
                    (ChapterPlan.project_id == CanonCommitRecord.project_id)
                    & (ChapterPlan.chapter_number == CanonCommitRecord.chapter_number),
                )
                .where(
                    CanonCommitRecord.project_id == plan.project_id,
                    CanonCommitRecord.chapter_number > plan.chapter_number,
                    CanonCommitRecord.status == "committed",
                )
            )
        )
        if any(status != "accepted" for _, status in successors):
            raise HistoricalRewriteInvalid(
                "historical rewrite has a nonaccepted successor"
            )
        ids_by_chapter = {
            plan.chapter_number: [*retired_delta_ids, *retained_delta_ids]
        }
        for successor, _ in successors:
            if successor.chapter_number in ids_by_chapter:
                raise HistoricalRewriteInvalid(
                    "historical rewrite delta manifest missing or invalid"
                )
            owned_ids, retained_ids = repository.validate_contribution(successor)
            ids_by_chapter[successor.chapter_number] = [*owned_ids, *retained_ids]
        delta_ids = [
            delta_id
            for chapter_number in sorted(ids_by_chapter)
            for delta_id in ids_by_chapter[chapter_number]
        ]
        deltas_by_id = {
            delta.id: delta
            for delta in BookStateRepository(self.session).list_graph_deltas(
                plan.project_id,
                after_chapter=plan.chapter_number - 1,
                through_chapter=through_chapter,
            )
        }
        if len(delta_ids) != len(set(delta_ids)) or set(delta_ids) != set(deltas_by_id):
            raise HistoricalRewriteInvalid(
                "historical rewrite delta manifest missing or invalid"
            )
        # Canon manifests retain compile order even when transaction timestamps
        # tie. Accepted standalone edits follow their chapter contribution.
        ordered_deltas = [deltas_by_id[delta_id] for delta_id in delta_ids]
        marker = repository.pending_marker(
            previous, persist_backfill=persist_marker_backfill
        )
        return HistoricalCanonReplacement(
            self.session,
            plan,
            previous,
            marker,
            through_chapter,
            retired_delta_ids,
            retained_delta_ids,
            ordered_deltas,
        )
