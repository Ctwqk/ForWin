from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from .base import Base, new_id


class KnowledgeProjectionPageRow(Base):
    __tablename__ = "knowledge_projection_pages"
    __table_args__ = (
        Index("ix_knowledge_pages_project_key", "project_id", "page_key"),
        Index("ix_knowledge_pages_project_type", "project_id", "page_type"),
        Index("ix_knowledge_pages_project_status", "project_id", "status"),
        Index(
            "ix_knowledge_pages_project_identity",
            "project_id",
            "page_type",
            "logical_identity_key",
        ),
        Index(
            "ux_knowledge_pages_live_identity",
            "project_id",
            "page_type",
            "logical_identity_key",
            unique=True,
            postgresql_where=text(
                "status = 'canon_live' AND logical_identity_key <> ''"
            ),
        ),
        Index(
            "ix_knowledge_pages_project_projection",
            "project_id",
            "projection_kind",
            "projection_version",
        ),
        Index(
            "ix_knowledge_pages_project_source_digest", "project_id", "source_digest"
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=False
    )
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
    logical_identity_key: Mapped[str] = mapped_column(
        String, nullable=False, default=""
    )
    canonical_source_type: Mapped[str] = mapped_column(
        String, nullable=False, default=""
    )
    canonical_source_id: Mapped[str] = mapped_column(String, nullable=False, default="")
    supersedes_page_id: Mapped[str] = mapped_column(String, nullable=False, default="")
    canonical_rank: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    projection_kind: Mapped[str] = mapped_column(
        String, nullable=False, default="world_studio"
    )
    projection_version: Mapped[str] = mapped_column(String, nullable=False, default="")
    source_digest: Mapped[str] = mapped_column(String, nullable=False, default="")
    section_digest_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    observer_type: Mapped[str] = mapped_column(String, nullable=False, default="")
    observer_id: Mapped[str] = mapped_column(String, nullable=False, default="")
    role_scope: Mapped[str] = mapped_column(String, nullable=False, default="")
    visibility_scope: Mapped[str] = mapped_column(String, nullable=False, default="")
    canon_status: Mapped[str] = mapped_column(
        String, nullable=False, default="canon_projection"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now()
    )


class KnowledgeEditProposalRow(Base):
    __tablename__ = "knowledge_edit_proposals"
    __table_args__ = (
        Index("ix_knowledge_edit_proposals_project_status", "project_id", "status"),
        Index(
            "ix_knowledge_edit_proposals_project_page", "project_id", "target_page_key"
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=False
    )
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


__all__ = ["KnowledgeEditProposalRow", "KnowledgeProjectionPageRow"]
