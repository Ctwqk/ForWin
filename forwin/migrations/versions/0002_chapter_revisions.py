"""Stable acceptance revisions and publication protection; preserve existing evidence."""

from __future__ import annotations

import hashlib
import json
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision = "0002_chapter_revisions"
down_revision = "0001_v5_recovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    duplicate = bind.execute(
        sa.text("""SELECT project_id, chapter_number FROM chapter_plans
        WHERE chapter_number > 0 GROUP BY project_id, chapter_number HAVING count(*) > 1 LIMIT 1""")
    ).first()
    if duplicate:
        raise ValueError(
            f"ambiguous stable chapter identity: {duplicate}; reconcile duplicate plans before migration"
        )
    # Validate legacy ownership before changing any identity. Never infer a
    # negative archive's original chapter from archive order or timestamps.
    rows = list(
        bind.execute(
            sa.text("""
        SELECT c.id, c.project_id, c.chapter_number, c.status, c.result_json,
               c.graph_delta_ids_json, c.world_snapshot_id, c.map_snapshot_id,
               d.chapter_plan_id, d.chapter_number AS candidate_chapter,
               d.metadata_json, d.canon_commit_plan_json, p.title AS plan_title,
               p.chapter_number AS plan_chapter, p.project_id AS plan_project
        FROM canon_commit_records c
        LEFT JOIN candidate_draft_records d ON d.id=c.candidate_id
        LEFT JOIN chapter_plans p ON p.id=d.chapter_plan_id
        ORDER BY c.created_at, c.id
    """)
        ).mappings()
    )
    for row in rows:
        if (
            not row["chapter_plan_id"]
            or row["project_id"] != row["plan_project"]
            or row["candidate_chapter"] != row["plan_chapter"]
        ):
            raise ValueError(f"ambiguous Canon chapter ownership: {row['id']}")
        if row["chapter_number"] < 1:
            try:
                original = json.loads(row["result_json"])["original_chapter_number"]
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"ambiguous archived Canon identity: {row['id']}"
                ) from exc
            if row["status"] != "superseded" or original != row["plan_chapter"]:
                raise ValueError(f"ambiguous archived Canon identity: {row['id']}")
        elif row["chapter_number"] != row["plan_chapter"]:
            raise ValueError(f"ambiguous Canon chapter ownership: {row['id']}")
    by_id = {row["id"]: row for row in rows}
    for row in rows:
        result = json.loads(row["result_json"])
        if row["chapter_number"] < 1:
            replacement_id = result.get("superseded_by_commit_id")
            replacement = by_id.get(replacement_id)
            markers = bind.execute(
                sa.text("""SELECT payload_json FROM decision_events
                WHERE project_id=:project AND chapter_number=:chapter AND actor_type='api'
                AND event_type='retry_attempt'"""),
                {"project": row["project_id"], "chapter": row["plan_chapter"]},
            ).scalars()
            matching = [
                json.loads(raw)
                for raw in markers
                if json.loads(raw).get("previous_commit_id") == row["id"]
                and json.loads(raw).get("replacement_commit_id") == replacement_id
            ]
            if (
                replacement is None
                or replacement["chapter_plan_id"] != row["chapter_plan_id"]
                or len(matching) != 1
            ):
                raise ValueError(f"ambiguous archived Canon audit: {row['id']}")
        ids = json.loads(row["graph_delta_ids_json"])
        if not isinstance(ids, list) or any(
            not isinstance(value, str) for value in ids
        ):
            raise ValueError(
                f"missing Canon evidence: invalid manifest for {row['id']}"
            )
        for delta_id in ids:
            if not bind.scalar(
                sa.text(
                    "SELECT id FROM graph_deltas WHERE id=:id AND project_id=:project"
                ),
                {"id": delta_id, "project": row["project_id"]},
            ):
                raise ValueError(
                    f"missing Canon evidence: graph delta {delta_id} for {row['id']}"
                )
        for table, field in [
            ("world_snapshots", "world_snapshot_id"),
            ("map_snapshots", "map_snapshot_id"),
        ]:
            if row[field] and not bind.scalar(
                sa.text(f"SELECT id FROM {table} WHERE id=:id AND project_id=:project"),
                {"id": row[field], "project": row["project_id"]},
            ):
                raise ValueError(
                    f"missing Canon evidence: {field} {row[field]} for {row['id']}"
                )
    unresolved = bind.scalar(
        sa.text("""SELECT j.id FROM publisher_upload_jobs j
        WHERE j.project_id <> '' AND j.task_kind='chapter_upload'
        AND j.canon_commit_id IS NULL AND (
            j.status IN ('succeeded','running','reconciling','uncertain','terminating')
            OR EXISTS (SELECT 1 FROM publisher_upload_receipts r WHERE r.upload_job_id=j.id)
            OR EXISTS (SELECT 1 FROM publisher_upload_attempts a WHERE a.upload_job_id=j.id
                       AND a.phase IN ('mutation_started','receipt_observed')))
        LIMIT 1""")
    )
    if unresolved:
        raise ValueError(f"unresolved publication identity: {unresolved}")
    active = {}
    for row in rows:
        if row["status"] == "committed":
            if row["chapter_plan_id"] in active:
                raise ValueError("ambiguous active Canon identity")
            active[row["chapter_plan_id"]] = row["id"]

    op.create_index(
        "ux_chapter_plans_stable_number",
        "chapter_plans",
        ["project_id", "chapter_number"],
        unique=True,
        postgresql_where=sa.text("chapter_number > 0"),
    )
    op.add_column(
        "projects",
        sa.Column("book_revision", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "chapter_plans", sa.Column("active_commit_id", sa.String(), nullable=True)
    )
    op.add_column(
        "canon_commit_records", sa.Column("chapter_plan_id", sa.String(), nullable=True)
    )
    op.add_column(
        "canon_commit_records",
        sa.Column("chapter_title", sa.String(), nullable=False, server_default=""),
    )
    op.add_column(
        "canon_commit_records",
        sa.Column(
            "acceptance_revision", sa.Integer(), nullable=False, server_default="1"
        ),
    )
    op.add_column(
        "canon_commit_records",
        sa.Column(
            "base_book_revision", sa.Integer(), nullable=False, server_default="0"
        ),
    )
    op.add_column(
        "canon_commit_records",
        sa.Column(
            "production_mode",
            sa.String(),
            nullable=False,
            server_default="daily_serial",
        ),
    )
    op.drop_index("ux_canon_commits_project_chapter", table_name="canon_commit_records")
    op.drop_index("ux_canon_commits_candidate", table_name="canon_commit_records")
    chapter_revisions, book_revisions = {}, {}
    for row in rows:
        chapter_id, project_id = row["chapter_plan_id"], row["project_id"]
        number = chapter_revisions.get(chapter_id, 0) + 1
        book = book_revisions.get(project_id, 0)
        chapter_revisions[chapter_id] = number
        book_revisions[project_id] = book + 1
        bind.execute(
            sa.text("""UPDATE canon_commit_records SET chapter_plan_id=:chapter,
            chapter_number=:number, chapter_title=:title, acceptance_revision=:revision, base_book_revision=:book WHERE id=:id"""),
            {
                "chapter": chapter_id,
                "number": row["plan_chapter"],
                "title": _accepted_title(row),
                "revision": number,
                "book": book,
                "id": row["id"],
            },
        )
    for chapter, commit in active.items():
        bind.execute(
            sa.text(
                "UPDATE chapter_plans SET active_commit_id=:commit WHERE id=:chapter"
            ),
            {"commit": commit, "chapter": chapter},
        )
    for project, count in book_revisions.items():
        bind.execute(
            sa.text("UPDATE projects SET book_revision=:count WHERE id=:project"),
            {"count": count, "project": project},
        )
    op.alter_column("canon_commit_records", "chapter_plan_id", nullable=False)
    op.create_foreign_key(
        "fk_canon_chapter_plan",
        "canon_commit_records",
        "chapter_plans",
        ["chapter_plan_id"],
        ["id"],
    )
    op.create_unique_constraint(
        "uq_canon_commit_chapter_identity",
        "canon_commit_records",
        ["chapter_plan_id", "id"],
    )
    op.create_foreign_key(
        "fk_chapter_active_commit",
        "chapter_plans",
        "canon_commit_records",
        ["id", "active_commit_id"],
        ["chapter_plan_id", "id"],
    )
    op.create_index(
        "ix_canon_commits_candidate", "canon_commit_records", ["candidate_id"]
    )
    op.create_index(
        "ux_canon_commits_chapter_revision",
        "canon_commit_records",
        ["chapter_plan_id", "acceptance_revision"],
        unique=True,
    )
    op.create_table(
        "canon_publication_protections",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=False
        ),
        sa.Column(
            "chapter_plan_id",
            sa.String(),
            sa.ForeignKey("chapter_plans.id"),
            nullable=False,
        ),
        sa.Column("chapter_number", sa.Integer(), nullable=False),
        sa.Column(
            "canon_commit_id",
            sa.String(),
            sa.ForeignKey("canon_commit_records.id"),
            nullable=False,
        ),
        sa.Column("upload_job_id", sa.String(), nullable=False),
        sa.Column("platform_id", sa.String(), nullable=False),
        sa.Column("content_sha256", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("remote_book_id", sa.String(), nullable=False, server_default=""),
        sa.Column("remote_chapter_id", sa.String(), nullable=False, server_default=""),
        sa.Column("evidence_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index(
        "ux_canon_publication_job",
        "canon_publication_protections",
        ["upload_job_id"],
        unique=True,
    )
    op.create_index(
        "ix_canon_publication_range",
        "canon_publication_protections",
        ["project_id", "chapter_number", "state"],
    )
    _backfill_publication_protections(bind)


def _backfill_publication_protections(bind):
    jobs = bind.execute(
        sa.text("""
        SELECT j.*, c.project_id AS canon_project, c.chapter_plan_id,
               c.chapter_number AS canon_chapter, c.candidate_id AS canon_candidate,
               c.chapter_title AS accepted_title, d.body_hash AS candidate_hash,
               t.body_text AS candidate_body
        FROM publisher_upload_jobs j JOIN canon_commit_records c ON c.id=j.canon_commit_id
        JOIN candidate_draft_records d ON d.id=c.candidate_id
        JOIN chapter_drafts t ON t.id=d.candidate_draft_id
    """)
    ).mappings()
    proofs = []
    for job in jobs:
        receipts = list(
            bind.execute(
                sa.text("""SELECT r.*, a.upload_job_id AS attempt_job
            FROM publisher_upload_receipts r
            JOIN publisher_upload_attempts a ON a.id=r.upload_attempt_id
            WHERE r.upload_job_id=:job"""),
                {"job": job["id"]},
            ).mappings()
        )
        started = bind.scalar(
            sa.text("""SELECT id FROM publisher_upload_attempts
            WHERE upload_job_id=:job AND phase IN ('mutation_started','receipt_observed') LIMIT 1"""),
            {"job": job["id"]},
        )
        if not (
            receipts
            or started
            or job["status"]
            in ("reconciling", "uncertain", "succeeded", "running", "terminating")
        ):
            continue
        digest = hashlib.sha256(job["body_text"].encode("utf-8")).hexdigest()
        if (
            job["project_id"] != job["canon_project"]
            or job["chapter_number"] != job["canon_chapter"]
            or job["candidate_id"] != job["canon_candidate"]
            or job["body_text"] != job["candidate_body"]
            or digest != job["body_sha256"]
            or digest != job["candidate_hash"]
        ):
            raise ValueError(f"contradictory publication content identity: {job['id']}")
        audits = [
            json.loads(value).get("legacy_content_identity")
            for value in bind.execute(
                sa.text("""SELECT new_state_json FROM publisher_operator_actions
                WHERE upload_job_id=:job AND action='legacy_content_identity'"""),
                {"job": job["id"]},
            ).scalars()
        ]
        audit = audits[0] if len(audits) == 1 and isinstance(audits[0], dict) else None
        if audit is not None:
            expected = {
                "match_basis": "project_and_exact_body_and_verified_candidate_sha256",
                "canon_commit_id": job["canon_commit_id"],
                "candidate_id": job["candidate_id"],
                "chapter_plan_id": job["chapter_plan_id"],
                "chapter_number": job["chapter_number"],
                "body_sha256": digest,
                "original_job_title": job["chapter_title"],
                "accepted_candidate_title": job["accepted_title"],
                "title_matches": job["chapter_title"] == job["accepted_title"],
                "remote_state": "unknown",
            }
            if any(audit.get(key) != value for key, value in expected.items()):
                raise ValueError(
                    f"contradictory publication audit identity: {job['id']}"
                )
        public = any(r["official_state"] == "published" for r in receipts)
        if job["chapter_title"] != job["accepted_title"] and not (
            audit and job["status"] == "uncertain" and not public
        ):
            raise ValueError(f"unresolved publication title identity: {job['id']}")
        remotes = set()
        for receipt in receipts:
            if (
                receipt["attempt_job"] != job["id"]
                or receipt["platform_id"] != job["platform_id"]
                or receipt["content_sha256"] != digest
                or not receipt["remote_book_id"]
                or not receipt["remote_chapter_id"]
            ):
                raise ValueError(
                    f"contradictory publication receipt identity: {job['id']}"
                )
            remotes.add((receipt["remote_book_id"], receipt["remote_chapter_id"]))
        if len(remotes) > 1:
            raise ValueError(f"contradictory publication remote identity: {job['id']}")
        remote_book, remote_chapter = next(iter(remotes), ("", ""))
        evidence = {"migration_receipt_ids": [r["id"] for r in receipts]}
        if audit:
            evidence["legacy_content_identity"] = audit
        bind.execute(
            sa.text("""INSERT INTO canon_publication_protections
            (id,project_id,chapter_plan_id,chapter_number,canon_commit_id,upload_job_id,platform_id,
             content_sha256,state,remote_book_id,remote_chapter_id,evidence_json)
            VALUES (:id,:project,:chapter,:number,:commit,:job,:platform,:hash,:state,:book,:remote,:evidence)"""),
            {
                "id": uuid4().hex,
                "project": job["project_id"],
                "chapter": job["chapter_plan_id"],
                "number": job["canon_chapter"],
                "commit": job["canon_commit_id"],
                "job": job["id"],
                "platform": job["platform_id"],
                "hash": digest,
                "state": "published" if public else "reserved",
                "book": remote_book,
                "remote": remote_chapter,
                "evidence": json.dumps(evidence, sort_keys=True),
            },
        )
        proofs.append((job, receipts, audit))
    bindings = bind.execute(
        sa.text("""SELECT b.*, w.remote_book_id, w.project_id AS work_project,
            w.platform_id AS work_platform
        FROM publisher_chapter_bindings b JOIN publisher_work_bindings w ON w.id=b.work_binding_id
        WHERE b.project_id <> '' AND b.publish_state IN
        ('published','review_pending','submitted','drafted','unknown')""")
    ).mappings()
    for binding in bindings:
        matched = False
        for job, receipts, audit in proofs:
            if (
                binding["project_id"] != job["project_id"]
                or binding["work_project"] != job["project_id"]
                or binding["work_platform"] != job["platform_id"]
                or binding["chapter_number"] != job["canon_chapter"]
                or binding["platform_id"] != job["platform_id"]
                or (
                    binding["chapter_title"]
                    and binding["chapter_title"] != job["chapter_title"]
                )
            ):
                continue
            if any(
                r["remote_book_id"] == binding["remote_book_id"]
                and r["remote_chapter_id"] == binding["remote_chapter_id"]
                and (
                    binding["publish_state"] != "published"
                    or r["official_state"] == "published"
                )
                for r in receipts
            ):
                matched = True
            # A bridge audit proves a specific pending binding's ownership only.
            # It cannot stand in for a confirmed public fact or remote receipt.
            if (
                audit
                and job["status"] == "uncertain"
                and binding["publish_state"] != "published"
                and audit.get("binding_id") == binding["id"]
            ):
                matched = True
        if not matched:
            raise ValueError(
                f"unresolved publication binding identity: {binding['id']}"
            )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.scalar(
        sa.text("SELECT count(*) FROM canon_publication_protections")
    ) or bind.scalar(
        sa.text(
            "SELECT count(*) FROM canon_commit_records WHERE acceptance_revision > 1"
        )
    ):
        raise ValueError(
            "cannot downgrade revision/publication evidence; restore a verified backup"
        )
    op.drop_table("canon_publication_protections")
    op.drop_index("ux_chapter_plans_stable_number", table_name="chapter_plans")
    op.drop_constraint("fk_chapter_active_commit", "chapter_plans", type_="foreignkey")
    op.drop_constraint(
        "uq_canon_commit_chapter_identity", "canon_commit_records", type_="unique"
    )
    op.drop_constraint(
        "fk_canon_chapter_plan", "canon_commit_records", type_="foreignkey"
    )
    op.drop_index(
        "ux_canon_commits_chapter_revision", table_name="canon_commit_records"
    )
    op.drop_index("ix_canon_commits_candidate", table_name="canon_commit_records")
    for name in [
        "chapter_title",
        "production_mode",
        "base_book_revision",
        "acceptance_revision",
        "chapter_plan_id",
    ]:
        op.drop_column("canon_commit_records", name)
    op.drop_column("chapter_plans", "active_commit_id")
    op.drop_column("projects", "book_revision")
    op.create_index(
        "ux_canon_commits_candidate",
        "canon_commit_records",
        ["candidate_id"],
        unique=True,
    )
    op.create_index(
        "ux_canon_commits_project_chapter",
        "canon_commit_records",
        ["project_id", "chapter_number"],
        unique=True,
    )


def _accepted_title(row):
    plan = json.loads(row["canon_commit_plan_json"] or "{}")
    metadata = json.loads(row["metadata_json"] or "{}")
    titles = {
        str(value).strip()
        for value in [plan.get("chapter_title"), metadata.get("title")]
        if value
    }
    if len(titles) > 1:
        raise ValueError(f"ambiguous accepted title evidence: {row['id']}")
    return next(iter(titles)) if titles else row["plan_title"]
