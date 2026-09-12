from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat as stat_module
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterator
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.book_state.repository import BookStateRepository
from forwin.book_state.visibility import node_page_visibility
from forwin.knowledge_system.page_repository import KnowledgePageRepository
from forwin.knowledge_system.store import KnowledgeProjectionStore
from forwin.models.project import Project
from forwin.protocol.book_state import FactNode, MapEdge, MapNode, WorldEdge, WorldNode

from .canvas import render_canvas
from .frontmatter import EDITABLE_FIELDS, LOCKED_FIELDS, parse_sections, render_page


DEFAULT_VAULT_ROOT = Path("data/world_vaults")
OBSIDIAN_PROJECTION_VERSION = "obsidian_v2"
OBSIDIAN_MANIFEST_FILENAME = ".forwin-projection-manifest.json"
OBSIDIAN_MANIFEST_SCHEMA_VERSION = 1
_EXPECTED_CURRENT_UNSET = object()
_DIRECTORY_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_FILE_READ_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)


@dataclass
class ObsidianExportResult:
    project_id: str
    vault_root: str
    exported_count: int = 0
    pages: list[str] = field(default_factory=list)
    as_of_chapter: int = 0
    source_digest: str = ""
    manifest_written: bool = False
    deletion_enabled: bool = False
    deletion_disabled_reason: str = ""
    deleted_files: list[str] = field(default_factory=list)
    retained_human_modified: list[str] = field(default_factory=list)
    retained_unsafe: list[str] = field(default_factory=list)
    retired_page_count: int = 0


@dataclass(frozen=True, slots=True)
class ManagedProjectionManifest:
    project_id: str
    as_of_chapter: int
    files: dict[str, dict[str, str]]


@dataclass(frozen=True, slots=True)
class ManagedManifestState:
    manifest: ManagedProjectionManifest | None
    disabled_reason: str = ""

    @property
    def deletion_enabled(self) -> bool:
        return self.manifest is not None


@dataclass(frozen=True, slots=True)
class ManagedFileConvergence:
    source_digest: str
    manifest_written: bool
    deletion_enabled: bool
    deletion_disabled_reason: str
    deleted_files: tuple[str, ...]
    retained_human_modified: tuple[str, ...]
    retained_unsafe: tuple[str, ...]


