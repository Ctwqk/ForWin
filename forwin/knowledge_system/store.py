from __future__ import annotations

import json
from hashlib import sha256
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.models import new_id
from forwin.models.knowledge import (
    KnowledgeEditProposalRow,
    KnowledgeProjectionPageRow,
)
from .page_repository import KnowledgePageRepository


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(*parts: str) -> str:
    digest = sha256()
    for part in parts:
        digest.update(str(part or "").encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def load_json(raw: str | None, default: Any) -> Any:
    try:
        value = json.loads(raw or "")
    except (json.JSONDecodeError, TypeError):
        return default
    return value if isinstance(value, type(default)) else default


class KnowledgeProjectionStore:
    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_page(
        self,
        *,
        project_id: str,
        page_key: str,
        page_type: str,
        title: str,
        vault_path: str,
        markdown: str,
        frontmatter: dict[str, Any],
        as_of_chapter: int,
        projection_kind: str = "world_studio",
        projection_version: str = "",
        source_digest: str = "",
        dependency_manifest: dict | None = None,
        section_digest: dict[str, str] | None = None,
        observer_type: str = "",
        observer_id: str = "",
        role_scope: str = "",
        visibility_scope: str = "",
        canon_status: str = "canon_projection",
    ) -> KnowledgeProjectionPageRow:
        digest = content_hash(stable_json(frontmatter), markdown)
        page_repo = KnowledgePageRepository(self.session)
        identity = page_repo.identity_for_values(
            page_type=page_type,
            title=title,
            page_key=page_key,
            frontmatter=frontmatter,
            as_of_chapter=as_of_chapter,
        )
        section_digest_json = json.dumps(
            section_digest or {}, ensure_ascii=False, sort_keys=True
        )
        page_repo.supersede_duplicate_pages(
            project_id,
            identity_key=identity.logical_identity_key,
            page_type=page_type,
        )
        desired_status = "canon_live"
        supersedes_page_id = ""
        existing_live_rows = [
            existing
            for existing in self.session.execute(
                select(KnowledgeProjectionPageRow).where(
                    KnowledgeProjectionPageRow.project_id == project_id,
                    KnowledgeProjectionPageRow.page_type == page_type,
                    KnowledgeProjectionPageRow.status == "canon_live",
                )
            ).scalars()
            if page_repo.identity_for_row(existing).logical_identity_key
            == identity.logical_identity_key
        ]
        existing_live_rows = [
            existing for existing in existing_live_rows if existing.page_key != page_key
        ]
        if existing_live_rows:
            best_existing = max(
                existing_live_rows,
                key=lambda existing: (
                    int(page_repo.identity_for_row(existing).canonical_rank),
                    int(existing.as_of_chapter or 0),
                    int(existing.revision or 0),
                    str(existing.updated_at or ""),
                    str(existing.id or ""),
                ),
            )
            best_existing_key = (
                int(page_repo.identity_for_row(best_existing).canonical_rank),
                int(best_existing.as_of_chapter or 0),
                int(best_existing.revision or 0),
            )
            new_key = (int(identity.canonical_rank), int(as_of_chapter or 0), 1)
            if best_existing_key >= new_key:
                desired_status = "superseded"
                supersedes_page_id = best_existing.id
            else:
                for existing in existing_live_rows:
                    existing.status = "superseded"
                    existing.supersedes_page_id = ""
                    self.session.add(existing)
                self.session.flush()
        stmt = (
            select(KnowledgeProjectionPageRow)
            .where(
                KnowledgeProjectionPageRow.project_id == project_id,
                KnowledgeProjectionPageRow.page_key == page_key,
            )
            .order_by(KnowledgeProjectionPageRow.revision.desc())
            .limit(1)
        )
        row = self.session.execute(stmt).scalar_one_or_none()
        if row is None:
            row = KnowledgeProjectionPageRow(
                id=new_id(),
                project_id=project_id,
                page_key=page_key,
                page_type=page_type,
                title=title,
                vault_path=vault_path,
                markdown=markdown,
                frontmatter_json=json.dumps(frontmatter, ensure_ascii=False),
                content_hash=digest,
                revision=1,
                status=desired_status,
                as_of_chapter=int(as_of_chapter),
                logical_identity_key=identity.logical_identity_key,
                canonical_source_type=identity.canonical_source_type,
                canonical_source_id=identity.canonical_source_id,
                supersedes_page_id=supersedes_page_id,
                canonical_rank=identity.canonical_rank,
                projection_kind=projection_kind,
                projection_version=projection_version,
                source_digest=source_digest,
                dependency_manifest_json=stable_json(dependency_manifest or {}),
                section_digest_json=section_digest_json,
                observer_type=observer_type,
                observer_id=observer_id,
                role_scope=role_scope,
                visibility_scope=visibility_scope,
                canon_status=canon_status,
            )
        else:
            if row.content_hash != digest:
                row.revision = int(row.revision or 1) + 1
            row.page_type = page_type
            row.title = title
            row.vault_path = vault_path
            row.markdown = markdown
            row.frontmatter_json = json.dumps(frontmatter, ensure_ascii=False)
            row.content_hash = digest
            row.status = desired_status
            row.as_of_chapter = int(as_of_chapter)
            row.logical_identity_key = identity.logical_identity_key
            row.canonical_source_type = identity.canonical_source_type
            row.canonical_source_id = identity.canonical_source_id
            row.canonical_rank = identity.canonical_rank
            row.supersedes_page_id = supersedes_page_id
            row.projection_kind = projection_kind
            row.projection_version = projection_version
            row.source_digest = source_digest
            row.dependency_manifest_json = stable_json(dependency_manifest or {})
            row.section_digest_json = section_digest_json
            row.observer_type = observer_type
            row.observer_id = observer_id
            row.role_scope = role_scope
            row.visibility_scope = visibility_scope
            row.canon_status = canon_status
        self.session.add(row)
        self.session.flush()
        if desired_status == "canon_live":
            page_repo.supersede_duplicate_pages(
                project_id,
                identity_key=identity.logical_identity_key,
                page_type=page_type,
            )
        self.session.refresh(row)
        return row

    def create_proposal(
        self,
        *,
        project_id: str,
        source: str,
        target_page_key: str,
        target_field: str,
        proposed_patch: dict[str, Any],
        target_node_id: str = "",
        proposal_type: str = "",
        human_notes: str = "",
        reason: str = "",
        created_by: str = "",
        status: str = "pending",
    ) -> KnowledgeEditProposalRow:
        row = KnowledgeEditProposalRow(
            id=new_id(),
            project_id=project_id,
            source=source,
            target_page_key=target_page_key,
            target_node_id=target_node_id,
            target_field=target_field,
            proposal_type=proposal_type,
            proposed_patch_json=json.dumps(proposed_patch, ensure_ascii=False),
            reason=reason,
            human_notes=human_notes,
            status=status or "pending",
            created_by=created_by,
        )
        self.session.add(row)
        self.session.flush()
        return row


__all__ = [
    "KnowledgeProjectionStore",
    "content_hash",
    "load_json",
    "stable_json",
]
