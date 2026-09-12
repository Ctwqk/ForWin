from __future__ import annotations

import gzip
import hashlib
from pathlib import Path
from types import ModuleType

import pytest
from alembic import command
from alembic.migration import MigrationContext
from alembic.operations import Operations
from tests.test_v5_live_migration import EXPECTED_REVISION
from sqlalchemy import create_engine, inspect, text

from forwin.models.base import alembic_config, run_migrations
from tests.postgres import postgres_empty_test_url

FIXTURE = Path(__file__).parent / "fixtures/migrations/0001_v5_baseline.py.gz"
LEGACY_SHA256 = "e382386a1130878df4ee16280b85e1f4ed7b3c454fde9c38ccf2267cbfe2174f"


def _legacy_database(name: str):
    source = gzip.decompress(FIXTURE.read_bytes())
    assert hashlib.sha256(source).hexdigest() == LEGACY_SHA256
    module = ModuleType("legacy_fixture")
    exec(compile(source, str(FIXTURE), "exec"), module.__dict__)  # noqa: S102 — hash-verified historical fixture
    engine = create_engine(postgres_empty_test_url(name))
    with engine.begin() as conn:
        context = MigrationContext.configure(conn)
        with Operations.context(context):
            module.upgrade()
        conn.execute(
            text("CREATE TABLE alembic_version (version_num varchar(32) PRIMARY KEY)")
        )
        conn.execute(text("INSERT INTO alembic_version VALUES ('0001_v5_baseline')"))
    return engine


def test_legacy_upgrade_preserves_history_and_makes_current_schema_usable():
    engine = _legacy_database("legacy-forward")
    before = set(inspect(engine).get_table_names())
    with engine.begin() as conn:
        conn.execute(
            text("""INSERT INTO publisher_upload_jobs
            (id, project_id, platform_id, task_kind, status, book_name, chapter_title,
             body_text, upload_url, publish, abort_requested, extension_client_id,
             current_url, result_message, error_message, result_payload_json, created_at, updated_at)
            VALUES ('untouched', '', 'qidian', 'chapter_upload', 'pending', 'title', 'chapter',
                    'original body', '', false, false, '', '', '', '', '{}', now(), now())""")
        )
    run_migrations(engine.url.render_as_string(hide_password=False))
    after = inspect(engine)
    assert before <= set(after.get_table_names())
    assert {"publisher_upload_attempts", "canon_publication_protections"} <= set(
        after.get_table_names()
    )
    assert {"locked_by", "locked_at", "worker_id", "lease_epoch"} <= {
        c["name"] for c in after.get_columns("outbox_events")
    }
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT body_text, status, canon_commit_id, pause_reason FROM publisher_upload_jobs WHERE id='untouched'"
            )
        ).one()
        assert row.body_text == "original body"
        assert row.status == "paused"
        assert row.canon_commit_id is None
        assert row.pause_reason == "legacy_identity_unresolved"
        assert (
            conn.scalar(
                text(
                    "SELECT count(*) FROM publisher_operator_actions WHERE upload_job_id='untouched'"
                )
            )
            == 1
        )
        assert (
            conn.scalar(text("SELECT version_num FROM alembic_version"))
            == EXPECTED_REVISION
        )
    run_migrations(engine.url.render_as_string(hide_password=False))
    engine.dispose()


@pytest.mark.parametrize("status", ["succeeded", "running", "uncertain", "failed"])
def test_legacy_possible_publication_refuses_atomically(status):
    engine = _legacy_database("legacy-external")
    with engine.begin() as conn:
        conn.execute(
            text("""INSERT INTO publisher_upload_jobs
            (id, project_id, platform_id, task_kind, status, book_name, chapter_title,
             body_text, upload_url, publish, abort_requested, extension_client_id,
             started_at, current_url, result_message, error_message, result_payload_json, created_at, updated_at)
            VALUES ('external-effect', '', 'qidian', 'chapter_upload', :status, 'title', 'chapter',
                    'body', '', true, false, '', now(), '', '', '', '{}', now(), now())"""),
            {"status": status},
        )
    with pytest.raises(RuntimeError, match="external-effect"):
        run_migrations(engine.url.render_as_string(hide_password=False))
    assert "lease_epoch" not in {
        c["name"] for c in inspect(engine).get_columns("generation_tasks")
    }
    with engine.connect() as conn:
        assert (
            conn.scalar(text("SELECT version_num FROM alembic_version"))
            == "0001_v5_baseline"
        )
        assert conn.scalar(text("SELECT status FROM publisher_upload_jobs")) == status
    engine.dispose()


