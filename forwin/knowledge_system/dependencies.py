"""Semantic dependencies captured from the same inputs used to render a page."""

from __future__ import annotations

import json
from collections import defaultdict
from hashlib import sha256

DEPENDENCY_MANIFEST_VERSION = 2


def semantic(value):
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(k): semantic(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [semantic(v) for v in value]
    if isinstance(value, set):
        return sorted(semantic(v) for v in value)
    return value


def fingerprint(value):
    return sha256(
        json.dumps(
            semantic(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()


class DependencyPreparation:
    """Immutable-build dependency material shared by page proofs."""

    def __init__(self, runtime) -> None:
        self.runtime = runtime
        cognition = {
            str(key): semantic(vars(view))
            for key, view in runtime.cognition_by_observer.items()
        }
        self.cognition_fingerprint = fingerprint(cognition)
        self._world_nodes: dict[str, object] = {}
        self._world_edge_ids_by_node: dict[str, list[str]] = defaultdict(list)
        for edge_id, edge in runtime.world.edges_by_id.items():
            self._world_edge_ids_by_node[edge.source_id].append(edge_id)
            if edge.target_id != edge.source_id:
                self._world_edge_ids_by_node[edge.target_id].append(edge_id)
        self._map_edge_ids_by_node: dict[str, list[str]] = defaultdict(list)
        for edge_id, edge in runtime.map.edges_by_id.items():
            self._map_edge_ids_by_node[edge.from_node_id].append(edge_id)
            if edge.to_node_id != edge.from_node_id:
                self._map_edge_ids_by_node[edge.to_node_id].append(edge_id)

    def world_node(self, node_id: str):
        if node_id in self._world_nodes:
            return self._world_nodes[node_id]
        node = self.runtime.world.nodes_by_id.get(node_id)
        if node is None:
            return None
        materialized = node.model_copy(
            update={"state": self.runtime.world.get_state(node_id)}
        )
        self._world_nodes[node_id] = materialized
        return materialized

    def world_nodes(self) -> dict[str, object]:
        for node_id in self.runtime.world.nodes_by_id:
            self.world_node(node_id)
        return dict(self._world_nodes)

    def world_edges_for(self, node_id: str) -> dict[str, object]:
        return {
            edge_id: self.runtime.world.edges_by_id[edge_id]
            for edge_id in self._world_edge_ids_by_node.get(node_id, ())
            if edge_id in self.runtime.world.edges_by_id
        }

    def map_edges_for(self, node_id: str) -> dict[str, object]:
        return {
            edge_id: self.runtime.map.edges_by_id[edge_id]
            for edge_id in self._map_edge_ids_by_node.get(node_id, ())
            if edge_id in self.runtime.map.edges_by_id
        }


def prepare_dependencies(runtime) -> DependencyPreparation:
    return DependencyPreparation(runtime)


def page_dependencies(
    runtime,
    *,
    scope,
    node_id="",
    extra=None,
    preparation: DependencyPreparation | None = None,
):
    preparation = preparation or prepare_dependencies(runtime)
    if preparation.runtime is not runtime:
        raise ValueError("dependency preparation belongs to a different runtime")
    if scope == "node":
        node = preparation.world_node(node_id)
        if node is None:
            return {}
        payload = {
            "node": node,
            "edges": preparation.world_edges_for(node_id),
        }
    elif scope == "map_node":
        node = runtime.map.nodes_by_id.get(node_id)
        if node is None:
            return {}
        payload = {
            "node": node,
            "edges": preparation.map_edges_for(node_id),
        }
    elif scope in {"book", "llm_kb"}:
        payload = {
            "world_nodes": preparation.world_nodes(),
            "edges": runtime.world.edges_by_id,
            "facts": runtime.world.facts_by_id,
            "map_nodes": runtime.map.nodes_by_id,
            "map_edges": runtime.map.edges_by_id,
            "narrative": runtime.narrative.snapshot(),
            "extra": extra,
        }
    else:
        return {}
    return {
        "version": DEPENDENCY_MANIFEST_VERSION,
        "scope": scope,
        "node_id": node_id,
        "fingerprint": fingerprint(
            {
                "inputs": payload,
                "cognition_fingerprint": preparation.cognition_fingerprint,
            }
        ),
    }


def dependencies_valid(
    manifest,
    runtime,
    *,
    extra=None,
    preparation: DependencyPreparation | None = None,
):
    if (
        not isinstance(manifest, dict)
        or manifest.get("version") != DEPENDENCY_MANIFEST_VERSION
    ):
        return False
    return bool(manifest) and manifest == page_dependencies(
        runtime,
        scope=manifest.get("scope"),
        node_id=manifest.get("node_id", ""),
        extra=extra,
        preparation=preparation,
    )


def llm_kb_inputs(session, project_id, as_of):
    from forwin.retrieval.source_identity import fresh_orm_reads

    with fresh_orm_reads(session):
        return _llm_kb_inputs(session, project_id, as_of)


def _llm_kb_inputs(session, project_id, as_of):
    from forwin.book_state.repository import BookStateRepository

    repo = BookStateRepository(session)
    from sqlalchemy import select

    from forwin.models.world_contract import (
        ArcWorldContractRow,
        ChapterWorldDeltaIntentRow,
    )

    system_audit_columns = {
        ArcWorldContractRow.__tablename__: {"created_at", "updated_at"},
        ChapterWorldDeltaIntentRow.__tablename__: {"created_at", "updated_at"},
    }
    contracts = {}
    for model in (ArcWorldContractRow, ChapterWorldDeltaIntentRow):
        contracts[model.__tablename__] = [
            {
                column.name: getattr(row, column.name)
                for column in model.__table__.columns
                if column.name not in system_audit_columns[model.__tablename__]
            }
            for row in session.scalars(
                select(model)
                .where(model.project_id == project_id)
                .order_by(model.id)
                .execution_options(populate_existing=True)
            )
        ]
    return {
        "role_contracts": contracts,
        "native_promises": repo.list_reader_promises_native(
            project_id, as_of_chapter=as_of
        ),
        "deltas": repo.list_graph_deltas(
            project_id, after_chapter=-1, through_chapter=as_of
        ),
    }
