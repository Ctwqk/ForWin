from __future__ import annotations

import json
import re
from enum import StrEnum
from typing import Any, Iterable

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.models.entity import Entity
from forwin.models.subworld import SubWorld, SubWorldRosterItem


class EntityKind(StrEnum):
    person = "person"
    organization = "organization"
    location = "location"
    item = "item"
    code = "code"
    concept = "concept"
    placeholder = "placeholder"


class SubworldAdmissionEntry(BaseModel):
    entity_id: str = ""
    name: str
    kind: EntityKind = EntityKind.person
    subworld_id: str = ""
    explicit: bool = False
    auto_carried: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class SubworldAdmissionSignal(BaseModel):
    signal_kind: str = ""
    blocking: bool = False
    entity_name: str = ""
    entity_kind: EntityKind = EntityKind.person
    reason: str = ""


class SubworldAdmission(BaseModel):
    project_id: str
    chapter_number: int
    entries: list[SubworldAdmissionEntry] = Field(default_factory=list)
    code_patterns: list[str] = Field(default_factory=list)

    @property
    def entries_by_name(self) -> dict[str, SubworldAdmissionEntry]:
        return {entry.name: entry for entry in self.entries if entry.name}


_KIND_ALIASES = {
    "character": EntityKind.person,
    "named_person": EntityKind.person,
    "person": EntityKind.person,
    "human": EntityKind.person,
    "faction": EntityKind.organization,
    "guild": EntityKind.organization,
    "government": EntityKind.organization,
    "polity": EntityKind.organization,
    "organization": EntityKind.organization,
    "institution": EntityKind.organization,
    "region": EntityKind.location,
    "place": EntityKind.location,
    "site": EntityKind.location,
    "location": EntityKind.location,
    "item": EntityKind.item,
    "artifact": EntityKind.item,
    "document": EntityKind.item,
    "archive_code": EntityKind.code,
    "system_id": EntityKind.code,
    "code": EntityKind.code,
    "rule": EntityKind.concept,
    "protocol": EntityKind.concept,
    "law": EntityKind.concept,
    "concept": EntityKind.concept,
    "placeholder": EntityKind.placeholder,
}

_AUTO_CARRY_KINDS = {
    EntityKind.person,
    EntityKind.organization,
    EntityKind.location,
    EntityKind.item,
    EntityKind.concept,
}


def normalize_entity_kind(value: Any) -> EntityKind:
    normalized = str(value or "").strip().lower()
    return _KIND_ALIASES.get(normalized, EntityKind.person)


def build_subworld_admission(
    *,
    session: Session,
    project_id: str,
    chapter_number: int,
    active_subworld_ids: list[str] | None = None,
    window_chapters: int = 5,
    code_patterns: list[str] | None = None,
) -> SubworldAdmission:
    active_ids = list(active_subworld_ids or [])
    if not active_ids:
        active_ids = _fallback_active_subworld_ids(session=session, project_id=project_id)
    roster_items = _load_roster_items(session=session, project_id=project_id, subworld_ids=active_ids)
    canon_entities = _load_canon_entities(
        session=session,
        project_id=project_id,
        chapter_number=chapter_number,
        window_chapters=window_chapters,
    )
    return build_subworld_admission_from_rows(
        project_id=project_id,
        chapter_number=chapter_number,
        roster_items=roster_items,
        canon_entities=canon_entities,
        code_patterns=code_patterns,
    )


