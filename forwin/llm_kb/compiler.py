from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy.orm import Session

from forwin.book_state.projection import BookStateProjection
from forwin.book_state.repository import BookStateRepository
from forwin.protocol.book_state import FactNode, GraphDelta, WorldNode
from forwin.retrieval.broker import RetrievalBroker
from forwin.state.repo import StateRepository

from .store import DEFAULT_LLM_KB_ROOT
from .vector_index import LLMKBVectorIndex


ROOT_MARKDOWN_FILES = [
    "CURRENT_STATE.md",
    "NEXT_CHAPTER_CONTEXT.md",
    "ACTIVE_THREADS.md",
    "CHARACTER_MEMORY.md",
    "FACTION_MEMORY.md",
    "MAP_CONTEXT.md",
    "READER_PROMISES.md",
    "KNOWLEDGE_GAPS.md",
    "REVEAL_LADDER.md",
    "MUST_NOT_REVEAL.md",
    "RECENT_CHANGES.md",
    "STYLE_AND_TONE.md",
    "CONSTRAINTS.md",
]


@dataclass
class LLMKBCompileResult:
    project_id: str
    root: str
    as_of_chapter: int
    files: list[str] = field(default_factory=list)
    source_digest: str = ""
    vector_index: dict[str, Any] = field(default_factory=dict)


