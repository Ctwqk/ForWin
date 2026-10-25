from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    select,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func


from .base import Base, new_id


def _row_sequence(table_name):
    def next_value(context):
        connection = context.connection
        if connection.dialect.name == "postgresql":
            return connection.scalar(select(func.nextval(func.pg_get_serial_sequence(table_name, "seq"))))
        # SQLite serializes writers; reserve each default in this INSERT batch as well.
        counters = getattr(context, "_book_state_sequences", None)
        if counters is None:
            counters = context._book_state_sequences = {}
        if table_name not in counters:
            table = Base.metadata.tables[table_name]
            counters[table_name] = connection.scalar(select(func.coalesce(func.max(table.c.seq), 0)))
        counters[table_name] += 1
        return counters[table_name]
    return next_value


class WorldNodeRow(Base):
    __tablename__ = "world_nodes"
    __table_args__ = (
        Index("ix_world_nodes_project_type", "project_id", "node_type"),
        Index("ix_world_nodes_project_active", "project_id", "is_active"),
        Index("ix_world_nodes_project_name", "project_id", "name"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String, ForeignKey("projects.id"), nullable=False)
    node_type: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(Text, default="")
    aliases_json: Mapped[str] = mapped_column(Text, default="[]")
    summary: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String, default="active")
    importance: Mapped[int] = mapped_column(Integer, default=5)
    scope: Mapped[str] = mapped_column(String, default="")
    tags_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at_chapter: Mapped[int] = mapped_column(Integer, default=0)
    retired_at_chapter: Mapped[int | None] = mapped_column(Integer, nullable=True)
    valid_from_chapter: Mapped[int] = mapped_column(Integer, default=0)
    valid_until_chapter: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_refs_json: Mapped[str] = mapped_column(Text, default="[]")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    profile_json: Mapped[str] = mapped_column(Text, default="{}")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())


class WorldNodeStateRow(Base):
    __tablename__ = "world_node_states"
    __table_args__ = (
        Index("ix_world_node_states_project_node_chapter", "project_id", "node_id", "as_of_chapter"),
        Index("ix_world_node_states_project_chapter", "project_id", "as_of_chapter"),
        Index("ix_world_node_states_project_type", "project_id", "node_type"),
    )

    seq: Mapped[int] = mapped_column(BigInteger, Identity(), default=_row_sequence("world_node_states"))
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String, ForeignKey("projects.id"), nullable=False)
    node_id: Mapped[str] = mapped_column(String, nullable=False)
    node_type: Mapped[str] = mapped_column(String, default="")
    name: Mapped[str] = mapped_column(Text, default="", server_default="")
    aliases_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    profile_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true()
    )
    as_of_chapter: Mapped[int] = mapped_column(Integer, default=0)
    as_of_story_time: Mapped[str] = mapped_column(Text, default="")
    state_json: Mapped[str] = mapped_column(Text, default="{}")
    source_delta_id: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class CharacterIdentityMapRow(Base):
    __tablename__ = "character_identity_map"
    __table_args__ = (
        Index("ix_character_identity_project_canonical", "project_id", "canonical_character_id"),
        Index("ix_character_identity_project_book_node", "project_id", "book_state_node_id"),
        Index("ix_character_identity_project_genesis", "project_id", "genesis_ref_id"),
        Index("ix_character_identity_project_status", "project_id", "status"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String, ForeignKey("projects.id"), nullable=False)
    canonical_character_id: Mapped[str] = mapped_column(String, nullable=False, default="")
    book_state_node_id: Mapped[str] = mapped_column(String, nullable=False, default="")
    genesis_ref_id: Mapped[str] = mapped_column(String, nullable=False, default="")
    aliases_json: Mapped[str] = mapped_column(Text, default="[]")
    display_name: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String, default="active")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())