def test_legacy_anchor_cannot_create_a_fresh_database():
    engine = create_engine(postgres_empty_test_url("legacy-anchor"))
    config = alembic_config(engine.url.render_as_string(hide_password=False))
    config.set_main_option("script_location", "forwin:legacy_migrations")
    with pytest.raises(RuntimeError, match="existing legacy database"):
        command.upgrade(config, "head")
    assert set(inspect(engine).get_table_names()) <= {"alembic_version"}
    engine.dispose()


def _insert_legacy(conn, table_name, row_id, **values):
    from datetime import UTC, datetime

    import sqlalchemy as sa

    table = sa.Table(table_name, sa.MetaData(), autoload_with=conn)
    record = {"id": row_id, **values}
    for column in table.columns:
        if column.name in record or column.nullable or column.server_default:
            continue
        if isinstance(column.type, sa.Boolean):
            record[column.name] = False
        elif isinstance(column.type, sa.Integer):
            record[column.name] = 0
        elif isinstance(column.type, sa.Float):
            record[column.name] = 0.0
        elif isinstance(column.type, sa.DateTime):
            record[column.name] = datetime(2026, 9, 9, tzinfo=UTC).replace(tzinfo=None)
        else:
            record[column.name] = ""
    conn.execute(table.insert().values(**record))


def test_history_rows_and_old_outbox_leases_survive_new_orm_inserts():
    from sqlalchemy.orm import Session

    from forwin.models.outbox import OutboxEvent

    engine = _legacy_database("legacy-history")
    with engine.begin() as conn:
        _insert_legacy(conn, "projects", "project")
        _insert_legacy(
            conn,
            "world_lines",
            "old-history",
            project_id="project",
            world_line_id="original-line",
        )
        _insert_legacy(
            conn,
            "outbox_events",
            "old-event",
            event_id="old-event",
            status="completed",
            locked_by="former-worker",
        )
        original = (
            conn.execute(text("SELECT * FROM world_lines WHERE id='old-history'"))
            .mappings()
            .one()
        )
    run_migrations(engine.url.render_as_string(hide_password=False))
    with Session(engine) as session:
        session.add(
            OutboxEvent(id="new-event", event_id="new-event", event_type="test")
        )
        session.commit()
    with engine.connect() as conn:
        assert (
            conn.execute(text("SELECT * FROM world_lines WHERE id='old-history'"))
            .mappings()
            .one()
            == original
        )
        assert (
            conn.scalar(
                text("SELECT locked_by FROM outbox_events WHERE id='old-event'")
            )
            == "former-worker"
        )
        assert (
            conn.scalar(
                text("SELECT locked_by FROM outbox_events WHERE id='new-event'")
            )
            == ""
        )
    engine.dispose()


def test_cross_project_subworld_refuses_without_repairing_the_row():
    engine = _legacy_database("legacy-ownership")
    with engine.begin() as conn:
        _insert_legacy(conn, "projects", "owner")
        _insert_legacy(conn, "projects", "other")
        _insert_legacy(conn, "sub_worlds", "subworld", project_id="owner")
        _insert_legacy(
            conn,
            "map_generation_runs",
            "wrong-owner",
            project_id="other",
            subworld_id="subworld",
        )
    with pytest.raises(
        RuntimeError, match="cross-project subworld ownership.*wrong-owner"
    ):
        run_migrations(engine.url.render_as_string(hide_password=False))
    with engine.connect() as conn:
        assert (
            conn.scalar(
                text(
                    "SELECT project_id FROM map_generation_runs WHERE id='wrong-owner'"
                )
            )
            == "other"
        )
        assert (
            conn.scalar(text("SELECT version_num FROM alembic_version"))
            == "0001_v5_baseline"
        )
    engine.dispose()


