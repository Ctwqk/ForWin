from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.models.world_model import WorldModelPageRow
from forwin.protocol.world_model import WorldModelImportResult

from .page_repository import WorldModelPageRepository
from .store import WorldModelStore, content_hash, load_json


class ObsidianWorldImporter:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.store = WorldModelStore(session)

    def import_project(
        self,
        project_id: str,
        *,
        vault_root: str | Path | None = None,
    ) -> WorldModelImportResult:
        root = Path(vault_root) if vault_root is not None else Path("data") / "world_vaults" / project_id
        rows = self.session.execute(
            select(WorldModelPageRow).where(WorldModelPageRow.project_id == project_id)
        ).scalars().all()
        rows_by_path = {row.vault_path: row for row in rows}
        changed_paths: list[str] = []
        proposal_count = 0
        for relative_path, row in rows_by_path.items():
            path = root / relative_path
            if not path.exists():
                continue
            markdown = path.read_text(encoding="utf-8")
            digest = content_hash(row.frontmatter_json, markdown)
            if digest == row.content_hash or markdown == row.markdown:
                continue
            existing_pending = WorldModelPageRepository(self.session).resolve_page_key(project_id, row.page_key)
            if existing_pending is None:
                continue
            frontmatter = load_json(existing_pending.frontmatter_json, {})
            target_node_id = str(frontmatter.get("node_id") or "").strip()
            self.store.create_proposal(
                project_id=project_id,
                source="obsidian",
                target_page_key=existing_pending.page_key,
                target_node_id=target_node_id,
                target_field="markdown",
                proposed_patch={
                    "vault_path": relative_path,
                    "old_hash": row.content_hash,
                    "new_hash": digest,
                    "old_markdown": row.markdown,
                    "new_markdown": markdown,
                    "target_resolution_required": not bool(target_node_id),
                },
                reason="Obsidian markdown content changed.",
                created_by="obsidian",
                status="pending" if target_node_id else "needs_resolution",
            )
            changed_paths.append(relative_path)
            proposal_count += 1
        return WorldModelImportResult(
            ok=True,
            project_id=project_id,
            vault_root=str(root),
            proposal_count=proposal_count,
            changed_paths=changed_paths,
            message=f"已生成 {proposal_count} 个 WorldEditProposal。",
        )
