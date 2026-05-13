from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from .base import Base, new_id


class WorldModelSnapshotRow(Base):
    __tablename__ = "world_model_snapshots"
    __table_args__ = (
        Index("ix_world_model_snapshots_project_chapter", "project_id", "as_of_chapter"),
        Index("ix_world_model_snapshots_project_status", "project_id", "status"),
        Index("ix_world_model_snapshots_project_digest", "project_id", "source_digest"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String, ForeignKey("projects.id"), nullable=False)
    as_of_chapter: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String, nullable=False, default="live")
    snapshot_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    source_digest: Mapped[str] = mapped_column(String, nullable=False, default="")
    compiled_from_event_id: Mapped[str] = mapped_column(String, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())


class WorldModelPageRow(Base):
    __tablename__ = "world_model_pages"
    __table_args__ = (
        Index("ix_world_model_pages_project_key", "project_id", "page_key"),
        Index("ix_world_model_pages_project_type", "project_id", "page_type"),
        Index("ix_world_model_pages_project_status", "project_id", "status"),
        Index("ix_world_model_pages_project_identity", "project_id", "page_type", "logical_identity_key"),
        Index(
            "ux_world_model_pages_live_identity",
            "project_id",
            "page_type",
            "logical_identity_key",
            unique=True,
            postgresql_where=text("status = 'canon_live' AND logical_identity_key <> ''"),
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String, ForeignKey("projects.id"), nullable=False)
    page_key: Mapped[str] = mapped_column(String, nullable=False)
    page_type: Mapped[str] = mapped_column(String, nullable=False, default="overview")
    title: Mapped[str] = mapped_column(String, nullable=False, default="")
    vault_path: Mapped[str] = mapped_column(String, nullable=False, default="")
    markdown: Mapped[str] = mapped_column(Text, nullable=False, default="")
    frontmatter_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    content_hash: Mapped[str] = mapped_column(String, nullable=False, default="")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String, nullable=False, default="canon_live")
    as_of_chapter: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    logical_identity_key: Mapped[str] = mapped_column(String, nullable=False, default="")
    canonical_source_type: Mapped[str] = mapped_column(String, nullable=False, default="")
    canonical_source_id: Mapped[str] = mapped_column(String, nullable=False, default="")
    supersedes_page_id: Mapped[str] = mapped_column(String, nullable=False, default="")
    canonical_rank: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())


class WorldModelLinkRow(Base):
    __tablename__ = "world_model_links"
    __table_args__ = (
        Index("ix_world_model_links_project_source", "project_id", "source_page_id"),
        Index("ix_world_model_links_project_target", "project_id", "target_page_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String, ForeignKey("projects.id"), nullable=False)
    source_page_id: Mapped[str] = mapped_column(String, ForeignKey("world_model_pages.id"), nullable=False)
    target_page_id: Mapped[str] = mapped_column(String, ForeignKey("world_model_pages.id"), nullable=False)
    relation_type: Mapped[str] = mapped_column(String, nullable=False, default="related")
    evidence_refs_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class WorldEditProposalRow(Base):
    __tablename__ = "world_edit_proposals"
    __table_args__ = (
        Index("ix_world_edit_proposals_project_status", "project_id", "status"),
        Index("ix_world_edit_proposals_project_page", "project_id", "target_page_key"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String, ForeignKey("projects.id"), nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False, default="obsidian")
    target_page_key: Mapped[str] = mapped_column(String, nullable=False, default="")
    target_node_id: Mapped[str] = mapped_column(String, nullable=False, default="")
    target_field: Mapped[str] = mapped_column(String, nullable=False, default="")
    proposal_type: Mapped[str] = mapped_column(String, nullable=False, default="")
    proposed_patch_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    human_notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    created_by: Mapped[str] = mapped_column(String, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    review_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    graph_delta_id: Mapped[str] = mapped_column(String, nullable=False, default="")


class WorldModelConflictRow(Base):
    __tablename__ = "world_model_conflicts"
    __table_args__ = (
        Index("ix_world_model_conflicts_project_status", "project_id", "status"),
        Index("ix_world_model_conflicts_project_type", "project_id", "conflict_type"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String, ForeignKey("projects.id"), nullable=False)
    conflict_type: Mapped[str] = mapped_column(String, nullable=False, default="")
    severity: Mapped[str] = mapped_column(String, nullable=False, default="warning")
    subject_key: Mapped[str] = mapped_column(String, nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    evidence_refs_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    status: Mapped[str] = mapped_column(String, nullable=False, default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class WorldModelCompileRunRow(Base):
    __tablename__ = "world_model_compile_runs"
    __table_args__ = (
        Index("ix_world_model_compile_runs_project_chapter", "project_id", "as_of_chapter"),
        Index("ix_world_model_compile_runs_project_status", "project_id", "status"),
        Index("ix_world_model_compile_runs_project_digest", "project_id", "source_digest"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String, ForeignKey("projects.id"), nullable=False)
    trigger: Mapped[str] = mapped_column(String, nullable=False, default="")
    as_of_chapter: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String, nullable=False, default="started")
    source_refs_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    source_digest: Mapped[str] = mapped_column(String, nullable=False, default="")
    snapshot_id: Mapped[str] = mapped_column(String, nullable=False, default="")
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())