@pytest.mark.parametrize(
    "table,status",
    [
        ("generation_tasks", "queued"),
        ("generation_tasks", "running"),
        ("outbox_events", "running"),
    ],
)
def test_active_work_requires_quiescence(table, status):
    engine = _legacy_database("legacy-quiescence")
    with engine.begin() as conn:
        _insert_legacy(conn, table, "active-work", status=status)
    with pytest.raises(RuntimeError, match="active work.*active-work"):
        run_migrations(engine.url.render_as_string(hide_password=False))
    engine.dispose()


def test_later_revision_rejection_rolls_back_bridge_and_job_pause():
    engine = _legacy_database("legacy-whole-transaction")
    with engine.begin() as conn:
        _insert_legacy(conn, "projects", "project")
        _insert_legacy(conn, "arc_plan_versions", "arc", project_id="project")
        for row_id in ("chapter-a", "chapter-b"):
            _insert_legacy(
                conn,
                "chapter_plans",
                row_id,
                project_id="project",
                arc_plan_id="arc",
                chapter_number=1,
            )
        _insert_legacy(
            conn,
            "publisher_upload_jobs",
            "pending-upload",
            task_kind="chapter_upload",
            status="pending",
            result_payload_json="{}",
        )
    with pytest.raises(
        Exception, match="(ambiguous|duplicate|ux_chapter_plans_stable_number)"
    ):
        run_migrations(engine.url.render_as_string(hide_password=False))
    with engine.connect() as conn:
        assert (
            conn.scalar(
                text(
                    "SELECT status FROM publisher_upload_jobs WHERE id='pending-upload'"
                )
            )
            == "pending"
        )
        assert (
            conn.scalar(text("SELECT version_num FROM alembic_version"))
            == "0001_v5_baseline"
        )
    assert "publisher_operator_actions" not in inspect(engine).get_table_names()
    engine.dispose()


def test_forward_schema_matches_fresh_recovery_columns_and_constraints():
    legacy = _legacy_database("legacy-schema-parity")
    config = alembic_config(legacy.url.render_as_string(hide_password=False))
    config.set_main_option("script_location", "forwin:legacy_migrations")
    command.upgrade(config, "head")
    fresh = create_engine(postgres_empty_test_url("fresh-recovery-parity"))
    command.upgrade(
        alembic_config(fresh.url.render_as_string(hide_password=False)),
        "0001_v5_recovery",
    )
    old, new = inspect(legacy), inspect(fresh)
    for table in new.get_table_names():
        old_cols = {c["name"]: c for c in old.get_columns(table)}
        for column in new.get_columns(table):
            migrated = old_cols[column["name"]]
            assert str(migrated["type"]) == str(column["type"]), (table, column["name"])
            assert migrated["nullable"] == column["nullable"], (table, column["name"])

        def fks(inspector, table=table):
            return {
                (
                    tuple(fk["constrained_columns"]),
                    fk["referred_table"],
                    tuple(fk["referred_columns"]),
                )
                for fk in inspector.get_foreign_keys(table)
            }

        assert fks(old) == fks(new), table

        def uniques(inspector, table=table):
            return {
                tuple(u["column_names"])
                for u in inspector.get_unique_constraints(table)
            }

        assert uniques(old) == uniques(new), table

        def indexes(inspector, table=table):
            return {
                (tuple(i["column_names"]), i["unique"])
                for i in inspector.get_indexes(table)
            }

        assert indexes(old) == indexes(new), table
    legacy.dispose()
    fresh.dispose()


def test_unreviewed_legacy_column_drift_is_not_stamped_as_recovery():
    engine = _legacy_database("legacy-schema-drift")
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE performance_spans DROP COLUMN duration_ms"))
    with pytest.raises(
        RuntimeError, match="schema differs from the audited legacy baseline"
    ):
        run_migrations(engine.url.render_as_string(hide_password=False))
    with engine.connect() as conn:
        assert (
            conn.scalar(text("SELECT version_num FROM alembic_version"))
            == "0001_v5_baseline"
        )
    engine.dispose()


