from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.models.world_model import WorldModelPageRow
from forwin.protocol.world_model import WorldModelImportResult

from .store import WorldModelStore, content_hash


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
            existing_pending = self.session.execute(
                select(WorldModelPageRow).where(
                    WorldModelPageRow.project_id == project_id,
                    WorldModelPageRow.page_key == row.page_key,
                )
            ).scalar_one_or_none()
            if existing_pending is None:
                continue
            self.store.create_proposal(
                project_id=project_id,
                source="obsidian",
                target_page_key=row.page_key,
                target_field="markdown",
                proposed_patch={
                    "vault_path": relative_path,
                    "old_hash": row.content_hash,
                    "new_hash": digest,
                    "old_markdown": row.markdown,
                    "new_markdown": markdown,
                },
                reason="Obsidian markdown content changed.",
                created_by="obsidian",
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
