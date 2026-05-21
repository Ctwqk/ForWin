from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.models.world_model import WorldModelPageRow


CANONICAL_DEDUPE_TYPES = {
    "character",
    "faction",
    "organization",
    "family",
    "institution",
    "resource",
    "region",
    "node",
    "location",
}


@dataclass(frozen=True)
class PageIdentity:
    logical_identity_key: str
    canonical_source_type: str
    canonical_source_id: str
    canonical_rank: int


class WorldModelPageRepository:
    """Canonical read/write boundary for WorldModel projection pages."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def list_canonical_rows(
        self,
        project_id: str,
        *,
        page_type: str = "",
        include_superseded: bool = False,
    ) -> list[WorldModelPageRow]:
        if not include_superseded:
            self.supersede_duplicate_pages(project_id, page_type=page_type)
        rows = self._load_rows(project_id, page_type=page_type, include_superseded=include_superseded)
        if include_superseded:
            return sorted(rows, key=_page_output_key)
        return sorted(_canonical_rows(rows), key=_page_output_key)

    def resolve_page_key(self, project_id: str, page_key: str) -> WorldModelPageRow | None:
        row = self.session.execute(
            select(WorldModelPageRow)
            .where(
                WorldModelPageRow.project_id == project_id,
                WorldModelPageRow.page_key == page_key,
            )
            .order_by(WorldModelPageRow.revision.desc(), WorldModelPageRow.updated_at.desc(), WorldModelPageRow.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            return None
        if row.status == "superseded":
            target = self._superseding_row(row)
            if target is not None:
                return target
        if row.status == "canon_live":
            identity = self.identity_for_row(row)
            chosen = self._canonical_for_identity(row.project_id, identity.logical_identity_key, row.page_type)
            if chosen is not None:
                return chosen
        return row

    def prepare_row(
        self,
        row: WorldModelPageRow,
        *,
        frontmatter: dict[str, Any] | None = None,
    ) -> WorldModelPageRow:
        identity = self.identity_for_values(
            page_type=row.page_type,
            title=row.title,
            page_key=row.page_key,
            frontmatter=frontmatter if frontmatter is not None else _load_json(row.frontmatter_json),
            as_of_chapter=row.as_of_chapter,
        )
        row.logical_identity_key = identity.logical_identity_key
        row.canonical_source_type = identity.canonical_source_type
        row.canonical_source_id = identity.canonical_source_id
        row.canonical_rank = identity.canonical_rank
        return row

    def supersede_duplicate_pages(
        self,
        project_id: str,
        *,
        identity_key: str = "",
        page_type: str = "",
    ) -> None:
        rows = self._load_rows(project_id, page_type=page_type, include_superseded=False)
        groups: dict[tuple[str, str], list[WorldModelPageRow]] = {}
        for row in rows:
            identity = self.identity_for_row(row)
            if identity_key and identity.logical_identity_key != identity_key:
                continue
            if not identity.logical_identity_key:
                continue
            key = (row.page_type, identity.logical_identity_key)
            groups.setdefault(key, []).append(row)

        duplicate_groups = [group for group in groups.values() if len(group) > 1]
        for group in duplicate_groups:
            chosen = _pick_canonical_row(group)
            for row in group:
                if row.id == chosen.id:
                    continue
                row.status = "superseded"
                row.supersedes_page_id = chosen.id
                self.session.add(row)
        if duplicate_groups:
            self.session.flush()

        for group in groups.values():
            chosen = _pick_canonical_row(group)
            for row in group:
                self.prepare_row(row)
                if row.id == chosen.id:
                    row.status = "canon_live"
                    row.supersedes_page_id = ""
                else:
                    row.status = "superseded"
                    row.supersedes_page_id = chosen.id
                self.session.add(row)
        self.session.flush()

    def identity_for_row(self, row: WorldModelPageRow) -> PageIdentity:
        return _identity_for_row(row)

    @staticmethod
    def identity_for_values(
        *,
        page_type: str,
        title: str,
        page_key: str,
        frontmatter: dict[str, Any],
        as_of_chapter: int = 0,
    ) -> PageIdentity:
        page_type = str(page_type or "").strip() or "overview"
        title = str(title or "").strip()
        page_key = str(page_key or "").strip()
        explicit_identity = str(
            frontmatter.get("canonical_identity_key")
            or frontmatter.get("logical_identity_key")
            or ""
        ).strip()
        if explicit_identity:
            logical_identity_key = explicit_identity
        elif page_type in CANONICAL_DEDUPE_TYPES and title:
            logical_identity_key = f"{page_type}:name:{_normalize_identity(title)}"
        else:
            logical_identity_key = f"{page_type}:page:{page_key or _normalize_identity(title)}"

        source_type, source_id, source_rank = _source_from_frontmatter(page_key, frontmatter)
        chapter_rank = min(max(int(as_of_chapter or 0), 0), 9999)
        return PageIdentity(
            logical_identity_key=logical_identity_key,
            canonical_source_type=source_type,
            canonical_source_id=source_id,
            canonical_rank=source_rank + chapter_rank,
        )

    def _load_rows(
        self,
        project_id: str,
        *,
        page_type: str,
        include_superseded: bool,
    ) -> list[WorldModelPageRow]:
        stmt = select(WorldModelPageRow).where(WorldModelPageRow.project_id == project_id)
        if page_type:
            stmt = stmt.where(WorldModelPageRow.page_type == page_type)
        if not include_superseded:
            stmt = stmt.where(WorldModelPageRow.status == "canon_live")
        return list(
            self.session.execute(
                stmt.order_by(
                    WorldModelPageRow.page_type.asc(),
                    WorldModelPageRow.title.asc(),
                    WorldModelPageRow.as_of_chapter.desc(),
                    WorldModelPageRow.updated_at.desc(),
                    WorldModelPageRow.id.desc(),
                )
            )
            .scalars()
            .all()
        )

    def _canonical_for_identity(
        self,
        project_id: str,
        identity_key: str,
        page_type: str,
    ) -> WorldModelPageRow | None:
        if not identity_key:
            return None
        rows = [
            row
            for row in self._load_rows(project_id, page_type=page_type, include_superseded=False)
            if self.identity_for_row(row).logical_identity_key == identity_key
        ]
        return _pick_canonical_row(rows) if rows else None

    def _superseding_row(self, row: WorldModelPageRow) -> WorldModelPageRow | None:
        target_id = str(getattr(row, "supersedes_page_id", "") or "").strip()
        if target_id:
            target = self.session.get(WorldModelPageRow, target_id)
            if target is not None and target.status == "canon_live":
                return target
        identity = self.identity_for_row(row)
        return self._canonical_for_identity(row.project_id, identity.logical_identity_key, row.page_type)


def _canonical_rows(rows: list[WorldModelPageRow]) -> list[WorldModelPageRow]:
    passthrough: list[WorldModelPageRow] = []
    grouped: dict[tuple[str, str], list[WorldModelPageRow]] = {}
    for row in rows:
        identity = _identity_for_row(row)
        if not identity.logical_identity_key:
            passthrough.append(row)
            continue
        grouped.setdefault((row.page_type, identity.logical_identity_key), []).append(row)
    return [*passthrough, *(_pick_canonical_row(group) for group in grouped.values())]


def _pick_canonical_row(rows: list[WorldModelPageRow]) -> WorldModelPageRow:
    return max(rows, key=_page_preference_key)


def _page_preference_key(row: WorldModelPageRow) -> tuple[int, int, int, str, str]:
    identity = _identity_for_row(row)
    updated = getattr(row, "updated_at", None)
    if isinstance(updated, datetime):
        updated_key = updated.isoformat()
    else:
        updated_key = str(updated or "")
    return (
        int(getattr(row, "canonical_rank", 0) or identity.canonical_rank),
        int(row.as_of_chapter or 0),
        int(row.revision or 0),
        updated_key,
        str(row.id or ""),
    )


def _page_output_key(row: WorldModelPageRow) -> tuple[str, str, str]:
    return (str(row.page_type or ""), str(row.title or ""), str(row.page_key or ""))


def _source_from_frontmatter(page_key: str, frontmatter: dict[str, Any]) -> tuple[str, str, int]:
    node_id = str(frontmatter.get("node_id") or "").strip()
    if node_id:
        return "book_state_node", node_id, 30000
    forwin_id = str(frontmatter.get("forwin_id") or page_key or "").strip()
    if ":genesis:" in forwin_id or forwin_id.startswith("genesis:"):
        return "genesis", forwin_id, 10000
    return "world_model_page", forwin_id, 20000


def _identity_for_row(row: WorldModelPageRow) -> PageIdentity:
    frontmatter = _load_json(row.frontmatter_json)
    existing_key = str(getattr(row, "logical_identity_key", "") or "").strip()
    identity = WorldModelPageRepository.identity_for_values(
        page_type=row.page_type,
        title=row.title,
        page_key=row.page_key,
        frontmatter=frontmatter,
        as_of_chapter=row.as_of_chapter,
    )
    if existing_key and existing_key == identity.logical_identity_key:
        return PageIdentity(
            logical_identity_key=existing_key,
            canonical_source_type=str(getattr(row, "canonical_source_type", "") or identity.canonical_source_type),
            canonical_source_id=str(getattr(row, "canonical_source_id", "") or identity.canonical_source_id),
            canonical_rank=int(getattr(row, "canonical_rank", 0) or identity.canonical_rank),
        )
    return identity


def _normalize_identity(value: str) -> str:
    text = re.sub(r"\s+", "", str(value or "").strip().lower())
    return text or "unnamed"


def _load_json(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}