class WorldEdgeRow(Base):
    __tablename__ = "world_edges"
    __table_args__ = (
        Index("ix_world_edges_project_source", "project_id", "source_id"),
        Index("ix_world_edges_project_target", "project_id", "target_id"),
        Index("ix_world_edges_project_type", "project_id", "edge_type"),
        Index("ix_world_edges_project_family", "project_id", "edge_family"),
        Index("ix_world_edges_project_active", "project_id", "is_active"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String, ForeignKey("projects.id"), nullable=False)
    source_id: Mapped[str] = mapped_column(String, nullable=False)
    target_id: Mapped[str] = mapped_column(String, nullable=False)
    edge_type: Mapped[str] = mapped_column(String, nullable=False)
    edge_family: Mapped[str] = mapped_column(String, nullable=False)
    directionality: Mapped[str] = mapped_column(String, default="directed")
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    established_at_chapter: Mapped[int] = mapped_column(Integer, default=0)
    ended_at_chapter: Mapped[int | None] = mapped_column(Integer, nullable=True)
    valid_from_chapter: Mapped[int] = mapped_column(Integer, default=0)
    valid_until_chapter: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String, default="active")
    visibility: Mapped[str] = mapped_column(String, default="")
    truth_relation: Mapped[str] = mapped_column(String, default="true")
    visibility_default: Mapped[str] = mapped_column(String, default="visible")
    source_refs_json: Mapped[str] = mapped_column(Text, default="[]")
    state_json: Mapped[str] = mapped_column(Text, default="{}")
    evidence_refs_json: Mapped[str] = mapped_column(Text, default="[]")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())


class FactNodeRow(Base):
    __tablename__ = "fact_nodes"
    __table_args__ = (
        Index("ix_fact_nodes_project_type", "project_id", "fact_type"),
        Index("ix_fact_nodes_project_truth", "project_id", "truth_value"),
        Index("ix_fact_nodes_project_sensitivity", "project_id", "sensitivity_level"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String, ForeignKey("projects.id"), nullable=False)
    proposition: Mapped[str] = mapped_column(Text, nullable=False)
    fact_type: Mapped[str] = mapped_column(String, default="")
    truth_value: Mapped[str] = mapped_column(String, default="true")
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    related_node_refs_json: Mapped[str] = mapped_column(Text, default="[]")
    related_edge_refs_json: Mapped[str] = mapped_column(Text, default="[]")
    source_refs_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at_chapter: Mapped[int] = mapped_column(Integer, default=0)
    happened_at_story_time: Mapped[str] = mapped_column(Text, default="")
    contradiction_refs_json: Mapped[str] = mapped_column(Text, default="[]")
    sensitivity_level: Mapped[str] = mapped_column(String, default="")
    narrative_function: Mapped[str] = mapped_column(Text, default="")
    state_json: Mapped[str] = mapped_column(Text, default="{}")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())


