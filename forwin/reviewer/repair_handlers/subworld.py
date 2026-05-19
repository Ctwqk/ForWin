from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.models.base import new_id
from forwin.models.entity import Entity
from forwin.models.subworld import SubWorld, SubWorldRosterItem
from forwin.planning.subworld_admission import normalize_entity_kind
from forwin.reviewer.repair_scope_router import RoutedSignal


@dataclass(slots=True)
class SubworldRepairReport:
    applied: int = 0
    rejected: int = 0
    added_names: list[str] = field(default_factory=list)
    rejection_reasons: list[str] = field(default_factory=list)


def apply_subworld_admission_repairs(
    *,
    session: Session,
    project_id: str,
    chapter_number: int,
    signals: list[RoutedSignal],
    subworld_id: str = "",
) -> SubworldRepairReport:
    report = SubworldRepairReport()
    target_subworld_id = subworld_id or _default_subworld_id(session=session, project_id=project_id)
    if not target_subworld_id:
        report.rejected += len(signals)
        report.rejection_reasons.append("no active subworld found")
        return report

    for signal in signals:
        if signal.kind != "subworld_admission_missing_canon_entity":
            report.rejected += 1
            report.rejection_reasons.append(f"unsupported signal kind: {signal.kind}")
            continue
        entity_name = _signal_entity_name(signal)
        entity = _find_entity(session=session, project_id=project_id, entity_name=entity_name)
        if entity is None:
            report.rejected += 1
            report.rejection_reasons.append(f"canon entity not found: {entity_name}")
            continue
        existing = _existing_roster_item(
            session=session,
            project_id=project_id,
            subworld_id=target_subworld_id,
            entity_id=entity.id,
        )
        if existing is None:
            session.add(
                SubWorldRosterItem(
                    id=new_id(),
                    project_id=project_id,
                    subworld_id=target_subworld_id,
                    entity_id=entity.id,
                    entity_kind=normalize_entity_kind(entity.kind).value,
                    display_name=entity.name,
                    slot_key="",
                    role_hint="auto-carried canon entity",
                    description=entity.description,
                    is_core=False,
                    status="activated_named",
                    activation_chapter=int(chapter_number or 0),
                    metadata_json=json.dumps(
                        {
                            "auto_carried": True,
                            "source": "subworld_admission_repair",
                            "source_signal_id": signal.source_signal_id,
                        },
                        ensure_ascii=False,
                    ),
                )
            )
            report.applied += 1
            report.added_names.append(entity.name)
        else:
            metadata = _json_object(existing.metadata_json)
            metadata["auto_carried"] = True
            metadata.setdefault("source", "subworld_admission_repair")
            existing.metadata_json = json.dumps(metadata, ensure_ascii=False)
            existing.status = "activated_named"
            session.add(existing)
            report.applied += 1
            report.added_names.append(existing.display_name or entity.name)
    session.flush()
    return report


def _signal_entity_name(signal: RoutedSignal) -> str:
    payload_name = str(signal.payload.get("entity_name") or "").strip()
    if payload_name:
        return payload_name
    return str(signal.subject_key or signal.description or "").strip()


def _find_entity(*, session: Session, project_id: str, entity_name: str) -> Entity | None:
    if not entity_name:
        return None
    return session.execute(
        select(Entity).where(
            Entity.project_id == project_id,
            Entity.name == entity_name,
        )
    ).scalars().first()


def _default_subworld_id(*, session: Session, project_id: str) -> str:
    row = session.execute(
        select(SubWorld.id)
        .where(SubWorld.project_id == project_id, SubWorld.status == "active")
        .order_by(SubWorld.scope.asc(), SubWorld.created_at.asc(), SubWorld.id.asc())
    ).first()
    return str(row[0] or "") if row else ""


def _existing_roster_item(
    *,
    session: Session,
    project_id: str,
    subworld_id: str,
    entity_id: str,
) -> SubWorldRosterItem | None:
    return session.execute(
        select(SubWorldRosterItem).where(
            SubWorldRosterItem.project_id == project_id,
            SubWorldRosterItem.subworld_id == subworld_id,
            SubWorldRosterItem.entity_id == entity_id,
        )
    ).scalars().first()


def _json_object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


__all__ = ["SubworldRepairReport", "apply_subworld_admission_repairs"]