class ObsidianExporter:
    """BookState-backed Obsidian vault projection.

    The vault is a projection only: all generated canon sections come from
    BookState, while editable sections are preserved and human-indexed.
    """

    def __init__(self, session: Session) -> None:
        self.session = session
        self.repo = BookStateRepository(session)
        self.store = KnowledgeProjectionStore(session)
        self._emitted_page_ids: set[str] = set()

    def export_project(
        self,
        project_id: str,
        *,
        vault_root: Path | None = None,
        as_of_chapter: int = 0,
    ) -> ObsidianExportResult:
        root = vault_root or DEFAULT_VAULT_ROOT / project_id
        root.mkdir(parents=True, exist_ok=True)
        manifest_state = load_managed_projection_manifest(root, project_id)
        as_of = self._resolve_as_of(project_id, as_of_chapter)
        from forwin.retrieval.source_identity import CanonReadBaseline
        self._read_baseline = CanonReadBaseline.capture(self.session, project_id, as_of_chapter=as_of)
        from forwin.book_state.query import BookStateQuery
        runtime = BookStateQuery(self.session, baseline=self._read_baseline).runtime(
            project_id, as_of_chapter=as_of
        )
        self._read_baseline.assert_current(self.session)
        self._dependency_runtime = runtime
        self._dependency_project_title = self.session.scalar(select(Project.title).where(Project.id == project_id))
        world_nodes = sorted(
            [node.model_copy(update={"state": runtime.world.get_state(node.id)}) for node in runtime.world.nodes_by_id.values()],
            key=lambda item: (str(item.node_type), item.id),
        )
        map_nodes = sorted(runtime.map.nodes_by_id.values(), key=lambda item: item.id)
        node_page_by_id = {
            node.id: self._node_relpath(node) for node in world_nodes
        }
        map_page_by_id = {
            node.id: self._map_node_relpath(node) for node in map_nodes
        }
        relationship_canvas = "03_Actors/Relationship_Canvas.canvas"
        map_canvas = "02_Map/Map_Canvas.canvas"
        _require_unique_managed_paths(
            [
                "AGENTS.md",
                "00_Index.md",
                "01_Book/Current_State.md",
                "01_Book/Reader_Promise_Ledger.md",
                relationship_canvas,
                map_canvas,
                *node_page_by_id.values(),
                *map_page_by_id.values(),
            ]
        )
        self._emitted_page_ids = set()
        self._ensure_dirs(root)

        page_paths: list[str] = []
        relationship_edges: list[tuple[str, str, str]] = []
        map_edges: list[tuple[str, str, str]] = []

        self._write_rules(root)
        project = self.session.get(Project, project_id)
        index_path = self._write_index(
            root, project_id, project.title if project else project_id, as_of
        )
        page_paths.append(index_path)

        book_pages = self._write_book_pages(root, project_id, as_of, runtime)
        page_paths.extend(book_pages)

        for node in world_nodes:
            rel_path = node_page_by_id[node.id]
            page_paths.append(
                self._write_node_page(
                    root, project_id, rel_path, node, runtime.world.edges_by_id, as_of
                )
            )

        for node in map_nodes:
            rel_path = map_page_by_id[node.id]
            page_paths.append(
                self._write_map_node_page(
                    root, project_id, rel_path, node, runtime.map.edges_by_id, as_of
                )
            )

        for edge in runtime.world.edges_by_id.values():
            source = node_page_by_id.get(edge.source_id)
            target = node_page_by_id.get(edge.target_id)
            if source and target:
                relationship_edges.append((source, target, edge.edge_type))
        for edge_id, edge in runtime.map.edges_by_id.items():
            if "__reverse" in edge_id:
                continue
            source = map_page_by_id.get(edge.from_node_id)
            target = map_page_by_id.get(edge.to_node_id)
            if source and target:
                map_edges.append((source, target, str(edge.edge_type)))

        write_managed_text_if_changed(
            root,
            relationship_canvas,
            render_canvas(
                page_paths=sorted(node_page_by_id.values()),
                edges=relationship_edges,
            ),
        )
        write_managed_text_if_changed(
            root,
            map_canvas,
            render_canvas(
                page_paths=sorted(map_page_by_id.values()),
                edges=map_edges,
            ),
        )

        managed_paths = {
            "AGENTS.md",
            relationship_canvas,
            map_canvas,
            *page_paths,
        }
        convergence = converge_managed_projection_files(
            root,
            project_id=project_id,
            as_of_chapter=as_of,
            desired_paths=managed_paths,
            prior_state=manifest_state,
        )
        retired_page_count = KnowledgePageRepository(
            self.session
        ).retire_missing_projection_pages(
            project_id,
            projection_kind="obsidian",
            active_page_ids=set(self._emitted_page_ids),
        )

        return ObsidianExportResult(
            project_id=project_id,
            vault_root=str(root),
            exported_count=len(page_paths),
            pages=page_paths,
            as_of_chapter=as_of,
            source_digest=convergence.source_digest,
            manifest_written=convergence.manifest_written,
            deletion_enabled=convergence.deletion_enabled,
            deletion_disabled_reason=convergence.deletion_disabled_reason,
            deleted_files=list(convergence.deleted_files),
            retained_human_modified=list(
                convergence.retained_human_modified
            ),
            retained_unsafe=list(convergence.retained_unsafe),
            retired_page_count=retired_page_count,
        )

    def _resolve_as_of(self, project_id: str, requested: int) -> int:
        if requested and requested > 0:
            return int(requested)
        snapshot = self.repo.latest_world_snapshot(project_id, 1_000_000_000)
        if snapshot is not None:
            return int(snapshot.as_of_chapter or 0)
        deltas = self.repo.list_graph_deltas(
            project_id, after_chapter=-1, through_chapter=1_000_000_000
        )
        if deltas:
            return max(delta.chapter_number for delta in deltas)
        return 0

    def _ensure_dirs(self, root: Path) -> None:
        for rel in [
            "01_Book",
            "02_Map/SubWorlds",
            "02_Map/Regions",
            "02_Map/Nodes",
            "02_Map/Routes",
            "03_Actors/Characters",
            "03_Actors/Factions",
            "03_Actors/Organizations",
            "03_Actors/Families",
            "04_Systems",
            "05_Plot/Arcs",
            "05_Plot/Threads",
            "05_Plot/Chapter_Status",
            "06_Secrets/Knowledge_Gaps",
            "07_Reader",
            "08_Conflicts",
            "09_LLM_KB",
        ]:
            ensure_managed_directory(root, rel)

    def _write_rules(self, root: Path) -> None:
        content = "\n".join(
            [
                "# ForWin Obsidian Vault Rules",
                "",
                "DB / BookState canon is the only source of truth.",
                "Generated canon sections are locked.",
                "Manual Notes, Human Questions, and Proposed Correction are editable.",
                "Editable sections are preserved and human-indexed.",
                "Canon changes require an explicit generic proposal; the vault has no reverse sync.",
                "",
            ]
        )
        write_managed_text_if_changed(
            root,
            "AGENTS.md",
            content,
        )

    def _write_index(
        self, root: Path, project_id: str, title: str, as_of_chapter: int
    ) -> str:
        frontmatter = self._frontmatter(
            project_id=project_id,
            node_id="book:index",
            node_type="book",
            as_of_chapter=as_of_chapter,
            source_refs=[f"book_state:snapshot:{as_of_chapter}"],
        )
        sections = {
            "Canon Summary": f"{title}\n\nBookState canon projection as of chapter {as_of_chapter}.",
            "Current State": "- [[01_Book/Current_State]]\n- [[01_Book/Reader_Promise_Ledger]]\n- [[02_Map/Map_Canvas.canvas]]\n- [[03_Actors/Relationship_Canvas.canvas]]",
            "Relationships": "_index_",
            "Reader Visibility": "Generated pages mark reader visibility from BookState cognition overlays when available.",
            "Open Questions": "_see 06_Secrets and 07_Reader_",
            "Evidence": f"- book_state:snapshot:{as_of_chapter}",
            "Manual Notes": "",
            "Human Questions": "",
            "Proposed Correction": "",
        }
        rel_path = "00_Index.md"
        self._write_page(
            root,
            rel_path,
            "ForWin Knowledge Index",
            frontmatter,
            sections,
            page_type="book",
        )
        return rel_path

    def _write_book_pages(
        self, root: Path, project_id: str, as_of_chapter: int, runtime
    ) -> list[str]:
        pages: list[tuple[str, str, dict[str, str]]] = [
            (
                "01_Book/Current_State.md",
                "Current State",
                {
                    "Canon Summary": f"BookState runtime as of chapter {as_of_chapter}.",
                    "Current State": (
                        f"- World nodes: {len(runtime.world.nodes_by_id)}\n"
                        f"- World edges: {len(runtime.world.edges_by_id)}\n"
                        f"- Facts: {len(runtime.world.facts_by_id)}\n"
                        f"- Map nodes: {len(runtime.map.nodes_by_id)}"
                    ),
                    "Relationships": "_see generated actor pages_",
                    "Reader Visibility": self._reader_visibility(runtime),
                    "Open Questions": self._open_questions(
                        runtime.world.nodes_by_id.values(),
                        runtime.world.facts_by_id.values(),
                    ),
                    "Evidence": f"- book_state:snapshot:{as_of_chapter}",
                    "Manual Notes": "",
                    "Human Questions": "",
                    "Proposed Correction": "",
                },
            ),
            (
                "01_Book/Reader_Promise_Ledger.md",
                "Reader Promise Ledger",
                self._ledger_sections(project_id, as_of_chapter, runtime),
            ),
        ]
        rel_paths: list[str] = []
        for rel_path, title, sections in pages:
            frontmatter = self._frontmatter(
                project_id=project_id,
                node_id=f"book:{_slug(title)}",
                node_type="book",
                as_of_chapter=as_of_chapter,
                source_refs=[f"book_state:snapshot:{as_of_chapter}"],
            )
            self._write_page(
                root, rel_path, title, frontmatter, sections, page_type="book"
            )
            rel_paths.append(rel_path)
        return rel_paths

    def _write_node_page(
        self,
        root: Path,
        project_id: str,
        rel_path: str,
        node: WorldNode,
        edges_by_id: dict[str, WorldEdge],
        as_of_chapter: int,
    ) -> str:
        related_edges = [
            edge
            for edge in edges_by_id.values()
            if edge.source_id == node.id or edge.target_id == node.id
        ]
        frontmatter = self._frontmatter(
            project_id=project_id,
            node_id=node.id,
            node_type=str(node.node_type),
            as_of_chapter=as_of_chapter,
            visibility=node_page_visibility(node),
            truth_relation=node.metadata.get("truth_relation", "true"),
            source_refs=node.source_refs or [f"book_state:node:{node.id}"],
        )
        sections = {
            "Canon Summary": node.summary or node.description or node.name or node.id,
            "Current State": _format_mapping(
                node.state
            ),
            "Relationships": "\n".join(
                _format_edge(edge, node.id) for edge in related_edges
            )
            or "_none_",
            "Reader Visibility": str(frontmatter["visibility"]),
            "Open Questions": _format_open_questions(node),
            "Evidence": "\n".join(f"- {ref}" for ref in frontmatter["source_refs"])
            or f"- book_state:node:{node.id}",
            "Manual Notes": "",
            "Human Questions": "",
            "Proposed Correction": "",
        }
        self._write_page(
            root,
            rel_path,
            node.name or node.id,
            frontmatter,
            sections,
            page_type=str(node.node_type),
        )
        return rel_path

    def _write_map_node_page(
        self,
        root: Path,
        project_id: str,
        rel_path: str,
        node: MapNode,
        edges_by_id: dict[str, MapEdge],
        as_of_chapter: int,
    ) -> str:
        related_edges = [
            edge
            for edge_id, edge in edges_by_id.items()
            if "__reverse" not in edge_id
            and (edge.from_node_id == node.id or edge.to_node_id == node.id)
        ]
        frontmatter = self._frontmatter(
            project_id=project_id,
            node_id=node.id,
            node_type=f"map_{node.node_type}",
            as_of_chapter=as_of_chapter,
            visibility=node_page_visibility(node),
            source_refs=[f"book_state:map_node:{node.id}"],
        )
        sections = {
            "Canon Summary": node.description or node.name or node.id,
            "Current State": _format_mapping(node.model_dump(mode="json")),
            "Relationships": "\n".join(
                _format_map_edge(edge, node.id) for edge in related_edges
            )
            or "_none_",
            "Reader Visibility": str(frontmatter["visibility"]),
            "Open Questions": "_none_",
            "Evidence": f"- book_state:map_node:{node.id}",
            "Manual Notes": "",
            "Human Questions": "",
            "Proposed Correction": "",
        }
        self._write_page(
            root,
            rel_path,
            node.name or node.id,
            frontmatter,
            sections,
            page_type="map_node",
        )
        return rel_path

    def _write_page(
        self,
        root: Path,
        rel_path: str,
        title: str,
        frontmatter: dict[str, Any],
        sections: dict[str, str],
        *,
        page_type: str,
    ) -> None:
        current_markdown = read_managed_text(root, rel_path)
        if current_markdown is not None:
            current_sections = parse_sections(current_markdown)
            for field_name in EDITABLE_FIELDS:
                if (
                    current_sections.get(field_name, "").strip()
                    and not sections.get(field_name, "").strip()
                ):
                    sections[field_name] = current_sections[field_name]
        from forwin.knowledge_system.dependencies import page_dependencies
        self._read_baseline.assert_current(self.session)
        scope = "book" if page_type == "book" else "map_node" if page_type == "map_node" else "node"
        dependency_manifest = page_dependencies(self._dependency_runtime, scope=scope, node_id="" if scope == "book" else str(frontmatter.get("node_id") or ""), extra={"project_title": self._dependency_project_title} if scope == "book" else None)
        source_digest = _page_source_digest(frontmatter, sections)
        section_digest = _section_digest(sections)
        frontmatter = {
            **frontmatter,
            "projection_version": OBSIDIAN_PROJECTION_VERSION,
            "source_digest": source_digest,
        }
        markdown = render_page(frontmatter, title, sections)
        write_managed_text_if_changed(
            root,
            rel_path,
            markdown,
            expected_current=current_markdown,
        )
        row = self.store.upsert_page(
            project_id=frontmatter.get("project_id", ""),
            page_key=frontmatter.get("forwin_id", rel_path),
            page_type=page_type,
            title=title,
            vault_path=rel_path,
            markdown=markdown,
            frontmatter=frontmatter,
            as_of_chapter=int(frontmatter.get("as_of_chapter", 0) or 0),
            projection_kind="obsidian",
            projection_version=OBSIDIAN_PROJECTION_VERSION,
            source_digest=source_digest,
            dependency_manifest=dependency_manifest,
            section_digest=section_digest,
            observer_type="reader",
            observer_id="reader",
            role_scope="human",
            visibility_scope=str(frontmatter.get("visibility", "")),
            canon_status="canon_projection",
        )
        self._emitted_page_ids.add(row.id)

    def _frontmatter(
        self,
        *,
        project_id: str,
        node_id: str,
        node_type: str,
        as_of_chapter: int,
        visibility: str = "reader_known",
        truth_relation: str = "true",
        source_refs: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            "forwin_id": f"{node_type}:{node_id}",
            "project_id": project_id,
            "node_id": node_id,
            "node_type": node_type,
            "status": "canon_live",
            "as_of_chapter": int(as_of_chapter or 0),
            "visibility": visibility,
            "truth_relation": truth_relation,
            "source_refs": source_refs or [],
            "locked_fields": LOCKED_FIELDS,
            "editable_fields": EDITABLE_FIELDS,
            "projection_version": OBSIDIAN_PROJECTION_VERSION,
            "source_digest": "",
        }

    def _node_relpath(self, node: WorldNode) -> str:
        node_type = str(node.node_type)
        name = _slug(node.name or node.id)
        directories = {
            "character": "03_Actors/Characters",
            "faction": "03_Actors/Factions",
            "organization": "03_Actors/Organizations",
            "family": "03_Actors/Families",
            "subworld": "02_Map/SubWorlds",
            "region": "02_Map/Regions",
            "location": "02_Map/Nodes",
            "rule": "04_Systems",
            "institution": "04_Systems",
            "technology": "04_Systems",
            "magic_system": "04_Systems",
            "thread": "05_Plot/Threads",
            "event": "05_Plot/Chapter_Status",
            "secret": "06_Secrets",
            "knowledge_gap": "06_Secrets/Knowledge_Gaps",
            "reader_promise": "07_Reader",
            "conflict": "08_Conflicts",
            "contract": "05_Plot",
        }
        directory = directories.get(node_type, "01_Book")
        filename = f"{name}_{_filename_id(node.id)}.md"
        if directory.startswith("02_Map/"):
            filename = f"World_{filename}"
        return f"{directory}/{filename}"

    def _map_node_relpath(self, node: MapNode) -> str:
        directory = "02_Map/Nodes"
        if str(node.node_type) == "world_area":
            directory = "02_Map/SubWorlds"
        elif str(node.node_type) == "region":
            directory = "02_Map/Regions"
        return (
            f"{directory}/Map_{_slug(node.name or node.id)}_"
            f"{_filename_id(node.id)}.md"
        )

    def _reader_visibility(self, runtime) -> str:
        reader = runtime.cognition_by_observer.get(("reader", "reader"))
        if reader is None:
            return "Reader cognition overlay is not materialized."
        return (
            f"- Visible refs: {len(reader.visible_refs)}\n"
            f"- Suspected refs: {len(reader.suspected_refs)}\n"
            f"- Confirmed refs: {len(reader.confirmed_refs)}\n"
            f"- Hidden refs: {len(reader.hidden_refs)}"
        )

    def _open_questions(
        self, nodes: list[WorldNode] | Any, facts: list[FactNode] | Any
    ) -> str:
        lines = []
        for node in nodes:
            if node.node_type == "knowledge_gap":
                lines.append(
                    f"- {node.name or node.id}: {node.summary or node.description}"
                )
        for fact in facts:
            if fact.truth_value in {"unknown", "disputed"}:
                lines.append(f"- {fact.proposition}")
        return "\n".join(lines) or "_none_"

    def _ledger_sections(
        self, project_id: str, as_of_chapter: int, runtime
    ) -> dict[str, str]:
        promises = [
            node
            for node in runtime.world.nodes_by_id.values()
            if node.node_type == "reader_promise"
        ]
        return {
            "Canon Summary": "Reader promise ledger derived from BookState nodes.",
            "Current State": "\n".join(
                f"- {node.name or node.id}: {node.summary or node.description}"
                for node in promises
            )
            or "_none_",
            "Relationships": "_promise links are on each node page_",
            "Reader Visibility": "writer-safe unless a promise node is marked hidden in canon metadata.",
            "Open Questions": "_none_",
            "Evidence": f"- book_state:reader_promises:{project_id}:{as_of_chapter}",
            "Manual Notes": "",
            "Human Questions": "",
            "Proposed Correction": "",
        }


