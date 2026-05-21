from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from forwin.models.book_state import CharacterIdentityMapRow


@dataclass(frozen=True)
class CharacterIdentity:
    row: CharacterIdentityMapRow
    resolution: str


class CharacterIdentityMap:
    """Canonical character identity boundary.

    BookState node ids are the canonical character ids. Roster ids, Genesis refs
    and aliases are lookup signals only.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def resolve(
        self,
        *,
        project_id: str,
        character_id: str = "",
        book_state_node_id: str = "",
        roster_item_id: str = "",
        genesis_ref_id: str = "",
    ) -> CharacterIdentity | None:
        project_id = str(project_id or "").strip()
        character_id = str(character_id or "").strip()
        book_state_node_id = str(book_state_node_id or "").strip() or character_id
        roster_item_id = str(roster_item_id or "").strip()
        genesis_ref_id = str(genesis_ref_id or "").strip()

        for column, value, resolution in (
            (CharacterIdentityMapRow.book_state_node_id, book_state_node_id, "identity_book_state_node_id"),
            (CharacterIdentityMapRow.canonical_character_id, character_id, "identity_canonical_character_id"),
            (CharacterIdentityMapRow.genesis_ref_id, genesis_ref_id, "identity_genesis_ref_id"),
        ):
            if not value:
                continue
            row = self.session.execute(
                select(CharacterIdentityMapRow)
                .where(
                    CharacterIdentityMapRow.project_id == project_id,
                    CharacterIdentityMapRow.status == "active",
                    column == value,
                )
                .order_by(CharacterIdentityMapRow.updated_at.desc(), CharacterIdentityMapRow.id.desc())
            ).scalar_one_or_none()
            if row is not None:
                return CharacterIdentity(row=row, resolution=resolution)

        if roster_item_id:
            rows = self.session.execute(
                select(CharacterIdentityMapRow)
                .where(
                    CharacterIdentityMapRow.project_id == project_id,
                    CharacterIdentityMapRow.status == "active",
                    CharacterIdentityMapRow.roster_item_ids_json.contains(roster_item_id),
                )
                .order_by(CharacterIdentityMapRow.updated_at.desc(), CharacterIdentityMapRow.id.desc())
            ).scalars()
            for row in rows:
                if roster_item_id in _loads_list(row.roster_item_ids_json):
                    return CharacterIdentity(row=row, resolution="identity_roster_item_id")
        return None

    def upsert(
        self,
        *,
        project_id: str,
        canonical_character_id: str,
        book_state_node_id: str = "",
        genesis_ref_id: str = "",
        roster_item_ids: list[str] | None = None,
        aliases: list[str] | None = None,
        display_name: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> CharacterIdentityMapRow:
        project_id = str(project_id or "").strip()
        canonical_character_id = str(canonical_character_id or "").strip()
        book_state_node_id = str(book_state_node_id or "").strip() or canonical_character_id
        genesis_ref_id = str(genesis_ref_id or "").strip()
        roster_item_ids = _dedupe(roster_item_ids or [])
        aliases = _dedupe(aliases or [])
        display_name = str(display_name or "").strip()

        row = self._find_existing(
            project_id=project_id,
            canonical_character_id=canonical_character_id,
            book_state_node_id=book_state_node_id,
            genesis_ref_id=genesis_ref_id,
            roster_item_ids=roster_item_ids,
        )
        if row is None:
            row = CharacterIdentityMapRow(project_id=project_id)
            self.session.add(row)

        row.canonical_character_id = canonical_character_id or row.canonical_character_id
        row.book_state_node_id = book_state_node_id or row.book_state_node_id
        row.genesis_ref_id = genesis_ref_id or row.genesis_ref_id
        row.display_name = display_name or row.display_name
        row.status = "active"
        row.roster_item_ids_json = _dump(_dedupe([*_loads_list(row.roster_item_ids_json), *roster_item_ids]))
        row.aliases_json = _dump(_dedupe([*_loads_list(row.aliases_json), *aliases, display_name]))
        merged_metadata = _loads_dict(row.metadata_json)
        merged_metadata.update(metadata or {})
        row.metadata_json = _dump(merged_metadata)
        self.session.flush()
        return row

    def _find_existing(
        self,
        *,
        project_id: str,
        canonical_character_id: str,
        book_state_node_id: str,
        genesis_ref_id: str,
        roster_item_ids: list[str],
    ) -> CharacterIdentityMapRow | None:
        clauses = []
        if canonical_character_id:
            clauses.append(CharacterIdentityMapRow.canonical_character_id == canonical_character_id)
        if book_state_node_id:
            clauses.append(CharacterIdentityMapRow.book_state_node_id == book_state_node_id)
        if genesis_ref_id:
            clauses.append(CharacterIdentityMapRow.genesis_ref_id == genesis_ref_id)
        if not clauses and not roster_item_ids:
            return None
        rows = list(
            self.session.execute(
                select(CharacterIdentityMapRow)
                .where(
                    CharacterIdentityMapRow.project_id == project_id,
                    CharacterIdentityMapRow.status == "active",
                    or_(*clauses) if clauses else CharacterIdentityMapRow.project_id == project_id,
                )
                .order_by(CharacterIdentityMapRow.updated_at.desc(), CharacterIdentityMapRow.id.desc())
            )
            .scalars()
            .all()
        )
        if rows:
            return rows[0]
        for roster_item_id in roster_item_ids:
            found = self.resolve(project_id=project_id, roster_item_id=roster_item_id)
            if found is not None:
                return found.row
        return None


def _loads_list(raw: str | None) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _loads_dict(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result
