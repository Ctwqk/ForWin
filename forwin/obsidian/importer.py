from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from forwin.world_model.page_repository import WorldModelPageRepository
from forwin.world_model.store import WorldModelStore, load_json

from .frontmatter import EDITABLE_FIELDS, LOCKED_FIELDS, parse_frontmatter, parse_sections
from .proposal_classifier import classify_proposal


DEFAULT_VAULT_ROOT = Path("data/world_vaults")


@dataclass
class ObsidianImportResult:
    project_id: str
    vault_root: str
    proposal_count: int = 0
    changed_paths: list[str] = field(default_factory=list)
    proposal_ids: list[str] = field(default_factory=list)


class ObsidianImporter:
    """Import an Obsidian projection as reviewable proposals only."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.store = WorldModelStore(session)

    def import_project(
        self,
        project_id: str,
        *,
        vault_root: Path | None = None,
        created_by: str = "obsidian",
    ) -> ObsidianImportResult:
        root = vault_root or DEFAULT_VAULT_ROOT / project_id
        proposal_ids: list[str] = []
        changed_paths: list[str] = []
        if not root.exists():
            return ObsidianImportResult(project_id=project_id, vault_root=str(root))

        for path in sorted(root.rglob("*.md")):
            if path.name == "AGENTS.md":
                continue
            markdown = path.read_text(encoding="utf-8")
            frontmatter, _ = parse_frontmatter(markdown)
            if not frontmatter:
                continue
            if str(frontmatter.get("project_id", project_id)) != project_id:
                continue
            created = self._import_page(project_id, root, path, frontmatter, markdown, created_by=created_by)
            if created:
                changed_paths.append(str(path.relative_to(root)))
                proposal_ids.extend(created)

        return ObsidianImportResult(
            project_id=project_id,
            vault_root=str(root),
            proposal_count=len(proposal_ids),
            changed_paths=changed_paths,
            proposal_ids=proposal_ids,
        )

    def _import_page(
        self,
        project_id: str,
        root: Path,
        path: Path,
        frontmatter: dict[str, Any],
        markdown: str,
        *,
        created_by: str,
    ) -> list[str]:
        page_key = str(frontmatter.get("forwin_id") or frontmatter.get("node_id") or path.relative_to(root))
        page_type = str(frontmatter.get("node_type", ""))
        node_id = str(frontmatter.get("node_id", ""))
        target_resolution_required = not bool(node_id)
        current_sections = parse_sections(markdown)
        baseline_sections = self._baseline_sections(project_id, page_key)
        proposal_ids: list[str] = []

        for field_name in EDITABLE_FIELDS:
            value = current_sections.get(field_name, "").strip()
            if not value or value == "_empty_":
                continue
            baseline_value = baseline_sections.get(field_name, "").strip()
            if value == baseline_value:
                continue
            proposal_type = classify_proposal(page_type=page_type, target_field=field_name, proposed_text=value)
            row = self.store.create_proposal(
                project_id=project_id,
                source="obsidian",
                target_page_key=page_key,
                target_node_id=node_id,
                target_field=field_name,
                proposal_type=proposal_type,
                proposed_patch={
                    "field": field_name,
                    "old_value": baseline_value,
                    "new_value": value,
                    "vault_path": str(path.relative_to(root)),
                    "frontmatter": frontmatter,
                    "target_resolution_required": target_resolution_required,
                },
                human_notes=value if field_name in {"Manual Notes", "Human Questions"} else "",
                reason=f"Obsidian editable section changed: {field_name}",
                created_by=created_by,
                status="needs_resolution" if target_resolution_required else "pending",
            )
            proposal_ids.append(row.id)

        for field_name in LOCKED_FIELDS:
            value = current_sections.get(field_name, "").strip()
            baseline_value = baseline_sections.get(field_name, "").strip()
            if not baseline_value or value == baseline_value:
                continue
            proposal_type = classify_proposal(page_type=page_type, target_field=field_name, proposed_text=value)
            row = self.store.create_proposal(
                project_id=project_id,
                source="obsidian",
                target_page_key=page_key,
                target_node_id=node_id,
                target_field=field_name,
                proposal_type=proposal_type,
                proposed_patch={
                    "field": field_name,
                    "old_value": baseline_value,
                    "new_value": value,
                    "vault_path": str(path.relative_to(root)),
                    "frontmatter": frontmatter,
                    "locked_section": True,
                    "target_resolution_required": target_resolution_required,
                },
                reason=f"Obsidian locked section changed: {field_name}",
                created_by=created_by,
                status="needs_resolution" if target_resolution_required else "pending",
            )
            proposal_ids.append(row.id)
        return proposal_ids

    def _baseline_sections(self, project_id: str, page_key: str) -> dict[str, str]:
        row = WorldModelPageRepository(self.session).resolve_page_key(project_id, page_key)
        if row is None:
            return {}
        frontmatter = load_json(row.frontmatter_json, {})
        markdown = row.markdown
        if frontmatter and markdown:
            return parse_sections(markdown)
        return {}