def load_managed_projection_manifest(
    root: Path,
    project_id: str,
) -> ManagedManifestState:
    try:
        manifest_text = read_managed_text(root, OBSIDIAN_MANIFEST_FILENAME)
    except (OSError, ValueError):
        return ManagedManifestState(None, "unsafe_manifest_path")
    if manifest_text is None:
        return ManagedManifestState(None, "missing_manifest")
    try:
        payload = json.loads(manifest_text)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return ManagedManifestState(None, "corrupt_manifest")
    if not isinstance(payload, dict):
        return ManagedManifestState(None, "corrupt_manifest")
    try:
        schema_version = int(payload.get("schema_version") or 0)
        as_of_chapter = int(payload.get("as_of_chapter") or 0)
    except (TypeError, ValueError):
        return ManagedManifestState(None, "corrupt_manifest")
    if (
        schema_version != OBSIDIAN_MANIFEST_SCHEMA_VERSION
        or str(payload.get("project_id") or "") != project_id
        or as_of_chapter < 0
    ):
        return ManagedManifestState(None, "invalid_manifest_identity")
    raw_files = payload.get("files")
    if not isinstance(raw_files, dict):
        return ManagedManifestState(None, "corrupt_manifest")
    files: dict[str, dict[str, str]] = {}
    for rel_path, raw_metadata in raw_files.items():
        if not isinstance(rel_path, str) or not isinstance(raw_metadata, dict):
            return ManagedManifestState(None, "corrupt_manifest")
        digest = str(raw_metadata.get("sha256") or "").strip().lower()
        kind = str(raw_metadata.get("kind") or "").strip()
        if not re.fullmatch(r"[0-9a-f]{64}", digest) or not kind:
            return ManagedManifestState(None, "corrupt_manifest")
        files[rel_path] = {"sha256": digest, "kind": kind}
    return ManagedManifestState(
        ManagedProjectionManifest(
            project_id=project_id,
            as_of_chapter=as_of_chapter,
            files=files,
        )
    )


