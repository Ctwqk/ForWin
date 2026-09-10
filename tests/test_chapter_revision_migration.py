import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text

from tests.postgres import postgres_empty_test_url
from tests.test_v5_live_migration import _alembic_config


def _seed(connection, *, archived=False, ambiguous=False):
    # Baseline identities are deliberately seeded before the forward migration.
    from sqlalchemy import MetaData, Table

    from forwin.models import (
        ArcPlanVersion,
        ChapterDraft,
        ChapterPlan,
        ChapterReview,
        Project,
    )
    from forwin.models.canon import CanonCommitRecord
    from forwin.models.draft import CandidateDraftRecord

    rows = [
        (Project, {"id": "p", "title": "book", "premise": "story"}),
        (ArcPlanVersion, {"id": "a", "project_id": "p", "arc_synopsis": "arc"}),
        (
            ChapterPlan,
            {
                "id": "chapter",
                "project_id": "p",
                "arc_plan_id": "a",
                "chapter_number": 1,
                "status": "accepted",
            },
        ),
        (
            ChapterDraft,
            {
                "id": "draft",
                "chapter_plan_id": "chapter",
                "version": 1,
                "body_text": "body",
            },
        ),
        (ChapterReview, {"id": "review", "draft_id": "draft", "verdict": "pass"}),
        (
            CandidateDraftRecord,
            {
                "id": "candidate",
                "project_id": "p",
                "chapter_plan_id": "chapter",
                "chapter_number": 1,
                "candidate_draft_id": "draft",
                "review_id": "review",
                "status": "accepted",
            },
        ),
        (
            CanonCommitRecord,
            {
                "id": "commit",
                "project_id": "p",
                "candidate_id": "candidate",
                "chapter_number": -1 if archived else 1,
                "idempotency_key": "key",
                "status": "superseded" if archived else "committed",
                "result_json": "{}" if ambiguous else '{"original_chapter_number":1}',
            },
        ),
    ]
    for model, values in rows:
        table = Table(model.__tablename__, MetaData(), autoload_with=connection)
        for col in model.__table__.columns:
            if (
                col.name in table.c
                and col.name not in values
                and col.default is not None
            ):
                default = col.default
                if default.is_scalar or default.is_clause_element:
                    values[col.name] = default.arg
        connection.execute(table.insert().values(**values))


def _seed_publication(
    connection, *, body="body", receipt=True, remote_chapter="remote-1", title=""
):
    import hashlib

    from sqlalchemy import MetaData, Table

    from forwin.models.publisher import (
        PublisherChapterBinding,
        PublisherUploadAttempt,
        PublisherUploadJob,
        PublisherUploadReceipt,
        PublisherWorkBinding,
    )

    digest = hashlib.sha256(b"body").hexdigest()
    connection.execute(
        text("UPDATE candidate_draft_records SET body_hash=:hash"), {"hash": digest}
    )
    records = [
        (
            PublisherUploadJob,
            {
                "id": "job",
                "project_id": "p",
                "candidate_id": "candidate",
                "canon_commit_id": "commit",
                "chapter_number": 1,
                "platform_id": "qidian",
                "body_text": body,
                "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
                "chapter_title": title,
                "status": "uncertain",
            },
        ),
        (
            PublisherWorkBinding,
            {
                "id": "work",
                "project_id": "p",
                "platform_id": "qidian",
                "remote_book_id": "book-1",
            },
        ),
        (
            PublisherChapterBinding,
            {
                "id": "binding",
                "work_binding_id": "work",
                "project_id": "p",
                "platform_id": "qidian",
                "chapter_number": 1,
                "remote_chapter_id": "remote-1",
                "publish_state": "published",
            },
        ),
    ]
    if receipt:
        records.extend(
            [
                (
                    PublisherUploadAttempt,
                    {
                        "id": "attempt",
                        "upload_job_id": "job",
                        "attempt_number": 1,
                        "attempt_kind": "execute",
                    },
                ),
                (
                    PublisherUploadReceipt,
                    {
                        "id": "receipt",
                        "upload_job_id": "job",
                        "upload_attempt_id": "attempt",
                        "receipt_key": "receipt",
                        "platform_id": "qidian",
                        "remote_book_id": "book-1",
                        "remote_chapter_id": remote_chapter,
                        "official_state": "published",
                        "content_sha256": digest,
                    },
                ),
            ]
        )
    for model, values in records:
        table = Table(model.__tablename__, MetaData(), autoload_with=connection)
        for col in model.__table__.columns:
            if (
                col.name not in values
                and col.default is not None
                and (col.default.is_scalar or col.default.is_clause_element)
            ):
                values[col.name] = col.default.arg
        connection.execute(table.insert().values(**values))


