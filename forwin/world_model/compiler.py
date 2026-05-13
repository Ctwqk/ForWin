from __future__ import annotations

import json
import re
from hashlib import sha256
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.book_state.repository import BookStateRepository
from forwin.models import (
    BookGenesisRevision,
    CanonEvent,
    ChapterTimeline,
    Entity,
    EntityState,
    EventEntityLink,
    NPCIntentSnapshot,
    PlotThread,
    PlotThreadBeat,
    Project,
    RelationEdge,
    StoryTimePoint,
    WorldSimulationTurn,
)
from forwin.models.book_state import WorldNodeStateRow
from forwin.models.world_model import WorldModelSnapshotRow
from forwin.protocol.world_model import EvidenceRef, WorldModelConflict, WorldModelSnapshot

from .conflict_detector import detect_conflicts
from .store import WorldModelStore, content_hash, snapshot_to_schema, stable_json


def _load_json(raw: str | None, default: Any) -> Any:
    try:
        value = json.loads(raw or "")
    except (json.JSONDecodeError, TypeError):
        return default
    return value if isinstance(value, type(default)) else default


def _safe_filename(value: str, fallback: str = "Untitled") -> str:
    text = str(value or "").strip() or fallback
    text = re.sub(r"[\\/:*?\"<>|]+", "_", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:80] or fallback


def _slug(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_\-\u4e00-\u9fff]+", "-", str(value or "").strip().lower())
    text = re.sub(r"-+", "-", text).strip("-")
    return text or "item"


def _metadata_chapter(metadata: dict[str, Any]) -> int:
    try:
        return int((metadata or {}).get("created_at_chapter") or 0)
    except (TypeError, ValueError):
        return 0


def _frontmatter_text(frontmatter: dict[str, Any]) -> str:
    lines = ["---"]
    for key, value in frontmatter.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"  - {json.dumps(item, ensure_ascii=False)}")
        else:
            lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    lines.append("---")
    return "\n".join(lines)


def _page_markdown(
    *,
    frontmatter: dict[str, Any],
    title: str,
    canon_summary: str,
    current_state: str = "",
    relationships: list[str] | None = None,
    plot_usage: list[str] | None = None,
    open_questions: list[str] | None = None,
    evidence: list[str] | None = None,
) -> str:
    relation_lines = relationships or []
    usage_lines = plot_usage or []
    question_lines = open_questions or []
    evidence_lines = evidence or []
    return "\n\n".join(
        [
            _frontmatter_text(frontmatter),
            f"# {title}",
            "## Canon Summary\n" + (canon_summary.strip() or "暂无稳定摘要。"),
            "## Current State\n" + (current_state.strip() or "暂无运行时状态。"),
            "## Relationships\n" + ("\n".join(f"- {item}" for item in relation_lines) if relation_lines else "- 暂无关系记录。"),
            "## Plot Usage\n" + ("\n".join(f"- {item}" for item in usage_lines) if usage_lines else "- 暂无明确剧情用途。"),
            "## Open Questions\n" + ("\n".join(f"- {item}" for item in question_lines) if question_lines else "- 暂无。"),
            "## Evidence\n" + ("\n".join(f"- {item}" for item in evidence_lines) if evidence_lines else "- 暂无。"),
            "## Manual Notes\n",
        ]
    ) + "\n"


