from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.models import new_id
from forwin.models.world_model import (
    WorldEditProposalRow,
    WorldModelCompileRunRow,
    WorldModelConflictRow,
    WorldModelLinkRow,
    WorldModelPageRow,
    WorldModelSnapshotRow,
)
from forwin.protocol.world_model import (
    EvidenceRef,
    WorldEditProposal,
    WorldModelConflict,
    WorldModelPage,
    WorldModelSnapshot,
)


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


def utc_now() -> datetime:
    return datetime.now(UTC)


class WorldModelStore:
    def __init__(self, session: Session) -> None:
        self.session = session

    def latest_snapshot(
        self,
        project_id: str,
        *,
        as_of_chapter: int | None = None,
    ) -> WorldModelSnapshotRow | None:
        stmt = (
            select(WorldModelSnapshotRow)
            .where(
                WorldModelSnapshotRow.project_id == project_id,
                WorldModelSnapshotRow.status == "live",
            )
            .order_by(
                WorldModelSnapshotRow.as_of_chapter.desc(),
                WorldModelSnapshotRow.version.desc(),
                WorldModelSnapshotRow.created_at.desc(),
            )
            .limit(1)
        )
        if as_of_chapter is not None:
            stmt = stmt.where(WorldModelSnapshotRow.as_of_chapter <= int(as_of_chapter))
        return self.session.execute(stmt).scalar_one_or_none()

    def snapshot_by_digest(
        self,
        project_id: str,
        *,
        as_of_chapter: int,
        source_digest: str,
    ) -> WorldModelSnapshotRow | None:
        stmt = (
            select(WorldModelSnapshotRow)
            .where(
                WorldModelSnapshotRow.project_id == project_id,
                WorldModelSnapshotRow.as_of_chapter == int(as_of_chapter),
                WorldModelSnapshotRow.status == "live",
                WorldModelSnapshotRow.source_digest == source_digest,
            )
            .order_by(WorldModelSnapshotRow.version.desc(), WorldModelSnapshotRow.created_at.desc())
            .limit(1)
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def create_compile_run(
        self,
        *,
        project_id: str,
        trigger: str,
        as_of_chapter: int,
        source_refs: list[dict[str, Any]],
        source_digest: str,
        status: str = "started",
        snapshot_id: str = "",
        error: str = "",
    ) -> WorldModelCompileRunRow:
        row = WorldModelCompileRunRow(
            id=new_id(),
            project_id=project_id,
            trigger=trigger,
            as_of_chapter=int(as_of_chapter),
            status=status,
            source_refs_json=json.dumps(source_refs, ensure_ascii=False),
            source_digest=source_digest,
            snapshot_id=snapshot_id,
            error=error,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def mark_compile_run(
        self,
        row: WorldModelCompileRunRow,
        *,
        status: str,
        snapshot_id: str = "",
        error: str = "",
    ) -> WorldModelCompileRunRow:
        row.status = status
        if snapshot_id:
            row.snapshot_id = snapshot_id
        row.error = error
        row.updated_at = utc_now()
        self.session.add(row)
        self.session.flush()
        return row

    def save_snapshot(
        self,
        *,
        project_id: str,
        as_of_chapter: int,
        snapshot: dict[str, Any],
        source_digest: str,
        compiled_from_event_id: str = "",
    ) -> WorldModelSnapshotRow:
        latest = self.latest_snapshot(project_id)
        version = 1
        if latest is not None:
            version = max(1, int(latest.version or 1) + 1)
        row = WorldModelSnapshotRow(
            id=new_id(),
            project_id=project_id,
            as_of_chapter=int(as_of_chapter),
            version=version,
            status="live",
            snapshot_json=json.dumps(snapshot, ensure_ascii=False),
            source_digest=source_digest,
            compiled_from_event_id=compiled_from_event_id,
        )
        self.session.add(row)
        self.session.flush()
        return row

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
    ) -> WorldModelPageRow:
        digest = content_hash(stable_json(frontmatter), markdown)
        stmt = (
            select(WorldModelPageRow)
            .where(
                WorldModelPageRow.project_id == project_id,
                WorldModelPageRow.page_key == page_key,
            )
            .order_by(WorldModelPageRow.revision.desc())
            .limit(1)
        )
        row = self.session.execute(stmt).scalar_one_or_none()
        if row is None:
            row = WorldModelPageRow(
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
                status="canon_live",
                as_of_chapter=int(as_of_chapter),
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
            row.status = "canon_live"
            row.as_of_chapter = int(as_of_chapter)
        self.session.add(row)
        self.session.flush()
        return row

    def replace_links(
        self,
        *,
        project_id: str,
        links: list[tuple[str, str, str, list[dict[str, Any]]]],
    ) -> None:
        self.session.query(WorldModelLinkRow).filter(WorldModelLinkRow.project_id == project_id).delete(
            synchronize_session=False
        )
        page_ids = {
            row.page_key: row.id
            for row in self.session.execute(
                select(WorldModelPageRow).where(WorldModelPageRow.project_id == project_id)
            ).scalars()
        }
        for source_key, target_key, relation_type, refs in links:
            source_id = page_ids.get(source_key)
            target_id = page_ids.get(target_key)
            if not source_id or not target_id or source_id == target_id:
                continue
            self.session.add(
                WorldModelLinkRow(
                    id=new_id(),
                    project_id=project_id,
                    source_page_id=source_id,
                    target_page_id=target_id,
                    relation_type=relation_type,
                    evidence_refs_json=json.dumps(refs, ensure_ascii=False),
                )
            )
        self.session.flush()

    def replace_conflicts(
        self,
        *,
        project_id: str,
        conflicts: list[WorldModelConflict],
    ) -> None:
        self.session.query(WorldModelConflictRow).filter(
            WorldModelConflictRow.project_id == project_id,
            WorldModelConflictRow.status == "open",
        ).delete(synchronize_session=False)
        for item in conflicts:
            self.session.add(
                WorldModelConflictRow(
                    id=new_id(),
                    project_id=project_id,
                    conflict_type=item.conflict_type,
                    severity=item.severity,
                    subject_key=item.subject_key,
                    description=item.description,
                    evidence_refs_json=json.dumps(
                        [ref.model_dump(mode="json") for ref in item.evidence_refs],
                        ensure_ascii=False,
                    ),
                    status=item.status,
                )
            )
        self.session.flush()

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
    ) -> WorldEditProposalRow:
        row = WorldEditProposalRow(
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
            status="pending",
            created_by=created_by,
        )
        self.session.add(row)
        self.session.flush()
        return row


def snapshot_to_schema(row: WorldModelSnapshotRow) -> WorldModelSnapshot:
    payload = load_json(row.snapshot_json, {})
    source_refs = [
        EvidenceRef.model_validate(item)
        for item in payload.get("source_refs", [])
        if isinstance(item, dict)
    ]
    return WorldModelSnapshot(
        id=row.id,
        project_id=row.project_id,
        as_of_chapter=row.as_of_chapter,
        version=row.version,
        status=row.status,
        snapshot=payload,
        source_digest=row.source_digest,
        source_refs=source_refs,
    )


def page_to_schema(row: WorldModelPageRow) -> WorldModelPage:
    return WorldModelPage(
        id=row.id,
        page_key=row.page_key,
        page_type=row.page_type,
        title=row.title,
        vault_path=row.vault_path,
        markdown=row.markdown,
        frontmatter=load_json(row.frontmatter_json, {}),
        content_hash=row.content_hash,
        revision=row.revision,
        status=row.status,
        as_of_chapter=row.as_of_chapter,
    )


def conflict_to_schema(row: WorldModelConflictRow) -> WorldModelConflict:
    refs = [
        EvidenceRef.model_validate(item)
        for item in load_json(row.evidence_refs_json, [])
        if isinstance(item, dict)
    ]
    return WorldModelConflict(
        id=row.id,
        conflict_type=row.conflict_type,
        severity=row.severity if row.severity in {"info", "warning", "error"} else "warning",
        subject_key=row.subject_key,
        description=row.description,
        evidence_refs=refs,
        status=row.status,
    )


def proposal_to_schema(row: WorldEditProposalRow) -> WorldEditProposal:
    return WorldEditProposal(
        id=row.id,
        source=row.source,
        target_page_key=row.target_page_key,
        target_node_id=getattr(row, "target_node_id", "") or "",
        target_field=row.target_field,
        proposal_type=getattr(row, "proposal_type", "") or "",
        proposed_patch=load_json(row.proposed_patch_json, {}),
        reason=row.reason,
        human_notes=getattr(row, "human_notes", "") or "",
        status=row.status,
        created_by=row.created_by,
        review_reason=getattr(row, "review_reason", "") or "",
        graph_delta_id=getattr(row, "graph_delta_id", "") or "",
    )