class GraphDeltaRow(Base):
    __tablename__ = "graph_deltas"
    __table_args__ = (
        Index("ix_graph_deltas_project_chapter", "project_id", "chapter_number"),
        Index("ix_graph_deltas_project_type", "project_id", "delta_type"),
        Index("ix_graph_deltas_project_line", "project_id", "world_line_id"),
        Index("ix_graph_deltas_project_tick", "project_id", "tick_id"),
        Index("ix_graph_deltas_project_beat_sheet", "project_id", "beat_sheet_id"),
    )

    seq: Mapped[int] = mapped_column(BigInteger, Identity(), default=_row_sequence("graph_deltas"))
    id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(String, ForeignKey("projects.id"), nullable=False)
    chapter_number: Mapped[int] = mapped_column(Integer, default=0)
    story_time: Mapped[str] = mapped_column(Text, default="")
    story_time_start: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    story_time_end: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    delta_type: Mapped[str] = mapped_column(String, default="world_state")
    operation: Mapped[str] = mapped_column(String, default="")
    target_type: Mapped[str] = mapped_column(String, default="")
    target_id: Mapped[str] = mapped_column(String, default="")
    source_type: Mapped[str] = mapped_column(String, default="")
    source_id: Mapped[str] = mapped_column(String, default="")
    world_line_id: Mapped[str] = mapped_column(String, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    evidence_refs_json: Mapped[str] = mapped_column(Text, default="[]")
    review_verdict_id: Mapped[str] = mapped_column(String, default="")
    allowed_for_canon: Mapped[bool] = mapped_column(Boolean, default=True)
    tick_id: Mapped[str | None] = mapped_column(String, nullable=True)
    beat_sheet_id: Mapped[str | None] = mapped_column(String, nullable=True)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class GraphDeltaPatchRow(Base):
    __tablename__ = "graph_delta_patches"
    __table_args__ = (
        Index("ix_graph_delta_patches_project_delta", "project_id", "delta_id"),
        Index("ix_graph_delta_patches_project_target", "project_id", "target_ref"),
        Index("ix_graph_delta_patches_project_type", "project_id", "patch_type"),
        Index("ix_graph_delta_patches_project_chapter", "project_id", "chapter_number"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String, ForeignKey("projects.id"), nullable=False)
    delta_id: Mapped[str] = mapped_column(String, nullable=False)
    chapter_number: Mapped[int] = mapped_column(Integer, default=0)
    patch_type: Mapped[str] = mapped_column(String, nullable=False)
    target_ref: Mapped[str] = mapped_column(Text, default="")
    op: Mapped[str] = mapped_column(String, nullable=False)
    field_path: Mapped[str] = mapped_column(Text, default="")
    old_value_json: Mapped[str] = mapped_column(Text, default="null")
    new_value_json: Mapped[str] = mapped_column(Text, default="null")
    reason: Mapped[str] = mapped_column(Text, default="")
    visibility_default: Mapped[str] = mapped_column(String, default="visible")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class CognitionOverlayRow(Base):
    __tablename__ = "cognition_overlays"
    __table_args__ = (
        Index("ix_cognition_overlays_project_observer", "project_id", "observer_type", "observer_id", "as_of_chapter"),
        Index("ix_cognition_overlays_project_chapter", "project_id", "as_of_chapter"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String, ForeignKey("projects.id"), nullable=False)
    observer_type: Mapped[str] = mapped_column(String, nullable=False)
    observer_id: Mapped[str] = mapped_column(String, nullable=False)
    as_of_chapter: Mapped[int] = mapped_column(Integer, default=0)
    as_of_story_time: Mapped[str] = mapped_column(Text, default="")
    visible_refs_json: Mapped[str] = mapped_column(Text, default="[]")
    hidden_refs_json: Mapped[str] = mapped_column(Text, default="[]")
    suspected_refs_json: Mapped[str] = mapped_column(Text, default="[]")
    confirmed_refs_json: Mapped[str] = mapped_column(Text, default="[]")
    field_overrides_json: Mapped[str] = mapped_column(Text, default="{}")
    false_nodes_json: Mapped[str] = mapped_column(Text, default="{}")
    false_edges_json: Mapped[str] = mapped_column(Text, default="{}")
    false_facts_json: Mapped[str] = mapped_column(Text, default="{}")
    evidence_by_ref_json: Mapped[str] = mapped_column(Text, default="{}")
    knowledge_by_ref_json: Mapped[str] = mapped_column(
        Text, default="{}", server_default="{}"
    )
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())


class CognitionOverlayPatchRow(Base):
    __tablename__ = "cognition_overlay_patches"
    __table_args__ = (
        Index("ix_cognition_overlay_patches_project_overlay", "project_id", "overlay_id"),
        Index("ix_cognition_overlay_patches_project_observer", "project_id", "observer_type", "observer_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String, ForeignKey("projects.id"), nullable=False)
    overlay_id: Mapped[str] = mapped_column(String, default="")
    observer_type: Mapped[str] = mapped_column(String, nullable=False)
    observer_id: Mapped[str] = mapped_column(String, nullable=False)
    delta_id: Mapped[str] = mapped_column(String, default="")
    op: Mapped[str] = mapped_column(String, nullable=False)
    field_path: Mapped[str] = mapped_column(Text, default="")
    old_value_json: Mapped[str] = mapped_column(Text, default="null")
    new_value_json: Mapped[str] = mapped_column(Text, default="null")
    reason: Mapped[str] = mapped_column(Text, default="")
    evidence_refs_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class WorldSnapshotRow(Base):
    __tablename__ = "world_snapshots"
    __table_args__ = (
        Index("ix_world_snapshots_project_chapter", "project_id", "as_of_chapter"),
    )

    seq: Mapped[int] = mapped_column(BigInteger, Identity(), default=_row_sequence("world_snapshots"))
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String, ForeignKey("projects.id"), nullable=False)
    as_of_chapter: Mapped[int] = mapped_column(Integer, default=0)
    as_of_story_time: Mapped[str] = mapped_column(Text, default="")
    base_snapshot_id: Mapped[str] = mapped_column(String, default="")
    objective_graph_digest: Mapped[str] = mapped_column(String, default="")
    map_graph_digest: Mapped[str] = mapped_column(String, default="")
    reader_overlay_digest: Mapped[str] = mapped_column(String, default="")
    character_overlay_digests_json: Mapped[str] = mapped_column(Text, default="{}")
    world_node_state_index_json: Mapped[str] = mapped_column(Text, default="{}")
    narrative_node_index_json: Mapped[str] = mapped_column(
        Text, default="{}", server_default="{}"
    )
    narrative_edge_index_json: Mapped[str] = mapped_column(
        Text, default="{}", server_default="{}"
    )
    active_edge_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    active_fact_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    active_world_line_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    open_gap_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    active_promise_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    objective_state_summary: Mapped[str] = mapped_column(Text, default="")
    reader_state_summary: Mapped[str] = mapped_column(Text, default="")
    source_delta_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    built_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class MapSnapshotRow(Base):
    __tablename__ = "map_snapshots"
    __table_args__ = (
        Index("ix_map_snapshots_project_chapter", "project_id", "as_of_chapter"),
    )

    seq: Mapped[int] = mapped_column(BigInteger, Identity(), default=_row_sequence("map_snapshots"))
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String, ForeignKey("projects.id"), nullable=False)
    as_of_chapter: Mapped[int] = mapped_column(Integer, default=0)
    map_node_index_json: Mapped[str] = mapped_column(Text, default="{}")
    map_edge_index_json: Mapped[str] = mapped_column(Text, default="{}")
    blocked_edge_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    hidden_edge_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    built_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class BookCognitionSnapshotRow(Base):
    __tablename__ = "book_cognition_snapshots"
    __table_args__ = (
        Index("ix_book_cognition_snapshots_project_observer", "project_id", "observer_type", "observer_id", "as_of_chapter"),
    )

    seq: Mapped[int] = mapped_column(BigInteger, Identity(), default=_row_sequence("book_cognition_snapshots"))
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String, ForeignKey("projects.id"), nullable=False)
    observer_type: Mapped[str] = mapped_column(String, nullable=False)
    observer_id: Mapped[str] = mapped_column(String, nullable=False)
    as_of_chapter: Mapped[int] = mapped_column(Integer, default=0)
    overlay_id: Mapped[str] = mapped_column(String, default="")
    visible_refs_json: Mapped[str] = mapped_column(Text, default="[]")
    suspected_refs_json: Mapped[str] = mapped_column(Text, default="[]")
    confirmed_refs_json: Mapped[str] = mapped_column(Text, default="[]")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    built_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class NarrativeNodeRow(Base):
    __tablename__ = "narrative_nodes"
    __table_args__ = (
        Index("ix_narrative_nodes_project_type", "project_id", "node_type"),
        Index("ix_narrative_nodes_project_status", "project_id", "status"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String, ForeignKey("projects.id"), nullable=False)
    node_type: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String, default="active")
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())


class NarrativeEdgeRow(Base):
    __tablename__ = "narrative_edges"
    __table_args__ = (
        Index("ix_narrative_edges_project_source", "project_id", "source_id"),
        Index("ix_narrative_edges_project_target", "project_id", "target_id"),
        Index("ix_narrative_edges_project_type", "project_id", "edge_type"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String, ForeignKey("projects.id"), nullable=False)
    source_id: Mapped[str] = mapped_column(String, nullable=False)
    target_id: Mapped[str] = mapped_column(String, nullable=False)
    edge_type: Mapped[str] = mapped_column(String, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