def build_subworld_admission_from_rows(
    *,
    project_id: str,
    chapter_number: int,
    roster_items: Iterable[Any],
    canon_entities: Iterable[Any],
    code_patterns: list[str] | None = None,
) -> SubworldAdmission:
    entries: dict[str, SubworldAdmissionEntry] = {}
    for item in roster_items:
        name = str(_row_value(item, "display_name") or "").strip()
        entity_id = str(_row_value(item, "entity_id") or "").strip()
        if not name and entity_id:
            name = entity_id
        if not name:
            continue
        metadata = _json_object(_row_value(item, "metadata_json") or {})
        entries[name] = SubworldAdmissionEntry(
            entity_id=entity_id,
            name=name,
            kind=normalize_entity_kind(_row_value(item, "entity_kind")),
            subworld_id=str(_row_value(item, "subworld_id") or ""),
            explicit=True,
            auto_carried=bool(metadata.get("auto_carried", False)),
            metadata=metadata,
        )
    for entity in canon_entities:
        name = str(_row_value(entity, "name") or "").strip()
        if not name or name in entries:
            continue
        kind = normalize_entity_kind(_row_value(entity, "kind"))
        if kind not in _AUTO_CARRY_KINDS:
            continue
        if _is_sunset(entity, chapter_number):
            continue
        entries[name] = SubworldAdmissionEntry(
            entity_id=str(_row_value(entity, "id") or _row_value(entity, "entity_id") or ""),
            name=name,
            kind=kind,
            auto_carried=True,
            metadata={"auto_carried": True, "source": "canon_entity"},
        )
    return SubworldAdmission(
        project_id=project_id,
        chapter_number=int(chapter_number or 0),
        entries=list(entries.values()),
        code_patterns=list(code_patterns or []),
    )


def classify_admission_signal(
    admission: SubworldAdmission,
    *,
    entity_name: str,
    entity_kind: str = "",
) -> SubworldAdmissionSignal:
    name = str(entity_name or "").strip()
    kind = normalize_entity_kind(entity_kind)
    if not name:
        return SubworldAdmissionSignal()
    entry = admission.entries_by_name.get(name)
    if entry is not None:
        if entry.auto_carried and not entry.explicit:
            return SubworldAdmissionSignal(
                signal_kind="subworld_admission_missing_canon_entity",
                blocking=False,
                entity_name=name,
                entity_kind=entry.kind,
                reason="canon entity missing from explicit chapter admission roster",
            )
        return SubworldAdmissionSignal(entity_name=name, entity_kind=entry.kind)
    if kind == EntityKind.code and _matches_code_pattern(name, admission.code_patterns):
        return SubworldAdmissionSignal(entity_name=name, entity_kind=EntityKind.code)
    return SubworldAdmissionSignal(
        signal_kind="subworld_admission_unauthorized_new_entity",
        blocking=True,
        entity_name=name,
        entity_kind=kind,
        reason="entity is not admitted and is not known canon",
    )


def _load_roster_items(
    *,
    session: Session,
    project_id: str,
    subworld_ids: list[str],
) -> list[SubWorldRosterItem]:
    stmt = select(SubWorldRosterItem).where(SubWorldRosterItem.project_id == project_id)
    if subworld_ids:
        stmt = stmt.where(SubWorldRosterItem.subworld_id.in_(subworld_ids))
    return list(session.execute(stmt).scalars().all())


def _load_canon_entities(
    *,
    session: Session,
    project_id: str,
    chapter_number: int,
    window_chapters: int,
) -> list[Entity]:
    del window_chapters
    return list(
        session.execute(
            select(Entity).where(
                Entity.project_id == project_id,
                Entity.is_active == True,  # noqa: E712
                Entity.created_at_chapter <= max(0, int(chapter_number or 0) - 1),
            )
        ).scalars().all()
    )


def _fallback_active_subworld_ids(*, session: Session, project_id: str) -> list[str]:
    rows = session.execute(
        select(SubWorld.id).where(
            SubWorld.project_id == project_id,
            SubWorld.status == "active",
        )
    ).all()
    return [str(row[0] or "") for row in rows if str(row[0] or "").strip()]


def _matches_code_pattern(value: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        try:
            if re.match(pattern, value):
                return True
        except re.error:
            continue
    return False


def _is_sunset(entity: Any, chapter_number: int) -> bool:
    metadata = _json_object(_row_value(entity, "metadata_json") or _row_value(entity, "metadata") or {})
    sunset = metadata.get("sunset_chapter")
    if sunset in {None, ""}:
        return False
    try:
        return int(sunset) < int(chapter_number or 0)
    except (TypeError, ValueError):
        return False


def _row_value(row: Any, key: str, default: Any = "") -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


__all__ = [
    "EntityKind",
    "SubworldAdmission",
    "SubworldAdmissionEntry",
    "SubworldAdmissionSignal",
    "build_subworld_admission",
    "build_subworld_admission_from_rows",
    "classify_admission_signal",
    "normalize_entity_kind",
]