def test_fresh_entrypoint_uses_main_chain_and_unknown_or_unstamped_stores_are_not_rebased():
    fresh = create_engine(postgres_empty_test_url("fresh-forward-entry"))
    run_migrations(fresh.url.render_as_string(hide_password=False))
    with fresh.begin() as conn:
        assert (
            conn.scalar(text("SELECT version_num FROM alembic_version"))
            == EXPECTED_REVISION
        )
        conn.execute(text("UPDATE alembic_version SET version_num='unknown_revision'"))
    from alembic.util.exc import CommandError

    with pytest.raises(CommandError, match="unknown_revision"):
        run_migrations(fresh.url.render_as_string(hide_password=False))
    with fresh.begin() as conn:
        assert (
            conn.scalar(text("SELECT version_num FROM alembic_version"))
            == "unknown_revision"
        )
        conn.execute(text("DELETE FROM alembic_version"))
    with pytest.raises(RuntimeError, match="Existing unstamped database"):
        run_migrations(fresh.url.render_as_string(hide_password=False))
    fresh.dispose()


def test_extra_historical_table_is_preserved_by_the_audited_bridge():
    engine = _legacy_database("legacy-extra-history")
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE npc_intent_snapshots (id text PRIMARY KEY, evidence text NOT NULL)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO npc_intent_snapshots VALUES ('historical', 'untouched evidence')"
            )
        )
    run_migrations(engine.url.render_as_string(hide_password=False))
    with engine.connect() as conn:
        assert (
            conn.scalar(
                text("SELECT evidence FROM npc_intent_snapshots WHERE id='historical'")
            )
            == "untouched evidence"
        )
    engine.dispose()


def _seed_legacy_accepted(
    conn, *, chapter=1, body="immutable exact body", title="Accepted title"
):
    import json

    if not conn.scalar(text("SELECT id FROM projects WHERE id='identity-project'")):
        _insert_legacy(conn, "projects", "identity-project")
        _insert_legacy(
            conn, "arc_plan_versions", "identity-arc", project_id="identity-project"
        )
    plan_id, draft_id, candidate_id, commit_id = (
        f"{kind}-{chapter}" for kind in ("plan", "draft", "candidate", "commit")
    )
    _insert_legacy(
        conn,
        "chapter_plans",
        plan_id,
        project_id="identity-project",
        arc_plan_id="identity-arc",
        chapter_number=chapter,
        title=title,
        status="accepted",
    )
    _insert_legacy(
        conn, "chapter_drafts", draft_id, chapter_plan_id=plan_id, body_text=body
    )
    _insert_legacy(
        conn, "chapter_reviews", f"review-{chapter}", draft_id=draft_id, verdict="pass"
    )
    _insert_legacy(
        conn,
        "candidate_draft_records",
        candidate_id,
        review_id=f"review-{chapter}",
        project_id="identity-project",
        chapter_plan_id=plan_id,
        chapter_number=chapter,
        candidate_draft_id=draft_id,
        body_hash=hashlib.sha256(body.encode()).hexdigest(),
        canon_commit_plan_json=json.dumps({"chapter_title": title}),
        metadata_json=json.dumps({"title": title}),
        canon_commit_id=commit_id,
        canon_status="committed",
        status="accepted",
    )
    _insert_legacy(
        conn,
        "canon_commit_records",
        commit_id,
        idempotency_key=f"canon-{chapter}",
        candidate_id=candidate_id,
        project_id="identity-project",
        chapter_number=chapter,
        status="committed",
        result_json="{}",
        graph_delta_ids_json="[]",
    )


