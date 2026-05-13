from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy.orm import Session

from forwin.models.world_model import WorldModelLinkRow, WorldModelPageRow
from forwin.protocol.world_model import WorldModelExportResult
from forwin.world_model.page_repository import WorldModelPageRepository


class ObsidianWorldExporter:
    def __init__(self, session: Session) -> None:
        self.session = session

    def export_project(
        self,
        project_id: str,
        *,
        vault_root: str | Path | None = None,
    ) -> WorldModelExportResult:
        root = Path(vault_root) if vault_root is not None else Path("data") / "world_vaults" / project_id
        root.mkdir(parents=True, exist_ok=True)
        page_repo = WorldModelPageRepository(self.session)
        rows = sorted(page_repo.list_canonical_rows(project_id), key=lambda row: row.vault_path)

        self._write_rules(root)
        exported = 0
        for row in rows:
            target = root / row.vault_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(row.markdown, encoding="utf-8")
            exported += 1
        self._write_canvas(root, project_id, rows)
        return WorldModelExportResult(
            ok=True,
            project_id=project_id,
            vault_root=str(root),
            exported_count=exported,
            message=f"已导出 {exported} 个 WorldModel 页面。",
        )

    @staticmethod
    def _write_rules(root: Path) -> None:
        agents = root / "AGENTS.md"
        if not agents.exists():
            agents.write_text(
                "\n".join(
                    [
                        "# ForWin World Wiki Rules",
                        "",
                        "1. 不得凭空创造 canon。",
                        "2. 所有 canon claim 必须带 source_refs。",
                        "3. 不确定内容放入 Open Questions。",
                        "4. 矛盾内容放入 Contradictions。",
                        "5. 人类手动编辑只能生成 proposal，不能直接覆盖 canon。",
                        "6. Manual Notes 可自由补充，但 Canon Summary 不会被直接回写。",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

    def _write_canvas(self, root: Path, project_id: str, rows: list[WorldModelPageRow]) -> None:
        canvas_root = root / "canvas"
        canvas_root.mkdir(parents=True, exist_ok=True)
        groups = {
            "character_relationships.canvas": {"character"},
            "faction_conflicts.canvas": {"faction", "contradiction"},
            "map_overview.canvas": {"region", "node"},
            "secret_reveal_ladder.canvas": {"secret", "promise"},
            "arc_dependencies.canvas": {"arc", "promise", "overview"},
        }
        from sqlalchemy import select

        links = self.session.execute(
            select(WorldModelLinkRow).where(WorldModelLinkRow.project_id == project_id)
        ).scalars().all()
        page_by_id = {row.id: row for row in rows}
        for filename, page_types in groups.items():
            selected = [row for row in rows if row.page_type in page_types]
            if not selected and filename != "arc_dependencies.canvas":
                continue
            if not selected:
                selected = [row for row in rows if row.page_type == "overview"]
            canvas = self._canvas_payload(selected, links, page_by_id)
            (canvas_root / filename).write_text(json.dumps(canvas, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _canvas_payload(
        rows: list[WorldModelPageRow],
        links: list[WorldModelLinkRow],
        page_by_id: dict[str, WorldModelPageRow],
    ) -> dict[str, list[dict[str, object]]]:
        nodes = []
        selected_ids = {row.id for row in rows}
        for index, row in enumerate(rows):
            nodes.append(
                {
                    "id": row.id,
                    "type": "file",
                    "file": row.vault_path,
                    "x": (index % 4) * 340,
                    "y": (index // 4) * 180,
                    "width": 300,
                    "height": 140,
                }
            )
        edges = []
        for link in links:
            if link.source_page_id not in selected_ids or link.target_page_id not in selected_ids:
                continue
            source = page_by_id.get(link.source_page_id)
            target = page_by_id.get(link.target_page_id)
            if source is None or target is None:
                continue
            edges.append(
                {
                    "id": link.id,
                    "fromNode": source.id,
                    "fromSide": "right",
                    "toNode": target.id,
                    "toSide": "left",
                    "label": link.relation_type,
                }
            )
        return {"nodes": nodes, "edges": edges}
