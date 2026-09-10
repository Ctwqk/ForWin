"""Preserve legacy data while adding the recovery schema.

Revision ID: 0001_v5_recovery
Revises: 0001_v5_baseline

Source schema: Ctwqk/ForWin@57241ff0e4e2, original baseline SHA256
 e382386a1130878df4ee16280b85e1f4ed7b3c454fde9c38ccf2267cbfe2174f.
Target additions are frozen from the existing 0001_v5_recovery migration.
Neither baseline is rewritten. Removed legacy tables remain audit history.
"""

import hashlib
import json
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0001_v5_recovery"
down_revision = "0001_v5_baseline"
branch_labels = None
depends_on = None

SCOPED_TABLES = (
    "map_generation_runs",
    "map_region_edges",
    "map_regions",
    "sub_world_roster_items",
)


# The 99 known tables are checked before any data/DDL mutation. Extra historical
# tables (including the deployed npc_intent_snapshots) remain untouched.
LEGACY_SCHEMA_FINGERPRINT = (
    "9f84777a49ba98382659f762494552409c56f0636ce77611fbedcdb75a90495d"
)
LEGACY_TABLES = (
    "arc_envelope_analyses",
    "arc_envelopes",
    "arc_plan_versions",
    "arc_structure_drafts",
    "arc_world_contracts",
    "artifact_collection_ledgers",
    "band_checkpoints",
    "band_experience_plans",
    "band_world_contracts",
    "beliefs",
    "book_cognition_snapshots",
    "book_genesis_revisions",
    "book_reader_experience_deltas",
    "book_reader_promises",
    "candidate_draft_records",
    "canon_admission_runs",
    "canon_commit_records",
    "canon_quality_signals",
    "chapter_body_metrics",
    "chapter_drafts",
    "chapter_plans",
    "chapter_reviews",
    "chapter_rewrite_attempts",
    "chapter_world_delta_intents",
    "character_identity_map",
    "character_state_transitions",
    "cognition_overlay_patches",
    "cognition_overlays",
    "cognition_snapshots",
    "comment_signal_candidates",
    "countdown_ledgers",
    "decision_events",
    "entities",
    "entity_aliases",
    "fact_nodes",
    "feedback_action_records",
    "future_plan_audit_runs",
    "generation_tasks",
    "graph_delta_patches",
    "graph_deltas",
    "knowledge_edit_proposals",
    "knowledge_gaps",
    "knowledge_projection_pages",
    "knowledge_update_events",
    "map_edges",
    "map_generation_runs",
    "map_nodes",
    "map_region_edges",
    "map_regions",
    "map_snapshots",
    "narrative_constraints",
    "narrative_edges",
    "narrative_nodes",
    "narrative_obligations",
    "narrative_plan_patches",
    "outbox_events",
    "performance_spans",
    "project_progression_rules",
    "project_replan_events",
    "project_stage_analyses",
    "projects",
    "prompt_traces",
    "provisional_band_executions",
    "provisional_chapter_ledgers",
    "provisional_promotion_records",
    "publisher_browser_session_entries",
    "publisher_browser_sessions",
    "publisher_chapter_bindings",
    "publisher_comment_sync_jobs",
    "publisher_connection_states",
    "publisher_cover_assets",
    "publisher_extension_clients",
    "publisher_extension_platform_states",
    "publisher_milestones",
    "publisher_raw_comments",
    "publisher_upload_jobs",
    "publisher_work_bindings",
    "quality_analysis_runs",
    "reader_experience_deltas",
    "reader_scale_snapshots",
    "reveal_events",
    "reveal_registry_entries",
    "scenario_plan_patches",
    "scenario_rehearsal_runs",
    "signal_window_aggregates",
    "story_obligations",
    "sub_world_roster_items",
    "sub_worlds",
    "trope_usage_records",
    "world_compile_runs_v4",
    "world_deltas",
    "world_edges",
    "world_lines",
    "world_model_snapshots_v4",
    "world_node_states",
    "world_nodes",
    "world_projection_deltas",
    "world_simulation_turns",
    "world_snapshots",
)