class WorldModelCompiler:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.store = WorldModelStore(session)

    def bootstrap_from_genesis(self, project_id: str) -> WorldModelSnapshot:
        return self._compile(
            project_id=project_id,
            as_of_chapter=0,
            trigger="genesis_bootstrap",
            require_genesis=True,
        )

    def compile_after_chapter(self, project_id: str, chapter_number: int) -> WorldModelSnapshot:
        return self._compile(
            project_id=project_id,
            as_of_chapter=max(0, int(chapter_number or 0)),
            trigger="chapter_accepted",
            require_genesis=False,
        )

    def _compile(
        self,
        *,
        project_id: str,
        as_of_chapter: int,
        trigger: str,
        require_genesis: bool,
    ) -> WorldModelSnapshot:
        project = self.session.get(Project, project_id)
        if project is None:
            raise ValueError(f"Project {project_id} not found.")
        genesis = self._active_genesis_revision(project)
        if genesis is None and require_genesis:
            raise ValueError(f"Project {project_id} has no active Genesis revision.")

        snapshot_payload = self._build_snapshot_payload(
            project=project,
            genesis=genesis,
            as_of_chapter=as_of_chapter,
        )
        conflicts = detect_conflicts(snapshot_payload)
        snapshot_payload.setdefault("quality_model", {})["contradictions"] = [
            item.model_dump(mode="json") for item in conflicts
        ]
        source_refs = snapshot_payload.get("source_refs", [])
        digest = content_hash(stable_json({"snapshot": snapshot_payload, "as_of_chapter": as_of_chapter}))
        existing = self.store.snapshot_by_digest(
            project_id,
            as_of_chapter=as_of_chapter,
            source_digest=digest,
        )
        run = self.store.create_compile_run(
            project_id=project_id,
            trigger=trigger,
            as_of_chapter=as_of_chapter,
            source_refs=source_refs,
            source_digest=digest,
            status="started",
        )
        if existing is not None:
            self._sync_pages(project=project, snapshot=snapshot_payload, snapshot_row=existing)
            self.store.replace_conflicts(project_id=project_id, conflicts=conflicts)
            self.store.mark_compile_run(run, status="skipped", snapshot_id=existing.id)
            return snapshot_to_schema(existing)

        row = self.store.save_snapshot(
            project_id=project_id,
            as_of_chapter=as_of_chapter,
            snapshot=snapshot_payload,
            source_digest=digest,
        )
        self._sync_pages(project=project, snapshot=snapshot_payload, snapshot_row=row)
        self.store.replace_conflicts(project_id=project_id, conflicts=conflicts)
        self.store.mark_compile_run(run, status="succeeded", snapshot_id=row.id)
        return snapshot_to_schema(row)

    def record_failed_compile(
        self,
        *,
        project_id: str,
        as_of_chapter: int,
        trigger: str,
        error: str,
    ) -> None:
        self.store.create_compile_run(
            project_id=project_id,
            trigger=trigger,
            as_of_chapter=as_of_chapter,
            source_refs=[],
            source_digest="",
            status="failed",
            error=error,
        )

    def _active_genesis_revision(self, project: Project) -> BookGenesisRevision | None:
        revision_id = str(project.active_genesis_revision_id or "").strip()
        if revision_id:
            row = self.session.get(BookGenesisRevision, revision_id)
            if row is not None:
                return row
        stmt = (
            select(BookGenesisRevision)
            .where(BookGenesisRevision.project_id == project.id)
            .order_by(BookGenesisRevision.revision.desc(), BookGenesisRevision.created_at.desc())
            .limit(1)
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def _build_snapshot_payload(
        self,
        *,
        project: Project,
        genesis: BookGenesisRevision | None,
        as_of_chapter: int,
    ) -> dict[str, Any]:
        if genesis is not None:
            pack = _load_json(genesis.pack_json, {})
        else:
            pack = {
                "world": {
                    "world_bible": {
                        "overview": project.setting_summary or project.premise,
                        "axioms": [],
                        "history_slice": "",
                        "culture_profiles": [],
                    },
                    "map_atlas": {"overview": project.setting_summary or ""},
                    "story_engine": {"reader_promises": [], "long_arcs": []},
                }
            }
        world = pack.get("world") if isinstance(pack.get("world"), dict) else {}
        if not world:
            world = {
                "world_bible": pack.get("world_bible") if isinstance(pack.get("world_bible"), dict) else {},
                "map_atlas": pack.get("map_atlas") if isinstance(pack.get("map_atlas"), dict) else {},
                "story_engine": pack.get("story_engine") if isinstance(pack.get("story_engine"), dict) else {},
            }
        world_bible = world.get("world_bible") if isinstance(world.get("world_bible"), dict) else {}
        map_atlas = world.get("map_atlas") if isinstance(world.get("map_atlas"), dict) else {}
        story_engine = world.get("story_engine") if isinstance(world.get("story_engine"), dict) else {}

        book_state_payload = self._build_book_state_snapshot_payload(
            project=project,
            genesis=genesis,
            as_of_chapter=as_of_chapter,
            world_bible=world_bible,
            map_atlas=map_atlas,
            story_engine=story_engine,
            world=world,
        )
        if book_state_payload is not None:
            return book_state_payload

        entities = self.session.execute(
            select(Entity)
            .where(Entity.project_id == project.id, Entity.created_at_chapter <= as_of_chapter)
            .order_by(Entity.importance.desc(), Entity.name.asc())
        ).scalars().all()
        latest_state_by_entity = self._latest_entity_states([entity.id for entity in entities], as_of_chapter)
        relations = self.session.execute(
            select(RelationEdge)
            .where(
                RelationEdge.project_id == project.id,
                RelationEdge.established_at_chapter <= as_of_chapter,
                RelationEdge.is_active.is_(True),
            )
        ).scalars().all()
        entity_name_by_id = {entity.id: entity.name for entity in entities}

        characters = []
        factions = []
        organizations = []
        other_entities = []
        for entity in entities:
            state = latest_state_by_entity.get(entity.id)
            payload = {
                "id": entity.id,
                "kind": entity.kind,
                "name": entity.name,
                "aliases": _load_json(entity.aliases_json, []),
                "description": entity.description,
                "importance": entity.importance,
                "created_at_chapter": entity.created_at_chapter,
                "current_state": _load_json(state.state_json, {}) if state is not None else {},
                "state_chapter": int(state.as_of_chapter or 0) if state is not None else 0,
            }
            if entity.kind == "character":
                characters.append(payload)
            elif entity.kind == "faction":
                factions.append(payload)
            elif entity.kind in {"organization", "family"}:
                organizations.append(payload)
            else:
                other_entities.append(payload)

        events = self._canon_events(project.id, as_of_chapter)
        threads = self._plot_threads(project.id, as_of_chapter)
        time_model = self._time_model(project.id, as_of_chapter)
        world_turn = self._world_turn(project.id, as_of_chapter)
        npc_intents = self._npc_intents(project.id, as_of_chapter)

        if genesis is not None:
            source_refs = [
                EvidenceRef(
                    source_type="book_genesis_revision",
                    source_id=genesis.id,
                    chapter_number=0,
                    summary=f"Genesis revision {genesis.revision}",
                ).model_dump(mode="json")
            ]
        else:
            source_refs = [
                EvidenceRef(
                    source_type="project",
                    source_id=project.id,
                    chapter_number=0,
                    summary="Legacy project scaffold without Genesis revision.",
                ).model_dump(mode="json")
            ]
        source_refs.extend(
            EvidenceRef(source_type="canon_event", source_id=str(event.get("id", "")), chapter_number=int(event.get("chapter_number", 0)), summary=str(event.get("summary", ""))).model_dump(mode="json")
            for event in events
        )

        return {
            "project_id": project.id,
            "project_title": project.title,
            "as_of_chapter": as_of_chapter,
            "world_root": {
                "overview": world_bible.get("overview", "") or project.setting_summary,
                "axioms": world_bible.get("axioms") if isinstance(world_bible.get("axioms"), list) else [],
                "history": world_bible.get("history_slice", ""),
                "culture_profiles": world_bible.get("culture_profiles") if isinstance(world_bible.get("culture_profiles"), list) else [],
            },
            "space_model": {
                "overview": map_atlas.get("overview", ""),
                "submaps": map_atlas.get("submaps") if isinstance(map_atlas.get("submaps"), list) else [],
                "regions": map_atlas.get("regions") if isinstance(map_atlas.get("regions"), list) else [],
                "nodes": map_atlas.get("nodes") if isinstance(map_atlas.get("nodes"), list) else [],
                "routes": map_atlas.get("edges") if isinstance(map_atlas.get("edges"), list) else [],
                "ownership": [],
                "travel_constraints": map_atlas.get("topology_rules") if isinstance(map_atlas.get("topology_rules"), list) else [],
            },
            "actor_model": {
                "characters": self._dedupe_actor_payloads(
                    characters
                    + self._genesis_actor_pages(story_engine, "characters", "character")
                    + self._genesis_actor_pages(story_engine, "core_cast", "character")
                    + self._culture_example_characters(world_bible)
                ),
                "factions": self._dedupe_actor_payloads(
                    factions + self._genesis_actor_pages(story_engine, "factions", "faction")
                ),
                "organizations": organizations,
                "families": [],
                "other_entities": other_entities,
                "relationship_edges": [
                    {
                        "id": relation.id,
                        "source_name": entity_name_by_id.get(relation.source_entity_id, relation.source_entity_id),
                        "target_name": entity_name_by_id.get(relation.target_entity_id, relation.target_entity_id),
                        "relation_type": relation.relation_type,
                        "description": relation.description,
                        "established_at_chapter": relation.established_at_chapter,
                    }
                    for relation in relations
                ],
                "hierarchy_templates": [],
                "npc_intents": npc_intents,
            },
            "institution_model": {
                "governments": [],
                "religious_orders": [],
                "sects": [],
                "guilds": [],
                "military_chains": [],
                "laws": [],
                "ranks": [],
                "permissions": [],
                "profiles": world.get("institution_profiles") if isinstance(world.get("institution_profiles"), list) else [],
            },
            "economy_model": {
                "resources": [],
                "currencies": [],
                "trade_routes": [],
                "scarcity": [],
                "production": [],
                "monopolies": [],
                "profiles": world.get("resource_economy_profiles") if isinstance(world.get("resource_economy_profiles"), list) else [],
            },
            "time_model": time_model,
            "technology_model": {
                "magic": world.get("magic_system", {}),
                "cultivation": world.get("cultivation_system", {}),
                "science": {},
                "weapons": [],
                "communication": [],
                "transport": [],
                "medical_limits": [],
            },
            "plot_model": {
                "canon_events": events,
                "active_threads": threads,
                "reader_promises": story_engine.get("reader_promises") if isinstance(story_engine.get("reader_promises"), list) else [],
                "secrets": self._world_extension_list(world, "secrets_codex"),
                "reveal_ladder": [],
                "unresolved_hooks": [thread for thread in threads if thread.get("status") == "active"],
                "future_constraints": [],
                "world_pressure": world_turn,
            },
            "quality_model": {
                "contradictions": [],
                "risky_claims": [],
                "open_questions": [],
                "review_findings": [],
            },
            "source_refs": source_refs,
        }

    def _build_book_state_snapshot_payload(
        self,
        *,
        project: Project,
        genesis: BookGenesisRevision | None,
        as_of_chapter: int,
        world_bible: dict[str, Any],
        map_atlas: dict[str, Any],
        story_engine: dict[str, Any],
        world: dict[str, Any],
    ) -> dict[str, Any] | None:
        repo = BookStateRepository(self.session)
        nodes = repo.list_world_nodes(project.id, as_of_chapter=as_of_chapter)
        edges = repo.list_world_edges(project.id, as_of_chapter=as_of_chapter)
        facts = repo.list_fact_nodes(project.id, as_of_chapter=as_of_chapter)
        map_nodes = [
            node
            for node in repo.list_map_nodes(project.id)
            if _metadata_chapter(node.metadata) <= as_of_chapter
        ]
        if not (nodes or edges or facts or map_nodes):
            return None

        latest_state_by_node = self._latest_book_state_node_states(
            project.id,
            [node.id for node in nodes],
            as_of_chapter,
        )
        characters: list[dict[str, Any]] = []
        factions: list[dict[str, Any]] = []
        organizations: list[dict[str, Any]] = []
        other_entities: list[dict[str, Any]] = []
        for node in nodes:
            state = latest_state_by_node.get(node.id, {})
            payload = {
                "id": node.id,
                "kind": str(node.node_type),
                "name": node.name or node.id,
                "aliases": list(node.aliases),
                "description": node.description or node.summary,
                "importance": node.importance,
                "created_at_chapter": node.created_at_chapter,
                "current_state": state,
                "state_chapter": int(state.get("_as_of_chapter", node.created_at_chapter) or 0) if isinstance(state, dict) else 0,
                "source": "book_state",
                "source_refs": list(node.source_refs),
                "profile": dict(node.profile),
                "metadata": dict(node.metadata),
            }
            node_type = str(node.node_type)
            if node_type == "character":
                characters.append(payload)
            elif node_type == "faction":
                factions.append(payload)
            elif node_type in {"organization", "family", "institution"}:
                organizations.append(payload)
            else:
                other_entities.append(payload)

        node_name_by_id = {node.id: node.name or node.id for node in nodes}
        relationships = [
            {
                "id": edge.id,
                "source_name": node_name_by_id.get(edge.source_id, edge.source_id),
                "target_name": node_name_by_id.get(edge.target_id, edge.target_id),
                "relation_type": edge.edge_type,
                "description": str(edge.state.get("description") or edge.metadata.get("description") or edge.edge_family),
                "established_at_chapter": edge.established_at_chapter,
                "source": "book_state",
            }
            for edge in edges
        ]
        region_payloads = []
        node_payloads = []
        for map_node in map_nodes:
            payload = {
                "id": map_node.id,
                "name": map_node.name or map_node.id,
                "title": map_node.name or map_node.id,
                "summary": map_node.description,
                "description": map_node.description,
                "node_type": str(map_node.node_type),
                "source": "book_state",
                "metadata": dict(map_node.metadata),
            }
            if str(map_node.node_type) in {"world_area", "region"}:
                region_payloads.append(payload)
            else:
                node_payloads.append(payload)

        source_refs: list[dict[str, Any]] = []
        if genesis is not None:
            source_refs.append(
                EvidenceRef(
                    source_type="book_genesis_revision",
                    source_id=genesis.id,
                    chapter_number=0,
                    summary=f"Genesis revision {genesis.revision}",
                ).model_dump(mode="json")
            )
        source_refs.extend(
            EvidenceRef(
                source_type="book_state_node",
                source_id=node.id,
                chapter_number=int(node.created_at_chapter or 0),
                summary=node.name or node.id,
            ).model_dump(mode="json")
            for node in nodes[:20]
        )
        source_refs.extend(
            EvidenceRef(
                source_type="book_state_fact",
                source_id=fact.id,
                chapter_number=int(fact.created_at_chapter or 0),
                summary=fact.proposition,
            ).model_dump(mode="json")
            for fact in facts[:20]
        )
        return {
            "project_id": project.id,
            "project_title": project.title,
            "as_of_chapter": as_of_chapter,
            "projection_source": "book_state",
            "world_root": {
                "overview": world_bible.get("overview", "") or project.setting_summary,
                "axioms": world_bible.get("axioms") if isinstance(world_bible.get("axioms"), list) else [],
                "history": world_bible.get("history_slice", ""),
                "culture_profiles": world_bible.get("culture_profiles") if isinstance(world_bible.get("culture_profiles"), list) else [],
            },
            "space_model": {
                "overview": map_atlas.get("overview", ""),
                "submaps": [],
                "regions": region_payloads,
                "nodes": node_payloads,
                "routes": [],
                "ownership": [],
                "travel_constraints": [],
            },
            "actor_model": {
                "characters": characters,
                "factions": factions,
                "organizations": organizations,
                "families": [],
                "other_entities": other_entities,
                "relationship_edges": relationships,
                "hierarchy_templates": [],
                "npc_intents": [],
            },
            "institution_model": {
                "governments": [],
                "religious_orders": [],
                "sects": [],
                "guilds": [],
                "military_chains": [],
                "laws": [],
                "ranks": [],
                "permissions": [],
                "profiles": [item for item in organizations if item.get("kind") == "institution"],
            },
            "economy_model": {
                "resources": [],
                "currencies": [],
                "trade_routes": [],
                "scarcity": [],
                "production": [],
                "monopolies": [],
                "profiles": [],
            },
            "time_model": self._time_model(project.id, as_of_chapter),
            "technology_model": {
                "magic": world.get("magic_system", {}),
                "cultivation": world.get("cultivation_system", {}),
                "science": {},
                "weapons": [],
                "communication": [],
                "transport": [],
                "medical_limits": [],
            },
            "plot_model": {
                "canon_events": [
                    {
                        "id": fact.id,
                        "chapter_number": fact.created_at_chapter,
                        "summary": fact.proposition,
                        "significance": fact.truth_value,
                        "source": "book_state",
                    }
                    for fact in facts
                ],
                "active_threads": [],
                "reader_promises": story_engine.get("reader_promises") if isinstance(story_engine.get("reader_promises"), list) else [],
                "secrets": [],
                "reveal_ladder": [],
                "unresolved_hooks": [],
                "world_pressure": self._world_turn(project.id, as_of_chapter),
            },
            "quality_model": {
                "contradictions": [],
                "risky_claims": [],
                "open_questions": [],
                "review_findings": [],
            },
            "source_refs": source_refs,
        }

    def _latest_book_state_node_states(
        self,
        project_id: str,
        node_ids: list[str],
        as_of_chapter: int,
    ) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for node_id in node_ids:
            row = self.session.execute(
                select(WorldNodeStateRow)
                .where(
                    WorldNodeStateRow.project_id == project_id,
                    WorldNodeStateRow.node_id == node_id,
                    WorldNodeStateRow.as_of_chapter <= as_of_chapter,
                )
                .order_by(WorldNodeStateRow.as_of_chapter.desc(), WorldNodeStateRow.created_at.desc(), WorldNodeStateRow.id.desc())
                .limit(1)
            ).scalar_one_or_none()
            if row is None:
                continue
            state = _load_json(row.state_json, {})
            if isinstance(state, dict):
                state = {**state, "_as_of_chapter": int(row.as_of_chapter or 0)}
                result[node_id] = state
        return result

    def _latest_entity_states(self, entity_ids: list[str], as_of_chapter: int) -> dict[str, EntityState]:
        result: dict[str, EntityState] = {}
        for entity_id in entity_ids:
            row = self.session.execute(
                select(EntityState)
                .where(
                    EntityState.entity_id == entity_id,
                    EntityState.as_of_chapter <= as_of_chapter,
                )
                .order_by(EntityState.as_of_chapter.desc(), EntityState.updated_at.desc(), EntityState.id.desc())
                .limit(1)
            ).scalar_one_or_none()
            if row is not None:
                result[entity_id] = row
        return result

    def _canon_events(self, project_id: str, as_of_chapter: int) -> list[dict[str, Any]]:
        rows = self.session.execute(
            select(CanonEvent)
            .where(CanonEvent.project_id == project_id, CanonEvent.chapter_number <= as_of_chapter)
            .order_by(CanonEvent.chapter_number.asc(), CanonEvent.created_at.asc())
        ).scalars().all()
        if not rows:
            return []
        event_ids = [row.id for row in rows]
        links = self.session.execute(
            select(EventEntityLink, Entity)
            .join(Entity, Entity.id == EventEntityLink.entity_id)
            .where(EventEntityLink.event_id.in_(event_ids))
        ).all()
        names_by_event: dict[str, list[str]] = {}
        for link, entity in links:
            names_by_event.setdefault(link.event_id, []).append(entity.name)
        return [
            {
                "id": row.id,
                "chapter_number": row.chapter_number,
                "summary": row.summary,
                "significance": row.significance,
                "involved_entity_names": names_by_event.get(row.id, []),
            }
            for row in rows
        ]

    def _plot_threads(self, project_id: str, as_of_chapter: int) -> list[dict[str, Any]]:
        rows = self.session.execute(
            select(PlotThread)
            .where(PlotThread.project_id == project_id, PlotThread.opened_at_chapter <= as_of_chapter)
            .order_by(PlotThread.priority.asc(), PlotThread.name.asc())
        ).scalars().all()
        payloads = []
        for row in rows:
            beats = self.session.execute(
                select(PlotThreadBeat)
                .where(PlotThreadBeat.thread_id == row.id, PlotThreadBeat.chapter_number <= as_of_chapter)
                .order_by(PlotThreadBeat.chapter_number.asc())
            ).scalars().all()
            payloads.append(
                {
                    "id": row.id,
                    "name": row.name,
                    "description": row.description,
                    "status": row.status,
                    "priority": row.priority,
                    "beats": [
                        {
                            "chapter_number": beat.chapter_number,
                            "beat_type": beat.beat_type,
                            "description": beat.description,
                        }
                        for beat in beats
                    ],
                }
            )
        return payloads

    def _time_model(self, project_id: str, as_of_chapter: int) -> dict[str, Any]:
        timeline = self.session.execute(
            select(ChapterTimeline)
            .where(ChapterTimeline.project_id == project_id, ChapterTimeline.chapter_number <= as_of_chapter)
            .order_by(ChapterTimeline.chapter_number.desc())
            .limit(1)
        ).scalar_one_or_none()
        if timeline is None:
            return {"calendar": "", "current_time": "", "festivals": [], "cycles": [], "deadlines": []}
        time_point = self.session.get(StoryTimePoint, timeline.end_time_id or timeline.start_time_id)
        return {
            "calendar": "",
            "current_time": time_point.label if time_point is not None else "",
            "current_ordinal": time_point.ordinal if time_point is not None else 0,
            "duration_description": timeline.duration_description,
            "festivals": [],
            "cycles": [],
            "deadlines": [],
        }

    def _world_turn(self, project_id: str, as_of_chapter: int) -> dict[str, Any]:
        row = self.session.execute(
            select(WorldSimulationTurn)
            .where(WorldSimulationTurn.project_id == project_id, WorldSimulationTurn.chapter_number <= as_of_chapter)
            .order_by(WorldSimulationTurn.chapter_number.desc(), WorldSimulationTurn.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            return {}
        return {
            "chapter_number": row.chapter_number,
            "pressure_level": row.pressure_level,
            "pressure_summary": row.pressure_summary,
            "notable_shifts": _load_json(row.notable_shifts_json, []),
        }

    def _npc_intents(self, project_id: str, as_of_chapter: int) -> list[dict[str, Any]]:
        rows = self.session.execute(
            select(NPCIntentSnapshot)
            .where(NPCIntentSnapshot.project_id == project_id, NPCIntentSnapshot.chapter_number <= as_of_chapter)
            .order_by(NPCIntentSnapshot.chapter_number.desc(), NPCIntentSnapshot.urgency.desc())
            .limit(12)
        ).scalars().all()
        return [
            {
                "entity_id": row.entity_id,
                "entity_name": row.entity_name,
                "chapter_number": row.chapter_number,
                "intent_kind": row.intent_kind,
                "objective": row.objective,
                "tactic": row.tactic,
                "urgency": row.urgency,
                "notes": row.notes,
            }
            for row in rows
        ]

    @staticmethod
    def _dedupe_actor_payloads(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: dict[str, int] = {}
        for payload in payloads:
            if not isinstance(payload, dict):
                continue
            name = str(payload.get("name", "") or "").strip()
            key = re.sub(r"\s+", "", name).lower() or str(payload.get("id", "") or "").strip()
            if not key:
                continue
            if key not in seen:
                seen[key] = len(result)
                result.append(payload)
                continue
            existing = result[seen[key]]
            aliases = []
            for source in (existing.get("aliases"), payload.get("aliases")):
                if isinstance(source, list):
                    aliases.extend(str(item).strip() for item in source if str(item).strip())
            if aliases:
                existing["aliases"] = list(dict.fromkeys(aliases))
            if not str(existing.get("description", "") or "").strip() and payload.get("description"):
                existing["description"] = payload.get("description")
            existing_state = existing.get("current_state") if isinstance(existing.get("current_state"), dict) else {}
            payload_state = payload.get("current_state") if isinstance(payload.get("current_state"), dict) else {}
            if payload_state:
                existing["current_state"] = {**payload_state, **existing_state}
        return result

    @staticmethod
    def _genesis_actor_pages(story_engine: dict[str, Any], key: str, kind: str) -> list[dict[str, Any]]:
        values = story_engine.get(key) if isinstance(story_engine.get(key), list) else []
        payloads = []
        for item in values:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "") or "").strip()
            if not name:
                continue
            payloads.append(
                {
                    "id": f"genesis:{kind}:{_slug(name)}",
                    "kind": kind,
                    "name": name,
                    "aliases": [],
                    "description": str(item.get("description") or item.get("role") or item.get("agenda") or ""),
                    "importance": 5,
                    "created_at_chapter": 0,
                    "current_state": item,
                    "state_chapter": 0,
                    "source": "genesis",
                }
            )
        return payloads

    @staticmethod
    def _culture_example_characters(world_bible: dict[str, Any]) -> list[dict[str, Any]]:
        profiles = world_bible.get("culture_profiles") if isinstance(world_bible.get("culture_profiles"), list) else []
        payloads: list[dict[str, Any]] = []
        seen: set[str] = set()
        for profile in profiles:
            if not isinstance(profile, dict):
                continue
            examples = profile.get("character_name_examples")
            if not isinstance(examples, list):
                continue
            for example in examples:
                name = str(example or "").strip()
                if not name or name in seen:
                    continue
                seen.add(name)
                payloads.append(
                    {
                        "id": f"genesis:character:{_slug(name)}",
                        "kind": "character",
                        "name": name,
                        "aliases": [],
                        "description": f"{name} 来自 Genesis 文化命名样例，需在后续 Arc/章节中确认具体角色职责。",
                        "importance": 4,
                        "created_at_chapter": 0,
                        "current_state": {"source": "culture_profile_name_example"},
                        "state_chapter": 0,
                        "source": "genesis",
                    }
                )
        return payloads

    @staticmethod
    def _world_extension_list(world: dict[str, Any], key: str) -> list[dict[str, Any]]:
        extensions = world.get("world_extensions") if isinstance(world.get("world_extensions"), dict) else {}
        values = extensions.get(key) if isinstance(extensions.get(key), list) else []
        return [item for item in values if isinstance(item, dict)]

    def _sync_pages(
        self,
        *,
        project: Project,
        snapshot: dict[str, Any],
        snapshot_row: WorldModelSnapshotRow,
    ) -> None:
        pages = self._build_pages(project=project, snapshot=snapshot, snapshot_id=snapshot_row.id)
        for page in pages:
            self.store.upsert_page(**page)
        self.store.replace_links(project_id=project.id, links=self._build_links(snapshot))

    def _build_pages(self, *, project: Project, snapshot: dict[str, Any], snapshot_id: str) -> list[dict[str, Any]]:
        as_of_chapter = int(snapshot.get("as_of_chapter", 0) or 0)
        source_refs = snapshot.get("source_refs") if isinstance(snapshot.get("source_refs"), list) else []

        def frontmatter(page_key: str, page_type: str, title: str) -> dict[str, Any]:
            return {
                "forwin_id": page_key,
                "project_id": project.id,
                "page_type": page_type,
                "status": "canon_live",
                "as_of_chapter": as_of_chapter,
                "snapshot_id": snapshot_id,
                "source_refs": [ref.get("source_type", "") + ":" + ref.get("source_id", "") for ref in source_refs[:8] if isinstance(ref, dict)],
                "locked_fields": ["Canon Summary", "Current State", "Evidence"],
            }

        pages: list[dict[str, Any]] = []
        world_root = snapshot.get("world_root") if isinstance(snapshot.get("world_root"), dict) else {}
        overview_key = "world:index"
        overview_fm = frontmatter(overview_key, "overview", "00_Index")
        overview_text = _page_markdown(
            frontmatter=overview_fm,
            title="ForWin World Index",
            canon_summary=str(world_root.get("overview", "") or project.setting_summary or project.premise),
            current_state=f"截至第 {as_of_chapter} 章。世界规则：{'; '.join(str(item) for item in world_root.get('axioms', [])[:8])}",
            plot_usage=[str(item) for item in (snapshot.get("plot_model", {}).get("reader_promises", []) if isinstance(snapshot.get("plot_model"), dict) else [])],
            evidence=[f"{ref.get('source_type')}:{ref.get('source_id')}" for ref in source_refs[:8] if isinstance(ref, dict)],
        )
        pages.append(
            {
                "project_id": project.id,
                "page_key": overview_key,
                "page_type": "overview",
                "title": "00_Index",
                "vault_path": "00_Index.md",
                "markdown": overview_text,
                "frontmatter": overview_fm,
                "as_of_chapter": as_of_chapter,
            }
        )

        actor_model = snapshot.get("actor_model") if isinstance(snapshot.get("actor_model"), dict) else {}
        for character in actor_model.get("characters", []) if isinstance(actor_model.get("characters"), list) else []:
            self._append_entity_page(pages, project, character, "character", "04_Characters", as_of_chapter, snapshot_id, source_refs)
        for faction in actor_model.get("factions", []) if isinstance(actor_model.get("factions"), list) else []:
            self._append_entity_page(pages, project, faction, "faction", "03_Factions", as_of_chapter, snapshot_id, source_refs)

        space_model = snapshot.get("space_model") if isinstance(snapshot.get("space_model"), dict) else {}
        for region in space_model.get("regions", []) if isinstance(space_model.get("regions"), list) else []:
            self._append_structured_page(pages, project, region, "region", "02_Map/Regions", as_of_chapter, snapshot_id, source_refs)
        for node in space_model.get("nodes", []) if isinstance(space_model.get("nodes"), list) else []:
            self._append_structured_page(pages, project, node, "node", "02_Map/Nodes", as_of_chapter, snapshot_id, source_refs)

        institution_model = snapshot.get("institution_model") if isinstance(snapshot.get("institution_model"), dict) else {}
        for profile in institution_model.get("profiles", []) if isinstance(institution_model.get("profiles"), list) else []:
            self._append_structured_page(pages, project, profile, "institution", "05_Systems/Institutions", as_of_chapter, snapshot_id, source_refs)
        economy_model = snapshot.get("economy_model") if isinstance(snapshot.get("economy_model"), dict) else {}
        for profile in economy_model.get("profiles", []) if isinstance(economy_model.get("profiles"), list) else []:
            self._append_structured_page(pages, project, profile, "resource", "05_Systems/Economy", as_of_chapter, snapshot_id, source_refs)

        plot_model = snapshot.get("plot_model") if isinstance(snapshot.get("plot_model"), dict) else {}
        for index, promise in enumerate(plot_model.get("reader_promises", []) if isinstance(plot_model.get("reader_promises"), list) else [], start=1):
            self._append_text_page(pages, project, f"promise:{index}", "promise", f"Reader Promise {index}", "07_Promises", str(promise), as_of_chapter, snapshot_id, source_refs)
        for secret in plot_model.get("secrets", []) if isinstance(plot_model.get("secrets"), list) else []:
            self._append_structured_page(pages, project, secret, "secret", "08_Secrets", as_of_chapter, snapshot_id, source_refs)

        quality_model = snapshot.get("quality_model") if isinstance(snapshot.get("quality_model"), dict) else {}
        for conflict in quality_model.get("contradictions", []) if isinstance(quality_model.get("contradictions"), list) else []:
            title = str(conflict.get("subject_key") or conflict.get("conflict_type") or "Conflict")
            self._append_text_page(pages, project, f"conflict:{_slug(title)}", "contradiction", title, "09_Contradictions", str(conflict.get("description", "")), as_of_chapter, snapshot_id, source_refs)
        return pages

    def _append_entity_page(
        self,
        pages: list[dict[str, Any]],
        project: Project,
        entity: dict[str, Any],
        page_type: str,
        folder: str,
        as_of_chapter: int,
        snapshot_id: str,
        source_refs: list[dict[str, Any]],
    ) -> None:
        name = str(entity.get("name", "") or "").strip()
        if not name:
            return
        page_key = f"{page_type}:{entity.get('id') or _slug(name)}"
        fm = {
            "forwin_id": page_key,
            "project_id": project.id,
            "page_type": page_type,
            "status": "canon_live",
            "as_of_chapter": as_of_chapter,
            "snapshot_id": snapshot_id,
            "projection_source_refs": [ref.get("source_type", "") + ":" + ref.get("source_id", "") for ref in source_refs[:8] if isinstance(ref, dict)],
            "canon_evidence_refs": list(entity.get("source_refs", [])) if isinstance(entity.get("source_refs"), list) else [],
            "locked_fields": ["Canon Summary", "Current State", "Evidence"],
        }
        if str(entity.get("source", "")) == "book_state":
            fm.update(
                {
                    "projection_source": "book_state",
                    "node_id": str(entity.get("id") or ""),
                    "node_type": page_type,
                    "editable_fields": ["Manual Notes", "Human Questions", "Proposed Correction"],
                }
            )
        fm["source_refs"] = list(dict.fromkeys([*fm["projection_source_refs"], *fm["canon_evidence_refs"]]))
        state = entity.get("current_state") if isinstance(entity.get("current_state"), dict) else {}
        markdown = _page_markdown(
            frontmatter=fm,
            title=name,
            canon_summary=str(entity.get("description", "") or state.get("summary", "") or f"{name} 是 {page_type}。"),
            current_state=json.dumps(state, ensure_ascii=False, indent=2) if state else "",
            relationships=[],
            plot_usage=[str(state.get("role") or state.get("agenda") or "")] if state else [],
            evidence=[f"entity:{entity.get('id', '')}", f"as_of_chapter:{as_of_chapter}"],
        )
        pages.append(
            {
                "project_id": project.id,
                "page_key": page_key,
                "page_type": page_type,
                "title": name,
                "vault_path": f"{folder}/{_safe_filename(name)}.md",
                "markdown": markdown,
                "frontmatter": fm,
                "as_of_chapter": as_of_chapter,
            }
        )

    def _append_structured_page(
        self,
        pages: list[dict[str, Any]],
        project: Project,
        payload: dict[str, Any],
        page_type: str,
        folder: str,
        as_of_chapter: int,
        snapshot_id: str,
        source_refs: list[dict[str, Any]],
    ) -> None:
        title = str(payload.get("name") or payload.get("title") or payload.get("id") or page_type).strip()
        page_key = f"{page_type}:{payload.get('id') or _slug(title)}"
        fm = {
            "forwin_id": page_key,
            "project_id": project.id,
            "page_type": page_type,
            "status": "canon_live",
            "as_of_chapter": as_of_chapter,
            "snapshot_id": snapshot_id,
            "source_refs": [ref.get("source_type", "") + ":" + ref.get("source_id", "") for ref in source_refs[:8] if isinstance(ref, dict)],
            "locked_fields": ["Canon Summary", "Current State", "Evidence"],
        }
        markdown = _page_markdown(
            frontmatter=fm,
            title=title,
            canon_summary=str(payload.get("summary") or payload.get("description") or json.dumps(payload, ensure_ascii=False)),
            current_state=json.dumps(payload, ensure_ascii=False, indent=2),
            evidence=[f"genesis:{page_type}:{payload.get('id', title)}"],
        )
        pages.append(
            {
                "project_id": project.id,
                "page_key": page_key,
                "page_type": page_type,
                "title": title,
                "vault_path": f"{folder}/{_safe_filename(title)}.md",
                "markdown": markdown,
                "frontmatter": fm,
                "as_of_chapter": as_of_chapter,
            }
        )

    def _append_text_page(
        self,
        pages: list[dict[str, Any]],
        project: Project,
        page_key: str,
        page_type: str,
        title: str,
        folder: str,
        text: str,
        as_of_chapter: int,
        snapshot_id: str,
        source_refs: list[dict[str, Any]],
    ) -> None:
        fm = {
            "forwin_id": page_key,
            "project_id": project.id,
            "page_type": page_type,
            "status": "canon_live",
            "as_of_chapter": as_of_chapter,
            "snapshot_id": snapshot_id,
            "source_refs": [ref.get("source_type", "") + ":" + ref.get("source_id", "") for ref in source_refs[:8] if isinstance(ref, dict)],
            "locked_fields": ["Canon Summary", "Evidence"],
        }
        pages.append(
            {
                "project_id": project.id,
                "page_key": page_key,
                "page_type": page_type,
                "title": title,
                "vault_path": f"{folder}/{_safe_filename(title)}.md",
                "markdown": _page_markdown(frontmatter=fm, title=title, canon_summary=text),
                "frontmatter": fm,
                "as_of_chapter": as_of_chapter,
            }
        )

    @staticmethod
    def _build_links(snapshot: dict[str, Any]) -> list[tuple[str, str, str, list[dict[str, Any]]]]:
        links: list[tuple[str, str, str, list[dict[str, Any]]]] = []
        actor_model = snapshot.get("actor_model") if isinstance(snapshot.get("actor_model"), dict) else {}
        character_keys = {
            str(item.get("name", "")): f"character:{item.get('id') or _slug(str(item.get('name', '')))}"
            for item in actor_model.get("characters", [])
            if isinstance(item, dict)
        }
        faction_keys = {
            str(item.get("name", "")): f"faction:{item.get('id') or _slug(str(item.get('name', '')))}"
            for item in actor_model.get("factions", [])
            if isinstance(item, dict)
        }
        all_keys = {**character_keys, **faction_keys}
        for edge in actor_model.get("relationship_edges", []) if isinstance(actor_model.get("relationship_edges"), list) else []:
            if not isinstance(edge, dict):
                continue
            source_key = all_keys.get(str(edge.get("source_name", "")))
            target_key = all_keys.get(str(edge.get("target_name", "")))
            if source_key and target_key:
                links.append((source_key, target_key, str(edge.get("relation_type", "related")), []))
        return links