class LLMKnowledgeBaseCompiler:
    """Compile a writer-safe Karpathy-style KB from BookState canon."""

    def __init__(
        self,
        session: Session,
        *,
        root: Path | None = None,
        qdrant_url: str | None = None,
        qdrant_collection: str | None = None,
        qdrant_client: Any | None = None,
        qdrant_models: Any | None = None,
    ) -> None:
        self.session = session
        self.root = root or DEFAULT_LLM_KB_ROOT
        self.qdrant_url = qdrant_url
        self.qdrant_collection = qdrant_collection
        self.qdrant_client = qdrant_client
        self.qdrant_models = qdrant_models
        self.repo = BookStateRepository(session)

    def rebuild(self, project_id: str, *, as_of_chapter: int = 0) -> LLMKBCompileResult:
        as_of = self._resolve_as_of(project_id, as_of_chapter)
        project_root = self.root / project_id
        project_root.mkdir(parents=True, exist_ok=True)
        (project_root / "packs").mkdir(exist_ok=True)
        runtime = BookStateProjection(self.session).load_runtime_as_of(project_id, as_of_chapter=as_of)
        nodes = list(runtime.world.nodes_by_id.values())
        facts = list(runtime.world.facts_by_id.values())
        safe_nodes = [node for node in nodes if not _hidden_node(node)]
        safe_facts = [fact for fact in facts if not _hidden_fact(fact)]
        deltas = [
            delta for delta in self.repo.list_graph_deltas(project_id, after_chapter=-1, through_chapter=as_of)
            if not _hidden_delta(delta)
        ]
        source_digest = _digest(
            {
                "as_of_chapter": as_of,
                "nodes": [node.model_dump(mode="json") for node in safe_nodes],
                "facts": [fact.model_dump(mode="json") for fact in safe_facts],
                "deltas": [delta.model_dump(mode="json") for delta in deltas[-20:]],
            }
        )
        common_refs = [f"book_state:snapshot:{as_of}", f"source_digest:{source_digest}"]

        files: list[str] = []
        markdown_by_key = {
            "CURRENT_STATE.md": self._current_state(as_of, source_digest, common_refs, safe_nodes, safe_facts),
            "NEXT_CHAPTER_CONTEXT.md": self._next_chapter_context(as_of, source_digest, common_refs, safe_nodes),
            "ACTIVE_THREADS.md": self._active_threads(as_of, source_digest, common_refs, safe_nodes),
            "CHARACTER_MEMORY.md": self._character_memory(as_of, source_digest, common_refs, safe_nodes),
            "FACTION_MEMORY.md": self._faction_memory(as_of, source_digest, common_refs, safe_nodes),
            "MAP_CONTEXT.md": self._map_context(as_of, source_digest, common_refs, runtime),
            "READER_PROMISES.md": self._reader_promises(project_id, as_of, source_digest, common_refs, safe_nodes),
            "KNOWLEDGE_GAPS.md": self._knowledge_gaps(as_of, source_digest, common_refs, safe_nodes, safe_facts),
            "REVEAL_LADDER.md": self._reveal_ladder(as_of, source_digest, common_refs, safe_nodes),
            "MUST_NOT_REVEAL.md": self._must_not_reveal(as_of, source_digest, common_refs, nodes, facts),
            "RECENT_CHANGES.md": self._recent_changes(as_of, source_digest, common_refs, deltas),
            "STYLE_AND_TONE.md": self._static_file(as_of, source_digest, common_refs, "Style And Tone", "Use the project's established prose constraints and accepted chapter voice."),
            "CONSTRAINTS.md": self._constraints(as_of, source_digest, common_refs, safe_nodes),
        }
        for key, content in markdown_by_key.items():
            (project_root / key).write_text(content, encoding="utf-8")
            files.append(key)

        jsonl_payloads = {
            "facts.jsonl": [_fact_record(fact, as_of, source_digest) for fact in safe_facts],
            "events.jsonl": [_node_record(node, as_of, source_digest) for node in safe_nodes if node.node_type == "event"],
            "graph_deltas.jsonl": [_delta_record(delta, as_of, source_digest) for delta in deltas],
            "open_questions.jsonl": [
                _node_record(node, as_of, source_digest)
                for node in safe_nodes
                if node.node_type in {"knowledge_gap", "secret"} and node.status not in {"closed", "resolved"}
            ],
        }
        for key, records in jsonl_payloads.items():
            (project_root / key).write_text(
                "".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in records),
                encoding="utf-8",
            )
            files.append(key)

        retrieval_index = {
            "project_id": project_id,
            "as_of_chapter": as_of,
            "source_digest": source_digest,
            "files": sorted(files),
            "root_policy": "writer_safe",
            "canon_source": "BookState DB canon",
        }
        (project_root / "retrieval_index.json").write_text(json.dumps(retrieval_index, ensure_ascii=False, indent=2), encoding="utf-8")
        files.append("retrieval_index.json")
        self._write_role_packs(project_id, as_of, project_root)
        vector_index = LLMKBVectorIndex(
            self.root,
            qdrant_url=self.qdrant_url,
            collection_name=self.qdrant_collection,
            qdrant_client=self.qdrant_client,
            qdrant_models=self.qdrant_models,
        ).rebuild_project(project_id, source_digest=source_digest)
        retrieval_index["vector_index"] = vector_index
        (project_root / "retrieval_index.json").write_text(json.dumps(retrieval_index, ensure_ascii=False, indent=2), encoding="utf-8")
        return LLMKBCompileResult(
            project_id=project_id,
            root=str(project_root),
            as_of_chapter=as_of,
            files=files,
            source_digest=source_digest,
            vector_index=vector_index,
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

    def _header(self, title: str, as_of: int, source_digest: str, source_refs: list[str]) -> str:
        refs = "\n".join(f"- {ref}" for ref in source_refs)
        return (
            f"# {title}\n\n"
            f"as_of_chapter: {as_of}\n"
            f"source_digest: {source_digest}\n"
            f"source_refs:\n{refs}\n\n"
        )

    def _current_state(self, as_of: int, digest: str, refs: list[str], nodes: list[WorldNode], facts: list[FactNode]) -> str:
        lines = [self._header("Current State", as_of, digest, refs), "## Public Canon Facts"]
        lines.extend(f"- {fact.proposition} [source_refs: {', '.join(fact.source_refs)}]" for fact in facts[:80])
        lines.append("\n## Active Public Nodes")
        lines.extend(f"- {node.node_type}:{node.id} {node.name}: {node.summary or node.description}" for node in nodes[:80])
        return "\n".join(lines).rstrip() + "\n"

    def _next_chapter_context(self, as_of: int, digest: str, refs: list[str], nodes: list[WorldNode]) -> str:
        active = [node for node in nodes if node.node_type in {"character", "location", "event", "thread"}][:30]
        lines = [self._header("Next Chapter Context", as_of, digest, refs), "## Available Public Context"]
        lines.extend(f"- {node.name or node.id}: {node.summary or node.description or node.state.get('state_summary', '')}" for node in active)
        lines.append("\n## Forbidden Reveal Policy")
        lines.append("Use MUST_NOT_REVEAL.md by id/ref only; do not infer hidden truth from it.")
        return "\n".join(lines).rstrip() + "\n"

    def _active_threads(self, as_of: int, digest: str, refs: list[str], nodes: list[WorldNode]) -> str:
        return self._node_section_file("Active Threads", as_of, digest, refs, [node for node in nodes if node.node_type in {"thread", "objective", "event"}])

    def _character_memory(self, as_of: int, digest: str, refs: list[str], nodes: list[WorldNode]) -> str:
        return self._node_section_file("Character Memory", as_of, digest, refs, [node for node in nodes if node.node_type == "character"])

    def _faction_memory(self, as_of: int, digest: str, refs: list[str], nodes: list[WorldNode]) -> str:
        return self._node_section_file("Faction Memory", as_of, digest, refs, [node for node in nodes if node.node_type in {"faction", "organization", "family"}])

    def _map_context(self, as_of: int, digest: str, refs: list[str], runtime) -> str:
        lines = [self._header("Map Context", as_of, digest, refs), "## Known Map Nodes"]
        for node in runtime.map.nodes_by_id.values():
            if getattr(node, "visibility_default", "visible") == "hidden" or node.status == "hidden":
                continue
            lines.append(f"- {node.id}: {node.name} ({node.node_type}) status={node.status}")
        lines.append("\n## Known Routes")
        for edge_id, edge in runtime.map.edges_by_id.items():
            if "__reverse" in edge_id or edge.status == "hidden" or not edge.discovered_by_default:
                continue
            lines.append(f"- {edge.from_node_id} -> {edge.to_node_id}: {edge.edge_type} time={edge.travel_time} risk={edge.risk_level}")
        return "\n".join(lines).rstrip() + "\n"

    def _reader_promises(self, project_id: str, as_of: int, digest: str, refs: list[str], nodes: list[WorldNode]) -> str:
        native_promises = self.repo.list_reader_promises_native(project_id, as_of_chapter=as_of)
        lines = [self._header("Reader Promises", as_of, digest, refs)]
        if native_promises:
            for promise in native_promises:
                lines.append(f"## {promise.summary or promise.promise_id}")
                lines.append(f"- id: {promise.promise_id}")
                lines.append(f"- type: {promise.promise_type}")
                lines.append(f"- status: {promise.status}")
                lines.append(f"- debt: {promise.current_debt_level}")
                lines.append(f"- reward_tags: {', '.join(promise.reward_tags)}")
                lines.append(f"- source_refs: {', '.join(promise.source_refs)}")
                lines.append("")
            return "\n".join(lines).rstrip() + "\n"
        return self._node_section_file("Reader Promises", as_of, digest, refs, [node for node in nodes if node.node_type == "reader_promise"])

    def _knowledge_gaps(self, as_of: int, digest: str, refs: list[str], nodes: list[WorldNode], facts: list[FactNode]) -> str:
        gaps = [node for node in nodes if node.node_type == "knowledge_gap"]
        lines = [self._header("Knowledge Gaps", as_of, digest, refs), "## Writer-safe Gaps"]
        lines.extend(f"- {node.id}: {node.summary or node.description or node.name}" for node in gaps)
        disputed = [fact for fact in facts if fact.truth_value in {"unknown", "disputed"}]
        lines.extend(f"- fact:{fact.id}: {fact.proposition}" for fact in disputed)
        return "\n".join(lines).rstrip() + "\n"

    def _reveal_ladder(self, as_of: int, digest: str, refs: list[str], nodes: list[WorldNode]) -> str:
        items = [node for node in nodes if node.node_type in {"secret", "knowledge_gap"}]
        lines = [self._header("Reveal Ladder", as_of, digest, refs), "## Planned Public Reveal Handles"]
        lines.extend(f"- {node.id}: {node.state.get('planned_reveal_window', '') or node.summary or node.name}" for node in items)
        return "\n".join(lines).rstrip() + "\n"

    def _must_not_reveal(self, as_of: int, digest: str, refs: list[str], nodes: list[WorldNode], facts: list[FactNode]) -> str:
        lines = [self._header("Must Not Reveal", as_of, digest, refs), "## Forbidden Refs"]
        hidden_refs = [f"node:{node.id}" for node in nodes if _hidden_node(node)]
        hidden_refs.extend(f"fact:{fact.id}" for fact in facts if _hidden_fact(fact))
        lines.extend(f"- {ref}" for ref in sorted(hidden_refs))
        if not hidden_refs:
            lines.append("_none_")
        return "\n".join(lines).rstrip() + "\n"

    def _recent_changes(self, as_of: int, digest: str, refs: list[str], deltas: list[GraphDelta]) -> str:
        lines = [self._header("Recent Changes", as_of, digest, refs), "## Recent Writer-safe GraphDeltas"]
        for delta in deltas[-20:]:
            lines.append(f"- {delta.id} ch={delta.chapter_number}: {delta.summary} [source_refs: {', '.join(delta.evidence_refs)}]")
        return "\n".join(lines).rstrip() + "\n"

    def _constraints(self, as_of: int, digest: str, refs: list[str], nodes: list[WorldNode]) -> str:
        return self._node_section_file("Constraints", as_of, digest, refs, [node for node in nodes if node.node_type in {"contract", "rule", "institution"}])

    def _static_file(self, as_of: int, digest: str, refs: list[str], title: str, body: str) -> str:
        return self._header(title, as_of, digest, refs) + body.rstrip() + "\n"

    def _node_section_file(self, title: str, as_of: int, digest: str, refs: list[str], nodes: Iterable[WorldNode]) -> str:
        lines = [self._header(title, as_of, digest, refs)]
        for node in nodes:
            lines.append(f"## {node.name or node.id}")
            lines.append(f"- id: {node.id}")
            lines.append(f"- type: {node.node_type}")
            lines.append(f"- summary: {node.summary or node.description or node.state.get('state_summary', '')}")
            lines.append(f"- source_refs: {', '.join(node.source_refs)}")
            lines.append("")
        if len(lines) == 1:
            lines.append("_empty_")
        return "\n".join(lines).rstrip() + "\n"

    def _write_role_packs(self, project_id: str, as_of: int, project_root: Path) -> None:
        broker = RetrievalBroker(
            llm_kb_root=self.root,
            llm_kb_qdrant_url=self.qdrant_url,
            llm_kb_qdrant_collection=self.qdrant_collection,
            llm_kb_qdrant_client=self.qdrant_client,
            llm_kb_qdrant_models=self.qdrant_models,
        )
        repo = StateRepository(self.session)
        role_map = {
            "writer": "writing",
            "reviewer": "review",
            "planner": "planning",
            "compiler": "compiler",
        }
        for role, pack_kind in role_map.items():
            role_root = project_root / "packs" / role
            role_root.mkdir(parents=True, exist_ok=True)
            try:
                pack = broker.build_world_model_pack(repo, project_id, as_of + 1, pack_kind)
                payload = pack.model_dump(mode="json")
            except Exception as exc:  # noqa: BLE001 - pack generation should not block root KB rebuild.
                payload = {"project_id": project_id, "role": role, "error": str(exc)}
            (role_root / "context.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _hidden_node(node: WorldNode) -> bool:
    tags = set(str(tag).lower() for tag in node.tags)
    metadata = node.metadata if isinstance(node.metadata, dict) else {}
    return (
        str(node.status).lower() in {"hidden", "secret", "must_not_reveal"}
        or tags.intersection({"hidden", "secret", "must_not_reveal"})
        or str(metadata.get("visibility", "")).lower() in {"hidden", "secret", "must_not_reveal"}
    )


def _hidden_fact(fact: FactNode) -> bool:
    return str(fact.sensitivity_level).lower() in {"hidden", "secret", "must_not_reveal"}


def _hidden_delta(delta: GraphDelta) -> bool:
    metadata = delta.metadata if isinstance(delta.metadata, dict) else {}
    marker = " ".join(str(metadata.get(key, "")) for key in ("visibility", "sensitivity", "sensitivity_level", "role"))
    return any(token in marker.lower() for token in ("hidden", "secret", "must_not_reveal"))


def _node_record(node: WorldNode, as_of: int, digest: str) -> dict[str, Any]:
    refs = node.source_refs or [f"book_state:node:{node.id}"]
    return {
        "id": node.id,
        "type": node.node_type,
        "name": node.name,
        "summary": node.summary or node.description,
        "source_refs": refs,
        "source_digest": _digest({"source_digest": digest, "id": node.id, "refs": refs}),
        "as_of_chapter": as_of,
    }


def _fact_record(fact: FactNode, as_of: int, digest: str) -> dict[str, Any]:
    refs = fact.source_refs or [f"book_state:fact:{fact.id}"]
    return {
        "id": fact.id,
        "fact_type": fact.fact_type,
        "proposition": fact.proposition,
        "truth_value": fact.truth_value,
        "source_refs": refs,
        "source_digest": _digest({"source_digest": digest, "id": fact.id, "refs": refs}),
        "as_of_chapter": as_of,
    }


def _delta_record(delta: GraphDelta, as_of: int, digest: str) -> dict[str, Any]:
    refs = delta.evidence_refs or [f"book_state:graph_delta:{delta.id}"]
    return {
        "id": delta.id,
        "chapter_number": delta.chapter_number,
        "delta_type": delta.delta_type,
        "operation": delta.operation,
        "target_type": delta.target_type,
        "target_id": delta.target_id,
        "summary": delta.summary,
        "source_refs": refs,
        "source_digest": _digest({"source_digest": digest, "id": delta.id, "refs": refs}),
        "as_of_chapter": as_of,
    }


def _digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