def _refuse(reason):
    raise RuntimeError(
        f"Legacy migration refused: {reason}. Preserve the backup and resolve this through the supported operator workflow before retrying."
    )


def _preflight(bind):
    inspector = sa.inspect(bind)
    missing = set(LEGACY_TABLES) - set(inspector.get_table_names())
    if missing:
        _refuse(f"legacy schema is missing tables: {sorted(missing)}")
    for table, column in (
        ("generation_tasks", "lease_epoch"),
        ("publisher_upload_jobs", "canon_commit_id"),
    ):
        if column in {c["name"] for c in inspector.get_columns(table)}:
            _refuse(f"unexpected partial recovery schema at {table}.{column}")
    # Lock before checking, so a waiting old worker cannot claim between the
    # read and schema change. Runtime roles must be quiesced during deployment.
    bind.execute(
        sa.text("LOCK TABLE " + ", ".join(LEGACY_TABLES) + " IN ACCESS EXCLUSIVE MODE")
    )
    contract = {
        table: {
            "columns": sorted(
                (column["name"], str(column["type"]), column["nullable"])
                for column in inspector.get_columns(table)
            ),
            "foreign_keys": sorted(
                (
                    key["constrained_columns"],
                    key["referred_table"],
                    key["referred_columns"],
                )
                for key in inspector.get_foreign_keys(table)
            ),
        }
        for table in LEGACY_TABLES
    }
    fingerprint = hashlib.sha256(
        json.dumps(contract, sort_keys=True).encode()
    ).hexdigest()
    if fingerprint != LEGACY_SCHEMA_FINGERPRINT:
        _refuse(
            f"schema differs from the audited legacy baseline (observed {fingerprint})"
        )
    for table, condition in (
        ("generation_tasks", "status IN ('queued','running','capacity_wait')"),
        ("outbox_events", "status IN ('running','processing')"),
    ):
        active = bind.scalar(
            sa.text(f"SELECT id FROM {table} WHERE {condition} LIMIT 1")
        )
        if active:
            _refuse(f"active work in {table}: {active}")
    for table in SCOPED_TABLES:
        invalid = bind.scalar(
            sa.text(f"""SELECT child.id FROM {table} child
            LEFT JOIN sub_worlds parent ON parent.id=child.subworld_id
            WHERE parent.id IS NULL OR child.project_id<>parent.project_id LIMIT 1""")
        )
        if invalid:
            _refuse(f"cross-project subworld ownership in {table}: {invalid}")
    duplicate = bind.execute(
        sa.text("""SELECT project_id, chapter_number
        FROM canon_commit_records GROUP BY project_id, chapter_number
        HAVING count(*) > 1 LIMIT 1""")
    ).first()
    if duplicate:
        _refuse(
            f"ambiguous legacy Canon identity {duplicate.project_id}/{duplicate.chapter_number}"
        )

    return _recover_content_identities(bind)


def _json_object(raw, *, subject):
    try:
        value = json.loads(raw or "{}")
    except (ValueError, TypeError):
        _refuse(f"invalid identity evidence JSON for {subject}")
    if not isinstance(value, dict):
        _refuse(f"invalid identity evidence object for {subject}")
    return value


