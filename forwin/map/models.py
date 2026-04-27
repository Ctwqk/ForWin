from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from forwin.models.base import Base, new_id


class MapRegionRow(Base):
    __tablename__ = "map_regions"
    __table_args__ = (
        Index("ix_map_regions_project_subworld", "project_id", "subworld_id"),
        Index("ix_map_regions_project_type", "project_id", "region_type"),
        Index("ix_map_regions_project_status", "project_id", "status"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String, ForeignKey("projects.id"), nullable=False)
    subworld_id: Mapped[str] = mapped_column(String, ForeignKey("sub_worlds.id"), nullable=False)
    region_type: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(Text, default="")
    aliases_json: Mapped[str] = mapped_column(Text, default="[]")
    description: Mapped[str] = mapped_column(Text, default="")
    terrain: Mapped[str] = mapped_column(String, default="")
    culture_tag: Mapped[str] = mapped_column(String, default="")
    controlling_faction_id: Mapped[str] = mapped_column(String, default="")
    danger_level: Mapped[float] = mapped_column(Float, default=0.0)
    node_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    boundary_node_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    entry_node_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    status: Mapped[str] = mapped_column(String, default="active")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=func.now(),
        server_default=func.now(),
        onupdate=func.now(),
    )


class MapRegionEdgeRow(Base):
    __tablename__ = "map_region_edges"
    __table_args__ = (
        Index("ix_map_region_edges_project_subworld", "project_id", "subworld_id"),
        Index("ix_map_region_edges_project_from", "project_id", "from_region_id"),
        Index("ix_map_region_edges_project_to", "project_id", "to_region_id"),
        Index("ix_map_region_edges_project_status", "project_id", "status"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String, ForeignKey("projects.id"), nullable=False)
    subworld_id: Mapped[str] = mapped_column(String, ForeignKey("sub_worlds.id"), nullable=False)
    from_region_id: Mapped[str] = mapped_column(String, nullable=False)
    to_region_id: Mapped[str] = mapped_column(String, nullable=False)
    edge_type: Mapped[str] = mapped_column(String, default="adjacent")
    bidirectional: Mapped[bool] = mapped_column(Boolean, default=True)
    distance: Mapped[float] = mapped_column(Float, default=0.0)
    travel_time: Mapped[float] = mapped_column(Float, default=0.0)
    risk_level: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String, default="open")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=func.now(),
        server_default=func.now(),
        onupdate=func.now(),
    )


class MapNodeRow(Base):
    __tablename__ = "map_nodes"
    __table_args__ = (
        Index("ix_map_nodes_project_subworld", "project_id", "subworld_id"),
        Index("ix_map_nodes_project_region", "project_id", "region_id"),
        Index("ix_map_nodes_project_parent", "project_id", "parent_id"),
        Index("ix_map_nodes_project_type", "project_id", "node_type"),
        Index("ix_map_nodes_project_path", "project_id", "hierarchy_path"),
        Index("ix_map_nodes_project_status", "project_id", "status"),
        Index("ix_map_nodes_project_created_chapter", "project_id", "created_at_chapter"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String, ForeignKey("projects.id"), nullable=False)
    subworld_id: Mapped[str] = mapped_column(String, default="")
    region_id: Mapped[str] = mapped_column(String, default="")
    node_type: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(Text, default="")
    aliases_json: Mapped[str] = mapped_column(Text, default="[]")
    description: Mapped[str] = mapped_column(Text, default="")
    parent_id: Mapped[str] = mapped_column(String, default="")
    hierarchy_path: Mapped[str] = mapped_column(Text, default="")
    scale_level: Mapped[str] = mapped_column(String, default="")
    coordinates_json: Mapped[str] = mapped_column(Text, default="{}")
    shape_ref: Mapped[str] = mapped_column(Text, default="")
    terrain: Mapped[str] = mapped_column(String, default="")
    climate: Mapped[str] = mapped_column(String, default="")
    culture_tag: Mapped[str] = mapped_column(String, default="")
    default_danger_level: Mapped[float] = mapped_column(Float, default=0.0)
    access_level: Mapped[str] = mapped_column(String, default="open")
    status: Mapped[str] = mapped_column(String, default="normal")
    created_at_chapter: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=func.now(),
        server_default=func.now(),
        onupdate=func.now(),
    )


class MapEdgeRow(Base):
    __tablename__ = "map_edges"
    __table_args__ = (
        Index("ix_map_edges_project_subworld", "project_id", "subworld_id"),
        Index("ix_map_edges_project_from", "project_id", "from_node_id"),
        Index("ix_map_edges_project_to", "project_id", "to_node_id"),
        Index("ix_map_edges_project_type", "project_id", "edge_type"),
        Index("ix_map_edges_project_status", "project_id", "status"),
        Index("ix_map_edges_project_created_chapter", "project_id", "created_at_chapter"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String, ForeignKey("projects.id"), nullable=False)
    subworld_id: Mapped[str] = mapped_column(String, default="")
    from_node_id: Mapped[str] = mapped_column(String, nullable=False)
    to_node_id: Mapped[str] = mapped_column(String, nullable=False)
    edge_type: Mapped[str] = mapped_column(String, nullable=False)
    bidirectional: Mapped[bool] = mapped_column(Boolean, default=False)
    distance: Mapped[float] = mapped_column(Float, default=0.0)
    travel_time: Mapped[float] = mapped_column(Float, default=0.0)
    travel_cost: Mapped[float] = mapped_column(Float, default=0.0)
    risk_level: Mapped[float] = mapped_column(Float, default=0.0)
    narrative_cost: Mapped[float] = mapped_column(Float, default=0.0)
    access_rule_id: Mapped[str] = mapped_column(String, default="")
    status: Mapped[str] = mapped_column(String, default="open")
    discovered_by_default: Mapped[bool] = mapped_column(Boolean, default=True)
    visibility_default: Mapped[str] = mapped_column(String, default="visible")
    created_at_chapter: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=func.now(),
        server_default=func.now(),
        onupdate=func.now(),
    )


class MapGenerationRunRow(Base):
    __tablename__ = "map_generation_runs"
    __table_args__ = (
        Index("ix_map_generation_runs_project_subworld", "project_id", "subworld_id"),
        Index("ix_map_generation_runs_project_created", "project_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String, ForeignKey("projects.id"), nullable=False)
    subworld_id: Mapped[str] = mapped_column(String, ForeignKey("sub_worlds.id"), nullable=False)
    generation_seed: Mapped[int] = mapped_column(Integer, default=0)
    algorithm: Mapped[str] = mapped_column(String, default="anchor_graph_v1")
    input_spec_json: Mapped[str] = mapped_column(Text, default="{}")
    result_summary_json: Mapped[str] = mapped_column(Text, default="{}")
    validation_report_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), server_default=func.now())
