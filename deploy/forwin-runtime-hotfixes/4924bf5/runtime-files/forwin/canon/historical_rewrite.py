from __future__ import annotations

import json
from dataclasses import dataclass, field

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from forwin.audit.events import DecisionEventType
from forwin.book_state.compiler import BookStateCompiler
from forwin.book_state.repository import BookStateRepository, _dump, _loads
from forwin.models.audit import DecisionEvent
from forwin.models.book_state import (
    BookCognitionSnapshotRow,
    GraphDeltaRow,
    MapSnapshotRow,
    WorldSnapshotRow,
)
from forwin.models.canon import CanonCommitRecord
from forwin.models.knowledge import KnowledgeEditProposalRow
from forwin.models.project import ChapterPlan
from forwin.protocol.book_state import BookStateCompileResult, GraphDelta

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
        chapter_ids = list(
            self.session.scalars(
                select(GraphDeltaRow.id)
                .where(
                    GraphDeltaRow.project_id == previous.project_id,
                    GraphDeltaRow.chapter_number == previous.chapter_number,
                )
                .order_by(GraphDeltaRow.created_at, GraphDeltaRow.id)
                .with_for_update()
            )
        )
        owned_ids = set(delta_ids)
        if not owned_ids.issubset(chapter_ids):
            raise HistoricalRewriteInvalid(
                "historical rewrite delta manifest missing or invalid"
            )
        retained_ids = [
            delta_id for delta_id in chapter_ids if delta_id not in owned_ids
        ]
        if retained_ids:
            world_edit_ids = set(
                self.session.scalars(
                    select(KnowledgeEditProposalRow.graph_delta_id).where(
                        KnowledgeEditProposalRow.project_id == previous.project_id,
                        KnowledgeEditProposalRow.status == "accepted",
                        KnowledgeEditProposalRow.graph_delta_id.in_(retained_ids),
                    )
                )
            )
            if world_edit_ids != set(retained_ids):
                raise HistoricalRewriteInvalid(
                    "historical rewrite delta manifest missing or invalid"
                )
        return delta_ids, retained_ids

    def pending_marker(self, previous: CanonCommitRecord) -> DecisionEvent:
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
            event.payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
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
        """Restore accepted standalone edits after the replacement chapter contribution."""
        repo = BookStateRepository(self.session)
        project_id = compile_result.project_id
        chapter_number = compile_result.chapter_number
        cognition_after = max(
            (
                int(view.as_of_chapter or 0)
                for view in repo.load_cognition_views(
                    project_id, as_of_chapter=chapter_number
                ).values()
            ),
            default=-1,
        )
        runtime = compiler.projection.load_runtime_as_of(
            project_id, as_of_chapter=chapter_number
        )
        retained_ids = set(delta_ids)
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
        for delta in deltas:
            # The loader already replays cognition beyond its checkpoint. Only
            # a replacement-chapter checkpoint still needs these retained edits.
            compiler.projection.apply_delta_to_runtime(
                runtime, delta, apply_cognition=cognition_after >= chapter_number
            )
        for delta in deltas:
            compiler._persist_delta_side_effects(runtime, delta)
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
            source_delta_ids=[*compile_result.graph_delta_ids, *delta_ids],
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
            deltas = deltas_by_chapter.get(commit.chapter_number, [])
            for delta in deltas:
                compiler.projection.apply_delta_to_runtime(runtime, delta)
            for delta in deltas:
                compiler._persist_delta_side_effects(runtime, delta)
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
        self, plan: CanonCommitPlan
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
        marker = repository.pending_marker(previous)
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