def _recover_content_identities(bind):
    # A failed/cancelled upload may already have mutated the platform. Its
    # original state is retained in audit; no status becomes a public receipt.
    jobs = list(
        bind.execute(
            sa.text("""SELECT * FROM publisher_upload_jobs
        WHERE task_kind='chapter_upload' AND (
            status NOT IN ('pending','scheduled','cancelled','aborted','paused')
            OR claimed_at IS NOT NULL OR started_at IS NOT NULL OR finished_at IS NOT NULL
            OR current_url <> '' OR result_payload_json NOT IN ('', '{}'))
        ORDER BY id""")
        ).mappings()
    )
    bindings = {
        row["id"]: row
        for row in bind.execute(
            sa.text("""SELECT b.*, w.project_id AS work_project,
        w.platform_id AS work_platform FROM publisher_chapter_bindings b
        LEFT JOIN publisher_work_bindings w ON w.id=b.work_binding_id""")
        ).mappings()
    }
    identities, binding_updates = {}, {}
    for job in jobs:
        matches = list(
            bind.execute(
                sa.text("""SELECT c.id AS commit_id, c.project_id,
            c.chapter_number AS canon_chapter, d.id AS candidate_id, d.body_hash,
            d.chapter_plan_id, d.chapter_number AS candidate_chapter,
            d.canon_commit_plan_json, d.metadata_json, p.project_id AS plan_project,
            p.chapter_number AS plan_chapter, draft.body_text
            FROM canon_commit_records c JOIN candidate_draft_records d ON d.id=c.candidate_id
            JOIN chapter_drafts draft ON draft.id=d.candidate_draft_id AND draft.chapter_plan_id=d.chapter_plan_id
            JOIN chapter_plans p ON p.id=d.chapter_plan_id
            WHERE c.project_id=:project AND d.project_id=:project AND c.status='committed'
                AND draft.body_text=:body"""),
                {"project": job["project_id"], "body": job["body_text"]},
            ).mappings()
        )
        if len(matches) != 1 or not job["project_id"]:
            _refuse(
                f"{'ambiguous' if len(matches) > 1 else 'unresolved'} external publication body identity for job {job['id']}"
            )
        match = matches[0]
        digest = hashlib.sha256(job["body_text"].encode()).hexdigest()
        if (
            match["body_text"] != job["body_text"]
            or match["body_hash"] != digest
            or match["plan_project"] != job["project_id"]
            or not (
                match["canon_chapter"]
                == match["candidate_chapter"]
                == match["plan_chapter"]
                > 0
            )
        ):
            _refuse(
                f"invalid immutable candidate body or chapter ownership for job {job['id']}"
            )
        plan = _json_object(match["canon_commit_plan_json"], subject=job["id"])
        metadata = _json_object(match["metadata_json"], subject=job["id"])
        titles = {
            str(title).strip()
            for title in (plan.get("chapter_title"), metadata.get("title"))
            if title
        }
        if len(titles) != 1:
            _refuse(f"unresolved immutable candidate title for job {job['id']}")
        title = next(iter(titles))
        identity = {
            "match_basis": "project_and_exact_body_and_verified_candidate_sha256",
            "canon_commit_id": match["commit_id"],
            "candidate_id": match["candidate_id"],
            "chapter_plan_id": match["chapter_plan_id"],
            "chapter_number": match["canon_chapter"],
            "body_sha256": digest,
            "original_job_title": job["chapter_title"],
            "accepted_candidate_title": title,
            "title_matches": title == job["chapter_title"],
            "remote_state": "unknown",
            "binding_id": "",
            "original_binding_chapter_number": None,
        }
        payload = _json_object(job["result_payload_json"], subject=job["id"])
        snapshot = payload.get("chapter_binding")
        if isinstance(snapshot, dict) and snapshot.get("id"):
            binding_id = str(snapshot["id"])
            binding = bindings.get(binding_id)
            if binding is None:
                _refuse(
                    f"unresolved recorded publication binding {binding_id} for job {job['id']}"
                )
            if (
                binding["project_id"] != job["project_id"]
                or binding["platform_id"] != job["platform_id"]
                or binding["work_project"] != job["project_id"]
                or binding["work_platform"] != job["platform_id"]
                or snapshot.get("project_id", job["project_id"]) != job["project_id"]
                or snapshot.get("chapter_title", job["chapter_title"])
                != job["chapter_title"]
                or binding["chapter_title"] != job["chapter_title"]
                or binding["chapter_number"] not in (0, match["canon_chapter"])
                or snapshot.get("chapter_number", 0) not in (0, match["canon_chapter"])
            ):
                _refuse(
                    f"conflicting recorded publication binding {binding_id} for job {job['id']}"
                )
            prior = binding_updates.get(binding_id)
            if prior and prior["canon_commit_id"] != match["commit_id"]:
                _refuse(f"ambiguous recorded publication binding {binding_id}")
            binding_updates[binding_id] = {
                "canon_commit_id": match["commit_id"],
                "chapter_number": match["canon_chapter"],
            }
            identity["binding_id"] = binding_id
            identity["original_binding_chapter_number"] = binding["chapter_number"]
        identities[job["id"]] = identity
    for binding in bindings.values():
        if binding["publish_state"] == "published":
            _refuse(
                f"published legacy binding has no verified immutable receipt: {binding['id']}"
            )
        if (
            binding["publish_state"]
            in {"submitted", "review_pending", "drafted", "unknown"}
            and binding["id"] not in binding_updates
        ):
            _refuse(f"unresolved external publication binding {binding['id']}")
    return identities, binding_updates


