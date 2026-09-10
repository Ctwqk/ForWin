"""Transaction-bound before/after images for the entity committer's touched rows."""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import DateTime, delete, select

from forwin.models.audit import DecisionEvent
from forwin.models.entity import Entity, EntityAlias
from forwin.models.subworld import SubWorldRosterItem

from .revision_validation import revision_digest

EVENT = "canon_entity_before_image"
MODELS = (Entity, EntityAlias, SubWorldRosterItem)


def registry_scope(session, project_id, plan):
    ids = sorted(
        {d.entity_id for d in plan.decisions if d.action != "background_generic"}
    )
    names = {
        name
        for d in plan.decisions
        for name in (d.mention_name, d.canonical_name, *d.aliases)
        if name
    }
    for row in session.scalars(
        select(Entity).where(Entity.project_id == project_id, Entity.id.in_(ids))
    ):
        names.update((row.name, *json.loads(row.aliases_json or "[]")))
    aliases = sorted({a for d in plan.decisions for a in d.aliases if a})
    roster = sorted(
        session.scalars(
            select(SubWorldRosterItem.id).where(
                SubWorldRosterItem.project_id == project_id,
                SubWorldRosterItem.entity_kind == "character",
                SubWorldRosterItem.display_name.in_(names),
            )
        )
    )
    return {"entity_ids": ids, "aliases": aliases, "roster_ids": roster}


def registry_rows(session, project_id, scope):
    conditions = (
        Entity.id.in_(scope["entity_ids"]),
        EntityAlias.alias.in_(scope["aliases"]),
        SubWorldRosterItem.id.in_(scope["roster_ids"]),
    )
    result = {}
    for model, condition in zip(MODELS, conditions):
        rows = session.execute(
            select(model.__table__).where(model.project_id == project_id, condition)
        ).mappings()
        result[model.__tablename__] = sorted(
            [
                {
                    key: value.isoformat() if isinstance(value, datetime) else value
                    for key, value in row.items()
                }
                for row in rows
            ],
            key=lambda row: row["id"],
        )
    return result


def save_registry_evidence(session, *, project_id, plan, acceptance_id, scope, before):
    payload = {
        "version": 1,
        "project_id": project_id,
        "acceptance_id": acceptance_id,
        "chapter_number": plan.chapter_number,
        "plan_sha256": revision_digest(plan.model_dump(mode="json")),
        "scope": scope,
        "before": before,
        "after": registry_rows(session, project_id, scope),
    }
    session.add(
        DecisionEvent(
            project_id=project_id,
            chapter_number=plan.chapter_number,
            event_type=EVENT,
            event_family="business_event",
            scope="chapter",
            related_object_type="canon_commit",
            related_object_id=acceptance_id,
            summary="Canon entity registry before/after evidence",
            payload_json=json.dumps(payload, ensure_ascii=False, sort_keys=True),
        )
    )


def restore_registry_prefix(session, manifest):
    from forwin.models.canon import CanonCommitRecord
    from forwin.models.draft import CandidateDraftRecord

    from .revision_replica import PrefixProvenanceUnknown

    for chapter in reversed(manifest.chapters):
        commit = session.get(CanonCommitRecord, chapter.base_commit_id)
        events = list(
            session.scalars(
                select(DecisionEvent).where(
                    DecisionEvent.project_id == manifest.project_id,
                    DecisionEvent.related_object_id == commit.id,
                    DecisionEvent.event_type == EVENT,
                )
            )
        )
        if len(events) != 1:
            raise PrefixProvenanceUnknown(
                f"registry before-image missing or ambiguous: {commit.id}"
            )
        evidence = json.loads(events[0].payload_json)
        if (
            evidence.get("acceptance_id") != commit.id
            or evidence.get("project_id") != manifest.project_id
            or evidence.get("chapter_number") != chapter.chapter_number
        ):
            raise PrefixProvenanceUnknown(
                "registry evidence acceptance ownership mismatch"
            )
        original = session.get(CandidateDraftRecord, commit.candidate_id)
        prepared = json.loads(original.canon_commit_plan_json or "{}")
        expected = json.loads(commit.result_json or "{}").get(
            "entity_plan_sha256"
        ) or revision_digest(prepared.get("entity_admission_plan", {}))
        if evidence.get("plan_sha256") != expected:
            raise PrefixProvenanceUnknown(
                "registry before-image does not match accepted entity plan"
            )
        scope = evidence["scope"]
        if registry_rows(session, manifest.project_id, scope) != evidence["after"]:
            raise PrefixProvenanceUnknown(
                "registry rows changed outside proven acceptance history"
            )
        # Child rows go first; all work is confined to the scratch database.
        for model, key in (
            (EntityAlias, "aliases"),
            (SubWorldRosterItem, "roster_ids"),
            (Entity, "entity_ids"),
        ):
            condition = (
                model.alias.in_(scope[key])
                if model is EntityAlias
                else model.id.in_(scope[key])
            )
            session.execute(
                delete(model).where(model.project_id == manifest.project_id, condition)
            )
        session.flush()
        for model in MODELS:
            for raw in evidence["before"][model.__tablename__]:
                if raw.get("project_id") != manifest.project_id:
                    raise PrefixProvenanceUnknown(
                        "registry before-image project mismatch"
                    )
                row = dict(raw)
                for column in model.__table__.columns:
                    if (
                        isinstance(column.type, DateTime)
                        and row[column.name] is not None
                    ):
                        row[column.name] = datetime.fromisoformat(row[column.name])
                session.execute(model.__table__.insert().values(**row))
        session.expire_all()