@pytest.mark.parametrize(
    "problem",
    [
        "body",
        "title",
        "missing_receipt",
        "remote_identity",
        "binding_title",
        "work_platform",
    ],
)
def test_migration_refuses_unproven_published_binding_identity(problem):
    url = postgres_empty_test_url("revision-publication-proof")
    config = _alembic_config(url)
    command.upgrade(config, "0001_v5_recovery")
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            _seed(connection)
            _seed_publication(
                connection,
                body="different body" if problem == "body" else "body",
                receipt=problem != "missing_receipt",
                remote_chapter="other-remote"
                if problem == "remote_identity"
                else "remote-1",
                title="different title" if problem == "title" else "",
            )
            if problem == "binding_title":
                connection.execute(
                    text(
                        "UPDATE publisher_chapter_bindings SET chapter_title='other accepted title'"
                    )
                )
            if problem == "work_platform":
                connection.execute(
                    text("UPDATE publisher_work_bindings SET platform_id='fanqie'")
                )
        with pytest.raises(ValueError, match="publication.*identity"):
            command.upgrade(config, "0002_chapter_revisions")
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == "0001_v5_recovery"
            )
            assert (
                "canon_publication_protections"
                not in inspect(connection).get_table_names()
            )
    finally:
        engine.dispose()


def test_migration_preserves_proven_public_binding_as_durable_public_fact():
    url = postgres_empty_test_url("revision-publication-proof-valid")
    config = _alembic_config(url)
    command.upgrade(config, "0001_v5_recovery")
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            _seed(connection)
            _seed_publication(connection)
        command.upgrade(config, "0002_chapter_revisions")
        with engine.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT state,remote_book_id,remote_chapter_id FROM canon_publication_protections"
                )
            ).one() == ("published", "book-1", "remote-1")
    finally:
        engine.dispose()


@pytest.mark.parametrize("status", ["succeeded", "running", "terminating"])
def test_migration_reserves_unresolved_external_job_without_surviving_receipt(status):
    url = postgres_empty_test_url("revision-publication-lost-receipt")
    config = _alembic_config(url)
    command.upgrade(config, "0001_v5_recovery")
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            _seed(connection)
            _seed_publication(connection, receipt=False)
            connection.execute(text("DELETE FROM publisher_chapter_bindings"))
            connection.execute(
                text("UPDATE publisher_upload_jobs SET status=:status"),
                {"status": status},
            )
        command.upgrade(config, "0002_chapter_revisions")
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT state FROM canon_publication_protections")
                )
                == "reserved"
            )
    finally:
        engine.dispose()


@pytest.mark.parametrize("tamper", [False, True])
def test_migration_keeps_explicit_uncertain_title_evidence_without_claiming_publication(
    tamper,
):
    import hashlib
    import json

    from sqlalchemy import MetaData, Table, func

    url = postgres_empty_test_url("revision-publication-audit")
    config = _alembic_config(url)
    command.upgrade(config, "0001_v5_recovery")
    engine = create_engine(url)
    audit = {
        "match_basis": "project_and_exact_body_and_verified_candidate_sha256",
        "canon_commit_id": "commit",
        "candidate_id": "candidate",
        "chapter_plan_id": "chapter",
        "chapter_number": 1,
        "body_sha256": hashlib.sha256(b"body").hexdigest(),
        "original_job_title": "incorrect" if tamper else "remote title",
        "accepted_candidate_title": "",
        "title_matches": False,
        "remote_state": "unknown",
        "binding_id": "binding",
    }
    try:
        with engine.begin() as connection:
            _seed(connection)
            _seed_publication(connection, title="remote title", receipt=False)
            connection.execute(
                text("UPDATE publisher_chapter_bindings SET publish_state='drafted'")
            )
            table = Table(
                "publisher_operator_actions", MetaData(), autoload_with=connection
            )
            connection.execute(
                table.insert().values(
                    id="audit",
                    upload_job_id="job",
                    action="legacy_content_identity",
                    pause_token="audit",
                    actor_id="migration",
                    auth_method="migration",
                    reason="recovered body identity only",
                    old_state_json="{}",
                    new_state_json=json.dumps({"legacy_content_identity": audit}),
                    occurred_at=func.now(),
                    created_at=func.now(),
                )
            )
        if tamper:
            with pytest.raises(ValueError, match="publication audit identity"):
                command.upgrade(config, "0002_chapter_revisions")
        else:
            command.upgrade(config, "0002_chapter_revisions")
            with engine.begin() as connection:
                connection.execute(text("DELETE FROM publisher_operator_actions"))
                connection.execute(text("DELETE FROM publisher_upload_jobs"))
                row = connection.execute(
                    text(
                        "SELECT state,evidence_json FROM canon_publication_protections"
                    )
                ).one()
                assert row.state == "reserved"
                assert json.loads(row.evidence_json)["legacy_content_identity"] == audit
    finally:
        engine.dispose()


