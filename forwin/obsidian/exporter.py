from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from forwin.book_state.projection import BookStateProjection
from forwin.book_state.repository import BookStateRepository
from forwin.models.project import Project
from forwin.protocol.book_state import FactNode, MapEdge, MapNode, WorldEdge, WorldNode
from forwin.world_model.store import WorldModelStore

from .canvas import write_canvas
from .frontmatter import EDITABLE_FIELDS, LOCKED_FIELDS, parse_sections, render_page


DEFAULT_VAULT_ROOT = Path("data/world_vaults")


@dataclass
class ObsidianExportResult:
    project_id: str
    vault_root: str
    exported_count: int = 0
    pages: list[str] = field(default_factory=list)
    as_of_chapter: int = 0


class ObsidianExporter:
    """BookState-backed Obsidian vault projection.

    The vault is a projection only: all generated canon sections come from
    BookState, while editable sections are preserved for import as proposals.
    """

    def __init__(self, session: Session) -> None:
        self.session = session
        self.repo = BookStateRepository(session)
        self.store = WorldModelStore(session)

    def export_project(
        self,
        project_id: str,
        *,
        vault_root: Path | None = None,
        as_of_chapter: int = 0,
    ) -> ObsidianExportResult:
        root = vault_root or DEFAULT_VAULT_ROOT / project_id
        root.mkdir(parents=True, exist_ok=True)
        self._ensure_dirs(root)
        as_of = self._resolve_as_of(project_id, as_of_chapter)
        runtime = BookStateProjection(self.session).load_runtime_as_of(project_id, as_of_chapter=as_of)

        page_paths: list[str] = []
        relationship_edges: list[tuple[str, str, str]] = []
        map_edges: list[tuple[str, str, str]] = []

        self._write_rules(root)
        project = self.session.get(Project, project_id)
        index_path = self._write_index(root, project_id, project.title if project else project_id, as_of)
        page_paths.append(index_path)

        book_pages = self._write_book_pages(root, project_id, as_of, runtime)
        page_paths.extend(book_pages)

        node_page_by_id: dict[str, str] = {}
        for node in sorted(runtime.world.nodes_by_id.values(), key=lambda item: (str(item.node_type), item.id)):
            rel_path = self._node_relpath(node)
            node_page_by_id[node.id] = rel_path
            page_paths.append(self._write_node_page(root, project_id, rel_path, node, runtime.world.edges_by_id, as_of))

        map_page_by_id: dict[str, str] = {}
        for node in sorted(runtime.map.nodes_by_id.values(), key=lambda item: item.id):
            rel_path = self._map_node_relpath(node)
            map_page_by_id[node.id] = rel_path
            page_paths.append(self._write_map_node_page(root, project_id, rel_path, node, runtime.map.edges_by_id, as_of))

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

        write_canvas(root / "03_Actors" / "Relationship_Canvas.canvas", page_paths=sorted(node_page_by_id.values()), edges=relationship_edges)
        write_canvas(root / "02_Map" / "Map_Canvas.canvas", page_paths=sorted(map_page_by_id.values()), edges=map_edges)

        return ObsidianExportResult(
            project_id=project_id,
            vault_root=str(root),
            exported_count=len(page_paths),
            pages=page_paths,
            as_of_chapter=as_of,
        )

    def _resolve_as_of(self, project_id: str, requested: int) -> int:
        if requested and requested > 0:
            return int(requested)
        snapshot = self.repo.latest_world_snapshot(project_id, 1_000_000_000)
        if snapshot is not None:
            return int(snapshot.as_of_chapter or 0)
        deltas = self.repo.list_graph_deltas(project_id, after_chapter=-1, through_chapter=1_000_000_000)
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
            (root / rel).mkdir(parents=True, exist_ok=True)

    def _write_rules(self, root: Path) -> None:
        (root / "AGENTS.md").write_text(
            "\n".join(
                [
                    "# ForWin Obsidian Vault Rules",
                    "",
                    "DB / BookState canon is the only source of truth.",
                    "Generated canon sections are locked.",
                    "Manual Notes, Human Questions, and Proposed Correction are editable.",
                    "Import creates proposals only; it never writes canon directly.",
                    "",
                ]
            ),
            encoding="utf-8",
        )

    def _write_index(self, root: Path, project_id: str, title: str, as_of_chapter: int) -> str:
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
        self._write_page(root, rel_path, "ForWin Knowledge Index", frontmatter, sections, page_type="book")
        return rel_path

    def _write_book_pages(self, root: Path, project_id: str, as_of_chapter: int, runtime) -> list[str]:
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
                    "Open Questions": self._open_questions(runtime.world.nodes_by_id.values(), runtime.world.facts_by_id.values()),
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
            self._write_page(root, rel_path, title, frontmatter, sections, page_type="book")
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
            edge for edge in edges_by_id.values()
            if edge.source_id == node.id or edge.target_id == node.id
        ]
        frontmatter = self._frontmatter(
            project_id=project_id,
            node_id=node.id,
            node_type=str(node.node_type),
            as_of_chapter=as_of_chapter,
            visibility=node.metadata.get("reader_visibility", node.metadata.get("visibility", "reader_known")),
            truth_relation=node.metadata.get("truth_relation", "true"),
            source_refs=node.source_refs or [f"book_state:node:{node.id}"],
        )
        sections = {
            "Canon Summary": node.summary or node.description or node.name or node.id,
            "Current State": _format_mapping(node.state or node.profile or node.metadata),
            "Relationships": "\n".join(_format_edge(edge, node.id) for edge in related_edges) or "_none_",
            "Reader Visibility": str(frontmatter["visibility"]),
            "Open Questions": _format_open_questions(node),
            "Evidence": "\n".join(f"- {ref}" for ref in frontmatter["source_refs"]) or f"- book_state:node:{node.id}",
            "Manual Notes": "",
            "Human Questions": "",
            "Proposed Correction": "",
        }
        self._write_page(root, rel_path, node.name or node.id, frontmatter, sections, page_type=str(node.node_type))
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
            edge for edge_id, edge in edges_by_id.items()
            if "__reverse" not in edge_id and (edge.from_node_id == node.id or edge.to_node_id == node.id)
        ]
        frontmatter = self._frontmatter(
            project_id=project_id,
            node_id=node.id,
            node_type=f"map_{node.node_type}",
            as_of_chapter=as_of_chapter,
            visibility=node.visibility_default if hasattr(node, "visibility_default") else "reader_known",
            source_refs=[f"book_state:map_node:{node.id}"],
        )
        sections = {
            "Canon Summary": node.description or node.name or node.id,
            "Current State": _format_mapping(node.model_dump(mode="json")),
            "Relationships": "\n".join(_format_map_edge(edge, node.id) for edge in related_edges) or "_none_",
            "Reader Visibility": str(frontmatter["visibility"]),
            "Open Questions": "_none_",
            "Evidence": f"- book_state:map_node:{node.id}",
            "Manual Notes": "",
            "Human Questions": "",
            "Proposed Correction": "",
        }
        self._write_page(root, rel_path, node.name or node.id, frontmatter, sections, page_type="map_node")
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
        path = root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            current_sections = parse_sections(path.read_text(encoding="utf-8"))
            for field_name in EDITABLE_FIELDS:
                if current_sections.get(field_name, "").strip() and not sections.get(field_name, "").strip():
                    sections[field_name] = current_sections[field_name]
        markdown = render_page(frontmatter, title, sections)
        path.write_text(markdown, encoding="utf-8")
        self.store.upsert_page(
            project_id=frontmatter.get("project_id", ""),
            page_key=frontmatter.get("forwin_id", rel_path),
            page_type=page_type,
            title=title,
            vault_path=rel_path,
            markdown=markdown,
            frontmatter=frontmatter,
            as_of_chapter=int(frontmatter.get("as_of_chapter", 0) or 0),
        )

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
        return f"{directory}/{name}_{node.id}.md"

    def _map_node_relpath(self, node: MapNode) -> str:
        directory = "02_Map/Nodes"
        if str(node.node_type) == "world_area":
            directory = "02_Map/SubWorlds"
        elif str(node.node_type) == "region":
            directory = "02_Map/Regions"
        return f"{directory}/{_slug(node.name or node.id)}_{node.id}.md"

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

    def _open_questions(self, nodes: list[WorldNode] | Any, facts: list[FactNode] | Any) -> str:
        lines = []
        for node in nodes:
            if node.node_type == "knowledge_gap":
                lines.append(f"- {node.name or node.id}: {node.summary or node.description}")
        for fact in facts:
            if fact.truth_value in {"unknown", "disputed"}:
                lines.append(f"- {fact.proposition}")
        return "\n".join(lines) or "_none_"

    def _ledger_sections(self, project_id: str, as_of_chapter: int, runtime) -> dict[str, str]:
        promises = [
            node for node in runtime.world.nodes_by_id.values()
            if node.node_type == "reader_promise"
        ]
        return {
            "Canon Summary": "Reader promise ledger derived from BookState nodes.",
            "Current State": "\n".join(f"- {node.name or node.id}: {node.summary or node.description}" for node in promises) or "_none_",
            "Relationships": "_promise links are on each node page_",
            "Reader Visibility": "writer-safe unless a promise node is marked hidden in canon metadata.",
            "Open Questions": "_none_",
            "Evidence": f"- book_state:reader_promises:{project_id}:{as_of_chapter}",
            "Manual Notes": "",
            "Human Questions": "",
            "Proposed Correction": "",
        }


def _slug(value: str) -> str:
    text = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", value.strip(), flags=re.UNICODE).strip("_")
    return text[:80] or "untitled"


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
    other = edge.to_node_id if edge.from_node_id == current_node_id else edge.from_node_id
    return f"- {direction} {edge.edge_type} {other} ({edge.status}, time={edge.travel_time}, risk={edge.risk_level})"


def _format_open_questions(node: WorldNode) -> str:
    questions = node.metadata.get("open_questions") if isinstance(node.metadata, dict) else None
    if isinstance(questions, list) and questions:
        return "\n".join(f"- {item}" for item in questions)
    if node.node_type in {"secret", "knowledge_gap"}:
        return node.summary or node.description or node.name or node.id
    return "_none_"