def _seed_touched_legacy_upload(
    conn,
    *,
    job_id="old-upload",
    title="Original external title",
    binding_state="drafted",
):
    import json
    from datetime import UTC, datetime

    binding_id = "old-binding" if binding_state else ""
    payload = {"official_status": binding_state, "historical_marker": "unchanged"}
    if binding_id:
        _insert_legacy(
            conn,
            "publisher_work_bindings",
            "old-work",
            project_id="identity-project",
            platform_id="fanqie",
        )
        _insert_legacy(
            conn,
            "publisher_chapter_bindings",
            binding_id,
            work_binding_id="old-work",
            project_id="identity-project",
            platform_id="fanqie",
            chapter_number=0,
            chapter_title=title,
            publish_state=binding_state,
        )
        payload["chapter_binding"] = {
            "id": binding_id,
            "project_id": "identity-project",
            "chapter_title": title,
            "chapter_number": 0,
        }
    raw = json.dumps(payload)
    _insert_legacy(
        conn,
        "publisher_upload_jobs",
        job_id,
        project_id="identity-project",
        platform_id="fanqie",
        task_kind="chapter_upload",
        status="succeeded",
        chapter_title=title,
        body_text="immutable exact body",
        publish=False,
        started_at=datetime(2026, 9, 9, tzinfo=UTC).replace(tzinfo=None),
        result_payload_json=raw,
    )
    return raw


@pytest.mark.parametrize("binding_state", ["drafted", "submitted", "unknown"])
def test_exact_legacy_body_identity_preserves_title_difference_and_durable_freeze(
    binding_state,
):
    import json

    from sqlalchemy.orm import Session

    from forwin.models.base import get_session_factory
    from forwin.publisher_runtime.protection import require_revision_unprotected
    from forwin.publisher_runtime.service import PublisherRuntimeService

    engine = _legacy_database("legacy-protected-identity-" + binding_state)
    with engine.begin() as conn:
        _seed_legacy_accepted(conn)
        raw = _seed_touched_legacy_upload(conn, binding_state=binding_state)
    run_migrations(engine.url.render_as_string(hide_password=False))
    with engine.connect() as conn:
        job = (
            conn.execute(
                text("SELECT * FROM publisher_upload_jobs WHERE id='old-upload'")
            )
            .mappings()
            .one()
        )
        assert (job["canon_commit_id"], job["candidate_id"], job["chapter_number"]) == (
            "commit-1",
            "candidate-1",
            1,
        )
        assert job["status"] == "uncertain"
        assert job["pause_reason"] == "legacy_reconciliation_required"
        assert job["chapter_title"] == "Original external title"
        assert job["body_text"] == "immutable exact body"
        assert job["result_payload_json"] == raw
        assert (
            conn.scalar(
                text(
                    "SELECT chapter_title FROM canon_commit_records WHERE id='commit-1'"
                )
            )
            == "Accepted title"
        )
        assert (
            conn.scalar(
                text(
                    "SELECT chapter_number FROM publisher_chapter_bindings WHERE id='old-binding'"
                )
            )
            == 1
        )
        audit = conn.execute(
            text(
                "SELECT old_state_json,new_state_json FROM publisher_operator_actions WHERE action='legacy_content_identity'"
            )
        ).one()
        assert json.loads(audit.old_state_json)["status"] == "succeeded"
        identity = json.loads(audit.new_state_json)["legacy_content_identity"]
        assert identity["original_job_title"] == "Original external title"
        assert identity["accepted_candidate_title"] == "Accepted title"
        assert identity["title_matches"] is False
        assert identity["remote_state"] == "unknown"
        protection = (
            conn.execute(text("SELECT * FROM canon_publication_protections"))
            .mappings()
            .one()
        )
        assert protection["state"] == "reserved"
        assert (
            json.loads(protection["evidence_json"])["legacy_content_identity"]
            == identity
        )
        assert conn.scalar(text("SELECT count(*) FROM publisher_upload_receipts")) == 0
    runtime = PublisherRuntimeService(
        session_factory=get_session_factory(engine),
        extension_api_key="test",
        heartbeat_stale_seconds=90,
        preferred_client_id="",
        publisher_session_secret="",
        publisher_session_encryption_required=False,
    )
    assert (
        runtime.attempts.claim(
            client_id="migration-worker", connected_platforms=["fanqie"]
        )
        is None
    )
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM publisher_operator_actions"))
        conn.execute(text("DELETE FROM publisher_upload_jobs"))
        conn.execute(text("DELETE FROM publisher_chapter_bindings"))
    with (
        Session(engine) as session,
        pytest.raises(ValueError, match="publication protection"),
    ):
        require_revision_unprotected(
            session, project_id="identity-project", from_chapter=1
        )
    with engine.connect() as conn:
        assert (
            json.loads(
                conn.scalar(
                    text("SELECT evidence_json FROM canon_publication_protections")
                )
            )["legacy_content_identity"]
            == identity
        )
    engine.dispose()