def test_forward_migration_preserves_stable_chapter_and_receipt_reference_identity():
    url = postgres_empty_test_url("revision-migration")
    config = _alembic_config(url)
    command.upgrade(config, "0001_v5_recovery")
    engine = create_engine(url)
    with engine.begin() as connection:
        _seed(connection)
    command.upgrade(config, "0002_chapter_revisions")
    with engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT chapter_plan_id, chapter_number, acceptance_revision FROM canon_commit_records"
            )
        ).one() == ("chapter", 1, 1)
        assert (
            connection.scalar(text("SELECT active_commit_id FROM chapter_plans"))
            == "commit"
        )
        assert connection.scalar(text("SELECT book_revision FROM projects")) == 1
        assert "canon_publication_protections" in inspect(connection).get_table_names()
    engine.dispose()


def test_forward_migration_rejects_ambiguous_negative_archive_without_rewriting_baseline():
    url = postgres_empty_test_url("revision-migration-ambiguous")
    config = _alembic_config(url)
    command.upgrade(config, "0001_v5_recovery")
    engine = create_engine(url)
    with engine.begin() as connection:
        _seed(connection, archived=True, ambiguous=True)
    with pytest.raises(Exception, match="ambiguous archived"):
        command.upgrade(config, "0002_chapter_revisions")
    with engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT chapter_number FROM canon_commit_records"))
            == -1
        )
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version"))
            == "0001_v5_recovery"
        )
    engine.dispose()


@pytest.mark.parametrize(
    "problem",
    [
        "archive_without_audit",
        "missing_delta",
        "unresolved_public_job",
        "unowned_submitted_binding",
    ],
)
def test_forward_migration_stops_on_unrecoverable_evidence(problem):
    url = postgres_empty_test_url("revision-evidence")
    config = _alembic_config(url)
    command.upgrade(config, "0001_v5_recovery")
    engine = create_engine(url)
    with engine.begin() as connection:
        _seed(connection, archived=problem == "archive_without_audit")
        if problem == "missing_delta":
            connection.execute(
                text(
                    """UPDATE canon_commit_records SET graph_delta_ids_json='["missing"]' """
                )
            )
        elif problem == "unowned_submitted_binding":
            from sqlalchemy import MetaData, Table

            from forwin.models.publisher import (
                PublisherChapterBinding,
                PublisherWorkBinding,
            )

            for model, values in [
                (
                    PublisherWorkBinding,
                    {"id": "work", "project_id": "p", "platform_id": "qidian"},
                ),
                (
                    PublisherChapterBinding,
                    {
                        "id": "submitted-chapter",
                        "work_binding_id": "work",
                        "project_id": "p",
                        "platform_id": "qidian",
                        "chapter_number": 1,
                        "publish_state": "submitted",
                    },
                ),
            ]:
                table = Table(model.__tablename__, MetaData(), autoload_with=connection)
                for col in model.__table__.columns:
                    if (
                        col.name not in values
                        and col.default is not None
                        and (col.default.is_scalar or col.default.is_clause_element)
                    ):
                        values[col.name] = col.default.arg
                connection.execute(table.insert().values(**values))
        elif problem == "unresolved_public_job":
            from sqlalchemy import MetaData, Table

            from forwin.models.publisher import PublisherUploadJob

            table = Table("publisher_upload_jobs", MetaData(), autoload_with=connection)
            values = {
                "id": "legacy-job",
                "project_id": "p",
                "chapter_number": 1,
                "platform_id": "qidian",
                "body_text": "body",
                "status": "succeeded",
            }
            for col in PublisherUploadJob.__table__.columns:
                if (
                    col.name not in values
                    and col.default is not None
                    and (col.default.is_scalar or col.default.is_clause_element)
                ):
                    values[col.name] = col.default.arg
            connection.execute(table.insert().values(**values))
    with pytest.raises(
        Exception,
        match="ambiguous archived|missing Canon evidence|unresolved publication",
    ):
        command.upgrade(config, "0002_chapter_revisions")
    with engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version"))
            == "0001_v5_recovery"
        )
    engine.dispose()
