"""Semantic dependencies captured from the same inputs used to render a page."""

from __future__ import annotations
import json
from hashlib import sha256


def semantic(value):
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        return {
            str(k): semantic(v)
            for k, v in value.items()
            if k not in {"created_at", "updated_at", "refreshed_at"}
        }
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


def page_dependencies(runtime, *, scope, node_id="", extra=None):
    cognition = {
        str(key): semantic(vars(view))
        for key, view in runtime.cognition_by_observer.items()
    }
    world_nodes = {
        key: node.model_copy(update={"state": runtime.world.get_state(key)})
        for key, node in runtime.world.nodes_by_id.items()
    }
    if scope == "node":
        node = world_nodes.get(node_id)
        if node is None:
            return {}
        payload = {
            "node": node,
            "edges": {
                key: edge
                for key, edge in runtime.world.edges_by_id.items()
                if node_id in (edge.source_id, edge.target_id)
            },
        }
    elif scope == "map_node":
        node = runtime.map.nodes_by_id.get(node_id)
        if node is None:
            return {}
        payload = {
            "node": node,
            "edges": {
                key: edge
                for key, edge in runtime.map.edges_by_id.items()
                if node_id in (edge.from_node_id, edge.to_node_id)
            },
        }
    elif scope in {"book", "llm_kb"}:
        payload = {
            "world_nodes": world_nodes,
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
        "version": 1,
        "scope": scope,
        "node_id": node_id,
        "fingerprint": fingerprint({"inputs": payload, "cognition": cognition}),
    }


def dependencies_valid(manifest, runtime, *, extra=None):
    if not isinstance(manifest, dict) or manifest.get("version") != 1:
        return False
    return bool(manifest) and manifest == page_dependencies(
        runtime,
        scope=manifest.get("scope"),
        node_id=manifest.get("node_id", ""),
        extra=extra,
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

    contracts = {}
    for model in (ArcWorldContractRow, ChapterWorldDeltaIntentRow):
        contracts[model.__tablename__] = [
            {
                column.name: getattr(row, column.name)
                for column in model.__table__.columns
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
