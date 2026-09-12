from __future__ import annotations

from sqlalchemy.orm import Session
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from forwin.retrieval.source_identity import CanonReadBaseline

from forwin.protocol.context import (
    AcceptedCognitionSnapshot,
    CognitionSource,
    CanonEventEvidence,
    EntitySnapshot,
    PlotThreadSnapshot,
    RelationSnapshot,
    TimelineSnapshot,
)

from .projection import BookStateProjection
from .repository import BookStateRepository
from .runtime import BookStateRuntime


_ENTITY_NODE_TYPES = {
    "character",
    "faction",
    "organization",
    "group",
    "family",
    "item",
    "resource",
    "ability",
    "rule",
    "location",
    "region",
    "institution",
    "technology",
    "magic_system",
}


class BookStateQuery:
    """Read accepted narrative state from BookState projections only."""

    def __init__(self, session: Session, *, baseline: CanonReadBaseline | None = None) -> None:
        self.baseline = baseline
        self.session = session
        self.repository = BookStateRepository(session)
        self.projection = BookStateProjection(session)
        self._runtime_cache: dict[tuple[str, int, int], BookStateRuntime] = {}

    def runtime(self, project_id: str, *, as_of_chapter: int) -> BookStateRuntime:
        from forwin.retrieval.source_identity import CanonReadBaseline, fresh_orm_reads
        baseline = self.baseline or CanonReadBaseline.capture(self.session, project_id, as_of_chapter=as_of_chapter)
        baseline.assert_current(self.session, project_id=project_id, as_of_chapter=as_of_chapter)
        key = (project_id, baseline.book_revision, int(as_of_chapter))
        runtime = self._runtime_cache.get(key)
        if runtime is None:
            with fresh_orm_reads(self.session):
                runtime = self.projection.load_runtime_as_of(
                    project_id,
                    as_of_chapter=int(as_of_chapter),
                )
            baseline.assert_current(self.session)
            self._runtime_cache[key] = runtime
        return runtime

    def accepted_cognition(
        self,
        project_id: str,
        *,
        as_of_chapter: int,
    ) -> list[AcceptedCognitionSnapshot]:
        from forwin.retrieval.source_identity import CanonReadBaseline, fresh_orm_reads

        baseline = self.baseline or CanonReadBaseline.capture(
            self.session, project_id, as_of_chapter=as_of_chapter
        )
        runtime = self.runtime(project_id, as_of_chapter=as_of_chapter)
        views = runtime.cognition_by_observer
        refs: set[str] = set()
        for view in views.values():
            refs.update(
                view.visible_refs
                | view.hidden_refs
                | view.suspected_refs
                | view.confirmed_refs
            )
            refs.update(view.field_overrides)
            refs.update(view.evidence_by_ref)
            for prefix, objects in (
                ("node", view.false_nodes),
                ("edge", view.false_edges),
                ("fact", view.false_facts),
            ):
                refs.update(f"{prefix}:{key}" for key in objects)
        keys = set(views)
        keys.update(
            ("character", node.id)
            for node in runtime.world.nodes_by_id.values()
            if str(node.node_type) == "character"
        )
        sources: dict[tuple[str, str], dict[str, list[CognitionSource]]] = {}
        evidence = {
            key: {ref: list(items) for ref, items in view.evidence_by_ref.items()}
            for key, view in views.items()
        }
        with fresh_orm_reads(self.session):
            deltas = self.repository.list_graph_deltas(
                project_id, through_chapter=as_of_chapter
            )
        for delta in deltas:
            for patch in delta.cognition_patches:
                key = (str(patch.observer_type), patch.observer_id)
                value = patch.new_value
                patch_refs = []
                if patch.field_path in {
                    "visible_refs",
                    "hidden_refs",
                    "suspected_refs",
                    "confirmed_refs",
                } and isinstance(value, str):
                    patch_refs = [value]
                elif isinstance(value, dict):
                    prefix = {
                        "false_nodes": "node",
                        "false_edges": "edge",
                        "false_facts": "fact",
                    }.get(patch.field_path)
                    if prefix:
                        patch_refs = [f"{prefix}:{ref}" for ref in value]
                    elif patch.field_path in {"field_overrides", "evidence_by_ref"}:
                        patch_refs = list(value)
                if isinstance(value, dict) and patch_refs:
                    # Older replay records dict patches under str(dict). Normalize
                    # that representation to explicit field/false-object refs.
                    legacy = evidence.setdefault(key, {}).pop(str(value), [])
                    refs.discard(str(value))
                    for ref in patch_refs:
                        entries = evidence[key].setdefault(ref, [])
                        entries.extend(
                            item
                            for item in [*legacy, *patch.evidence_refs]
                            if item not in entries
                        )
                for ref in patch_refs:
                    sources.setdefault(key, {}).setdefault(ref, []).append(
                        CognitionSource(
                            delta_id=delta.id,
                            chapter_number=delta.chapter_number,
                            field_path=patch.field_path,
                            op=str(patch.op),
                            evidence_refs=list(patch.evidence_refs),
                        )
                    )
        snapshots = []
        for key in sorted(keys):
            view = views.get(key)

            def objects(field):
                return {
                    ref: value.model_dump(mode="json")
                    if hasattr(value, "model_dump")
                    else value
                    for ref, value in getattr(view, field, {}).items()
                }

            snapshots.append(
                AcceptedCognitionSnapshot(
                    observer_type=key[0],
                    observer_id=key[1],
                    as_of_chapter=as_of_chapter,
                    ref_states={
                        ref: view.get_belief(ref) if view else "unknown"
                        for ref in sorted(refs)
                    },
                    field_overrides=dict(view.field_overrides) if view else {},
                    false_nodes=objects("false_nodes"),
                    false_edges=objects("false_edges"),
                    false_facts=objects("false_facts"),
                    evidence_by_ref=evidence.get(key, {}),
                    sources_by_ref=sources.get(key, {}),
                )
            )
        baseline.assert_current(self.session)
        return snapshots

    def active_entities(
        self,
        project_id: str,
        *,
        as_of_chapter: int,
        kinds: set[str] | None = None,
    ) -> list[EntitySnapshot]:
        runtime = self.runtime(project_id, as_of_chapter=as_of_chapter)
        snapshots: list[EntitySnapshot] = []
        for node in runtime.world.nodes_by_id.values():
            kind = str(node.node_type)
            if kind not in _ENTITY_NODE_TYPES:
                continue
            if kinds is not None and kind not in kinds:
                continue
            if not node.is_active or node.status in {"inactive", "retired", "deleted"}:
                continue
            snapshots.append(
                EntitySnapshot(
                    entity_id=node.id,
                    kind=kind,
                    name=node.name or node.id,
                    importance=node.importance,
                    aliases=list(node.aliases),
                    description=node.description or node.summary,
                    current_state=runtime.world.get_state(node.id),
                )
            )
        return sorted(
            snapshots,
            key=lambda item: (
                -int(item.importance),
                item.kind,
                item.name,
                item.entity_id,
            ),
        )

    def entities_by_names(
        self,
        project_id: str,
        names: list[str],
        *,
        as_of_chapter: int,
    ) -> dict[str, EntitySnapshot]:
        requested = {
            str(name or "").strip() for name in names if str(name or "").strip()
        }
        mapping: dict[str, EntitySnapshot] = {}
        if not requested:
            return mapping
        for entity in self.active_entities(
            project_id,
            as_of_chapter=as_of_chapter,
        ):
            for name in (entity.entity_id, entity.name, *entity.aliases):
                normalized = str(name or "").strip()
                if normalized in requested:
                    mapping[normalized] = entity
        return mapping

    def active_relations(
        self,
        project_id: str,
        *,
        as_of_chapter: int,
        entity_names: list[str] | None = None,
    ) -> list[RelationSnapshot]:
        runtime = self.runtime(project_id, as_of_chapter=as_of_chapter)
        allowed = {
            str(name or "").strip()
            for name in entity_names or []
            if str(name or "").strip()
        }
        relations: list[RelationSnapshot] = []
        for edge in runtime.world.edges_by_id.values():
            if not edge.is_active or edge.status in {"inactive", "ended", "deleted"}:
                continue
            source = runtime.world.nodes_by_id.get(edge.source_id)
            target = runtime.world.nodes_by_id.get(edge.target_id)
            source_name = source.name if source is not None else edge.source_id
            target_name = target.name if target is not None else edge.target_id
            if allowed and source_name not in allowed and target_name not in allowed:
                continue
            description = str(
                edge.metadata.get("description")
                or edge.state.get("state_summary")
                or ""
            )
            relations.append(
                RelationSnapshot(
                    source_name=source_name,
                    target_name=target_name,
                    relation_type=edge.edge_type,
                    description=description,
                )
            )
        return relations

    def active_threads(
        self,
        project_id: str,
        *,
        as_of_chapter: int,
    ) -> list[PlotThreadSnapshot]:
        runtime = self.runtime(project_id, as_of_chapter=as_of_chapter)
        threads: list[PlotThreadSnapshot] = []
        for node in runtime.narrative.nodes_by_id.values():
            if str(node.node_type) not in {"plot_thread", "world_line"}:
                continue
            payload = node.payload if isinstance(node.payload, dict) else {}
            beats = (
                payload.get("beats") if isinstance(payload.get("beats"), list) else []
            )
            beat_descriptions = [
                str(item.get("description") or "")
                for item in beats[-3:]
                if isinstance(item, dict) and str(item.get("description") or "").strip()
            ]
            threads.append(
                PlotThreadSnapshot(
                    thread_id=node.id,
                    name=node.title or node.id,
                    description=str(
                        payload.get("description")
                        or payload.get("latest_summary")
                        or ""
                    ),
                    status=node.status,
                    priority=_int_value(payload.get("priority"), default=5),
                    recent_beats=beat_descriptions,
                )
            )
        return sorted(threads, key=lambda item: (-item.priority, item.name))

    def thread_by_name(
        self,
        project_id: str,
        name: str,
        *,
        as_of_chapter: int,
    ) -> PlotThreadSnapshot | None:
        normalized = str(name or "").strip()
        return next(
            (
                thread
                for thread in self.active_threads(
                    project_id,
                    as_of_chapter=as_of_chapter,
                )
                if thread.name == normalized
            ),
            None,
        )

    def recent_events(
        self,
        project_id: str,
        *,
        before_chapter: int,
        entity_names: list[str] | None = None,
        thread_names: list[str] | None = None,
        limit: int = 5,
    ) -> list[CanonEventEvidence]:
        as_of_chapter = max(int(before_chapter) - 1, 0)
        runtime = self.runtime(project_id, as_of_chapter=as_of_chapter)
        entity_filter = {
            str(name or "").strip()
            for name in entity_names or []
            if str(name or "").strip()
        }
        thread_filter = {
            str(name or "").strip()
            for name in thread_names or []
            if str(name or "").strip()
        }
        ranked: list[tuple[float, CanonEventEvidence]] = []
        for node in runtime.world.nodes_by_id.values():
            if str(node.node_type) != "event":
                continue
            if int(node.created_at_chapter or 0) >= int(before_chapter):
                continue
            profile = node.profile if isinstance(node.profile, dict) else {}
            refs = profile.get("involved_node_refs")
            refs = refs if isinstance(refs, list) else []
            involved_names = [
                _node_name(runtime, str(ref).removeprefix("node:"))
                for ref in refs
                if str(ref or "").strip()
            ]
            involved_names = [name for name in involved_names if name]
            summary = node.summary or node.description or node.name
            overlap_score = float(len(entity_filter & set(involved_names))) * 3.0
            thread_score = (
                float(sum(1 for thread_name in thread_filter if thread_name in summary))
                * 2.0
            )
            recency_score = max(
                0.0,
                10.0 - float(before_chapter - int(node.created_at_chapter or 0)),
            )
            ranked.append(
                (
                    overlap_score + thread_score + recency_score,
                    CanonEventEvidence(
                        event_id=node.id,
                        chapter_number=int(node.created_at_chapter or 0),
                        summary=summary,
                        significance=str(profile.get("event_type") or "minor"),
                        involved_entity_names=involved_names,
                        evidence_id=f"book_state_event:{node.id}",
                    ),
                )
            )
        ranked.sort(
            key=lambda item: (
                -item[0],
                -item[1].chapter_number,
                item[1].event_id,
            )
        )
        return [event for _, event in ranked[: max(0, int(limit))]]

    def event_count(
        self,
        project_id: str,
        *,
        as_of_chapter: int,
    ) -> int:
        runtime = self.runtime(project_id, as_of_chapter=as_of_chapter)
        return sum(
            1
            for node in runtime.world.nodes_by_id.values()
            if str(node.node_type) == "event"
        )

    def current_timeline(
        self,
        project_id: str,
        *,
        as_of_chapter: int,
    ) -> TimelineSnapshot | None:
        deltas = self.repository.list_graph_deltas(
            project_id,
            through_chapter=int(as_of_chapter),
        )
        for delta in reversed(deltas):
            label = str(delta.story_time or "").strip()
            if label:
                return TimelineSnapshot(
                    current_time_label=label,
                    ordinal=int(delta.chapter_number or 0),
                )
        return None


def _node_name(runtime: BookStateRuntime, node_id: str) -> str:
    node = runtime.world.nodes_by_id.get(node_id)
    return str(node.name or node.id) if node is not None else ""


def _int_value(raw: object, *, default: int) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return int(default)


__all__ = ["BookStateQuery"]