def test_legacy_body_identity_does_not_guess_between_equal_bodies():
    engine = _legacy_database("legacy-ambiguous-body")
    with engine.begin() as conn:
        _seed_legacy_accepted(conn, chapter=1)
        _seed_legacy_accepted(conn, chapter=2, title="Different accepted title")
        _seed_touched_legacy_upload(conn, title="Accepted title", binding_state=None)
    with pytest.raises(RuntimeError, match="(ambiguous|unresolved).*old-upload"):
        run_migrations(engine.url.render_as_string(hide_password=False))
    with engine.connect() as conn:
        assert (
            conn.scalar(text("SELECT version_num FROM alembic_version"))
            == "0001_v5_baseline"
        )
    engine.dispose()


def test_legacy_published_binding_without_confirmed_receipt_still_refuses():
    engine = _legacy_database("legacy-published-unknown")
    with engine.begin() as conn:
        _seed_legacy_accepted(conn)
        _seed_touched_legacy_upload(conn, binding_state="published")
    with pytest.raises(RuntimeError, match="(unresolved|published)"):
        run_migrations(engine.url.render_as_string(hide_password=False))
    engine.dispose()


def test_binding_without_recorded_job_link_is_not_inferred_from_its_title():
    engine = _legacy_database("legacy-unlinked-binding")
    with engine.begin() as conn:
        _seed_legacy_accepted(conn)
        _seed_touched_legacy_upload(conn)
        conn.execute(text("UPDATE publisher_upload_jobs SET result_payload_json='{}'"))
    with pytest.raises(
        RuntimeError, match="unresolved external publication binding old-binding"
    ):
        run_migrations(engine.url.render_as_string(hide_password=False))
    with engine.connect() as conn:
        assert (
            conn.scalar(text("SELECT chapter_number FROM publisher_chapter_bindings"))
            == 0
        )
    engine.dispose()


def test_conflicting_binding_commit_mapping_is_not_chosen_by_latest_job():
    import json
    from datetime import UTC, datetime

    engine = _legacy_database("legacy-binding-conflict")
    with engine.begin() as conn:
        _seed_legacy_accepted(conn)
        raw = _seed_touched_legacy_upload(conn)
        _seed_legacy_accepted(conn, chapter=2, body="different immutable body")
        _insert_legacy(
            conn,
            "publisher_upload_jobs",
            "second-job",
            project_id="identity-project",
            platform_id="fanqie",
            task_kind="chapter_upload",
            status="succeeded",
            chapter_title="Original external title",
            body_text="different immutable body",
            started_at=datetime(2026, 9, 9, tzinfo=UTC).replace(tzinfo=None),
            result_payload_json=json.dumps(json.loads(raw)),
        )
    with pytest.raises(
        RuntimeError, match="ambiguous recorded publication binding old-binding"
    ):
        run_migrations(engine.url.render_as_string(hide_password=False))
    with engine.connect() as conn:
        assert (
            conn.scalar(text("SELECT version_num FROM alembic_version"))
            == "0001_v5_baseline"
        )
    engine.dispose()


def test_equal_draft_text_with_invalid_stored_candidate_hash_is_not_identity():
    engine = _legacy_database("legacy-body-hash-corruption")
    with engine.begin() as conn:
        _seed_legacy_accepted(conn)
        _seed_touched_legacy_upload(conn)
        conn.execute(
            text("UPDATE candidate_draft_records SET body_hash=:wrong"),
            {"wrong": "0" * 64},
        )
    with pytest.raises(RuntimeError, match="invalid immutable candidate body"):
        run_migrations(engine.url.render_as_string(hide_password=False))
    engine.dispose()