def upgrade():
    bind = op.get_bind()
    identities, binding_updates = _preflight(bind)
    op.add_column(
        "generation_tasks",
        sa.Column("lease_epoch", sa.Integer(), nullable=False, server_default="0"),
    )
    op.alter_column("generation_tasks", "lease_epoch", server_default=None)
    for column in (
        sa.Column("worker_id", sa.String(), nullable=False, server_default=""),
        sa.Column("lease_epoch", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(), nullable=True),
    ):
        op.add_column("outbox_events", column)
    # Preserve old lease evidence without making new ORM inserts supply it.
    op.alter_column("outbox_events", "locked_by", server_default="")
    op.create_index(
        "ix_outbox_events_status_lease_expires",
        "outbox_events",
        ["status", "lease_expires_at", "created_at"],
    )
    for column in (
        sa.Column("canon_commit_id", sa.String(), nullable=True),
        sa.Column("candidate_id", sa.String(), nullable=False, server_default=""),
        sa.Column("chapter_number", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("idempotency_key", sa.String(), nullable=False, server_default=""),
        sa.Column("body_sha256", sa.String(), nullable=False, server_default=""),
        sa.Column("current_attempt_id", sa.String(), nullable=False, server_default=""),
        sa.Column("available_at", sa.DateTime(), nullable=True),
        sa.Column("reconcile_after", sa.DateTime(), nullable=True),
        sa.Column("paused_at", sa.DateTime(), nullable=True),
        sa.Column("pause_reason", sa.String(), nullable=False, server_default=""),
    ):
        op.add_column("publisher_upload_jobs", column)
    op.create_foreign_key(
        "fk_publisher_upload_jobs_canon_commit_id",
        "publisher_upload_jobs",
        "canon_commit_records",
        ["canon_commit_id"],
        ["id"],
    )
    op.create_index(
        "ux_publisher_upload_jobs_idempotency_key",
        "publisher_upload_jobs",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key <> ''"),
    )
    op.create_unique_constraint(
        "uq_sub_worlds_project_id_id", "sub_worlds", ["project_id", "id"]
    )
    for table in SCOPED_TABLES:
        for constraint in sa.inspect(bind).get_foreign_keys(table):
            if constraint["referred_table"] == "sub_worlds" and constraint[
                "constrained_columns"
            ] == ["subworld_id"]:
                op.drop_constraint(constraint["name"], table, type_="foreignkey")
        op.create_foreign_key(
            f"fk_{table}_project_subworld",
            table,
            "sub_worlds",
            ["project_id", "subworld_id"],
            ["project_id", "id"],
        )
    op.create_index(
        "ux_canon_commits_project_chapter",
        "canon_commit_records",
        ["project_id", "chapter_number"],
        unique=True,
    )
    op.drop_index("ix_canon_commits_project_chapter", table_name="canon_commit_records")
    op.create_table(
        "projection_checkpoints",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("projection_kind", sa.String(), nullable=False),
        sa.Column("status", sa.String(), server_default="never", nullable=False),
        sa.Column("target_canon_commit_id", sa.String(), nullable=True),
        sa.Column(
            "target_chapter_number",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("projected_canon_commit_id", sa.String(), nullable=True),
        sa.Column(
            "projected_chapter_number",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("last_event_id", sa.String(), server_default="", nullable=False),
        sa.Column("source_digest", sa.String(), server_default="", nullable=False),
        sa.Column("last_error", sa.Text(), server_default="", nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
        ),
        sa.ForeignKeyConstraint(
            ["projected_canon_commit_id"],
            ["canon_commit_records.id"],
        ),
        sa.ForeignKeyConstraint(
            ["target_canon_commit_id"],
            ["canon_commit_records.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id",
            "projection_kind",
            name="uq_projection_checkpoints_project_kind",
        ),
    )
    op.create_index(
        "ix_projection_checkpoints_project_status",
        "projection_checkpoints",
        ["project_id", "status", "updated_at"],
        unique=False,
    )
    op.create_index(
        "ix_projection_checkpoints_status_updated",
        "projection_checkpoints",
        ["status", "updated_at"],
        unique=False,
    )
    op.create_table(
        "post_canon_maintenance_runs",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("canon_commit_id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("chapter_number", sa.Integer(), nullable=False),
        sa.Column("candidate_id", sa.String(), nullable=False),
        sa.Column("step_name", sa.String(), nullable=False),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("status", sa.String(), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("available_at", sa.DateTime(), nullable=True),
        sa.Column("worker_id", sa.String(), server_default="", nullable=False),
        sa.Column("lease_epoch", sa.Integer(), server_default="0", nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(), nullable=True),
        sa.Column("result_json", sa.Text(), server_default="{}", nullable=False),
        sa.Column("last_error", sa.Text(), server_default="", nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["candidate_draft_records.id"],
        ),
        sa.ForeignKeyConstraint(
            ["canon_commit_id"],
            ["canon_commit_records.id"],
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "canon_commit_id",
            "step_name",
            name="uq_post_canon_maintenance_canon_step",
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_post_canon_maintenance_idempotency_key",
        ),
    )
    op.create_index(
        "ix_post_canon_maintenance_project_chapter_status",
        "post_canon_maintenance_runs",
        ["project_id", "chapter_number", "status"],
        unique=False,
    )
    op.create_index(
        "ix_post_canon_maintenance_status_available",
        "post_canon_maintenance_runs",
        ["status", "available_at", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_post_canon_maintenance_status_lease_expires",
        "post_canon_maintenance_runs",
        ["status", "lease_expires_at", "created_at"],
        unique=False,
    )
    op.create_table(
        "publisher_upload_attempts",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("upload_job_id", sa.String(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("attempt_kind", sa.String(), nullable=False),
        sa.Column("worker_id", sa.String(), server_default="", nullable=False),
        sa.Column("lease_epoch", sa.Integer(), server_default="0", nullable=False),
        sa.Column("status", sa.String(), server_default="pending", nullable=False),
        sa.Column("phase", sa.String(), server_default="", nullable=False),
        sa.Column("claimed_at", sa.DateTime(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("content_sha256", sa.String(), server_default="", nullable=False),
        sa.Column("error_code", sa.String(), server_default="", nullable=False),
        sa.Column("error_message", sa.Text(), server_default="", nullable=False),
        sa.Column("result_json", sa.Text(), server_default="{}", nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["upload_job_id"],
            ["publisher_upload_jobs.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "upload_job_id",
            "attempt_number",
            name="uq_publisher_upload_attempts_job_number",
        ),
    )
    op.create_index(
        "ix_publisher_upload_attempts_job_status",
        "publisher_upload_attempts",
        ["upload_job_id", "status", "attempt_number"],
        unique=False,
    )
    op.create_index(
        "ix_publisher_upload_attempts_status_lease_expires",
        "publisher_upload_attempts",
        ["status", "lease_expires_at", "created_at"],
        unique=False,
    )
    op.create_table(
        "publisher_upload_receipts",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("upload_job_id", sa.String(), nullable=False),
        sa.Column("upload_attempt_id", sa.String(), nullable=False),
        sa.Column("receipt_key", sa.String(), nullable=False),
        sa.Column("idempotency_key", sa.String(), server_default="", nullable=False),
        sa.Column("platform_id", sa.String(), server_default="", nullable=False),
        sa.Column("remote_book_id", sa.String(), server_default="", nullable=False),
        sa.Column("remote_chapter_id", sa.String(), server_default="", nullable=False),
        sa.Column("remote_url", sa.String(), server_default="", nullable=False),
        sa.Column("official_state", sa.String(), server_default="", nullable=False),
        sa.Column("content_sha256", sa.String(), server_default="", nullable=False),
        sa.Column("evidence_json", sa.Text(), server_default="{}", nullable=False),
        sa.Column("source", sa.String(), server_default="", nullable=False),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["upload_attempt_id"],
            ["publisher_upload_attempts.id"],
        ),
        sa.ForeignKeyConstraint(
            ["upload_job_id"],
            ["publisher_upload_jobs.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "receipt_key",
            name="uq_publisher_upload_receipts_receipt_key",
        ),
    )
    op.create_index(
        "ix_publisher_upload_receipts_idempotency_key",
        "publisher_upload_receipts",
        ["idempotency_key"],
        unique=False,
    )
    op.create_index(
        "ix_publisher_upload_receipts_job_created",
        "publisher_upload_receipts",
        ["upload_job_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_publisher_upload_receipts_platform_remote",
        "publisher_upload_receipts",
        ["platform_id", "remote_book_id", "remote_chapter_id"],
        unique=False,
    )
    op.create_table(
        "publisher_operator_actions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("upload_job_id", sa.String(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("pause_token", sa.String(), nullable=False),
        sa.Column("actor_id", sa.String(), nullable=False),
        sa.Column("auth_method", sa.String(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("old_state_json", sa.Text(), nullable=False),
        sa.Column("new_state_json", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["upload_job_id"],
            ["publisher_upload_jobs.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "upload_job_id",
            "action",
            "pause_token",
            name="uq_publisher_operator_actions_job_action_pause",
        ),
    )
    op.create_index(
        "ix_publisher_operator_actions_job_created",
        "publisher_operator_actions",
        ["upload_job_id", "created_at"],
        unique=False,
    )
    now = datetime.now(UTC).replace(tzinfo=None)
    for binding_id, update in binding_updates.items():
        bind.execute(
            sa.text(
                "UPDATE publisher_chapter_bindings SET chapter_number=:number WHERE id=:id"
            ),
            {"id": binding_id, "number": update["chapter_number"]},
        )
    for row in bind.execute(
        sa.text(
            "SELECT id, status, body_text, chapter_title, publish FROM publisher_upload_jobs WHERE task_kind='chapter_upload'"
        )
    ).mappings():
        identity = identities.get(row["id"])
        if identity is not None:
            status, reason, action = (
                "uncertain",
                "legacy_reconciliation_required",
                "legacy_content_identity",
            )
            bind.execute(
                sa.text("""UPDATE publisher_upload_jobs SET canon_commit_id=:commit,
                candidate_id=:candidate, chapter_number=:number WHERE id=:id"""),
                {
                    "id": row["id"],
                    "commit": identity["canon_commit_id"],
                    "candidate": identity["candidate_id"],
                    "number": identity["chapter_number"],
                },
            )
        else:
            status, reason, action = (
                "paused",
                "legacy_identity_unresolved",
                "legacy_migration_pause",
            )
        bind.execute(
            sa.text("""UPDATE publisher_upload_jobs SET status=:status,
            paused_at=:now, pause_reason=:reason, body_sha256=:digest WHERE id=:id"""),
            {
                "id": row["id"],
                "now": now,
                "status": status,
                "reason": reason,
                "digest": hashlib.sha256(row["body_text"].encode()).hexdigest(),
            },
        )
        new_state = {"status": status, "pause_reason": reason}
        if identity is not None:
            new_state["legacy_content_identity"] = identity
        bind.execute(
            sa.text("""INSERT INTO publisher_operator_actions
            (id, upload_job_id, action, pause_token, actor_id, auth_method, reason,
             old_state_json, new_state_json, occurred_at, created_at)
            VALUES (:id, :job, :action, :token, 'alembic', 'migration',
                    'Preserve historical external payload; require explicit reconciliation',
                    :old, :new, :now, :now)"""),
            {
                "id": "legacy-pause-" + row["id"],
                "job": row["id"],
                "token": "legacy-" + row["id"],
                "action": action,
                "old": json.dumps(
                    {
                        "status": row["status"],
                        "chapter_title": row["chapter_title"],
                        "publish": row["publish"],
                    }
                ),
                "new": json.dumps(new_state, ensure_ascii=False, sort_keys=True),
                "now": now,
            },
        )


def downgrade():
    raise RuntimeError(
        "Legacy bridge is forward-only; restore a verified backup for rollback."
    )
