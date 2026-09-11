"""Disposable project replica for historical body validation.

Only this private database may rewind derived rows. The source transaction is
used to capture rows and identities, then closed before extraction/model calls.
Supported prefix provenance is an unbroken active acceptance manifest with
surviving snapshots/deltas and reversible structural patches. Missing before
images, unversioned promise rewrites and narrative updates fail closed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Self

from sqlalchemy import create_engine, delete, or_, select, update
from sqlalchemy.orm import Session

from forwin.book_state.repository import BookStateRepository
from forwin.candidate_drafts import candidate_plan_revision
from forwin.models.base import Base
from forwin.models.canon import CanonCommitRecord
from forwin.models.draft import CandidateDraftRecord, ChapterDraft
from forwin.models.project import ChapterPlan, Project

from .revision_validation import RevisionChapterInput, RevisionManifest, revision_digest


class PrefixProvenanceUnknown(ValueError):
    """Historical data cannot prove the exact input state to a revision."""


_EXCLUDED = (
    "publisher_",
    "background_",
    "generation_",
    "serial_capacity",
    "outbox",
    "task",
    "maintenance",
    "knowledge_edit",
    "canon_revision_validations",
)


@dataclass(frozen=True)
class RevisionSnapshot:
    manifest: RevisionManifest
    tables: dict[str, tuple[dict[str, Any], ...]]


def capture_revision(
    session: Session,
    *,
    project_id: str,
    candidate_id: str,
    model_identity: dict,
    policy_fingerprint: str,
) -> RevisionSnapshot:
    """Capture under a caller-owned consistent read transaction, without writes."""
    project = session.get(Project, project_id)
    candidate = session.get(CandidateDraftRecord, candidate_id)
    if project is None or candidate is None or candidate.project_id != project_id:
        raise PrefixProvenanceUnknown("revision candidate ownership is missing")
    active = list(
        session.execute(
            select(ChapterPlan, CanonCommitRecord)
            .outerjoin(
                CanonCommitRecord, CanonCommitRecord.id == ChapterPlan.active_commit_id
            )
            .where(
                ChapterPlan.project_id == project_id,
                or_(
                    ChapterPlan.status == "accepted",
                    ChapterPlan.active_commit_id.is_not(None),
                ),
            )
            .order_by(ChapterPlan.chapter_number)
        ).all()
    )
    if not active or [p.chapter_number for p, c in active] != list(
        range(1, active[-1][0].chapter_number + 1)
    ):
        raise PrefixProvenanceUnknown("active accepted prefix is not complete")
    inputs = []
    baseline = []
    for chapter, commit in active:
        if (
            commit is None
            or commit.status != "committed"
            or chapter.status != "accepted"
            or commit.chapter_plan_id != chapter.id
            or commit.chapter_number != chapter.chapter_number
        ):
            raise PrefixProvenanceUnknown("active accepted chapter ownership changed")
        accepted_candidate = session.get(CandidateDraftRecord, commit.candidate_id)
        accepted_draft = (
            session.get(ChapterDraft, accepted_candidate.candidate_draft_id)
            if accepted_candidate
            else None
        )
        from forwin.candidate_drafts import candidate_body_hash

        if (
            accepted_draft is None
            or candidate_body_hash(accepted_draft.body_text)
            != accepted_candidate.body_hash
            or accepted_draft.chapter_plan_id != chapter.id
        ):
            raise PrefixProvenanceUnknown("accepted baseline draft is missing")
        baseline.append(
            {
                "chapter_plan_id": chapter.id,
                "chapter_number": chapter.chapter_number,
                "commit_id": commit.id,
                "candidate_id": accepted_candidate.id,
                "draft_id": accepted_draft.id,
                "body_sha256": accepted_candidate.body_hash,
                "title": commit.chapter_title,
                "plan_revision": candidate_plan_revision(chapter),
            }
        )
        if chapter.chapter_number < candidate.chapter_number:
            continue
        source = (
            candidate
            if chapter.chapter_number == candidate.chapter_number
            else session.get(CandidateDraftRecord, commit.candidate_id)
        )
        draft = session.get(ChapterDraft, source.candidate_draft_id) if source else None
        if draft is None or draft.chapter_plan_id != chapter.id:
            raise PrefixProvenanceUnknown("accepted body identity is missing")
        # A changed candidate has its own immutable prepared title; otherwise use
        # the active acceptance title, never a newer mutable plan title.
        metadata = json.loads(source.metadata_json or "{}")
        title = (
            str(metadata.get("title") or commit.chapter_title)
            if source.id != commit.candidate_id
            else commit.chapter_title
        )
        inputs.append(
            RevisionChapterInput(
                chapter_plan_id=chapter.id,
                chapter_number=chapter.chapter_number,
                base_commit_id=commit.id,
                candidate_id=source.id,
                draft_id=draft.id,
                title=title,
                body=draft.body_text,
                body_sha256=source.body_hash,
            )
        )
    from .outbox_events import publisher_binding_snapshot

    manifest = RevisionManifest(
        project_id=project_id,
        base_book_revision=project.book_revision,
        from_chapter=candidate.chapter_number,
        through_chapter=active[-1][0].chapter_number,
        chapters=tuple(inputs),
        model_identity=model_identity,
        policy_fingerprint=policy_fingerprint,
        baseline_identity=tuple(baseline),
        policy_version=project.runtime_policy_version,
        publisher_bindings=tuple(
            binding.model_dump(mode="json")
            for binding in publisher_binding_snapshot(
                project.automation_json, default_book_name=project.title
            )
        ),
    )
    tables: dict[str, tuple[dict[str, Any], ...]] = {}
    allowed = [
        t for t in Base.metadata.tables.values() if not t.name.startswith(_EXCLUDED)
    ]
    for table in allowed:
        condition = (
            table.c.id == project_id
            if table.name == "projects"
            else table.c.project_id == project_id
            if "project_id" in table.c
            else None
        )
        if condition is not None:
            tables[table.name] = tuple(
                dict(row)
                for row in session.execute(select(table).where(condition)).mappings()
            )
    # Drafts, reviews and other project-local children can lack project_id.
    remaining = [t for t in allowed if t.name not in tables]
    while remaining:
        progress = False
        for table in list(remaining):
            conditions = []
            for foreign in table.foreign_keys:
                parent = foreign.column.table.name
                if parent in tables:
                    values = {row[foreign.column.name] for row in tables[parent]}
                    if values:
                        conditions.append(foreign.parent.in_(values))
            if conditions:
                tables[table.name] = tuple(
                    dict(row)
                    for row in session.execute(
                        select(table).where(or_(*conditions))
                    ).mappings()
                )
                remaining.remove(table)
                progress = True
        if not progress:
            break
    # Content reviews may be appended after capture; only source state and
    # accepted evidence participate in this baseline fingerprint.
    excluded = {
        "projects",
        "chapter_plans",
        "chapter_drafts",
        "chapter_reviews",
        "candidate_draft_records",
        "decision_events",
        "canon_admission_runs",
        "quality_analysis_runs",
    }
    source_state = {
        name: sorted(
            (
                {
                    k: v.isoformat() if hasattr(v, "isoformat") else v
                    for k, v in row.items()
                }
                for row in rows
            ),
            key=lambda row: json.dumps(row, sort_keys=True),
        )
        for name, rows in tables.items()
        if name not in excluded
    }
    active_ids = {commit.id for _, commit in active}
    evidence_rows = [
        row
        for row in tables.get("decision_events", ())
        if (
            row["event_type"] == "canon_entity_before_image"
            and row["related_object_id"] in active_ids
        )
        or row["event_type"] == "canon_obligation_before_image"
    ]
    accepted_run_ids = {
        row["source_admission_run_id"]
        for row in tables.get("canon_quality_acceptance_evidence", ())
        if row["acceptance_id"] in active_ids and row["source_admission_run_id"]
    }
    selected = {
        "before_image_events": evidence_rows,
        "accepted_quality_runs": [
            row
            for row in tables.get("canon_admission_runs", ())
            if row["id"] in accepted_run_ids
        ],
    }
    source_state.update(
        {
            name: sorted(
                (
                    {
                        key: value.isoformat() if hasattr(value, "isoformat") else value
                        for key, value in row.items()
                    }
                    for row in rows
                ),
                key=lambda row: row["id"],
            )
            for name, rows in selected.items()
        }
    )
    manifest = manifest.model_copy(
        update={"source_state_fingerprint": revision_digest(source_state)}
    )
    snapshot = RevisionSnapshot(manifest, tables)
    return snapshot


def _verify_manifest_evidence(
    snapshot: RevisionSnapshot, *, active_ids: set[str]
) -> None:
    baseline = {
        row["chapter_number"]: row for row in snapshot.manifest.baseline_identity
    }
    changed = {
        chapter.chapter_number: chapter
        for chapter in snapshot.manifest.chapters
        if chapter.draft_id != baseline[chapter.chapter_number]["draft_id"]
    }
    for row in snapshot.tables.get("narrative_obligations", ()):
        chapter = changed.get(row["origin_chapter_number"])
        if chapter is None:
            continue
        origin = (
            json.loads(row["metadata_json"] or "{}")
            .get("_canon_obligation_provenance", {})
            .get("origin_acceptance_id")
        )
        prior = baseline[chapter.chapter_number]
        if origin == prior["commit_id"] and row["origin_draft_id"] == prior["draft_id"]:
            # The existing form has no retained/removed/fulfilled disposition for
            # a new body's own obligations. Never infer it from similar wording.
            raise PrefixProvenanceUnknown(
                f"origin obligation requires grounded new-body disposition: {row['id']}"
            )
    known_deltas = {row["id"]: row for row in snapshot.tables.get("graph_deltas", ())}
    state_sources = {
        (row["delta_id"], json.loads(row["metadata_json"] or "{}").get("node_id"))
        for row in snapshot.tables.get("graph_delta_patches", ())
        if row["patch_type"] == "node"
        and (
            row["op"] == "create"
            or row["field_path"] == "state"
            or row["field_path"].startswith("state.")
        )
    }
    for row in snapshot.tables.get("world_node_states", ()):
        if row["as_of_chapter"] <= 0:
            continue  # Explicit Genesis/bootstrap state has no chapter acceptance.
        delta = known_deltas.get(row["source_delta_id"])
        if (
            delta is None
            or delta["chapter_number"] != row["as_of_chapter"]
            or (row["source_delta_id"], row["node_id"]) not in state_sources
        ):
            raise PrefixProvenanceUnknown(
                "positive-chapter world state lacks proven delta identity"
            )
    identities = {
        name: {row["id"] for row in rows if "id" in row}
        for name, rows in snapshot.tables.items()
    }
    owned_deltas = {
        delta
        for commit in snapshot.tables.get("canon_commit_records", ())
        for delta in json.loads(commit["graph_delta_ids_json"] or "[]")
    }
    if any(
        row["chapter_number"] >= snapshot.manifest.from_chapter
        and row["id"] not in owned_deltas
        for row in snapshot.tables.get("graph_deltas", ())
    ):
        raise PrefixProvenanceUnknown(
            "unowned suffix delta manifest lacks accepted prefix provenance"
        )
    for commit in snapshot.tables.get("canon_commit_records", ()):
        if commit["id"] not in active_ids:
            continue
        manifest = json.loads(commit["graph_delta_ids_json"] or "[]")
        if not isinstance(manifest, list) or not set(manifest).issubset(
            identities.get("graph_deltas", set())
        ):
            raise PrefixProvenanceUnknown(
                f"missing active delta manifest: {commit['id']}"
            )
        for field, table in (
            ("world_snapshot_id", "world_snapshots"),
            ("map_snapshot_id", "map_snapshots"),
        ):
            if not commit[field] or commit[field] not in identities.get(table, set()):
                raise PrefixProvenanceUnknown(
                    f"missing active snapshot provenance: {commit['id']}:{field}"
                )


class CandidateReplica:
    """Finite local scratch state, with no scheduler, publisher or Canon owner."""

    def __init__(self, snapshot: RevisionSnapshot) -> None:
        self.snapshot = snapshot
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        self.session: Session | None = None

    def __enter__(self) -> Self:
        try:
            _verify_manifest_evidence(
                self.snapshot,
                active_ids={
                    row["commit_id"] for row in self.snapshot.manifest.baseline_identity
                },
            )
            Base.metadata.create_all(self.engine)
            with self.engine.begin() as connection:
                for name, rows in self.snapshot.tables.items():
                    if rows:
                        connection.execute(
                            Base.metadata.tables[name].insert(), list(rows)
                        )
            self.session = Session(self.engine, expire_on_commit=False)
            self._restore_prefix()
            return self
        except Exception:
            self.close()
            raise

    def _restore_prefix(self) -> None:
        assert self.session is not None
        manifest = self.snapshot.manifest
        from forwin.canon_quality.repository import CanonQualityRepository

        CanonQualityRepository(self.session).restore_prefix(
            project_id=manifest.project_id,
            active_commit_ids={
                row["chapter_number"]: row["commit_id"]
                for row in manifest.baseline_identity
            },
            from_chapter=manifest.from_chapter,
        )
        from forwin.narrative_obligations.repository import (
            NarrativeObligationRepository,
        )

        NarrativeObligationRepository(self.session).restore_prefix(
            project_id=manifest.project_id,
            active_commit_ids={
                row["chapter_number"]: row["commit_id"]
                for row in manifest.baseline_identity
            },
            from_chapter=manifest.from_chapter,
        )
        repo = BookStateRepository(self.session)
        deltas = repo.list_graph_deltas(
            manifest.project_id,
            after_chapter=manifest.from_chapter - 1,
            through_chapter=manifest.through_chapter,
        )
        if any(
            patch.op != "create"
            for delta in deltas
            for patch in delta.narrative_patches
        ):
            raise PrefixProvenanceUnknown(
                "unversioned narrative update has no proven prefix before-image"
            )
        try:
            repo.invalidate_project_range(
                manifest.project_id,
                from_chapter=manifest.from_chapter,
                through_chapter=manifest.through_chapter,
                ordered_deltas=deltas,
            )
        except ValueError as exc:
            raise PrefixProvenanceUnknown(str(exc)) from exc
        from .revision_registry import restore_registry_prefix

        restore_registry_prefix(self.session, manifest)
        for name in (
            "graph_delta_patches",
            "graph_deltas",
            "character_state_transitions",
            "artifact_collection_ledgers",
            "countdown_ledgers",
            "reveal_registry_entries",
            "canon_quality_signals",
            "chapter_body_metrics",
            "quality_analysis_runs",
            "canon_admission_runs",
        ):
            table = Base.metadata.tables.get(name)
            if table is not None and "chapter_number" in table.c:
                self.session.execute(
                    delete(table).where(
                        table.c.project_id == manifest.project_id,
                        table.c.chapter_number >= manifest.from_chapter,
                    )
                )
        self.session.execute(
            update(ChapterPlan)
            .where(
                ChapterPlan.project_id == manifest.project_id,
                ChapterPlan.chapter_number >= manifest.from_chapter,
            )
            .values(active_commit_id=None, status="planned")
        )
        self.session.flush()
        from forwin.book_state.projection import BookStateProjection, _digest

        runtime = BookStateProjection(self.session).load_runtime_as_of(
            manifest.project_id, as_of_chapter=manifest.from_chapter - 1
        )
        prior = repo.latest_world_snapshot(
            manifest.project_id, manifest.from_chapter - 1
        )
        if manifest.from_chapter > 1:
            if (
                prior is None
                or prior.as_of_chapter != manifest.from_chapter - 1
                or runtime.world.objective_digest() != prior.objective_graph_digest
            ):
                raise PrefixProvenanceUnknown(
                    "reconstructed prefix objective state differs from accepted snapshot"
                )
            map_digest = _digest(
                {
                    "nodes": runtime.map.nodes_by_id,
                    "edges": {
                        key: value
                        for key, value in runtime.map.edges_by_id.items()
                        if "__reverse" not in key
                    },
                }
            )
            if map_digest != prior.map_graph_digest:
                raise PrefixProvenanceUnknown(
                    "reconstructed prefix map differs from accepted snapshot"
                )
        self.session.commit()

    def close(self) -> None:
        if self.session is not None:
            self.session.close()
        self.engine.dispose()

    def __exit__(self, *exc) -> None:
        self.close()