def converge_managed_projection_files(
    root: Path,
    *,
    project_id: str,
    as_of_chapter: int,
    desired_paths: set[str],
    prior_state: ManagedManifestState,
) -> ManagedFileConvergence:
    desired_files: dict[str, dict[str, str]] = {}
    for raw_rel_path in sorted(desired_paths):
        rel_path = _managed_relative_path(raw_rel_path)
        desired_files[rel_path] = {
            "sha256": _sha256_managed_file(root, rel_path),
            "kind": _managed_file_kind(rel_path),
        }

    deleted_files: list[str] = []
    retained_human_modified: list[str] = []
    retained_unsafe: list[str] = []
    prior_manifest = prior_state.manifest
    if prior_manifest is not None:
        stale_paths = sorted(set(prior_manifest.files) - set(desired_files))
        for rel_path in stale_paths:
            if rel_path == OBSIDIAN_MANIFEST_FILENAME:
                retained_unsafe.append(rel_path)
                continue
            outcome = _delete_managed_file_if_unchanged(
                root,
                rel_path,
                prior_manifest.files[rel_path]["sha256"],
            )
            if outcome == "unsafe":
                retained_unsafe.append(rel_path)
            elif outcome == "modified":
                retained_human_modified.append(rel_path)
            elif outcome == "deleted":
                deleted_files.append(rel_path)

    manifest_payload = {
        "schema_version": OBSIDIAN_MANIFEST_SCHEMA_VERSION,
        "project_id": project_id,
        "as_of_chapter": int(as_of_chapter or 0),
        "files": desired_files,
    }
    manifest_text = json.dumps(
        manifest_payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    manifest_written = write_managed_text_if_changed(
        root,
        OBSIDIAN_MANIFEST_FILENAME,
        manifest_text,
    )
    source_digest = hashlib.sha256(
        json.dumps(
            manifest_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return ManagedFileConvergence(
        source_digest=source_digest,
        manifest_written=manifest_written,
        deletion_enabled=prior_state.deletion_enabled,
        deletion_disabled_reason=(
            "" if prior_state.deletion_enabled else prior_state.disabled_reason
        ),
        deleted_files=tuple(deleted_files),
        retained_human_modified=tuple(retained_human_modified),
        retained_unsafe=tuple(retained_unsafe),
    )


def ensure_managed_directory(root: Path | int, rel_path: str) -> None:
    normalized = _managed_relative_path(rel_path)
    directory_fd = _open_managed_root(root)
    try:
        for part in PurePosixPath(normalized).parts:
            try:
                os.mkdir(part, mode=0o777, dir_fd=directory_fd)
            except FileExistsError:
                pass
            child_fd = os.open(
                part,
                _DIRECTORY_OPEN_FLAGS,
                dir_fd=directory_fd,
            )
            os.close(directory_fd)
            directory_fd = child_fd
    finally:
        os.close(directory_fd)


def read_managed_text(root: Path | int, rel_path: str) -> str | None:
    with _managed_parent_fd(root, rel_path) as (parent_fd, name):
        current = _read_named_file(parent_fd, name)
    if current is None:
        return None
    content, _identity = current
    return content.decode("utf-8")


def write_managed_text_if_changed(
    root: Path | int,
    rel_path: str,
    content: str,
    *,
    expected_current: object = _EXPECTED_CURRENT_UNSET,
) -> bool:
    desired = content.encode("utf-8")
    with _managed_parent_fd(root, rel_path) as (parent_fd, name):
        current = _read_named_file(parent_fd, name)
        current_content = current[0] if current is not None else None
        current_identity = current[1] if current is not None else None
        if current_content == desired:
            return False
        if expected_current is not _EXPECTED_CURRENT_UNSET:
            expected_content = (
                None
                if expected_current is None
                else str(expected_current).encode("utf-8")
            )
            if current_content != expected_content:
                raise RuntimeError(
                    f"managed projection file changed concurrently: {rel_path}"
                )

        temp_name = _unused_sibling_name(parent_fd, prefix=".forwin-write-")
        temp_fd = os.open(
            temp_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o666,
            dir_fd=parent_fd,
        )
        try:
            _write_all_fd(temp_fd, desired)
            os.fsync(temp_fd)
        finally:
            os.close(temp_fd)
        try:
            latest_identity = _stat_named_file(parent_fd, name)
            if _file_identity(latest_identity) != _file_identity(current_identity):
                raise RuntimeError(
                    f"managed projection file changed concurrently: {rel_path}"
                )
            os.replace(
                temp_name,
                name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            temp_name = ""
            return True
        finally:
            if temp_name:
                try:
                    os.unlink(temp_name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass


def _sha256_managed_file(root: Path, rel_path: str) -> str:
    with _managed_parent_fd(root, rel_path) as (parent_fd, name):
        current = _read_named_file(parent_fd, name)
        if current is None:
            raise RuntimeError(
                f"managed projection output is missing: {rel_path}"
            )
        content, _identity = current
    return hashlib.sha256(content).hexdigest()


def _delete_managed_file_if_unchanged(
    root: Path,
    rel_path: str,
    expected_digest: str,
) -> str:
    try:
        with _managed_parent_fd(root, rel_path) as (parent_fd, name):
            current = _read_named_file(parent_fd, name)
            if current is None:
                return "missing"
            content, original_stat = current
            if hashlib.sha256(content).hexdigest() != expected_digest:
                return "modified"

            quarantine_name = _unused_sibling_name(
                parent_fd,
                prefix=".forwin-delete-",
            )
            try:
                os.rename(
                    name,
                    quarantine_name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
            except FileNotFoundError:
                return "missing"

            try:
                try:
                    quarantined = _read_named_file(parent_fd, quarantine_name)
                except ValueError:
                    outcome = "unsafe"
                else:
                    if quarantined is None:
                        raise RuntimeError(
                            "managed projection quarantine disappeared during delete"
                        )
                    moved_content, moved_stat = quarantined
                    if not _same_inode(moved_stat, original_stat):
                        outcome = "unsafe"
                    elif hashlib.sha256(moved_content).hexdigest() != expected_digest:
                        outcome = "modified"
                    else:
                        final_stat = _stat_named_file(parent_fd, quarantine_name)
                        if _file_identity(final_stat) != _file_identity(moved_stat):
                            outcome = "unsafe"
                        else:
                            os.unlink(quarantine_name, dir_fd=parent_fd)
                            return "deleted"

                _restore_quarantined_file(
                    parent_fd,
                    quarantine_name,
                    name,
                )
                return outcome
            except Exception:
                if _stat_named_file(parent_fd, quarantine_name) is not None:
                    _restore_quarantined_file(
                        parent_fd,
                        quarantine_name,
                        name,
                    )
                raise
    except FileNotFoundError:
        return "missing"
    except ValueError:
        return "unsafe"
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            return "unsafe"
        raise


def _open_managed_root(root: Path | int) -> int:
    # Descriptor callers retain directory identity across a rename or a path
    # replacement. Duplicate ownership so helpers never close their caller's FD.
    return (
        os.dup(root)
        if isinstance(root, int)
        else os.open(root.resolve(), _DIRECTORY_OPEN_FLAGS)
    )


@contextmanager
def _managed_parent_fd(root: Path | int, rel_path: str) -> Iterator[tuple[int, str]]:
    normalized = _managed_relative_path(rel_path)
    parts = PurePosixPath(normalized).parts
    directory_fd = _open_managed_root(root)
    try:
        for part in parts[:-1]:
            child_fd = os.open(
                part,
                _DIRECTORY_OPEN_FLAGS,
                dir_fd=directory_fd,
            )
            os.close(directory_fd)
            directory_fd = child_fd
        yield directory_fd, parts[-1]
    finally:
        os.close(directory_fd)


def _read_named_file(
    parent_fd: int,
    name: str,
) -> tuple[bytes, os.stat_result] | None:
    try:
        file_fd = os.open(name, _FILE_READ_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise ValueError("managed projection path is not a regular file") from exc
        raise
    try:
        file_stat = os.fstat(file_fd)
        if not stat_module.S_ISREG(file_stat.st_mode):
            raise ValueError("managed projection path is not a regular file")
        return _read_all_fd(file_fd), file_stat
    finally:
        os.close(file_fd)


def _read_all_fd(file_fd: int) -> bytes:
    os.lseek(file_fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while True:
        chunk = os.read(file_fd, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _write_all_fd(file_fd: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(file_fd, view)
        if written <= 0:
            raise OSError("managed projection write made no progress")
        view = view[written:]


def _stat_named_file(parent_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _file_identity(file_stat: os.stat_result | None) -> tuple[int, ...] | None:
    if file_stat is None:
        return None
    return (
        int(file_stat.st_dev),
        int(file_stat.st_ino),
        int(file_stat.st_mode),
        int(file_stat.st_size),
        int(file_stat.st_mtime_ns),
        int(file_stat.st_ctime_ns),
    )


def _same_inode(
    left: os.stat_result | None,
    right: os.stat_result | None,
) -> bool:
    if left is None or right is None:
        return left is right
    return (
        int(left.st_dev),
        int(left.st_ino),
        stat_module.S_IFMT(left.st_mode),
    ) == (
        int(right.st_dev),
        int(right.st_ino),
        stat_module.S_IFMT(right.st_mode),
    )


def _unused_sibling_name(parent_fd: int, *, prefix: str) -> str:
    for _attempt in range(8):
        name = f"{prefix}{uuid4().hex}.tmp"
        if _stat_named_file(parent_fd, name) is None:
            return name
    raise RuntimeError("could not allocate a managed projection sibling name")


def _restore_quarantined_file(
    parent_fd: int,
    quarantine_name: str,
    original_name: str,
) -> None:
    if _stat_named_file(parent_fd, original_name) is not None:
        raise RuntimeError(
            "managed projection file changed while a stale file was quarantined"
        )
    os.rename(
        quarantine_name,
        original_name,
        src_dir_fd=parent_fd,
        dst_dir_fd=parent_fd,
    )


def _require_unique_managed_paths(paths: list[str]) -> None:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for raw_path in paths:
        path = _managed_relative_path(raw_path)
        if path in seen:
            duplicates.add(path)
        seen.add(path)
    if duplicates:
        raise ValueError(
            "managed projection paths collide: " + ", ".join(sorted(duplicates))
        )


def _managed_relative_path(rel_path: str) -> str:
    raw = str(rel_path or "")
    if not raw or "\\" in raw or raw.startswith("/"):
        raise ValueError(f"invalid managed projection path: {raw!r}")
    raw_parts = raw.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise ValueError(f"invalid managed projection path: {raw!r}")
    return PurePosixPath(raw).as_posix()


def _managed_file_kind(rel_path: str) -> str:
    if rel_path == "AGENTS.md":
        return "vault_rules"
    if rel_path.endswith(".canvas"):
        return "canvas"
    if rel_path.endswith(".md"):
        return "page"
    return "generated_file"


def _slug(value: str) -> str:
    text = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", value.strip(), flags=re.UNICODE).strip(
        "_"
    )
    return text[:80] or "untitled"


def _filename_id(value: str) -> str:
    raw = str(value or "")
    slug = _slug(raw)
    if slug == raw and len(raw) <= 80 and raw not in {".", ".."}:
        return slug
    prefix = slug[:67].rstrip("._-") or "id"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def _format_mapping(payload: dict[str, Any]) -> str:
    if not payload:
        return "_empty_"
    lines = []
    for key in sorted(payload):
        value = payload[key]
        if isinstance(value, (dict, list)):
            value = str(value)
        lines.append(f"- {key}: {value}")
    return "\n".join(lines)


def _format_edge(edge: WorldEdge, current_node_id: str) -> str:
    direction = "->" if edge.source_id == current_node_id else "<-"
    other = edge.target_id if edge.source_id == current_node_id else edge.source_id
    return f"- {direction} {edge.edge_type} {other} ({edge.status})"


def _format_map_edge(edge: MapEdge, current_node_id: str) -> str:
    direction = "->" if edge.from_node_id == current_node_id else "<-"
    other = (
        edge.to_node_id if edge.from_node_id == current_node_id else edge.from_node_id
    )
    return f"- {direction} {edge.edge_type} {other} ({edge.status}, time={edge.travel_time}, risk={edge.risk_level})"


def _format_open_questions(node: WorldNode) -> str:
    questions = (
        node.metadata.get("open_questions") if isinstance(node.metadata, dict) else None
    )
    if isinstance(questions, list) and questions:
        return "\n".join(f"- {item}" for item in questions)
    if node.node_type in {"secret", "knowledge_gap"}:
        return node.summary or node.description or node.name or node.id
    return "_none_"


def _page_source_digest(frontmatter: dict[str, Any], sections: dict[str, str]) -> str:
    digest_frontmatter = {
        key: value for key, value in frontmatter.items() if key != "source_digest"
    }
    return _sha256_json(
        {
            "frontmatter": digest_frontmatter,
            "locked_sections": {key: sections.get(key, "") for key in LOCKED_FIELDS},
            "projection_version": OBSIDIAN_PROJECTION_VERSION,
        }
    )


def _section_digest(sections: dict[str, str]) -> dict[str, str]:
    return {
        key: hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()
        for key, value in sorted(sections.items())
    }


def _sha256_json(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
